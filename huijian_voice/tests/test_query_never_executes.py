# -*- coding: utf-8 -*-
"""查询句绝不由 klar 支执行（v1.1.17 复审，线上实锤驱动）。

现场（办公 .91 / 加载项 **1.1.17**，2026-09-28 静默探测，非硬件路径）：
    → 「客厅射灯关了吗」  回复「客厅射灯关了吗关了（注：「射灯」离线，已改指同名的另一台）」
      ＝**真把灯关了**，还带改指注；
    → 「哪些灯开着」      回复「好的，「射灯」本来就在要求的状态上」
      ＝落了 HassTurnOn（灯要是关着，这句就是真开灯）。

机制：疑问闸只挂在**字面表**一侧（`fast_path.py:711` 的 `is_state_question` 与
`:714` 的 状态|情况|哪些|列表），`select_primary_plan` 的 klar 支**从来没有**——
字面表按问句弃权后，klar 的计划照样胜出并执行。v1.1.2「状态问句一律不进执行档」
这条当年只补了一侧。本批两档共用 `is_query_like`（单点定义）收口。
"""
import asyncio

from core.nlu.fast_path import Plan

from test_experience_batch import Lane, RecExecutor, _pipe


def _arun(coro):
    return asyncio.run(coro)


def _klar_reply(utterance, intent="HassTurnOff", eid="light.she_deng"):
    return Plan(intent=intent, args={"entity_id": eid}, source="klar",
                utterance=utterance)


def test_state_question_not_executed_by_klar():
    """「客厅射灯关了吗」：klar 认得也不得执行（回归线上现场）。"""
    kl = Lane({"客厅射灯关了吗": _klar_reply("客厅射灯关了吗")})
    ex = RecExecutor()
    r = _arun(_pipe(kl=kl, ex=ex).handle("客厅射灯关了吗", origin="o"))
    assert ex.plans == [], f"疑问句被 klar 执行了：{ex.plans}"
    assert r.source != "klar", r.source


def test_anyhow_query_not_executed_by_klar():
    """「哪些灯开着」：同一条闸（字面表按"哪些"弃权，klar 此前直接执行）。"""
    kl = Lane({"哪些灯开着": _klar_reply("哪些灯开着", intent="HassTurnOn")})
    ex = RecExecutor()
    _arun(_pipe(kl=kl, ex=ex).handle("哪些灯开着", origin="o"))
    assert ex.plans == [], f"查询句被 klar 执行了：{ex.plans}"


def test_imperative_still_executes_via_klar():
    """反向钉：祈使句的 klar 计划照旧执行（闸不得把命令一起拦掉）。"""
    kl = Lane({"把客厅灯关了": _klar_reply("把客厅灯关了")})
    ex = RecExecutor()
    _arun(_pipe(kl=kl, ex=ex).handle("把客厅灯关了", origin="o"))
    assert [p.intent for p in ex.plans] == ["HassTurnOff"], ex.plans


def test_quantity_question_not_executed_by_klar():
    """量纲疑问（「射灯亮度多少」「空调温度多少」）也不得被 klar 当写命令执行。

    判据刻意用**疑问词**（多少/多大/几度…）而不是量纲词（度/电量）——后者会把
    「调到26度」这类量纲命令一起拦掉（既有钉当场红，实测）。"""
    for utt in ("射灯亮度多少", "办公室空调温度多少", "现在多少度", "风量多大"):
        kl = Lane({utt: _klar_reply(utt, intent="HassLightSet")})
        ex = RecExecutor()
        _arun(_pipe(kl=kl, ex=ex).handle(utt, origin="o"))
        assert ex.plans == [], f"量纲疑问被 klar 执行了：{utt} -> {ex.plans}"


def test_quantity_commands_still_execute_via_klar():
    """反向钉：量纲**命令**照旧执行（疑问词表不得误伤"调到26度"这类）。"""
    # entity 域必须与意图匹配：既有"目标证据"守卫会否决域不符的计划（与本题无关的噪音）
    cases = (("屋里空调调到26度", "HassClimateSetTemperature", "climate.a"),
             ("办公室射灯亮度调到百分之三十", "HassLightSet", "light.she_deng"))
    for utt, intent, eid in cases:
        kl = Lane({utt: _klar_reply(utt, intent=intent, eid=eid)})
        ex = RecExecutor()
        _arun(_pipe(kl=kl, ex=ex).handle(utt, origin="o"))
        assert [p.intent for p in ex.plans] == [intent], (utt, ex.plans)


def test_query_predicate_is_shared_single_source():
    """单点定义钉：字面表与 klar 闸必须用**同一个**判据（两处各写一份＝下次又漂）。"""
    from core.nlu import query as Q
    from core.nlu import fast_path as F
    import core.pipeline as P
    assert hasattr(Q, "is_query_like")
    assert P.is_query_like is Q.is_query_like
    assert callable(F.is_state_question)
