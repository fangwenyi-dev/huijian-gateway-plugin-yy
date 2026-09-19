# -*- coding: utf-8 -*-
"""v1.0.97 意图槽位校验 500 崩面收口钉（2026-09-19 VM HAOS 2026.9.2 全量实锤）。

事故链（裸「关闭」→「集成还没生效」三日悬案的终章）：
executor 开关族裸词按通道设计空 args 透传（test_executor_turn_gate:125 钉）→
ha_client POST /api/intent/handle → HA core `async_validate_slots` 裸抛
（①slot_schema=None→迭代 None 抛 AttributeError；②Required 键缺失→vol.Invalid）→
HTTP 500 纯文本 → ha_client 洗成「HA 内部错误(500)」→ zh_error 指路"重启/确认安装"。
VM 实锤 11 个注册面全崩（TurnDeviceOn/Off、PauseDevice、SetDeviceMode、
AdjustDeviceAttribute、场景 create/trigger、自动化 create/delete/update）+
live_context schema=None 崩；ControlWindow/HassLock/Unlock 因 M5 式整段 try 幸存。

本文件三层钉：
① 行为：真载 intent_turn，TurnDeviceOn/Off/PauseDevice 三 handler 在 core 裸抛
   形态下 async_handle **必返结构化失败**（success False + 具名分因），
   IntentHandleError 透传，正常流不误伤（空注册表→No available devices found）；
② helper 真值表：validate_slots_safely 四形态（崩/透传/正常/None-slots）；
③ AST 全量防回退：huijian_ai 所有 intent_*.py 中 `self.async_validate_slots(`
   调用点必须整处位于 try 内；validate_slots_safely 使用处=11（把任一站点改回
   裸调用或删 try，本钉当场红）。
"""
import ast
import asyncio
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_v1097_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

import test_window_speed_behavior as bench  # noqa: E402  触发替身装载

CC_DIR = bench.CC_DIR
_STUB = bench._stub


def _extend_stubs_for_turn():
    """intent_turn 需要的额外替身面（幂等，_stub 复用现有模块对象）。"""
    ha = sys.modules["homeassistant"]
    comps = _STUB("homeassistant.components")
    for dom in ("lock", "valve"):
        mod = _STUB(f"homeassistant.components.{dom}")
        cmc = _STUB(f"homeassistant.components.{dom}.const")
        cmc.DOMAIN = dom
        mod.DOMAIN = dom
        mod.const = cmc
        setattr(comps, dom, mod)
    # input_button 模块 bench 已建但没挂 components 属性——from-import 链需要
    _STUB("homeassistant.components.input_button").DOMAIN = "input_button"
    comps.input_button = sys.modules["homeassistant.components.input_button"]
    cc = _STUB("homeassistant.components.cover.const")
    cc.DOMAIN = "cover"
    it = _STUB("homeassistant.helpers.intent")

    class _MatchTargetsConstraints:
        def __init__(self, **kw):
            self.kw = kw

    def _match_targets(hass, constraints):
        # 空注册表镜像：永不命中 → 走 "No available devices found" 业务分支
        return types.SimpleNamespace(is_match=False, states=[])

    it.MatchTargetsConstraints = _MatchTargetsConstraints
    it.async_match_targets = _match_targets
    const = ha.const
    for name in ("SERVICE_TURN_ON", "SERVICE_TURN_OFF", "SERVICE_LOCK",
                 "SERVICE_UNLOCK", "SERVICE_OPEN_COVER", "SERVICE_CLOSE_COVER",
                 "SERVICE_OPEN_VALVE", "SERVICE_CLOSE_VALVE"):
        setattr(const, name, name.lower())


@pytest.fixture(autouse=True)
def _stubs_in_place():
    bench._install_ha_stubs()
    _extend_stubs_for_turn()
    yield


