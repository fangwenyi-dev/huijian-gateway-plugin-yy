# -*- coding: utf-8 -*-
"""多设备一句话 + 开窗器名称识别 回归钉（2026-09 用户令三项）。

背景（数据集先行核对）：《意图数据集/意图集要求.md》铁律——
「看到窗字→ControlWindow+button，看到帘字→Turn*+cover」，窗族含
平开/平推/推拉/天窗/飘窗/推拉门/内开内倒/单内倒/外装平开/智能窗；
开窗器/开合器/推窗器是窗控机型的**设备词**（button 按压体系），
HassCreateVoiceScene 的 actions 数组天然多动作。

三层钉：
① fast_path：开窗器整词不再被剥成 name="窗" 残渣、不再错判 Turn*；
   「暂停播放器」不被 播放 切点腰斩；连排超限句有 _SERIAL_RESIDUE 守卫，
   宁可如实拒收，绝不静默半执行；
② pipeline 链发：并/并且/同时/顿号/逗号/无标点动词连排 → 多设备逐段执行；
③ creation：场景/自动化多设备动作（2~8 段），超限整单拒收不建半成品；
   开窗器动作可入场景。
另钉 LLM 面（agent.py）：系统规则含窗/帘铁律与多动作意识，ControlWindow
描述点名开窗器，设备简报把 button 窗实体纳入视野。
"""
import asyncio
import copy
import json
import os
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_mdvo_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

from core.settings import DEFAULTS  # noqa: E402
from core.nlu.fast_path import (  # noqa: E402
    FastPath, _opener_word, _WINDOW_OPENER_WORDS)
from core.nlu import creation as cr  # noqa: E402
from core.nlu import targets as T  # noqa: E402
from core.nlu.textcnn import TextCNN  # noqa: E402
from core.pipeline import Pipeline  # noqa: E402


class _S:
    def get(self, dotted, default=None):
        cur = copy.deepcopy(DEFAULTS)
        for k in dotted.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur


class FakeScenes:
    def __init__(self):
        self.d = {}
        self.triggers = []

    def check(self, t):
        return self.d.get(t)

    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def refresh(self, force=False):
        pass

    async def verify_or_refresh(self, phrase):
        return True


class HA:
    def __init__(self, states=None):
        self._states = states or {}
        self._areas = {}
        self._entity_area = {}

    async def states(self):
        return self._states

    async def fire_event(self, name, data):
        pass


class Klar:
    async def match(self, t):
        return None


class RecExecutor:
    def __init__(self):
        self.plans = []

    async def run(self, plan):
        self.plans.append(plan)
        return True, "好的，办好了"


@pytest.fixture(scope="module")
def tc():
    t = TextCNN(os.environ["HUIJIAN_NLU_DATA"])
    t._ensure()
    return t


@pytest.fixture()
def fp(tc):
    return FastPath(FakeScenes(), tc, _S())


def _m(fp, text):
    return asyncio.run(fp.match(text))


# ── ① 开窗器名称识别 ───────────────────────────────────────────
OPENER_CASES = [
    ("关闭开窗器", "close", "开窗器"),
    ("打开开窗器", "open", "开窗器"),
    ("开窗器关闭", "close", "开窗器"),
    ("开窗器打开", "open", "开窗器"),
    ("开一下开窗器", "open", "开窗器"),
    ("关掉开窗器", "close", "开窗器"),
    ("关闭办公室开窗器", "close", "开窗器"),
    ("打开3号开窗器", "open", "3号开窗器"),
    ("关掉三号开合器", "close", "3号开合器"),
    ("关闭开创器", "close", "开窗器"),        # ASR 近音（纠错表配套）
]


