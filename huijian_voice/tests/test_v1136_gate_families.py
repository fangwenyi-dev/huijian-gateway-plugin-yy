# -*- coding: utf-8 -*-
"""v1136 ⑤：证据闸必须**真的**全族同闸（pipeline.py:207 那句注释此前不实）。

复现（`_goldtest/probe_gate_families.py` 实测，同形句只差意图名）：

    HassLock            「把会飞的门锁上」      主裁决=放行执行  降级支=放行执行
    HassLock            「给故事机上个锁」      主裁决=放行执行  降级支=放行执行
    HassVacuumStart     「启动会飞的扫地机」    主裁决=放行执行  降级支=放行执行
    HassFanSetPresetMode「会飞的风扇设成睡眠」  主裁决=放行执行  降级支=放行执行
    HassTurnOn          「关掉会飞的灯」        主裁决=拦        降级支=拦

根因不是词表缺——`_DOMAIN_EVIDENCE` 里 lock(:220)/vacuum(:223)/fan(:218) **三域
都在**——是意图集 `_KLAR_WRITE_INTENTS`(:202-209) 只有 8 个意图，而 klar 白名单
`KLAR_CONTROL_INTENTS`（nlu/klar_client.py:46-54）有 12 个。闸按意图集过滤，
白名单里的锁族/扫地机族整条绕过去。

锁是**高风险面**：intent_turn.py:326 `# off = unlock`。

所以本文件除行为钉外还钉一条**机器可验的不变量**：白名单控制族必须全部进闸。
今后再有人往白名单加意图而忘了加闸，这条自己转红（今天那处"注释说全族同闸、
实现差 6 族"就是缺这条才混过去的）。
"""
import asyncio

import pytest
from core.nlu.fast_path import Plan
from core.nlu.klar_client import KLAR_CONTROL_INTENTS
from core.pipeline import (_DOMAIN_EVIDENCE, _KLAR_WRITE_INTENTS,
                           select_fallback_plan, select_primary_plan)
from test_experience_batch import HA, Lane, RecExecutor, _pipe

# 未进闸的族（本批要清零的那 6 个）
UNCOVERED = sorted(set(KLAR_CONTROL_INTENTS) - set(_KLAR_WRITE_INTENTS))


def _kl(intent, eid, utt):
    return Plan(intent=intent, args={"entity_id": eid}, source="klar", utterance=utt)


def _fp(utt):
    return Plan(intent="TurnDeviceOn", args={}, source="t1", utterance=utt)


# ── 不变量：白名单控制族 == 闸内族（机器可验，别信注释）──────────
def test_every_whitelisted_control_intent_is_gated():
    assert not (set(KLAR_CONTROL_INTENTS) - set(_KLAR_WRITE_INTENTS)), (
        f"这些引擎可接管的控制意图不在证据闸内：{UNCOVERED}")


def test_gate_evidence_table_covers_every_reachable_domain():
    """闸内意图能落到的域，必须在 `_DOMAIN_EVIDENCE` 里有词表——
    否则 `words is None` 直接放行（fail-open），族补齐也只是形似。"""
    doms = {e.split(".", 1)[0] for e in (
        "light.x", "switch.x", "cover.x", "fan.x", "climate.x", "lock.x",
        "media_player.x", "vacuum.x", "humidifier.x", "scene.x")}
    missing = sorted(doms - set(_DOMAIN_EVIDENCE))
    assert not missing, f"这些域没有证据词表，闸对它们恒放行：{missing}"


