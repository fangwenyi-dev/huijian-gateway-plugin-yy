import asyncio
import html as html_mod
import logging
import re
from datetime import datetime
from pathlib import Path

from aiohttp import web
from homeassistant.const import ATTR_FRIENDLY_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent as ha_intent
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView

from .const import DOMAIN
from .intent_automation import get_automation_manager, get_automation_store
from .intent_voice_scene import get_voice_scene_store

_LOGGER = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_TEMPLATE_CACHE: dict[str, str] = {}


def _load_template(filename: str) -> str:
    if filename not in _TEMPLATE_CACHE:
        template_path = _TEMPLATES_DIR / filename
        _TEMPLATE_CACHE[filename] = template_path.read_text(encoding="utf-8")
    return _TEMPLATE_CACHE[filename]


def _js(s) -> str:
    """嵌入 onclick='f(\'…\')' 的字符串：先 JS 单引号层转义（反斜杠/单引号/换行），
    再 HTML 属性层转义（& < > " '）。顺序不可反。

    v1.0.41（F1）：onclick 属性值要过**两层解析器**（HTML 属性 → JS 词法）。
    只做 html 转义时，`'` 解码回 JS 单引号直接把字符串截断（XSS 注入面）；
    而"先 html 转义再嵌、页面上再 escape 一次"的双层转义会让浏览器解码
    一层后 JS 拿到 `a&#x27;b` 字面量——编辑弹窗回写即数据污染。
    凡进 onclick 的值一律 _js(原始值)；纯展示上下文（id 属性/正文 span）仍用
    html_mod.escape(原始值)，两不相干。"""
    t = str(s).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")
    return html_mod.escape(t, quote=True)


async def async_setup_api(hass: HomeAssistant):
    """Set up the voice scenes and automations API."""
    hass.http.register_view(VoiceScenesListView)
    hass.http.register_view(VoiceSceneDeleteView)
    hass.http.register_view(AutomationsListView)
    hass.http.register_view(AutomationDeleteView)
    hass.http.register_view(AutomationsManageView)
    hass.http.register_view(CombinedManageView)
    hass.http.register_view(AutomationLogView)
    hass.http.register_view(TestSceneView)
    hass.http.register_view(TestAutomationView)


def _extract_device_info(action: dict) -> str:
    """Extract device info from action for display."""
    intent_name = action.get("intent") or action.get("name", "Unknown")
    params = action.get("params") or action.get("parameters", {})
    target = params.get("target", [])

    device_info_parts = []
    for t in target:
        area = t.get("area", "")
        devices = t.get("devices", [])
        for device in devices:
            domains = device.get("domains", [])
            name = device.get("name", "")
            if area:
                device_info_parts.append(f"{area} {'/'.join(domains)}")
            elif name:
                device_info_parts.append(f"{name}({','.join(domains)})")
            else:
                device_info_parts.append("/".join(domains))

    if not device_info_parts:
        return intent_name

    return f"{intent_name} -> {', '.join(device_info_parts)}"


def _get_action_summary(action: dict) -> str:
    """Get a short summary of an action."""
    intent_name = action.get("intent") or action.get("name", "Unknown")
    params = action.get("params") or action.get("parameters", {})
    target = params.get("target", [])

    summaries = []
    for t in target:
        area = t.get("area", "")
        devices = t.get("devices", [])
        for device in devices:
            domains = device.get("domains", [])
            name = device.get("name", "")
            if area:
                if domains:
                    summaries.append(f"{area} {'/'.join(domains)}")
                else:
                    summaries.append(area)
            elif name:
                summaries.append(f"{name}")
            else:
                summaries.append("/".join(domains) if domains else "")

    return f"{intent_name} {', '.join(filter(None, summaries))}"


_TRIGGER_ENTITY_RE = re.compile(r"[a-z0-9_]{1,64}\.[a-z0-9_]{1,64}")