@pytest.mark.parametrize("text,action,name", OPENER_CASES)
def test_opener_routes_to_control_window(fp, text, action, name):
    p = _m(fp, text)
    assert p is not None, f"{text!r} 落空"
    assert p.intent == "ControlWindow", f"{text!r} → {p.intent}（错意图）"
    tgt = p.args.get("target") or [{}]
    got = tgt[0].get("devices", [{}])[0].get("name")
    assert got == name, f"{text!r} name={got!r}≠{name!r}（残渣）"
    assert p.args.get("action") == action


def test_opener_tilt_and_pause(fp):
    assert _m(fp, "开窗器内倒").args.get("action") == "a"
    assert _m(fp, "暂停开窗器").args.get("action") == "pause"


def test_opener_position_and_speed_lanes_intact(fp):
    p = _m(fp, "开窗器开到50%")
    assert p.intent == "ControlWindow" and p.args.get("position") == 50
    p = _m(fp, "开窗器速度设为80")
    assert p.intent == "ControlWindow" and p.args.get("speed") == 80


def test_window_type_beats_opener_word(fp):
    """窗型词优先：「办公室平开窗开窗器」仍按窗型纠正出 name=平开窗+区域。"""
    p = _m(fp, "打开办公室平开窗开窗器")
    assert p.intent == "ControlWindow"
    tgt = p.args["target"][0]
    assert tgt["area"] == "办公室"
    assert tgt["devices"][0]["name"] == "平开窗"


def test_curtain_opener_not_hijacked_by_window_lane(fp):
    """「窗帘开合器」= 开合帘设备（cover 语义），绝不进 ControlWindow 按压道。"""
    p = _m(fp, "打开窗帘开合器")
    assert p is not None and p.intent == "TurnDeviceOn"


def test_plain_window_words_unchanged(fp):
    for text, action in [("开窗", "open"), ("关窗", "close"),
                         ("打开窗户", "open"), ("关闭办公室平开窗", "close")]:
        p = _m(fp, text)
        assert p is not None and p.intent == "ControlWindow"
        if p.args.get("action"):
            assert p.args["action"] == action


def test_opener_vocab_sync_guard():
    """防漂移：开窗器词族必须同时在设备表与 ② 前缀剥离表内。"""
    for w in _WINDOW_OPENER_WORDS:
        assert w in set(T.KNOWN_DEVICES_PREFIX), f"{w} 未进 KNOWN_DEVICES_PREFIX"
        assert w in set(T.KNOWN_DEVICES), f"{w} 未进 KNOWN_DEVICES"
    assert _opener_word("窗帘开合器") is None   # 帘族上下文裁决在调用侧


# ── ② 一句话多设备（链发）──────────────────────────────────────
def _pipe():
    ex = RecExecutor()
    p = Pipeline(_S(), HA(), FakeScenes(), _tc_singleton(), ex,
                 agent=None, klar=Klar())
    return p, ex


_TC = None


def _tc_singleton():
    global _TC
    if _TC is None:
        _TC = TextCNN(os.environ["HUIJIAN_NLU_DATA"])
        _TC._ensure()
    return _TC


COMPOUND_TEXTS = [
    "打开办公室空调，并关闭办公室平开窗",
    "打开办公室空调，关闭办公室平开窗",
    "打开办公室空调并关闭办公室平开窗",
    "打开办公室空调、关闭办公室平开窗",
    "打开办公室空调关闭办公室平开窗",           # 无标点动词连排
    "打开办公室空调，同时关闭办公室平开窗",
    "打开办公室空调，并且关闭办公室平开窗",
]


@pytest.mark.parametrize("text", COMPOUND_TEXTS)
def test_two_device_one_sentence_chains(text):
    p, ex = _pipe()

    async def go():
        return await p.handle(text, origin="t")
    r = asyncio.run(go())
    assert r.ok and r.source == "chain", f"{text!r} → src={r.source} trace={r.trace}"
    assert len(ex.plans) == 1
    head = ex.plans[0]
    assert head.intent == "TurnDeviceOn"
    assert head.extra_steps and head.extra_steps[0]["name"] == "ControlWindow"
    step_tgt = head.extra_steps[0]["args"]["target"][0]
    assert step_tgt["area"] == "办公室"
    assert step_tgt["devices"][0]["name"] == "平开窗"


