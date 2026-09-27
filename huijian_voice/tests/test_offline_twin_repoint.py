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

    def __init__(self, **kw):
        super().__init__(**kw)
        self.svc_calls = []

    async def call_service(self, domain, service, data, timeout=10.0):
        self.svc_calls.append((domain, service, data))
        return {"success": True}


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
    """唯一同名可用台 → 改指并真执行，不再回"离线"。"""
    ha = Ha(states={"light.she_deng": OFFLINE_TWIN,
                    "light.ban_gong_shi_she_deng": GOOD_LAMP})
    ok, msg = _run(ha, _klar("HassTurnOn", {"entity_id": "light.she_deng"}, "打开客厅的灯"))
    assert ok is True, msg
    assert "离线" not in msg and "不可用" not in msg, msg
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
