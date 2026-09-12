# -*- coding: utf-8 -*-
"""窗控「误开全窗」守卫行为钉（用户 2026-09-21 报障复盘转正）。

事故形态：一句「打开展厅的内开窗」把展厅内开窗+推拉窗**同时**打开。
机制：当年"内开窗"不在窗型映射表 → extract_window_name 返回 None →
集成把"解析失败的具体名"误当"全窗泛称"升级执行。名识别本体已在 v1.0.5x
系列修复，本文件钉的是**放大器本身**（复现台架复用
test_window_speed_behavior 的 HA 替身，真 import 真执行真记账）：
  ① 具名精确 → 恰好压中那一扇，别扇零调用；
  ② 具体名解析失败（垃圾名）→ 如实失败，**不得**升级全窗；
  ③ 显式泛称（所有窗户/裸窗户/空名+区域）→ 全窗语义保持（不误伤功能）；
  ④ 本区域没有该窗型 → 不得跨区去开别屋同型窗（摘区回捞删除的回归钉）；
  ⑤ 位置/速度参数通道同闸（'未装窗型 开到50%' 不得动全区域 cover）。
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_ovr_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

import test_window_speed_behavior as bench  # noqa: E402  触发替身+wctl 装载

wctl = bench.wctl


@pytest.fixture(autouse=True)
def _stubs_in_place():
    bench._install_ha_stubs()   # 不赌别的测试文件的重装顺序（同 speed_behavior 口径）
    yield


# ── fake hass：展厅=内开窗+推拉窗（带 cover），客厅=推拉窗，卧室=内开窗 ──
AREA_OF = {"展厅内开窗": "area_展厅", "展厅推拉窗": "area_展厅",
           "客厅推拉窗": "area_客厅", "卧室内开窗": "area_卧室"}
COVERED = {"展厅内开窗", "卧室内开窗"}   # 这两台有同设备 cover（带 SET_POSITION）


def _hass():
    State, Entry = bench._State, bench._Entry
    states, entries, devices = [], [], {}
    for i, dname in enumerate(AREA_OF):
        dev_id = f"dev{i}"   # 真 HA device_id 是 UUID 串——int 0 会踩产品代码
                             # `not entry.device_id` 空值闸，替身必须同形
        area = AREA_OF[dname]
        devices[dev_id] = bench._Device(dname)
        for act in ("开启", "关闭"):
            eid = f"button.{dname}_{act}"
            states.append(State(eid, name=f"{dname} {act}"))
            entries.append(Entry(eid, "button", dev_id, f"GW_SN{dev_id}_{act}", area))
        if dname in COVERED:
            ceid = f"cover.{dname}"
            states.append(State(ceid, name=f"{dname}",
                                attributes={"supported_features": 4}))
            entries.append(Entry(ceid, "cover", dev_id, f"GW_SN{dev_id}_cover", area))
    h = types.SimpleNamespace()
    h._er = bench._ER(entries)
    h._dr = bench._DR(devices)
    h._ar = bench._AR()
    h.states = bench._States(states)
    h.services = bench._Services()
    return h


def _handle(name, area, action=None, **extra):
    slots = {"target": {"value": [{"area": area,
                                  "devices": [{"name": name, "domains": []}]}]}}
    if action is not None:
        slots["action"] = {"value": action}
    for k, v in extra.items():
        slots[k] = {"value": v}
    hass = _hass()
    ito = types.SimpleNamespace(hass=hass, context=None, slots=slots)
    res = asyncio.run(wctl.ControlWindowIntent().async_handle(ito))
    return res, hass.services.calls


def _eids(calls):
    return sorted(c[2]["entity_id"] for c in calls)


# ── ① 具名精确 ────────────────────────────────────────────────
def test_specific_window_presses_only_that_window():
    res, calls = _handle("内开窗", "展厅", "open")
    assert res["success"] is True, res
    assert _eids(calls) == ["button.展厅内开窗_开启"], \
        "『内开窗』只许压内开窗那一扇，推拉窗零调用（用户事故主钉）"


def test_specific_window_close_lane():
    res, calls = _handle("推拉窗", "展厅", "close")
    assert res["success"] is True, res
    assert _eids(calls) == ["button.展厅推拉窗_关闭"]


# ── ② 垃圾具体名：如实失败，绝不升级全窗 ─────────────────────
def test_unrecognized_specific_name_refuses_all_windows():
    res, calls = _handle("内开", "展厅", "open")   # 丢字形——当年事故入参
    assert res["success"] is False, res
    assert calls == [], "解析失败的具体名被升级成全窗=事故复现"
    assert "不敢按全窗" in res["error"]


def test_foreign_junk_name_refuses():
    res, calls = _handle("kitchen-hood", "展厅", "open")
    assert res["success"] is False and calls == []


# ── ③ 显式泛称全窗语义保持（修复不许把功能一起闸死）──────────
@pytest.mark.parametrize("generic", ["所有窗户", "全部窗", "窗户", "窗"])
def test_generic_names_still_open_all_in_area(generic):
    res, calls = _handle(generic, "展厅", "open")
    assert res["success"] is True, res
    assert _eids(calls) == ["button.展厅内开窗_开启", "button.展厅推拉窗_开启"]


def test_empty_name_with_area_opens_all():
    res, calls = _handle("", "展厅", "open")
    assert res["success"] is True, res
    assert len(calls) == 2


# ── ④ 区域硬约束：本区没有该窗型不得跨区借用 ─────────────────
def test_area_absent_type_does_not_cross_area():
    # 客厅只有推拉窗；说"打开内开窗"→ 失败，绝不跑卧室去按卧室内开窗
    res, calls = _handle("内开窗", "客厅", "open")
    assert res["success"] is False, res
    assert calls == [], "摘区回捞=跨区静默误执行（v1.0.42 R3 同族）"


# ── ⑤ 位置/参数通道同闸 ──────────────────────────────────────
def test_position_specific_absent_type_honest_fail():
    # 展厅没装"上悬窗"：旧 `or not button_ids` 会升级成全窗——把展厅内开窗
    # 的 cover 拉到 50%（用户只提了上悬窗！）。fake 数据特意让展厅内开窗
    # 带 SET_POSITION cover，旧行为必然留下 set_cover_position 记账。
    res, calls = _handle("上悬窗", "展厅", position=50)
    assert res["success"] is False, res
    assert calls == [], "具名未装窗型的位置命令绝不许动本区域其它窗的 cover"


def test_position_junk_name_honest_fail():
    res, calls = _handle("内开", "展厅", position=50)
    assert res["success"] is False and calls == []


def test_position_generic_still_moves_all_covers():
    res, calls = _handle("窗户", "展厅", position=50)
    assert res["success"] is True, res
    assert _eids(calls) == ["cover.展厅内开窗"], \
        "展厅只有内开窗带 cover 实体；泛称路径必须仍然工作（不误伤功能）"


# ── ⑥ 单一事实源 + 结构钉（防未来重构悄悄拆闸）───────────────
def test_generic_gate_single_source_of_truth():
    src_ctl = (bench.CC_DIR / "intent_window_control.py").read_text(encoding="utf-8")
    src_const = (bench.CC_DIR / "intent_window_const.py").read_text(encoding="utf-8")
    assert "GENERIC_WINDOW_NAMES" in src_const
    assert "def is_generic_window_name" in src_const
    # extract 与泛称闸共读同一张表（不得两处各抄一份清单）
    assert "for gn in GENERIC_WINDOW_NAMES" in src_const
    # 主路径与 resolver 双入口都过闸
    assert src_ctl.count("is_generic_window_name(") >= 3, "两个升级入口+import"
    # 旧危险形已连根拔：升级条件不再含 not button_ids；摘区回捞不再存在
    assert "generic_all or not button_ids" not in src_ctl
    assert "Found buttons (without area filter)" not in src_ctl


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