def test_chain_with_opener_clause_keeps_full_name():
    p, ex = _pipe()
    r = asyncio.run(p.handle("打开办公室空调，并关闭3号开窗器", origin="t"))
    assert r.source == "chain"
    step = ex.plans[0].extra_steps[0]
    assert step["name"] == "ControlWindow"
    assert step["args"]["target"][0]["devices"][0]["name"] == "3号开窗器"


def test_three_device_one_sentence_chains():
    p, ex = _pipe()
    r = asyncio.run(p.handle(
        "打开办公室空调、关闭办公室平开窗、关闭办公室窗帘", origin="t"))
    assert r.source == "chain", r.trace
    assert len(ex.plans[0].extra_steps) == 2
    names = [ex.plans[0].intent] + [s["name"] for s in ex.plans[0].extra_steps]
    assert names == ["TurnDeviceOn", "ControlWindow", "TurnDeviceOff"]


def test_overflow_serial_refuses_silently_not_half_executes():
    """>8 段超限退回单发：_SERIAL_RESIDUE 守卫必须拒收，绝不执行半截。"""
    p, ex = _pipe()
    text = ("打开空调、关窗、关灯、开新风、关窗帘、开窗、关空调、"
            "开电视、关电视、开加湿器")
    r = asyncio.run(p.handle(text, origin="t"))
    # 真安全属性：一个执行 plan 都没进 executor（旧版会把首段当整句半执行）。
    assert ex.plans == [], f"超限句被半执行：{[pl.intent for pl in ex.plans]}"
    assert r.source == "fallback", r.source


# ── ③ 场景/自动化多设备动作 ────────────────────────────────────
def test_scene_two_actions_with_window():
    p, ex = _pipe()
    r = asyncio.run(p.handle(
        "当我说我有点热，就打开办公室空调、关闭办公室平开窗", origin="t"))
    assert r.ok and ex.plans[0].intent == "HassCreateVoiceScene"
    acts = ex.plans[0].args["actions"]
    assert [a["intent"] for a in acts] == ["TurnDeviceOn", "ControlWindow"]
    assert "我有点热" in r.text


def test_scene_five_actions_and_opener():
    p, ex = _pipe()
    r = asyncio.run(p.handle(
        "当我说我有点热，就打开办公室空调、关闭办公室平开窗、关闭办公室窗帘、"
        "关闭办公室新风、关掉开窗器", origin="t"))
    assert r.ok, r.text
    acts = ex.plans[0].args["actions"]
    assert len(acts) == 5
    assert acts[4]["intent"] == "ControlWindow"
    assert acts[4]["params"]["target"][0]["devices"][0]["name"] == "开窗器"


def test_scene_trigger_without_comma_no_anchor_still_creates():
    """ASR 无标点吞「就/帮我」：_SCENE_RE2 动作字锚点路径 + 多动作。"""
    p, ex = _pipe()
    r = asyncio.run(p.handle(
        "当我说我有点热打开办公室空调并关闭办公室平开窗", origin="t"))
    assert r.ok and ex.plans, r.text
    acts = ex.plans[0].args["actions"]
    assert [a["intent"] for a in acts] == ["TurnDeviceOn", "ControlWindow"]


def test_scene_overflow_actions_rejected_not_half_built():
    p, ex = _pipe()
    clauses = ["打开空调", "关闭平开窗", "关闭窗帘", "关闭新风", "关掉开窗器",
               "打开电视", "关闭电视", "打开加湿器", "关闭加湿器"]
    r = asyncio.run(p.handle("当我说有点忙，就" + "、".join(clauses), origin="t"))
    assert not r.ok or ex.plans == []
    if not r.ok:
        assert "先不创建" in r.text or "没听懂" in r.text
    for pl in ex.plans:
        assert pl.intent != "HassCreateVoiceScene" or \
            len(pl.args["actions"]) != 1, "超限句被建成单动作半截场景"


