# -*- coding: utf-8 -*-
"""v1.1.1 属性车道批次钉（2026-10-01 v1.1.0 发版后对账批次 #2/#3/#4/#5）。

四条缺陷同族：**网关侧话术产出的属性名，集成端注册表里没有 ⇒ unsupported，
或值被静默丢弃**。集成注册表（intent_adjust_attribute.register_adjustment）
权威集 = light:{brightness,color,temperature} fan:{fan_speed}
climate:{fan_speed,temperature} cover:{position} humidifier:{humidity}
number:{value} media_player:{volume,brightness}(恒 unsupported)。

② 色温死字段：网关内部属性名 color_temperature（三端另有 colour_temperature
   拼法）不在注册表 ⇒ 「色温调到4000K」必 unsupported。内部名**不能直接改**
   ——temperature 在网关侧是空调℃口径（_T0_ATTR_WORD/H1 改道/HassClimateSet-
   Temperature 全用它），改名即串台。修法=出站（executor 上 wire 前）单点映射，
   Plan.args 与播报层口径一律不动。
③ 颜色/色调话术：数据集实锤「把书房灯调成绿色」→ color/#00FF00 全表 MISS；
   「把色温调高一点」被 T1 判成 AdjustTemperature ⇒ 去动**空调**（误执行，
   与「内倒→雷达」同级，宁 MISS 也不碰空调）。
④ 非窗户「开到/调到+值」绝对值车道：字面表把 开到N 硬编码成 position，
   「客厅灯开到50」发 position 给 light=unsupported；「把窗帘调到50%」在
   ②③ 够不到，掉进 T1 Turn 把 50% 整个丢光（假动作）。
   合同（intent_adjust_attribute.parse_delta/calc_target 逐条核）：% 与裸数
   走 number、档 走 level、max/min/low/high 可用而 medium/auto 落 unsupport-
   ed、cover 只支持 number、climate.temperature 拒 %。认不出设备族一律 MISS。
⑤ 内倒语序：「打开客厅窗户内倒」被 ^(打开) 吃成 TurnDeviceOn→窗型纠正，
   纠正分支把 action 硬填 open，句尾 内倒 蒸发（用户要内倒得到全开）。
   动作 a 车道本就存在（_WINDOW_ACTION_SCAN），本条纯话术可达性。
   反向守卫同钉：「打开客厅的内开内倒窗」的 内倒 是**窗型词根**不是动作，
   不得被扫成 a（数据集实锤该句 action=open）。
"""
import asyncio
import copy
import os
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_v1111_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))
sys.path.insert(0, str(HERE / "tests"))

from conftest import FakeHAClient                            # noqa: E402
from core.executor import Executor                           # noqa: E402
from core.nlu import fast_path as F                          # noqa: E402
from core.nlu.fast_path import FastPath, Plan                # noqa: E402
from core.nlu.textcnn import TextCNN                         # noqa: E402
from core.settings import DEFAULTS                           # noqa: E402

# 集成端注册表权威集（本文件多处判据的事实来源；抄自 register_adjustment 装饰器）
REGISTRY = {
    "light": {"brightness", "color", "temperature"},
    "fan": {"fan_speed"},
    "climate": {"fan_speed", "temperature"},
    "cover": {"position"},
    "humidifier": {"humidity"},
    "number": {"value"},
}


@pytest.fixture(scope="module")
def fp():
    class _S:
        def get(self, dotted, default=None):
            cur = copy.deepcopy(DEFAULTS)
            for k in dotted.split("."):
                if not isinstance(cur, dict) or k not in cur:
                    return default
                cur = cur[k]
            return cur

    tc = TextCNN(os.environ["HUIJIAN_NLU_DATA"])
    tc._ensure()
    return FastPath(FakeScenes(), tc, _S())


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


def _m(fp, text):
    return asyncio.run(fp.match(text))


def _domains(plan):
    return {d for t in (plan.args.get("target") or [])
            for d in ((t.get("devices") or [{}])[0].get("domains") or [])}


