# -*- coding: utf-8 -*-
"""开窗器百分比开度语音定位回归钉（2026-09-18 主诉修复）。

病灶（诊断实锤）：「开窗器」以动作词「开窗」起头，fast_path 的
`^开窗(?!帘)` 在位置规则之前命中 ControlWindow(open)——
「开窗器开到50%」百分比静默丢、按钮全开（假动作成功）；
「将展厅推拉窗打开50%」「打开展厅推拉窗50%」两种语序锚定动作表
够不到；位置形态不认「百分之五十/五十/一半」。

本文件三层各钉各的：
① fast_path 预检（真 runtime，零 HA 依赖）：正例产 ControlWindow+
   position，反例逐字维持改动前行为（帘族走 Adjust、无数字句不动）；
② executor klar 直调表：服务名必须是真实存在的 cover.set_cover_position
   （旧值 "set_position" 真机必 "Service not found"——正是铁律点名的
   "HA 符号缺陷字符串钉拦不住"类，这里直接钉值）；
③ 集成端 ControlWindow position 通道：源码契约钉（测试环境无 HA，
   沿用 test_intent_contract/test_config_flow 的文本钉先例）——
   服务名从 homeassistant.const 导入、按钮→同设备 cover 寻径、
   SET_POSITION 能力位逐台裁决、部分失败不折叠成全成功。
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
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_winpos_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

CC = HERE / "custom_components" / "huijian_ai"

from core.executor import Executor  # noqa: E402
from core.nlu.fast_path import FastPath, Plan, _parse_position  # noqa: E402


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
    return Settings(Path(os.environ["HUIJIAN_DATA"]) / f"winpos-{os.getpid()}.json")


@pytest.fixture()
def fp(settings):
    # textcnn=None：预检发生在 T1 之前，本文件不测 T1
    return FastPath(FakeScenes(), None, settings)


def _match(fp, text):
    return asyncio.run(fp.match(text))


# ── ① fast_path：用户拍板句式 + 常见百分比形态 ──────────────────────
POSITION_CASES = [
    # (句子, 期望 position, 期望 target 断言)
    ("将展厅推拉窗打开50%", 50, lambda t: t[0]["area"] == "展厅"
        and t[0]["devices"][0]["name"] == "推拉窗"),
    ("打开展厅推拉窗50%", 50, lambda t: t[0]["area"] == "展厅"
        and t[0]["devices"][0]["name"] == "推拉窗"),
    ("展厅推拉窗打开50%", 50, lambda t: t[0]["area"] == "展厅"),
    ("开窗器开到50%", 50, lambda t: bool(t)),
    ("开窗器开到百分之五十", 50, lambda t: bool(t)),
    ("展厅的开窗器打开一半", 50, lambda t: t[0]["area"] == "展厅"),
    ("推拉窗关到30", 30, lambda t: bool(t)),
    ("窗户开一半", 50, lambda t: bool(t)),
    ("把书房平开窗开到80", 80, lambda t: t[0]["area"] == "书房"
        and t[0]["devices"][0]["name"] == "平开窗"),
    ("内开窗打开百分之三十", 30, lambda t: bool(t)),
    ("展厅所有窗户打开50%", 50, lambda t: t[0]["area"] == "展厅"),
    ("把推拉窗调到70", 70, lambda t: bool(t)),          # 裸数字+到类动词
]


@pytest.mark.parametrize(("sentence", "pos", "tgt_ok"), POSITION_CASES)
def test_position_sentences(fp, sentence, pos, tgt_ok):
    plan = _match(fp, sentence)
    assert plan is not None, f"未接管：{sentence}"
    assert plan.intent == "ControlWindow", f"{sentence} → {plan.intent}"
    assert plan.args.get("position") == pos, f"{sentence} position 错"
    assert "action" not in plan.args, f"{sentence} 不该带 action"
    tgt = plan.args.get("target") or []
    assert tgt and tgt_ok(tgt), f"{sentence} target 解析错：{tgt}"


# 反例：无数字句/帘族/其它属性必须逐字维持原车道（改动零扰动）
NEGATIVE_CASES = [
    ("打开窗户", "ControlWindow", None),
    ("关闭展厅推拉窗", "ControlWindow", None),
    ("内倒展厅窗户", "ControlWindow", None),
    ("暂停窗户动作", "ControlWindow", None),
    ("窗帘开到50", "AdjustDeviceAttribute", None),
    ("客厅窗帘开到50%", "AdjustDeviceAttribute", None),
    ("纱窗开到50%", "AdjustDeviceAttribute", None),
    ("亮度调到50", "AdjustDeviceAttribute", None),
    ("推拉窗开大一点", "ControlWindow", None),
    ("打开50", "TurnDeviceOn", None),
    ("关灯", "TurnDeviceOff", None),
]


@pytest.mark.parametrize(("sentence", "intent", "_"), NEGATIVE_CASES)
def test_position_gate_keeps_existing_lanes(fp, sentence, intent, _):
    plan = _match(fp, sentence)
    assert plan is not None and plan.intent == intent, \
        f"{sentence} → {plan.intent if plan else None}（应为 {intent}）"
    assert plan.args.get("position") is None, f"{sentence} 被误判为百分比"


def test_position_sentences_have_no_fabricated_action(fp):
    """百分比句绝不带 action=open——旧雷正是「丢数值+按全开按钮」假动作。"""
    for s, pos, _ in POSITION_CASES:
        plan = _match(fp, s)
        assert plan is not None and plan.args.get("action") is None, s
        assert plan.args["position"] == pos, s


# ── ①b 数值解析护栏 ─────────────────────────────────────────────
def test_parse_position_bare_number_needs_verb():
    assert _parse_position("50", True) == 50
    assert _parse_position("50", False) is None       # 「窗户50」≠百分比令
    assert _parse_position("50%", False) == 50
    assert _parse_position("一半", False) == 50
    assert _parse_position("百分之五十", True) == 50
    assert _parse_position("150", True) is None       # 越界拒接
    assert _parse_position("二十五", True) == 25


# ── ② executor：klar 直调服务名钉（真 HA 符号）───────────────────
def test_klar_set_position_service_is_real():
    ex = Executor(ha=None)
    got = ex._klar_direct("HassSetPosition",
                          {"entity_id": "cover.a", "position": 50})
    assert got == ("cover", "set_cover_position",
                   {"entity_id": "cover.a", "position": 50}), \
        "cover 域无 set_position 服务（真机必 not found），正名见 HA 文档"


def test_klar_service_table_names_are_valid():
    """_KLAR_SERVICE 全部 domain/service 与 HA core 实源核对表。

    homeassistant 不在测试环境依赖面，用手工核对的白名单钉死（每次改动
    _KLAR_SERVICE 都必须同步核 HA 源码后更新此表——漂移即红，逼迫核验）。
    """
    known_good = {
        ("homeassistant", "turn_on"), ("homeassistant", "turn_off"),
        ("homeassistant", "toggle"),
        ("lock", "lock"), ("lock", "unlock"),
        ("climate", "set_temperature"),
        ("humidifier", "set_humidity"),
        ("cover", "set_cover_position"), ("cover", "open_cover"),
        ("cover", "close_cover"), ("cover", "stop_cover"),
        ("fan", "set_percentage"), ("fan", "set_preset_mode"),
        ("vacuum", "start"), ("vacuum", "pause"), ("vacuum", "return_to_base"),
        ("light", "turn_on"),
    }
    for intent_name, (dom, svc) in Executor._KLAR_SERVICE.items():
        assert (dom, svc) in known_good, \
            f"{intent_name} → {dom}.{svc} 不在 HA 真实服务核对表内"


def test_control_window_position_speech_fallback():
    """兜底话术带数值（集成端正常走中文 message 直返，此处防 message 缺失）。"""
    ex = Executor(ha=None)
    plan = Plan(intent="ControlWindow",
                args={"position": 50, "target": [{"area": "展厅",
                                                  "devices": [{"name": "推拉窗"}]}]})
    reply = ex.speech(plan, {"control_targets": [{"name": "推拉窗", "area": "展厅"}]})
    assert "50%" in reply and "推拉窗" in reply, reply


# ── ③ 集成端源码契约钉 ─────────────────────────────────────────
def _win_ctl_src():
    return (CC / "intent_window_control.py").read_text(encoding="utf-8")


def _win_const_src():
    return (CC / "intent_window_const.py").read_text(encoding="utf-8")


def test_slot_schema_declares_position():
    src = _win_ctl_src()
    assert 'vol.Optional("position")' in src, "position 槽位未注册"
    assert "pos_raw" in src, "handler 未消费 position 槽位"


def test_cover_service_from_homeassistant_const():
    """服务名/属性名走已核验导入路径（components.lock.const 事故同族防线）：
    SERVICE_SET_COVER_POSITION 在 homeassistant.const；
    components.cover.const 只准导入 DOMAIN（本文件甚至不走 .const）。"""
    src = _win_ctl_src()
    assert "from homeassistant.const import" in src
    assert "SERVICE_SET_COVER_POSITION" in src
    for line in src.splitlines():
        if "components.cover.const import" in line:
            assert "SERVICE_" not in line, f"cover.const 无 SERVICE_*：{line}"
    assert "set_position\"" not in src and '"set_position"' not in src, \
        "禁止假服务名 set_position"


def test_position_resolution_rides_button_system():
    src = _win_ctl_src()
    assert "find_covers_for_buttons" in src, "未按按钮→同设备 cover 寻径"
    assert "find_all_window_buttons_by_action" in src
    assert "ATTR_SUPPORTED_FEATURES" in src and "SET_POSITION" in src, \
        "必须逐台核 SET_POSITION 能力位（5002 等机型诚实拒绝）"
    # 部分失败不得折叠成全成功：ok+bad 混合分支必须如实点名
    assert "但" in src and "未成功" in src, "部分失败话术缺失"
    i = src.index("async def _apply_window_position")
    body = src[i:i + 6000]
    assert "bad_msgs" in body and '"success": True' in body
    # 逐台调用（entity_id 单值循环），不 bulk 一抛全断
    assert "for dev_name, cover_entity_id in covers" in body


def test_find_covers_for_buttons_derives_from_registries():
    src = _win_const_src()
    i = src.index("def find_covers_for_buttons")
    body = src[i:]
    assert 'entry.device_id' in body, "必须走 entity_registry→device_id"
    assert 'async_entries_for_device' in body, "必须走真实注册表派生"
    # 真 HA 2026.8.3 实证（winpos_e2e）：它从来不是 EntityRegistry 的实例
    # 方法，只能以模块函数 er.async_entries_for_device(reg, device_id) 调用
    # （text.py 同形制）——写成 reg.async_entries_for_device 即 AttributeError。
    assert re.search(r"er\.async_entries_for_device\(\s*entity_registry", body), \
        "必须用模块级 er.async_entries_for_device(registry, device_id) 形制"
    assert 'domain != "cover"' in body or 'domain == "cover"' in body
    assert "seen_devices" in body, "同设备多按钮必须去重"


def test_emitted_position_intent_contract():
    """ControlWindow 发射名有注册 handler（test_intent_contract 同口径的最小版：
    此处专钉 position 车道不引入新意图名）。"""
    reg = set()
    for py in CC.glob("*.py"):
        reg |= set(re.findall(r'intent_type\s*=\s*"([^"]+)"',
                              py.read_text(encoding="utf-8")))
    assert "ControlWindow" in reg