def test_automation_multi_actions_with_opener():
    p, ex = _pipe()
    r = asyncio.run(p.handle(
        "当客厅温度超过28度，就打开办公室空调并关闭3号开窗器", origin="t"))
    assert r.ok and ex.plans[0].intent == "HassCreateAutomation"
    acts = ex.plans[0].args["actions"]
    assert [a["intent"] for a in acts] == ["TurnDeviceOn", "ControlWindow"]
    assert acts[1]["params"]["target"][0]["devices"][0]["name"] == "3号开窗器"


def test_scene_parse_trigger_with_verb_like_phrase():
    """「当我说我要通风…」贪婪 X 钉（旧懒惰 X 腰斩成 trigger="我"）：
    触发词内含 要/把/给 动词头的句子不得截胡；常规「吃饭的时候把灯打开」
    把=连接词形态同步不回退。"""
    p = cr.parse("当我说我要通风，就打开客厅上悬窗")
    assert p and p["trigger_phrase"] == "我要通风" and p["y"] == "打开客厅上悬窗"
    p = cr.parse("帮我创建一个语音场景，当我说吃饭的时候把餐厅灯打开")
    assert p and p["trigger_phrase"] == "吃饭" and p["y"] == "餐厅灯打开"
    p = cr.parse("当我说我回来了就打开客厅灯")
    assert p and p["trigger_phrase"] == "我回来了"


def test_split_actions_caps_and_validity():
    # 上限 8：9 段退化整句（由残渣守卫/级联拒收兜底）
    nine = "、".join(f"关闭{i}号灯" for i in range(9))
    assert cr.split_actions(nine) == [nine]
    six = "、".join(f"关闭{i}号灯" for i in range(6))
    assert len(cr.split_actions(six)) == 6
    # 「暂停播放器」不被 播放 切点腰斩（2026-09 过切修）
    assert cr.split_actions("暂停播放器") == ["暂停播放器"]
    assert cr.split_actions("关闭电视暂停播放器") == ["关闭电视", "暂停播放器"]


# ── ④ LLM 面对齐（数据集规则同构）──────────────────────────────
def test_llm_prompt_carries_window_and_multi_rules():
    from core.agent import SYSTEM_PROMPT, TOOLS
    assert "开窗器" in SYSTEM_PROMPT and "ControlWindow" in SYSTEM_PROMPT
    assert "button" in SYSTEM_PROMPT and "cover" in SYSTEM_PROMPT
    assert "多个" in SYSTEM_PROMPT                      # 多设备同轮多调用指引
    cw = next(t for t in TOOLS if t["function"]["name"] == "ControlWindow")
    desc = cw["function"]["description"]
    assert "开合器" in desc and "绝不用 TurnDeviceOn" in desc
    sc = next(t for t in TOOLS if t["function"]["name"] == "HassCreateVoiceScene")
    assert "多个设备" in sc["function"]["description"]


def test_device_brief_includes_window_buttons():
    from core.agent import Agent
    st = _S()

    async def _run():
        ha = HA(states={
            "light.office": {"state": "on", "attributes": {"friendly_name": "办公室射灯"}},
            "button.office_open": {"state": "unknown",
                                   "attributes": {"friendly_name": "办公室平开窗开窗器"}},
            "button.ambient": {"state": "unknown",
                               "attributes": {"friendly_name": "氛围灯切换"}},
            "cover.cur": {"state": "closed", "attributes": {"friendly_name": "办公室窗帘"}},
        })
        a = Agent(st, ha, None)
        return await a._device_brief()
    brief = asyncio.run(_run())
    assert "办公室平开窗开窗器[button(窗)]" in brief, brief
    assert "办公室窗帘[cover]" in brief, brief
    assert "氛围灯切换" not in brief                     # 无窗语义的 button 不挤简报


