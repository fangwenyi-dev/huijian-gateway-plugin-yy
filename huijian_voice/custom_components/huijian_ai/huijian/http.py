import asyncio
import hashlib
import hmac
import json
import logging
import time

from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView

from ..const import CONF_STT_ENTITY_ID, CONF_TTS_ENTITY_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_https(hass: HomeAssistant):
    this_data = hass.data.setdefault(DOMAIN, {})
    if this_data.get("https_setup"):
        return
    this_data["https_setup"] = True
    hass.http.register_view(HuijianSetupView)
    hass.http.register_view(HuijianRemoveView)
    hass.http.register_view(HuijianSetNameView)
    hass.http.register_view(HuijianTtsSttView)
    hass.http.register_view(HuijianDeviceInfoView)
    hass.http.register_view(HuijianSatellitesView)
    hass.http.register_view(HuijianSatelliteOtaView)
    hass.http.register_view(HuijianSatelliteContinuousView)


class HuijianHttpView(HomeAssistantView):
    requires_auth = False

    async def check_sign(self, request: web.Request, speak_id=None):
        hass = request.app[KEY_HASS]
        params = request.query
        if request.method in ("PUT", "POST"):
            params = await request.json() or {}
        if not speak_id:
            speak_id = params.get("speak_id") or request.query.get("speak_id", "")
        entry = None
        for ent in hass.config_entries.async_loaded_entries(DOMAIN):
            if speak_id == ent.data.get("speak_id"):
                entry = ent
                break
        if not entry:
            return None
        salt = request.headers.get("Salt", "")
        ret = request.headers.get("Authorization") == calculate_sign(
            request.path,
            params,
            entry.data.get("mac", "").lower(),
            salt,
        )
        return entry if ret else False


