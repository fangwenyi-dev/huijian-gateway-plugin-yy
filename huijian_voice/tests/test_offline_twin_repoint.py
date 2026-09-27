"""v1.1.15 钉桩（办公 .91 实锤 D4）：离线孪生要**先改指同名可用台**，再谈拒答。

现场（2026-09-27，HA .91 / 板 32b8 / 加载项 1.1.14 / SenseVoice）：
    [级联] '打开客厅的灯'  → 回复「抱歉，「射灯」现在离线（不可用）」
    [级联] '打开床头灯'    → 回复「抱歉，「射灯」现在离线（不可用）」
    [级联] '把客厅的灯关掉' → 回复「抱歉，「射灯」现在离线（不可用）」
三条都被 klar 落到 `light.she_deng`（离线孪生，friendly_name 恰好也叫"射灯"），
而可用的 `light.ban_gong_shi_she_deng` **同名**就在旁边。v1.1.7 的闸报的是真话，
但用户听到的是"我的灯不受控"——真正的病是**选错了台**：klar 的家快照里没有 state
字段（`nlu/klar-ha-nlu/src/home/snapshot.rs:59-83`），它物理上避不开离线实体。

纪律：改指只在"同名全等 + 同域 + 恰好一台确证可用"时发生；两台同名、名字缺失、
候选是 'unknown'（未首 poll 瞬态）一律不动，交回闸如实说——宁可不改，不可猜台。
"""
import asyncio

from conftest import FakeHAClient
from core.capability import resolve_candidates
from core.executor import Executor
from core.nlu.fast_path import Plan


class Ha(FakeHAClient):
    """FakeHAClient 补 call_service：模拟 HA 对 unavailable 实体照样回 success。"""

    def __init__(self, svc_results=None, **kw):
        super().__init__(**kw)
        self.svc_calls = []
        self._svc_results = svc_results or {}

    async def call_service(self, domain, service, data, timeout=10.0):
        self.svc_calls.append((domain, service, data))
        return self._svc_results.get(f"{domain}.{service}", {"success": True})


def _klar(intent, args, utterance):
    return Plan(intent=intent, args=args, source="klar", utterance=utterance)


def _ent(eid, state, name):
    return {"entity_id": eid, "state": state, "attributes": {"friendly_name": name}}


OFFLINE_TWIN = _ent("light.she_deng", "unavailable", "射灯")
GOOD_LAMP = _ent("light.ban_gong_shi_she_deng", "off", "射灯")


def _run(ha, plan):
    return asyncio.run(Executor(ha, None).run(plan))


def _sent(ha) -> str:
    """两条外发通道（klar 直调 call_service / 内置意图 handle_intent）的落点合集。"""
    import json
    return json.dumps([ha.svc_calls, getattr(ha, "calls", [])], ensure_ascii=False)


def _targets_ok(ha, good: str, offline: str) -> bool:
    blob = _sent(ha).replace(good, "")
    return good in _sent(ha) and offline not in blob and (ha.svc_calls or ha.calls)


def test_repoints_to_unique_available_twin():
    """唯一同名可用台 → 改指并真执行，不再**拒答**"离线"。

    口径随收口批更新：播报现在会**主动交代**改指（"（注：「射灯」离线，已改指同名的
    另一台）"），故旧钉"回执里不得出现'离线'二字"已不成立——真正要守的是"不再是拒绝
    口吻"（无"抱歉/不可用"）。逐字断言条见 test_repoint_is_announced。"""
    ha = Ha(states={"light.she_deng": OFFLINE_TWIN,
                    "light.ban_gong_shi_she_deng": GOOD_LAMP})
    ok, msg = _run(ha, _klar("HassTurnOn", {"entity_id": "light.she_deng"}, "打开客厅的灯"))
    assert ok is True, msg
    assert "抱歉" not in msg and "不可用" not in msg, msg
    assert _targets_ok(ha, "light.ban_gong_shi_she_deng", "light.she_deng"), _sent(ha)


def test_two_available_twins_are_not_guessed():
    """两台同名可用 → 不猜，交回歧义/闸处理（宁拒不错按）。"""
    ha = Ha(states={"light.she_deng": OFFLINE_TWIN,
                    "light.a": _ent("light.a", "off", "射灯"),
                    "light.b": _ent("light.b", "on", "射灯")})
    ok, msg = _run(ha, _klar("HassTurnOn", {"entity_id": "light.she_deng"}, "打开客厅的灯"))
    assert ok is False and "离线" in msg, msg
    assert ha.svc_calls == []


