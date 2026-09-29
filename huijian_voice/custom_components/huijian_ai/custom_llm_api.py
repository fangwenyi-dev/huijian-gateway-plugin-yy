"""慧尖控制面 LLM Tool API（HA custom LLM API 面）。

注：本模块顶部的 `from __future__ import annotations` 是**可导入性前提**——
`_should_include_entity` 的返回注解 `tuple[bool, er.RegistryEntry | None]` 里
`er` 只在函数内局部 import（懒载纪律），类体求值期无此名 → 无本行则整包
import 即 NameError（v1.1.27 复查实证；本文件长期无人 import 才未暴露）。
"""
from __future__ import annotations

import logging
import time

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


# v1.0.62 P0-3（同构闸）：本包 intent_turn.py 是 D7 语义（注释实锤
# `on = lock / off = unlock`），而工具面暴露 HassTurnDeviceOff——不拦就是与
# 加载项 v1.0.61 级联 C2 完全同构的「HA 端配了 LLM 即免确认解锁」后门。
# 逻辑对齐 core/nlu/targets.py args_target_lock（集成包不 import 加载项 core，
# 属受控重复，行为由 tests/test_v1062_nlu_batch.py 双端同构钉桩）。
# 无条件拒：集成侧读不到加载项 settings（confirm_risky 开关在加载项进程），
# 拒答话术把用户引导回主语音通道——那边按用户配置要么先问要么直办。
_RISKY_LOCK_OFF_INTENTS = frozenset({"TurnDeviceOff", "HassTurnOff", "HassToggle"})


# H2/M6（2026-09-23 深审批2）：原判据只认字面 "lock" in domains——而执行面
# _expand_domains 用 DOMAIN_ALIASES 把 door 扩进锁域，`HassTurnDeviceOff{
# name:大门,domains:["door"]}` 旁路免确认解锁。撤防族同并入：intent_turn D7
# 映射 alarm×Off→alarm_disarm，一句话直撤家庭安防。
# 本表与加载项 core/nlu/targets.py `_RISKY_DOMAIN_ALIASES` 双端同构（**受控重复**
# ——闸须自包含，测试用 _claw_fn 空命名空间抽函数跑，函数体内 import 会退化）；
# 执行面 intent_helper.DOMAIN_ALIASES 是唯一真源，三表 door/alarm 形由
# tests/test_v1064_risk_batch.py 一致钉。行为矩阵双端同钉。
_RISKY_DOMAIN_ALIASES: dict = {
    "door": ("lock", "cover", "button"),
    "doors": ("lock", "cover", "button"),
}
_LOCK_NAME_WORDS = ("锁", "大门", "房门", "卷帘门")
_ALARM_NAME_WORDS = ("安防", "报警", "布防", "撤防")
_ALARM_DOMAINS = ("alarm_control_panel",)


def _risky_domain_closure(domains) -> set:
    """domains（可混 entity_id 形）→ 小写裸域 + 别名闭包。永不抛。"""
    out: set = set()
    try:
        stack = [str(d).lower().split(".", 1)[0].strip() for d in (domains or [])]
        while stack:
            d = stack.pop()
            if not d or d in out:
                continue
            out.add(d)
            for a in _RISKY_DOMAIN_ALIASES.get(d, ()):
                if a not in out:
                    stack.append(a)
    except Exception:  # noqa: BLE001
        return out
    return out


def _args_targets_lock(arguments):
    """target 面是否命中风险实体（锁域闭包 / 锁·安防中文词 / alarm 域）。"""
    if not isinstance(arguments, dict):
        return False
    try:
        for t in arguments.get("target") or []:
            if not isinstance(t, dict):
                continue
            for d in t.get("devices") or []:
                if not isinstance(d, dict):
                    continue
                name = str(d.get("name") or "")
                if any(w in name for w in _LOCK_NAME_WORDS + _ALARM_NAME_WORDS):
                    return True
                closed = _risky_domain_closure(d.get("domains") or [])
                if "lock" in closed or closed & set(_ALARM_DOMAINS):
                    return True
        name_all = str(arguments.get("name") or "")
        if any(w in name_all for w in _LOCK_NAME_WORDS + _ALARM_NAME_WORDS):
            return True
        eids = arguments.get("entity_id")
        if isinstance(eids, str):
            eids = [eids]
        if isinstance(eids, (list, tuple)):
            return any(isinstance(e, str)
                       and e.split(".", 1)[0] in ("lock",) + _ALARM_DOMAINS
                       for e in eids)
    except (TypeError, AttributeError):
        return False
    return False


