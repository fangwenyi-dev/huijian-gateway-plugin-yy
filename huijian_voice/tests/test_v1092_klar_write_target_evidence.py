# -*- coding: utf-8 -*-
"""v1.0.92 klar 控制步「目标证据」闸钉（2026-09-17 真机实锤）。

现场（加载项 1.0.91 容器日志原样）：
  17:35:09,778 [执行] HassLightSet {'area':'ban_gong_shi','domain':'light',
             'brightness':'1'} → 成功 | 办公室 1%
  17:35:09,779 [级联] '给我讲一个三百字左右的睡前故事' → [klar] '办公室 1%'
  17:48:13,349 [级联] '内会议告知内阁选体投僚提消辞访日本自民党总裁高示扫描
             十六号调整自民党' → [klar] '办公室 16%'（电视声真调光）
  18:11:26,544 [级联] '另外一公也' → [klar] '办公室 1%'（TV 残句又调光）
= klar draft.rs 回放兜底（未知目标+任意数字→硬套上一个可见灯）的残余族：
v1.0.55 窗闸词表只护「窗/速度」语义，纯闲聊/电视噪声句畅通无阻真执行。
真机 light.ban_gong_shi_she_deng br=3（1%）last_changed 与上述时刻吻合，
两次由测试者手工恢复 254。

修法：core/pipeline._klar_write_without_target_evidence——grounded 控制步的
原话必须含目标证据（目标域设备词/已知区域名/回指代词），数字不算证据。
主裁决与**降级通道**同闸（v1.0.90 假成功案的教训：只闸主路=半道闸）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.nlu.fast_path import Plan                                   # noqa: E402
from core.pipeline import (select_primary_plan,                       # noqa: E402
                           select_fallback_plan)

AREAS = {"办公室", "客厅", "书房", "展厅"}


def _kl(intent, utterance, args=None):
    return Plan(intent=intent, args=args if args is not None
                else {"entity_id": "light.ban_gong_shi_she_deng",
                      "brightness": "1"},
                source="klar", utterance=utterance)


# ── ① 三起现场事故原句必须 veto ─────────────────────────────────
def test_field_story_sentence_no_longer_dims_light():
    kl = _kl("HassLightSet", "给我讲一个三百字左右的睡前故事")
    assert select_primary_plan(None, kl, AREAS) is None


def test_field_tv_garbage_narration_vetoed():
    kl = _kl("HassLightSet",
             "内会议告知内阁选体投僚提消辞访日本自民党总裁高示扫描十六号调整自民党",
             {"area": "ban_gong_shi", "domain": "light", "brightness": "16"})
    assert select_primary_plan(None, kl, AREAS) is None


def test_field_tv_fragment_vetoed():
    kl = _kl("HassLightSet", "另外一公也")
    assert select_primary_plan(None, kl, AREAS) is None


# ── ② 合法控制句零误伤 ──────────────────────────────────────────
def test_legit_control_sentences_pass():
    L = {"entity_id": "light.ban_gong_shi_she_deng", "brightness": "30"}
    for utt in ("把办公室的射灯亮度调到百分之三十",   # 设备词 亮/射灯
                "开灯", "关灯",                       # 灯
                "办公室灯调暗一点",                   # 暗
                "色温调暖",                           # 色温/暖
                "客厅的灯打开",                       # 区域+灯
                "把氛围拉满",                         # 氛围
                "办公室的"):                          # 纯区域名（fan-out 自有闸管）
        assert select_primary_plan(None, _kl("HassTurnOn", utt), AREAS) is not None, utt


def test_anaphora_context_follows_pass():
    for utt in ("把它关了", "那个也打开", "再亮一点"):
        assert select_primary_plan(None, _kl("HassTurnOff", utt), AREAS) is not None, utt


def test_other_domains_evidence():
    assert select_primary_plan(None, _kl("HassTurnOn", "打开客厅空调",
                                         {"entity_id": "climate.kt"}), AREAS)
    assert select_primary_plan(None, _kl("HassTurnOff", "关掉扫地机",
                                         {"entity_id": "vacuum.sm"}), AREAS)
    assert select_primary_plan(None, _kl("HassToggle", "打开纱窗",
                                         {"entity_id": "cover.sw"}), AREAS)
    # 闲聊指向未知域（词表外）→ 不管（fail-open）
    assert select_primary_plan(None, _kl("HassTurnOn", "给我讲个笑话",
                                         {"entity_id": "water_heater.w"}), AREAS)


# ── ③ fail-open 边界 ────────────────────────────────────────────
def test_failopen_shapes():
    # 未 grounded（无 entity_id/domain）
    assert select_primary_plan(None, _kl("HassLightSet", "讲个故事",
                                         {"brightness": "1"}), AREAS)
    # utterance 缺失（链/回放轮）
    assert select_primary_plan(None, _kl("HassLightSet", None), AREAS)
    # 无区域名可用（known_areas 空）→ 仍靠设备词判据：闲聊句照拦
    assert select_primary_plan(None, _kl("HassLightSet", "给我讲个故事"), None) is None
    # 老调用形态（两参）照常工作
    kl = _kl("HassLightSet", "给我讲个故事")
    assert select_primary_plan(None, kl) is None
    # 慧尖字面表（fp 路）完全不受本闸影响
    fp = Plan(intent="TurnDeviceOn", args={"target": []}, source="t0",
              utterance="开灯")
    assert select_primary_plan(fp, None) is fp


# ── ④ 降级通道同闸（v1.0.90 教训：主路拦了降级不许绕） ─────────
def test_fallback_channel_gated_too():
    fp = Plan(intent="TurnDeviceOn", args={"target": [
        {"area": "办公室", "devices": [{"name": "射灯", "domains": ["light"]}]}]},
        source="t0", utterance="打开办公室的射灯")
    kl = _kl("HassLightSet", "另外一公也")
    fb = select_fallback_plan(fp, fp, kl, "集成未响应", AREAS)
    assert fb is None, "降级到无证据 klar 步必须拒"
    # 有证据的降级照常放行
    kl_ok = _kl("HassLightSet", "把灯调到30%")
    assert select_fallback_plan(fp, fp, kl_ok, "集成未响应", AREAS) is kl_ok


# ── ⑤ 数字是诱饵不是证据（draft.rs 回放形态钉） ────────────────
def test_numbers_are_not_evidence():
    for utt in ("百分之三十", "30", "调成26", "扫描十六号"):
        assert select_primary_plan(None, _kl("HassLightSet", utt), AREAS) is None, utt
