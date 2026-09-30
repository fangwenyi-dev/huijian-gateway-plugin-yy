# -*- coding: utf-8 -*-
"""v1.1.27 集成动作面根因批（8 项）行为钉——先钉后修，每项执行真实行为。

覆盖（编号=修批 A 报告项号）：
① intent_lock：LockIntentBase 继承 intent.IntentHandler，而 `_run_then_background`
   只活在 HA core 的 DynamicServiceIntentHandler / 本仓 intent_turn —— 每次
   解锁/上锁必 AttributeError，**锁服务从未下发**，返回体里还是 Python 内部串。
② intent_lock：逐台 {"success": True} 硬编码 ⇒ 服务失败也报成功（加内 executor
   的解锁话术正是按 states[].success 取名字播报，谎报直达用户耳朵）。
③ intent_turn：裸键「窗」把「纱窗/百叶窗」判进窗控道，而 extract_window_name
   对帘族返 None ⇒ 被升级成"本区所有窗按钮"（「关纱窗」把整区窗全关）。
④ intent_turn：窗侧失败只在"窗成灯败"分支消费 ⇒「关掉窗和灯」窗败灯成仍回 success。
⑤ intent_turn：裸「窗/窗子」用旧等值判定（extract 已归一成「窗户」）⇒ 全剔=failed。
⑥ intent_adjust_attribute：adjust_number_value 只读 delta.value ⇒「调高10」被当
   绝对 10、「调到最大」→ set_value(0)。
⑦ intent_device_shared：非 Turn* 族（ControlWindow/PauseDevice）action 被三元定死
   "close"、params 重建为 {target,action} ⇒ 内倒/暂停/position 全丢（入库即坏）。
⑧ intent_helper：AreaInfo.name 取 aliases+… 的 [0]（set 无序）⇒ 严格 != 比较随机
   丢候选；空 area 被当"有区域"剔除；名称精确过滤只取第一个 device.name 做全局
   过滤 ⇒ 多目标其余静默丢却回 success。

钉桩纪律（同 test_v1125 / test_window_speed_behavior）：本机与 CI 都不装
homeassistant，先幂等注入最小替身模块，再 **真 import** 集成源码执行真实函数
（不复制逻辑、不做文本钉）；intent_adjust_attribute 的 import 面太重（climate/
fan/light/number 全族），按 test_v1043 的 ast 摘定义手法取出被测函数本体执行。
"""
import ast
import asyncio
import importlib.util
import json
import logging
import re
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parents[1]
CC = HERE / "custom_components" / "huijian_ai"
PKG = "hv1127ia"


# ── homeassistant 最小替身（幂等；每次用例前回装，不赌同会话安装顺序）──────
class _AnyMod(types.ModuleType):
    """宽容 stub：未知属性 → 大写名造类（注解用），小写名给空函数（HA 面很宽）。"""

    def __getattr__(self, k):
        sub = sys.modules.get(f"{self.__name__}.{k}")   # 先认已注册的子模块 stub
        if sub is not None:
            return sub
        if k[:1].isupper():
            return type(k, (), {})
        return lambda *a, **kw: None


def _stub(name: str) -> types.ModuleType:
    m = sys.modules.get(name)
    if m is None or not isinstance(m, _AnyMod):
        m = _AnyMod(name)
        sys.modules[name] = m
    return m


class IntentHandleError(Exception):
    """HA 正规失败通道（真类：homeassistant.helpers.intent.IntentHandleError）。"""


class MatchTargetsConstraints:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _MatchResult:
    def __init__(self, states):
        self.states = list(states)
        self.is_match = bool(states)


class IntentHandler:
    """真 handler 只承担槽位校验；行为钉直供 slots，校验面按恒等返回。"""

    slot_schema = None

    def async_validate_slots(self, slots):
        return slots


_WORD_SEP = re.compile(r"[\s_/、，,]+")


def _tokens(text):
    return [t for t in _WORD_SEP.split((text or "").strip().lower()) if t]


def _area_hits(area_entry, want: str) -> bool:
    """区域名命中（名或别名）——替身按真 HA 区域口径。"""
    want = (want or "").strip().lower()
    if not want:
        return False
    names = {str(getattr(area_entry, "name", "") or "")}
    names |= {str(a) for a in (getattr(area_entry, "aliases", None) or ())}
    return any(want == n.strip().lower() for n in names)


