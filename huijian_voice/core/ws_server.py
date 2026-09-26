"""WebSocket 服务（aiohttp :8000，三通道路径 /xiaozhi/v1/{stt|tts|llm}）。

认证定式（契约 §5）：token 仅 URL query（无 header 通道）；
- require_token=false（LAN MVP）：缺 token 放行，但**显式错误 token 仍 401**
  （给了错的还放行等于安全开关无意义；集成侧只有拿错 token 才会永久停重连，
  这正是期望行为——配对错误必须可见）。
- 401 发生在 HTTP 握手层（ws.prepare 之前），符合「拒绝=401 不升级」硬约束。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from aiohttp import web

from . import const
from .session import SESSION_BY_CHANNEL

logger = logging.getLogger("huijian.ws")

# v1.0.70（深审⑪）：三通道全客户并发上限（正常 LAN：卫星×个位数 + 小程序
# 零星，32 倍裕量取 64）。见 _ws_handler 闸内注释。
_MAX_SESSIONS = 64

try:
    CTX_KEY: web.AppKey = web.AppKey("huijian_ctx", object)   # aiohttp>=3.9
except AttributeError:                                        # 老版本回退
    CTX_KEY = "huijian_ctx"


@dataclass
class AppContext:
    """运行期组件袋（main 装配）。"""
    settings: object = None
    ha: object = None
    asr: object = None
    tts: object = None
    pipeline: object = None
    scenes: object = None
    textcnn: object = None
    store: object = None
    firmware: object = None
    started_at: float = 0.0
    sessions: set = field(default_factory=set)
    host: str = ""


def make_ws_app(ctx: AppContext) -> web.Application:
    app = web.Application()
    app[CTX_KEY] = ctx          # 处理器经 request.app 取用（闭包全局名是移植手误）
    app.router.add_get("/xiaozhi/v1/{channel}", _ws_handler)
    app.router.add_get("/healthz", _health)
    app.router.add_get("/readyz", _readyz)
    app.router.add_get("/discover", _discover)
    # allow_head=False（v1.0.65 审查批 F-OTA-03）：微信/企业微信/浏览器的链接
    # 预览器会对 URL 先发 HEAD——默认 allow_head=True 时 HEAD 复用同一 handler，
    # 一次性令牌在设备真 GET 之前就被烧掉（现场表现「链接刚发就失效」）。
    app.router.add_get("/firmware/{fname}", _firmware_get, allow_head=False)
    return app


async def _firmware_get(request: web.Request) -> web.Response:
    """固件领取口（OTA 方案 Phase 2「近场链接兜底」下发端，2026-09-23）：
    GET /firmware/<file>?t=<一次性令牌>。令牌由面板 POST /api/firmware/issue
    签发（10min TTL、消费即废、绑文件名——不可指使他包）。:8000 本就 LAN
    开放（/discover 同面），匿名枚举被"无令牌 404 + 单次消费"挡住；HTTPS 与
    包签名属固件仓 Phase 1 收口，本口不假装有——面板文案如实警示。"""
    ctx = request.app[CTX_KEY]
    store = getattr(ctx, "firmware", None)
    fname = request.match_info.get("fname", "")
    if store is None:
        return web.json_response({"message": "no firmware store"}, status=404)
    # to_thread（v1.0.65 F-OTA-08）：take 抢的 _lock 可能与 scan_import 收编同锁——
    # 直调会在 :8000 语音事件循环热路径上等磁盘哈希，拖停全部在流会话。
    path = await asyncio.to_thread(store.take, request.query.get("t", ""), fname)
    if path is None:
        # 拒绝原因分类日志在 store.take 内（v1.0.65）；此处只留匿名面取证要素
        logger.warning("[OTA] 领取被拒 from %s", request.remote)
        return web.json_response({"message": "invalid token"}, status=404)
    logger.info("[OTA] 发放 %s to %s", fname[:120], request.remote)
    return web.FileResponse(path)


def _auth_fail(request: web.Request, why: str) -> web.Response:
    logger.warning("[WS] 401 %s from %s", why, request.remote)
    return web.json_response({"message": why}, status=401)


async def _ws_handler(request: web.Request) -> web.WebSocketResponse:
    ctx = request.app[CTX_KEY]
    channel = request.match_info.get("channel", "")
    cls = SESSION_BY_CHANNEL.get(channel)
    if cls is None:
        return web.json_response({"message": "unknown channel"}, status=404)
    settings = ctx.settings
    require = bool(settings.get("security.require_token", False))
    expected = str(settings.get("security.ws_token", ""))
    provided = request.query.get("token")
    if provided is not None and provided != expected:
        return _auth_fail(request, "invalid token")
    if require and not provided:
        return _auth_fail(request, "token required")

    # v1.0.70（深审⑪）：会话总量闸。heartbeat=None 保留——:8000 的 WS 消费
    # 端除 HA 集成（aiohttp 自动回 PONG）外还有嵌入式/小程序形态，服务端
    # 主动 PING 对不回 PONG 的旧客户端=错杀；半开回收交给集成侧 55s 心跳+
    # 180s 空闲监控（ws_transport ⑨⑩已闭环）。无上限的 sessions 集是事件
    # 循环卡死场景的放大器（泄漏会话无读超时、永不走 finally）——设硬闸
    # 并点名拒绝，泄漏堆不满也看得见。
    if len(ctx.sessions) >= _MAX_SESSIONS:
        logger.warning("[WS] 会话数 %d 已达上限 %d，拒绝 %s 通道新连接（疑似泄漏/风暴）",
                       len(ctx.sessions), _MAX_SESSIONS, channel)
        return web.json_response({"message": "too many sessions"}, status=503)

    ws = web.WebSocketResponse(heartbeat=None, max_msg_size=1 << 16, compress=False)
    await ws.prepare(request)
    session = cls(ws, ctx)
    session.device_hint = request.remote or ""
    ctx.sessions.add(session)
    logger.info("[WS] %s 通道接入 %s（在线 %d 会话）", channel, session.device_hint, len(ctx.sessions))
    if channel == "tts":
        # v1.0.48（P5）：音色指纹随建连欢迎下发——集成在加载项保存后重连等
        # 场景也必须取到当前值（web 保存侧另有在线推送）。旧集成把此消息按
        # unknown message 仅 INFO 一条、不break，向后兼容无副作用。
        # fail-open：指纹只是缓存键轮换增强，任何异常不得杀掉播报主链。
        try:
            await session.send_json({"type": "settings",
                                     "voice_fp": ctx.tts.voice_fingerprint(),
                                     # v1.0.88：同处申报协议代次——集成见 >=2 才启用
                                     # 下行流标识（detect 带 rid + 未同步即丢）。旧集成
                                     # 未知键忽略，本帧从不进业务帧流，零暴露。
                                     "tts_proto": const.TTS_PROTO_VERSION})
        except Exception:
            logger.exception("[WS] tts 欢迎指纹发送失败（不影响播报）")
    elif channel == "stt":
        # v1.0.92：STT 轮次身份协商——建连欢迎帧申报 stt_proto，集成见 >=2 才
        # 在 listen start/stop 携带 rid（服务端回显进回执）。settings 帧从不进
        # 业务帧流（客户端基类旁路），旧集成零暴露；发不出也不影响识别主链。
        try:
            await session.send_json({"type": "settings",
                                     "stt_proto": const.STT_PROTO_VERSION})
        except Exception:
            logger.exception("[WS] stt 协议欢迎发送失败（不影响识别）")
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    await session.on_text(msg.data)
                except Exception:
                    logger.exception("[WS] %s 文本帧处理异常", channel)      # 单帧异常不断链
            elif msg.type == web.WSMsgType.BINARY:
                try:
                    await session.on_binary(msg.data)
                except Exception:
                    logger.exception("[WS] %s 二进制帧处理异常", channel)
            elif msg.type in (web.WSMsgType.ERROR,):
                logger.info("[WS] %s 连接错误: %s", channel, ws.exception())
    finally:
        ctx.sessions.discard(session)
        await session.on_close()
        logger.info("[WS] %s 通道断开 %s（%.0fs）", channel, session.device_hint,
                    __import__("time").time() - session.created)
    return ws


async def _discover(request: web.Request) -> web.Response:
    """无凭据设备端点发现（:8000 面向 LAN 设备/小程序的自描述端点）。

    背景：小程序侧三扇门全关——`/api/endpoints` 被 nginx server 级白名单
    403（LAN 直连 :8001 一律拒，见 nginx-huijian.conf）、`/data/run/
    endpoints.json` 不落静态根、微信 mDNS 读不到 TXT——设备拿不到端点。
    本端点在 :8000（本就对 LAN 开放）提供发现，是判定书 D-2/缺口2 的修法。

    安全边界：只回端点路径 + 是否强制校验，**永不回 token**（含 require_
    token=true 时）。require_token=false 场景 token 本非必需（LAN 免 token
    即连）；true 场景设备必须带 query token，端点路径不含凭据可安全下发。
    """
    ctx = request.app[CTX_KEY]
    settings = ctx.settings
    require = bool(settings.get("security.require_token", False))
    host = (ctx.host or request.headers.get("Host", "").split(":")[0] or "")
    return web.json_response({
        "ok": True,
        "name": "huijian_voice",
        "version": const.addon_version(),
        "require_token": require,
        "endpoints": {k: f"ws://{host}:{const.WS_PORT}/xiaozhi/v1/{k}"
                      for k in ("stt", "tts", "llm")},
        "channels": ["stt", "tts", "llm"],
        "audio": {"format": "opus", "sample_rate": const.SAMPLE_RATE,
                  "frame_ms": const.FRAME_MS},
    })


# 模型资产"已经判负"的状态：不是"还没轮到"，是下不来了（源全失败/需手动导入）。
_FATAL_MODEL_STATES = ("failed", "incomplete", "manual")


def _readiness(ctx) -> dict:
    """就绪判定 + 分因。

    引擎是懒加载（第一句话才载），所以 asr_ready=False **本身不是故障**。
    把 /healthz 直接改成"引擎没载就 503"，会让一台十分钟没人说话的正常客户机
    被 Supervisor 无限重启——比"watchdog 永远绿"更糟。真故障判据是模型资产已
    判负：那时引擎永远起不来，且重启也救不回来（下载还会再失败）。
    """
    snap = {}
    snapshot = getattr(ctx.store, "snapshot", None)
    if callable(snapshot):
        try:
            snap = snapshot() or {}
        except Exception:  # noqa: BLE001 探针不得把自己弄成 500
            logger.exception("[WS] 模型状态快照读取失败（healthz 降级为无资产信息）")
            snap = {}
    fatal = sorted(k for k, v in snap.items()
                   if v.get("state") in _FATAL_MODEL_STATES and not v.get("ready"))
    # ── 所选引擎真相（2026-09-26 补）───────────────────────────────
    # asr_ready 只回答"有没有 recognizer 在载"，回落档在载也是 true。家网那次实测
    # 就是这个形状骗过了所有人：asr_ready:true / tts_ready:true / models_fatal:[]，
    # 而配置主档 asr_sensevoice_small 一直 pending ⇒ 每轮语音都起不来、设备 20s
    # 超时无应答，健康面却全程绿灯。这里把"所选引擎"的真实状态单列出来，
    # ok/asr_ready 的既有语义一字不动（Supervisor 重启判定依赖它）。
    asr = getattr(ctx, "asr", None)
    primary = str(getattr(asr, "model_key", "") or "")
    loaded = str(getattr(asr, "loaded_kind", lambda: "")() or "") if asr else ""
    out = {
        "ok": not fatal,
        "asr_ready": bool(ctx.asr and ctx.asr.ready()),
        "tts_ready": bool(ctx.tts and ctx.tts.ready()),
        "sessions": len(ctx.sessions),
        "models_fatal": fatal,
        "models": {k: v.get("state") for k, v in sorted(snap.items())},
        "asr_model": primary,
        "asr_model_state": (snap.get(primary) or {}).get("state", "unknown") if primary else "n/a",
        "asr_loaded_model": loaded,
        # 回落在载＝在载档 ≠ 配置主档；判据复用 asr.stale_kind()（它比的是档名，
        # 这里 primary 是存储键，不能直接拿 loaded 去等值比较）。
        "asr_fallback_in_use": bool(getattr(asr, "stale_kind", lambda: False)()) if asr else False,
    }
    if not out["asr_ready"]:
        out["asr_reason"] = str(getattr(asr, "last_reason", "") or "") or "引擎未加载（懒加载或补下载中）"
    return out


async def _health(request: web.Request) -> web.Response:
    """Supervisor watchdog 目标（config.yaml:26）。状态码语义一字不动：恒 200，
    只在 body 里把就绪与判负面如实带出。真就绪探针见 /readyz。"""
    return web.json_response(_readiness(request.app[CTX_KEY]))


async def _readyz(request: web.Request) -> web.Response:
    """严格就绪探针：仅当模型资产判负才 503。watchdog 换成它是安全的一行改动，
    但那是全体客户的重启行为变更，需先在真实安装上看到 /healthz 的 body 再决定。"""
    body = _readiness(request.app[CTX_KEY])
    return web.json_response(body, status=200 if body["ok"] else 503)