# ── 行为钉：三族各一形，主裁决与降级支都必须拦 ──────────────────
@pytest.mark.parametrize("intent,eid,utt,nouns", [
    ("HassLock", "lock.front_door", "把会飞的门锁上", None),
    ("HassUnlock", "lock.front_door", "把会飞的门打开锁", None),
    ("HassVacuumStart", "vacuum.robot", "启动会飞的扫地机", None),
    ("HassVacuumPause", "vacuum.robot", "暂停会飞的扫地机", None),
    ("HassVacuumReturnToBase", "vacuum.robot", "让会飞的扫地机回桩", None),
    ("HassFanSetPresetMode", "fan.bed", "会飞的风扇设成睡眠", None),
])
def test_absent_named_device_refused_for_all_control_families(intent, eid, utt, nouns):
    """主裁决：点了家里没有的名字 ⇒ 不得由这些族执行。"""
    NAMES = ("办公室射灯", "大门锁", "扫地机器人小白", "卧室循环扇")
    got = select_primary_plan(None, _kl(intent, eid, utt),
                              known_areas=("办公室",), device_names=NAMES)
    assert got is None, f"{intent} 绕过了点名查无闸：{utt}"
    fb = select_fallback_plan(_fp(utt), None, _kl(intent, eid, utt), utt,
                              ("办公室",), NAMES)
    assert fb is None, f"{intent} 从降级支绕过（只闸主裁决=半道闸）：{utt}"


@pytest.mark.parametrize("intent,eid,utt", [
    ("HassLock", "lock.da_men", "把大门锁上"),
    ("HassVacuumStart", "vacuum.xiaobai", "启动小白扫地机"),
    ("HassFanSetPresetMode", "fan.xunhuan", "循环扇设成睡眠"),
])
def test_real_named_device_still_executes_for_those_families(intent, eid, utt):
    """**正向不变量**：把真名加进清单，同一句必须照旧执行。

    没有这条，"补齐 6 族"会退化成"把锁/扫地机/风扇一杆子打死"。
    """
    NAMES = ("大门锁", "小白扫地机", "循环扇", "办公室射灯")
    got = select_primary_plan(None, _kl(intent, eid, utt),
                              known_areas=("办公室",), device_names=NAMES)
    assert got is not None, f"{intent} 合法点名被误拦：{utt}"


def test_no_registry_means_no_verdict():
    """拿不到清单 ⇒ 没有裁决权（fail-open），不得凭空说"没这台设备"。"""
    for intent, eid, utt in (("HassLock", "lock.x", "把会飞的门锁上"),
                             ("HassVacuumStart", "vacuum.x", "启动会飞的扫地机")):
        got = select_primary_plan(None, _kl(intent, eid, utt),
                                  known_areas=("办公室",), device_names=())
        assert got is not None, f"{intent} 在无清单时被拦（越权裁决）"


# ── 整链：真句不得动设备，且回显点名用户自己说的那截 ─────────────
@pytest.mark.parametrize("utt,eid,intent", [
    ("把会飞的门锁上", "lock.front_door", "HassLock"),
    ("启动会飞的扫地机", "vacuum.robot", "HassVacuumStart"),
])
def test_full_chain_does_not_actuate(utt, eid, intent):
    kl = Lane({utt: _kl(intent, eid, utt)})
    ex = RecExecutor()
    st = {"light.ban_gong_shi_she_deng": {
        "entity_id": "light.ban_gong_shi_she_deng", "state": "on",
        "attributes": {"friendly_name": "办公室射灯"}}}
    ha = HA(st)
    ha._areas = {"o": "办公室"}
    r = asyncio.run(_pipe(kl=kl, ex=ex, ha=ha).handle(utt, origin="o"))
    assert ex.plans == [], f"整链真执行了 {intent}：{ex.plans}"
    assert "没有找到对应的设备" in r.text, r.text
    assert "会飞的" in r.text, f"回显没带用户自己说的那截：{r.text}"


def test_domain_unresolvable_shape_is_documented_fail_open():
    """口径边界（对抗复核抓到、我复现）：klar 只给 area、既无 entity_id 又无
    domain 时，`words is None` ⇒ 闸**放行**。这不是漏族，是"没有裁决原料就没有
    裁决权"的同一条纪律（与 `_device_names` 空表不判一致）。

    本钉把现状钉死：哪天有人给这类形状补上域来源，这条会转红，届时应改注释
    而不是悄悄改变行为。
    """
    kl = Plan(intent="HassLock", args={"area": "办公室"}, source="klar",
              utterance="把会飞的门锁上")
    got = select_primary_plan(None, kl, known_areas=("办公室",),
                              device_names=("大门",), real_areas=("办公室",))
    assert got is not None, "域判不了的形状被拦了（口径变了，请同步注释与本钉）"