class HuijianSetupView(HuijianHttpView):
    url = "/api/huijian-ai/setup/qrcode"
    name = "api:huijian-ai:setup-qrcode"

    async def post(self, request: web.Request):
        hass = request.app[KEY_HASS]
        this_data = hass.data.setdefault(DOMAIN, {})
        if not (uuid := request.query.get("uuid")):
            return self.json_message("uuid missing", 400)
        if uuid not in this_data:
            return self.json_message("uuid invalid", 400)

        setup_data = await request.json() or {}

        # Verify signature using UUID as shared secret
        # If no signature is provided, fall back to UUID-only check
        # for backward compatibility with existing clients
        auth_header = request.headers.get("Authorization", "")
        if auth_header:
            expected_sign = hmac.new(
                uuid.encode("utf-8"),
                json.dumps(setup_data, separators=(",", ":")).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if auth_header != expected_sign:
                _LOGGER.warning("Setup request with invalid signature for uuid=%s", uuid)
                return self.json_message("invalid signature", 401)

        _LOGGER.info("Setup qrcode from miniprogram: %s",
                     {k: v for k, v in setup_data.items() if k != "noise_psk"})

        this_data[uuid] = setup_data
        # OTA 设备台账（v1.0.65 审查批·契约 F-02 断链修复）：固件 CMD20 入驻 POST
        # （ble_manager.cc:761）是现网**唯一**携带 fw_version 的设备上报，落点就在
        # 本口——旧实现的台账写入挂在 speakname 口（设备 body 只有 speak_name/
        # speak_id），fw_version 永远进不了账 → 面板版本恒「未上报」、updatable
        # 恒 false。speak_id 维度台账=运行期内存态（每次重新入驻刷新）；建账时
        # 版本由 config_flow 持久化进 entry.data（HA 重启后仍可显示）。
        sid = str(setup_data.get("speak_id") or "")
        fv = str(setup_data.get("fw_version") or "").strip()
        if sid and fv:
            this_data.setdefault("satellite_ledger_by_speakid", {})[sid] = {
                "fw_version": fv, "ts": time.time()}
        return self.json_message("ok")


class HuijianRemoveView(HuijianHttpView):
    url = "/api/huijian-ai/remove"
    name = "api:huijian-ai:remove"

    async def delete(self, request: web.Request):
        hass = request.app[KEY_HASS]
        if not (speak_id := request.query.get("speak_id")):
            return self.json_message("speak_id missing", 400)
        entry = await self.check_sign(request, speak_id)
        if not entry:
            return self.json_message("params error", 400)

        _LOGGER.info("Remove entry: %s", entry.entry_id)
        await hass.config_entries.async_remove(entry.entry_id)
        return self.json_message("ok")


class HuijianSetNameView(HuijianHttpView):
    url = "/api/huijian-ai/update/speakname"
    name = "api:huijian-ai:update:speakname"

    async def post(self, request: web.Request):
        hass = request.app[KEY_HASS]
        entry = await self.check_sign(request)
        if not entry:
            return self.json_message("params error", 400)
        data = await request.json() or {}
        # OTA 设备台账·mac 维度（预留演进位，v1.0.65 注释纠偏）：本口 body
        # 现网只有 speak_name/speak_id（固件 r_postDeviceName，ble_manager.cc:540
        # 附近）——**不带** fw_version；带版本的 CMD20 入驻 POST 落 SetupView，
        # 台账写点在彼处（契约 F-02 修复）。此处保留 schema-free 写入分支：
        # 未来固件若随改名重报版本，自动入账，无需改集成。
        _ledger = hass.data.setdefault(DOMAIN, {}).setdefault("satellite_ledger", {})
        _mac = str(entry.data.get("mac", "") or "").lower()
        if _mac:
            _rec = _ledger.setdefault(_mac, {})
            _rec["ts"] = time.time()
            if v := str(data.get("fw_version") or "").strip():
                _rec["fw_version"] = v
            if sn := str(data.get("speak_name") or "").strip():
                _rec["speak_name"] = sn
        if not (name := data.get("speak_name")):
            return self.json_message("speak_name missing", 400)
        mac = entry.data.get("mac")
        device_registry = dr.async_get(hass)
        device_entry = device_registry.async_get_device(
            connections={(dr.CONNECTION_NETWORK_MAC, mac)},
        )
        if not device_entry:
            return self.json_message("device not found", 400)
        device_registry.async_update_device(device_entry.id, name=name)
        hass.config_entries.async_update_entry(entry, title=name)
        return self.json_message("ok")


class HuijianDeviceInfoView(HuijianHttpView):
    """按 mac（或 speak_id）查卫星设备入驻信息——小程序 queryHaDevice 的缺失路由。

    三项目适配判定书缺口4：小程序 ha-connect/setup 配网后需拿设备
    host:port（6053，供显示设备网页配置入口/后续 mcp 跳转），但集成
    历史上没有这个 View → queryHaDevice 404 静默失败（setup.js 只置空
    host，不炸但功能缺）。
    数据真源=config entry data（_async_make_config_data 写入的
    CONF_HOST/CONF_PORT + speak_id/mac/mcp_endpoint/device_name）。
    安全：requires_auth=True——返回内网拓扑，必须 HA 长期令牌。调用对象
    是「已持有 HA token 的小程序/客户端」（扫码入驻本身靠 /setup/qrcode 的
    uuid 通道、不经此 View，token 为空是正常态）；mac 匹配不区分大小写
    （entry 存小写，设备上报可能大写）。assist 类（语音引擎服务）entry 无
    host 被跳过——本 View 只回答卫星设备，引擎端点走 assist 条目自身配置。
    """

    requires_auth = True
    url = "/api/huijian-ai/device-info"
    name = "api:huijian-ai:device-info"

    async def get(self, request: web.Request):
        hass = request.app[KEY_HASS]
        mac = (request.query.get("mac") or "").lower().strip()
        speak_id = request.query.get("speak_id") or ""
        if not mac and not speak_id:
            return self.json_message("mac or speak_id required", 400)
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            hit = (mac and str(entry.data.get("mac", "")).lower() == mac) or (
                speak_id and entry.data.get("speak_id") == speak_id
            )
            if not hit:
                continue
            host = entry.data.get("host")          # CONF_HOST 字面值
            port = entry.data.get("port", 6053)   # CONF_PORT；卫星 API 默认 6053
            if not host:
                continue                          # assist 类 entry 无 host，跳过
            return self.json({
                "ok": True,
                "host": host,
                "port": port,
                "mac": entry.data.get("mac", ""),
                "speak_id": entry.data.get("speak_id", ""),
                "device_name": entry.data.get("device_name", entry.title),
                "mcp_endpoint": entry.data.get("mcp_endpoint", ""),
                "config_type": entry.data.get("config_type", "device"),
            })
        return self.json_message("device not found", 404)


class HuijianSatellitesView(HuijianHttpView):
    """卫星台账（OTA 方案 Phase 2 集成侧，2026-09-23；加载项面板数据源）。

    每行=一个已加载的卫星 config entry：身份(mac/speak_id/host:port)、展示名
    与区域（device registry 单一事实源）、在线态（RuntimeEntryData.available，
    API 连接真源）、固件版本（三级回退：mac 台账→speak_id 台账（CMD20 入驻
    POST，本运行期）→entry.data（建账时版本，可能陈旧，fw_source 标「入驻时」）
    全缺=""=未上报）、以及设备端 OTA 接收口探测（entry_data.services 里带 ota
    字样的 user service——现网 v2.1.35 恒空，固件 Phase 1 落地后自动点亮，面板
    据此在「近场代发」与「远程下发」两态间切换，无需再改集成）。

    安全：requires_auth=True——含内网拓扑；加载项 ha_client 已持 HA 长期令牌，
    经 rest_get 调用（同 /api/huijian-ai/manage 系列数据面口径）。
    """

    requires_auth = True
    url = "/api/huijian-ai/satellites"
    name = "api:huijian-ai:satellites"

    async def get(self, request: web.Request):
        hass = request.app[KEY_HASS]
        domain_data = hass.data.setdefault(DOMAIN, {})
        ledger = domain_data.get("satellite_ledger", {})
        sid_ledger = domain_data.get("satellite_ledger_by_speakid", {})
        device_registry = dr.async_get(hass)
        areas = ar.async_get(hass)
        out = []
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            if not entry.data.get("host"):
                continue  # assist 引擎类条目不是卫星（与 device-info 同判定）
            mac = str(entry.data.get("mac", "") or "").lower()
            rd = getattr(entry, "runtime_data", None)
            ota_services = []
            for svc in (getattr(rd, "services", None) or {}).values():
                name = getattr(svc, "name", "") or ""
                if "ota" in name.lower() or "upgrade" in name.lower():
                    ota_services.append(name)
            area_name = ""
            device_id = ""
            if mac:
                dev = device_registry.async_get_device(
                    connections={(dr.CONNECTION_NETWORK_MAC, mac)})
                if dev:
                    device_id = dev.id
                    if dev.area_id and (a := areas.async_get_area(dev.area_id)):
                        area_name = a.name or ""
            speak_id = str(entry.data.get("speak_id", "") or "")
            # 固件版本三级回退（v1.0.65·契约 F-02）：mac 台账（speakname 口，
            # 预留演进）→ speak_id 台账（CMD20 入驻，本运行期实时）→ entry.data
            # （建账时持久化，跨重启可显但可能陈旧，如实标源）。
            rec = ledger.get(mac, {}) or sid_ledger.get(speak_id, {})
            fw_live = str(rec.get("fw_version", "") or "")
            fw_source = "实时" if fw_live else ""
            fw = fw_live or str(entry.data.get("fw_version", "") or "")
            if fw and not fw_source:
                fw_source = "入驻时"
            out.append({
                "entry_id": entry.entry_id,
                "name": entry.title or entry.data.get("device_name", ""),
                "mac": mac,
                "speak_id": speak_id,
                "host": entry.data.get("host", ""),
                "port": entry.data.get("port", 6053),
                "online": bool(getattr(rd, "available", False)),
                "area": area_name,
                "device_id": device_id,
                "fw_version": fw,
                "fw_source": fw_source,
                "fw_reported_at": rec.get("ts"),
                "ota_services": ota_services,
                # v1.0.80：面板「连续对话」列数据源（True/False/None=不可判）
                "continuous_dialogue": _continuous_state(hass, entry),
                # v1.0.87：None 的四种成因如实分开（面板按钮话术数据源）
                "continuous_dialogue_diag": _continuous_diag(hass, entry),
            })
        return self.json({"devices": out})


class HuijianSatelliteOtaView(HuijianHttpView):
    """v1.0.74 OTA 真下发中继：加载项 /api/firmware/dispatch 签好一次性链接后来
    此调用——body {mac|entry_id, url}，本视图经已建连的 :6053 Noise 通道调设备
    用户服务 ota_upgrade(url)。URL 来源校验**不在此重复实现**：设备侧
    Ota::IsUpgradeUrlAllowed 白名单闸只收私网字面 IPv4+http（单一事实源）；
    领取令牌一次性/TTL 在加载项固件仓。永不抛全路径折叠 200 JSON——加载项
    rest_write 契约要求结构化 error 可读，不许裸 500。"""

    url = "/api/huijian-ai/satellites/ota"
    name = "api:huijian-ai:satellites-ota"
    # 写命令通道（向设备下发固件 URL）：同卫星台账面口径必须 HA 令牌——
    # 匿名 POST 能把任意 URL 喷向在线卫星，设备白名单闸只限网段不限意图。
    requires_auth = True

    async def post(self, request: web.Request):
        hass = request.app[KEY_HASS]
        try:
            body = await request.json() or {}
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        mac = str(body.get("mac", "") or "").strip().lower()
        entry_id = str(body.get("entry_id", "") or "").strip()
        url = str(body.get("url", "") or "").strip()
        if not url.startswith("http://"):
            return self.json({"success": False,
                              "error": "url 缺失或非 http（设备白名单闸只收私网字面 IPv4）"})
        if not mac and not entry_id:
            return self.json({"success": False, "error": "mac/entry_id 必填其一"})
        target = None
        for ent in hass.config_entries.async_loaded_entries(DOMAIN):
            if not ent.data.get("host"):
                continue  # assist 引擎条目不是卫星（与 satellites 台账同判定）
            if (mac and str(ent.data.get("mac", "") or "").lower() == mac) or \
               (entry_id and ent.entry_id == entry_id):
                target = ent
                break
        if target is None:
            return self.json({"success": False,
                              "error": f"卫星台账无此设备（mac={mac or entry_id}）——"
                                       "配网入驻后且 HA 已连接才会出现在台账"})
        rd = getattr(target, "runtime_data", None)
        client = getattr(rd, "client", None) if rd is not None else None
        if rd is None or client is None or not getattr(rd, "available", False):
            return self.json({"success": False, "error": "设备离线或 API 通道未建立"})
        svc = None
        for s in (getattr(rd, "services", None) or {}).values():
            name = getattr(s, "name", "") or ""
            if "ota" in name.lower() or "upgrade" in name.lower():
                svc = s
                break
        if svc is None:
            return self.json({"success": False,
                              "error": "设备固件无 ota_upgrade 接收口（<v2.1.36）——"
                                       "请退回面板签发链接备存形态"})
        try:
            await client.execute_service(svc, {"url": url})
        except Exception as e:
            _LOGGER.warning("[OTA] 调设备 %s 失败: %s", svc.name, e)
            return self.json({"success": False, "error": f"服务调用失败: {e}"[:160]})
        _LOGGER.info("[OTA] ota_upgrade 已下发 %s (mac=%s)", target.title, mac or "-")
        return self.json({"success": True, "service": svc.name})


_CONT_SUFFIX = "-continuous_dialogue_switch"  # unique_id 契约：MAC-object_id（aioesphomeapi
# build_unique_id 上游式）；object_id 由固件 v2.1.46 钉死 "continuous_dialogue_switch"


# v1.0.87：连续对话态诊断码。v1.0.80 的 None 三态把"真无实体/实体被禁用/设备
# 离线/实体未就绪"混成一句"需固件≥2.1.46"——现场据此换固件仍不亮，白折腾一轮
# （v1.0.65 F-OTA-04 纪律：一种文案不背两种锅）。True/False/None 对外契约不变
# （面板 data-on 与 v1080 钉仍吃它），细分原因另走 continuous_dialogue_diag。
_CONT_ON, _CONT_OFF = "on", "off"
_CONT_MISSING = "no_entity"      # 注册表无此实体：固件 < v2.1.46（或从未上报）
_CONT_DISABLED = "disabled"      # 实体存在但在 HA 被禁用（用户在 HA 实体页关过）
_CONT_OFFLINE = "offline"        # 实体在，HA 态 unavailable：设备/API 断链
_CONT_PENDING = "pending"        # 实体在，还没有态：HA 刚起/平台未加载完
_CONT_ERRORS = {
    _CONT_MISSING: "该设备无「连续对话」实体（固件需 ≥v2.1.46）",
    _CONT_DISABLED: "「连续对话」实体在 HA 中被禁用——请在 HA 里启用该实体后再试",
    _CONT_OFFLINE: "设备当前离线（API 连接断开），开关指令无法送达——上线后再试",
    _CONT_PENDING: "设备实体尚未就绪（HA 刚重启/平台加载中）——稍后重试",
}


def _continuous_entity(hass, entry):
    """→ (entity_id | None, 诊断码)。禁用实体不作为可写实体，但单独报因。

    注意 er 的注册表条目**含禁用实体**：不查 ent.disabled 就会把"被禁用"当成
    "可以 turn_on"，服务调用必然以"服务调用失败: …"回话，病因被折叠成怪症状。
    """
    reg = er.async_get(hass)
    disabled = None
    for ent in er.async_entries_for_config_entry(reg, entry.entry_id):
        if not (ent.entity_id.startswith("switch.")
                and str(ent.unique_id or "").endswith(_CONT_SUFFIX)):
            continue
        if ent.disabled:
            disabled = disabled or ent.entity_id
            continue
        return ent.entity_id, ""
    return None, (_CONT_DISABLED if disabled else _CONT_MISSING)


def _continuous_diag(hass, entry) -> str:
    """台账诊断码：on|off|no_entity|disabled|offline|pending（面板话术数据源）。"""
    eid, reason = _continuous_entity(hass, entry)
    if eid is None:
        return reason
    st = hass.states.get(eid)
    if st is None:
        return _CONT_PENDING
    if st.state == _CONT_ON:
        return _CONT_ON
    if st.state == _CONT_OFF:
        return _CONT_OFF
    return _CONT_OFFLINE if st.state == "unavailable" else _CONT_PENDING


def _continuous_state(hass, entry):
    """True/False=实体在且可达；None=不可判（无实体/禁用/离线/未就绪）。"""
    d = _continuous_diag(hass, entry)
    return True if d == _CONT_ON else (False if d == _CONT_OFF else None)


class HuijianSatelliteContinuousView(HuijianHttpView):
    """v1.0.80 连续对话开关（加载项面板用）：写设备同一个 esphome switch 实体
    ——设备侧 setContinuousDialogue 是 NVS 唯一收口（BLE/HA/面板三边同源回显，
    固件 v2.1.46 deferred publish）。永不抛折叠 200 JSON（OTA 中继视图同纪律）。"""

    url = "/api/huijian-ai/satellites/continuous"
    name = "api:huijian-ai:satellites-continuous"
    requires_auth = True

    async def post(self, request: web.Request):
        hass = request.app[KEY_HASS]
        try:
            body = await request.json() or {}
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        mac = str(body.get("mac", "") or "").strip().lower()
        entry_id = str(body.get("entry_id", "") or "").strip()
        enabled = bool(body.get("enabled", False))
        if not mac and not entry_id:
            return self.json({"success": False, "error": "mac/entry_id 必填其一"})
        target = None
        for ent in hass.config_entries.async_loaded_entries(DOMAIN):
            if not ent.data.get("host"):
                continue
            if (mac and str(ent.data.get("mac", "") or "").lower() == mac) or \
               (entry_id and ent.entry_id == entry_id):
                target = ent
                break
        if target is None:
            return self.json({"success": False,
                              "error": f"卫星台账无此设备（mac={mac or entry_id}）"})
        eid, reason = _continuous_entity(hass, target)
        if eid is None:
            return self.json({"success": False, "error": _CONT_ERRORS[reason]})
        if _continuous_diag(hass, target) == _CONT_OFFLINE:
            # 离线时服务调用只会以异常收口（"服务调用失败: Connection…"），
            # 病因说是连接还是固件全靠猜——这里直接点名，且省掉下面的复核等待。
            return self.json({"success": False,
                              "error": _CONT_ERRORS[_CONT_OFFLINE]})
        try:
            await hass.services.async_call(
                "switch", "turn_on" if enabled else "turn_off",
                {"entity_id": eid}, blocking=True)
        except Exception as e:
            _LOGGER.warning("[连续对话] 调实体失败 %s: %s", eid, e)
            return self.json({"success": False, "error": f"服务调用失败: {e}"[:160]})
        # v1.0.87（现场"点了没反应，再点一次才变"根修）：esphome 的 switch_command
        # 是即发即回（写进发送缓冲就返回，blocking=True 也只代表服务调用完成），
        # 真态要等设备 NVS 写 + deferred publish 回灌（固件 v2.1.46 一拍）。旧形态
        # 面板拿 success 立刻重读台账 → 读到旧态 → 按钮纹丝不动。这里有界复核
        # ≤1.6s，把"设备已回显"与"仅令已下发"分成两句话，面板据此出话术。
        echoed = False
        for _ in range(8):
            await asyncio.sleep(0.2)
            st = hass.states.get(eid)
            if st is not None and (st.state == _CONT_ON) == enabled:
                echoed = True
                break
        _LOGGER.info("[连续对话] %s → %s (mac=%s) 设备回显=%s", eid,
                     "on" if enabled else "off", mac or entry_id, echoed)
        return self.json({"success": True, "entity_id": eid, "enabled": enabled,
                          "echoed": echoed})


def parse_tts_stt_options(raw):
    """options query 参数 → dict（审查修复 2026-09-21）。旧版把 query string
    原样直传 async_create_result_stream，core 里 options.pop → AttributeError
    ——该参数一传即 400，形同虚设。显式 JSON 解析并校验必须为对象；缺省 {}。"""
    if not raw:
        return {}
    try:
        opts = json.loads(raw)
    except ValueError:
        raise ValueError("options 必须为 JSON 对象字符串")
    if not isinstance(opts, dict):
        raise ValueError("options 必须为 JSON 对象")
    return opts


def pick_default_entities(loaded_entries):
    """多条目取默认实体的**first-wins** 规则（审查修复 2026-09-21）。
    旧循环"最后一条 entry 覆盖"——多设备条目下取到随机末位的配置
    （张冠李戴）。各键取首个显式配置者；皆无则定案默认。"""
    conf_tts = conf_stt = None
    for entry in loaded_entries:
        if not conf_tts:
            conf_tts = entry.options.get(CONF_TTS_ENTITY_ID)
        if not conf_stt:
            conf_stt = entry.options.get(CONF_STT_ENTITY_ID)
        if conf_tts and conf_stt:
            break
    return (conf_tts or "tts.huijian_speech",
            conf_stt or "stt.huijian_asr")


class HuijianTtsSttView(HuijianHttpView):
    requires_auth = True
    url = "/api/huijian-ai/tts-stt"
    name = "api:huijian-ai:tts-stt"

    async def get(self, request: web.Request):
        hass = request.app[KEY_HASS]
        message = request.query.get("message")
        if not message or not message.strip():
            return self.json_message("message 必填", 400)

        default_tts, default_stt = pick_default_entities(
            hass.config_entries.async_loaded_entries(DOMAIN))

        tts_entity = request.query.get("tts_entity", default_tts)
        stt_entity = request.query.get("stt_entity", default_stt)

        try:
            options = parse_tts_stt_options(request.query.get("options"))
        except ValueError as err:
            return self.json({"error": str(err)}, 400)
        try:
            stream = hass.data["tts_manager"].async_create_result_stream(
                engine=tts_entity,
                use_file_cache=not request.query.get("nocache"),
                options=options,
            )
        except Exception as err:
            return self.json({"error": str(err)}, 400)
        stream.async_set_message(message)

        stt_entity = hass.data["stt"].get_entity(stt_entity)
        if not stt_entity:
            return self.json_message("stt entity not found", 400)

        from homeassistant.components import stt

        from .audio import async_convert_audio

        metadata = stt.SpeechMetadata(
            language="zh",
            format=stt.AudioFormats.WAV,
            codec=stt.AudioCodecs.PCM,
            bit_rate=stt.AudioBitRates.BITRATE_16,
            sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
            channel=stt.AudioChannels.CHANNEL_MONO,
        )
        converting = async_convert_audio(
            hass,
            stream.async_stream_result(),
            stream.extension,
            to_extension=metadata.format.value,
            to_sample_rate=metadata.sample_rate.value,
        )
        result = await stt_entity.async_process_audio_stream(metadata, converting)
        return self.json(
            {
                "text": result.text,
                "result": result.result,
            }
        )


def calculate_sign(uri, params, mac, salt):
    """
    签名算法:
    1. n = sha256(uri)
    2. 拼接参数字符串并计算 m = sha256(参数字符串)
    3. response = sha256(m + n + mac + salt)

    跨端约定（勿破坏）：
    · uri 必须是**纯路径**（request.path / 固件侧固定路径字面量），**不得含
      host/scheme/query**——固件 hashAuthorization（0513gujian ble_manager.cc）
      以纯路径参与哈希，任何一侧把 host 或 query 卷进来都会签名失配 400。
    · params 按 key ASCII 字典序拼 k=v&；固件 std::map 天然同序，Python 侧
      sorted() 同序。POST 用 body JSON 作为 params 集合（request.json()），
      固件 r_postDeviceName 用同名字段 map——两侧字段名/值必须逐字一致。
    · mac 参与前统一小写（固件侧也转小写）；salt 由请求方生成经 Salt 头携带。
    """
    # 步骤1: 计算 n = sha256(uri)
    n = hashlib.sha256(uri.encode("utf-8")).hexdigest()

    # 步骤2: 拼接参数并计算 m = sha256(参数字符串)
    # 将参数排序后拼接成 key=value 格式
    sorted_params = sorted(params.items(), key=lambda x: x[0])
    param_str = "&".join([f"{k}={v}" for k, v in sorted_params])
    m = hashlib.sha256(param_str.encode("utf-8")).hexdigest()

    # 步骤3: 计算最终摘要
    response_str = f"{m}{n}{mac}{salt}"
    return hashlib.sha256(response_str.encode("utf-8")).hexdigest()
