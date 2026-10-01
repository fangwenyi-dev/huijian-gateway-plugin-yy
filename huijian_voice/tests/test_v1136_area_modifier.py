# -*- coding: utf-8 -*-
"""v1.1.36 ⑥：点名设备闸的"位置修饰语"口径必须单向一致。

v1.1.35 那道闸今天上线后，实测（`_goldtest/probe_named_gate_fp.py` + 整链
`_goldtest/probe_named_gate_chain.py`，清单=办公室射灯/台灯，注册区域=办公室）：

    拦：「关掉书桌的灯」「关掉书桌上的灯」「关掉窗边的灯」「关掉进门处的灯」
        → 「没有找到对应的设备「书桌的灯」…」
    放行执行：「关掉阳台的灯」「关掉走廊的灯」
        → 真动办公室那台射灯，还回播「阳台的灯关了」

两向都偏，根子是同一处：**豁免判据来自静态词表而不是家里的真区域**
（`_area_like` 里 BASE_AREAS=主卧/次卧/阳台/玄关/车库/露台/走廊 + 室厅房间楼区馆
后缀，targets.py:426-442）。家里根本没有"阳台"这间房时，"阳台"被当成位置词豁免，
同类别唯一那台就被顶走——撞用户一贯红线「绝不猜房间」「没点名的设备任何方向都不动」。

统一后的判据只有一条：**这句里的修饰段，家里到底有没有对应的东西**——
命中**真注册区域表**（`Pipeline._real_areas()`，来自 `ha._areas` + 卫星映射）⇒
放行（真有那间房，交给区域机器）；区域表和设备清单都查无 ⇒ 拦。
表还没拿到（空）时退回旧静态判据，宁可不拦也不误拦。

注：本文件多数钉显式传 `real_areas`（生产入口三原料的形状，见 test_klar_nlu 的
接线钉）；`test_full_chain_*` 走 `Pipeline.handle` 真入口，由 `_real_areas()` 自取。
"""
import asyncio

import pytest
from core.nlu.fast_path import Plan
from core.pipeline import select_primary_plan
from test_experience_batch import HA, Lane, RecExecutor, _pipe

NAMES = ("办公室射灯", "台灯")
OFFICE_ONLY = ("办公室",)


def _kl(utt, eid="light.ban_gong_shi_she_deng", intent="HassTurnOff"):
    return Plan(intent=intent, args={"entity_id": eid}, source="klar", utterance=utt)


@pytest.mark.parametrize("utt", ["关掉阳台的灯", "关掉走廊的灯", "关掉洗手间的灯",
                                 "关掉书房的灯", "关掉阳台上的灯"])
def test_unregistered_area_words_are_refused(utt):
    """家里没这间房 ⇒ 不得动别的房间那台（旧形豁免放行=猜房间）。"""
    got = select_primary_plan(None, _kl(utt), known_areas=OFFICE_ONLY,
                              device_names=NAMES, real_areas=OFFICE_ONLY)
    assert got is None, f"{utt}：家里没有这个区域却执行了（猜房间）"


@pytest.mark.parametrize("utt", ["关掉办公室的灯", "关掉阳台的灯"])
def test_registered_area_words_still_pass(utt):
    """**正向不变量**：真注册了的区域照旧放行——本批不许把区域句打死。

    「关掉阳台的灯」在**有**阳台这个区域的家里，必须仍能一句话关灯。
    """
    areas = OFFICE_ONLY + ("阳台",)
    got = select_primary_plan(None, _kl(utt), known_areas=areas,
                              device_names=NAMES, real_areas=areas)
    assert got is not None, f"{utt}（区域已注册）被误拦"


def test_empty_area_registry_falls_back_and_does_not_refuse():
    """注册表还没拿到（real_areas 空）⇒ 退回静态判据，不得凭空拦。

    反向说：这条钉住"fail-open 而不是把位置词一律打死"，防我把收口做成新误杀。
    known_areas 里那些静态名（阳台/走廊…）在生产上恒在，正是旧豁免的来源。
    """
    for utt in ("关掉阳台的灯", "关掉走廊的灯", "打开客厅的灯"):
        got = select_primary_plan(None, _kl(utt),
                                  known_areas=("办公室", "阳台", "走廊", "客厅"),
                                  device_names=NAMES, real_areas=())
        assert got is not None, f"无区域表时 {utt} 被拦（越权裁决）"


@pytest.mark.parametrize("utt", ["把灯关了", "关掉那盏灯", "关掉所有的灯",
                                 "关掉台灯", "关掉办公室射灯"])
def test_generic_and_anaphora_sentences_untouched(utt):
    """泛称/回指/真名三类既有形态零漂移。"""
    got = select_primary_plan(None, _kl(utt), known_areas=OFFICE_ONLY,
                              device_names=NAMES, real_areas=OFFICE_ONLY)
    assert got is not None, f"{utt} 被误拦"


def test_real_areas_accessor_reads_registry_not_static():
    """`_real_areas()` 只报**这台 HA 真注册过的**区域，不得把 BASE_AREAS 混进来——
    混进来豁免就等于没改（生产侧 `_known_areas()` 恒含静态基准，两表必须分开）。
    """
    ha = HA({})
    ha._areas = {"o": "办公室", "s": "展厅"}
    p = _pipe(ha=ha)
    got = set(p._real_areas())
    assert got == {"办公室", "展厅"}, got
    from core.nlu import targets as T
    assert not (T.BASE_AREAS & got), "静态基准区名漏进了真区域表（豁免等于没改）"


