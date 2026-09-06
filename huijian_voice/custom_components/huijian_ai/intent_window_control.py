import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.button.const import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button.const import \
    SERVICE_PRESS as SERVICE_PRESS_BUTTON
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.helpers import intent
from homeassistant.util.json import JsonObjectType

from .intent_helper import HaTargetItem, target_parameter_type
from .intent_window_const import (WINDOW_ACTION_MAPPING, WINDOW_NAME_MAPPING,
                                  extract_window_name, find_action_in_text,
                                  find_all_window_buttons_by_action,
                                  find_window_buttons, normalize_chinese_numbers)

_LOGGER = logging.getLogger(__name__)

ACTION_CHINESE = {"open": "开启", "close": "关闭", "pause": "暂停", "a": "内倒"}


async def _press_multi_buttons(
    hass, context, action: str, button_entity_ids: list[str]
) -> dict:
    """Press multiple window buttons and return results dict.

    Used by all-window paths (generic name like "所有窗户" or bare type name like "窗户").
    """
    results = []
    for button_entity_id in button_entity_ids:
        try:
            await hass.services.async_call(
                BUTTON_DOMAIN,
                SERVICE_PRESS_BUTTON,
                {ATTR_ENTITY_ID: button_entity_id},
                context=context,
                blocking=True,
            )
            results.append(button_entity_id)
            _LOGGER.info("Pressed all-window button: %s", button_entity_id)
            await asyncio.sleep(0.5)
        except Exception as err:
            _LOGGER.error("Failed to press %s: %s", button_entity_id, err)
    return results


class ControlWindowIntent(intent.IntentHandler):
    intent_type = "ControlWindow"
    description = (
        "Unified entry for ALL window commands (open/close/pause/tilt). "
        "Action keywords: 开/开启=open, 关/关闭=close, 暂停/停止/停=pause, 内倒/内岛=A(tilt). "
        "Examples: '内岛展厅窗户' -> action=A, area=展厅, name=窗户. "
        "'打开平推窗' -> action=open, name=平推窗. "
        "Valid window names: 平推窗,平开窗,推拉窗,内开窗,外开窗,天窗,飘窗,推拉门,内开内倒窗,单内倒窗,外装平开窗,智能窗,窗户."
    )

    @property
    def slot_schema(self) -> dict | None:
        """Return a slot schema."""
        return {
            vol.Optional("action"): str,
            vol.Required("target"): target_parameter_type(),
        }

    async def async_handle(self, intent_obj: intent.Intent) -> JsonObjectType:
        """Handle window control intent."""
        slots = self.async_validate_slots(intent_obj.slots)
        _LOGGER.info("ControlWindow slots=%s", slots)

        action_slot = slots.get("action", {}).get("value")
        targets: list[HaTargetItem] = slots.get("target", {}).get("value", [])
        if not targets:
            return {"success": False, "error": "No target specified"}

        target = targets[0]
        area_name = target.get("area")
        devices = target.get("devices", [])

        device_name = None
        domains = []
        if devices:
            domains = devices[0].get("domains", [])
            device_name = devices[0].get("name")

        _LOGGER.info(
            "Input: device_name='%s', domains=%s, area_name='%s', action_slot='%s'",
            device_name, domains, area_name, action_slot,
        )

        # 归一化中文数字（如"五号"->"5号"），提高与HA实体名称的匹配成功率
        if device_name:
            device_name = normalize_chinese_numbers(device_name)
            _LOGGER.info("After number normalization: device_name='%s'", device_name)

        window_name = extract_window_name(device_name or "")
        action = find_action_in_text(device_name or "")

        if not action and action_slot:
            action = find_action_in_text(action_slot)

        _LOGGER.info("Extracted: window_name='%s', action='%s'", window_name, action)

        if not window_name:
            if area_name and action:
                all_buttons = find_all_window_buttons_by_action(
                    intent_obj.hass, area_name, action
                )
                if all_buttons:
                    results = await _press_multi_buttons(
                        intent_obj.hass, intent_obj.context, action, all_buttons
                    )
                    if not results:
                        return {
                            "success": False,
                            "error": f"未能{ACTION_CHINESE.get(action, action)}任何窗户",
                            "buttons": results,
                        }
                    return {
                        "success": True,
                        "message": f"已{ACTION_CHINESE.get(action, action)}所有窗户",
                        "buttons": results,
                    }
            return {
                "success": False,
                "error": f"Could not extract window name from '{device_name}'",
            }

        # Detect when LLM sends just the bare general window name (e.g., name="窗户" or "窗")
        # This means "all windows of this type in the area"
        # Specific type names like "平推窗" should NOT trigger all-windows mode
        is_all_windows = (
            window_name
            and device_name
            and device_name.strip().lower() == window_name.lower()
            and window_name.lower() in ("窗户", "窗")
        )

        if is_all_windows:
            if area_name and action:
                all_buttons = find_all_window_buttons_by_action(
                    intent_obj.hass, area_name, action
                )
                if all_buttons:
                    results = await _press_multi_buttons(
                        intent_obj.hass, intent_obj.context, action, all_buttons
                    )
                    if not results:
                        return {
                            "success": False,
                            "error": f"未能{ACTION_CHINESE.get(action, action)}任何窗户",
                            "buttons": results,
                        }
                    return {
                        "success": True,
                        "message": f"已{ACTION_CHINESE.get(action, action)}所有窗户",
                        "buttons": results,
                    }
            return {
                "success": False,
                "error": f"Could not find any {action} buttons in {area_name}",
            }

        if not action:
            return {
                "success": False,
                "error": f"Could not determine action from '{device_name}' or '{action_slot}'",
            }

        buttons = find_window_buttons(
            intent_obj.hass, window_name, area_name, original_name=device_name
        )

        _LOGGER.info("Found buttons (with area filter): %s", buttons)

        if action not in buttons and area_name:
            buttons = find_window_buttons(
                intent_obj.hass, window_name, None, original_name=device_name
            )
            _LOGGER.info("Found buttons (without area filter): %s", buttons)

        if action not in buttons:
            return {
                "success": False,
                "error": f"Could not find {action} button for {window_name} in {area_name or 'any area'}",
            }

        button_entity_id = buttons[action]

        try:
            await intent_obj.hass.services.async_call(
                BUTTON_DOMAIN,
                SERVICE_PRESS_BUTTON,
                {ATTR_ENTITY_ID: button_entity_id},
                context=intent_obj.context,
                blocking=True,
            )
            _LOGGER.info("Successfully pressed: %s", button_entity_id)
            return {
                "success": True,
                "message": "已经帮你执行了",
            }
        except Exception as err:
            _LOGGER.error("Failed to press %s: %s", button_entity_id, err)
            return {"success": False, "error": str(err)}
