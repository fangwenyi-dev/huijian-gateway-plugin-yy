# -*- coding: utf-8 -*-
"""开窗器速度/力度参数语音回归钉（2026-09-12 现场主诉，网关 v1.4.3+ 配套）。

现场日志：「办公室平台窗速度设为百分之三十」被百分比预检当**开度**接管——
播报"已将办公室的平开窗开到30%"、窗位被改，而网关的开窗速度设定
（number 滑动条，unique_id 后缀 _speed/_strength，0-100%）纹丝不动。

本文件三层各钉各的：
① fast_path（真 runtime，零 HA 依赖）：窗类目标 + 参数词收尾 + 句尾数值
   → ControlWindow(speed|strength)，绝不产 position/action；「开窗器速度
   设为80」的连排假分裂豁免；真连排（句首另有子句动词）仍拒收——单发误
   执行"只关窗丢数值"比听不懂更糟；非窗类（风扇）/帘族逐字维持原车道；
② executor 话术兜底：message 缺失时播报也带数值与参数名；
③ 集成端源码契约钉（测试环境无 HA，沿用 test_window_position 文本钉先例）：
   speed/strength 槽位注册、按钮→同设备 number 实体按 unique_id 后缀寻径
   （真实注册表派生）、number.set_value 真服务名、缺实体如实报"没有速度
   设置 + 升级指引"、部分失败不折叠成全成功。
"""
import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_winspd_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

CC = HERE / "custom_components" / "huijian_ai"

from core.executor import Executor  # noqa: E402
from core.nlu.fast_path import FastPath, Plan, _is_param_single  # noqa: E402


class FakeScenes:
    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def refresh(self, force=False):
        pass

    def check(self, text):
        return None

    async def verify_or_refresh(self, phrase):
        return False


@pytest.fixture()
def settings():
    from core.settings import Settings
    return Settings(Path(os.environ["HUIJIAN_DATA"]) / f"winspd-{os.getpid()}.json")


@pytest.fixture()
def fp(settings):
    return FastPath(FakeScenes(), None, settings)


def _match(fp, text):
    return asyncio.run(fp.match(text))


# ── ① fast_path：参数句式（现场句式 + 常见语序/数值形态）──────────
SPEED_CASES = [
    # (句子, 参数键, 期望值, target 断言)
    ("办公室平台窗速度设为百分之三十", "speed", 30,
     lambda t: t[0]["area"] == "办公室" and t[0]["devices"][0]["name"] == "平开窗"),
    ("办公室平开窗速度设为百分之三十", "speed", 30,
     lambda t: t[0]["area"] == "办公室"),
    ("把书房平开窗的速度调到50%", "speed", 50,
     lambda t: t[0]["area"] == "书房" and t[0]["devices"][0]["name"] == "平开窗"),
    ("开窗器速度设为80", "speed", 80, lambda t: bool(t)),          # 连排假分裂豁免
    ("开窗器速度调到百分之八十", "speed", 80, lambda t: bool(t)),
    ("窗户速度30", "speed", 30, lambda t: bool(t)),                 # 裸数字也放行
    ("展厅推拉窗力度调成百分之七十", "strength", 70,
     lambda t: t[0]["area"] == "展厅" and t[0]["devices"][0]["name"] == "推拉窗"),
    ("三号窗速度设为100%", "speed", 100, lambda t: bool(t)),
]


@pytest.mark.parametrize(("sentence", "key", "val", "tgt_ok"), SPEED_CASES)
def test_speed_sentences(fp, sentence, key, val, tgt_ok):
    plan = _match(fp, sentence)
    assert plan is not None, f"未接管：{sentence}"
    assert plan.intent == "ControlWindow", f"{sentence} → {plan.intent}"
    assert plan.args.get(key) == val, f"{sentence} {key} 错：{plan.args}"
    # 参数令绝不与开度/动作混发（混发=窗位被误动，正是本次现场事故形状）
    assert "position" not in plan.args, f"{sentence} 误带 position"
    assert "action" not in plan.args, f"{sentence} 误带 action"
    tgt = plan.args.get("target") or []
    assert tgt and tgt_ok(tgt), f"{sentence} target 解析错：{tgt}"


