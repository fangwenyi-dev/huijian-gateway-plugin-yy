# -*- coding: utf-8 -*-
"""2026-10-01 数据集对账二期钉——两份慧尖数据集全量比对后落地的四类缺口。

对账口径：SFT合并 350 行 + 完整数据集 436 行 = 786 user 句 / 798 assistant
payload（HassCreateVoiceScene 的嵌套 actions[] 已递归展开，否则口径低估）。

① **数词裸替换腐蚀设备名**（双端同根）：表内既有词「百叶窗」被 normalize 成
   「100叶窗」→ 实测「客厅百叶窗关闭」等所有句式整句失能；「百叶帘」→「100叶帘」
   连 domains 一起丢。插件 targets.normalize_name 与集成 normalize_chinese_numbers
   各一份，同闸同判据（数词仅在**后接索引量词**时转；整串纯数词仍转）。
② **具名设备词从未入表**：灯具族（主灯/吊灯/阅读灯/镜前灯/感应灯/工业灯/大灯）
   一律折成泛称「灯」=无区域时整屋灯扇出；帘族（卷帘/百叶帘）与扇族（落地扇）
   parse score=0 硬失败；空调机型族（中央/挂机/柜机）折成裸「空调」后被
   「空调缺区域」守卫整句拦死（实测 打开中央空调=MISS）。
③ **域提示缺词尾族**：卷帘/落地扇 domains=[] → 集成端只能全实体面找名。
④ **模式族动词条与 preset 族漂移**：同句「设为睡眠模式」出档、「设成送风」落 MISS。
"""
import asyncio
import importlib.util
import io
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_ds2_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

from core.nlu import targets as T                              # noqa: E402
from core.nlu.fast_path import FastPath, _WINDOW_TYPES, _window_type  # noqa: E402
from core.settings import Settings                             # noqa: E402

CC = HERE / "custom_components" / "huijian_ai"


@pytest.fixture(autouse=True)
def _static_vocab():
    T.clear_vocab()
    yield
    T.clear_vocab()


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
def fp():
    return FastPath(FakeScenes(), None,
                    Settings(Path(os.environ["HUIJIAN_DATA"]) / f"ds2-{os.getpid()}.json"))


def _match(fp, text):
    return asyncio.run(fp.match(text))


