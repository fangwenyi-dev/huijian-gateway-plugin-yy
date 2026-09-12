# -*- coding: utf-8 -*-
"""开窗器速度/力度参数——集成 handler 真跑行为钉（fake-registry 台架）。

test_window_speed.py 的源码契约钉只能证明"文件里写了什么"；本文件按仓内
既有的 HA 替身导入先例（test_integration_link_stability）把
intent_window_control **真 import、真执行**：注册表替身的数据形状与网关
number.py 实锤一致（同设备 button/number 共享 device_id，unique_id 后缀
_speed/_strength），服务调用逐字节记账。若代码的域过滤/后缀匹配/下发数据
键写错，寻径或记账必然对不上——这是"替身不恒成功"口径下的最强本地实证；
真机 E2E 仍以现场三行日志复核。
"""
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
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_winsbdb_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

CC_DIR = HERE / "custom_components" / "huijian_ai"


# ── homeassistant 替身（只为跑通导入与注册表寻径，非业务替身）──────
def _stub(name):
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    return mod


def _install_ha_stubs():
    ha = _stub("homeassistant")
    core = _stub("homeassistant.core")

    class HomeAssistant:  # noqa: F811
        pass

    class State:
        def __init__(self, entity_id, name="", attributes=None, state="unknown"):
            self.entity_id = entity_id
            self.domain = entity_id.split(".", 1)[0]
            self.name = name
            self.state = state
            self.attributes = attributes or {}

    core.HomeAssistant = HomeAssistant
    core.State = State
    global _State
    _State = State

    _stub("homeassistant.helpers")
    _stub("homeassistant.util")
    uj = _stub("homeassistant.util.json")
    uj.JsonObjectType = dict
    uj.JsonValueType = object

    ar = _stub("homeassistant.helpers.area_registry")
    dr = _stub("homeassistant.helpers.device_registry")
    er = _stub("homeassistant.helpers.entity_registry")
    it = _stub("homeassistant.helpers.intent")

    class Intent:
        pass

    class IntentHandler:
        slot_schema = None

        def async_validate_slots(self, slots):
            return slots

    class IntentHandleError(Exception):
        pass

    it.Intent = Intent
    it.IntentHandler = IntentHandler
    it.IntentHandleError = IntentHandleError
    it.non_empty_string = str
    er.async_get = lambda hass: hass._er
    er.async_entries_for_device = (
        lambda reg, device_id: [e for e in reg.by_id.values() if e.device_id == device_id])
    # py≤3.13 在 def 期求值注解（intent_helper: er.RegistryEntry/EntityRegistry、
    # intent.Intent）——AST 扫净，缺一即整包 import 崩
    er.RegistryEntry = type("RegistryEntry", (), {})
    er.EntityRegistry = type("EntityRegistry", (), {})
    dr.async_get = lambda hass: hass._dr
    ar.async_get = lambda hass: hass._ar

    comps = _stub("homeassistant.components")
    btn = _stub("homeassistant.components.button")
    btnc = _stub("homeassistant.components.button.const")
    btnc.DOMAIN = "button"
    btnc.SERVICE_PRESS = "press"
    btn.DOMAIN = "button"
    inbtn = _stub("homeassistant.components.input_button")
    inbtn.DOMAIN = "input_button"
    cov = _stub("homeassistant.components.cover")

    class CoverEntityFeature:
        SET_POSITION = 4

    cov.DOMAIN = "cover"
    cov.CoverEntityFeature = CoverEntityFeature
    cov.ATTR_POSITION = "position"
    num = _stub("homeassistant.components.number")
    numc = _stub("homeassistant.components.number.const")
    numc.SERVICE_SET_VALUE = "set_value"
    numc.ATTR_VALUE = "value"
    num.DOMAIN = "number"
    num.const = numc
    comps.cover = cov
    comps.number = num
    comps.button = btn
    ha.const = _stub("homeassistant.const")
    ha.const.ATTR_ENTITY_ID = "entity_id"
    ha.const.ATTR_SUPPORTED_FEATURES = "supported_features"
    ha.const.SERVICE_SET_COVER_POSITION = "set_cover_position"
    # 真 voluptuous 不在测试环境依赖面：本文件不调 slot_schema 构造，
    # 只需 import 期符号存在（intent_helper 顶层 import voluptuous as vol）。
    vol = _stub("voluptuous")

    class _Schema:
        def __init__(self, *a, **k):
            pass

    def _passthrough(*a, **k):
        return a[0] if len(a) == 1 else a

    vol.Schema = _Schema
    vol.Optional = vol.Required = vol.Any = vol.In = vol.All = _passthrough
    vol.Coerce = lambda f: f
    vol.Boolean = bool
    vol.boolean = bool
    vol.string = str
    cv = _stub("homeassistant.helpers.config_validation")
    cv.string = str
    cv.ensure_list = lambda f: f
    cv.slug = str
    return ha


# 别的测试文件（test_integration_link_stability 等）也在 sys.modules 里放
# homeassistant 替身，且可能在其**测试执行期**重装——本 harness 不赌顺序：
# autouse 每条用例前幂等回装（_stub 复用现有模块对象，只补齐本文件需要的
# 属性），State 直接捕获类引用不经 sys.modules。
_State = None


_install_ha_stubs()
_pkg = types.ModuleType("hjspeed_pkg")
_pkg.__path__ = [str(CC_DIR)]
sys.modules.setdefault("hjspeed_pkg", _pkg)
_spec = importlib.util.spec_from_file_location(
    "hjspeed_pkg.intent_window_control", CC_DIR / "intent_window_control.py")