# 反例：真连排/非窗类/帘族/其它属性必须维持拒收或原车道（零扰动）
NEGATIVE_CASES = [
    # 句首另有真子句动词＝连排语义，单发拒收（宁可如实听不懂，不误关窗丢数值）
    ("关闭办公室平开窗速度设为百分之三十", None),
    ("打开窗户速度设为百分之三十", None),
    # 非窗类/帘族不接管参数通道（行为与改动前一致）
    ("风扇速度调到30%", None),
    ("窗帘速度调到30%", None),
]


@pytest.mark.parametrize(("sentence", "_"), NEGATIVE_CASES)
def test_speed_negatives(fp, sentence, _):
    plan = _match(fp, sentence)
    if plan is not None:
        assert "speed" not in plan.args and "strength" not in plan.args, \
            f"{sentence} 被误判为开窗参数令"


# 既有开度/动作/属性车道零扰动（与 test_window_position 同句式抽查）
def test_position_lane_untouched(fp):
    plan = _match(fp, "开窗器开到50%")
    assert plan and plan.intent == "ControlWindow" and plan.args.get("position") == 50
    assert "speed" not in plan.args and "strength" not in plan.args
    plan = _match(fp, "办公室平开窗开到30%")
    assert plan and plan.args.get("position") == 30 and "speed" not in plan.args
    plan = _match(fp, "打开办公室平开窗")
    assert plan and plan.args.get("action") == "open" and "speed" not in plan.args
    plan = _match(fp, "亮度调到50")
    assert plan and plan.intent == "AdjustDeviceAttribute"


def test_param_single_guard_semantics():
    # 「开窗器」的 开窗 是设备名一部分 → 假分裂，豁免
    assert _is_param_single("开窗器速度设为80") is True
    # 「关闭 …」句首真动词 → 真连排语义，不豁免
    assert _is_param_single("关闭办公室平开窗速度设为百分之三十") is False
    # 无数值尾巴/非参数词 → 不豁免
    assert _is_param_single("关闭办公室平开窗") is False
    assert _is_param_single("风扇速度设为百分之三十") is False


# ── ② executor：话术兜底带数值与参数名 ───────────────────────────
def test_speed_speech_fallback():
    ex = Executor(ha=None)
    plan = Plan(intent="ControlWindow",
                args={"speed": 30, "target": [{"area": "办公室",
                                               "devices": [{"name": "平开窗"}]}]})
    reply = ex.speech(plan, {"control_targets": [{"name": "平开窗", "area": "办公室"}]})
    assert "速度" in reply and "30%" in reply and "平开窗" in reply, reply
    plan = Plan(intent="ControlWindow",
                args={"strength": 70, "target": [{"area": "展厅",
                                                  "devices": [{"name": "推拉窗"}]}]})
    reply = ex.speech(plan, {"control_targets": [{"name": "推拉窗", "area": "展厅"}]})
    assert "力度" in reply and "70%" in reply, reply


# ── ③ 集成端源码契约钉 ─────────────────────────────────────────
def _win_ctl_src():
    return (CC / "intent_window_control.py").read_text(encoding="utf-8")


def _win_const_src():
    return (CC / "intent_window_const.py").read_text(encoding="utf-8")


def test_slot_schema_declares_speed_and_strength():
    src = _win_ctl_src()
    assert 'vol.Optional("speed")' in src, "speed 槽位未注册"
    assert 'vol.Optional("strength")' in src, "strength 槽位未注册"
    assert "_apply_window_param" in src, "handler 未消费参数槽位"
    # 裁决位次：参数分发必须与 position 同在、赶在全窗兜底之前
    # （2026-09-21 事故修复后，兜底入口=泛称闸 is_generic_window_name，标记随迁）
    i_pos = src.index('slots.get("position")')
    i_par = src.index('for _param in ("speed", "strength")')
    i_fb = src.index("if is_generic_window_name(device_name):")
    assert i_pos < i_par < i_fb, "参数槽裁决位次错（会被全窗兜底吞掉）"


