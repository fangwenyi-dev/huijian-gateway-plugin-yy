# -*- coding: utf-8 -*-
"""修⑦：状态定语（「没关紧的窗」「没拉严的窗帘」）不是"点名的设备名"。

现场（本机 2026-10-01 实得，修① 之前灯域就在、修① 把同形扩到窗域后暴露）：

    _unknown_spoken_device_name('把没关紧的灯关上', 灯锚点, ('台灯',), 办公室, 办公室)
        -> '没关紧的灯'          ⇒ 拦，播「没有找到对应的设备『没关紧的灯』」

v1.1.27 的 CHANGELOG 明写「同时修掉它对『把没关紧的窗关上』等正常句的误杀」——那只修了
字面表一侧（`_NEGATION_CMD` 的词尾排除）。v1.1.35 新加的"点名查无"闸把修饰段当名字，
同一句话在这条路上**原样复活**：用户用状态指认那台东西（屋里就一扇窗、一盏灯），
系统却回答"没这台"。红线相反方向也守住：描述性修饰（会飞的/书桌上）照旧拦。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.nlu.fast_path import Plan                                  # noqa: E402
from core.pipeline import (_category_nouns, select_primary_plan,      # noqa: E402
                           _unknown_spoken_device_name)
from test_experience_batch import (Lane, RecExecutor, HA, _pipe)      # noqa: E401

AREA = ("办公室",)
LAMP = "light.ban_gong_shi_tai_deng"
WIN = "cover.ban_gong_shi_ping_kai_chuang"


def _plan(utt, eid):
    intent = "HassTurnOff" if eid.startswith("cover") else "HassTurnOn"
    return Plan(intent=intent, args={"entity_id": eid}, source="klar", utterance=utt)


def _decide(utt, eid, names):
    return select_primary_plan(None, _plan(utt, eid), known_areas=AREA,
                               device_names=names, real_areas=AREA)


# ── ① 判据本体 ────────────────────────────────────────────────────
def test_state_descriptor_yields_no_verdict():
    assert _unknown_spoken_device_name(
        "把没关紧的灯关上", _category_nouns("light"), ("台灯",), AREA, AREA) == ""
    assert _unknown_spoken_device_name(
        "把没拉严的窗帘拉上", _category_nouns("cover"), ("平开窗",), AREA, AREA) == ""
    assert _unknown_spoken_device_name(
        "把未锁好的门关上", _category_nouns("cover"), ("平开窗",), AREA, AREA) == ""


def test_descriptive_modifier_still_refused():
    """反向不变量：状态规则不许变成"一律豁免"，描述性修饰照旧拦。"""
    assert _unknown_spoken_device_name(
        "关掉会飞的灯", _category_nouns("light"), ("台灯",), AREA, AREA) == "会飞的灯"
    assert _unknown_spoken_device_name(
        "关掉书桌上的灯", _category_nouns("light"), ("台灯",), AREA, AREA) != ""


# ── ② 裁决面：真命令照旧执行（灯与窗两域同形都钉）───────────────────
def test_state_descriptor_sentences_execute():
    assert _decide("把没关紧的灯关上", LAMP, ("台灯",)) is not None
    assert _decide("把没关的灯打开", LAMP, ("台灯",)) is not None
    assert _decide("把没拉严的窗帘拉上", WIN, ("平开窗",)) is not None


def test_full_chain_state_descriptor_moves_the_device():
    utt = "把没关紧的窗关上"
    kl = Lane({utt: _plan(utt, WIN)})
    ex = RecExecutor()
    ha = HA({WIN: {"entity_id": WIN, "state": "open",
                   "attributes": {"friendly_name": "平开窗"}}})
    ha._areas = {"o": "办公室"}
    asyncio.run(_pipe(kl=kl, ex=ex, ha=ha).handle(utt, origin="o"))
    assert len(ex.plans) == 1, f"状态定语被当查无此名打死：{ex.plans}"


def test_flight_window_still_refused_end_to_end():
    """同一条链上把"该拦的"也验一遍，防我这条豁免写宽。"""
    utt = "关掉会飞的窗"
    kl = Lane({utt: _plan(utt, WIN)})
    ex = RecExecutor()
    ha = HA({WIN: {"entity_id": WIN, "state": "open",
                   "attributes": {"friendly_name": "平开窗"}}})
    ha._areas = {"o": "办公室"}
    r = asyncio.run(_pipe(kl=kl, ex=ex, ha=ha).handle(utt, origin="o"))
    assert ex.plans == []
    assert "没有找到对应的设备" in r.text, r.text
