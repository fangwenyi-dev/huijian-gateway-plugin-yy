# -*- coding: utf-8 -*-
"""v1.1.3 批次钉：设备词表/域提示注册表化（P0-1）+ 能力矩阵只读裁决（P0-2 第一步）。

两条都源自 2026-09-21 真机对账，不是假想需求：

P0-1 病灶（可数）：targets.sync_vocab 的域**白名单**只有 9 个域
（light/cover/climate/fan/switch/humidifier/lock/vacuum/media_player），其余
HA 域的实体连**词表都进不去**——客户接了 valve/number/select/alarm/
water_heater/dishwasher/siren/scene/script，语音怎么叫都是"没找到设备"。
而 116 个静态设备词里 **54 词无域提示**（domains=[] → 集成端只能在全实体面
按名找，同名歧义面最大）。域信息本来就直接写在 entity_id 前缀里，不该再靠
一张手抄词表二次猜。本批改成正面事实：排除只读/系统域，其余按注册表派生，
词→域取实体自己的前缀。

P0-2 病灶（真机实测两条）：
  · 「把空调风速调大」→ 我们发 fan_speed=high，小米空调的 fan_modes 是
    level1~7 → 集成 `unsupported the mode`，用户听到一句没有信息量的失败。
  · 「把灯调成绿色」打在只支持色温的灯上 → HA 把 rgb 换算成冷白并**报成功**
    （A 组真机复现：ct 3003→7812、播报"颜色已设为绿色"）。
修法边界：**只拒不改写**，且**没有否证就放行**——错拒一个能做的动作等于用户
永久失去这条口令，漏放一次只是多一个 HA 报错。
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_v1113_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

from conftest import FakeHAClient                       # noqa: E402
from core import capability as C                        # noqa: E402
from core.executor import Executor, wire_args          # noqa: E402
from core.nlu import targets as T                       # noqa: E402
from core.nlu.fast_path import Plan                     # noqa: E402


# ── P0-1 词表 / 域提示注册表化 ──────────────────────────────────
REG = {
    "valve.garden_valve": {"attributes": {"friendly_name": "花园电动阀"}},
    "number.spa_speed": {"attributes": {"friendly_name": "泳池泵转速"}},
    "select.lamp_mode": {"attributes": {"friendly_name": "氛围灯模式"}},
    "alarm_control_panel.home": {"attributes": {"friendly_name": "全屋告警"}},
    "water_heater.bath": {"attributes": {"friendly_name": "浴室热水器"}},
    "dishwasher.kitchen": {"attributes": {"friendly_name": "厨房洗碗机"}},
    "siren.attic": {"attributes": {"friendly_name": "阁楼警报"}},
    "scene.movie": {"attributes": {"friendly_name": "观影场景"}},
    "light.she_deng": {"attributes": {"friendly_name": "办公室射灯"}},
    "sensor.temp_x": {"attributes": {"friendly_name": "温湿度传感器 温度"}},
    "stt.huijian_asr": {"attributes": {"friendly_name": "语音识别"}},
    "update.firmware": {"attributes": {"friendly_name": "固件更新"}},
}


@pytest.fixture()
def synced():
    T.clear_vocab()
    T.sync_vocab(REG)
    yield
    T.clear_vocab()


@pytest.mark.parametrize(("word", "want"), [
    ("花园电动阀", "valve"), ("泳池泵转速", "number"), ("氛围灯模式", "select"),
    ("全屋告警", "alarm_control_panel"), ("浴室热水器", "water_heater"),
    ("厨房洗碗机", "dishwasher"), ("观影场景", "scene"),
])
def test_white_listed_outside_domains_now_recognized(synced, word, want):
    """白名单外域的实体：名字进词表、域取 entity_id 前缀真值。"""
    assert word in T.ALL_DEVICES, word
    assert want in T.domain_hint(word), (word, T.domain_hint(word))


def test_only_read_noise_domains_stay_out_of_vocab(synced):
    """只读/系统域不进设备词表：传感器不是"可以按的开关"，收进来只会产
    注定失败的计划（它们由查询族按实体名直接读，不经过这张表）。"""
    for w in ("语音识别", "固件更新", "温湿度传感器", "办公室温度"):
        assert w not in T.ALL_DEVICES, w


def test_gate_still_covers_non_lexical_lanes(synced):
    """sensor 名字进不了词表 ≠ 不需要门：klar/LLM 兜底档可直接给出
    domains=[sensor] 的计划，那条路上门是唯一还在判的地方。"""
    assert T.domain_hint("花园电动阀") == ["valve"]
    msg = C.gate("TurnDeviceOn", {"target": [{"devices": [
        {"name": "", "domains": ["sensor"]}]}]}, [SENSOR])
    assert msg and "传感器" in msg, msg


def test_dyn_domain_beats_static_guess_and_falls_back(synced):
    """动态优先、静态兜底：注册表没同步时旧行为一字不变（冷启动零扰动）。"""
    assert T.domain_hint("办公室射灯") == ["light"]
    T.clear_vocab()
    assert T.domain_hint("射灯") == ["light"]      # 静态链兜底仍在
    assert T.domain_hint("没听过的设备名") == []


def test_vocab_sync_is_deterministic():
    """跨进程确定性：同一份注册表两次派生必须逐字相同（v1.1.0 词表事故同族护栏）。"""
    T.clear_vocab()
    T.sync_vocab(REG)
    a = (T.ALL_DEVICES, {k: T._dyn_domains[k] for k in sorted(T._dyn_domains)})
    T.sync_vocab(dict(reversed(list(REG.items()))))
    b = (T.ALL_DEVICES, {k: T._dyn_domains[k] for k in sorted(T._dyn_domains)})
    assert a == b


def test_target_shape_carries_registry_domain(synced):
    """端到端：解析出的 target 直接带真域，集成端不再全实体面按名找。"""
    doms = T.domain_hint("电动阀") or T.domain_hint("花园电动阀")
    assert doms == ["valve"], doms


# ── P0-2 能力矩阵：只读裁决 ─────────────────────────────────────
def ent(eid, **attrs):
    a = dict(attrs)
    a.setdefault("friendly_name", eid.split(".", 1)[-1])
    return {"entity_id": eid, "state": "on", "attributes": a}


AC_LEVELS = ent("climate.office_ac", friendly_name="办公室空调",
                hvac_modes=["off", "cool", "heat"], fan_modes=["level1", "level2", "level7"])
AC_NORMAL = ent("climate.room_ac", friendly_name="卧室空调",
                hvac_modes=["off", "cool"], fan_modes=["auto", "low", "medium", "high"])
FAN_PLAIN = ent("fan.living_fan", friendly_name="客厅风扇", percentage=60,
                percentage_step=25)
CT_LIGHT = ent("light.she_deng", friendly_name="办公室射灯", supported_features=2,
               supported_color_modes=["color_temp"])
RGB_LIGHT = ent("light.strip", friendly_name="灯带", supported_features=9,
                supported_color_modes=["rgb", "color_temp"])
COVER_NOPOS = ent("cover.window_opener", friendly_name="平开窗 开窗器",
                  supported_features=1)          # 仅 OPEN_CLOSE
COVER_POS = ent("cover.curain", friendly_name="客厅窗帘", supported_features=5,
                current_position=40)
MP = ent("media_player.tv", friendly_name="客厅电视", volume_level=0.4)
SENSOR = ent("sensor.temp", friendly_name="办公室温度", device_class="temperature")
BARE = {"entity_id": "light.unknown_thing", "state": "on", "attributes": {}}


def gate(intent, attribute, delta, *ents):
    doms = sorted({str(e["entity_id"]).split(".", 1)[0] for e in ents}) or ["light"]
    args = {"target": [{"devices": [{"name": "", "domains": doms}]}]}
    if attribute:
        args["attribute"] = attribute
    if delta is not None:
        args["delta"] = delta
    return C.gate(intent, args, list(ents))


def test_climate_fan_speed_high_refused_with_real_options():
    msg = gate("AdjustDeviceAttribute", "fan_speed", "high", AC_LEVELS)
    assert msg and "level1" in msg, msg          # 把真实档位教回给用户


def test_climate_fan_speed_real_mode_passes():
    assert gate("AdjustDeviceAttribute", "fan_speed", "level2", AC_LEVELS) is None
    assert gate("AdjustDeviceAttribute", "fan_speed", "high", AC_NORMAL) is None
    assert gate("AdjustDeviceAttribute", "fan_speed", "50", AC_LEVELS) is None


def test_color_temp_light_cannot_take_color():
    msg = gate("AdjustDeviceAttribute", "color", "#00FF00", CT_LIGHT)
    assert msg and "色温" in msg, msg
    assert gate("AdjustDeviceAttribute", "color", "#00FF00", RGB_LIGHT) is None


def test_color_temp_slot_still_ok_on_ct_light():
    assert gate("AdjustDeviceAttribute", "color_temperature", "3000", CT_LIGHT) is None


def test_cover_without_set_position_refused():
    assert gate("AdjustDeviceAttribute", "position", "50", COVER_NOPOS)
    assert gate("AdjustDeviceAttribute", "position", "50", COVER_POS) is None


def test_media_player_volume_not_forwarded():
    """集成侧 volume 是显式 raise unsupported，网关不再白跑一趟。"""
    assert gate("AdjustDeviceAttribute", "volume", "50", MP)


def test_fan_percentage_ok():
    assert gate("AdjustDeviceAttribute", "fan_speed", "50", FAN_PLAIN) is None


def test_missing_metadata_never_refused():
    """没有否证就放行——错拒等于永久夺走用户的口令。"""
    assert gate("AdjustDeviceAttribute", "brightness", "50", BARE) is None
    assert gate("AdjustDeviceAttribute", "position", "50", BARE) is None
    assert gate("TurnDeviceOn", None, None, BARE) is None


def test_read_only_entity_turn_refused_with_hint():
    msg = gate("TurnDeviceOn", None, None, SENSOR)
    assert msg and "传感器" in msg, msg


def test_no_candidates_is_pass_through():
    """注册表未同步/零命中：一律放行，存在性由集成端判，网关不越权。"""
    assert gate("AdjustDeviceAttribute", "color", "#00FF00") is None
    assert C.gate("AdjustDeviceAttribute", {"attribute": "color", "delta": "#00FF00",
                                           "target": []}, [AC_LEVELS]) is None


# ── 端到端：执行器真的被拦下且不外发 ────────────────────────────
def _run(ha, intent, args, utter="测试句"):
    return asyncio.run(Executor(ha, None).run(
        Plan(intent=intent, args=args, source="t0", utterance=utter)))


def test_executor_refuses_unsupported_colour_temp_value_and_sends_nothing():
    ha = FakeHAClient(states={"climate.office_ac": dict(AC_LEVELS, entity_id="climate.office_ac")})
    plan_args = {"attribute": "fan_speed", "delta": "high",
                 "target": [{"devices": [{"name": "办公室空调", "domains": ["climate"]}]}]}
    ok, speech = _run(ha, "AdjustDeviceAttribute", plan_args)
    assert ok is False and speech.startswith("抱歉")
    assert "level" in speech, speech
    assert ha.calls == [], "拦下即不得外发 intent"


def test_executor_passes_supported_case_through():
    ha = FakeHAClient(states={"climate.office_ac": dict(AC_LEVELS, entity_id="climate.office_ac")})
    ok, speech = _run(ha, "AdjustDeviceAttribute",
                      {"attribute": "fan_speed", "delta": "level2",
                       "target": [{"devices": [{"name": "办公室空调",
                                                "domains": ["climate"]}]}]})
    assert ha.calls != []                     # 放行的仍走原通道（成败由集成判）


def test_executor_gate_survives_internal_wire_name_pair():
    """网关内部名 color_temperature 与出站名 temperature 不得让裁决口径错位。"""
    ha = FakeHAClient(states={"light.she_deng": dict(CT_LIGHT, entity_id="light.she_deng")})
    ok, speech = _run(ha, "AdjustDeviceAttribute",
                      {"attribute": "color_temperature", "delta": "3000",
                       "target": [{"devices": [{"name": "办公室射灯",
                                                "domains": ["light"]}]}]})
    assert ha.calls != [] and ha.calls[0][1]["attribute"] == "temperature"
    assert speech or ok is not None
    # 同一盏灯要颜色 → 拦
    ok2, speech2 = _run(ha, "AdjustDeviceAttribute",
                        {"attribute": "color", "delta": "#00FF00",
                         "target": [{"devices": [{"name": "办公室射灯",
                                                  "domains": ["light"]}]}]})
    assert ok2 is False and ha.calls and len(ha.calls) == 1, "颜色请求不得第二次外发"


# ── 融合第一步：契约单点 × 跨仓漂移守卫 ─────────────────────────
def _integration_registry_attrs() -> set[str]:
    """直接从集成源码里抽 register_adjustment 登记的属性名（真正的裁决者）。

    用文本抽而不是 import：本测试栈没有 homeassistant 包，import 会炸。
    """
    import re
    src = (HERE / "custom_components" / "huijian_ai" /
           "intent_adjust_attribute.py").read_text(encoding="utf-8")
    return {m.group(2) for m in re.finditer(
        r'@register_adjustment\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)', src)}


def test_contract_matches_integration_registry():
    from core.nlu.schema import REGISTRY_ATTRIBUTES
    real = _integration_registry_attrs()
    assert real, "集成注册表解析失败（装饰器形态变了，守卫要跟着改）"
    assert set(REGISTRY_ATTRIBUTES) == real, (
        f"契约表与集成注册表漂移：契约多 {set(REGISTRY_ATTRIBUTES) - real}，"
        f"集成多 {real - set(REGISTRY_ATTRIBUTES)}")


def test_llm_tool_enum_is_generated_from_contract_not_copied():
    """LLM 工具枚举由契约生成，且只给"真能成"的那部分。

    历史上这里手抄过一个注册表不存在的 colour_temperature；反过来全量照抄注册表
    也不对——volume 注册了但处理器显式 raise unsupported（adjust.py:595-602），
    发出去就是白跑一趟 + 一个用户听不懂的失败。
    """
    from core.agent import TOOLS
    from core.nlu.schema import ADDRESSABLE_ATTRIBUTES, REGISTRY_ATTRIBUTES
    tool = next(t for t in TOOLS
                if t["function"]["name"] == "AdjustDeviceAttribute")
    enum = tool["function"]["parameters"]["properties"]["attribute"]["enum"]
    assert set(enum) == set(ADDRESSABLE_ATTRIBUTES), enum
    assert "volume" not in enum and "volume" in REGISTRY_ATTRIBUTES
    assert "colour_temperature" not in enum and "color_temperature" not in enum


def test_wire_map_and_attr_cn_share_the_contract():
    from core import executor as X
    from core.nlu import schema as SC
    assert X._ATTR_WIRE is SC.ATTR_TO_WIRE
    assert X.ATTR_CN is SC.ATTR_CN
    assert SC.wire_attribute("color_temperature") == "temperature"
    assert SC.wire_attribute("brightness") == "brightness"


# ── 变异靶 ────────────────────────────────────────────────────
def test_capability_module_surface():
    for fn in ("gate", "supports_attribute", "READ_ONLY_DOMAINS"):
        assert hasattr(C, fn), fn
    assert wire_args("AdjustDeviceAttribute", {"attribute": "color_temperature"}) \
        ["attribute"] == "temperature"