def _async_match_targets(hass, constraints):
    """HA 词级匹配的最小忠实替身：请求名须逐词出现在实体名分词里；点名区域时
    按注册表（名/别名）过滤到本区；assistant 曝光位不设限。"""
    name = (getattr(constraints, "name", None) or "").strip().lower()
    want_area = (getattr(constraints, "area_name", None) or "").strip()
    domains = getattr(constraints, "domains", None)
    out = []
    for state in hass.states.async_all(domains):
        if name:
            have = _tokens(getattr(state, "name", "") or "")
            if not all(t in have for t in _tokens(name)):
                continue
        if want_area:
            entry = hass.entity_registry.async_get(state.entity_id)
            area_id = getattr(entry, "area_id", None) if entry else None
            if not area_id and entry is not None and entry.device_id:
                dev = hass.device_registry.async_get(entry.device_id)
                area_id = getattr(dev, "area_id", None) if dev else None
            area_entry = hass.area_registry.async_get_area(area_id) if area_id else None
            if area_entry is None or not _area_hits(area_entry, want_area):
                continue
        out.append(state)
    return _MatchResult(out)


_HA_CONST = {
    "ATTR_ENTITY_ID": "entity_id",
    "ATTR_SUPPORTED_FEATURES": "supported_features",
    "ATTR_TEMPERATURE": "temperature",
    "SERVICE_SET_COVER_POSITION": "set_cover_position",
    "SERVICE_TURN_ON": "turn_on",
    "SERVICE_TURN_OFF": "turn_off",
    "SERVICE_LOCK": "lock",
    "SERVICE_UNLOCK": "unlock",
    "SERVICE_OPEN_COVER": "open_cover",
    "SERVICE_CLOSE_COVER": "close_cover",
    "SERVICE_OPEN_VALVE": "open_valve",
    "SERVICE_CLOSE_VALVE": "close_valve",
}

_STUB_NAMES = (
    "homeassistant", "homeassistant.core", "homeassistant.const",
    "homeassistant.exceptions", "homeassistant.helpers",
    "homeassistant.helpers.area_registry", "homeassistant.helpers.config_validation",
    "homeassistant.helpers.device_registry", "homeassistant.helpers.entity_registry",
    "homeassistant.helpers.intent", "homeassistant.util", "homeassistant.util.json",
    "homeassistant.components", "homeassistant.components.button",
    "homeassistant.components.button.const", "homeassistant.components.input_button",
    "homeassistant.components.cover", "homeassistant.components.cover.const",
    "homeassistant.components.lock", "homeassistant.components.lock.const",
    "homeassistant.components.valve", "homeassistant.components.valve.const",
    "homeassistant.components.number", "homeassistant.components.number.const",
)


def _install_stubs():
    for name in _STUB_NAMES:
        _stub(name)
    # 父子属性挂接（from homeassistant.components import cover 需父模块有属性）
    for child in ("core", "const", "exceptions", "helpers", "util", "components"):
        setattr(sys.modules["homeassistant"], child, sys.modules[f"homeassistant.{child}"])
    comps = sys.modules["homeassistant.components"]
    for comp in ("button", "input_button", "cover", "lock", "valve", "number"):
        setattr(comps, comp, sys.modules[f"homeassistant.components.{comp}"])
    for comp, dom in (("button", "button"), ("cover", "cover"),
                      ("lock", "lock"), ("valve", "valve")):
        sys.modules[f"homeassistant.components.{comp}.const"].DOMAIN = dom
        sys.modules[f"homeassistant.components.{comp}"].DOMAIN = dom
    sys.modules["homeassistant.components.input_button"].DOMAIN = "input_button"
    sys.modules["homeassistant.components.button.const"].SERVICE_PRESS = "press"
    sys.modules["homeassistant.components.number.const"].SERVICE_SET_VALUE = "set_value"
    sys.modules["homeassistant.components.number"].DOMAIN = "number"
    sys.modules["homeassistant.components.number"].const = \
        sys.modules["homeassistant.components.number.const"]

    hc = sys.modules["homeassistant.const"]
    for k, v in _HA_CONST.items():
        setattr(hc, k, v)
    sys.modules["homeassistant.core"].HomeAssistant = type("HomeAssistant", (), {})
    sys.modules["homeassistant.core"].State = type("State", (), {})
    sys.modules["homeassistant.util.json"].JsonObjectType = dict
    sys.modules["homeassistant.util.json"].JsonValueType = object

    it = sys.modules["homeassistant.helpers.intent"]
    it.Intent = object
    it.IntentHandler = IntentHandler
    it.IntentHandleError = IntentHandleError
    it.IntentResponse = object
    it.MatchTargetsConstraints = MatchTargetsConstraints
    it.async_match_targets = _async_match_targets
    it.non_empty_string = str

    er = sys.modules["homeassistant.helpers.entity_registry"]
    dr = sys.modules["homeassistant.helpers.device_registry"]
    ar = sys.modules["homeassistant.helpers.area_registry"]
    er.RegistryEntry = type("RegistryEntry", (), {})
    er.EntityRegistry = type("EntityRegistry", (), {})
    er.async_get = lambda hass: hass.entity_registry
    dr.async_get = lambda hass: hass.device_registry
    ar.async_get = lambda hass: hass.area_registry