def _validate_trigger(trigger) -> str:
    """PUT 形态闸（v1.0.41 F2）：过闸返回 ""，违规返回字段名。

    局域网任意客户端可 PUT 任意 trigger dict：不带闸时 `above:"a',alert(
    document.cookie),('"` 原样入库（喂给渲染层的 XSS 面），非数值 junk 又让
    trigger_eval 误判恒不触发。规则（与仓内既有 trigger 形态兼容）：
      · trigger 必须是 dict；
      · entity_id（若给）必须是标准小写 HA id：``[a-z0-9_]{1,64}\\.[a-z0-9_]{1,64}``；
      · above/below（若给）必须 int/float 且不得是 bool（JSON true 不是阈值）；
      · to/attribute/platform（若给）只许 str(≤128)/bool/int/float；
      · 未知键：标量放行（at/for 等合法形态要过），dict/list 拒。
    """
    if not isinstance(trigger, dict):
        return "trigger"
    for key, val in trigger.items():
        if key == "entity_id":
            if not isinstance(val, str) or not _TRIGGER_ENTITY_RE.fullmatch(val):
                return "entity_id"
        elif key in ("above", "below"):
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                return key
        elif key in ("to", "attribute", "platform"):
            if isinstance(val, (dict, list)):
                return key
            if isinstance(val, str) and len(val) > 128:
                return key
        elif isinstance(val, (dict, list)):
            return key
    return ""


class VoiceScenesListView(HomeAssistantView):
    requires_auth = False
    url = "/api/huijian-ai/voice-scenes"
    name = "api:huijian-ai:voice-scenes"

    async def get(self, request: web.Request):
        """Get all voice scenes with detailed info."""
        hass = request.app[KEY_HASS]
        try:
            store = get_voice_scene_store(hass)
            scenes = await store.get_all_scenes()

            scene_list = []
            for scene in scenes:
                actions = scene.get("actions", [])
                device_details = [_extract_device_info(a) for a in actions]
                action_summaries = [_get_action_summary(a) for a in actions]

                scene_list.append(
                    {
                        "scene_id": scene.get("scene_id"),
                        "trigger_phrase": scene.get("trigger_phrase"),
                        "action_count": len(actions),
                        "device_details": device_details,
                        "action_summaries": action_summaries,
                        "created_at": scene.get("created_at"),
                    }
                )

            return self.json({"success": True, "scenes": scene_list})
        except Exception as e:
            _LOGGER.error("Failed to get voice scenes: %s", e)
            return self.json({"success": False, "error": str(e)}, 500)


class VoiceSceneDeleteView(HomeAssistantView):
    requires_auth = False
    url = "/api/huijian-ai/voice-scenes/{scene_id}"
    name = "api:huijian-ai:voice-scenes:delete"

    async def delete(self, request: web.Request, scene_id: str):
        """Delete a voice scene."""
        hass = request.app[KEY_HASS]
        try:
            store = get_voice_scene_store(hass)
            success, message = await store.delete_scene(scene_id=scene_id)

            if success:
                return self.json({"success": True, "message": message})
            else:
                return self.json({"success": False, "error": message}, 404)
        except Exception as e:
            _LOGGER.error("Failed to delete voice scene: %s", e)
            return self.json({"success": False, "error": str(e)}, 500)

    async def put(self, request: web.Request, scene_id: str):
        """Update a voice scene's trigger phrase and/or actions."""
        hass = request.app[KEY_HASS]
        try:
            body = await request.json()
            _LOGGER.info("Updating voice scene %s: body=%s", scene_id, body)
            store = get_voice_scene_store(hass)
            success, message = await store.update_scene(
                scene_id,
                trigger_phrase=body.get("trigger_phrase"),
                actions=body.get("actions"),
            )
            _LOGGER.info("Update scene result: success=%s, message=%s", success, message)
            return self.json(
                {
                    "success": success,
                    "message": message if success else None,
                    "error": message if not success else None,
                },
                200 if success else 400,
            )
        except Exception as e:
            _LOGGER.error("Failed to update voice scene %s: %s", scene_id, e, exc_info=True)
            return self.json({"success": False, "error": str(e)}, 500)


class AutomationLogView(HomeAssistantView):
    requires_auth = False
    url = "/api/huijian-ai/automation-logs"
    name = "api:huijian-ai:automation-logs"

    async def get(self, request: web.Request):
        hass = request.app[KEY_HASS]
        mgr = get_automation_manager(hass)
        return self.json(mgr.trigger_logs)


