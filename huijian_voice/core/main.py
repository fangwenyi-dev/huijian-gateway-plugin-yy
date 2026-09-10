"""huijian_voice 服务装配根。

进程内组件：aiohttp(:8000 WS 三通道 + 127.0.0.1:8002 管理 API)、模型引擎（惰性/
预热双态）、HA 桥、mDNS、事实文件回写（nginx 静态目录）。bashio/Supervisor 只负责
进程守护与配置注入（run.sh → env），Python 侧不再感知 Supervisor 细节。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

from aiohttp import web

from . import const
from .admin_api import make_admin_app
from .agent import Agent
from .asr import AsrEngine
from .executor import Executor
from .ha_client import HAClient
from .mdns import Publisher, local_ip
from .model_store import ModelStore
from .nlu.klar_client import KlarClient
from .nlu.query import QueryZone  # noqa: F401  (pipeline 内部实例化，此处仅保证包完整)
from .nlu.scenes import SceneCache
from .nlu.textcnn import TextCNN
from .pipeline import Pipeline
from .settings import Settings
from .tts import TtsEngine
from .ws_server import AppContext, make_ws_app

logger = logging.getLogger("huijian.main")


def _setup_logging() -> None:
    level = os.environ.get("HUIJIAN_OPT_LOG_LEVEL", "info").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        stream=sys.stdout)
    for noisy in ("aiohttp.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class Service:
    def __init__(self):
        self.settings = Settings()
        self.store = ModelStore(self.settings)
        self.ha = HAClient()
        self.nlu_data = Path(os.environ.get("HUIJIAN_NLU_DATA", const.NLU_DATA_DIR))
        self.textcnn = TextCNN(self.nlu_data,
                               thresholds_override=self.settings.get("nlu.thresholds_override") or {})
        self.scenes = SceneCache(self.ha)
        self.executor = Executor(self.ha, self.settings)
        self.agent = Agent(self.settings, self.ha, self.executor)
        self.asr = AsrEngine(self.settings, self.store)
        self.tts = TtsEngine(self.settings, self.store)
        self.klar = KlarClient(self.settings)
        self.pipeline = Pipeline(self.settings, self.ha, self.scenes, self.textcnn,
                                 self.executor, agent=self.agent, klar=self.klar)
        self.host = local_ip()
        self.ctx = AppContext(settings=self.settings, ha=self.ha, asr=self.asr, tts=self.tts,
                              pipeline=self.pipeline, scenes=self.scenes, textcnn=self.textcnn,
                              store=self.store, started_at=time.time(), host=self.host)
        self.mdns = Publisher(
            props={"stt": "/xiaozhi/v1/stt", "tts": "/xiaozhi/v1/tts",
                   "llm": "/xiaozhi/v1/llm", "version": self._version()},
            version=self._version())
        self._tasks: list[asyncio.Task] = []

    @staticmethod
    def _version() -> str:
        return const.addon_version()  # 与 /api/health 同链

    # ── 启动 ────────────────────────────────────────────────────
    async def run(self) -> None:
        log = logging.getLogger("huijian.banner")
        log.warning("═" * 46)
        log.warning(" 慧尖语音加载项 huijian_voice %s 启动", self._version())
        log.warning(" WS 三通道 :%d  管理 :%d(内部)  主机 %s", const.WS_PORT, const.ADMIN_PORT, self.host)
        log.warning("═" * 46)
        await self.ha.start()
        self.scenes.refresh_soon()   # 体验批 P0-2：场景契约词表预热（首句零阻塞）
        self.settings.add_listener(self._on_settings_change)
        await self._start_http()
        await asyncio.to_thread(self.mdns.start)  # 构造+register 含阻塞 I/O，禁在 loop 内直调
        self._tasks += [
            asyncio.create_task(self._loop_models(), name="models"),
            asyncio.create_task(self._loop_status(), name="status"),
            asyncio.create_task(self._loop_reaper(), name="reaper"),
        ]
        self._write_endpoints()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        await self.shutdown()

    async def _start_http(self) -> None:
        ws_runner = web.AppRunner(make_ws_app(self.ctx), access_log=None)
        await ws_runner.setup()
        await web.TCPSite(ws_runner, "0.0.0.0", const.WS_PORT).start()
        admin_runner = web.AppRunner(make_admin_app(self.ctx), access_log=None)
        await admin_runner.setup()
        await web.TCPSite(admin_runner, "127.0.0.1", const.ADMIN_PORT).start()
        self._runners = (ws_runner, admin_runner)

    # ── 后台循环 ────────────────────────────────────────────────
    async def _loop_models(self) -> None:
        """本地档模型保障：未就绪则 ensure（带网络的重试退避），就绪后预热加载。"""
        backoff = 30
        while True:
            pend: list[str] = []      # F4 附带：预置，except 引用 pend 不再 NameError
            need_asr = str(self.settings.get("stt.provider", "")).startswith("local")
            need_tts = str(self.settings.get("tts.provider", "")).startswith("local")
            need = ([self.asr.model_key] if need_asr else []) + \
                   (["tts_kokoro_multilang"] if need_tts else [])
            try:
                pend = [k for k in need if not self.store.is_ready(k)]
                if pend:
                    for k in pend:
                        self.store.ensure_async(k)
                    backoff = min(backoff * 2, 900)
                else:
                    backoff = 30
                    loop = asyncio.get_running_loop()
                    if need_asr and not self.asr.ready():
                        await loop.run_in_executor(None, self.asr.ensure_loaded)
                    elif need_asr and self.asr.stale_kind():
                        # v4.2：回落档在载/用户切了 local_model——主档就绪即原地换绑
                        # （推理在飞 rebind 返回 False，60s 后下一轮再试，不断会话）
                        await loop.run_in_executor(None, self.asr.rebind_primary)
                    if need_tts and not self.tts.ready():
                        await loop.run_in_executor(None, self.tts.ensure_loaded)
                    # TextCNN 预热（小模型，镜像内置）
                    if self.settings.get("nlu.textcnn_enabled", True):
                        await loop.run_in_executor(None, self.textcnn._ensure)
            except Exception:
                logger.exception("[模型] 保障循环异常")
            await asyncio.sleep(backoff if pend else 60)

    async def _loop_status(self) -> None:
        while True:
            try:
                await ha_health_tick(self.ha)   # 桥接自愈（2026-09-12 真机实锤）
                snap = {
                    "ok": True,
                    "version": self._version(),
                    "uptime_s": int(time.time() - self.ctx.started_at),
                    "ha_bridge": self.ha.reachable and bool(self.ha.token),
                    "ha_error": self.ha.last_error,
                    "sessions": len(self.ctx.sessions),
                    "stt_loaded": self.asr.ready(), "tts_loaded": self.tts.ready(),
                    "textcnn": self.textcnn.available,
                    "llm_enabled": bool(self.settings.get("llm.enabled")),
                    "nlu_enabled": bool(self.settings.get("nlu.enabled", True)),
                    "ts": time.time(),
                }
                _atomic_write(const.STATUS_FILE, json.dumps(snap, ensure_ascii=False))
                _atomic_write(const.MODELS_STATUS_FILE,
                              json.dumps({"updated": time.time(), "models": self.store.snapshot()},
                                         ensure_ascii=False))
            except Exception:
                logger.debug("[状态] 回写失败", exc_info=True)
            await asyncio.sleep(5)

    async def _loop_reaper(self) -> None:
        """省电档：空闲超 N 分钟卸载大模型（下次请求自动重载）。0=常驻。"""
        while True:
            await asyncio.sleep(60)
            try:
                mins = int(self.settings.get("power.unload_when_idle_min", 0) or 0)
                if mins <= 0:
                    continue
                idle = mins * 60
                # F1/F4：unload 可能触发 ORT arena 数百 ms 析构 → 出循环执行；
                # 在飞推理时 unload() 返回 False 跳过本轮，下一轮再试。
                if self.asr.ready() and time.time() - self.asr.last_used > idle:
                    await asyncio.get_running_loop().run_in_executor(None, self.asr.unload)
                if self.tts.ready() and time.time() - self.tts.last_used > idle:
                    await asyncio.get_running_loop().run_in_executor(None, self.tts.unload)
            except Exception:
                logger.debug("[省电] reaper 异常", exc_info=True)

    # ── 热应用回调（v2 §4.1：不重启不断连）──────────────────────
    def _on_settings_change(self, data: dict) -> None:
        try:
            self.textcnn.set_thresholds_override(data.get("nlu", {}).get("thresholds_override") or {})
            self._write_endpoints()
            self._warn_local_nlu(data)
        except Exception:
            logger.exception("[配置] 热应用失败")

    def _warn_local_nlu(self, data: dict) -> None:
        """本地理解总开关关掉＝场景触发词/场景自动化本地建・改・删/本地查询/音乐带
        全停（只剩大模型）。离场必须留痕：现场支持第一眼就能看到根因；两端都关再
        点明"只会回固定兜底"。只在开关翻转时告警，避免每次保存刷日志。"""
        nlu = data.get("nlu", {}) or {}
        llm = data.get("llm", {}) or {}
        off = nlu.get("enabled", True) is False
        prev = getattr(self, "_nlu_warn_off", False)
        self._nlu_warn_off = off
        if off == prev:
            return
        if not off:
            logger.info("[配置] 本地理解已恢复启用：场景触发词/本地建改删/查询族/音乐带恢复工作")
        elif llm.get("enabled") and str(llm.get("base_url") or "").strip():
            logger.warning("[配置] 本地理解已关闭：语音场景触发词、场景/自动化本地建・改・删、"
                           "本地查询、音乐带全部停用，仅大模型兜底")
        else:
            logger.warning("[配置] 本地理解已关闭且大模型未配置：助手只会回一句固定兜底")

    def _write_endpoints(self) -> None:
        try:
            urls = self.settings.endpoint_urls(self.host)
            payload = dict(urls)
            payload.update({"token": self.settings.get("security.ws_token", ""),
                            "require_token": bool(self.settings.get("security.require_token")),
                            "version": self._version()})
            dst = const.DATA_DIR / "run" / "endpoints.json"  # 含 token，绝落静态根（infra-F7）
            dst.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(dst, json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception:
            logger.debug("[状态] endpoints.json 回写失败", exc_info=True)

    async def shutdown(self) -> None:
        logger.warning("[退出] 优雅停机开始")
        # F4：①abort 让下载线程在下个分块边界主动退出（否则默认 executor
        # join 会把分钟级下载拖成 SIGKILL 收尾）；②会话在飞 task 先取消
        # （runner.cleanup 对 WS handler 最长可等 60s）；③循环 task await
        # 收束带超时。
        with contextlib.suppress(Exception):
            self.store.abort.set()
        for sess in list(self.ctx.sessions):
            with contextlib.suppress(Exception):
                sess.close_work()
        for t in self._tasks:
            t.cancel()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True), timeout=5)
        await asyncio.to_thread(self.mdns.close)  # unregister 亦阻塞网络 I/O
        if getattr(self, "_runners", None):
            for r in self._runners:
                await r.cleanup()
        await self.agent.close()
        await self.klar.close()
        await self.ha.close()
        logger.warning("[退出] 完成")


async def ha_health_tick(ha) -> None:
    """HA 桥接自愈：不可达时轻量重探（GET /api/states 恒存在，成本极低）。

    2026-09-12 真机实锤（用户 HAOS 18.2 / Core 2026.9.1）：加载项重启若撞上
    HA Core 启动窗口，启动期 `ha.start()` 那次探测失败后**没有任何周期重探**，
    状态页「HA 桥接 不可达」会一直亮到下一句语音/查询碰巧调用 HA 为止
    （用户实感"手动触发一次才在线"）。状态循环每 5s 顺带重探即可自愈；
    refresh_states 自带 5s TTL，不会打爆 HA。
    """
    if ha is None or ha.reachable or not ha.ok:
        return
    with contextlib.suppress(Exception):
        await ha.refresh_states()


def _atomic_write(path: Path, content: str) -> None:
    # F6：tmp 唯一名——固定 .tmp 名在多写者（models_status 双写者）下可把
    # 对方半写文件 rename 转正。mkstemp 原子创建且互不碰撞。
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        # mkstemp 默认 0600：rename 后 nginx worker（www-data）读走 403——
        # 事实文件必须世界可读（v1.0.0 CI e2e step7 实锤，Windows 本地不可见面）。
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def main() -> None:
    _setup_logging()
    try:
        asyncio.run(Service().run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