# ── ② 出站映射：只改上 wire 的那一份，内部名与播报口径不动 ──────────
def test_colour_temp_ships_registry_name_on_wire():
    """「色温调到4000K」真机能接的形态是 temperature（light 域处理器）。"""
    ha = FakeHAClient()
    plan = Plan(intent="AdjustDeviceAttribute",
                args={"attribute": "color_temperature", "delta": "4000",
                      "target": [{"area": "客厅",
                                  "devices": [{"name": "灯", "domains": ["light"]}]}]},
                source="t0", utterance="客厅灯色温调到4000K")
    asyncio.run(Executor(ha, None).run_raw(plan))
    name, data = ha.calls[0]
    assert name == "AdjustDeviceAttribute"
    assert data["attribute"] == "temperature", data
    assert data["delta"] == "4000", data


def test_british_spelling_also_mapped():
    """LLM 工具面历史上发过 colour_temperature，同一死字段同修。"""
    ha = FakeHAClient()
    plan = Plan(intent="AdjustDeviceAttribute",
                args={"attribute": "colour_temperature", "delta": "3000",
                      "target": []}, source="t0", utterance="色温调到3000k")
    asyncio.run(Executor(ha, None).run_raw(plan))
    assert ha.calls[0][1]["attribute"] == "temperature"


def test_climate_temperature_is_not_the_colour_lane():
    """反向闸：空调℃口径的 temperature 原样透传（映射只吃 color/colour_ 前缀）。"""
    ha = FakeHAClient()
    plan = Plan(intent="AdjustDeviceAttribute",
                args={"attribute": "temperature", "delta": "26",
                      "target": [{"devices": [{"domains": ["climate"]}]}]},
                source="t0", utterance="空调调到26度")
    asyncio.run(Executor(ha, None).run_raw(plan))
    assert ha.calls[0][1]["attribute"] == "temperature"


def test_utterance_still_shows_in_internal_plan_name():
    """出站改名不得回灌 Plan.args：话术层按 color_temperature 挑「色温/调冷调暖」。"""
    plan = Plan(intent="AdjustDeviceAttribute",
                args={"attribute": "color_temperature", "delta": "+500",
                      "target": []}, source="t0", utterance="色温调高一点")
    speech = Executor(FakeHAClient(), None).speech(
        plan, {"states": [{"name": "客厅灯", "success": True}]})
    assert plan.args["attribute"] == "color_temperature"
    assert "色温" in speech and "调冷" in speech, speech


def test_llm_tool_attribute_enum_is_registry_names():
    """工具面 enum 里每个属性名都必须在注册表内——colour_temperature 是死字段来源。"""
    from core.agent import TOOLS
    enum = next(t for t in TOOLS
                if t["function"]["name"] == "AdjustDeviceAttribute")["function"] \
        ["parameters"]["properties"]["attribute"]["enum"]
    supported = set().union(*REGISTRY.values())
    dead = {a for a in enum if a not in supported}
    assert not dead, f"LLM 工具枚举含注册表外死字段: {dead}"


# ── ③ 颜色/色调话术（数据集原句 + 数据集期望 hex）─────────────────
DATASET_COLOR = [
    ("把客厅灯调成暖色调", "#FFAA80"),
    ("把客厅灯调成冷色调", "#80FFFF"),
    ("把书房灯调成绿色", "#00FF00"),
    ("把客厅灯调成黄色", "#FFFF00"),
    ("把卧室灯调成蓝色", "#0000FF"),
    ("把灯调成红色", "#FF0000"),
    ("把灯光调成白色", "#FFFFFF"),
    ("把灯光调成紫色", "#8000FF"),
    ("把灯光调成暖白色", "#FFEFD5"),
]


@pytest.mark.parametrize(("sentence", "hex_color"), DATASET_COLOR)
def test_dataset_color_phrases(fp, sentence, hex_color):
    p = _m(fp, sentence)
    assert p is not None and p.intent == "AdjustDeviceAttribute", sentence
    assert p.args["attribute"] == "color", (sentence, p.args)
    assert p.args["delta"] == hex_color, (sentence, p.args)
    assert "light" in _domains(p), (sentence, p.args)


