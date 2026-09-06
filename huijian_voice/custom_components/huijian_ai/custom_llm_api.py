import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import intent, llm

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_DOMAIN_ALIASES = "lamp\u2192light, ac\u2192climate, curtain\u2192cover, window\u2192cover/button"

_PROMPT_OPERATION_GUIDE = (
    "操作指南:\n"
    "1. 简单开关用HassTurnDeviceOn/Off，不用先查状态\n"
    "2. 调亮度/温度/风速等用HassAdjustDeviceAttribute\n"
    "3. 空调设模式用HassSetDeviceMode\n"
    "4. 窗户开/关/暂停/内倒用ControlWindow\n"
    "5. 查设备状态(开关/温度等)用HuijianGetLiveContext\n"
    "6. target格式: [{devices: [{domains: ['light'], name: '筒灯'}], area: '办公室'}]\n"
    "7. 实体名用中文精确匹配，区域名也用中文\n"
    f"8. 领域别名: {_DOMAIN_ALIASES}\n"
    "9. delta格式: +10(增) -10(减) 50(设值) 50%(百分比) max/min(极限)\n"
    "10.mode: heat/cool/auto/dry/fan_only"
)


def _build_slots(params: dict) -> dict:
    slots = {}
    for key, value in params.items():
        slots[key] = {"value": value}
    return slots