# ── ⑤ 集成侧源码契约钉（测试环境无 HA，钉源文本，同窗速钉风格）────
def test_integration_extract_window_name_opener_fallback():
    src = (HERE / "custom_components" / "huijian_ai" /
           "intent_window_const.py").read_text(encoding="utf-8")
    # ① 开合器兜底必须**后置**在映射循环之后（先具体后泛称的既有优先级不动）
    assert 'if "开合器" in name_lower' in src, "extract_window_name 缺开合器兜底"
    assert src.index('if "开合器" in name_lower') > src.index("WINDOW_NAME_MAPPING.items()")
    # ② 铁律：开窗器词族绝不进 WINDOW_NAME_MAPPING 本体——加键即进
    # WINDOW_ALL_NAMES，_build_conflict_names 会误杀含"窗"设备名的匹配。
    mapping_block = src[src.index("WINDOW_NAME_MAPPING = {"):
                        src.index("WINDOW_ALL_NAMES")]
    for w in ("开窗器", "开合器", "推窗器"):
        assert f'"{w}"' not in mapping_block, f"{w} 混入 WINDOW_NAME_MAPPING（冲突名误杀风险）"


# ── ⑥ 悬窗族/提升窗 + 通用智能家居设备词（2026-09 第二轮）─────────
TYPE_CASES = [
    ("关闭下悬窗", "close", "下悬窗", None),
    ("上悬窗打开", "open", "上悬窗", None),
    ("关闭客厅上悬窗", "close", "上悬窗", "客厅"),
    ("打开提升窗", "open", "提升窗", None),
    ("客厅提升窗关闭", "close", "提升窗", "客厅"),
    ("关掉悬窗", "close", "悬窗", None),
]


@pytest.mark.parametrize("text,action,name,area", TYPE_CASES)
def test_hanging_window_family(fp, text, action, name, area):
    p = _m(fp, text)
    assert p is not None and p.intent == "ControlWindow", f"{text!r}→{p}"
    tgt = p.args["target"][0]
    assert tgt["devices"][0]["name"] == name, f"{text!r} name 被短词截胡"
    if area:
        assert tgt.get("area") == area


def test_window_type_tuple_long_before_short():
    """_WINDOW_TYPES 是先命中先返回的线性扫描，下悬窗/上悬窗/提升窗必须排在
    裸 悬窗 之前，否则子串截胡丢编号语义（同 内开内倒窗 教训）。"""
    from core.nlu.fast_path import _WINDOW_TYPES as W
    for long_w in ("下悬窗", "上悬窗", "提升窗"):
        assert W.index(long_w) < W.index("悬窗")


def test_robo_devices_not_window_lane(fp):
    """含窗字的清洁家电绝不进按压窗道：擦窗机器人 → Turn + vacuum。"""
    p = _m(fp, "打开擦窗机器人")
    assert p is not None and p.intent == "TurnDeviceOn"
    dev = p.args["target"][0]["devices"][0]
    assert dev["name"] == "擦窗机器人" and dev["domains"] == ["vacuum"]


DEVICE_CASES = [
    ("关闭洗地机", "TurnDeviceOff", "洗地机", ["vacuum"]),
    ("打开除湿机", "TurnDeviceOn", "除湿机", ["humidifier"]),
    ("打开新风机", "TurnDeviceOn", "新风机", []),
    ("关闭办公室浴霸", "TurnDeviceOff", "浴霸", []),
    ("打开插座", "TurnDeviceOn", "插座", ["switch"]),
    ("关闭摄像头", "TurnDeviceOff", "摄像头", ["camera"]),
    ("开一下音响", "TurnDeviceOn", "音响", ["media_player"]),
    ("打开空气炸锅", "TurnDeviceOn", "空气炸锅", []),
    ("关闭扫地机器人", "TurnDeviceOff", "扫地机器人", ["vacuum"]),
]


