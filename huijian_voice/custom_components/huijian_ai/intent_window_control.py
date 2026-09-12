import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import cover
from homeassistant.components.button.const import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button.const import \
    SERVICE_PRESS as SERVICE_PRESS_BUTTON
from homeassistant.const import (ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES,
                                 SERVICE_SET_COVER_POSITION)
from homeassistant.helpers import intent
from homeassistant.util.json import JsonObjectType

from .intent_helper import HaTargetItem, target_parameter_type
from .intent_window_const import (WINDOW_ACTION_MAPPING, WINDOW_NAME_MAPPING,
                                  extract_window_name, find_action_in_text,
                                  find_all_window_buttons_by_action,
                                  find_covers_for_buttons,
                                  find_window_buttons,
                                  normalize_chinese_numbers)

_LOGGER = logging.getLogger(__name__)

ACTION_CHINESE = {"open": "开启", "close": "关闭", "pause": "暂停", "a": "内倒"}


async def _apply_window_position(
    intent_obj: intent.Intent,
    window_name: str | None,
    area_name: str | None,
    device_name: str | None,
    pos_raw,
) -> dict:
    """百分比开度定位：解析窗类目标 → 同设备 cover → set_cover_position。

    寻径与开/关/暂停/内倒同源（find_window_buttons 按钮体系），能按按钮
    找到的窗就有百分比入口；机型是否真支持由 cover 的 SET_POSITION 能力位
    逐台裁决（网关 v1.7.21 起 5002 等无百分比硬件机型不声明该位并在服务层
    拒绝）——这里绝不把「不支持」含糊成「成功」（用户 2026-09-15 能力边界
    铁律：失败必须确定且可复述）。
    """
    hass = intent_obj.hass
    try:
        position = int(float(str(pos_raw).strip().rstrip("%％")))
    except (TypeError, ValueError):
        return {"success": False, "error": f"无法识别的开度数值：{pos_raw!r}"}
    if not 0 <= position <= 100:
        return {"success": False, "error": f"开度超出范围(0-100)：{position}"}

    # 1) 定位窗类按钮（区域泛称/无窗型 → 区域内全部窗，与「开所有窗」同口径）
    button_ids: list[str] = []
    if window_name:
        buttons = find_window_buttons(
            hass, window_name, area_name, original_name=device_name
        )
        if not buttons and area_name:
            buttons = find_window_buttons(
                hass, window_name, None, original_name=device_name
            )
        button_ids = list(buttons.values())
        generic_all = (not device_name) or str(device_name).strip().lower() in (
            "窗户", "窗",
        )
        if area_name and (generic_all or not button_ids):
            button_ids = find_all_window_buttons_by_action(hass, area_name, "open")
    elif area_name:
        button_ids = find_all_window_buttons_by_action(hass, area_name, "open")
    else:
        return {"success": False, "error": "No target specified"}
    if not button_ids:
        return {
            "success": False,
            "error": f"Could not find window buttons for "
                     f"{device_name or window_name or area_name}",
        }

    # 2) 按钮 → 同设备 cover 实体
    covers = find_covers_for_buttons(hass, button_ids)
    if not covers:
        return {
            "success": False,
            "error": "no available cover entity — 该窗户没有带位置实体的开窗器，"
                     "不支持百分比定位",
        }

    # 3) 逐台下发，成败分收
    ok_names: list[str] = []
    bad_msgs: list[str] = []
    for dev_name, cover_entity_id in covers:
        state = hass.states.get(cover_entity_id)
        if state is None:
            bad_msgs.append(f"{dev_name}实体不可用")
            continue
        try:
            feats = int(state.attributes.get(ATTR_SUPPORTED_FEATURES, 0) or 0)
        except (TypeError, ValueError):
            feats = 0
        if not feats & cover.CoverEntityFeature.SET_POSITION:
            bad_msgs.append(f"{dev_name}机型不支持百分比定位")
            continue
        try:
            await hass.services.async_call(
                cover.DOMAIN,
                SERVICE_SET_COVER_POSITION,
                {ATTR_ENTITY_ID: cover_entity_id, cover.ATTR_POSITION: position},
                context=intent_obj.context,
                blocking=True,
            )
            ok_names.append(dev_name)
            _LOGGER.info("Set position %s%% on %s (%s)", position, cover_entity_id,
                         dev_name)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("set_cover_position %s failed: %s", cover_entity_id, err)
            bad_msgs.append(f"{dev_name}：{err}")

    # v1.0.49（现场「我说的是展厅」）：带设备名的话术过去只报设备名，区域被吞——
    # 用户在播报里听不出执行的是哪个房间，误以为 NLU 没识别区域。话术统一
    # 「区域+设备」，全屋形态保持「区域的所有窗户」。
    _dev_label = device_name or window_name
    if _dev_label:
        label = f"{area_name}的{_dev_label}" if area_name else _dev_label
    else:
        label = f"{area_name}的所有窗户" if area_name else "所有窗户"

    if ok_names and not bad_msgs:
        return {
            "success": True,
            "message": f"已将{label}开到{position}%",
            "covers": [eid for _, eid in covers],
        }
    if ok_names:
        return {
            "success": True,
            "message": (
                f"已将{len(ok_names)}扇窗开到{position}%，"
                f"但{len(bad_msgs)}扇未成功：{'；'.join(bad_msgs[:3])}"
            ),
            "covers": [eid for _, eid in covers],
        }
    return {
        "success": False,
        "error": f"开到{position}%未成功：{'；'.join(bad_msgs[:3])}",
    }


