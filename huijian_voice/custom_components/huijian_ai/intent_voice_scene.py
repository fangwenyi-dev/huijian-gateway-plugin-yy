import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from homeassistant.helpers.storage import Store
from homeassistant.util.json import JsonObjectType

from .intent_device_shared import WINDOW_KEYWORDS, split_actions_by_device

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "huijian_voice_scenes"
STORAGE_VERSION = 1


class VoiceSceneStore:
    """Manage voice scene storage using HA's storage mechanism."""

    def __init__(self, hass: HomeAssistant):
        self._hass = hass
        self._store: Store | None = None
        self._data: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    async def _get_store(self) -> Store:
        """Get or create the store instance."""
        if self._store is None:
            self._store = Store(self._hass, STORAGE_VERSION, STORAGE_KEY)
        return self._store

    async def _load_data(self) -> dict[str, Any]:
        """Load data from storage."""
        if self._data is None:
            store = await self._get_store()
            self._data = await store.async_load() or {
                "version": 1,
                "scenes": {},
                "trigger_index": {},
            }
        return self._data

    async def _save_data(self, data: dict[str, Any]) -> None:
        """Save data to storage."""
        self._data = data
        store = await self._get_store()
        await store.async_save(data)

    async def get_scene_by_trigger(self, trigger_phrase: str) -> dict[str, Any] | None:
        """Get scene by trigger phrase."""
        data = await self._load_data()
        scene_id = data.get("trigger_index", {}).get(trigger_phrase)
        if scene_id:
            return data.get("scenes", {}).get(scene_id)
        return None

    async def get_scene_by_id(self, scene_id: str) -> dict[str, Any] | None:
        """Get scene by ID."""
        data = await self._load_data()
        return data.get("scenes", {}).get(scene_id)

    async def get_all_scenes(self) -> list[dict[str, Any]]:
        """Get all scenes."""
        data = await self._load_data()
        return list(data.get("scenes", {}).values())

    async def create_scene(
        self, trigger_phrase: str, actions: list[dict[str, Any]]
    ) -> tuple[bool, str]:
        """Create a new scene.

        Returns:
            tuple: (success, scene_id or error_message)
        """
        async with self._lock:
            data = await self._load_data()

            if trigger_phrase in data.get("trigger_index", {}):
                return False, f"触发词'{trigger_phrase}'已存在，请使用其他词"

            scene_id = f"voice_scene_{datetime.now().strftime('%Y%m%d%H%M%S')}"
            scene = {
                "scene_id": scene_id,
                "trigger_phrase": trigger_phrase,
                "actions": actions,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            data["scenes"][scene_id] = scene
            data.setdefault("trigger_index", {})[trigger_phrase] = scene_id

            await self._save_data(data)
            _LOGGER.info("Created voice scene: %s, trigger: %s", scene_id, trigger_phrase)
            return True, scene_id

    async def delete_scene(
        self, trigger_phrase: str | None = None, scene_id: str | None = None
    ) -> tuple[bool, str]:
        """Delete a scene by trigger phrase or scene ID.

        Returns:
            tuple: (success, message)
        """
        async with self._lock:
            data = await self._load_data()

            if trigger_phrase:
                actual_scene_id = data.get("trigger_index", {}).get(trigger_phrase)
                if not actual_scene_id:
                    return False, f"未找到触发词'{trigger_phrase}'对应的场景"
                scene_id = actual_scene_id

            if scene_id:
                scene = data.get("scenes", {}).get(scene_id)
                if not scene:
                    return False, f"未找到场景ID'{scene_id}'对应的场景"

                trigger = scene.get("trigger_phrase")
                if trigger and trigger in data.get("trigger_index", {}):
                    del data["trigger_index"][trigger]

                del data["scenes"][scene_id]
                await self._save_data(data)
                _LOGGER.info("Deleted voice scene: %s", scene_id)
                return True, f"已删除语音场景：{trigger or scene_id}"
            else:
                return False, "请提供trigger_phrase或scene_id"

    async def update_scene(
        self,
        scene_id: str,
        trigger_phrase: str | None = None,
        actions: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, str]:
        """Update a scene's trigger phrase and/or actions.

        Returns:
            tuple: (success, message)
        """
        async with self._lock:
            data = await self._load_data()
            scenes = data.get("scenes", {})
            if scene_id not in scenes:
                return False, f"未找到场景ID'{scene_id}'"

            scene = scenes[scene_id]
            old_trigger = scene.get("trigger_phrase", "")

            if trigger_phrase is not None and trigger_phrase != old_trigger:
                if trigger_phrase in data.get("trigger_index", {}):
                    return False, f"触发词'{trigger_phrase}'已存在"
                if old_trigger and old_trigger in data.get("trigger_index", {}):
                    del data["trigger_index"][old_trigger]
                data.setdefault("trigger_index", {})[trigger_phrase] = scene_id
                scene["trigger_phrase"] = trigger_phrase

            if actions is not None:
                scene["actions"] = actions

            await self._save_data(data)
            _LOGGER.info("Updated voice scene: %s", scene_id)
            return True, f"已更新语音场景：{scene.get('trigger_phrase', scene_id)}"


_store_instance: VoiceSceneStore | None = None


def get_voice_scene_store(hass: HomeAssistant) -> VoiceSceneStore:
    """Get the singleton store instance."""
    global _store_instance
    if _store_instance is None:
        _store_instance = VoiceSceneStore(hass)
    return _store_instance


def reset_voice_scene_globals():
    """Reset global singleton reference for clean reload."""
    global _store_instance
    _store_instance = None


class HassCreateVoiceSceneIntent(intent.IntentHandler):
    intent_type = "HassCreateVoiceScene"
    description = (
        "Creates a voice-triggered scene that stores trigger phrase and actions. "
        "Use ONLY when user says something like '当我说xxx的时候，帮我执行yyy', "
        "'你听到我说xxx就yyy', '如果我说xxx就开机'. "
        "DO NOT use for sensor/condition-based triggers (temperature, humidity, etc.) - "
        "use HassCreateAutomation for those. "
        "IMPORTANT WINDOW RULE: If user says '打开窗户'/'打开展厅的平推窗' etc., "
        "use TurnDeviceOn (NOT ControlWindow). The system will auto-convert it to ControlWindow(open). "
        "If user says '关闭窗户'/'关窗' etc., "
        "use TurnDeviceOff. The system will auto-convert it to ControlWindow(close). "
        "Parameters: trigger_phrase (a spoken phrase that will trigger the scene), "
        "actions (array of intent+params objects)."
    )

    @property
    def slot_schema(self) -> dict | None:
        return {
            vol.Required("trigger_phrase"): cv.string,
            vol.Required("actions"): vol.All(cv.ensure_list, [dict]),
        }

    async def _auto_supplement_windows(
        self, hass: HomeAssistant, actions: list[dict]
    ) -> list[dict]:
        """Auto-add ControlWindow for areas that have window buttons but LLM missed them.

        When a TurnDeviceOn/Off covers multiple domain types in an area (e.g.
        light+switch+climate) but forgets window(button), this method detects
        window buttons via HA entity registry and auto-supplements a ControlWindow action.
        """
        ent_registry = er.async_get(hass)
        area_reg = ar.async_get(hass)

        area_has_window = {}
        for entity_entry in ent_registry.entities.values():
            if entity_entry.domain != "button":
                continue
            name = (entity_entry.name or entity_entry.original_name or "").lower()
            if not any(kw in name for kw in WINDOW_KEYWORDS):
                continue
            if entity_entry.area_id:
                area_entry = area_reg.async_get_area(entity_entry.area_id)
                if area_entry and area_entry.name:
                    area_has_window[area_entry.name] = True

        if not area_has_window:
            return actions

        actions_with_many_domains = []
        for action in actions:
            intent_name = action.get("name") or action.get("intent", "")
            if intent_name not in ("TurnDeviceOn", "TurnDeviceOff"):
                continue
            params = action.get("parameters") or action.get("params", {})
            targets = params.get("target", [])
            if not isinstance(targets, list):
                targets = [targets]
            all_domains = set()
            for t in targets:
                if not isinstance(t, dict):
                    continue
                for d in t.get("devices", []):
                    if isinstance(d, dict):
                        all_domains.update(d.get("domains", []))
            non_button_count = len([d for d in all_domains if d != "button"])
            if non_button_count >= 2:
                actions_with_many_domains.append(action)

        if not actions_with_many_domains:
            return actions

        def _find_area_name(target: dict) -> str | None:
            if target.get("area"):
                return target["area"]
            if target.get("area_id"):
                entry = area_reg.async_get_area(target["area_id"])
                if entry:
                    return entry.name
            return None

        existing_window_areas = set()
        for action in actions:
            if action.get("name") not in ("ControlWindow", "WindowControl"):
                continue
            params = action.get("parameters") or action.get("params", {})
            for t in params.get("target", []):
                if isinstance(t, dict):
                    area = _find_area_name(t)
                    if area:
                        existing_window_areas.add(area)

        new_actions = list(actions)
        for action in actions_with_many_domains:
            intent_name = action.get("name") or action.get("intent", "")
            params = action.get("parameters") or action.get("params", {})
            for t in params.get("target", []):
                if not isinstance(t, dict):
                    continue
                area = _find_area_name(t)
                if not area or area not in area_has_window:
                    continue
                if area in existing_window_areas:
                    continue

                window_action = "open" if intent_name == "TurnDeviceOn" else "close"
                new_actions.append(
                    {
                        "name": "ControlWindow",
                        "parameters": {
                            "target": [
                                {"area": area, "devices": [{"domains": ["button"]}]}
                            ],
                            "action": window_action,
                        },
                    }
                )
                existing_window_areas.add(area)
                _LOGGER.info(
                    f"自动补充: 区域'{area}'缺少窗户控制, 添加ControlWindow action"
                )

        return new_actions

    async def async_handle(self, intent_obj: intent.Intent) -> JsonObjectType:
        slots = self.async_validate_slots(intent_obj.slots)
        _LOGGER.info("HassCreateVoiceScene slots=%s", slots)

        trigger_phrase = slots.get("trigger_phrase", {}).get("value", "")
        actions = slots.get("actions", {}).get("value", [])

        if not trigger_phrase or not trigger_phrase.strip():
            return {"success": False, "error": "触发词不能为空"}

        if not actions:
            return {"success": False, "error": "动作列表不能为空"}

        split_actions = split_actions_by_device(actions)
        _LOGGER.info("HassCreateVoiceScene split_actions=%s", split_actions)

        supplemented = await self._auto_supplement_windows(
            intent_obj.hass, split_actions
        )
        if len(supplemented) != len(split_actions):
            _LOGGER.info("自动补充后: %s个action", len(supplemented))
        split_actions = supplemented

        store = get_voice_scene_store(intent_obj.hass)
        success, result = await store.create_scene(trigger_phrase, split_actions)

        if success:
            return {
                "success": True,
                "scene_id": result,
                "message": f"已创建语音场景：{trigger_phrase}",
            }
        else:
            return {"success": False, "error": result}


class HassTriggerVoiceSceneIntent(intent.IntentHandler):
    intent_type = "HassTriggerVoiceScene"
    description = (
        "Triggers an existing voice scene by its trigger phrase. "
        "Use when user says the trigger phrase to execute a previously created scene. "
        "Parameters: trigger_phrase (string)."
    )
    service_timeout = 30

    @property
    def slot_schema(self) -> dict | None:
        return {
            vol.Required("trigger_phrase"): cv.string,
        }

    async def async_handle(self, intent_obj: intent.Intent) -> JsonObjectType:
        """Handle voice scene trigger - execute stored actions."""
        slots = self.async_validate_slots(intent_obj.slots)
        _LOGGER.info("HassTriggerVoiceScene slots=%s", slots)

        trigger_phrase = slots.get("trigger_phrase", {}).get("value", "")

        if not trigger_phrase:
            return {"success": False, "error": "触发词不能为空"}

        store = get_voice_scene_store(intent_obj.hass)
        scene = await store.get_scene_by_trigger(trigger_phrase)

        if not scene:
            return {
                "success": False,
                "error": f"未找到触发词'{trigger_phrase}'对应的场景",
            }

        executed_actions = []
        for action in scene.get("actions", []):
            intent_name = action.get("intent") or action.get("name")
            params = action.get("params") or action.get("parameters", {})
            _LOGGER.info(
                f"Executing scene action: intent={intent_name}, params={params}"
            )

            try:
                result = await self._execute_action_with_timeout(
                    intent_obj, intent_name, params
                )
                executed_actions.append(
                    {"intent": intent_name, "result": "success", "detail": result}
                )
            except asyncio.TimeoutError:
                _LOGGER.error("Action timeout: intent=%s", intent_name)
                executed_actions.append(
                    {"intent": intent_name, "result": "error", "error": "执行超时"}
                )
            except Exception as e:
                _LOGGER.error("Failed to execute action: %s", e)
                executed_actions.append(
                    {"intent": intent_name, "result": "error", "error": str(e)}
                )

        all_success = all(a.get("result") != "error" for a in executed_actions)
        return {
            "success": all_success,
            "scene_id": scene.get("scene_id"),
            "executed_actions": executed_actions,
            "message": f"已执行场景：{trigger_phrase}",
        }

    async def _execute_action_with_timeout(
        self, intent_obj: intent.Intent, intent_name: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute an intent action with timeout."""
        try:
            result = await asyncio.wait_for(
                self._execute_intent(intent_obj, intent_name, params),
                timeout=self.service_timeout,
            )
            return result
        except asyncio.TimeoutError:
            raise

    async def _execute_intent(
        self, intent_obj: intent.Intent, intent_name: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute an intent action by delegating to the registered IntentHandler.

        Calls the original IntentHandler via intent.async_handle, eliminating
        code duplication with intent_turn.py, intent_window_control.py, etc.
        """
        from homeassistant.helpers import intent as ha_intent

        from .const import DOMAIN

        if intent_name not in [
            "TurnDeviceOn",
            "TurnDeviceOff",
            "ControlWindow",
            "WindowControl",
            "AdjustDeviceAttribute",
            "SetDeviceMode",
        ]:
            return {"success": False, "error": f"不支持的intent类型: {intent_name}"}

        normalized_name = intent_name
        if intent_name == "WindowControl":
            normalized_name = "ControlWindow"

        try:
            ha_slots = {k: {"value": v} for k, v in params.items()}
            response = await ha_intent.async_handle(
                hass=intent_obj.hass,
                platform=DOMAIN,
                intent_type=normalized_name,
                slots=ha_slots,
                assistant=intent_obj.assistant,
                device_id=intent_obj.device_id,
            )
            return response
        except Exception as e:
            _LOGGER.error("Intent execution failed: %s: %s", intent_name, e)
            return {"success": False, "error": str(e)}


class HassDeleteVoiceSceneIntent(intent.IntentHandler):
    intent_type = "HassDeleteVoiceScene"
    description = (
        "Deletes an existing voice scene. "
        "Use when user wants to delete a created scene. "
        "Parameters: trigger_phrase (string) OR scene_id (string)."
    )

    @property
    def slot_schema(self) -> dict | None:
        return {
            vol.Optional("trigger_phrase"): cv.string,
            vol.Optional("scene_id"): cv.string,
        }

    async def async_handle(self, intent_obj: intent.Intent) -> JsonObjectType:
        slots = self.async_validate_slots(intent_obj.slots)
        _LOGGER.info("HassDeleteVoiceScene slots=%s", slots)

        trigger_phrase = slots.get("trigger_phrase", {}).get("value")
        scene_id = slots.get("scene_id", {}).get("value")

        if not trigger_phrase and not scene_id:
            return {"success": False, "error": "请提供trigger_phrase或scene_id"}

        store = get_voice_scene_store(intent_obj.hass)
        success, message = await store.delete_scene(
            trigger_phrase=trigger_phrase, scene_id=scene_id
        )

        return {
            "success": success,
            "message": message if success else None,
            "error": message if not success else None,
        }


class HassListVoiceScenesIntent(intent.IntentHandler):
    intent_type = "HassListVoiceScenes"
    description = (
        "Lists all stored voice scenes. "
        "Use when user wants to see all created scenes. "
        "No parameters required."
    )

    @property
    def slot_schema(self) -> dict | None:
        return None

    async def async_handle(self, intent_obj: intent.Intent) -> JsonObjectType:
        _LOGGER.info("HassListVoiceScenes called")

        store = get_voice_scene_store(intent_obj.hass)
        scenes = await store.get_all_scenes()

        return {"success": True, "scenes": scenes}