def _device_schema():
    return {
        vol.Optional("domains"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("name"): cv.string,
    }


def _target_schema():
    return vol.All(
        cv.ensure_list,
        [vol.Schema({
            vol.Optional("area"): cv.string,
            vol.Optional("devices"): vol.All(cv.ensure_list, [vol.Schema(_device_schema())]),
        })],
    )


class _Tool(llm.Tool):
    """LLM Tool that delegates async_call to a handler."""

    def __init__(self, name: str, description: str, handler, parameters: vol.Schema | None = None):
        self.name = name
        self.description = description or ""
        self.parameters = parameters or vol.Schema({})
        self._handler = handler

    async def async_call(self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext) -> dict:
        return await self._handler(hass, tool_input, llm_context)


class HuijianControlAPI(llm.API):
    """Custom LLM API exposing all huijian-ai tools."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass=hass, id="huijian_control", name="\u6167\u7b80AI\u63a7\u5236")

    async def async_get_api_instance(self, llm_context: llm.LLMContext) -> llm.APIInstance:
        return llm.APIInstance(
            api=self,
            api_prompt=self._build_entity_prompt(llm_context),
            llm_context=llm_context,
            tools=self.tools,
            custom_serializer=None,
        )

    @callback
    def _should_include_entity(
        self, state, entity_reg, llm_context: llm.LLMContext | None,
    ) -> tuple[bool, er.RegistryEntry | None]:
        """Check if entity should be included. Returns (include, entry)."""
        from .intent_live_context import async_should_expose

        assistant = llm_context.assistant if llm_context and hasattr(llm_context, "assistant") else None
        if assistant:
            try:
                if not async_should_expose(self.hass, assistant, state.entity_id):
                    return False, None
            except (KeyError, Exception):
                pass
        entry = entity_reg.async_get(state.entity_id)
        if not entry or entry.hidden_by or entry.disabled_by:
            return False, None
        return True, entry

    @callback
    def _get_entity_area_name(self, entry, area_reg) -> str | None:
        """Get area display name for an entity registry entry."""
        if entry and entry.area_id:
            area = area_reg.async_get_area(entry.area_id)
            if area:
                return area.name
        return None

    @callback
    def _format_entity_line(self, state, entry) -> str:
        """Format a single entity line for the prompt."""
        name = state.name or state.entity_id
        aliases = entry.aliases or []
        alias_str = f"/{'/'.join(str(a) for a in aliases)}" if aliases else ""
        return f"{name}({state.domain}{alias_str})"

    @callback
    def _build_entity_prompt(self, llm_context: llm.LLMContext | None = None) -> str:
        """Build system prompt: guide + device name reference."""
        parts = [_PROMPT_OPERATION_GUIDE]

        from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

        area_reg = ar.async_get(self.hass)
        entity_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)

        # 1. 获取说话人所在区域
        speaker_area_name = None
        if llm_context and llm_context.device_id:
            device = dev_reg.async_get(llm_context.device_id)
            if device and device.area_id:
                area_entry = area_reg.async_get_area(device.area_id)
                if area_entry:
                    speaker_area_name = area_entry.name

        # 2. 先全量收集所有实体（分三桶）
        _MAX_COLLECT = 200
        speaker_entities: list[str] = []
        area_entities: dict[str, list[str]] = {}
        no_area_entities: list[str] = []
        _total_collected = 0

        for state in self.hass.states.async_all():
            if _total_collected >= _MAX_COLLECT:
                break
            included, entry = self._should_include_entity(state, entity_reg, llm_context)
            if not included:
                continue
            area_name = self._get_entity_area_name(entry, area_reg)
            line = self._format_entity_line(state, entry)
            if speaker_area_name and area_name == speaker_area_name:
                speaker_entities.append(line)
            elif area_name:
                area_entities.setdefault(area_name, []).append(line)
            else:
                no_area_entities.append(line)
            _total_collected += 1

        # 3. 按优先级截断（说话人区域优先保留）
        _MAX_ENTITIES = 40
        remaining = _MAX_ENTITIES
        display_speaker = speaker_entities[:remaining]
        remaining -= len(display_speaker)
        display_areas = {}
        for area in sorted(area_entities):
            if remaining <= 0:
                break
            take = min(len(area_entities[area]), remaining)
            display_areas[area] = area_entities[area][:take]
            remaining -= take
        display_no_area = no_area_entities[:max(0, remaining)]

        # 4. 拼接设备列表：说话人区域排最前
        if display_speaker or display_areas or display_no_area:
            parts.append("可用设备(按区域):")
            if display_speaker:
                parts.append(f"  [{speaker_area_name}]: {', '.join(display_speaker)}")
            for area in sorted(display_areas):
                parts.append(f"  [{area}]: {', '.join(display_areas[area])}")
            if display_no_area:
                parts.append(f"  [其他]: {', '.join(display_no_area)}")

        return "\n".join(parts)

    @property
    def tools(self) -> list[_Tool]:
        return [
            _Tool(
                "HassTurnDeviceOn",
                "Turn on/open device. e.g. '打开卧室筒灯'(light), '按场景按钮'(button), '打开窗帘'(cover). "
                "NOTE: 开窗/关窗 auto-forwarded to ControlWindow.",
                self._handle_turn_on,
                vol.Schema({vol.Required("target"): _target_schema()}),
            ),
            _Tool(
                "HassTurnDeviceOff",
                "Turn off/close device. e.g. '关闭卧室筒灯'(light), '关闭窗帘'(cover). "
                "NOTE: 开窗/关窗 auto-forwarded to ControlWindow.",
                self._handle_turn_off,
                vol.Schema({vol.Required("target"): _target_schema()}),
            ),
            _Tool(
                "HassSetDeviceMode",
                "Set device mode. climate(heat/cool/auto/dry/fan_only), humidifier. "
                "e.g. '把空调设为制热模式'(mode=heat). "
                "温度值用HassAdjustDeviceAttribute，非此工具.",
                self._handle_set_mode,
                vol.Schema({
                    vol.Required("target"): _target_schema(),
                    vol.Required("mode"): cv.string,
                }),
            ),
            _Tool(
                "HassAdjustDeviceAttribute",
                "Set/adjust device attribute. "
                "attributes: brightness(light), color(light), temperature(light/climate), "
                "position(cover), fan_speed(fan/climate), humidity(humidifier). "
                "delta: +10(增), -5(减), 50(设值), 50%(百分比), max/min(极限). "
                "e.g. '把卧室灯调亮20%'(brightness,+20), '空调调到26度'(temperature,26).",
                self._handle_adjust_attribute,
                vol.Schema({
                    vol.Required("target"): _target_schema(),
                    vol.Required("attribute"): vol.In(["brightness", "color", "temperature", "position", "fan_speed", "humidity"]),
                    vol.Required("delta"): cv.string,
                }),
            ),
            _Tool(
                "ControlWindow",
                "Unified entry for ALL window commands (open/close/pause/tilt). "
                "action: 开/开启=open, 关/关闭=close, 暂停/停止/停=pause, 内倒/内岛=A(tilt). "
                "e.g. '内岛展厅窗户'(A,展厅), '打开平推窗'(open,平推窗).",
                self._handle_control_window,
                vol.Schema({
                    vol.Required("action"): cv.string,
                    vol.Required("target"): _target_schema(),
                }),
            ),
            _Tool(
                "HuijianGetLiveContext",
                "Query real-time state/condition of devices/sensors/areas. "
                "Use for: '灯是开的吗', '温度多少', or as first step of conditional actions. "
                "No parameters required.",
                self._call_intent_factory("huijianGetLiveContext"),
                vol.Schema({}),
            ),
            _Tool(
                "HassCreateVoiceScene",
                "Create voice-triggered scene. "
                "Use when: '当我说xxx的时候帮我执行yyy', '你听到我说xxx就yyy'. "
                "NOT for sensor/condition triggers(use HassCreateAutomation). "
                "params: trigger_phrase, actions(intent+params array).",
                self._call_intent_factory("HassCreateVoiceScene"),
                vol.Schema({
                    vol.Required("trigger_phrase"): cv.string,
                    vol.Required("actions"): vol.All(cv.ensure_list, [dict]),
                }),
            ),
            _Tool(
                "HassTriggerVoiceScene",
                "Execute voice scene by trigger phrase. "
                "params: trigger_phrase.",
                self._call_intent_factory("HassTriggerVoiceScene"),
                vol.Schema({vol.Required("trigger_phrase"): cv.string}),
            ),
            _Tool(
                "HassDeleteVoiceScene",
                "Delete voice scene by trigger_phrase or scene_id. "
                "e.g. '删除场景', '删除xxx场景'.",
                self._call_intent_factory("HassDeleteVoiceScene"),
                vol.Schema({
                    vol.Optional("trigger_phrase"): cv.string,
                    vol.Optional("scene_id"): cv.string,
                }),
            ),
            _Tool(
                "HassListVoiceScenes",
                "List all stored voice scenes. No parameters.",
                self._call_intent_factory("HassListVoiceScenes"),
                vol.Schema({}),
            ),
            _Tool(
                "HassCreateAutomation",
                "Create sensor-triggered automation. "
                "Use when: '当温度大于30度就开窗', '如果传感器检测到xxx就yyy'. "
                "NOT for voice-triggered(use HassCreateVoiceScene). "
                "params: trigger(entity_id, above/below), actions(intent+params array). "
                "e.g. trigger={entity_id:'sensor.office_temperature', above:29}",
                self._call_intent_factory("HassCreateAutomation"),
                vol.Schema({
                    vol.Required("trigger"): {
                        vol.Required("entity_id"): cv.string,
                        vol.Optional("above"): vol.Coerce(float),
                        vol.Optional("below"): vol.Coerce(float),
                    },
                    vol.Required("actions"): vol.All(cv.ensure_list, [dict]),
                }),
            ),
            _Tool(
                "HassDeleteAutomation",
                "Delete automation by automation_id. e.g. '删除自动化'. params: automation_id.",
                self._call_intent_factory("HassDeleteAutomation"),
                vol.Schema({vol.Required("automation_id"): cv.string}),
            ),
            _Tool(
                "HassListAutomations",
                "List all stored automations. e.g. '查看自动化', '有哪些自动化'. No parameters.",
                self._call_intent_factory("HassListAutomations"),
                vol.Schema({}),
            ),
            _Tool(
                "HassUpdateAutomation",
                "Update automation trigger or actions by automation_id. "
                "params: automation_id(required), trigger(entity_id,above/below), actions. "
                "e.g. trigger={entity_id:'sensor.office_temperature', above:30}",
                self._call_intent_factory("HassUpdateAutomation"),
                vol.Schema({
                    vol.Required("automation_id"): cv.string,
                    vol.Optional("trigger"): {
                        vol.Required("entity_id"): cv.string,
                        vol.Optional("above"): vol.Coerce(float),
                        vol.Optional("below"): vol.Coerce(float),
                    },
                    vol.Optional("actions"): vol.All(cv.ensure_list, [dict]),
                }),
            ),
        ]

    async def _handle_turn_on(self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext) -> dict:
        return await self._call_intent(hass, "TurnDeviceOn", tool_input.tool_args, llm_context)

    async def _handle_turn_off(self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext) -> dict:
        return await self._call_intent(hass, "TurnDeviceOff", tool_input.tool_args, llm_context)

    async def _handle_set_mode(self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext) -> dict:
        return await self._call_intent(hass, "SetDeviceMode", tool_input.tool_args, llm_context)

    async def _handle_adjust_attribute(self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext) -> dict:
        return await self._call_intent(hass, "AdjustDeviceAttribute", tool_input.tool_args, llm_context)

    async def _handle_control_window(self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext) -> dict:
        return await self._call_intent(hass, "ControlWindow", tool_input.tool_args, llm_context)

    @staticmethod
    async def _enrich_target_domains(hass: HomeAssistant, arguments: dict) -> dict:
        target = arguments.get("target", [])
        if not target or not isinstance(target, list):
            return arguments

        arguments = dict(arguments)
        arguments["target"] = list(target)

        for ti, t in enumerate(target):
            devices = t.get("devices", [])
            if not devices:
                continue
            enriched = False
            for di, device in enumerate(devices):
                if "domains" not in device or not device["domains"]:
                    name = device.get("name", "")
                    if not name:
                        continue
                    matching_domains = set()
                    name_lower = name.lower().strip()
                    for state in hass.states.async_all():
                        if name_lower == state.name.lower() or (len(name_lower) >= 2 and state.name.lower().endswith(name_lower)):
                            matching_domains.add(state.domain)
                    if matching_domains:
                        if not enriched:
                            arguments["target"][ti] = dict(t)
                            arguments["target"][ti]["devices"] = list(devices)
                            enriched = True
                        arguments["target"][ti]["devices"][di] = dict(device)
                        arguments["target"][ti]["devices"][di]["domains"] = list(matching_domains)
                        _LOGGER.info(
                            "Auto-injected domains=%s for device '%s' from HA states",
                            matching_domains, name,
                        )
        return arguments

    def _call_intent_factory(self, intent_type: str):
        async def handler(hass, tool_input, llm_context):
            return await self._call_intent(hass, intent_type, tool_input.tool_args, llm_context)
        return handler

    async def _call_intent(self, hass: HomeAssistant, intent_type: str, arguments: dict, llm_context: llm.LLMContext) -> dict:
        arguments = await self._enrich_target_domains(hass, arguments)
        slots = _build_slots(arguments)
        if llm_context and llm_context.device_id:
            slots["_speaker_id"] = {"value": llm_context.device_id}
        assistant = llm_context.assistant if llm_context and hasattr(llm_context, "assistant") else None

        try:
            response = await intent.async_handle(
                hass=hass,
                platform=DOMAIN,
                intent_type=intent_type,
                slots=slots,
                assistant=assistant,
                device_id=llm_context.device_id if llm_context else None,
            )
        except (intent.IntentHandleError, HomeAssistantError, vol.Invalid) as e:
            _LOGGER.error("Intent %s failed: %s", intent_type, e)
            return {"success": False, "error": str(e)}
        except Exception as e:
            _LOGGER.error("Intent %s unexpected error: %s", intent_type, e)
            return {"success": False, "error": f"Unexpected error: {e}"}

        result_text = str(response)
        if len(result_text) > 200:
            result_text = result_text[:200] + "..."
        return {"success": True, "result": result_text}