def test_unknown_state_candidate_is_not_used():
    """候选是 'unknown'（未首 poll 瞬态）→ 不算可用，不改指。"""
    ha = Ha(states={"light.she_deng": OFFLINE_TWIN,
                    "light.ban_gong_shi_she_deng": _ent("light.ban_gong_shi_she_deng",
                                                        "unknown", "射灯")})
    ok, msg = _run(ha, _klar("HassTurnOn", {"entity_id": "light.she_deng"}, "打开客厅的灯"))
    assert ok is False and "离线" in msg, msg
    assert ha.svc_calls == []


def test_different_name_twin_is_not_used():
    """名字不全等（"书房灯" vs "射灯"）→ 绝不改指。"""
    ha = Ha(states={"light.she_deng": OFFLINE_TWIN,
                    "light.shufang": _ent("light.shufang", "off", "书房灯")})
    ok, msg = _run(ha, _klar("HassTurnOn", {"entity_id": "light.she_deng"}, "打开客厅的灯"))
    assert ok is False and "离线" in msg, msg


def test_available_target_untouched():
    """目标本就可用 → 一个字都不改（既有路径零扰动）。"""
    ha = Ha(states={"light.ban_gong_shi_she_deng": GOOD_LAMP})
    ok, msg = _run(ha, _klar("HassTurnOff",
                             {"entity_id": "light.ban_gong_shi_she_deng"}, "把客厅的灯关掉"))
    assert ok is True, msg
    assert _targets_ok(ha, "light.ban_gong_shi_she_deng", "light.she_deng"), _sent(ha)


def test_missing_from_snapshot_still_passes_through():
    """快照里没有该实体（桥不全）→ 不拒不改（既有"未知=放行"口径不变）。"""
    ha = Ha(states={"light.other": _ent("light.other", "off", "别的灯")})
    ok, msg = _run(ha, _klar("HassTurnOn", {"entity_id": "light.she_deng"}, "打开客厅的灯"))
    assert ok is True, msg


def test_entity_id_list_form_repoints_in_place():
    """entity_id 是列表形时按位替换，顺序与其余目标不变。"""
    ha = Ha(states={"light.she_deng": OFFLINE_TWIN,
                    "light.ban_gong_shi_she_deng": GOOD_LAMP,
                    "fan.f": _ent("fan.f", "off", "风扇")})
    args = {"entity_id": ["light.she_deng", "fan.f"]}
    ok, _ = _run(ha, _klar("HassTurnOn", args, "打开客厅的灯和风扇"))
    assert ok is True
    assert args["entity_id"] == ["light.ban_gong_shi_she_deng", "fan.f"], args


# ── 改指必须在播报里点名（收口批）──────────────────────────────
# 收敛前：用户说「打开客厅的灯」，被 klar 落到离线的 light.she_deng，执行层静默改指到
# 同名的 light.ban_gong_shi_she_deng，播报只说"客厅的灯开了"——**换了一台物理设备却
# 一个字都不交代**（办公 .91 的射灯孪生正是这个形状；两台的 friendly_name 全等，
# 名字本身给不出区域信息，所以"没告知"等于让人以为客厅那台动了）。
def test_repoint_is_announced():
    """改指成功 ⇒ 播报必须带注：说的那台离线、动的是同名另一台；原话术不得被吞。"""
    ha = Ha(states={"light.she_deng": OFFLINE_TWIN,
                    "light.ban_gong_shi_she_deng": GOOD_LAMP})
    ok, msg = _run(ha, _klar("HassTurnOn", {"entity_id": "light.she_deng"}, "打开客厅的灯"))
    assert ok is True, msg
    assert "客厅的灯开了" in msg, msg              # 既有回执保住
    assert "「射灯」" in msg and "离线" in msg and "同名" in msg, msg
    assert _targets_ok(ha, "light.ban_gong_shi_she_deng", "light.she_deng"), _sent(ha)


