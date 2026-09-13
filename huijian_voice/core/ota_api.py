"""设备台账与固件仓端点（OTA 方案 Phase 2 加载项侧，2026-09-23）。

独立小模块——避开 admin_api 热区的并行冲突（tts_voices_api 同惯例）。
挂载：admin_api.make_admin_app 内 `ota_api.setup(app, ctx)` 一行。

契约：
  GET  /api/devices            → 卫星台账（数据真源=集成 HuijianSatellitesView：
                                  CMD20 入驻 fw_version + API 在线态 + 区域），
                                  叠加固件仓比对出 updatable；集成不可达回
                                  {"devices": [], "error": …} 不 500。
  GET  /api/firmware           → FirmwareStore.status()：登记版本/在盘/最新/投递口。
  POST /api/firmware/download  → {version} 按 lock urls 拉包（GitHub→Gitee 容灾序，
                                  sha256 必核，运维点触——**无自动公网拉取**，
                                  运行期零 GitHub 依赖定案不破）。
  POST /api/firmware/issue     → {version?, mac?} 生成一次性领取链接（10min）：
                                  现网固件(v2.1.35)无远程接收口，链接供小程序
                                  BLE CMD21 近场代发；固件 v2.1.36+ 的 ota_services
                                  非空后面板同键切真下发（集成侧已探测）。
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from aiohttp import web

from . import const
from .firmware_store import vkey

logger = logging.getLogger("huijian.ota")

_SATELLITES_PATH = "/api/huijian-ai/satellites"


async def _json_body(request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def setup(app, ctx):
    async def _devices(request):
        store = getattr(ctx, "firmware", None)
        latest = None
        try:
            latest = await asyncio.to_thread(store.latest) if store else None
        except Exception as e:  # noqa: BLE001 面板数据面永不抛
            logger.warning("[OTA] 固件仓状态读取失败: %s", e)
        led = await ctx.ha.rest_get(_SATELLITES_PATH) if ctx.ha else None
        devices = (led or {}).get("devices")
        if devices is None:
            return web.json_response({
                "devices": [],
                "latest": (latest or {}).get("version", "") if latest else "",
                "error": "集成未回卫星台账（集成版本过旧或 HA 桥未通；设备入驻后自动出现）"})
        for d in devices:
            cur = str(d.get("fw_version") or "")
            d["updatable"] = bool(latest and cur and vkey(cur) < vkey(latest["version"]))
        return web.json_response({
            "devices": devices,
            "latest": (latest or {}).get("version", "") if latest else ""})

    async def _firmware(request):
        store = getattr(ctx, "firmware", None)
        if store is None:
            return web.json_response({"items": [], "latest": "", "error": "固件仓未挂载"})
        try:
            return web.json_response(await asyncio.to_thread(store.status))
        except Exception as e:  # noqa: BLE001
            logger.warning("[OTA] 固件仓快照失败: %s", e)
            return web.json_response({"items": [], "latest": "", "error": str(e)})

    async def _download(request):
        store = getattr(ctx, "firmware", None)
        if store is None:
            return web.json_response({"success": False, "error": "固件仓未挂载"}, status=503)
        body = await _json_body(request)
        version = str(body.get("version", "")).strip()
        if not version:
            return web.json_response({"success": False, "error": "version 必填"}, status=400)
        ok, msg = await asyncio.to_thread(store.download, version)
        logger.info("[OTA] 拉取 v%s → %s（%s）", version, "ok" if ok else "失败", msg)
        return web.json_response({"success": ok, "message": msg})

    async def _issue(request):
        store = getattr(ctx, "firmware", None)
        if store is None:
            return web.json_response({"success": False, "error": "固件仓未挂载"}, status=503)
        body = await _json_body(request)
        version = str(body.get("version", "")).strip()
        mac = str(body.get("mac", "")).strip()
        try:
            res = await asyncio.to_thread(store.issue, version, mac) \
                if version else None
        except Exception as e:  # noqa: BLE001
            logger.warning("[OTA] 签发失败: %s", e)
            res = None
        if not version:
            lat = await asyncio.to_thread(store.latest)
            if lat:
                version = lat["version"]
                res = await asyncio.to_thread(store.issue, version, mac)
        if not res:
            return web.json_response({
                "success": False,
                "error": "该版本不在盘（投递口放包或先「拉取」；或尚无已登记版本）"}, status=400)
        host = ctx.host or "homeassistant.local"
        url = f"http://{host}:{const.WS_PORT}/firmware/{quote(res['file'])}?t={res['token']}"
        logger.info("[OTA] 生成近场下发链接 v%s mac=%s", res["version"], mac or "-")
        return web.json_response({
            "success": True, "url": url, "sha256": res["sha256"], "size": res["size"],
            "version": res["version"], "expires_in": res["expires_in"],
            "warn_zh": "v2.1.35 及更早固件不校验哈希/不防降级——请核对版本号后再代发；"
                       "链接一次性、10 分钟过期"})

    app.router.add_get("/api/devices", _devices)
    app.router.add_get("/api/firmware", _firmware)
    app.router.add_post("/api/firmware/download", _download)
    app.router.add_post("/api/firmware/issue", _issue)
