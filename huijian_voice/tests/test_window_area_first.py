# -*- coding: utf-8 -*-
"""2026-09-14 现场事故（13:59 日志）行为钉：「打开办公室平开窗」压中**展厅**平开窗。

NLU 侧槽位没有错（日志实锤 area=办公室 / name=平开窗都解析出来了）；病灶在
集成端 find_window_buttons 的区域裁决——旧过滤只读**实体级** entry.area_id，
真机用户把区域挂在**设备**上，实体级恒 None → 「无区域放行」分支让全屋同名
窗全部进候选，async_all 迭代序（≈注册序）先到先得 → 开错房间还播「成功」。

本文件按 test_window_speed_behavior 的替身先例真 import、真执行、逐字节记账：
① 设备级区域（事故主形态）：只许压办公室那一扇，且与注册序无关；
② 区域只活在名字里（设备未挂区的现场常模）：名字回声明命中、别区名信号剔除；
③ 实体级区域（v1.0.69 旧形态）不回退；
④ 无任何区域证据的孤窗：兜底放行（修复不许把小家庭功能闸死）；
⑤ 口述区域解析不出且候选确凿在别区：如实失败零调用（绝不掷硬币）；
⑥ 泛称全窗（find_all_window_buttons_by_action）同受设备级区域裁决；
⑦ 百分比定位通道同样区域优先；
⑧ TurnDeviceOn 窗控转发的「摘区回捞」不再存在（与 ControlWindow 主路径同修）；
⑨ 执行层话术：集成如实失败句不再播英文残句（现场「（Could not find op」）。
"""
import asyncio
import os
import re
import sys
import tempfile
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_areafirst_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

import test_window_speed_behavior as bench  # noqa: E402  触发替身装载

wctl = bench.wctl
_State, _Entry = bench._State, bench._Entry


@pytest.fixture(autouse=True)
def _stubs_in_place():
    bench._install_ha_stubs()
    yield


# ── 替身注册表：这次把 async_list_areas 与设备级 area_id 补全（真机形态）──
class _Device:
    def __init__(self, name, area_id=None):
        self.name = name
        self.name_by_user = None
        self.area_id = area_id


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
    def __init__(self, mapping):          # {区域名: area_id}
        self._m = mapping

    def async_get_area_by_name(self, name):
        aid = self._m.get(name)
        return types.SimpleNamespace(id=aid, name=name, aliases=set()) if aid else None

    def async_list_areas(self):
        return [types.SimpleNamespace(id=i, name=n, aliases=set())
                for n, i in self._m.items()]


class _States:
    def __init__(self, states):
        self._states = states

    def async_all(self):
        return list(self._states)        # 迭代序=入表序（真机 async_all 亦非按区排序）

    def get(self, entity_id):
        for s in self._states:
            if s.entity_id == entity_id:
                return s
        return None


class _Services:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, service_data=None,
                         blocking=False, context=None, **kw):
        self.calls.append((domain, service, dict(service_data or {})))


def _mk_button(states, entries, dev_id, area_of_entity, fname, uid):
    eid = f"button.{uid}"
    states.append(_State(eid, name=fname))
    entries.append(_Entry(eid, "button", dev_id, uid, area_of_entity))
    return eid


def _build(devices):
    """devices: [(dev_id, dev_name, dev_area_id|None, [btn_fname...], ent_area|None)]
    入表序即 async_all 扫描序——用例按「展厅先注册」的现场事实排列设备，
    旧「先到先得」裁决必然压中展厅（事故复现），修复后必须按区域证据顶掉它。"""
    states, entries, devs = [], [], {}
    for dev_id, dev_name, dev_area, btns, ent_area in devices:
        devs[dev_id] = _Device(dev_name, dev_area)
        for j, fname in enumerate(btns):
            _mk_button(states, entries, dev_id, ent_area, fname, f"{dev_id}_{j}")
    h = types.SimpleNamespace()
    h._er = _ER(entries)
    h._dr = _DR(devs)
    h._ar = _AR({"办公室": "area_office", "展厅": "area_hall"})
    h.states = _States(states)
    h.services = _Services()
    return h