class TestSceneView(HomeAssistantView):
    requires_auth = False
    url = "/api/huijian-ai/test-scene"
    name = "api:huijian-ai:test-scene"

    async def post(self, request: web.Request):
        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "Invalid JSON"}, status_code=400)
        trigger_phrase = (body.get("trigger_phrase", "") or "").strip()
        if not trigger_phrase:
            return self.json({"success": False, "error": "trigger_phrase is required"}, status_code=400)
        hass = request.app[KEY_HASS]
        try:
            store = get_voice_scene_store(hass)
            scene = await store.get_scene_by_trigger(trigger_phrase)
            if not scene:
                return self.json(
                    {"success": False, "error": f"未找到触发词'{trigger_phrase}'对应的场景"},
                    status_code=404,
                )

            actions = scene.get("actions", [])
            if not actions:
                return self.json(
                    {"success": False, "error": "场景没有配置任何动作"},
                    status_code=400,
                )

            executed = []
            has_errors = False
            for action in actions:
                intent_name = action.get("intent") or action.get("name")
                params = action.get("params") or action.get("parameters", {})
                ha_slots = {k: {"value": v} for k, v in params.items()}
                try:
                    async with asyncio.timeout(30):
                        await ha_intent.async_handle(
                            hass, DOMAIN, intent_name, slots=ha_slots,
                        )
                    executed.append({"intent": intent_name, "result": "success"})
                except asyncio.TimeoutError:
                    has_errors = True
                    _LOGGER.error("Test scene action timed out: %s", intent_name)
                    executed.append({"intent": intent_name, "result": "error", "error": "执行超时"})
                except Exception as e:
                    has_errors = True
                    _LOGGER.error("Test scene action failed: %s: %s", intent_name, e)
                    executed.append({"intent": intent_name, "result": "error", "error": str(e)})

            if has_errors:
                return self.json({"success": False, "error": "部分动作执行失败", "executed": executed})
            return self.json({"success": True, "message": "测试完成"})
        except Exception as e:
            _LOGGER.error("Test scene failed: %s", e, exc_info=True)
            return self.json({"success": False, "error": str(e)}, status_code=500)


class TestAutomationView(HomeAssistantView):
    requires_auth = False
    url = "/api/huijian-ai/test-automation"
    name = "api:huijian-ai:test-automation"

    async def post(self, request: web.Request):
        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "Invalid JSON"}, status_code=400)
        automation_id = (body.get("automation_id", "") or "").strip()
        if not automation_id:
            return self.json({"success": False, "error": "automation_id is required"}, status_code=400)
        try:
            store = get_automation_store(request.app[KEY_HASS])
            automations = await store.get_all_automations()
            automation = None
            for a in automations:
                if a.get("automation_id") == automation_id:
                    automation = a
                    break
            if not automation:
                return self.json({"success": False, "error": "Automation not found"}, status_code=404)
            mgr = get_automation_manager(request.app[KEY_HASS])
            async with asyncio.timeout(30):
                outcomes = await mgr._execute_actions(automation.get("actions", []))
            # v1.0.41（F4）：动作级折算——上方 TestSceneView 已是诚实口径，这里
            # 不再"全失败也报成功"（旧版 _execute_actions 吞异常，success 恒 True）。
            # 保持 HTTP 200，靠 success 旗标 + error 文案如实上报。
            ntotal = len(outcomes)
            fails = [(intent, err) for intent, ok, err in outcomes if not ok]
            if fails:
                first_err = next((e for _i, e in fails if e), "动作执行失败")
                return self.json(
                    {
                        "success": False,
                        "error": f"{len(fails)}/{ntotal} 个动作执行失败：{first_err[:120]}",
                    }
                )
            mgr._add_trigger_log(automation_id, "", "test", "手动测试触发")
            return self.json({"success": True, "executed": ntotal})
        except Exception as e:
            return self.json({"success": False, "error": str(e)}, status_code=500)


