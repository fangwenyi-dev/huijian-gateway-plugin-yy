import logging
from dataclasses import asdict, dataclass, field
from typing import Callable

import voluptuous as vol
from homeassistant.components import climate, humidifier
from homeassistant.const import ATTR_ENTITY_ID, ATTR_MODE, Platform
from homeassistant.core import State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from homeassistant.util.json import JsonObjectType

from .intent_helper import (HaTargetItem, match_intent_entities,
                            normalize_targets_device_names, target_parameter_type)

_LOGGER = logging.getLogger(__name__)


@dataclass
class OperationContext:
    state: State
    entity: er.RegistryEntry
    mode: str


@dataclass
class OperationTarget:
    service: str = ""
    service_data: dict = field(default_factory=dict)
    avail_modes: list[str] = field(default_factory=list)


handle_map: dict[
    str, dict[str, Callable[[OperationContext, OperationTarget], None]]
] = {}


def register_handler(domain: str, attrbute: str):
    def decorator(func):
        attrbute_handlers = handle_map.setdefault(domain, {})
        attrbute_handlers[attrbute] = func

        def wrapper(ctx: OperationContext, target: OperationTarget):
            func(ctx, target)

        return wrapper

    return decorator


@register_handler("climate", "mode")
def set_climate_mode(ctx: OperationContext, target: OperationTarget):
    avail_modes = []
    if ctx.entity.capabilities:
        # Current device
        avail_modes = ctx.entity.capabilities.get("hvac_modes")
    if not avail_modes:
        # Global
        avail_modes = climate.const.HVAC_MODES

    # Remove 'off' from mode list.
    avail_modes = avail_modes[:]
    if "off" in avail_modes:
        avail_modes.remove("off")

    if len(avail_modes) == 0:
        raise intent.IntentHandleError("Unsupported set mode")

    if ctx.mode not in avail_modes:
        # v1.0.30 场景模式双通道：hvac_mode 不中再看 preset 档（sleep/eco/
        # boost/silent… 在真实空调实体上是 set_preset_mode，060401 五拆永远
        # 够不着的半张能力表）。两表皆不中才报真错。
        presets = (ctx.entity.capabilities or {}).get("preset_modes") or \
            ctx.state.attributes.get("preset_modes") or []
        if ctx.mode in presets:
            target.service = "set_preset_mode"      # 字面名：SERVICE_* 常量坑规避
            target.service_data["preset_mode"] = ctx.mode
            target.avail_modes = list(avail_modes) + [
                p for p in presets if p not in avail_modes
            ]
            return
        raise intent.IntentHandleError(
            f"Invalid mode, not in [{','.join(list(avail_modes) + list(presets))}]"
        )

    target.service = climate.const.SERVICE_SET_HVAC_MODE
    target.service_data[climate.const.ATTR_HVAC_MODE] = ctx.mode
    target.avail_modes = avail_modes


@register_handler("fan", "mode")
def set_fan_mode(ctx: OperationContext, target: OperationTarget):
    """v1.0.30：风扇「睡眠/静音/正常」等= preset 档（不是风速）。加载项
    mode 词表已归一为英文规范名，此处仅按实体能力校验。"""
    presets = (ctx.entity.capabilities or {}).get("preset_modes") or \
        ctx.state.attributes.get("preset_modes") or []
    if not presets:
        raise intent.IntentHandleError("Unsupported set mode")
    if ctx.mode not in presets:
        raise intent.IntentHandleError(
            f"Invalid mode, not in [{','.join(presets)}]"
        )
    target.service = "set_preset_mode"
    target.service_data["preset_mode"] = ctx.mode
    target.avail_modes = list(presets)


@register_handler("humidifier", "mode")
def set_humidifier_mode(ctx: OperationContext, target: OperationTarget):
    mode = ctx.mode
    state = ctx.state
    avail_modes = state.attributes.get(humidifier.const.ATTR_AVAILABLE_MODES, [])
    if len(avail_modes) == 0:
        raise intent.IntentHandleError("Unsupported set mode")

    if mode not in avail_modes:
        raise intent.IntentHandleError(
            f"Invalid mode, not in [{','.join(avail_modes)}]"
        )

    target.service = humidifier.const.SERVICE_SET_MODE
    target.service_data[ATTR_MODE] = ctx.mode
    target.avail_modes = avail_modes


class SetDeviceModeIntent(intent.IntentHandler):
    intent_type = "SetDeviceMode"
    description = (
        "Set the operation mode of a device. "
        "Supported devices: climate(heat/cool/auto/dry/fan_only; preset: "
        "sleep/eco/comfort/silent/boost/normal), fan(preset modes), humidifier. "
        "Examples: '把空调设为制热模式' -> mode=heat, target=空调. "
        "'空调设为睡眠模式' -> mode=sleep (走 preset 通道). "
        "'把空调设为26度制冷' -> use AdjustDeviceAttribute with attribute=temperature instead."
    )
    platforms = {Platform.CLIMATE, Platform.HUMIDIFIER, Platform.FAN}

    @property
    def slot_schema(self) -> dict | None:
        """Return a slot schema."""
        return {
            vol.Required("mode"): intent.non_empty_string,
            vol.Required("target"): target_parameter_type(),
        }

    async def async_handle(self, intent_obj: intent.Intent) -> JsonObjectType:  # type: ignore
        """Handle the intent."""
        hass = intent_obj.hass
        slots = self.async_validate_slots(intent_obj.slots)

        mode: str = slots.get("mode", {}).get("value")
        targets: list[HaTargetItem] = slots.get("target", {}).get("value", [])
        # 归一化中文数字（如"五号"->"5号"），提高实体匹配成功率
        targets = normalize_targets_device_names(targets)

        error_msg, candidate_entities = await match_intent_entities(intent_obj, targets)
        if error_msg:
            return error_msg
        if not candidate_entities:
            # 永不抛（同 intent_turn：assert 炸 500 会让话术层只剩空括号）
            return {"success": False, "error": "No available devices found"}

        results = []
        for item in candidate_entities:
            domain = item.state.domain
            state = item.state
            _LOGGER.info("SetDeviceMode state: %s", item.state.as_dict_json)

            error: str | None = None
            target = OperationTarget()
            try:
                handle = handle_map.get(domain, {}).get("mode")
                if not handle:
                    raise intent.IntentHandleError("unsupported")

                # Find the paramters to adjust.
                handle(
                    OperationContext(state=state, entity=item.entity, mode=mode), target
                )
                target.service_data[ATTR_ENTITY_ID] = state.entity_id

                # Execute.
                _LOGGER.info("AdjustDeviceAttribute call target: %s", asdict(target))
                await hass.services.async_call(
                    domain,
                    target.service,
                    service_data=target.service_data,
                    blocking=True,
                    context=intent_obj.context,
                )
            except (intent.IntentHandleError, ServiceValidationError) as e:
                error = str(e)

            success = not error
            result = {
                "success": success,
                "name": item.name,
                "area": item.area_name,
                "supported_modes": target.avail_modes,
            }
            if error:
                result["error"] = error

            results.append(result)

        return {
            "results": results,
        }