def test_color_area_kept_when_spoken(fp):
    p = _m(fp, "把主卧灯调成冷色调")
    tgt = p.args["target"][0]
    assert tgt.get("area") == "主卧", tgt
    assert (tgt.get("devices") or [{}])[0]["domains"] == ["light"], tgt


@pytest.mark.parametrize("sentence", [
    "把客厅灯调成暖色调", "把书房灯调成绿色", "把灯光调成红色",
])
def test_color_delta_always_carries_hash_prefix(fp, sentence):
    """集成 parse_delta 只认 # 前缀（hex_color_pattern），丢了 # 即 invalid value。"""
    p = _m(fp, sentence)
    assert str(p.args["delta"]).startswith("#"), p.args


def test_unmapped_color_word_is_refused_not_shipped():
    """色表外词不得把中文原词当 delta 发出去（集成必 invalid value）。"""
    assert F.resolve_color_delta("马赛克色") is None
    assert F.resolve_color_delta("绿色") == "#00FF00"


def test_bare_attribute_word_without_value_is_refused(fp):
    """光有属性词、无数值无档位 → 如实 MISS，绝不发空 delta（集成 Required
    ('delta') 缺槽即 Invalid=白占一次执行，且话术只剩裸「好的」）。"""
    assert _m(fp, "色温") is None
    assert _m(fp, "把客厅湿度") is None


def test_pipeline_fills_domain_target_for_bare_mode():
    """级联层补域兜底目标（无上下文时）——集成 Required('target') 缺槽即 Invalid。"""
    from test_multi_device_and_opener import _pipe
    p, _ex = _pipe()
    plan = Plan(intent="SetDeviceMode", args={"mode": "cool"}, source="t0",
                utterance="设置为制冷模式")
    out = p._apply_context(plan, "设置为制冷模式", origin="v1111-pin-isolated")
    assert out is not None
    assert out.args["target"] == [{"devices": [{"domains": ["climate"]}]}], out.args


def test_bare_warm_light_stays_kelvin_lane(fp):
    """「暖光/冷光」是色温档（既有承诺），只有「暖色/冷色(调)」走 RGB 色值。"""
    for s, want in (("暖光", "2700"), ("冷光", "6500")):
        p = _m(fp, s)
        assert p.args["attribute"] == "color_temperature", (s, p.args)
        assert p.args["delta"] == want, (s, p.args)


# ── ③ 色温相对档：绝不得串到空调（误执行类）───────────────────────
@pytest.mark.parametrize(("sentence", "sign"), [
    ("把色温调高一点", "+"), ("色温调低一点", "-"),
    ("帮我把色温调暖一点", "-"), ("把色温调凉一点", "+"),
])
def test_colour_temp_relative_never_reaches_climate(fp, sentence, sign):
    p = _m(fp, sentence)
    assert p is not None and p.intent == "AdjustDeviceAttribute", (sentence, p)
    assert p.args["attribute"] == "color_temperature", (sentence, p.args)
    assert str(p.args["delta"]).startswith(sign), (sentence, p.args)
    assert "climate" not in _domains(p), (sentence, p.args)
    assert p.args["delta"].lstrip("+-") == "500", (sentence, p.args)


# ── ④ 绝对值车道：属性名由设备族决定，认不出族如实 MISS ────────────
ABSOLUTE_CASES = [
    ("客厅灯开到50", "brightness", "50", {"light"}),
    ("把灯开到百分之八十", "brightness", "80", {"light"}),
    ("风扇开到3档", "fan_speed", "3档", {"fan"}),
    ("把客厅窗帘调成80%", "position", "80", {"cover"}),
    ("客厅窗帘打开到50%", "position", "50", {"cover"}),
    ("把窗帘调到50%", "position", "50", {"cover"}),
]


