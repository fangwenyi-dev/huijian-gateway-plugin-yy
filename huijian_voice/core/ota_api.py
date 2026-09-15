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
                                  现网固件(v2.1.35)无远程接收口；且小程序 v1.4.14
                                  尚无 CMD21 代发入口（2026-09 OTA 深审查证）——
                                  链接为备存形态，面板话术已如实降级；
                                  固件 v2.1.36+ 的 ota_services 非空后面板同键
                                  切真下发（集成侧已探测）。
  POST /api/device/continuous  → {mac, enabled} 连续对话开关（v1.0.80）：中继
                                  集成写设备 esphome switch 实体（真源=设备 NVS
                                  cDialogue，HA/小程序/面板三边同源回显）。
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from aiohttp import web

from . import const
from .firmware_store import vkey
from .mdns import local_ip

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
        bridge_ok = bool(ctx.ha and getattr(ctx.ha, "ok", False))
        led = await ctx.ha.rest_get(_SATELLITES_PATH) if ctx.ha else None
        devices = (led or {}).get("devices")
        if devices is None:
            # v1.0.65 用户反馈：面板空表必须把原因说透——此前一种文案背两种锅
            if not bridge_ok:
                err = ("HA 桥未连接——请在设置页填好 HA 地址与长期令牌并确认"
                       f"连接绿标（当前 {getattr(const, 'APP_VERSION', '?')} 版加载项要求 HA 可达）")
            else:
                err = ("HA 里的 huijian_ai 集成没有卫星台账接口——集成与加载项是"
                       "两份代码，请把 HA 的 custom_components/huijian_ai 升级到"
                       f"与本加载项同版本（≥{const.APP_VERSION}）后重启 HA，"
                       "卫星入驻后台账自动出现")
            return web.json_response({
                "devices": [],
                "latest": (latest or {}).get("version", "") if latest else "",
                "error": err})
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
        try:   # v1.0.65 F-OTA-04：面板数据面永不抛——store 侧异常折叠 JSON
            ok, msg = await asyncio.to_thread(store.download, version)
        except Exception as e:  # noqa: BLE001
            logger.warning("[OTA] 拉取异常: %s", e)
            return web.json_response({"success": False, "error": f"拉取异常：{e}"[:200]},
                                     status=500)
        logger.info("[OTA] 拉取 v%s → %s（%s）", version, "ok" if ok else "失败", msg)
        return web.json_response({"success": ok, "message": msg})

    async def _issue(request):
        store = getattr(ctx, "firmware", None)
        if store is None:
            return web.json_response({"success": False, "error": "固件仓未挂载"}, status=503)
        body = await _json_body(request)
        version = str(body.get("version", "")).strip()
        # mac 仅日志要素，入日志前净化（安全 F4 同族）；空/脏不影响签发
        mac = str(body.get("mac", "")).strip()[:40]
        res = None
        try:   # v1.0.65 F-OTA-04：含 latest 兜底分支整体折叠——违背「永不抛」契约即 500
            if version:
                res = await asyncio.to_thread(store.issue, version, mac)
            else:
                lat = await asyncio.to_thread(store.latest)
                if lat:
                    version = lat["version"]
                    res = await asyncio.to_thread(store.issue, version, mac)
        except Exception as e:  # noqa: BLE001
            logger.warning("[OTA] 签发异常: %s", e)
            res = None
        if not res:
            return web.json_response({
                "success": False,
                "error": "该版本不在盘（投递口放包或先「拉取」；或尚无已登记版本）"}, status=400)
        # host 每次签发实时取（v1.0.65 契约 F-09）：ctx.host 是启动时一次性采样，
        # DHCP 重绑后旧值会让链接静默失效；回环地址签发无意义，如实报错。
        host = local_ip()
        if not host or host.startswith("127."):
            return web.json_response({
                "success": False,
                "error": "加载项当前无可路由局域网地址，链接无法供设备领取——"
                         "请确认本机已接入设备所在局域网后重试"}, status=503)
        url = f"http://{host}:{const.WS_PORT}/firmware/{quote(res['file'])}?t={res['token']}"
        logger.info("[OTA] 生成近场下发链接 v%s mac=%s", res["version"], mac or "-")
        return web.json_response({
            "success": True, "url": url, "sha256": res["sha256"], "size": res["size"],
            "version": res["version"], "expires_in": res["expires_in"],
            "warn_zh": "v2.1.35 及更早固件不校验哈希/不防降级——请核对版本号后再代发；"
                       "链接一次性、10 分钟过期，过期或下载中断请回本页重新签发；"
                       "仅限设备所在局域网内打开；当前小程序尚无 CMD21 代发入口"
                       "（代发能力未上线，链接先用于备存）"})

    async def _dispatch(request):
        """v1.0.74 真下发：issue + 经集成中继调设备 :6053 用户服务 ota_upgrade。
        设备接收口仅 ≥v2.1.36 有；URL 私网白名单闸在设备侧（单一事实源，此处
        不复装）。令牌签出后若下发失败不回滚——10min TTL 自然作废（防竞态
        简单化），日志点名。全路径折叠 JSON 永不抛（F-OTA-04 同纪律）。"""
        store = getattr(ctx, "firmware", None)
        if store is None:
            return web.json_response({"success": False, "error": "固件仓未挂载"}, status=503)
        body = await _json_body(request)
        version = str(body.get("version", "")).strip()
        mac = str(body.get("mac", "")).strip()[:40]
        if not mac:
            return web.json_response(
                {"success": False, "error": "mac 必填——下发按设备寻址，防发错机"}, status=400)
        res = None
        try:
            if not version:
                lat = await asyncio.to_thread(store.latest)
                if not lat:
                    return web.json_response(
                        {"success": False, "error": "固件仓无已登记版本——投递口放包或先「拉取」"},
                        status=400)
                version = lat["version"]
            res = await asyncio.to_thread(store.issue, version, mac)
        except Exception as e:  # noqa: BLE001
            logger.warning("[OTA] dispatch 签发异常: %s", e)
            res = None
        if not res:
            return web.json_response(
                {"success": False, "error": "该版本不在盘（投递口放包或先「拉取」）"}, status=400)
        host = local_ip()
        if not host or host.startswith("127."):
            return web.json_response({
                "success": False,
                "error": "加载项无可路由局域网地址——设备白名单闸只收私网字面 IPv4"}, status=503)
        url = f"http://{host}:{const.WS_PORT}/firmware/{quote(res['file'])}?t={res['token']}"
        bridge_ok = bool(ctx.ha and getattr(ctx.ha, "ok", False))
        if not bridge_ok:
            return web.json_response({
                "success": False, "error": "HA 桥未连接，无法中继到设备", "version": version},
                status=502)
        ack = await ctx.ha.rest_write("POST", "/api/huijian-ai/satellites/ota",
                                      {"mac": mac, "url": url})
        if not ack.get("success"):
            logger.warning("[OTA] dispatch 失败 v%s mac=%s: %s", version, mac,
                           ack.get("error", "?"))
            return web.json_response({
                "success": False, "error": ack.get("error", "集成未受理"), "version": version})
        logger.info("[OTA] 真下发 v%s → %s sha=%s… mac=%s",
                    version, res["file"], str(res["sha256"])[:12], mac)
        return web.json_response({
            "success": True, "version": version, "size": res["size"],
            "sha256": res["sha256"],
            "note": "设备已受理并立即下载（局域网约 1-3 分钟），成功后自动重启；"
                    "5-10 分钟后刷新台账核对版本号"})

    async def _device_continuous(request):
        """v1.0.80：面板「连续对话」开关 → 中继集成写设备 switch 实体。
        真源在设备 NVS（cDialogue，HA/小程序/面板三边同源），本端点纯转发；
        桥断/无实体/失败全折叠 JSON 永不抛（F-OTA-04 同纪律）。"""
        body = await _json_body(request)
        mac = str(body.get("mac", "")).strip()[:40]
        if not mac:
            return web.json_response({"success": False, "error": "mac 必填"}, status=400)
        enabled = bool(body.get("enabled", False))
        bridge_ok = bool(ctx.ha and getattr(ctx.ha, "ok", False))
        if not bridge_ok:
            return web.json_response({"success": False, "error": "HA 桥未连接"}, status=502)
        ack = await ctx.ha.rest_write("POST", "/api/huijian-ai/satellites/continuous",
                                      {"mac": mac, "enabled": enabled})
        if not ack.get("success"):
            logger.info("[连续对话] 中继拒绝 mac=%s: %s", mac, ack.get("error", "?"))
            return web.json_response({"success": False,
                                      "error": ack.get("error", "集成未受理")})
        logger.info("[连续对话] mac=%s → %s", mac, "开" if enabled else "关")
        return web.json_response({"success": True, "enabled": enabled})

    app.router.add_get("/api/devices", _devices)
    app.router.add_get("/api/firmware", _firmware)
    app.router.add_post("/api/firmware/download", _download)
    app.router.add_post("/api/firmware/issue", _issue)
    app.router.add_post("/api/firmware/dispatch", _dispatch)
    app.router.add_post("/api/device/continuous", _device_continuous)