def _handle(hass, name, area, action=None, **extra):
    slots = {"target": {"value": [{"area": area,
                                   "devices": [{"name": name, "domains": []}]}]}}
    if action is not None:
        slots["action"] = {"value": action}
    for k, v in extra.items():
        slots[k] = {"value": v}
    ito = types.SimpleNamespace(hass=hass, context=None, slots=slots)
    return asyncio.run(wctl.ControlWindowIntent().async_handle(ito))


def _eids(calls):
    return sorted(c[2]["entity_id"] for c in calls)


# 事故主形态：区域挂在设备上，实体级 area_id 恒 None；按钮同名纯动作名
# （网关 has_entity_name=True 时 friendly_name=「设备名 开启」，两台设备各自
# 带区域前缀的场景）。展厅注册在前（order_flip 复刻 async_all 序）。
_INCIDENT = [
    ("dev_hall", "展厅平开窗", "area_hall",
     ["展厅平开窗 开启", "展厅平开窗 关闭"], None),
    ("dev_off", "办公室平开窗", "area_office",
     ["办公室平开窗 开启", "办公室平开窗 关闭"], None),
]


# ── ① 设备级区域（事故主钉）───────────────────────────────────
def test_incident_device_level_area_opens_office_only():
    hass = _build(_INCIDENT)
    res = _handle(hass, "平开窗", "办公室", "open")
    assert res["success"] is True, res
    assert _eids(hass.services.calls) == ["button.dev_off_0"], \
        "『打开办公室平开窗』只许压办公室那扇（展厅注册在前也必须让位）"


def test_incident_close_lane_hits_hall_only():
    hass = _build(_INCIDENT)
    res = _handle(hass, "平开窗", "展厅", "close")
    assert res["success"] is True, res
    assert _eids(hass.services.calls) == ["button.dev_hall_1"]


def test_area_absent_in_office_honest_fail_no_cross_area():
    """办公室没装推拉窗：说打开办公室推拉窗 → 如实失败，绝不跑去按展厅推拉窗。"""
    devs = [
        ("dev_hall", "展厅推拉窗", "area_hall", ["展厅推拉窗 开启"], None),
        ("dev_off", "办公室平开窗", "area_office", ["办公室平开窗 开启"], None),
    ]
    hass = _build(devs)
    res = _handle(hass, "推拉窗", "办公室", "open")
    assert res["success"] is False, res
    assert hass.services.calls == [], "跨区借用=本事故同族，摘区回捞在候选层也须失效"


# ── ② 区域只活在名字里（设备未挂区）───────────────────────────
def test_name_signal_disambiguates_unlabeled_devices():
    devs = [
        ("dev_hall", "hall-casement", None, ["展厅平开窗 开启", "展厅平开窗 关闭"], None),
        ("dev_off", "off-casement", None, ["办公室平开窗 开启", "办公室平开窗 关闭"], None),
    ]
    hass = _build(devs)
    res = _handle(hass, "平开窗", "办公室", "open")
    assert res["success"] is True, res
    assert _eids(hass.services.calls) == ["button.dev_off_0"], \
        "无区域元数据时名字里的注册区域名是强证据，别区名信号一律剔除"


def test_tier_shadowing_beats_scan_order():
    """无证据按钮注册在前、办公室实锤按钮在后：tier 必须顶掉先到先得。"""
    devs = [
        ("dev_x", "no-area-window", None, ["平开窗 开启"], None),      # tier2 兜底
        ("dev_off", "办公室平开窗", "area_office", ["办公室平开窗 开启"], None),  # tier0
    ]
    hass = _build(devs)                                               # 兜底者在前
    res = _handle(hass, "平开窗", "办公室", "open")
    assert res["success"] is True, res
    assert _eids(hass.services.calls) == ["button.dev_off_0"]


