# -*- coding: utf-8 -*-
"""v1.1.4 批次钉：HA 语音别名进词表 + 逐实体状态回执 + 歧义目标确认环。

三条都直接对着 2026-09-21 真机 A 组的实测结果：

① 别名：用户在 HA「实体→别名 / 改名」里亲手写的叫法，过去我们**一行都没读**
   （ha_client 的注册表解析只取了 area_id），等于把用户已经告诉我们的说法丢掉、
   反过来要求用户按我们的手抄词表说话。现在别名与状态名同表同规则。

② 回执：真机返回 `{"success": true, "success_count": 1}` 而 per-entity 明确报
   `Entity cover.…_0603 does not support action cover.set_cover_position`，
   目标那台 position 一字未动——顶层 success 是**集成自己的口径**，不能拿它
   当"用户的设备真的动了"。现在成功判定改看逐实体回执：全失败=如实失败，
   部分失败=播报点名，绝不静默"都办妥了"。

③ 歧义：同一个 name=平开窗 在真机命中「平开窗」与「测试平开窗」两台。回执修好
   后不再谎报，但**动错设备**依旧会发生。现在点了名却匹配到多台不同设备时，把
   计划收窄到最优候选并借用现成确认环问一句（是/否/改口三态，不新增答案解析器、
   不改集成端）；没点名的整区/全屋批量语义照旧不问。
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_v1114_"))

from conftest import FakeHAClient                       # noqa: E402
from core.executor import Executor                      # noqa: E402
from core.nlu import targets as T                       # noqa: E402
from core.nlu.fast_path import Plan                     # noqa: E402
from core.pipeline import Pipeline                      # noqa: E402
from core.settings import Settings                      # noqa: E402


# ── ① 别名进词表 ──────────────────────────────────────────────
def test_ha_alias_becomes_a_vocab_word_with_the_right_domain():
    T.clear_vocab()
    try:
        T.sync_vocab(
            {"light.bath_downlight": {"attributes": {"friendly_name": "卫生间筒灯"}}},
            aliases={"light.bath_downlight": ["射灯二", "卫生间下灯"]})
        assert "射灯二" in T.ALL_DEVICES
        assert T.domain_hint("射灯二") == ["light"]
    finally:
        T.clear_vocab()


def test_disabled_hidden_entities_never_reach_vocab(ha_parse=None):
    """ha_client 侧剔除停用/隐藏实体 → 词表拿不到它们（防"已删设备还能被点名"）。"""
    rows = [
        {"entity_id": "light.ok", "name": "OK灯", "area_id": None},
        {"entity_id": "light.off", "name": "停用灯", "disabled_by": "user"},
        {"entity_id": "light.hid", "name": "隐藏灯", "hidden_by": "integration"},
    ]
    _ent, alias, _dc = __import__("core.ha_client", fromlist=["HAClient"]
                                   ).HAClient._parse_registry(rows, {})
    assert alias.get("light.ok") == ["OK灯"]
    assert "light.off" not in alias and "light.hid" not in alias


def test_alias_dict_form_and_list_form_both_parsed():
    C = __import__("core.ha_client", fromlist=["HAClient"]).HAClient
    _e, a1, dc1 = C._parse_registry(
        [{"entity_id": "fan.desk", "aliases": ["桌面扇", "台式扇"],
          "device_class": "fan"}], {})
    _e, a2, dc2 = C._parse_registry(
        [{"entity_id": "fan.desk", "aliases": {"老张的扇子": {}},
          "device_class": "fan"}], {})
    assert a1["fan.desk"] == ["桌面扇", "台式扇"]
    assert a2["fan.desk"] == ["老张的扇子"]
    assert dc1["fan.desk"] == dc2["fan.desk"] == "fan"


# ── ② 逐实体状态回执 ──────────────────────────────────────────
def _exec(result, plan=None):
    ha = FakeHAClient(results={"AdjustDeviceAttribute": result})
    plan = plan or Plan(intent="AdjustDeviceAttribute",
                        args={"attribute": "position", "delta": "60",
                              "target": [{"devices": [{"name": "平开窗",
                                                       "domains": ["cover"]}]}]},
                        source="t0", utterance="开窗器开到60")
    return asyncio.run(Executor(ha, None).run(plan))


def test_all_entities_failed_is_not_reported_success():
    ok, speech = _exec({"success": True, "success_count": 1,
                        "states": [{"name": "测试平开窗 开窗器", "success": False,
                                    "error": "Failed: unsupported"}]})
    assert ok is False, speech
    assert speech.startswith("抱歉") and "unsupported" in speech, speech


def test_partial_failure_is_named_in_speech():
    ok, speech = _exec({"success": True, "success_count": 1, "states": [
        {"name": "平开窗 开窗器", "success": True, "error": None},
        {"name": "测试平开窗 开窗器", "success": False,
         "error": "Failed: unsupported"}]})
    assert ok is True
    assert "1 台没成功" in speech, speech


def test_results_without_per_entity_states_keep_old_verdict():
    """老返回形态（无 states 键）不许被凭空判失败。"""
    ok, _speech = _exec({"success": True})
    assert ok is True


def test_receipt_counts_only_real_per_entity_rows():
    r = Executor._receipt([{"success": True, "states": [
        {"success": True}, {"success": False, "error": "boom"}, {"success": False,
        "error": "boom"}]}])
    assert r == (1, 2, ["boom"]), r
    assert Executor._receipt([{"success": True}]) == (0, 0, [])


# ── ③ 歧义目标确认环 ──────────────────────────────────────────
class _S:
    def __init__(self, extra=None):
        self.extra = extra or {}

    def get(self, k, d=None):
        return self.extra.get(k, d)


STATES = {
    "cover.a": {"entity_id": "cover.a", "state": "closed",
                "attributes": {"friendly_name": "平开窗 开窗器"}},
    "cover.b": {"entity_id": "cover.b", "state": "closed",
                "attributes": {"friendly_name": "测试平开窗 开窗器"}},
}

# 「书桌灯」的全等名到位 ⇒ 这台才是可收敛的形态（o = 书桌灯 / 测试书桌灯 两名）
LIGHTS = {
    "light.desk": {"entity_id": "light.desk", "state": "off",
                   "attributes": {"friendly_name": "书桌灯"}},
    "light.test_desk": {"entity_id": "light.test_desk", "state": "off",
                        "attributes": {"friendly_name": "测试书桌灯"}},
}


def _pl(extra=None):
    p = Pipeline.__new__(Pipeline)
    p.settings = _S(extra)
    p._confirm = {}
    p._origin_ts = {}
    p.ha = types.SimpleNamespace(_states=dict(STATES), _entity_area={})
    return p


def _plan():
    return Plan(intent="TurnDeviceOn",
                args={"target": [{"devices": [{"name": "平开窗", "domains": ["cover"]}]}]},
                source="t0", utterance="打开平开窗")


def test_ambiguous_named_target_asks_instead_of_actuating():
    """点在名字上的 target 命中多台 ⇒ 先挂确认环问一句（宽-Target）用异物设备。"""
    p = _pl()
    p.ha = types.SimpleNamespace(_states=dict(LIGHTS), _entity_area={})
    plan = Plan(intent="TurnDeviceOn",
                args={"target": [{"devices": [{"name": "书桌灯",
                                               "domains": ["light"]}]}]},
                source="t0", utterance="打开书桌灯")
    r = p._ambiguity_ask(plan, "room-1")
    assert r is not None and r.source == "confirm"
    assert "2 台" in r.text and "确认" in r.text, r.text
    assert "room-1" in p._confirm                      # 挂起，等答复
    # 计划已被收窄到全等候选（不是靠运气命中第一台）
    assert p._confirm["room-1"]["plan"].args["target"][0]["devices"][0]["name"] \
        == "书桌灯"


def test_no_convergence_evidence_lists_candidates_and_picks_nothing():
    """v1.1.28（2026-09-29 金标复测 #5 实锤）：三条证据都落空 ⇒ 绝不替用户挑一台。

    name='平开窗' 在本家同时命中 button ①开启/②暂停/③关闭、number 速度/力度 与
    cover 开窗器（『关闭平开窗』整机实况），旧口径到此取 `cands[0]` ⇒ 把「关闭」
    收敛到「平开窗 ① 开启」（**方向相反**）；用户回「确认」即一次误动作。新纪律：
    列出来让用户说清——宁可把候选全列给他选，也不许替他挑一台错的。
    """
    p = _pl()
    plan = _plan()
    r = p._ambiguity_ask(plan, "room-1")
    assert r is not None and r.source == "clarify", r
    txt = r.text or ""
    assert "平开窗 开窗器" in txt and "测试平开窗 开窗器" in txt, txt
    assert "room-1" not in p._confirm, "未收敛却挂了执行桩 ⇒ 用户误答确认即误动作"
    assert plan.args["target"][0]["devices"][0]["name"] == "平开窗", "目标名被偷偷改写"


def test_answer_yes_executes_the_narrowed_plan():
    """确认后执行的是**收窄后**的计划（灯对做载体：窗族另有既有的开关族专用闸，
    不是本批要验的东西，混在一起会把两条链的结论搅糊）。"""
    p = _pl()
    p.ha = types.SimpleNamespace(_states=dict(LIGHTS), _entity_area={})
    ex = FakeHAClient(results={"TurnDeviceOn": {"success": True}})
    p.executor = Executor(ex, None)
    p._note_target = lambda *a, **k: None
    p._remember_turn = lambda *a, **k: None
    plan = Plan(intent="TurnDeviceOn",
                args={"target": [{"devices": [{"name": "书桌灯",
                                               "domains": ["light"]}]}]},
                source="t0", utterance="打开书桌灯")
    r0 = p._ambiguity_ask(plan, "room-2")
    assert r0 is not None and "2 台" in r0.text, r0
    r = asyncio.run(p._confirm_answer("确认", "room-2"))
    assert r is not None and r.ok, r
    sent = ex.calls[0][1]["target"][0]["devices"][0]["name"]
    assert sent == "书桌灯", ex.calls
    assert "room-2" not in p._confirm


def test_answer_no_cancels_without_touching_devices():
    p = _pl()
    p.ha = types.SimpleNamespace(_states=dict(LIGHTS), _entity_area={})
    ex = FakeHAClient(results={"TurnDeviceOn": {"success": True}})
    p.executor = Executor(ex, None)
    p._remember_turn = lambda *a, **k: None
    p._ambiguity_ask(Plan(intent="TurnDeviceOn",
                          args={"target": [{"devices": [{"name": "书桌灯",
                                                         "domains": ["light"]}]}]},
                          source="t0", utterance="打开书桌灯"), "room-3")
    r = asyncio.run(p._confirm_answer("取消", "room-3"))
    assert r is not None and "取消" in r.text
    assert ex.calls == [], "取消不得留下任何下发"


def test_single_candidate_and_unnamed_batch_are_not_asked():
    p = _pl()
    only = Plan(intent="TurnDeviceOn",
                args={"target": [{"devices": [{"name": "测试平开窗",
                                               "domains": ["cover"]}]}]},
                source="t0", utterance="打开测试平开窗")
    assert p._ambiguity_ask(only, "x") is None
    batch = Plan(intent="TurnDeviceOn",
                 args={"target": [{"devices": [{"name": "", "domains": ["cover"]}]}]},
                 source="t0", utterance="关闭所有窗")
    assert _pl()._ambiguity_ask(batch, "y") is None       # 整区/全屋批量语义不问


def test_best_candidate_prefers_exact_name_over_dict_order():
    """全等名必须压过注册表遍历顺序——否则"书桌灯"会被排在前面的"测试书桌灯"顶掉，
    正是 A 组实锤的动错设备形态（顺序碰对了不等于判据对了）。"""
    cands = [{"entity_id": "cover.t", "attributes": {"friendly_name": "测试平开窗"}},
             {"entity_id": "cover.p", "attributes": {"friendly_name": "平开窗"}}]
    tgt = [{"devices": [{"name": "平开窗", "domains": ["cover"]}]}]
    assert Pipeline._best_candidate(cands, tgt) == "平开窗"
    # v1.1.28：无全等名且无区域/域证据 ⇒ **不许**退回首台（旧口径在这里盲取
    # cands[0]，实锤把「关闭平开窗」收敛到方向相反的「平开窗 ① 开启」）。
    # 收不住就返回 None，由 _ambiguity_ask 把候选列给用户，而不是替他挑一台。
    assert Pipeline._best_candidate(cands, [{"devices": [{"name": "那扇窗"}]}]) is None


def test_best_candidate_converges_on_intent_main_domain():
    """域证据：同一台设备的零件兄弟（button/number）不得压过承载域那一台。

    金标复测 #5/#10 实锤形态：t0 给的是域并集 ⇒ 候选里混着「平开窗 ① 开启」
    （button）与「平开窗 开窗器」（cover）。ControlWindow 的承载域是 cover，
    此时**必须且只允许**收敛到 cover 那一台。
    """
    cands = [
        {"entity_id": "button.pk_1", "state": "2026-01-01T00:00:00",
         "attributes": {"friendly_name": "平开窗 ① 开启"}},
        {"entity_id": "number.pk_speed", "state": "unknown",
         "attributes": {"friendly_name": "平开窗 速度"}},
        {"entity_id": "cover.pk", "state": "open",
         "attributes": {"friendly_name": "平开窗 开窗器"}},
    ]
    tgt = [{"devices": [{"name": "平开窗",
                         "domains": ["button", "cover", "number"]}]}]
    assert Pipeline._best_candidate(cands, tgt, "ControlWindow",
                                    {}, ("cover",)) == "平开窗 开窗器"
    # 同名孪生形态（真机：办公室/客厅两台 light 都叫「射灯」）⇒ 取可用的那一台
    twins = [
        {"entity_id": "light.a", "state": "unavailable",
         "attributes": {"friendly_name": "射灯"}},
        {"entity_id": "light.b", "state": "off",
         "attributes": {"friendly_name": "射灯"}},
    ]
    tgt2 = [{"devices": [{"name": "射灯",
                          "domains": ["button", "light", "select", "switch"]}]}]
    assert Pipeline._best_candidate(twins, tgt2, "AdjustDeviceAttribute",
                                    {}, ("light",)) == "射灯"
    # 主域来自属性槽：temperature ⇒ 只认 climate 本体，不认摆风 switch（#10 实锤）
    ac = [
        {"entity_id": "switch.ac_swing", "state": "on",
         "attributes": {"friendly_name": "办公室空调 左右摆风"}},
        {"entity_id": "climate.ac", "state": "fan_only",
         "attributes": {"friendly_name": "办公室空调 Air Conditioner"}},
    ]
    tgt3 = [{"devices": [{"name": "办公室空调",
                          "domains": ["button", "climate", "light", "number",
                                      "select", "switch"]}]}]
    assert Pipeline._best_candidate(ac, tgt3, "AdjustDeviceAttribute", {},
                                    ("climate",)) == "办公室空调 Air Conditioner"


def test_ambiguity_gate_can_be_switched_off():
    p = _pl({"dialog.confirm_ambiguous": False})
    assert p._ambiguity_ask(_plan(), "z") is None


def test_existing_risky_confirm_ring_still_wins_first():
    p = _pl()
    p._confirm["busy"] = {"plan": _plan(), "ts": __import__("time").time()}
    assert p._ambiguity_ask(_plan(), "busy") is None      # 不叠加两个问句