def _integration_const():
    """集成侧常量模块（本栈无 homeassistant，按仓内既有姿势 stub 掉两个符号）。"""
    names = {
        "homeassistant": types.ModuleType("homeassistant"),
        "homeassistant.components": types.ModuleType("homeassistant.components"),
        "homeassistant.components.button": types.ModuleType("homeassistant.components.button"),
        "homeassistant.components.input_button": types.ModuleType("homeassistant.components.input_button"),
        "homeassistant.components.button.const": types.ModuleType("homeassistant.components.button.const"),
    }
    names["homeassistant.components.button.const"].DOMAIN = "button"
    names["homeassistant.components.input_button"].DOMAIN = "input_button"
    saved = dict(sys.modules)
    sys.modules.update(names)
    try:
        spec = importlib.util.spec_from_file_location("iwc_ds2", CC / "intent_window_const.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k in names:
            if k not in saved:
                sys.modules.pop(k, None)


# ── ① 数词闸（双端同判据）──────────────────────────────────────────
NUM_INDEX_CASES = [
    ("一号测试窗", "1号测试窗"),      # 既有语义必须保住（test_targets 原钉）
    ("五号窗", "5号窗"),
    ("二十三号", "23号"),
    ("一百二十号", "120号"),
    ("二十三", "23"),                # 整串纯数词仍转
]
NUM_LEXICAL_CASES = ["百叶窗", "百叶帘", "千叶灯", "三合板", "百合花"]


@pytest.mark.parametrize(("raw", "want"), NUM_INDEX_CASES)
def test_normalize_name_index_forms(raw, want):
    assert T.normalize_name(raw) == want


@pytest.mark.parametrize("raw", NUM_LEXICAL_CASES)
def test_normalize_name_does_not_corrupt_lexical_numerals(raw):
    """词汇化数字一律原样——「百叶窗→100叶窗」是本批的原始事故形态。"""
    assert T.normalize_name(raw) == raw


def test_normalize_name_keeps_table_words_verbatim():
    """表内词恒等（第二道闸）：任何含数字的具名设备词都不得被改写。"""
    for w in ["百叶窗", "百叶帘", "卷帘", "电动窗帘", "主灯", "吊灯", "中央空调"]:
        assert w in set(T.KNOWN_DEVICES), w
        assert T.normalize_name(w) == w, w


def test_integration_numeral_gate_same_contract():
    """集成侧同闸（intent_helper/intent_turn/intent_window_control 三处会再过一次
    这道归一，只修插件侧=名字在本侧被二次腐蚀）。"""
    iwc = _integration_const()
    for raw, want in NUM_INDEX_CASES:
        assert iwc.normalize_chinese_numbers(raw) == want, raw
    for raw in NUM_LEXICAL_CASES:
        assert iwc.normalize_chinese_numbers(raw) == raw, raw


# ── ② 具名设备词入表（数据集 payload name 实测词）──────────────────
DATASET_NAMES = [
    ("主灯", ["light"]), ("大灯", ["light"]), ("吊灯", ["light"]),
    ("阅读灯", ["light"]), ("镜前灯", ["light"]), ("感应灯", ["light"]),
    ("工业灯", ["light"]),
    ("卷帘", ["cover"]), ("百叶帘", ["cover"]),
    ("落地扇", ["fan"]),
    ("中央空调", ["climate"]), ("挂机空调", ["climate"]), ("柜机空调", ["climate"]),
    ("电风扇", ["fan"]), ("投影仪", ["media_player"]), ("空气净化器", ["fan", "humidifier"]),
]


@pytest.mark.parametrize(("word", "domains"), DATASET_NAMES)
def test_dataset_device_words_parse_verbatim(word, domains):
    """折成泛称=过宽扇出、score=0=硬失败，两种都算未收；域提示必须同批到位。"""
    assert word in set(T.KNOWN_DEVICES), f"{word} 未入 KNOWN_DEVICES"
    assert word in set(T.KNOWN_DEVICES_PREFIX), f"{word} 未入前缀表（SOV 尾动形会丢）"
    a, n, score = T.parse_target(word)
    assert n == word, (word, a, n, score)
    assert T.domain_hint(word) == domains, (word, T.domain_hint(word))


@pytest.mark.parametrize("word", [w for w, _ in DATASET_NAMES] + ["百叶窗"])
def test_curtain_and_model_words_stay_out_of_window_tables(word):
    """帘族/机型/灯具一律**不是窗型**：进 _WINDOW_TYPES 或集成窗表=按窗钮假动作。"""
    assert word not in _WINDOW_TYPES, word
    assert _window_type(word) is None, word
    src = (CC / "intent_window_const.py").read_text(encoding="utf-8")
    mapping = src[src.index("WINDOW_NAME_MAPPING = {"):src.index("WINDOW_ALL_NAMES")]
    assert f'"{word}"' not in mapping, f"{word} 混入 WINDOW_NAME_MAPPING"
    shared = (CC / "intent_device_shared.py").read_text(encoding="utf-8")
    blk = shared[shared.index("WINDOW_KEYWORDS"):shared.index("]")]
    assert f'"{word}"' not in blk, f"{word} 混入 WINDOW_KEYWORDS"


def test_bare_ac_still_requires_area_but_named_models_do_not(fp):
    """「空调缺区域」守卫是设计性拒收，不得被本批加词顶翻；具名机型自带限定豁免
    （_ac_name_qualified 既有判据：剥掉「空调」后仍剩实义汉字）。"""
    assert _match(fp, "打开空调") is None
    p = _match(fp, "打开中央空调")
    assert p is not None and p.intent == "TurnDeviceOn"
    dev = p.args["target"][0]["devices"][0]
    assert dev["name"] == "中央空调" and dev["domains"] == ["climate"], p.args


# ── 数据集原句端到端（payload 与话术双向对上）──────────────────────
DATASET_SENTENCES = [
    ("关闭客厅百叶帘", "TurnDeviceOff", "客厅", "百叶帘", ["cover"]),
    ("打开办公室的百叶帘", "TurnDeviceOn", "办公室", "百叶帘", ["cover"]),
    ("打开书房的卷帘", "TurnDeviceOn", "书房", "卷帘", ["cover"]),
    ("打开卧室的主灯", "TurnDeviceOn", "卧室", "主灯", ["light"]),
    ("关闭吊灯", "TurnDeviceOff", None, "吊灯", ["light"]),
    ("打开客厅的落地扇", "TurnDeviceOn", "客厅", "落地扇", ["fan"]),
    ("打开空气净化器", "TurnDeviceOn", None, "空气净化器", ["fan", "humidifier"]),
    ("打开客厅百叶窗", "TurnDeviceOn", "客厅", "百叶窗", ["cover"]),
]


@pytest.mark.parametrize(("sentence", "intent", "area", "name", "domains"),
                         DATASET_SENTENCES)
def test_dataset_sentences_reproduce_payload_names(fp, sentence, intent, area, name, domains):
    p = _match(fp, sentence)
    assert p is not None and p.intent == intent, sentence
    tgt = p.args["target"][0]
    assert tgt.get("area") == area, (sentence, tgt)
    dev = tgt["devices"][0]
    assert dev["name"] == name and dev["domains"] == domains, (sentence, dev)


TAIL_VERB_CASES = [                       # 区域+具名设备+尾动词（④ 前缀剥离车道）
    ("卷帘拉上", "TurnDeviceOff", None, "卷帘"),
    ("百叶帘拉上", "TurnDeviceOff", None, "百叶帘"),
    ("客厅卷帘拉上", "TurnDeviceOff", "客厅", "卷帘"),
    ("客厅百叶窗关闭", "TurnDeviceOff", "客厅", "百叶窗"),
    ("主灯打开", "TurnDeviceOn", None, "主灯"),
    ("中央空调关闭", "TurnDeviceOff", None, "中央空调"),
]


@pytest.mark.parametrize(("sentence", "intent", "area", "name"), TAIL_VERB_CASES)
def test_sov_tail_verb_forms(fp, sentence, intent, area, name):
    p = _match(fp, sentence)
    assert p is not None and p.intent == intent, sentence
    tgt = p.args["target"][0]
    assert tgt.get("area") == area, (sentence, tgt)
    assert tgt["devices"][0]["name"] == name, (sentence, tgt)


def test_curtain_wordorder_lane_keeps_window_semantics(fp):
    """_COVER_HEAD 扩帘族具名词，但窗户形态必须仍走窗型纠正（扩词不得改道）。"""
    p = _match(fp, "卧室窗户关上")
    assert p is not None and p.intent == "ControlWindow", p
    q = _match(fp, "客厅窗帘拉上")
    assert q is not None and q.intent == "TurnDeviceOff"
    assert q.args["target"][0]["devices"][0]["domains"] == ["cover"], q.args


# ── ③ SOV/SVO 那道动词表与方向表的漂移（数据集原句「放下投影幕布」）──
CURTAIN_SOV_CASES = [
    ("放下窗帘", "窗帘"),             # _COVER_CLOSE_WORDS 早认「放下」为关闭向，
    ("卧室百叶窗放下", "百叶窗"),      # 语序归一道却漏 → 整句落空（同型漂移）
    ("放下投影幕布", "幕布"),          # 数据集原句（影院模式场景）
    ("幕布放下", "幕布"),
    ("拉上投影幕布", "幕布"),
]


@pytest.mark.parametrize(("sentence", "name"), CURTAIN_SOV_CASES)
def test_curtain_verb_lane_closes_lexical_drift(fp, sentence, name):
    p = _match(fp, sentence)
    assert p is not None and p.intent == "TurnDeviceOff", (sentence, p)
    dev = p.args["target"][0]["devices"][0]
    assert dev["name"] == name and dev["domains"] == ["cover"], (sentence, dev)


def test_screen_domain_precedes_projector():
    """幕布 必须抢在 投影 之前判域：HA 生态「投影幕布」=cover，「投影仪」=media_player。
    顺序写反即实锤错域（集成按媒体设备找幕布必 miss）。"""
    assert T.domain_hint("投影幕布") == ["cover"]
    assert T.domain_hint("幕布") == ["cover"]
    assert T.domain_hint("投影仪") == ["media_player"]
    assert T.domain_hint("投影") == ["media_player"]


# ── ④ 模式族动词条与 preset 族拉齐（数据集原句「空调调成制冷模式」等）──
MODE_CASES = [
    ("客厅空调调成制冷模式", "cool"),   # 数据集原形 +区域（原句无区域，见下方守卫钉）
    ("主卧空调调成除湿模式", "dry"),
    ("客厅空调设成送风", "fan_only"),
    ("主卧空调换成制热", "heat"),
    ("厨房空调切到自动", "auto"),
    ("卧室空调设成睡眠模式", "sleep"),   # preset 族一直可用=漂移的反证
]


@pytest.mark.parametrize(("sentence", "mode"), MODE_CASES)
def test_mode_verb_parity(fp, sentence, mode):
    """同一话术在 preset 族（睡眠/节能…）可用、在模式族（制冷/制热/送风/除湿/
    自动）半套动词下落 MISS=口径漂移，本批按 preset 长表拉齐。"""
    p = _match(fp, sentence)
    assert p is not None and p.intent == "SetDeviceMode", (sentence, p)
    assert p.args.get("mode") == mode, (sentence, p.args)
    dev = p.args["target"][0]["devices"][0]
    assert dev["name"] == "空调" and dev["domains"] == ["climate"], (sentence, p.args)


def test_bare_ac_guard_narrowed_to_power_lane(fp):
    """v1.1.1 数据集对账：「空调缺区域」守卫**收窄到开关族**，模式/属性句放开。

    原 v1.1.0 钉把三条裸空调句一律拒收，代价是数据集 14 句打死（「空调调成制冷
    模式」② 已正确产 SetDeviceMode cool 仍被整句作废、「把空调风速调大」T1
    SetFanSpeed 1.00 同样作废）。分域判据不是放松红线，而是把红线钉在真正的
    实害面上：
      · 开关（Turn*）=改变**别人家**设备电源态，跨房间实害且不可一样撤回 → 仍拒；
      · 模式/参数=全屋同向语义一致，且数据集对这些句的期望 target 本就是
        {domains:[climate]} 无区域（单空调户是唯一 sane 解），说错了一句口令即可
        改回 → 放行。
    多空调户的可见影响已在 v1.1.1 发布说明单列，由产品负责人签字确认。
    """
    for sentence, mode in (("空调调成制冷模式", "cool"), ("空调调成除湿模式", "dry")):
        p = _match(fp, sentence)
        assert p is not None and p.intent == "SetDeviceMode", (sentence, p)
        assert p.args.get("mode") == mode, (sentence, p.args)
        dev = p.args["target"][0]["devices"][0]
        assert dev["name"] == "空调" and dev["domains"] == ["climate"], (sentence, p.args)
    # 开关族一字未动：这两句复现 2026-09 事故形态（扇出+假成功），必须继续 MISS
    assert _match(fp, "打开空调") is None
    assert _match(fp, "关闭空调") is None


# ── 护栏：加词后区域名与新设备词不得跨词根互吞（与一期同型风险）──────
def test_new_device_words_do_not_collide_with_area_prefixes():
    """BASE_AREAS ∪ 动态区名 × 「灯窗门扇机」：区域必须完整析出、设备字留在名上。
    新词入表后（吊灯/主灯/大灯…）这条是防「阳台灯→台灯」同型复发的机器化闸门。"""
    T.sync_areas(["南阳台", "生活阳台", "主卧"])
    for ar in sorted(set(T.BASE_AREAS) | {"南阳台", "生活阳台"}):
        for tail in ("灯", "窗", "门", "扇"):
            phrase = ar + tail
            a, n, _s = T.parse_target(phrase)
            assert (a, n) == (ar, tail), (phrase, a, n)
    # 具名设备词与区名相邻时零重叠不误伤（区名+完整具名词）
    assert T.parse_target("阳台吊灯")[:2] == ("阳台", "吊灯")
    assert T.parse_target("南阳台主灯")[:2] == ("南阳台", "主灯")
    assert T.parse_target("客厅百叶帘")[:2] == ("客厅", "百叶帘")


# ── ⑥ 2026-10-01 根修：等长设备词必须由**书写序**决定（跨进程确定性）──
# 原 KNOWN_DEVICES = sorted(set(表一)|set(表二), key=len, reverse=True)：sorted 虽
# 稳定，但喂进去的 set 迭代序随 PYTHONHASHSEED 变 → 等长词先后逐进程随机。实锤
# 「投影幕布」：多数种子命中 投影(media_player)、seed=42 才命中 幕布(cover)——同一
# 句话在现场表现为"有时能开、有时开错设备"。sync_vocab 的动态合并同根，一并修。
_PROBE = (
    "import sys; sys.path.insert(0, '.')\n"
    "from core.nlu import targets as T\n"
    "T.clear_vocab()\n"
    "static = T.parse_target('投影幕布')[:2]\n"
    "T.sync_vocab({'cover.b': {'attributes': {'friendly_name': '幕布'}},\n"
    "              'media_player.a': {'attributes': {'friendly_name': '投影仪'}}})\n"
    "merged = T.parse_target('投影幕布')[:2]\n"
    "print(ascii([list(static), list(merged)]))\n"      # ascii() 转义=与码页彻底解耦
)

def test_device_table_is_deterministic_across_processes():
    """跨 5 个哈希种子起子进程跑真代码：解析结果必须完全一致，且落在语义正确侧
    （投影幕布 是 cover 幕布，不是 media_player 投影仪）。

    探针输出走 ascii() 转义：Windows 码页下子进程 stdout 是 GBK，父端按 utf-8 解
    会直接解码崩（本钉第一版即栽在此，表现为 stdout=None）。"""
    import subprocess

    outs = set()
    for seed in ("0", "1", "7", "42", "12345"):
        r = subprocess.run([sys.executable, "-c", _PROBE], cwd=str(HERE),
                           capture_output=True, text=True, encoding="utf-8",
                           env={**os.environ, "PYTHONHASHSEED": seed,
                                "PYTHONIOENCODING": "utf-8"}, timeout=120)
        assert r.returncode == 0, f"seed={seed} 探针失败: {(r.stderr or '')[-300:]}"
        assert r.stdout, f"seed={seed} 探针无输出"
        outs.add(r.stdout.strip())
    assert len(outs) == 1, f"设备词表解析随进程哈希种子漂移：{sorted(outs)}"
    want = ascii([[None, "幕布"], [None, "幕布"]])
    assert outs.pop() == want, "等长平局落点不对（幕布 应赢 投影，且静态/动态合并同侧）"


def test_equal_length_ties_follow_authoring_order():
    """平局判据=书写序（表一在前），与仓内既有纪律同源（「电动窗 排在泛称 窗 前」、
    集成映射「本表 order=正确性」）；幕布 写在 投影 之前 → 投影幕布 归 cover。"""
    assert T._KNOWN_DEVICES_TAIL.index("幕布") < T._KNOWN_DEVICES_TAIL.index("投影")
    two_char = [w for w in T.ALL_DEVICES if len(w) == 2]
    assert two_char.index("幕布") < two_char.index("投影"), two_char[:12]
    assert T.KNOWN_DEVICES == sorted(T._STATIC_ORDER, key=len, reverse=True)



# ── ⑦ v1.1.0 发版后审计：近音档不得把「动作/模式语素」糊成设备词（误执行类）──
# 「内倒」neidao --⑦--> 「雷达」leida（滑窗 "neida" 距 1 ≤ tol，雷达=v1.0.42 家电族
# 收的毫米波存在传感器）⇒「打开内倒」曾产 TurnDeviceOn name=雷达：用户要的是窗内倒
# 动作，设备却去开传感器并回「办好了」= 误执行 + 谎报；「通风」→「筒灯」同型。
# 与「亮度→浴霸」同族，修法同 _ATTR_NO_PINYIN：动作/模式语素禁入近音档（
# targets._ACTION_NO_PINYIN），宁 MISS 也不猜设备（v1.0.69 宁如实失败红线）。
HALLUCINATION_CASES = [("内倒", "雷达"), ("客厅内倒", "雷达"), ("通风", "筒灯")]


@pytest.mark.parametrize(("frag", "banned"), HALLUCINATION_CASES)
def test_action_morphemes_never_become_device_names(frag, banned):
    a, n, _s = T.parse_target(frag)
    assert n != banned and banned not in str((a, n)), (frag, a, n)


@pytest.mark.parametrize(("sentence", "banned"), [
    ("打开内倒", "雷达"), ("打开客厅内倒", "雷达"), ("打开通风", "筒灯"),
])
def test_hallucination_never_reaches_the_plan(fp, sentence, banned):
    """端到端：计划里绝不能出现被近音糊出来的设备名（那会被集成真的执行掉）。"""
    p = _match(fp, sentence)
    assert p is None or banned not in str(p.args), (sentence, p.args if p else None)


def test_pinyin_blocklist_does_not_blind_literal_window_words(fp):
    """禁区只关近音档——字面窗词/机型动作道必须原样工作（防"修复"做成新失能）。"""
    for s, want in (("打开内倒窗", "内倒窗"), ("打开内开内倒窗", "内开内倒窗"),
                    ("打开客厅上悬窗", "上悬窗"), ("打开办公室推拉窗", "推拉窗")):
        p = _match(fp, s)
        assert p is not None and p.intent == "ControlWindow", s
        tgt = p.args["target"][0]
        assert tgt["devices"][0]["name"] == want, (s, tgt)
    q = _match(fp, "开窗器内倒")                      # 内倒作为**动作**的既有车道
    assert q is not None and q.args.get("action") == "a", q.args