class CombinedManageView(HomeAssistantView):
    requires_auth = False
    url = "/api/huijian-ai/manage-page"
    name = "api:huijian-ai:manage-page"

    async def get(self, request: web.Request):
        hass = request.app[KEY_HASS]

        scene_store = get_voice_scene_store(hass)
        auto_store = get_automation_store(hass)
        scenes_raw = await scene_store.get_all_scenes()
        automations_raw = await auto_store.get_all_automations()

        scene_cards_html = ""
        auto_cards_html = ""

        for scene in scenes_raw:
            scene_id_raw = str(scene.get("scene_id", ""))
            trigger_raw = str(scene.get("trigger_phrase", ""))
            scene_id = html_mod.escape(scene_id_raw)
            trigger = html_mod.escape(trigger_raw)
            created = scene.get("created_at", "")
            created_display = ""
            if created:
                try:
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    created_display = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    created_display = str(created)
            actions_raw = scene.get("actions", [])
            action_summaries = [_action_to_text(a) for a in actions_raw]
            action_count = len(action_summaries)

            actions_html = ""
            for s in action_summaries:
                actions_html += f'<div class="action-item">- {html_mod.escape(s)}</div>'

            scene_cards_html += f"""
<div class="card scene" id="scene-{scene_id}">
    <div class="card-header">
        <div><span class="card-trigger scene">"{trigger}"</span><span class="card-tag scene">语音场景</span></div>
        <div>
            <button class="delete-btn" onclick="deleteScene('{_js(scene_id_raw)}', '{_js(trigger_raw)}', event)">删除</button>
            <button class="edit-btn" onclick="openEditScene('{_js(scene_id_raw)}', '{_js(trigger_raw)}')">编辑</button>
            <button class="test-btn" onclick="testScene('{_js(trigger_raw)}', event)">测试</button>
        </div>
    </div>
    <div class="info">创建时间: {created_display}</div>
    <div class="actions-box">
        <div class="actions-title">执行动作 ({action_count}个):</div>
        {actions_html}
    </div>
</div>"""

        for auto in automations_raw:
            auto_id_raw = str(auto.get("automation_id", ""))
            auto_id = html_mod.escape(auto_id_raw)
            trigger_entity = auto.get("trigger", {}).get("entity_id", "")
            friendly = _entity_id_to_friendly(hass, trigger_entity)
            above = auto.get("trigger", {}).get("above")
            below = auto.get("trigger", {}).get("below")
            at = str(auto.get("trigger", {}).get("at") or "").strip()
            to_val = auto.get("trigger", {}).get("to")
            cond_parts = []
            if above is not None:
                cond_parts.append(f"> {above}度")
            if below is not None:
                cond_parts.append(f"< {below}度")
            if at:
                # v1.0.32 时间触发卡（旧渲染 entity 为空 → 标题整个空白）
                trigger_display = f"每天 {at} 自动执行"
                kind_tag = "时间自动化"
                edit_btn_html = ""      # 编辑弹窗仅支持传感器形态，时间档给删除重建
            else:
                if to_val is not None:
                    cond_parts.append(
                        "检测到有人" if str(to_val) == "on" else f"状态={to_val}")
                trigger_display = (
                    f"{friendly} {'、'.join(cond_parts)}" if cond_parts else friendly
                )
                kind_tag = "状态自动化" if to_val is not None else "传感器自动化"
                # 编辑弹窗只认 entity+above/below——to/at 形态给了会误导
                # （保存即覆盖成丢 to 的形态），一律以删除重建为准（v1.0.32）
                # v1.0.41（F1）：onclick 内四个实参全部 _js(原始值)。旧实现
                # above/below 裸插值（JS+HTML 双层皆穿），entity 只 html 转义
                # （单引号解码后照样截断 JS 字符串）。
                above_js = _js("" if above is None else above)
                below_js = _js("" if below is None else below)
                edit_btn_html = "" if to_val is not None else (
                    f"""<button class="edit-btn" onclick="openEditAuto('{_js(auto_id_raw)}', """
                    f"""'{_js(trigger_entity)}', '{above_js}', '{below_js}')">编辑</button>"""
                )

            created = auto.get("created_at", "")
            created_display = ""
            if created:
                try:
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    created_display = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    created_display = str(created)

            last_triggered = auto.get("last_triggered")
            trigger_info = " | 尚未触发"
            if last_triggered:
                try:
                    dt = datetime.fromisoformat(
                        str(last_triggered).replace("Z", "+00:00")
                    )
                    trigger_info = f" | 上次触发: {dt.strftime('%Y-%m-%d %H:%M')}"
                except Exception:
                    trigger_info = " | 已触发"

            actions_raw = auto.get("actions", [])
            summaries = [_action_to_text(a) for a in actions_raw]
            count = len(summaries)
            actions_html = ""
            for s in summaries:
                actions_html += f'<div class="action-item">- {html_mod.escape(s)}</div>'

            auto_cards_html += f"""
<div class="card auto" id="auto-{auto_id}">
    <div class="card-header">
        <div><span class="card-trigger auto">{html_mod.escape(trigger_display)}</span><span class="card-tag auto">{kind_tag}</span></div>
        <button class="delete-btn" onclick="deleteAutomation('{_js(auto_id_raw)}', '{_js(trigger_display)}', event)">删除</button>
        {edit_btn_html}
        <button class="test-btn" onclick="testAutomation('{_js(auto_id_raw)}', event)">测试</button>
    </div>
    <div class="info">创建时间: {created_display}{trigger_info}</div>
    <div class="actions-box">
        <div class="actions-title">执行动作 ({count}个):</div>
        {actions_html}
    </div>
</div>"""

        has_scenes = len(scene_cards_html) > 0
        has_autos = len(auto_cards_html) > 0

        if not has_scenes and not has_autos:
            content_html = '<div class="empty-state">暂无智能场景<br><br>通过语音创建语音场景，如："当我说晚安的时候，帮我关灯"<br>或<br>创建传感器自动化，如："当温度大于29度就打开窗户"</div>'
        else:
            parts = ""
            if has_scenes:
                parts += '<div class="section-title">语音场景</div>' + scene_cards_html
            if has_autos:
                parts += (
                    '<div class="section-title">传感器自动化</div>' + auto_cards_html
                )
            content_html = parts

        template = _load_template("manage.html")
        html_content = template.replace("{content_html}", content_html)
        return web.Response(text=html_content, content_type="text/html")