async def _press_multi_buttons(
    hass, context, action: str, button_entity_ids: list[str]
) -> tuple[list[str], list[str]]:
    """Press multiple window buttons.

    Used by all-window paths (generic name like "所有窗户" or bare type name like "窗户").

    v1.0.52：返回 (成功ids, 失败话术列表)——旧版只回成功列表、失败仅记日志，
    调用方把"5 扇败 2 扇"播报成"已所有窗户关闭"，违反本文件 _apply_window_position
    立下的铁律（失败必须确定且可复述），且"关所有窗"有安全语义。
    """
    results = []
    failed_msgs: list[str] = []
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
            state = hass.states.get(button_entity_id)
            label = (
                (state.attributes.get("friendly_name") if state else None)
                or button_entity_id.split(".", 1)[-1]
            )
            failed_msgs.append(f"{label}：{err}")
    return results, failed_msgs


def _all_window_result(
    area_name: str | None,
    action: str,
    results: list[str],
    failed_msgs: list[str],
) -> dict:
    """全窗按压的统一裁决：全成/部分成/全败三种话术，部分失败绝不折叠成全成功。"""
    action_cn = ACTION_CHINESE.get(action, action)
    area_label = (area_name + chr(30340)) if area_name else ""
    if not results:
        return {
            "success": False,
            "error": f"未能{action_cn}任何窗户",
            "buttons": [],
        }
    if failed_msgs:
        return {
            "success": True,
            "message": (
                f"已将{area_label}{len(results)}扇窗{action_cn}，"
                f"但{len(failed_msgs)}扇未成功：{'；'.join(failed_msgs[:3])}"
            ),
            "buttons": results,
        }
    return {
        "success": True,
        "message": f"已{area_label}所有窗户{action_cn}",
        "buttons": results,
    }


class ControlWindowIntent(intent.IntentHandler):
    intent_type = "ControlWindow"
    description = (
        "Unified entry for ALL window commands (open/close/pause/tilt) and "
        "percentage positioning. Action keywords: 开/开启=open, 关/关闭=close, "
        "暂停/停止/停=pause, 内倒/内岛=A(tilt). Optional slot position(0-100): "
        "'把推拉窗打开到50%' -> position=50 (定位开度，仅支持百分比的开窗器机型生效). "
        "Examples: '内岛展厅窗户' -> action=A, area=展厅, name=窗户. "
        "'打开平推窗' -> action=open, name=平推窗. "
        "Valid window names: 平推窗,平开窗,推拉窗,内开窗,外开窗,天窗,飘窗,推拉门,内开内倒窗,单内倒窗,外装平开窗,智能窗,窗户."
    )

    @property
    def slot_schema(self) -> dict | None:
        """Return a slot schema."""
        return {
            vol.Optional("action"): str,
            # 百分比开度（网关 v1.7.20+ 开窗器）：与 action 二选一，
            # 携带时走 cover.set_cover_position 定位，不再按按钮。
            vol.Optional("position"): vol.Any(int, float, str),
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

        # 百分比开度定位（v1.7.20+ 网关开窗器）：与二值开/关同一套窗类解析，
        # 命中后按「按钮→同设备 cover」下发 set_cover_position。
        # 必须在 window_name 缺失的全窗兜底分支之前裁决——位置语义不需要
        # action（"开到50%"剥掉动词尾巴后无独立动作词）。
        pos_raw = (slots.get("position") or {}).get("value")
        if pos_raw is not None:
            return await _apply_window_position(
                intent_obj, window_name, area_name, device_name, pos_raw
            )

        if not window_name:
            if area_name and action:
                all_buttons = find_all_window_buttons_by_action(
                    intent_obj.hass, area_name, action
                )
                if all_buttons:
                    results, failed_msgs = await _press_multi_buttons(
                        intent_obj.hass, intent_obj.context, action, all_buttons
                    )
                    return _all_window_result(area_name, action, results, failed_msgs)
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
                    results, failed_msgs = await _press_multi_buttons(
                        intent_obj.hass, intent_obj.context, action, all_buttons
                    )
                    return _all_window_result(area_name, action, results, failed_msgs)
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
