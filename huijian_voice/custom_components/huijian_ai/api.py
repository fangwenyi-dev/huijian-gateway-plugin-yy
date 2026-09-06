import asyncio
import html as html_mod
import logging
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
                await mgr._execute_actions(automation.get("actions", []))
            mgr._add_trigger_log(automation_id, "", "test", "手动测试触发")
            return self.json({"success": True})
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
            scene_id = html_mod.escape(str(scene.get("scene_id", "")))
            trigger = html_mod.escape(str(scene.get("trigger_phrase", "")))
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
            <button class="delete-btn" onclick="deleteScene('{scene_id}', '{html_mod.escape(trigger)}')">删除</button>
            <button class="edit-btn" onclick="openEditScene('{scene_id}', '{html_mod.escape(trigger)}')">编辑</button>
            <button class="test-btn" onclick="testScene('{html_mod.escape(trigger)}')">测试</button>
        </div>
    </div>
    <div class="info">创建时间: {created_display}</div>
    <div class="actions-box">
        <div class="actions-title">执行动作 ({action_count}个):</div>
        {actions_html}
    </div>
</div>"""

        for auto in automations_raw:
            auto_id = html_mod.escape(str(auto.get("automation_id", "")))
            trigger_entity = auto.get("trigger", {}).get("entity_id", "")
            friendly = _entity_id_to_friendly(hass, trigger_entity)
            above = auto.get("trigger", {}).get("above")
            below = auto.get("trigger", {}).get("below")
            cond_parts = []
            if above is not None:
                cond_parts.append(f"> {above}度")
            if below is not None:
                cond_parts.append(f"< {below}度")
            trigger_display = (
                f"{friendly} {'、'.join(cond_parts)}" if cond_parts else friendly
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
        <div><span class="card-trigger auto">{html_mod.escape(trigger_display)}</span><span class="card-tag auto">传感器自动化</span></div>
        <button class="delete-btn" onclick="deleteAutomation('{auto_id}', '{html_mod.escape(trigger_display)}')">删除</button>
        <button class="edit-btn" onclick="openEditAuto('{auto_id}', '{html_mod.escape(trigger_entity)}', '{above if above is not None else ""}', '{below if below is not None else ""}')">编辑</button>
        <button class="test-btn" onclick="testAutomation('{auto_id}')">测试</button>
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