def test_full_chain_unregistered_room_moves_nothing():
    """整链：说没有的房间 ⇒ 一台都不动，且回显点名用户自己说的那句。"""
    utt = "关掉阳台的灯"
    kl = Lane({utt: _kl(utt)})
    ex = RecExecutor()
    ha = HA({"light.ban_gong_shi_she_deng": {
        "entity_id": "light.ban_gong_shi_she_deng", "state": "on",
        "attributes": {"friendly_name": "办公室射灯"}}})
    ha._areas = {"o": "办公室"}
    r = asyncio.run(_pipe(kl=kl, ex=ex, ha=ha).handle(utt, origin="o"))
    assert ex.plans == [], f"没有「阳台」这间房却动了设备：{ex.plans}"
    assert "没有找到对应的设备" in r.text, r.text


# ── ⑥之三：接线——每一处裁决点都得传真区域表 ───────────────────
def test_all_gate_call_sites_thread_real_areas():
    """本仓反复栽在"只修一个调用点"（降级支半道闸、面板旁路、链内静默丢腿同型）。
    少传一处 ⇒ 那条路径退回静态区名豁免 ⇒ 「关掉阳台的灯」照样动别的房间。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "core" / "pipeline.py").read_text(
        encoding="utf-8")
    n_gate = src.count("self._real_areas())")
    assert n_gate >= 5, (
        f"真区域表只接了 {n_gate} 处裁决点（主裁决/主裁决话术源/降级支/"
        f"链内分句/链内话术源 五处都得传）")


def test_locative_modifier_survives_the_strip_and_is_refused():
    """「关掉阳台的灯」此前放行的**真因**（探针逐层实得，与最初的猜测不同）：
    ①尾缀表 `_NAME_TAIL_STRIP` 的 `+` 混着量词 台/头/个/只/盏，一次就把
      「阳台的」连吃成「阳」⇒ 长度不够 ⇒ 无判据；
    ②"从修饰段里剥区域名"那步用的是 `known_areas`（=静态 BASE_AREAS ∪ 注册表），
      家里没有阳台也把"阳台"剥走 ⇒ 修饰段只剩动词。

    现在：剥尾**剩孤字就停**（变异 A 证它承重）；区域名只剥真注册区域（变异 B/C 证）。
    回显仍用用户自己说的那截（含"的"），不是我拼出来的「阳台灯」。
    """
    from core.pipeline import _category_nouns, _unknown_spoken_device_name
    w = _category_nouns("light")
    got = _unknown_spoken_device_name("关掉阳台的灯", w, ("台灯", "办公室射灯"),
                                      known_areas=("办公室",),
                                      real_areas=("办公室",))
    assert got == "阳台的灯", f"位置段被剥残/放行（回={got!r}）"


def test_name_starting_at_anchor_still_rescues():
    """**正向不变量**：用户说了"前缀 + 在装全名"时必须照旧认出那台。

    「关掉床头台灯」里「台灯」正好从锚点起 ⇒ 合法，不得被我上条的收紧误杀。
    这条在，收紧才不是一杆子打死。
    """
    from core.pipeline import _category_nouns, _unknown_spoken_device_name
    w = _category_nouns("light")
    got = _unknown_spoken_device_name("关掉床头台灯", w, ("台灯",),
                                      known_areas=("办公室",),
                                      real_areas=("办公室",))
    assert got == "", f"合法的『前缀+全名』被拦了：{got!r}"


def test_place_word_plus_full_device_name_rescued():
    """`_locative_prefix_of` 那条通道：**位置词 + 在装真名**是同一台东西。

    「打开走廊感应灯」在家里那台叫「感应灯」时——锚点是"灯"、名字是"感应灯"，
    既不相等也不互含于修饰段，只有"剥掉前缀位置词后剩下完整真名"这一条能认。
    与「阳台灯」的分别就在词界：台灯 的起点切在 阳|台 中间 ⇒ 不算命中。
    """
    from core.pipeline import _category_nouns, _unknown_spoken_device_name
    w = _category_nouns("light")
    got = _unknown_spoken_device_name("打开走廊感应灯", w, ("感应灯",),
                                      known_areas=("办公室",),
                                      real_areas=("办公室",))
    assert got == "", f"『位置+真名』被误拦：{got!r}"
    got2 = _unknown_spoken_device_name("关掉阳台北的灯", w, ("台灯",),
                                       known_areas=("办公室",),
                                       real_areas=("办公室",))
    assert got2 == "阳台北的灯", f"跨词界的假命中又放行了：{got2!r}"


def test_known_limit_one_char_modifier():
    """已知边角（钉住现状，别把它写成"全类已闭"）。

    「关掉阳台灯」（不带"的"）里，同一结束位置的类别词按"取最长"取到「台灯」
    （阳|台灯），修饰段只剩「阳」⇒ 长度不足 ⇒ 无判据 ⇒ 放行。这是 v1.1.35
    为避免「关掉台灯」被算成「关掉台」而定的取锚规则带来的残余，不在本批射程内，
    留此钉防被误宣称为已修。
    """
    from core.pipeline import _category_nouns, _unknown_spoken_device_name
    w = _category_nouns("light")
    got = _unknown_spoken_device_name("关掉阳台灯", w, ("台灯",),
                                      known_areas=("办公室",),
                                      real_areas=("办公室",))
    assert got == "", got
