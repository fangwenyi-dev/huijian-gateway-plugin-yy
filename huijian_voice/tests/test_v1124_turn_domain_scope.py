# -*- coding: utf-8 -*-
"""v1.1.24 集成侧根修钉：宽域目标混进"不可 turn_on 的域"必须**跳过**而非整条失败。

现场实锤（HA 系统日志，2026-09-28 17:32:36；办公 .91「我有点热」场景 1/3 动作失败）：
    Scene action folded failure: TurnDeviceOn:
    Service turn_on does not support entity select.xiaomi_mc9_aeaf_fan_level
根因：加载项动态词表给出"域并集"（select/number/button…），集成对每个候选逐个下发，
旧式在 `has_service` 判负处 raise ⇒ **一个** select 把整条动作判失败。
本钉直调被测模块（sys.modules stub 真 import，同 test_runtime_data_guards 范式）。
"""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

CC = Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai"


def _load_intent_turn():
    if "cut1124" in sys.modules:
        return sys.modules["cut1124.intent_turn"]
    mods = {
        "homeassistant": {},
        "homeassistant.const": {
            "ATTR_ENTITY_ID": "entity_id", "SERVICE_TURN_ON": "turn_on",
            "SERVICE_TURN_OFF": "turn_off", "SERVICE_LOCK": "lock",
            "SERVICE_UNLOCK": "unlock", "SERVICE_CLOSE_COVER": "close_cover",
            "SERVICE_OPEN_COVER": "open_cover", "SERVICE_CLOSE_VALVE": "close_valve",
            "SERVICE_OPEN_VALVE": "open_valve"},
        "homeassistant.components": {},
        "homeassistant.components.button": {},
        "homeassistant.components.button.const": {"DOMAIN": "button", "SERVICE_PRESS": "press"},
        "homeassistant.components.cover": {},
        "homeassistant.components.cover.const": {"DOMAIN": "cover"},
        "homeassistant.components.input_button": {"DOMAIN": "input_button"},
        "homeassistant.components.lock": {},
        "homeassistant.components.lock.const": {"DOMAIN": "lock"},
        "homeassistant.components.valve": {},
        "homeassistant.components.valve.const": {"DOMAIN": "valve"},
        "homeassistant.core": {"State": object},
        "homeassistant.helpers": {},
        "homeassistant.helpers.config_validation": {},
        "homeassistant.helpers.entity_registry": {},
        "homeassistant.helpers.intent": {},
        "homeassistant.util": {},
        "homeassistant.util.json": {"JsonObjectType": dict},
    }
    for name, attrs in mods.items():
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules.setdefault(name, m)

    class IntentHandler:
        pass

    class IntentHandleError(Exception):
        pass

    intent_mod = sys.modules["homeassistant.helpers.intent"]
    intent_mod.IntentHandler = IntentHandler
    intent_mod.IntentHandleError = IntentHandleError
    intent_mod.Intent = object

    pkg = types.ModuleType("cut1124")
    pkg.__path__ = [str(CC)]
    sys.modules["cut1124"] = pkg

    helper = types.ModuleType("cut1124.intent_helper")
    helper.EntityInfo = object
    helper.HaDeviceItem = dict
    helper.HaTargetItem = dict

    async def match_intent_entities(*a, **k):      # 本钉用不到真匹配
        return None, []

    helper.match_intent_entities = match_intent_entities
    helper.target_parameter_type = lambda: dict
    helper.validate_slots_safely = lambda *a, **k: ({}, None)
    sys.modules["cut1124.intent_helper"] = helper

    wc = types.ModuleType("cut1124.intent_window_const")
    wc.normalize_chinese_numbers = lambda s: s
    wc.WINDOW_NAME_MAPPING = {}
    sys.modules["cut1124.intent_window_const"] = wc

    spec = importlib.util.spec_from_file_location(
        "cut1124.intent_turn", CC / "intent_turn.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cut1124.intent_turn"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Svc:
    def __init__(self, has):
        self._has = has
        self.calls = []

    def has_service(self, domain, service):
        return self._has

    async def async_call(self, domain, service, data, **kw):
        self.calls.append((domain, service, dict(data)))


class _Hass:
    def __init__(self, svc):
        self.services = svc
        self._loop_tasks = []

    def async_create_task_internal(self, coro, name=None):
        t = asyncio.get_running_loop().create_task(coro)
        if name:
            t.set_name(name)
        self._loop_tasks.append(t)
        return t


def test_unsupported_domain_skips_instead_of_failing_action():
    """select 域不支持 turn_on ⇒ handle_match_target 返回 False（跳过），不得 raise。
    （旧形态 raise IntentHandleError ⇒ 集成折叠成功败 ⇒ 场景 1/3 失败。）"""
    mod = _load_intent_turn()
    hass = _Hass(_Svc(has=False))
    intent_obj = SimpleNamespace(hass=hass, context=None)
    state = SimpleNamespace(domain="select",
                            entity_id="select.xiaomi_mc9_aeaf_fan_level",
                            attributes={})
    h = mod.TurnDeviceOnIntent()
    got = asyncio.run(h.handle_match_target(intent_obj, state, "turn_on"))
    assert got is False, "不支持域必须走跳过路径（False），旧式 raise 会把整条动作判失败"
    assert hass.services.calls == [], "跳过路径不得下发任何服务调用"


def test_supported_domain_still_dispatches():
    """反向钉：同样还是 select 域，但服务存在时照旧下发（修的是"不支持才跳过"，
    不是把所有 select 一刀切掉）。"""
    mod = _load_intent_turn()
    svc = _Svc(has=True)
    hass = _Hass(svc)
    intent_obj = SimpleNamespace(hass=hass, context=None)
    state = SimpleNamespace(domain="select", entity_id="select.x", attributes={})
    h = mod.TurnDeviceOnIntent()
    got = asyncio.run(h.handle_match_target(intent_obj, state, "turn_on"))
    assert got is None
    assert svc.calls == [("select", "turn_on", {"entity_id": "select.x"})], svc.calls


def test_all_unsupported_message_not_pause_specific():
    """全部候选都被跳过时才失败，且话术不再挂"暂停"字样（暂停语义话术另有钉）。"""
    src = (CC / "intent_turn.py").read_text(encoding="utf-8")
    assert "这些设备不支持该操作：" in src
    assert "暂不支持暂停该设备：" in src        # 暂停话术原样保留（v1042 钉）
    idx_pause = src.index("暂不支持暂停该设备：")
    idx_generic = src.index("这些设备不支持该操作：")
    assert idx_pause < idx_generic, "暂停分支应在通用话术之前（先判暂停语义）"