@pytest.mark.parametrize(("sentence", "attr", "delta", "doms"), ABSOLUTE_CASES)
def test_absolute_value_lane_resolves_by_device_family(fp, sentence, attr, delta, doms):
    """数据集 position 全表 + 用户现场「客厅灯开到50」：值一律不得丢。"""
    p = _m(fp, sentence)
    assert p is not None, f"{sentence} → MISS（值被丢光或整句不接）"
    assert p.intent == "AdjustDeviceAttribute", (sentence, p.intent, p.args)
    assert p.args["attribute"] == attr, (sentence, p.args)
    assert str(p.args["delta"]) == delta, (sentence, p.args)
    assert _domains(p) == doms, (sentence, p.args)


@pytest.mark.parametrize("sentence", [
    "把电视开到50",        # media_player：注册表只有恒 unsupported 的 volume/brightness
    "把书房开到50",        # 只有区域、认不出设备族
    "把插座开到50",        # switch：无任何可调属性
])
def test_absolute_value_without_family_is_refused(fp, sentence):
    """宁如实失败（v1.0.69 红线）：不猜属性、不把值塞给错域。"""
    assert _m(fp, sentence) is None, sentence


def test_shipped_attribute_names_are_all_in_registry(fp):
    """防回退总闸：本批全部落地句出站属性名必须落在对应设备族的注册表内。"""
    for sentence, _attr, _delta, doms in ABSOLUTE_CASES:
        p = _m(fp, sentence)
        for dom in doms:
            assert p.args["attribute"] in REGISTRY[dom], (sentence, dom, p.args)
    for sentence, _hex in DATASET_COLOR:
        p = _m(fp, sentence)
        assert p.args["attribute"] in REGISTRY["light"], sentence


def test_sentinel_attribute_never_escapes(fp):
    """@绝对值哨兵若漏到 args，集成侧=unsupported，本钉当场红。"""
    for s in [x[0] for x in ABSOLUTE_CASES] + ["把色温调高一点", "内倒展厅内倒窗"]:
        p = _m(fp, s)
        if p is not None:
            assert not str(p.args.get("attribute", "")).startswith("@"), (s, p.args)


# ── ⑤ 内倒语序：句尾动作可达，且窗型词根不得被误当动作 ─────────────
@pytest.mark.parametrize("sentence", [
    "打开客厅窗户内倒", "把客厅窗户内倒", "打开主卧的窗户内倒",
    "打开书房窗户内倒", "客厅窗户内倒",
])
def test_trailing_neidao_after_open_verb_reaches_action_a(fp, sentence):
    p = _m(fp, sentence)
    assert p is not None and p.intent == "ControlWindow", (sentence, p)
    assert str(p.args.get("action", "")).lower() == "a", (sentence, p.args)


@pytest.mark.parametrize(("sentence", "want_area"), [
    ("打开客厅窗户内倒", "客厅"), ("把主卧窗户内倒", "主卧"),
    ("打开书房窗户内倒", "书房"),
])
def test_neidao_lane_keeps_area(fp, sentence, want_area):
    p = _m(fp, sentence)
    assert (p.args["target"][0].get("area") or "") == want_area, (sentence, p.args)


@pytest.mark.parametrize(("sentence", "want"), [
    ("打开客厅的内开内倒窗", "open"),      # 内倒 是窗型词根，不是动作
    ("关闭卧室的内开内倒窗", "close"),
    ("打开书房的内开内倒窗", "open"),
    ("把客厅的内开内倒窗内倒", "a"),       # 词根之外另有动作 内倒
    ("打开内倒窗", "open"),
    ("打开客厅窗户", "open"),
])
def test_window_type_root_not_mistaken_for_neidao_action(fp, sentence, want):
    p = _m(fp, sentence)
    assert p is not None and p.intent == "ControlWindow", (sentence, p)
    assert str(p.args.get("action", "")).lower() == want, (sentence, p.args)


# ══ 第二批：2026-10-01 全量数据集对账（786 句复跑）落地的缺口族 ═════════
# 复算工具 = tests/dataset_recon.py（口径：FastPath 本地档逐句比数据集声明的
# intent/slot）。以下各族都是台账里**可复现的整句失能**，非假想需求。