@pytest.mark.parametrize("text,intent,name,domains", DEVICE_CASES)
def test_general_smart_home_vocab(fp, text, intent, name, domains):
    p = _m(fp, text)
    assert p is not None and p.intent == intent, f"{text!r}→{p}"
    dev = p.args["target"][0]["devices"][0]
    assert dev["name"] == name and dev["domains"] == domains


def test_door_lock_lane(fp):
    p = _m(fp, "锁上卧室门锁")
    assert p is not None and p.intent == "HassLock"
    tgt = p.args["target"][0]
    assert tgt["devices"][0]["name"] == "门锁"
    assert tgt["devices"][0]["domains"] == ["lock"]
    assert tgt.get("area") == "卧室"


def test_chain_mixes_new_devices():
    p, ex = _pipe()
    r = asyncio.run(p.handle("打开洗地机，关闭客厅下悬窗", origin="t"))
    assert r.source == "chain", r.trace
    step = ex.plans[0].extra_steps[0]
    assert step["name"] == "ControlWindow"
    assert step["args"]["target"][0]["devices"][0]["name"] == "下悬窗"
    assert ex.plans[0].args["target"][0]["devices"][0]["domains"] == ["vacuum"]


def test_scene_with_hanging_window_action():
    p, ex = _pipe()
    r = asyncio.run(p.handle(
        "当我说我要通风，就打开客厅上悬窗、关闭办公室平开窗", origin="t"))
    assert r.ok and ex.plans[0].intent == "HassCreateVoiceScene"
    acts = ex.plans[0].args["actions"]
    assert [a["intent"] for a in acts] == ["ControlWindow", "ControlWindow"]
    assert acts[0]["params"]["target"][0]["devices"][0]["name"] == "上悬窗"


def test_integration_hanging_window_sync_source_pins():
    """集成两表与加内 _WINDOW_TYPES 三方同步 + 键序（extract 先命中先返回）。"""
    const = (HERE / "custom_components" / "huijian_ai" /
             "intent_window_const.py").read_text(encoding="utf-8")
    shared = (HERE / "custom_components" / "huijian_ai" /
              "intent_device_shared.py").read_text(encoding="utf-8")
    mapping = const[const.index("WINDOW_NAME_MAPPING = {"):const.index("WINDOW_ALL_NAMES")]
    for w in ("下悬窗", "上悬窗", "提升窗", "悬窗"):
        assert f'"{w}"' in mapping, f"{w} 缺 WINDOW_NAME_MAPPING"
        assert f'"{w}"' in shared, f"{w} 缺 WINDOW_KEYWORDS"
    assert mapping.index('"下悬窗"') < mapping.index('"悬窗"')   # 键序截胡钉
    assert '"机器人"' in shared                                   # 含窗家电排除钉


# ── 2026-09-21 用户三连问（上下文回指 / NL→动态实体可靠性 / 并列多窗）──────

def test_context_chain_absolute_brightness():
    """「打开办公室射灯」→「调高亮度到80%」：绝对值不丢 + 继承上一目标。"""
    p, ex = _pipe()
    asyncio.run(p.handle("打开办公室射灯", origin="cc-1"))
    ex.plans.clear()
    r = asyncio.run(p.handle("调高亮度到80%", origin="cc-1"))
    assert r.source != "fallback", r.trace
    pl = ex.plans[0]
    assert pl.intent == "AdjustDeviceAttribute"
    assert pl.args["delta"] == "80"          # 旧缺陷：被 (调高)→+20 吞成相对档
    d = pl.args["target"][0]["devices"][0]
    assert d["name"] == "射灯"                # 上下文继承，不是幻觉设备


def test_attr_residual_never_hallucinates_device():
    """⑦拼音幻觉事故形：「亮度到80%」fresh 会话不得凭空配出"浴霸"类目标。"""
    p, ex = _pipe()
    r = asyncio.run(p.handle("调高亮度到80%", origin="fresh-iso-zz"))
    for pl in ex.plans:
        blob = str(pl.args)
        assert "浴霸" not in blob and "亮度" not in blob, blob


