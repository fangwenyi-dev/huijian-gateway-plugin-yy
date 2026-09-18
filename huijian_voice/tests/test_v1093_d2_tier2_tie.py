# -*- coding: utf-8 -*-
"""v1.0.93 D2 逻辑半：点名区域 × tier2 并列 = 拒绝掷硬币（2026-09-18 真机实锤）。

现场链：「打开办公室射灯、空调、平开窗」→ 应答「都办妥了」，但取证发现
**三扇与办公室无归属登记的开窗器**（020a/0001/0259）被真打开（last_changed
与试验时刻吻合；当场已恢复）。根因=数据+逻辑双洞：
  数据半（D4）：现场开窗器/空调全部没设 area —— 用户/现场同事在 HA 补登记；
  逻辑半（本钉）：用户点名区域时，find_window_buttons 对**区域证据全无**的
    tier2 候选同 tier 先到先得（async_all 注册序掷硬币）——修=并列拒。

与 v1.0.71 契约④的调和（不许走另一个极端把功能闸死）：tier2 **全屋唯一**
候选仍兜底放行（小家庭单窗形态）；并列（≥2 个无证据候选争同一动作）才拒，
且如实报「没找到」——错误结果比拒答危险，v1.0.27/v1.0.55 同族纪律。
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_d2_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

import test_window_area_first as waf          # noqa: E402 复用事故台架
import test_window_speed_behavior as bench    # noqa: E402 替身装载

wctl = bench.wctl
_build = waf._build
_handle = waf._handle
_eids = waf._eids


@pytest.fixture(autouse=True)
def _stubs_in_place():
    bench._install_ha_stubs()
    yield


# ── 事故形态复刻：三扇无归属平开窗 + 点名「办公室」（注册表有此区）──────
_INCIDENT3 = [
    ("dev_a", "开窗器A", None, ["平开窗 开启", "平开窗 关闭"], None),
    ("dev_b", "开窗器B", None, ["平开窗 开启", "平开窗 关闭"], None),
    ("dev_c", "开窗器C", None, ["平开窗 开启", "平开窗 关闭"], None),
]


def test_named_area_with_rival_tier2_refuses_coin_flip():
    hass = _build(_INCIDENT3)
    res = _handle(hass, "平开窗", "办公室", "open")
    assert res["success"] is False, \
        "三个无证据候选并列=注册序掷硬币，2026-09-18 误伤本体，必须如实失败"
    assert hass.services.calls == [], "零设备调用（错误结果比拒答危险）"


def test_single_tier2_still_allowed_contract4_preserved():
    """v1.0.71 契约④不回退：全屋唯一无证据窗仍兜底放行（小家庭不许闸死）。"""
    devs = [("dev_off", "开窗器01", None, ["平开窗 开启", "平开窗 关闭"], None)]
    hass = _build(devs)
    res = _handle(hass, "平开窗", "办公室", "open")
    assert res["success"] is True, res
    assert _eids(hass.services.calls) == ["button.dev_off_0"]


def test_tier0_shadow_rivals_untouched():
    """办公室实锤在位时：其他无证据候选本来就靠边（既有 tier 裁决零回退）。"""
    devs = [
        ("dev_a", "开窗器A", None, ["平开窗 开启"], None),
        ("dev_b", "开窗器B", None, ["平开窗 开启"], None),
        ("dev_off", "办公室平开窗", "area_office", ["办公室平开窗 开启"], None),
    ]
    hass = _build(devs)
    res = _handle(hass, "平开窗", "办公室", "open")
    assert res["success"] is True, res
    assert _eids(hass.services.calls) == ["button.dev_off_0"]


def test_unnamed_area_multi_tier2_untouched():
    """没点名区域（area_norm 空）：旧兜底语义零改动（本闸只管点名句）。"""
    hass = _build(_INCIDENT3)
    res = _handle(hass, "平开窗", None, "open")
    assert res["success"] is True, res
    assert len(hass.services.calls) == 1


def test_generic_all_windows_refused_on_rival_loose():
    """泛称句「打开办公室的窗户」同闸：loose 桶同窗型并列 → 整批拒。"""
    hass = _build(_INCIDENT3)
    res = _handle(hass, "窗户", "办公室", "open")
    assert res["success"] is False, res
    assert hass.services.calls == [], "泛称一次并列放行=开三扇，比误伤更烈"


def test_generic_all_windows_unique_loose_allowed():
    """泛称 + 单一无证据窗：兜底保留（契约④的泛称同型）。"""
    devs = [("dev_x", "开窗器01", None, ["平开窗 开启"], None)]
    hass = _build(devs)
    res = _handle(hass, "窗户", "办公室", "open")
    assert res["success"] is True, res
    assert _eids(hass.services.calls) == ["button.dev_x_0"]


# ── 源码钉：三处闸都在位（并列判据不许被"一律放行/一律拒绝"回改）────────
def test_d2_gate_shape_pins():
    src = (bench.CC_DIR / "intent_window_const.py").read_text(encoding="utf-8")
    assert "t2_seen" in src and "D2 证据闸" in src
    assert "loose_type_hits" in src
    assert 'if area_id and not eff_area' in src
    seg = src[src.index("def find_window_buttons_by_area_id"):]
    assert "continue" in seg[seg.index("if area_id and not eff_area"):], \
        "by_area_id 无区放行回退"
