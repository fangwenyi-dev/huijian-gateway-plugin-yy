# -*- coding: utf-8 -*-
"""修①：属性字表不得吃掉 cover 域自己的类别词（窗/帘）。

现场（v1.1.35 现役，我本机干净 checkout 探针实得，非推测）：

    [探针H] 关掉会飞的窗  无证据=False 查无名='' 裁决=放行执行
    [探针H3] 关掉会飞的灯 裁决 = 拦下

同形句只差一个类别词：灯族被 v1.1.35 那道"点名设备查无"闸拦下，窗族**整类根本不进闸**。
根因不在闸的调用点（三处都接了），在锚点集：`_category_nouns(dom)` 用
`_ATTR_EVIDENCE` 各值集的并集当 skip 表，而 `HassSetPosition` 的属性证据里收了
「窗」「帘」（它们是"开窗位置"那类句的属性证据）⇒ cover 的类别词被自己的属性字吃掉
⇒ 提不出锚点 ⇒ 无修饰段 ⇒ fail-open ⇒ 家里唯一那扇窗被顶包，还播「会飞的灯关了」的
窗版本。这正是本闸立起来要消灭的那一条红线（没点名的设备任何方向都不动）。

判据修法用单源：类别词身份优先——取 `targets._GENERIC_FAMILIES`（泛称字族既有单源），
帘显式补（该表按开窗器形状收词不含帘）。不新建第二张手抄词表。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.nlu.fast_path import Plan                                  # noqa: E402
from core.pipeline import (select_primary_plan, _category_nouns,      # noqa: E402
                           _klar_named_absent_target)
from test_experience_batch import (Lane, RecExecutor, HA, _pipe)      # noqa: E401

CEID = "cover.ban_gong_shi_ping_kai_chuang"
# 办公 .91 真实地形：只有「办公室」一间注册区域，窗只有一扇「平开窗」
AREA = ("办公室",)
NAMES = ("办公室射灯", "台灯", "平开窗")


def _cover(utt):
    return Plan(intent="HassTurnOff", args={"entity_id": CEID},
                source="klar", utterance=utt)


def _decide(utt, names=NAMES, areas=AREA):
    return select_primary_plan(None, _cover(utt), known_areas=areas,
                               device_names=names, real_areas=areas)


# ── ① 承重钉：窗族点名查无必须拦（撤掉 `_class_nouns` 即红）──────────
def test_flying_window_named_absent_is_refused():
    assert "窗" in _category_nouns("cover"), "窗又被属性字吃掉了（cover 整类不进闸）"
    assert "帘" in _category_nouns("cover"), "帘又被属性字吃掉了"
    assert _decide("关掉会飞的窗") is None
    assert _klar_named_absent_target(_cover("关掉会飞的窗"), NAMES,
                                     AREA, AREA) == "会飞的窗"


def test_full_chain_flight_window_moves_nothing_and_says_no_such_device():
    utt = "关掉会飞的窗"
    kl = Lane({utt: _cover(utt)})
    ex = RecExecutor()
    ha = HA({CEID: {"entity_id": CEID, "state": "closed",
                    "attributes": {"friendly_name": "平开窗"}}})
    ha._areas = {"o": "办公室"}
    r = asyncio.run(_pipe(kl=kl, ex=ex, ha=ha).handle(utt, origin="o"))
    assert ex.plans == [], f"没有「会飞的窗」这台却动了窗：{ex.plans}"
    assert "没有找到对应的设备" in r.text and "会飞的窗" in r.text, r.text


# ── ② 反向不变量：恢复锚点不得把日常窗句打死（误拦=用户天天说的句子失灵）──
def test_generic_window_sentences_still_execute():
    for utt in ("打开窗帘", "关上纱窗", "把平开窗关上", "关掉办公室的窗",
                "关掉那扇窗", "把窗开度调到一半"):
        assert _decide(utt) is not None, f"{utt} 被误拦"


def test_position_sentence_follows_area_registration_both_ways():
    """位置句的双向：房间真注册 ⇒ 照常执行；家里没这间房 ⇒ 拦（绝不猜房间）。

    反向钉不许只做"放行"那一侧——同一条判据两臂都要钉死，否则把「客厅」当
    豁免词表混进来的旧毛病会静默复活。
    """
    utt = "把客厅窗户开度调到一半"
    areas = AREA + ("客厅",)
    assert select_primary_plan(
        None, Plan(intent="HassSetPosition",
                   args={"entity_id": CEID, "position": 50}, source="klar",
                   utterance=utt),
        known_areas=areas, device_names=NAMES, real_areas=areas) is not None
    assert _decide(utt) is None, "家里没有客厅这间房，却动了办公室那扇窗"


def test_registered_area_window_sentence_still_executes():
    """家里真有「阳台」这间房 ⇒「关掉阳台的窗帘」交给区域机器，不拦。"""
    areas = AREA + ("阳台",)
    assert select_primary_plan(
        None, Plan(intent="HassTurnOff", args={"entity_id": CEID}, source="klar",
                   utterance="关掉阳台的窗帘"),
        known_areas=areas, device_names=NAMES, real_areas=areas) is not None


def test_homophone_window_name_still_executes():
    """ASR 听岔（催拉窗↔推拉窗）照旧救援，不因恢复锚点而失效。"""
    assert select_primary_plan(
        None, Plan(intent="HassTurnOff", args={"entity_id": CEID}, source="klar",
                   utterance="关掉催拉窗"),
        known_areas=AREA, device_names=("推拉窗",), real_areas=AREA) is not None


def test_light_position_attribute_sentence_untouched():
    """本钉保护的是 v1.0.92 那条既有纪律：属性字仍不得当锚点。"""
    assert _decide("把办公室的射灯亮度调到百分之三十",
                   names=("办公室射灯",)) is not None