def _entity_id_to_friendly(hass: HomeAssistant, entity_id: str) -> str:
    """Resolve entity_id to a human-friendly name."""
    if not entity_id:
        return ""
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(entity_id)
    if entry and (entry.name or entry.original_name):
        return entry.name or entry.original_name
    parts = entity_id.split(".")
    if len(parts) > 1:
        return parts[1].replace("_", "").replace("-", "")
    return entity_id


def _action_to_text(action: dict) -> str:
    """Convert an action dict to user-friendly text like '打开办公室筒灯'."""
    intent_name = action.get("name") or action.get("intent", "")
    params = action.get("parameters") or action.get("params", {})
    target = params.get("target", [])
    action_text = ""
    if intent_name == "ControlWindow":
        action_map = {"open": "打开", "close": "关闭", "pause": "暂停", "a": "内倒"}
        raw_action = params.get("action", "")
        action_text = action_map.get(raw_action, raw_action + "窗户")
    elif intent_name == "TurnDeviceOn":
        action_text = "打开"
    elif intent_name == "TurnDeviceOff":
        action_text = "关闭"
    elif intent_name == "AdjustDeviceAttribute":
        action_text = "调节"
    elif intent_name == "SetDeviceMode":
        action_text = "设置模式"
    else:
        action_text = intent_name

    device_parts = []
    for t in target:
        area = t.get("area", "")
        devices = t.get("devices", [])
        for d in devices:
            name = d.get("name", "")
            domains = d.get("domains", [])
            if area:
                device_parts.append(f"{area}的{name or '/'.join(domains)}")
            elif name:
                device_parts.append(name)
            else:
                device_parts.append("/".join(domains))

    if device_parts:
        return f"{action_text}{'、'.join(device_parts)}"
    return action_text


def _trigger_to_text(trigger: dict) -> str:
    """Convert a trigger dict to user-friendly text like '办公室温度 > 27度'."""
    entity_id = trigger.get("entity_id", "")
    above = trigger.get("above")
    below = trigger.get("below")
    condition = ""
    if above is not None:
        condition += f" > {above}度"
    if below is not None:
        condition += f" < {below}度" if condition else f" < {below}度"
    return f"{entity_id}{condition}"