# ── 区名切分：阳台/主卧/次卧 的「区域+设备+绝对值」此前整族丢数值 ──────
AREA_SPLIT_CASES = [
    ("把阳台窗帘调成50%", "position", "50", "阳台"),
    ("把主卧窗帘调成40%", "position", "40", "主卧"),
    ("把次卧窗帘调成60%", "position", "60", "次卧"),
    ("把客厅窗帘调成80%", "position", "80", "客厅"),
]


@pytest.mark.parametrize(("sentence", "attr", "delta", "area"), AREA_SPLIT_CASES)
def test_area_names_without_suffix_still_split(fp, sentence, attr, delta, area):
    """extract_prefix 靠尾字（室厅房间…），阳台/主卧/次卧 无尾字 → 曾在
    「阳台窗|帘」处把窗帘劈半 → ②③ 全失配 → 掉 T1 Turn，50% 蒸发。"""
    p = _m(fp, sentence)
    assert p is not None and p.args.get("attribute") == attr, (sentence, p)
    assert str(p.args["delta"]) == delta, f"{sentence} 数值错：{p.args}"
    assert p.args["target"][0].get("area") == area, (sentence, p.args)


def test_each_area_number_maps_distinctly(fp):
    """40/50/60 三句必须各得各的数——"一律 50"是本批修掉的真 bug（一半短路）。"""
    got = [str(_m(fp, s).args["delta"]) for s in
           ("把主卧窗帘调成40%", "把阳台窗帘调成50%", "把次卧窗帘调成60%")]
    assert got == ["40", "50", "60"], got


# ── 相对档不得被读成绝对值（误执行类：空调被设到 2℃）────────────────
@pytest.mark.parametrize(("sentence", "want"), [
    ("把空调温度调高2度", "+2"),
    ("把空调温度调低3度", "-3"),
    ("调高空调温度5度", "+5"),
    ("调低空调温度2度", "-2"),
])
def test_relative_temperature_degrees_keep_sign(fp, sentence, want):
    p = _m(fp, sentence)
    assert p is not None and p.args.get("attribute") == "temperature", (sentence, p)
    assert str(p.args["delta"]) == want, (sentence, p.args)


@pytest.mark.parametrize(("sentence", "want"), [
    ("风量调大2", "+2"), ("风速调小1", "-1"),
])
def test_direction_plus_bare_number_is_relative_not_absolute(fp, sentence, want):
    """无单位裸数 + 方向字 = 相对档（属性词快捷的 dir 分支唯一活路，变异必须致红）。"""
    p = _m(fp, sentence)
    assert p is not None, sentence
    assert str(p.args["delta"]) == want, (sentence, p.args)


@pytest.mark.parametrize(("sentence", "want"), [
    ("把客厅灯调暗到30%", "30"),
    ("亮度调暗到40%", "40"),
    ("把灯调亮到80", "80"),
])
def test_darken_to_value_is_absolute_not_relative(fp, sentence, want):
    """「调暗到N%」是绝对值：数据集实锤原表漏 调暗到 → 被 ^(调暗)→-20 截成相对档，
    用户要 30% 得到"再暗 20"（值方向双错，本批唯一现测到的静默错值句）。"""
    p = _m(fp, sentence)
    assert p is not None and p.args.get("attribute") == "brightness", (sentence, p)
    assert str(p.args["delta"]) == want, (sentence, p.args)


def test_bare_diao_verb_never_hijacks_attribute_sentence(fp):
    """裸 调 不得进绝对值动词表：「调高亮度到80%」被截走后属性词蒸发。"""
    p = _m(fp, "调高亮度到80%")
    assert p is not None and p.args.get("attribute") == "brightness", p
    assert str(p.args["delta"]) == "80", p.args


# ── 风速/湿度/开合度：数据集三族整句 MISS ────────────────────────────
FAN_HUMID_CASES = [
    ("把空调风速调大", "fan_speed", "high"),
    ("把空调风速调小", "fan_speed", "low"),
    ("风速调到最大", "fan_speed", "max"),
    ("湿度设为50%", "humidity", "50"),
    ("把窗帘位置调低", "position", "-20"),
    ("亮度调到最大", "brightness", "max"),
]