wctl = importlib.util.module_from_spec(_spec)
sys.modules["hjspeed_pkg.intent_window_control"] = wctl
_spec.loader.exec_module(wctl)


@pytest.fixture(autouse=True)
def _stubs_in_place():
    _install_ha_stubs()
    yield


# ── fake hass：数据形状=网关实锤（同设备 button+number，unique_id 后缀）──
class _Entry:
    def __init__(self, entity_id, domain, device_id, unique_id, area_id):
        self.entity_id = entity_id
        self.domain = domain
        self.device_id = device_id
        self.unique_id = unique_id
        self.area_id = area_id


class _Device:
    def __init__(self, name):
        self.name = name
        self.name_by_user = None


class _ER:
    def __init__(self, entries):
        self.by_id = {e.entity_id: e for e in entries}

    def async_get(self, entity_id):
        return self.by_id.get(entity_id)


class _DR:
    def __init__(self, devices):
        self.by_id = devices

    def async_get(self, device_id):
        return self.by_id.get(device_id)


class _AR:
    def async_get_area_by_name(self, name):
        return types.SimpleNamespace(id="area_" + name)


class _States:
    def __init__(self, states):
        self._states = states

    def async_all(self):
        return list(self._states)

    def get(self, entity_id):
        for s in self._states:
            if s.entity_id == entity_id:
                return s
        return None


class _Services:
    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)

    async def async_call(self, domain, service, service_data=None,
                         blocking=False, context=None, **kw):
        eid = (service_data or {}).get("entity_id")
        self.calls.append((domain, service, dict(service_data or {})))
        if eid in self.fail_on:
            raise RuntimeError("boom: 设备离线")


def _hass(speed_devices=1, fail_on=(), with_numbers=True):
    State = _State
    states, entries, devices = [], [], {}
    for i in range(1, speed_devices + 1):
        dev = f"dev{i}"
        name = f"办公室平开窗{i}" if speed_devices > 1 else "办公室平开窗"
        devices[dev] = _Device(name)
        btn = f"button.{name}_open"
        # 真机 friendly_name 形如「设备名 开启」（HA 设备+实体名拼接），
        # "开启" 必须空格独立才过 _find_standalone_keyword——与网关 button 实锤同形。
        states.append(State(btn, name=f"{name} 开启"))
        entries.append(_Entry(btn, "button", dev, f"GW1_SN00{i}_open", "area_办公室"))
        if with_numbers:
            e = f"number.{name}_speed"
            states.append(State(e, name=f"{name} 速度",
                                attributes={"min": 0, "max": 100, "value": 50}))
            entries.append(_Entry(e, "number", dev, f"GW1_SN00{i}_speed", "area_办公室"))
    hass = types.SimpleNamespace()
    hass._er = _ER(entries)
    hass._dr = _DR(devices)
    hass._ar = _AR()
    hass.states = _States(states)
    hass.services = _Services(fail_on=fail_on)
    return hass


def _intent(hass):
    return types.SimpleNamespace(hass=hass, context=None)


def _run(hass, window_name, area, device_name, raw, param="speed"):
    return asyncio.run(wctl._apply_window_param(
        _intent(hass), window_name, area, device_name, raw, param))


# ── 用例 ────────────────────────────────────────────────────────
def test_speed_happy_path_sets_number_entity():
    hass = _hass()
    res = _run(hass, "平开窗", "办公室", "平开窗", 30, "speed")
    assert res["success"] is True, res
    # 恰好一次、打的就是同设备 _speed number 实体、真服务名、value 键
    assert hass.services.calls == [
        ("number", "set_value",
         {"entity_id": "number.办公室平开窗_speed", "value": 30})], hass.services.calls
    assert res["message"] == "已将办公室的平开窗速度设为30%", res


def test_strength_requires_its_own_entity():
    hass = _hass(with_numbers=True)
    # strength 车道：这台没挂 _strength number → 必须如实失败（绝不拿速度实体凑数）
    res = _run(hass, "平开窗", "办公室", "平开窗", "70%", "strength")
    assert res["success"] is False and "力度" in res["error"], res
    assert hass.services.calls == []


def test_missing_number_entity_is_honest_failure():
    hass = _hass(with_numbers=False)
    res = _run(hass, "平开窗", "办公室", "平开窗", 50)
    assert res["success"] is False, res
    assert "没有速度设置" in res["error"] and "v1.4.3" in res["error"], res
    assert hass.services.calls == [], "缺实体绝不能仍下发"


def test_range_guard():
    hass = _hass()
    assert _run(hass, "平开窗", "办公室", "平开窗", 150)["success"] is False
    assert _run(hass, "平开窗", "办公室", "平开窗", "abc")["success"] is False
    assert hass.services.calls == []


def test_partial_failure_not_folded_into_success():
    hass = _hass(speed_devices=2, fail_on={"number.办公室平开窗2_speed"})
    res = _run(hass, "窗户", "办公室", "窗户", 50)
    assert res["success"] is True and "但1扇未成功" in res["message"], res
    assert "已将1扇窗速度设为50%" in res["message"], res
    eids = [c[2]["entity_id"] for c in hass.services.calls]
    assert eids == ["number.办公室平开窗1_speed", "number.办公室平开窗2_speed"], eids


def test_area_all_windows_sets_every_device():
    hass = _hass(speed_devices=2)
    res = _run(hass, "窗户", "办公室", "窗户", 80)
    assert res["success"] is True and "设为80%" in res["message"], res
    assert len(hass.services.calls) == 2


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