# v1.1.27（批7 P0-2）：场景两链的动作级风险闸。原闸只扫调用参数**顶层**
# target，而场景/自动化的动作藏在 actions[].params.target 里——
# `HassCreateVoiceScene{actions:[{intent:TurnDeviceOff,params:{target:[{devices:
# [{name:大门,domains:[lock]}]}]}}]}` 一句话造出「免确认关大门（=解锁）」，
# 之后 `HassTriggerVoiceScene`（args 只有 trigger_phrase）回放全程免确认。
# 触发链的 args 里没有动作面 → 回查场景库取存量动作再判（同一把闸）。
_RISKY_SCENE_INTENTS = frozenset({"HassCreateVoiceScene", "HassTriggerVoiceScene"})


def scene_actions_hit_risk(actions) -> bool:
    """actions（任意嵌套）里是否含「免确认解锁/撤防」动作。永不抛。

    判据与顶层闸同源（_args_targets_lock 域闭包 + 中文词表 + alarm 域/entity_id），
    只是改为**递归**遍历动作节点——场景动作形如 {intent|name, params|parameters}，
    风险实体不在参数顶层 target 就是在 params.target，故按「意图节点」取参后复用。
    """
    stack = [actions]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            name = cur.get("intent") or cur.get("name")
            if isinstance(name, str) and name in _RISKY_LOCK_OFF_INTENTS:
                params = cur.get("params")
                if not isinstance(params, dict):
                    params = cur.get("parameters")
                if isinstance(params, dict) and _args_targets_lock(params):
                    return True
            stack.extend(
                v for v in cur.values() if isinstance(v, (dict, list, tuple))
            )
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)
    return False