def test_repoint_note_names_the_area():
    """注里要带上**改指目标所在区域**（v1.1.17）：两台同名、名字自己给不出房间信息，
    不报区域时用户听不出动的是哪一间的那台（D4 的现场就是"客厅的灯"落到了办公室射灯）。"""
    ha = Ha(states={"light.she_deng": OFFLINE_TWIN,
                    "light.ban_gong_shi_she_deng": GOOD_LAMP},
            entity_area={"light.ban_gong_shi_she_deng": "办公室"})
    ok, msg = _run(ha, _klar("HassTurnOn", {"entity_id": "light.she_deng"}, "打开客厅的灯"))
    assert ok is True, msg
    assert "在办公室" in msg, msg


def test_repoint_note_invents_no_area():
    """反向：区域表没有这台 ⇒ 不得凭空编一个区域，退回既有措辞。"""
    ha = Ha(states={"light.she_deng": OFFLINE_TWIN,
                    "light.ban_gong_shi_she_deng": GOOD_LAMP})
    ok, msg = _run(ha, _klar("HassTurnOn", {"entity_id": "light.she_deng"}, "打开客厅的灯"))
    assert ok is True, msg
    assert "在" not in msg.split("注：")[-1], msg


def test_no_repoint_gets_no_note():
    """反向钉：本就可用（没改指）⇒ 一个字都不加，既有话术零扰动。"""
    ha = Ha(states={"light.ban_gong_shi_she_deng": GOOD_LAMP})
    ok, msg = _run(ha, _klar("HassTurnOn",
                             {"entity_id": "light.ban_gong_shi_she_deng"}, "打开客厅的灯"))
    assert ok is True, msg
    assert "离线" not in msg and "同名" not in msg, msg


def test_repoint_note_resets_between_runs():
    """留痕按 run 复位：第二句没改指，就不得带上一句的注（同一 Executor 连跑）。"""
    ha = Ha(states={"light.she_deng": OFFLINE_TWIN,
                    "light.ban_gong_shi_she_deng": GOOD_LAMP})
    ex = Executor(ha, None)
    ok1, msg1 = asyncio.run(ex.run(_klar("HassTurnOn", {"entity_id": "light.she_deng"},
                                         "打开客厅的灯")))
    assert ok1 is True and "同名" in msg1, msg1
    ok2, msg2 = asyncio.run(ex.run(_klar("HassTurnOn",
                                         {"entity_id": "light.ban_gong_shi_she_deng"},
                                         "打开客厅的灯")))
    assert ok2 is True and "同名" not in msg2 and "离线" not in msg2, msg2


def test_repoint_note_on_failure_keeps_apology():
    """改指后执行失败 ⇒ 注照留（交代了改指），失败话术照旧，注不得声称办妥。"""
    ha = Ha(states={"light.she_deng": OFFLINE_TWIN,
                    "light.ban_gong_shi_she_deng": GOOD_LAMP},
            svc_results={"homeassistant.turn_on": {"success": False,
                                                   "error": "Entity not found"}})
    ok, msg = _run(ha, _klar("HassTurnOn", {"entity_id": "light.she_deng"}, "打开客厅的灯"))
    assert ok is False, msg
    assert "同名" in msg, msg
    assert "办妥" not in msg, msg
# ── 候选排序：离线不得排在可用之前（歧义环/能力预裁共用同一份表）──────
def test_resolve_candidates_puts_offline_last():
    states = {"light.she_deng": OFFLINE_TWIN, "light.ban_gong_shi_she_deng": GOOD_LAMP}
    got = resolve_candidates(states, {}, [{"area": "", "devices": [
        {"name": "灯", "domains": ["light"]}]}])
    ids = [e["entity_id"] for e in got]
    assert ids == ["light.ban_gong_shi_she_deng", "light.she_deng"], ids


def test_resolve_candidates_keeps_unknown_ordering():
    """'unknown' 不是离线：不得被沉底（否则刚重启的实体会被当成后备）。"""
    states = {"light.x": _ent("light.x", "unknown", "射灯"),
              "light.y": _ent("light.y", "off", "射灯"),
              "light.z": _ent("light.z", "unavailable", "射灯")}
    got = resolve_candidates(states, {}, [{"area": "", "devices": [
        {"name": "射灯", "domains": ["light"]}]}])
    assert [e["entity_id"] for e in got] == ["light.x", "light.y", "light.z"], got