@pytest.mark.parametrize(("sentence", "attr", "delta"), FAN_HUMID_CASES)
def test_dataset_special_and_relative_lanes(fp, sentence, attr, delta):
    """max/min/high/low 都是集成 DELTA_SPECIAL_VALUES；medium/auto 反而
    unsupported，故本表一律不产（口径抄自 intent_adjust_attribute）。"""
    p = _m(fp, sentence)
    assert p is not None, f"{sentence} → MISS"
    assert p.args.get("attribute") == attr, (sentence, p.args)
    assert str(p.args["delta"]) == delta, (sentence, p.args)


def test_special_values_that_integration_refuses_are_never_shipped(fp):
    """medium/auto 在 calc_target 里直接 raise unsupported —— 网关不得产这两档。"""
    for s in ("风速调到中档", "风量调到medium", "风速设成auto"):
        p = _m(fp, s)
        assert p is None or str(p.args.get("delta")) not in ("medium", "auto"), (s, p.args)


# ── 模式族动词与词序（数据集 15 句 MISS 的两条根因）──────────────────
@pytest.mark.parametrize(("sentence", "mode"), [
    ("空调设置为制冷", "cool"), ("空调设置为制热模式", "heat"),
    ("空调调成除湿模式", "dry"), ("空调设置为送风模式", "fan_only"),
    ("空调设置为自动", "auto"),
])
def test_mode_verbs_and_suffix_forms(fp, sentence, mode):
    p = _m(fp, sentence)
    assert p is not None and p.intent == "SetDeviceMode", (sentence, p)
    assert p.args.get("mode") == mode, (sentence, p.args)


@pytest.mark.parametrize(("sentence", "mode"), [
    ("设置为制冷模式", "cool"), ("调成除湿模式", "dry"),
])
def test_bare_mode_without_device_is_not_lost(fp, sentence, mode):
    """无设备无区域的模式句：意图先立住，同域目标由 pipeline 兜底补
    （fp 内补=显式全屋形态，会豁免上下文继承，见 attribute_domain_target 注释）。"""
    p = _m(fp, sentence)
    assert p is not None and p.intent == "SetDeviceMode", (sentence, p)
    assert p.args.get("mode") == mode, (sentence, p.args)
    assert not p.args.get("target"), (sentence, p.args)


def test_attribute_domain_target_fills_only_domain_filter():
    from core.nlu.fast_path import attribute_domain_target
    assert attribute_domain_target("AdjustDeviceAttribute",
                                   {"attribute": "fan_speed"}) == [
        {"devices": [{"domains": ["climate"]}]}]
    assert attribute_domain_target("SetDeviceMode", {"mode": "cool"}) == [
        {"devices": [{"domains": ["climate"]}]}]
    # 场景模式（sleep/eco…）不属 climate 专属语义，绝不兜底成空调
    assert attribute_domain_target("SetDeviceMode", {"mode": "sleep"}) == []
    assert attribute_domain_target("TurnDeviceOn", {}) == []


# ── 守卫分域：开关族铁律原样保留 ─────────────────────────────────────
@pytest.mark.parametrize("sentence", ["开空调", "打开空调", "关闭空调"])
def test_power_lane_still_refuses_bare_ac(fp, sentence):
    assert _m(fp, sentence) is None, sentence


# ── 集成合同口径（parse_delta 单位分支）在本批的落点：delta 字符串形态 ──
@pytest.mark.parametrize(("sentence", "want_delta"), [
    ("风扇开到3档", "3档"),          # 档 → 集成走 level 分支
    ("客厅灯开到50", "50"),          # 裸数 → number 分支
    ("把客厅窗帘调成80%", "80"),     # % → number 分支（数值已归一，不带裸 %）
])
def test_delta_string_shape_matches_integration(fp, sentence, want_delta):
    """delta 必须是集成 parse_delta 认得的形态：符号+数值+可选单位（档/挡）。"""
    p = _m(fp, sentence)
    assert p is not None, sentence
    assert str(p.args["delta"]) == want_delta, (sentence, p.args)