# ── ③ 实体级区域（v1.0.69 旧形态）不回退 ──────────────────────
def test_entity_level_area_regression():
    devs = [
        ("dev_hall", "展厅平开窗", None, ["平开窗 开启"], "area_hall"),
        ("dev_off", "办公室平开窗", None, ["平开窗 开启"], "area_office"),
    ]
    hass = _build(devs)
    res = _handle(hass, "平开窗", "办公室", "open")
    assert res["success"] is True, res
    assert _eids(hass.services.calls) == ["button.dev_off_0"]


# ── ④ 全屋孤窗零证据：兜底放行（不许把功能闸死）───────────────
def test_single_unlabeled_window_still_works():
    devs = [("dev_off", "开窗器01", None, ["平开窗 开启", "平开窗 关闭"], None)]
    hass = _build(devs)
    res = _handle(hass, "平开窗", "办公室", "open")
    assert res["success"] is True, res
    assert _eids(hass.services.calls) == ["button.dev_off_0"]


# ── ⑤ 口述区域解析不出 + 候选确凿在别区：如实失败零调用 ───────
def test_unresolvable_area_refuses_instead_of_coin_flip():
    hass = _build(_INCIDENT)
    res = _handle(hass, "平开窗", "书房", "open")   # 注册表没有「书房」
    assert res["success"] is False, res
    assert hass.services.calls == [], \
        "旧行为=不设区域过滤掷硬币开走别家窗；现在宁如实失败"


# ── ⑥ 泛称全窗同样吃设备级区域 ────────────────────────────────
def test_generic_all_windows_respects_device_level_area():
    devs = [
        ("dev_hall", "展厅平开窗", "area_hall", ["展厅平开窗 开启"], None),
        ("dev_off1", "办公室平开窗", "area_office", ["办公室平开窗 开启"], None),
        ("dev_off2", "办公室推拉窗", "area_office", ["办公室推拉窗 开启"], None),
    ]
    hass = _build(devs)
    res = _handle(hass, "窗户", "办公室", "open")
    assert res["success"] is True, res
    eids = _eids(hass.services.calls)
    assert eids == ["button.dev_off1_0", "button.dev_off2_0"], \
        "『打开办公室所有窗户』绝不连带展厅（旧版实体级 None 全放行）"


# ── ⑦ 百分比定位通道同样区域优先 ──────────────────────────────
def test_position_lane_device_level_area():
    states, entries, devs = [], [], {}
    for dev_id, dname, darea in (("dev_hall", "展厅平开窗", "area_hall"),
                                 ("dev_off", "办公室平开窗", "area_office")):
        devs[dev_id] = _Device(dname, darea)
        _mk_button(states, entries, dev_id, None, f"{dname} 开启", f"{dev_id}_0")
        ceid = f"cover.{dev_id}"
        states.append(_State(ceid, name=dname, attributes={"supported_features": 4}))
        entries.append(_Entry(ceid, "cover", dev_id, f"{dev_id}_cover", None))
    # 展厅先注册：旧「先到先得」必翻车的现场序
    hass = types.SimpleNamespace()
    hass._er = _ER(entries)
    hass._dr = _DR(devs)
    hass._ar = _AR({"办公室": "area_office", "展厅": "area_hall"})
    hass.states = _States(states)
    hass.services = _Services()
    res = _handle(hass, "平开窗", "办公室", position=50)
    assert res["success"] is True, res
    eids = _eids(hass.services.calls)
    assert eids == ["cover.dev_off"], "『办公室平开窗开到50%』只许动办公室的 cover"


# ── ⑧ TurnDeviceOn 窗控转发：摘区回捞连根拔（源码钉）──────────
def test_turn_lane_no_area_removal_refetch():
    src = (bench.CC_DIR / "intent_turn.py").read_text(encoding="utf-8")
    assert not re.search(r"find_window_buttons\([^)]*window_name,\s*None", src), \
        "窗控转发残留「摘掉区域重找」=跨区静默误执行同族回退"


# ── ⑨ 执行层话术：如实失败句不再播英文残句（现场日志钉）──────
def test_zh_error_maps_window_not_found():
    from core.executor import zh_error
    out = zh_error("Could not find open button for 平开窗 in 办公室")
    assert "Could not find" not in out, out
    assert "没找到要操作的窗户" in out, out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