def test_brightness_half_word():
    p, ex = _pipe()
    asyncio.run(p.handle("打开办公室射灯", origin="half-1"))
    ex.plans.clear()
    asyncio.run(p.handle("亮度调到一半", origin="half-1"))
    assert ex.plans[0].args["delta"] == "50"


def test_coord_parallel_two_windows_chain():
    """用户令第③点：「打开展厅内倒窗和推拉窗」必须两扇都开（旧=只开一扇谎报）。"""
    p, ex = _pipe()
    r = asyncio.run(p.handle("打开展厅内倒窗和推拉窗", origin="cp-1"))
    assert r.source == "chain", r.source
    pl = ex.plans[0]
    steps = [(pl.intent, pl.args)] + [(s["name"], s["args"]) for s in pl.extra_steps]
    assert len(steps) == 2 and all(i == "ControlWindow" for i, _ in steps)
    names = [a["target"][0]["devices"][0]["name"] for _, a in steps]
    assert names == ["内倒窗", "推拉窗"]
    assert all(a["target"][0]["area"] == "展厅" for _, a in steps)  # 共享区域回填


def test_coord_parallel_mixed_domains():
    p, ex = _pipe()
    r = asyncio.run(p.handle("打开办公室的空调和射灯", origin="cp-2"))
    assert r.source == "chain"
    pl = ex.plans[0]
    assert pl.intent == "TurnDeviceOn"
    assert len(pl.extra_steps) == 1
    assert pl.args["target"][0]["devices"][0]["name"] == "空调"
    assert pl.extra_steps[0]["args"]["target"][0]["devices"][0]["name"] == "射灯"


def test_coord_unknown_piece_honest_refuse():
    """右片不认识 → 整句拒猜；绝不"吃左片执行+谎报成功"（半执行）。"""
    p, ex = _pipe()
    r = asyncio.run(p.handle("打开展厅内倒窗和不存在的xyz东西", origin="cp-3"))
    assert ex.plans == []
    assert not r.ok or "不会" in r.text or "听" in r.text


@pytest.mark.parametrize("t", ["打开开窗器", "关闭加湿器", "打开展厅的内开窗",
                               "打开调和模式", "关闭推窗器"])
def test_coord_negative_no_misfire(t):
    """含"和"字形/窗族长词不许被并列展开误伤（加湿器含和字、开窗器整词）。"""
    p, ex = _pipe()
    r = asyncio.run(p.handle(t, origin="cp-4"))
    assert r.source != "chain" or t in (), t


def test_neidao_window_recognized():
    """「内倒窗」现场简称五表同步后：打开/关闭形完整识别为 ControlWindow。"""
    p, ex = _pipe()
    for t, act in [("打开展厅内倒窗", "open"), ("关闭内倒窗", "close")]:
        ex.plans.clear()
        asyncio.run(p.handle(t, origin="nd-1"))
        pl = ex.plans[0]
        assert pl.intent == "ControlWindow", (t, pl.intent)
        assert pl.args["action"] == act
        assert pl.args["target"][0]["devices"][0]["name"] == "内倒窗"


def test_scene_coord_parallel_actions():
    """场景/自动化同纪律：「就打开内倒窗和推拉窗」→ 两个独立 action。"""
    p, ex = _pipe()
    r = asyncio.run(p.handle("当我说有点热，就打开内倒窗和推拉窗", origin="sc-1"))
    assert r.source == "creation" and ex.plans[0].intent == "HassCreateVoiceScene"
    acts = ex.plans[0].args["actions"]
    assert [a["intent"] for a in acts] == ["ControlWindow", "ControlWindow"]
    assert [a["params"]["target"][0]["devices"][0]["name"] for a in acts] == \
        ["内倒窗", "推拉窗"]