def _load(mod_name):
    full = f"hjspeed_pkg.{mod_name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, CC_DIR / f"{mod_name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def _turn():
    bench._install_ha_stubs()
    _extend_stubs_for_turn()
    return _load("intent_turn")


@pytest.fixture(scope="module")
def _helper():
    bench._install_ha_stubs()
    return _load("intent_helper")


class _StatesFS(bench._States):
    """fallback 轮 intent_helper:479 用 states.async_all(domain) 带参形态。"""

    def async_all(self, domain=None):
        return list(self._states)


def _ito(slots=None):
    h = types.SimpleNamespace()
    h._er = bench._ER([])
    h._dr = bench._DR({})
    h._dr.devices = {}   # intent_helper:423 fallback 轮要 registry.devices
    h._ar = bench._AR()
    h.states = _StatesFS([])
    h.services = bench._Services()
    return types.SimpleNamespace(hass=h, context=None, assistant=None,
                                 slots=slots if slots is not None else {})


class _Crash(Exception):
    """core 裸抛形态的替身穿甲（AttributeError/vol.Invalid 皆 Exception）。"""


def _explodes(exc):
    def _v(_slots):
        raise exc
    return _v


# ── ① 行为：三 handler 崩面全收口 ──────────────────────────────
@pytest.mark.parametrize("cls_name", [
    "TurnDeviceOnIntent", "TurnDeviceOffIntent", "PauseDeviceIntent"])
def test_core_slot_crash_becomes_structured(_turn, cls_name):
    handler = getattr(_turn, cls_name)()
    feature = handler.intent_type
    # Required 键缺失形态（VM 实锤消息）
    handler.async_validate_slots = _explodes(
        ValueError("required key not provided @ data['target']"))
    res = asyncio.run(handler.async_handle(_ito({})))
    assert isinstance(res, dict), f"{feature} 又裸抛了——REST 面=HTTP 500"
    assert res["success"] is False
    assert feature in res["error"] and "参数校验未通过" in res["error"]


@pytest.mark.parametrize("cls_name", [
    "TurnDeviceOnIntent", "TurnDeviceOffIntent", "PauseDeviceIntent"])
def test_crash_path_has_no_service_side_effects(_turn, cls_name):
    handler = getattr(_turn, cls_name)()
    handler.async_validate_slots = _explodes(_Crash("boom"))
    ito = _ito({})
    res = asyncio.run(handler.async_handle(ito))
    assert res["success"] is False
    assert ito.hass.services.calls == [], "校验未过绝不允许已经动了设备"


def test_intent_handle_error_passthrough(_turn):
    """HA 正规失败通道不得被二次折叠（M5 窗控同口径）。"""
    it_stubs = sys.modules["homeassistant.helpers.intent"]
    handler = _turn.TurnDeviceOffIntent()
    handler.async_validate_slots = _explodes(
        it_stubs.IntentHandleError("target invalid"))
    with pytest.raises(it_stubs.IntentHandleError):
        asyncio.run(handler.async_handle(_ito({})))


def test_normal_flow_untouched(_turn):
    """正常 slots（替身校验=透传）→ 走既有业务链，空注册表如实 No available。"""
    handler = _turn.TurnDeviceOffIntent()
    slots = {"target": {"value": [{"area": "客厅",
                                   "devices": [{"name": "不存在的灯", "domains": []}]}]}}
    res = asyncio.run(handler.async_handle(_ito(slots)))
    assert isinstance(res, dict) and res["success"] is False
    assert "No available devices found" in res.get("error", ""), res


# ── ② helper 真值表 ────────────────────────────────────────────
def test_helper_truth_table(_helper):
    it_stubs = sys.modules["homeassistant.helpers.intent"]

    class H:
        def __init__(self, fn):
            self.async_validate_slots = fn

    ok = {"target": {"value": []}}
    s, f = _helper.validate_slots_safely(H(lambda sl: ok), _ito(), "X")
    assert s is ok and f is None
    s, f = _helper.validate_slots_safely(H(_explodes(AttributeError(
        "'NoneType' object has no attribute 'items'"))), _ito(), "HuijianGetLiveContext")
    assert s is None and f["success"] is False
    assert "HuijianGetLiveContext" in f["error"]
    with pytest.raises(it_stubs.IntentHandleError):
        _helper.validate_slots_safely(
            H(_explodes(it_stubs.IntentHandleError("x"))), _ito(), "X")


# ── ③ AST 全量防回退 ───────────────────────────────────────────
# 修复后各文件**残留直调**数（只许 M5 整段 try 的两家各 1，其余必须 0）
_EXPECTED_DIRECT = {
    "intent_turn.py": 0,
    "intent_set_mode.py": 0,
    "intent_adjust_attribute.py": 0,
    "intent_live_context.py": 0,
    "intent_voice_scene.py": 0,
    "intent_automation.py": 0,
    "intent_window_control.py": 1,   # M5 既有整段 try
    "intent_lock.py": 1,             # 既有整段 try
}
# helper 守护调用站点数（VM 实锤 11 崩面一坑一钉）
_GUARDED_SITES = {
    "intent_turn.py": 3,
    "intent_set_mode.py": 1,
    "intent_adjust_attribute.py": 1,
    "intent_live_context.py": 1,
    "intent_voice_scene.py": 3,
    "intent_automation.py": 3,
}


def _try_covered_linenos(tree):
    covered = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Try, ast.TryStar)):
            for sub in node.body:
                for call in ast.walk(sub):
                    if isinstance(call, ast.Call):
                        covered.add(call.lineno)
    return covered


def test_no_direct_validate_slots_call_outside_try():
    """把任一已修站点改回裸调用（且不在 try 内）→ 本钉当场红。"""
    bad = []
    for p in sorted(CC_DIR.glob("intent*.py")):
        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src)
        covered = _try_covered_linenos(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "async_validate_slots"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"
                    and node.lineno not in covered):
                bad.append(f"{p.name}:{node.lineno}")
    assert bad == [], f"裸 async_validate_slots 复活（=500 崩面）: {bad}"


def test_guard_call_site_counts():
    """站点数量钉：11 崩面全走 helper；残留直调只许 window/lock 的既有 try。"""
    for f, want in _EXPECTED_DIRECT.items():
        src = (CC_DIR / f).read_text(encoding="utf-8")
        n = src.count("self.async_validate_slots(")
        assert n == want, f"{f} 直调点数 {n} ≠ {want}（站点漂移=防线漂移）"
    for f, want in _GUARDED_SITES.items():
        src = (CC_DIR / f).read_text(encoding="utf-8")
        n = src.count("validate_slots_safely(")
        assert n == want, f"{f} 守护调用 {n} ≠ {want}"


def test_helper_defined_in_intent_helper():
    src = (CC_DIR / "intent_helper.py").read_text(encoding="utf-8")
    assert "def validate_slots_safely(" in src
    assert "except intent.IntentHandleError" in src, "透传通道被删=窗控 M5 口径回退"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
