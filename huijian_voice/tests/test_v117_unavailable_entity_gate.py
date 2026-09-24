"""v1.1.7 钉桩：执行器「目标实体不可用」闸（防对 offline 实体谎报成功）。

办公室实锤（2026-09-24，HA .91 / 32b8）：用户说「把客厅的灯关」，klar grounded
成 {entity_id: light.she_deng}，而 light.she_deng 实测 state=unavailable（与可用的
light.ban_gong_shi_she_deng 同名「射灯」的离线孪生）。HA 对 unavailable 实体的
light.turn_off **照样回 success**（空操作），而 klar 直调走 call_service、结果无
per-entity `states`，_receipt 回落顶层 success ⇒ 播报「客厅的灯关了」谎报成功。

闸规则（用户定案：先只修执行器 availability 闸，klar 过度匹配另议）：
  · 目标 entity_id **全部**在 states 快照里且**全部** state=='unavailable' → 如实失败；
  · 任一可用 / 目标不在快照（未知）/ 快照空（桥不通）→ 放行（宁漏放不误拒，
    同 capability.py:116/201 纪律）；
  · 只认 'unavailable'（确证离线），不碰 'unknown'（推送实体未首 poll 的瞬态）。
"""
import asyncio

from conftest import FakeHAClient
from core.executor import Executor
from core.nlu.fast_path import Plan


class Ha(FakeHAClient):
    """FakeHAClient 补 call_service：模拟 HA 对 unavailable 实体仍回 success。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.svc_calls = []

    async def call_service(self, domain, service, data, timeout=10.0):
        self.svc_calls.append((domain, service, data))
        return {"success": True}


def _run(ha, plan):
    return asyncio.run(Executor(ha, None).run(plan))


def _klar(intent, args, utterance):
    return Plan(intent=intent, args=args, source="klar", utterance=utterance)


def _ent(eid, state, name="射灯"):
    return {"entity_id": eid, "state": state, "attributes": {"friendly_name": name}}


# ── 拦：确证不可用 → 如实失败，绝不谎报、绝不外发 ──────────────
def test_unavailable_entity_refused_no_false_success():
    ha = Ha(states={"light.she_deng": _ent("light.she_deng", "unavailable")})
    ok, msg = _run(ha, _klar("HassTurnOff", {"entity_id": "light.she_deng"},
                             "把客厅的灯关"))
    assert ok is False
    assert msg.startswith("抱歉")
    assert ("离线" in msg) or ("不可用" in msg)
    # 谎报防线：结果绝不含成功字尾
    assert "关了" not in msg and "开了" not in msg and "已关" not in msg
    # 拦下即两通道均不得外发（对 offline 实体发也是空操作，没必要发）
    assert ha.svc_calls == [] and ha.calls == []


def test_unavailable_named_in_message():
    """播报点名离线设备，用户知道是哪台没动。"""
    ha = Ha(states={"light.she_deng": _ent("light.she_deng", "unavailable", "射灯")})
    _, msg = _run(ha, _klar("HassTurnOn", {"entity_id": "light.she_deng"}, "打开床头灯"))
    assert "射灯" in msg


def test_unavailable_list_all_offline_refused():
    ha = Ha(states={"light.a": _ent("light.a", "unavailable", "甲灯"),
                    "light.b": _ent("light.b", "unavailable", "乙灯")})
    ok, _ = _run(ha, _klar("HassTurnOff",
                           {"entity_id": ["light.a", "light.b"]}, "关灯"))
    assert ok is False
    assert ha.svc_calls == []


# ── 放：既有合法路 / 不确定态 一律不误伤 ──────────────────────
def test_available_entity_passes_and_calls():
    ha = Ha(states={"light.she_deng": _ent("light.she_deng", "on")})
    ok, _ = _run(ha, _klar("HassTurnOff", {"entity_id": "light.she_deng"}, "关灯"))
    assert ok is True
    assert ha.svc_calls, "可用实体必须照常外发"


def test_partial_unavailable_passes():
    """有一台可用 → 放行，部分失败交 _receipt 逐实体点名（不在本闸扩大解释）。"""
    ha = Ha(states={"light.a": _ent("light.a", "unavailable"),
                    "light.b": _ent("light.b", "on")})
    ok, _ = _run(ha, _klar("HassTurnOff",
                           {"entity_id": ["light.a", "light.b"]}, "关灯"))
    assert ok is True
    assert ha.svc_calls


def test_unknown_state_passes():
    """unknown 非确证离线（推送实体未首 poll 的瞬态），放行。"""
    ha = Ha(states={"light.x": _ent("light.x", "unknown")})
    ok, _ = _run(ha, _klar("HassTurnOff", {"entity_id": "light.x"}, "关灯"))
    assert ok is True


def test_entity_absent_from_snapshot_passes():
    """目标不在快照=未知（快照可能不全），不凭空拒。"""
    ha = Ha(states={"light.other": _ent("light.other", "on")})
    ok, _ = _run(ha, _klar("HassTurnOff", {"entity_id": "light.ghost"}, "关灯"))
    assert ok is True


def test_empty_states_bridge_down_passes():
    """快照空（桥不通）：拒=凭空少做，放行（同 capability.py:201）。"""
    ha = Ha(states={})
    ok, _ = _run(ha, _klar("HassTurnOff", {"entity_id": "light.she_deng"}, "关灯"))
    assert ok is True


def test_no_entity_id_target_form_untouched():
    """无 entity_id 的 target/area 形不走本闸（交 core 解析 + _receipt）。"""
    ha = Ha(states={"light.she_deng": _ent("light.she_deng", "unavailable")})
    ok, _ = _run(ha, _klar("HassTurnOff", {"area": "客厅"}, "关客厅灯"))
    assert ok is True


def test_gate_never_raises_on_garbage():
    ha = Ha(states={"light.she_deng": _ent("light.she_deng", "unavailable")})
    ok, _ = _run(ha, Plan(intent="HassTurnOff",
                          args={"entity_id": [None, 42, "no_dot"]},
                          source="klar", utterance=None))
    assert ok is True  # 无合法 eid → 本闸放行