def _extract_automation_info(
    automation: dict, hass: HomeAssistant | None = None
) -> dict:
    """Extract automation info for display with user-friendly names."""
    trigger = automation.get("trigger", {})
    actions = automation.get("actions", [])

    entity_id = trigger.get("entity_id", "")
    friendly_name = entity_id
    if hass:
        friendly_name = _entity_id_to_friendly(hass, entity_id)

    above = trigger.get("above")
    below = trigger.get("below")
    condition_parts = []
    if above is not None:
        condition_parts.append(f"> {above}度")
    if below is not None:
        condition_parts.append(f"< {below}度")

    trigger_display = (
        f"{friendly_name} {'、'.join(condition_parts)}"
        if condition_parts
        else friendly_name
    )

    action_summaries = [_action_to_text(a) for a in actions]

    return {
        "automation_id": automation.get("automation_id"),
        "trigger_entity": entity_id,
        "trigger_friendly": friendly_name,
        "trigger_condition": "、".join(condition_parts) if condition_parts else "",
        "trigger_display": trigger_display,
        "action_count": len(actions),
        "action_summaries": action_summaries,
        "created_at": automation.get("created_at"),
        "last_triggered": automation.get("last_triggered"),
    }


class AutomationsListView(HomeAssistantView):
    requires_auth = False
    url = "/api/huijian-ai/automations"
    name = "api:huijian-ai:automations"

    async def get(self, request: web.Request):
        """Get all automations."""
        hass = request.app[KEY_HASS]
        try:
            store = get_automation_store(hass)
            automations = await store.get_all_automations()

            automation_list = [_extract_automation_info(a, hass) for a in automations]

            return self.json({"success": True, "automations": automation_list})
        except Exception as e:
            _LOGGER.error("Failed to get automations: %s", e)
            return self.json({"success": False, "error": str(e)}, 500)


class AutomationDeleteView(HomeAssistantView):
    requires_auth = False
    url = "/api/huijian-ai/automations/{automation_id}"
    name = "api:huijian-ai:automations:delete"

    async def delete(self, request: web.Request, automation_id: str):
        """Delete an automation."""
        hass = request.app[KEY_HASS]
        try:
            store = get_automation_store(hass)
            success, message = await store.delete_automation(automation_id)

            if success:
                return self.json({"success": True, "message": message})
            else:
                return self.json({"success": False, "error": message}, 404)
        except Exception as e:
            _LOGGER.error("Failed to delete automation: %s", e)
            return self.json({"success": False, "error": str(e)}, 500)

    async def put(self, request: web.Request, automation_id: str):
        """Update an automation's trigger and/or actions."""
        hass = request.app[KEY_HASS]
        try:
            body = await request.json()
            _LOGGER.info("Updating automation %s: body=%s", automation_id, body)
            store = get_automation_store(hass)
            existing = await store.get_automation(automation_id)
            if not existing:
                return self.json(
                    {"success": False, "error": f"未找到自动化ID'{automation_id}'"}, 404
                )

            trigger = body.get("trigger")
            actions = body.get("actions")
            if not trigger and not actions:
                return self.json(
                    {"success": False, "error": "请提供要修改的trigger或actions"}, 400
                )
            # v1.0.41（F2）：入库前形态闸——渲染层 _js 只是最后一道防御，
            # junk entity_id / 非数值阈值这类 trigger 从一开始就不该进 .storage。
            if trigger is not None:
                bad = _validate_trigger(trigger)
                if bad:
                    return self.json(
                        {"success": False, "error": f"trigger 字段不合规: {bad}"}, 400
                    )

            success, message = await store.update_automation(
                automation_id, trigger, actions
            )
            _LOGGER.info("Update automation result: success=%s, message=%s", success, message)
            return self.json(
                {
                    "success": success,
                    "message": message if success else None,
                    "error": message if not success else None,
                },
                200 if success else 400,
            )
        except Exception as e:
            _LOGGER.error("Failed to update automation %s: %s", automation_id, e, exc_info=True)
            return self.json({"success": False, "error": str(e)}, 500)


class AutomationsManageView(HomeAssistantView):
    requires_auth = False
    url = "/huijian-ai/automations/manage"
    name = "huijian-ai:automations:manage"

    async def get(self, request: web.Request):
        html_content = _load_template("automations.html")
        return web.Response(text=html_content, content_type="text/html")
