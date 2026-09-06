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
        self.pipeline = Pipeline(self.settings, self.ha, self.scenes, self.textcnn,
                                 self.executor, agent=self.agent)
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
        self.settings.add_listener(self._on_settings_change)
        await self._start_http()
        self.mdns.start()
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
        ws_runner = web.AppRunner(make_ws_app(self.ctx), access_logger=None)
        await ws_runner.setup()
        await web.TCPSite(ws_runner, "0.0.0.0", const.WS_PORT).start()
        admin_runner = web.AppRunner(make_admin_app(self.ctx), access_logger=None)
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
                snap = {
                    "ok": True,
                    "version": self._version(),
                    "uptime_s": int(time.time() - self.ctx.started_at),
                    "ha_bridge": self.ha.reachable and bool(self.ha.token),
                    "sessions": len(self.ctx.sessions),
                    "stt_loaded": self.asr.ready(), "tts_loaded": self.tts.ready(),
                    "textcnn": self.textcnn.available,
                    "llm_enabled": bool(self.settings.get("llm.enabled")),
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
        except Exception:
            logger.exception("[配置] 热应用失败")

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
        self.mdns.close()
        if getattr(self, "_runners", None):
            for r in self._runners:
                await r.cleanup()
        await self.agent.close()
        await self.ha.close()
        logger.warning("[退出] 完成")


def _atomic_write(path: Path, content: str) -> None:
    # F6：tmp 唯一名——固定 .tmp 名在多写者（models_status 双写者）下可把
    # 对方半写文件 rename 转正。mkstemp 原子创建且互不碰撞。
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
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