def load(name: str):
    """真源码 import（替身注入后执行，失败不入缓存）——同 test_v1125 范式。"""
    _install_stubs()
    key = f"{PKG}.{name}"
    if key in sys.modules:
        return sys.modules[key]
    pkg = sys.modules.get(PKG)
    if pkg is None:
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [str(CC)]
        sys.modules[PKG] = pkg
    spec = importlib.util.spec_from_file_location(key, CC / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)              # 先执行成功再入缓存（半初始化不缓存）
    sys.modules[key] = mod
    return mod


@pytest.fixture(autouse=True)
def _fresh_stubs():
    """同会话其它文件会重装 homeassistant 替身——每条用例前幂等回装。"""
    _install_stubs()
    yield


def _extract(path: Path, names: list[str], extra_ns: dict) -> dict:
    """按名从真源码摘出模块级 def/class/赋值（含装饰器）后 exec——不复制逻辑。

    intent_adjust_attribute 的 import 面横跨 climate/fan/light/number 全族 +
    Platform，整包 stub 成本远大于收益；被测函数本体照 test_v1043 手法真执行。
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    ns = dict(extra_ns)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            key = node.name
        elif isinstance(node, ast.Assign):
            key = getattr(node.targets[0], "id", None)
        elif isinstance(node, ast.AnnAssign):
            key = getattr(node.target, "id", None)
        else:
            continue
        if key in names:
            exec(compile(ast.unparse(node), str(path), "exec"), ns)  # noqa: S102
    missing = [n for n in names if n not in ns]
    assert not missing, f"{path.name} 缺定义: {missing}"
    return ns


# ── 通用假件 ────────────────────────────────────────────────────
class _States:
    def __init__(self, items):
        self._items = list(items)

    def async_all(self, domain=None):
        if not domain:
            return list(self._items)
        doms = {domain} if isinstance(domain, str) else set(domain)
        return [s for s in self._items if s.entity_id.split(".", 1)[0] in doms]

    def get(self, entity_id):
        for s in self._items:
            if s.entity_id == entity_id:
                return s
        return None


class _Reg:
    def __init__(self, mapping=None):
        self._m = dict(mapping or {})

    def async_get(self, key):
        return self._m.get(key)


class _AreaReg(_Reg):
    def async_get_area(self, area_id):
        return self._m.get(area_id)

    def async_get_area_by_name(self, name):
        for a in self._m.values():
            if _area_hits(a, name):
                return a
        return None

    def async_list_areas(self):
        return list(self._m.values())


class _DevReg(_Reg):
    @property
    def devices(self):
        return list(self._m.values())


class _EntReg(_Reg):
    def __init__(self, mapping=None):
        super().__init__(mapping)
        self.entities = self._m


class FakeServices:
    """服务调用留痕 + 可按 (domain, service, entity_id) 精确注入失败。"""

    def __init__(self):
        self.calls = []
        self.fail_on = set()

    def has_service(self, domain, service):
        return True

    async def async_call(self, domain, service, service_data=None, context=None,
                         blocking=False, target=None, return_response=None):
        data = dict(service_data or {})
        self.calls.append((domain, service, data))
        if (domain, service, data.get("entity_id")) in self.fail_on:
            raise RuntimeError(f"{domain}.{service} 拒绝了 {data.get('entity_id')}")
        return None

    def pressed(self):
        return [c[2].get("entity_id") for c in self.calls
                if c[0] == "button" and c[1] == "press"]

    def calls_of(self, domain, service):
        return [c[2].get("entity_id") for c in self.calls
                if c[0] == domain and c[1] == service]


class FakeHass:
    def __init__(self, states=(), entities=None, devices=None, areas=None):
        self.states = _States(states)
        self.entity_registry = _EntReg(entities)
        self.device_registry = _DevReg(devices)
        self.area_registry = _AreaReg(areas)
        self.services = FakeServices()
        self.data = {}
        self.tasks = []

    def async_create_task(self, coro, *a, **kw):
        # C7 口径：只给公开面；internal 不提供（回退旧 API 即红）。
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task


def _st(eid, name, state="off"):
    return SimpleNamespace(entity_id=eid, name=name, state=state,
                           domain=eid.split(".", 1)[0], unique_id="",
                           attributes={"friendly_name": name})


def _entry(area_id=None, device_id=None, name=None, aliases=(), domain=None,
           entity_id=None):
    return SimpleNamespace(area_id=area_id, device_id=device_id, name=name,
                           aliases=set(aliases), domain=domain, id=entity_id,
                           entity_id=entity_id,
                           hidden_by=None, disabled_by=None)


def _area(area_id, name, aliases=()):
    return SimpleNamespace(id=area_id, name=name, aliases=set(aliases))


def _device(device_id, name, area_id=None):
    return SimpleNamespace(id=device_id, name=name, name_by_user=None, area_id=area_id)


def _ito(hass, slots, **kw):
    return SimpleNamespace(hass=hass, slots=slots, context=object(), assistant=None,
                           language="zh-CN", **kw)


def _no_python_internals(result: dict, feature: str) -> None:
    """返回体是给加载项/用户看的：绝不带 Python 内部串（异常类名/协程名）。"""
    blob = json.dumps(result, ensure_ascii=False, default=str)
    for bad in ("object has no attribute", "Traceback", "_run_then_background",
                "coroutine", "asyncio.", "<bound method"):
        assert bad not in blob, f"{feature} 返回体泄漏内部串 {bad!r}: {blob}"


# ── ① 解锁/上锁：服务必须真下发 ─────────────────────────────────
def test_unlock_dispatches_lock_service_and_reports_clean_success():
    lock = load("intent_lock")
    states = [_st("lock.da_men", "大门", "locked"), _st("switch.da_men", "大门")]
    ents = {"lock.da_men": _entry(area_id="ke_ting"),
            "switch.da_men": _entry(area_id="ke_ting")}
    hass = FakeHass(states, ents, {}, {"ke_ting": _area("ke_ting", "客厅")})
    intent_obj = _ito(hass, {"target": {"value": [
        {"area": "客厅", "devices": [{"name": "大门"}]}]}})

    result = asyncio.run(lock.HassUnlockIntent().async_handle(intent_obj))

    assert hass.services.calls_of("lock", "unlock") == ["lock.da_men"], \
        f"锁服务未下发（P0：_run_then_background 不存在）: {hass.services.calls}"
    assert result.get("success") is True, result
    assert [s["name"] for s in result["states"]] == ["大门"], result
    _no_python_internals(result, "HassUnlock")


def test_lock_all_entities_path_also_dispatches():
    """空目标（全屋锁）同一根因面——不得只在具名路径上活。"""
    lock = load("intent_lock")
    hass = FakeHass([_st("lock.a", "A锁", "locked"), _st("lock.b", "B锁", "unlocked")])
    result = asyncio.run(lock.HassLockIntent().async_handle(_ito(hass, {})))
    assert sorted(hass.services.calls_of("lock", "lock")) == ["lock.a", "lock.b"], \
        result
    assert result.get("success") is True, result


# ── ② 锁：逐台 success 必须按真实调用结果回填 ───────────────────
def test_lock_partial_failure_is_not_reported_as_success():
    lock = load("intent_lock")
    hass = FakeHass([_st("lock.a", "A锁", "locked"), _st("lock.b", "B锁", "unlocked")])
    hass.services.fail_on.add(("lock", "unlock", "lock.a"))
    result = asyncio.run(lock.HassUnlockIntent().async_handle(_ito(hass, {})))

    rows = {r["name"]: r for r in result.get("states") or []}
    assert rows["A锁"]["success"] is False, f"失败台仍报成功: {result}"
    assert rows["B锁"]["success"] is True, result
    assert result.get("success") is True, result          # 有一台成功 → 整体成功
    assert result.get("partial_error"), "部分失败必须可复述（partial_error）"
    _no_python_internals(result, "HassUnlock")


# ── ③ 帘族（纱窗/百叶窗）绝不升级成"本区所有窗" ─────────────────
@pytest.mark.parametrize("cover_name", ["百叶窗", "纱窗"])
def test_curtain_family_never_escalates_to_all_windows(cover_name):
    turn = load("intent_turn")
    states = [_st(f"cover.{i}", cover_name, "open")
              for i in (1,)] + [
        _st("button.kt_chuang_kais", "客厅窗户 开启"),
        _st("button.kt_chuang_guan", "客厅窗户 关闭"),
        _st("button.kt_pingtui_guan", "客厅平推窗 关闭"),
    ]
    ents = {f"cover.{i}": _entry(area_id="ting") for i in (1,)}
    ents.update({b: _entry(area_id="ting") for b in
                 ("button.kt_chuang_kais", "button.kt_chuang_guan",
                  "button.kt_pingtui_guan")})
    hass = FakeHass(states, ents, {}, {"ting": _area("ting", "客厅")})
    intent_obj = _ito(hass, {"target": {"value": [
        {"area": "客厅", "devices": [{"name": cover_name, "domains": ["cover"]}]}]}})

    result = asyncio.run(turn.TurnDeviceOffIntent().async_handle(intent_obj))

    assert hass.services.pressed() == [], \
        f"帘族 {cover_name} 被当窗控按了本区窗钮（关一扇变关一排）: {hass.services.calls}"
    assert hass.services.calls_of("cover", "close_cover") == [f"cover.1"], \
        f"{cover_name} 应落该 cover 单设备: {hass.services.calls}"
    assert result.get("success") is True, result


# ── ④ 窗侧失败必须并入主返回（窗败灯成不得只播"关了"）────────────
def test_window_failure_merged_into_partial_error():
    turn = load("intent_turn")
    states = [_st("light.kt_deng", "灯", "on")]
    ents = {"light.kt_deng": _entry(area_id="ke_ting")}
    hass = FakeHass(states, ents, {}, {"ke_ting": _area("ke_ting", "客厅")})
    intent_obj = _ito(hass, {"target": {"value": [
        {"area": "客厅", "devices": [{"name": "平开窗", "domains": ["window"]}]},
        {"area": "客厅", "devices": [{"name": "灯", "domains": ["light"]}]},
    ]}})

    result = asyncio.run(turn.TurnDeviceOffIntent().async_handle(intent_obj))

    assert hass.services.calls_of("light", "turn_off") == ["light.kt_deng"], result
    assert result.get("success") is True, result
    assert result.get("partial_error"), \
        f"窗侧失败被丢弃（灯成即谎报全成）: {result}"
    assert "平开窗" in result["partial_error"], result


# ── ⑤ 裸「窗/窗子」= 本区域所有窗（不得被旧等值判定全剔）──────────
@pytest.mark.parametrize("bare", ["窗", "窗子"])
def test_bare_window_word_escalates_to_all_windows(bare):
    turn = load("intent_turn")
    states = [_st("button.kt_chuang_kais", "客厅窗户 开启"),
              _st("button.kt_chuang_guan", "客厅窗户 关闭"),
              _st("button.kt_pingtui_guan", "客厅平推窗 关闭")]
    ents = {s.entity_id: _entry(area_id="ting") for s in states}
    hass = FakeHass(states, ents, {}, {"ting": _area("ting", "客厅")})
    intent_obj = _ito(hass, {"target": {"value": [
        {"area": "客厅", "devices": [{"name": bare, "domains": ["window"]}]}]}})

    result = asyncio.run(turn.TurnDeviceOffIntent().async_handle(intent_obj))

    assert sorted(hass.services.pressed()) == [
        "button.kt_chuang_guan", "button.kt_pingtui_guan"], \
        f"裸「{bare}」应关本区全部窗（旧等值判定下被全剔）: {hass.services.calls}"
    assert result.get("success") is True, result


# ── ⑥ number.set_value：相对档 / 极值档 / 无法确定如实失败 ────────
def _number_ns():
    it = sys.modules["homeassistant.helpers.intent"]
    ns = {
        "re": re,
        "Enum": __import__("enum").Enum,
        "dataclass": dataclass,
        "field": field,
        "get_args": __import__("typing").get_args,
        "Literal": __import__("typing").Literal,
        "int": int,
        "float": float,
        "str": str,
        "State": object,
        "DeltaSpecialValue": str,
        "number": SimpleNamespace(const=SimpleNamespace(SERVICE_SET_VALUE="set_value")),
        "intent": it,
        "_LOGGER": logging.getLogger("hv1127ia.number"),
        "adjustment_functions": {},
        "supported_domain_list": set(),
        "supported_attribute_list": set(),
    }
    ns["UnsupportAdjustmentError"] = it.IntentHandleError("unsupported adjustment")
    return _extract(CC / "intent_adjust_attribute.py",
                    ["AdjustType", "DeltaSupport", "DeltaSpecialValue",
                     "DELTA_SPECIAL_VALUES", "Delta", "parse_delta",
                     "AdjustmentContext", "AdjustmentTarget", "register_adjustment",
                     "_current_number_value", "adjust_number_value"], ns)


def _num_ctx(ns, raw, current=30, min_v=0, max_v=100):
    state = SimpleNamespace(state=str(current),
                            attributes={"min": min_v, "max": max_v})
    return ns["AdjustmentContext"](state=state, delta=ns["parse_delta"](raw))


def _apply_number(ns, raw, **kw):
    """走真注册表（@register_adjustment("number","value") 的装饰器路径）。"""
    target = ns["AdjustmentTarget"]()
    handler = ns["adjustment_functions"]["number"]["value"]
    handler(_num_ctx(ns, raw, **kw), target)
    return target


def test_number_relative_delta_applies_to_current_value():
    """「数值调高10」= current+10（旧版当绝对 10 → set_value(10)）。"""
    ns = _number_ns()
    assert _apply_number(ns, "+10", current=30).service_data["value"] == 40
    assert _apply_number(ns, "-20", current=50).service_data["value"] == 30


def test_number_special_max_min_maps_to_entity_bounds():
    """「调到最大/最小」= 实体 min/max（旧版 value=0 → set_value(0)=关到最低）。"""
    ns = _number_ns()
    t = _apply_number(ns, "max", current=30, min_v=0, max_v=100)
    assert t.service_data["value"] == 100, t.service_data
    t = _apply_number(ns, "min", current=30, min_v=5, max_v=100)
    assert t.service_data["value"] == 5, t.service_data


def test_number_special_without_bounds_fails_honestly():
    """没有 min/max 就无从上界——不许猜（旧版猜 0）。"""
    ns = _number_ns()
    state = SimpleNamespace(state="30", attributes={})
    ctx = ns["AdjustmentContext"](state=state, delta=ns["parse_delta"]("max"))
    with pytest.raises(IntentHandleError):
        ns["adjustment_functions"]["number"]["value"](ctx, ns["AdjustmentTarget"]())


def test_number_special_unmappable_fails_honestly():
    """medium/auto 在数值实体无对应语义——如实失败而非静默写 0。"""
    ns = _number_ns()
    with pytest.raises(IntentHandleError):
        _apply_number(ns, "medium", current=30)
    ns2 = _number_ns()
    assert _apply_number(ns2, "60", current=30).service_data["value"] == 60  # 绝对值不回归


# ── ⑦ split_actions：仅 Turn* 映射，其余原样透传 action 与全部参数 ─
def test_split_actions_passes_through_non_turn_actions():
    shared = load("intent_device_shared")
    acts = [{
        "name": "ControlWindow",
        "parameters": {
            "target": [{"area": "卧室",
                        "devices": [{"name": "百叶窗", "domains": ["cover"]}]}],
            "action": "open", "position": 30,
        },
    }]
    out = shared.split_actions_by_device(acts)
    assert len(out) == 1, out
    assert out[0]["name"] == "ControlWindow", out
    assert out[0]["parameters"].get("action") == "open", \
        f"非 Turn* 动作被三元定死 close: {out}"
    assert out[0]["parameters"].get("position") == 30, \
        f"position 等参数被重建丢弃（入库即坏）: {out}"

    # 无 action 槽的非 Turn* 意图（PauseDevice）：不得凭空注入 "close"
    out2 = shared.split_actions_by_device([{
        "name": "PauseDevice",
        "parameters": {"target": [{"area": "卧室",
                                   "devices": [{"name": "平开窗",
                                                "domains": ["window"]}]}]},
    }])
    assert "action" not in out2[0]["parameters"], out2


def test_split_actions_turn_family_still_maps_to_controlwindow():
    """Turn* 族契约不回退：窗设备拆出 → ControlWindow + open/close。"""
    shared = load("intent_device_shared")
    out = shared.split_actions_by_device([{
        "name": "TurnDeviceOff",
        "parameters": {"target": [{"area": "客厅", "devices": [
            {"name": "灯", "domains": ["light"]},
            {"name": "平开窗", "domains": ["window"]},
        ]}]},
    }])
    by_name = {a["name"]: a for a in out}
    assert set(by_name) == {"TurnDeviceOff", "ControlWindow"}, out
    assert by_name["ControlWindow"]["parameters"]["action"] == "close", out
    assert by_name["ControlWindow"]["parameters"]["target"][0]["area"] == "客厅", out
    assert by_name["TurnDeviceOff"]["parameters"]["target"][0]["devices"][0]["name"] == "灯", out


# ── ⑧ 实体匹配：区域名/别名、空 area、多目标名称过滤 ─────────────
def _helper_world():
    """客厅（别名 起居室）+ 灯在客厅 + 射灯（无别名实体，靠包含回捞）。"""
    helper = load("intent_helper")
    states = [_st("light.deng", "灯", "off"),
              _st("light.she_deng", "TSL2011 射灯", "off"),
              _st("climate.kt_ac", "空调", "off")]
    ents = {
        "light.deng": _entry(area_id="ke_ting", device_id="dev_wg"),
        "light.she_deng": _entry(area_id="ke_ting", device_id="dev_tsl"),
        "climate.kt_ac": _entry(area_id="ke_ting"),
    }
    devs = {"dev_wg": _device("dev_wg", "网关 A", "ke_ting"),
            "dev_tsl": _device("dev_tsl", "TSL2011", "ke_ting")}
    areas = {"ke_ting": _area("ke_ting", "客厅", aliases=("起居室",))}
    return helper, FakeHass(states, ents, devs, areas)


def test_area_info_name_is_registry_name_not_alias():
    """区域 name 恒取注册名；别名另存且比较时算命中（旧版取 aliases+… 的 [0]）。"""
    helper, hass = _helper_world()
    info = helper.get_entity_area(hass, hass.entity_registry.async_get("light.deng"))
    assert info is not None
    assert info.name == "客厅", f"区域名取了别名（set 无序 ⇒ 比较随机丢候选）: {info}"
    assert info.matches("客厅") and info.matches("起居室"), info


def test_area_alias_still_matches_in_name_filter():
    """行为面：区域名取真名后，口述**别名**仍须命中（不许修一半）。

    走第 5 级「设备注册表兜底」的真路径（网关按钮名「开启」≠ 用户口述设备名
    「开窗器 01」）：该级回退里的区域比较就是 `entity_area.name != requested_area`
    这一处（intent_helper:509）。
    """
    helper = load("intent_helper")
    states = [_st("button.kt_open", "开启", "off")]
    ents = {"button.kt_open": _entry(device_id="dev_ck", domain="button",
                                     entity_id="button.kt_open")}
    devs = {"dev_ck": _device("dev_ck", "开窗器 01", "ke_ting")}
    areas = {"ke_ting": _area("ke_ting", "客厅", aliases=("起居室",))}
    hass = FakeHass(states, ents, devs, areas)

    def _find(area_word):
        targets = [{"area": area_word,
                    "devices": [{"name": "开窗器 01", "domains": ["button"]}]}]
        return asyncio.run(helper.match_intent_entities(_ito(hass, {}), targets))

    err, cands = _find("客厅")            # 注册名
    assert err is None and cands, f"区域真名被别名顶掉 ⇒ 回退过滤丢候选: {err}"
    assert [c.state.entity_id for c in cands] == ["button.kt_open"], cands
    err2, cands2 = _find("起居室")        # 别名（修一半会回退 → 本行变红）
    assert err2 is None and cands2, f"别名未算命中: {err2}"


def test_empty_area_means_no_area_constraint():
    """空 area（LLM/REST 常传）＝未点名区域，不得把挂区域的实体全剔。"""
    helper, hass = _helper_world()
    targets = [{"area": "", "devices": [{"name": "灯", "domains": ["light"]}]}]
    found, _doms = asyncio.run(helper._match_with_constraints(hass, targets, None))
    assert [s.entity_id for s in found[0].states] == ["light.deng"], found
    cands = helper._build_candidate_entities(hass, found, hass.entity_registry)
    assert [c.state.entity_id for c in cands] == ["light.deng"], \
        f"空 area 被当'有区域'剔除（挂区域实体全丢）: {cands}"


def test_multi_target_name_filter_keeps_every_target():
    """多目标：逐目标/逐 device 各自过滤后再合并（旧版只用第一个名字全局过滤）。"""
    helper, hass = _helper_world()
    intent_obj = _ito(hass, {"target": {"value": [
        {"area": "客厅", "devices": [{"name": "灯", "domains": ["light"]}]},
        {"area": "客厅", "devices": [{"name": "空调", "domains": ["climate"]}]},
    ]}})
    err, cands = asyncio.run(helper.match_intent_entities(intent_obj, [
        {"area": "客厅", "devices": [{"name": "灯", "domains": ["light"]}]},
        {"area": "客厅", "devices": [{"name": "空调", "domains": ["climate"]}]},
    ]))
    assert err is None and cands, (err, cands)
    assert sorted(c.state.entity_id for c in cands) == ["climate.kt_ac", "light.deng"], \
        f"多目标其余被静默丢弃却回 success: {[c.state.entity_id for c in cands]}"


# ── r2（金标复测 2 轮）：帘族/无区域裸窗不得被当窗控 ────────────────
def test_curtain_button_only_pair_never_pressed_as_window():
    """帘族（纱窗）在"只有按钮对"地形曾被当窗控按下（开启+关闭都按）。
    正确行为：不当窗控（不得按任何窗钮、不得回 success）。"""
    turn = load("intent_turn")
    states = [
        _st("button.sha_chuang_kai", "客厅纱窗 开启"),
        _st("button.sha_chuang_guan", "客厅纱窗 关闭"),
    ]
    ents = {"button.sha_chuang_kai": _entry(area_id="ting"),
            "button.sha_chuang_guan": _entry(area_id="ting")}
    hass = FakeHass(states, ents, {}, {"ting": _area("ting", "客厅")})
    intent_obj = _ito(hass, {"target": {"value": [
        {"area": "客厅", "devices": [{"name": "纱窗", "domains": ["button"]}]}]}})

    result = asyncio.run(turn.TurnDeviceOffIntent().async_handle(intent_obj))

    pressed = [c[2].get("entity_id") for c in hass.services.calls
               if c[0] == "button" and c[1] == "press"]
    # 旧病：动作词表只有单字「开/关」→「开启/关闭」不分组 ⇒ 两个钮各自成组全按
    # （关一扇＝把纱窗开回去）。正确：只按与命令同向的那一个。
    assert pressed == ["button.sha_chuang_guan"], \
        f"帘族按钮对应只按同向钮（实得 {pressed}）"
    assert result.get("success") is not True or pressed == ["button.sha_chuang_guan"], result


def test_bare_window_without_area_never_presses_whole_house():
    """无区域裸「窗」曾被升级为全屋盲按（客厅+卧室两扇都按）。
    正确行为：最多按"唯一命中"的那一扇，绝不跨区全按。"""
    turn = load("intent_turn")
    states = [
        _st("button.ke_ting_chuang_guan", "客厅窗户 关闭"),
        _st("button.wo_shi_chuang_guan", "卧室窗户 关闭"),
    ]
    ents = {"button.ke_ting_chuang_guan": _entry(area_id="ke_ting"),
            "button.wo_shi_chuang_guan": _entry(area_id="wo_shi")}
    hass = FakeHass(states, ents, {}, {"ke_ting": _area("ke_ting", "客厅"),
                                       "wo_shi": _area("wo_shi", "卧室")})
    intent_obj = _ito(hass, {"target": {"value": [
        {"area": "", "devices": [{"name": "窗", "domains": ["window"]}]}]}})

    result = asyncio.run(turn.TurnDeviceOffIntent().async_handle(intent_obj))

    pressed = [c[2].get("entity_id") for c in hass.services.calls
               if c[0] == "button" and c[1] == "press"]
    assert len(pressed) <= 1, f"无区域裸窗被升级成全屋盲按：{pressed}"
    assert not ({"button.ke_ting_chuang_guan",
                 "button.wo_shi_chuang_guan"} <= set(pressed)), \
        f"两间房全按（跨区）：{pressed}"
    assert result.get("success") is not True or len(pressed) == 1, result