def _response_text(response) -> str:
    """response（dict 折叠形 / HA IntentResponse 对象）→ LLM 可读文本（≤200 字）。

    v1.1.27（批7 P0-1）：旧实现只 `str(response)[:200]` 且 success 恒 True——
    执行面失败（handler 折成 {"success": False} 或 IntentResponse.success=False）
    会被播成「办好了」。此处只做文本抽取，成败判定由调用点读 success。
    """
    text = ""
    if isinstance(response, dict):
        for key in ("message", "result", "error", "speech"):
            value = response.get(key)
            if value:
                text = str(value)
                break
        if not text:
            text = str(response)
    else:
        text = str(getattr(response, "error", None) or "")
        if not text:
            # r2（金标复测）：真 IntentResponse 用 speech["plain"]["speech"] 承载
            # 话术；旧式 str(response) 会回 "<…object at 0x…>" 给 LLM。
            sp = getattr(response, "speech", None)
            if isinstance(sp, dict) and isinstance(sp.get("plain"), dict):
                text = str(sp["plain"].get("speech") or "")
        if not text:
            text = str(response)
    return text[:200] + "..." if len(text) > 200 else text


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
            except Exception:  # noqa: BLE001
                # v1.0.40 修复（A6）：原写法是"冗余异常的元组 + 完全静默"（KeyError 与
                # Exception 并列，后者已覆盖前者）。暴露判定一旦出问题（注册表结构变化
                # 等），这里会按"可见"放行且**不留任何痕迹** ⇒ LLM 可能看到用户刻意隐藏
                # 的实体，而现场无从归因。保持 fail-open 语义，但必须留痕。
                # v1.0.41 审查 S11：debug 在 HA 默认 INFO 档位下等于仍不留痕——判定
                # 持续坏时现场不可见。升 warning 并 60s 限流一次（语义不变，运维可见）。
                now = time.monotonic()
                if now - getattr(self, "_expose_warn_ts", 0.0) >= 60.0:
                    self._expose_warn_ts = now
                    _LOGGER.warning("实体暴露判定失败，按可见处理（60s 限流留痕）: %s",
                                    state.entity_id, exc_info=True)
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
                "NOTE: 开窗/关窗 auto-forwarded to ControlWindow. "
                "铁律：lock 域设备禁用本工具——关闭门锁=解锁，属风险操作会被拒绝；"
                "用户提及时直接口播引导「请说解锁门锁，会先确认」。",
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
                "e.g. '内岛展厅窗户'(A,展厅), '打开平推窗'(open,平推窗). "
                "开窗器速度/力度设定（网关 v1.4.3+）用 speed/strength 槽(0-100)且不带 action："
                "'办公室平开窗速度设为30%' -> speed=30.",
                self._handle_control_window,
                vol.Schema({
                    vol.Optional("action"): cv.string,
                    vol.Optional("speed"): vol.Coerce(int),
                    vol.Optional("strength"): vol.Coerce(int),
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

    async def _scene_chain_hits_risk(
        self, hass: HomeAssistant, intent_type: str, arguments: dict
    ) -> bool:
        """场景创建/触发两链的风险扫描（v1.1.27；见 _RISKY_SCENE_INTENTS 注释）。

        触发链 args 只有 trigger_phrase，动作面在场景库里 → 回查存量动作再判。
        库读取异常按放行处理（fail-open，与 _args_targets_lock 同口径）并留痕。
        """
        if intent_type not in _RISKY_SCENE_INTENTS:
            return False
        args = arguments if isinstance(arguments, dict) else {}
        if scene_actions_hit_risk(args.get("actions")):
            return True
        if intent_type != "HassTriggerVoiceScene":
            return False
        phrase = str(args.get("trigger_phrase") or "").strip()
        if not phrase:
            return False
        try:
            from .intent_voice_scene import get_voice_scene_store

            scene = await get_voice_scene_store(hass).get_scene_by_trigger(phrase)
        except Exception as err:  # noqa: BLE001 —— 闸不可因库故障变拒绝
            _LOGGER.warning(
                "[custom_llm_api] 场景「%s」风险校验失败，放行交执行面：%s",
                phrase, err)
            return False
        return scene_actions_hit_risk((scene or {}).get("actions"))

    async def _call_intent(self, hass: HomeAssistant, intent_type: str, arguments: dict, llm_context: llm.LLMContext) -> dict:
        arguments = await self._enrich_target_domains(hass, arguments)
        # v1.0.62 P0-3 同构闸（见文件头 _args_targets_lock 注释）：置于 enrich
        # 之后——LLM 常不写 domains、只给中文设备名，enrich 会按 HA 真实状态回填
        # domains，锁设备在此刻才现形，故必须在回填后判。命中即拒并回话术给 LLM，
        # 引导用户回主语音通道走确认。与加载项 agent._tool C2 同构语义。
        # v1.1.27：闸面扩到场景两链（actions 递归 + 触发链回查场景库）。
        if (intent_type in _RISKY_LOCK_OFF_INTENTS
                and _args_targets_lock(arguments)) or \
                await self._scene_chain_hits_risk(hass, intent_type, arguments):
            _LOGGER.warning("[custom_llm_api] 拒绝风险目标 %s（解锁/撤防引导至确认流）",
                            intent_type)
            return {"success": False,
                    "error": "解锁/撤防是风险操作，我不能替您跳过确认——"
                             "请直接说「解锁大门」「撤销布防」这类指令，会先问您一声再执行"}
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

        # v1.1.27（批7 P0-1）：读 response 的 success/error 如实折算——旧实现
        # 一律 {"success": True, "result": str(response)}，执行面失败（handler
        # 折叠 dict / IntentResponse.success=False）被 LLM 播成「办好了」。
        result_text = _response_text(response)
        if isinstance(response, dict):
            if "success" in response:
                ok = response.get("success") is not False
                err = str(response.get("error") or "")
            elif isinstance(response.get("results"), list):
                # r2（金标复测）：SetDeviceMode 族只回 {"results":[…]}（无 success
                # 键）——旧式 `get("success") is not False` 恒真 ⇒ 逐台全失败仍被
                # 播「办好了」。按逐台行折算：任一台成功＝该步可用，全败＝失败。
                rows = [r for r in response["results"] if isinstance(r, dict)]
                # v1.1.27 复审（本轮）：旧式 `if rows else True` 把「零成功证据」
                # 翻成成功——空 results（或元素全非 dict）时 any() 本已为 False，
                # 却被判「办好了」。SetDeviceMode 族无可用设备时已提前回
                # {"success": False, "No available devices found"}，能走到这里的
                # 空列表属异常形态 ⇒ 按「没有任何一台成功」如实判失败。
                ok = any(r.get("success") for r in rows)
                err = next((str(r.get("error")) for r in rows
                            if not r.get("success") and r.get("error")), "") \
                    or ("" if ok else "执行面未返回任何结果")
            else:
                # 未知 dict 形态（无 success 键、无 results 键）：不一律翻成失败
                # ——会误伤合法但非常规的成功形态；但**带 error 证据必须采信**，
                # 旧式恒 True 会让「有失败原因」的响应也被播成「办好了」。
                err = str(response.get("error") or "")
                ok = not err
        else:
            succ = getattr(response, "success", None)
            if succ is None:
                # 真 HA IntentResponse **没有** .success 属性（失败以
                # response_type=ERROR 表达）——旧判据 getattr(...,True) 恒真 ⇒
                # 真机每一次失败都被报成功。
                rtype = getattr(response, "response_type", None)
                err = str(getattr(response, "error_code", "") or "")
                if rtype is None:
                    # 既非 dict、无 .success、也无 response_type ⇒ 不是标准
                    # IntentResponse，此时 `"" != "ERROR"` 恒真（无证据即成功）。
                    # 一律翻成失败会误伤未知但合法的对象，故保留 fail-open 并
                    # **留痕**（异常形态可见，便于后续按实证收口）；有 error_code
                    # 证据则照旧采信。
                    _LOGGER.warning(
                        "[custom_llm_api] 无法判定成败的响应形态 %s，按成功处理"
                        "（请核对 handler 返回契约）",
                        type(response).__name__)
                    ok = not err
                else:
                    ok = str(getattr(rtype, "name", "") or "").upper() != "ERROR"
            else:
                ok = succ is not False
                err = str(getattr(response, "error", "") or "")
        if not ok:
            return {"success": False, "error": err or result_text}
        return {"success": True, "result": result_text}