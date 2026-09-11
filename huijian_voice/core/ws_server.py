"""WebSocket 服务（aiohttp :8000，三通道路径 /xiaozhi/v1/{stt|tts|llm}）。

认证定式（契约 §5）：token 仅 URL query（无 header 通道）；
- require_token=false（LAN MVP）：缺 token 放行，但**显式错误 token 仍 401**
  （给了错的还放行等于安全开关无意义；集成侧只有拿错 token 才会永久停重连，
  这正是期望行为——配对错误必须可见）。
- 401 发生在 HTTP 握手层（ws.prepare 之前），符合「拒绝=401 不升级」硬约束。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from aiohttp import web

from . import const
from .session import SESSION_BY_CHANNEL

logger = logging.getLogger("huijian.ws")

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
    started_at: float = 0.0
    sessions: set = field(default_factory=set)
    host: str = ""


def make_ws_app(ctx: AppContext) -> web.Application:
    app = web.Application()
    app[CTX_KEY] = ctx          # 处理器经 request.app 取用（闭包全局名是移植手误）
    app.router.add_get("/xiaozhi/v1/{channel}", _ws_handler)
    app.router.add_get("/healthz", _health)
    app.router.add_get("/discover", _discover)
    return app


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
                                     "voice_fp": ctx.tts.voice_fingerprint()})
        except Exception:
            logger.exception("[WS] tts 欢迎指纹发送失败（不影响播报）")
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


async def _health(request: web.Request) -> web.Response:
    ctx = request.app[CTX_KEY]
    ready = bool(ctx.asr and ctx.asr.ready())
    return web.json_response({"ok": True, "asr_ready": ready,
                              "tts_ready": bool(ctx.tts and ctx.tts.ready()),
                              "sessions": len(ctx.sessions)})
