# -*- coding: utf-8 -*-
"""v1.1.15 klar「写值必须有属性字面证据」闸钉（2026-09-27 办公 .91 真机实锤）。

现场（板 32b8 / fw 2.1.68 / 加载项 1.1.14 / SenseVoice，同一喇叭同一位置 0dB）：
    10:19:16,466 [执行] HassLightSet {'entity_id': 'light.ban_gong_shi_she_deng',
                'brightness': '100'}(+1步) → 成功 | 办公室 100% 办公室关了。
    10:19:16,466 [级联] '家财百万打开办公室射灯和关闭办公室灯器皿茶叶等等日用都是等'
设备侧那一轮 `sess=8416ms`（5.28s 的 wav 被端点续听拖长），SenseVoice 前后各缀了一段
幻听词。目标证据闸（v1.0.92）放行了——用户确实说了"射灯/灯"，目标有证据；但 klar
draft.rs 从幻听里挑了「百万」当数值，**真把亮度写成了 100**。

这与 v1.0.92 钉的「给我讲一个三百字左右的睡前故事」→亮度 1% 是同一族：目标有证据
≠"这句在要求这个属性"。故再加一道独立判据：**值型意图**（HassLightSet /
HassSetPosition / HassClimateSetTemperature 且真带值参数）必须在全句里回捞出属性字
（亮/暗/度/百分/光/温度…），捞不到整条弃用、落回降级链。误拦代价=一句「我还不会」，
误执行代价=真实世界把灯/窗/空调调到用户没要的档位。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.nlu.fast_path import Plan                                   # noqa: E402
from core.pipeline import (select_primary_plan, select_fallback_plan,   # noqa: E402
                           _klar_value_without_attr_evidence)

AREAS = {"办公室", "客厅", "书房", "展厅"}


def _kl(intent, utterance, args):
    return Plan(intent=intent, args=args, source="klar", utterance=utterance)


def _light(utterance, val="100"):
    return _kl("HassLightSet", utterance,
               {"entity_id": "light.ban_gong_shi_she_deng", "brightness": val})


# ── ① 现场实锤句必须 veto ────────────────────────────────────────
def test_field_hallucination_cannot_write_brightness():
    kl = _light("家财百万打开办公室射灯和关闭办公室灯器皿茶叶等等日用都是等")
    assert _klar_value_without_attr_evidence(kl) is True
    assert select_primary_plan(None, kl, AREAS) is None


def test_same_family_story_sentence_still_vetoed():
    """v1.0.92 那三起案子的原句在新闸下同样 veto（两闸叠住，不是替换）。"""
    for t in ("给我讲一个三百字左右的睡前故事",
              "内会议告知内阁选体投僚提消辞访日本自民党总裁高示扫描十六号调整自民党",
              "另外一公也"):
        assert _klar_value_without_attr_evidence(_light(t, "1")) is True, t


# ── ② 合法指令不得误伤 ──────────────────────────────────────────
def test_legit_brightness_commands_pass():
    for t in ("亮度调到一百", "办公室射灯亮度调到百分之百", "把射灯调成中性光",
              "灯调暗一点", "亮度调到百分之五十", "射灯最亮"):
        assert _klar_value_without_attr_evidence(_kl("HassLightSet", t,
                                                     {"brightness": "100"})) is False, t


def test_position_and_temperature_evidence():
    assert _klar_value_without_attr_evidence(
        _kl("HassSetPosition", "窗帘开到百分之六十", {"position": "60"})) is False
    assert _klar_value_without_attr_evidence(
        _kl("HassClimateSetTemperature", "空调调到二十六度", {"temperature": "26"})) is False
    # 无属性字：空调句里没出现"度/温度/冷/暖/热"⇒ 值来路不明
    assert _klar_value_without_attr_evidence(
        _kl("HassClimateSetTemperature", "把空调26", {"temperature": "26"})) is True


# ── ③ 边界：不判的一律放行（fail-open）──────────────────────────
def test_non_value_intents_untouched():
    """只开关不带值的意图完全不归本闸管（开关族由目标证据闸护）。"""
    assert _klar_value_without_attr_evidence(
        _kl("HassTurnOn", "打开办公室射灯", {"entity_id": "light.she_deng"})) is False
    # HassLightSet 但没带值参数（纯色域切换之类）→ 不判
    assert _klar_value_without_attr_evidence(
        _kl("HassLightSet", "随便一句没有属性字样", {"entity_id": "light.x"})) is False


def test_empty_utterance_fails_open():
    """合成/回放轮无原话：不得凭空 veto（与目标证据闸同口径）。"""
    assert _klar_value_without_attr_evidence(_light("", "100")) is False


def test_broken_input_never_raises():
    assert _klar_value_without_attr_evidence(None) is False
    assert _klar_value_without_attr_evidence(
        Plan(intent="HassLightSet", args=None, source="klar", utterance=None)) is False


# ── ④ 双通道同闸（v1.0.90 假成功案的教训：只闸主路＝半道闸）──────
def test_fallback_lane_uses_the_same_gate():
    """降级通道里出现的值型无证据 klar 也必须被弃用。"""
    kl = _kl("HassLightSet", "家财百万打开办公室射灯器皿茶叶等等",
             {"area": "办公室", "domain": "light", "brightness": "100"})
    prim = Plan(intent="TurnDeviceOn", args={"target": []}, source="t0",
                utterance="打开办公室射灯")
    out = select_fallback_plan(prim, None, kl, "打开办公室射灯", AREAS)
    assert out is None, out


def test_both_lanes_wired_source_pin():
    """结构钉：主裁决与降级通道**两处**都得挂本闸，少一处即红。"""
    src = (ROOT / "core" / "pipeline.py").read_text(encoding="utf-8")
    assert src.count("_klar_value_without_attr_evidence(kl)") == 2, \
        "两通道同闸是本钉的不变量，调用点数变了必须一起改测试"
