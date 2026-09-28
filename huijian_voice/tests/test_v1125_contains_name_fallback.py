# -*- coding: utf-8 -*-
"""v1.1.25 集成侧钉：词级名称匹配 miss 后的「包含」回捞（英文双语桥/泛称句根修）。

现场实锤（办公 .91，2026-09-28 18:07）：'turn on the office light' → 双语桥落到
（办公室/灯/light），实体叫「射灯」——HA 的 target 名称匹配是词级 ⇒ 严格匹配空 ⇒
「没找到符合条件的设备」。中文同形常被 klar 接走，英文没有兜底。
本钉直调被测函数（sys.modules stub 真 import，同 test_runtime_data_guards 范式）。
"""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

CC = Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai"


class _AnyMod(types.ModuleType):
    """宽容 stub：未知属性 → 大写名造类（注解用），小写名给空函数（HA 面很宽）。"""

    def __getattr__(self, k):
        sub = sys.modules.get(f"{self.__name__}.{k}")   # 先认已注册的子模块 stub
        if sub is not None:
            return sub
        if k[:1].isupper():
            return type(k, (), {})
        return lambda *a, **kw: None


def _load_helper():
    if "cut1125_ok" in sys.modules:
        return sys.modules["cut1125.intent_helper"]
    mods = {
        "homeassistant": {},
        "homeassistant.core": {"HomeAssistant": object, "State": object},
        "homeassistant.helpers": {},
        "homeassistant.helpers.area_registry": {},
        "homeassistant.helpers.config_validation": {},
        "homeassistant.helpers.device_registry": {},
        "homeassistant.helpers.entity_registry": {},
        "homeassistant.helpers.intent": {},
        "homeassistant.util": {},
        "homeassistant.util.json": {"JsonObjectType": dict},
    }
    for name, attrs in mods.items():
        m = _AnyMod(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules.setdefault(name, m)

    intent_mod = sys.modules["homeassistant.helpers.intent"]

    class MatchTargetsConstraints:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _MatchResult:
        def __init__(self, states):
            self.states = states
            self.is_match = bool(states)

    STRICT = {"states": []}          # 每钉自行设定

    def async_match_targets(hass, constraints):
        return _MatchResult(list(STRICT["states"]))

    intent_mod.MatchTargetsConstraints = MatchTargetsConstraints
    intent_mod.async_match_targets = async_match_targets
    intent_mod.IntentHandleError = type("IntentHandleError", (Exception,), {})

    pkg = types.ModuleType("cut1125")
    pkg.__path__ = [str(CC)]
    sys.modules["cut1125"] = pkg
    spec = importlib.util.spec_from_file_location(
        "cut1125.intent_helper", CC / "intent_helper.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                      # 先执行成功再入缓存（半初始化不缓存）
    sys.modules["cut1125.intent_helper"] = mod
    sys.modules["cut1125_ok"] = mod
    mod._STRICT = STRICT
    return mod


class _States:
    def __init__(self, all_states):
        self._all = all_states

    def async_all(self, domain=None):
        if domain is None:
            return list(self._all)
        doms = {domain} if isinstance(domain, str) else set(domain)
        return [s for s in self._all if s.entity_id.split(".", 1)[0] in doms]


def _ent(eid, fname, area_id=None, device_id=None):
    return SimpleNamespace(entity_id=eid, name=fname, attributes={"friendly_name": fname},
                           _area_id=area_id, _device_id=device_id)


class _Reg:
    def __init__(self, by_key):
        self._m = by_key

    def async_get(self, key):
        return self._m.get(key)


class _Hass:
    def __init__(self, states, areas, ents, devs):
        self.states = _States(states)
        self._areas = areas
        self._ents = ents
        self._devs = devs
        self.data = {}

    # 注册表 stub 入口（被测代码用 ar/er/dr.async_get(hass) 取注册表，再 .async_get(key)）
    def async_get_area_by_name(self, name):
        return self._areas.get(name)

    def async_get(self, key):
        if key in self._ents:
            return self._ents[key]
        return self._devs.get(key)


def _make(mod, strict_states):
    """组装：实体 射灯 在设备层归属办公室；另一盏 灯 在客厅（跨区诱饵）。"""
    mod._STRICT["states"] = strict_states
    s_office = _ent("light.she_deng_bg", "射灯")
    s_ke_ting = _ent("light.deng_kt", "客厅灯")
    ents = {
        "light.she_deng_bg": SimpleNamespace(area_id=None, device_id="dev_bg"),
        "light.deng_kt": SimpleNamespace(area_id="ke_ting", device_id=None),
    }
    devs = {"dev_bg": SimpleNamespace(area_id="ban_gong_shi")}
    areas = {"办公室": SimpleNamespace(id="ban_gong_shi"),
             "客厅": SimpleNamespace(id="ke_ting")}
    hass = _Hass([s_office, s_ke_ting], areas, ents, devs)

    class _ArMod:
        @staticmethod
        def async_get(_h):
            return hass

    mod.ar = _ArMod
    mod.er = _ArMod
    mod.dr = _ArMod
    return hass


def _targets(name="灯", area="办公室", domains=("light",)):
    return [{"area": area, "devices": [{"name": name, "domains": list(domains)}]}]


def test_contains_fallback_rescues_generic_word():
    """严格匹配空 + 名称包含 + 同区域 ⇒ 回捞到「射灯」（英文桥/泛称句根修）。"""
    mod = _load_helper()
    hass = _make(mod, strict_states=[])
    found, _doms = asyncio.run(mod._match_with_constraints(hass, _targets(), None))
    assert len(found) == 1, found
    assert [s.entity_id for s in found[0].states] == ["light.she_deng_bg"]
    assert found[0].unset_area_constraint is False


def test_strict_match_wins_over_fallback():
    """反向钉：严格匹配非空时**不走**回捞（回捞只会补，不会顶替）。"""
    mod = _load_helper()
    strict = [_ent("light.other", "别的灯")]
    hass = _make(mod, strict_states=strict)
    found, _ = asyncio.run(mod._match_with_constraints(hass, _targets(), None))
    assert [s.entity_id for s in found[0].states] == ["light.other"]


def test_fallback_never_crosses_area():
    """反向钉（判别式）：回捞只在**本区域**池子里找——说「客厅的灯」只许捞到
    「客厅灯」，绝不许把办公室的「射灯」一起捞进来（拆掉区域约束本钉必红）。"""
    mod = _load_helper()
    hass = _make(mod, strict_states=[])
    found, _ = asyncio.run(mod._match_with_constraints(
        hass, _targets(name="灯", area="客厅"), None))
    assert len(found) == 1, found
    assert [s.entity_id for s in found[0].states] == ["light.deng_kt"]


def test_fallback_unknown_area_stays_miss():
    """反向钉：区域名注册表不认识（英文 'office' 腿）⇒ 不回捞。"""
    mod = _load_helper()
    hass = _make(mod, strict_states=[])
    found, _ = asyncio.run(mod._match_with_constraints(
        hass, _targets(name="light", area="office"), None))
    assert found == []