def test_number_service_symbol_is_real():
    """number.set_value 走 intent_adjust_attribute 已实锤的同款引用形制
    （number.const.SERVICE_SET_VALUE 真机存在；禁止臆造 set/adjust_value）。"""
    src = _win_ctl_src()
    assert "number.const.SERVICE_SET_VALUE" in src
    assert "number.DOMAIN" in src
    assert '"value": value' in src, "number.set_value 的数据键必须是 value"
    for fake in ('"number.set"', "number.set(", "set_speed_value"):
        assert fake not in src, f"假服务/键名混入：{fake}"
    # 服务名不得从 components.number.const 导入（该路径只核验过 DOMAIN 通吃）
    for line in src.splitlines():
        if "components.number.const import" in line:
            pytest.fail(f"禁从 number.const 导入符号（同 lock.const 事故族）：{line}")


def test_param_resolution_rides_button_system():
    src = _win_ctl_src()
    assert "find_param_numbers_for_buttons" in src, "未按按钮→同设备 number 寻径"
    assert "_resolve_window_button_ids" in src, "参数通道必须与开度共用窗类寻径"
    i = src.index("async def _apply_window_param")
    body = src[i:i + 4000]
    # 缺实体如实报失败 + 升级指引，绝不含糊成功
    assert "没有" in body and "v1.4.3" in body, "缺速度/力度实体的诚实失败话术缺失"
    assert '"success": False' in body
    # 部分失败不折叠成全成功
    assert "bad_msgs" in body and "未成功" in body
    assert "已将{label}{cn}设为{value}%" in body, "全成功话术须带区域+参数名+数值"


def test_position_lane_shares_resolved_helpers():
    """重构零行为改动：position 通道仍在原处核 SET_POSITION、成败分收。"""
    src = _win_ctl_src()
    i = src.index("async def _apply_window_position")
    body = src[i:i + 6000]
    assert "_resolve_window_button_ids" in body and "find_covers_for_buttons" in body
    assert "ATTR_SUPPORTED_FEATURES" in body and "SET_POSITION" in body
    assert "for dev_name, cover_entity_id in covers" in body
    assert 'SERVICE_SET_COVER_POSITION' in body


def test_find_param_numbers_derives_from_registries():
    src = _win_const_src()
    i = src.index("def find_param_numbers_for_buttons")
    body = src[i:]
    assert "entry.device_id" in body, "必须走 entity_registry→device_id"
    assert re.search(r"er\.async_entries_for_device\(\s*entity_registry", body), \
        "必须用模块级 er.async_entries_for_device(registry, device_id) 形制"
    assert 'num_entry.domain != "number"' in body, "必须钉 number 域"
    assert "endswith(suffix)" in body, "必须按 unique_id 后缀（_speed/_strength）认实体"
    assert '"speed": "_speed"' in src and '"strength": "_strength"', \
        "后缀表必须与网关 number.py 的 unique_id 约定逐字一致"
    assert "seen_devices" in body, "同设备多按钮必须去重"


def test_llm_channels_expose_param_slots():
    """LLM 两通道（加载项 agent / 集成 custom_llm_api）同步参数槽——
    本地车道听不懂的变体句（配了 LLM 时）才有正确出口，且 action 不再
    强制（纯参数令无 action）。"""
    agent_src = (HERE / "core" / "agent.py").read_text(encoding="utf-8")
    i = agent_src.index('"ControlWindow"')
    body = agent_src[i:i + 1000]
    assert '"speed"' in body and '"strength"' in body
    llm_src = (CC / "custom_llm_api.py").read_text(encoding="utf-8")
    i = llm_src.index('"ControlWindow"')
    body = llm_src[i:i + 1200]
    assert 'vol.Optional("speed")' in body and 'vol.Optional("strength")' in body
    assert 'vol.Required("action")' not in body, "action 仍强制会逼 LLM 给纯参数令编造动作"