def test_coord_source_pins():
    """安全形态源头钉：拼音档属性词禁入 + 集成映射长词序。"""
    tg = (HERE / "core" / "nlu" / "targets.py").read_text(encoding="utf-8")
    assert "_ATTR_NO_PINYIN" in tg and "coord_refuse" in tg
    assert "tol = 1 if len(py_dev) <= 5 else 2" in tg   # 短拼音容差收紧钉
    const = (HERE / "custom_components" / "huijian_ai" /
             "intent_window_const.py").read_text(encoding="utf-8")
    mapping = const[const.index("WINDOW_NAME_MAPPING = {"):const.index("WINDOW_ALL_NAMES")]
    assert mapping.index('"外装平开窗"') < mapping.index('"平开窗"')  # 截胡修复钉
    assert mapping.index('"内开内倒窗"') < mapping.index('"内倒窗"')
    assert '"内倒窗"' in (HERE / "custom_components" / "huijian_ai" /
                          "intent_device_shared.py").read_text(encoding="utf-8")


# ── 2026-09-21 二批：SOV 尾动/顿号/语气词并列（同族半执行洞）──────────

@pytest.mark.parametrize("t", ["内倒窗和推拉窗打开", "把内倒窗和推拉窗打开"])
def test_coord_sov_parallel_both_open(t):
    """SOV「A和B打开」：T0 单发吃一扇+「内倒」动作a污染整句——必链发两全。"""
    p, ex = _pipe()
    r = asyncio.run(p.handle(t, origin="sov-p"))
    assert r.source == "chain", (t, r.source)
    pl = ex.plans[0]
    steps = [(pl.intent, pl.args)] + [(s["name"], s["args"]) for s in pl.extra_steps]
    assert len(steps) == 2
    names = [a["target"][0]["devices"][0]["name"] for _, a in steps]
    assert names == ["内倒窗", "推拉窗"]
    assert all(a["action"] == "open" for _, a in steps)   # a 污染根除：分片各裁


def test_coord_sov_close_direction():
    p, ex = _pipe()
    asyncio.run(p.handle("内开窗和推拉窗关闭", origin="sov-c"))
    pl = ex.plans[0]
    assert len(pl.extra_steps) == 1
    assert all(a.get("action") == "close"
               for a in [pl.args] + [s["args"] for s in pl.extra_steps])


def test_coord_comma_ellipsis_and_modal():
    """顿号省略「打开A、B」与语气尾巴「…吧」都要两全（strip_modal 片尾剥离）。"""
    p, ex = _pipe()
    asyncio.run(p.handle("打开平开窗、推拉窗", origin="ce-1"))
    assert len(ex.plans[0].extra_steps) == 1
    ex.plans.clear()
    r = asyncio.run(p.handle("打开展厅内倒窗和推拉窗吧", origin="ce-2"))
    assert r.source == "chain"
    assert ex.plans[0]["target"][0]["area"] == "展厅" if isinstance(
        ex.plans[0], dict) else ex.plans[0].args["target"][0]["area"] == "展厅"


def test_coord_sov_unknown_piece_still_refuses():
    """SOV 半执行同闸：右片不认识 → 一扇都不许开。"""
    p, ex = _pipe()
    asyncio.run(p.handle("内倒窗和不存在的xyz打开", origin="sov-neg"))
    assert ex.plans == []


def test_scene_sov_coord():
    """场景 Y 段 SOV 并列同纪律。"""
    p, ex = _pipe()
    r = asyncio.run(p.handle("当我说有点闷，就把内倒窗和推拉窗打开", origin="sc-sov"))
    assert r.source == "creation"
    acts = ex.plans[0].args["actions"]
    assert [a["intent"] for a in acts] == ["ControlWindow", "ControlWindow"]
    assert [a["params"]["target"][0]["devices"][0]["name"] for a in acts] == \
        ["内倒窗", "推拉窗"]
