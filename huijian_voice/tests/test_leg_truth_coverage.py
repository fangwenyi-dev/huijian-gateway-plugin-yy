"""v1.1.15 钉桩 E1/E2/E3：链式逐腿的"判得了、说清第几步、点名没落地的台"。

三条都是 D6（混形链通道）之后才暴露/才可修的邻面，都源自 2026-09-27 办公 .91 现场：
  E1 `_leg_truth` 只看 `args['target']`，klar grounded 的 `entity_id` 形分句恒"无从证伪"
     ⇒ 直调回来的空操作/查无此台仍被算进"都办妥了"。
  E2 三道前置闸（开关族能力闸 / 能力预裁 / 可用态闸）命中即整链 return False，
     却拿不到 P2-12 的步序定位——**首腿往往已经真执行了**，播报听成"整句没做"。
  E3 慧尖意图的返回里根本没有逐实体 `states`（intent_turn.py:184-187 只回
     `{success, control_targets}`），集成口径又是"control_targets 非空即成功"
     （:179-183）⇒ `_receipt` 对 Turn*/ControlWindow 恒 0/0，点名两台只落一台看不出来。

纪律与 D2 同：判不了就不判（快照空、非 on/off 域、'unknown'、泛称一律不动话术），
宁可不点名，也绝不把"做了"说成"没做"。
"""
import asyncio

from conftest import FakeHAClient
from core.executor import Executor
from core.nlu.fast_path import Plan

LAMP = {"entity_id": "light.ban_gong_shi_she_deng", "domain": "light", "area": "办公室"}
GHOST = {"entity_id": "light.bedside_lamp", "domain": "light", "area": "卧室"}


class Ha(FakeHAClient):
    """两条外发通道都可注入回执（intent 侧 results=，直调侧 svc_results= 按
    "domain.service" 取）——D6 之后 klar 腿走直调，只配 intent 侧永远测不到它。"""

    def __init__(self, svc_results=None, **kw):
        super().__init__(**kw)
        self.svc_calls = []
        self._svc = svc_results or {}

    async def call_service(self, domain, service, data, timeout=10.0):
        self.svc_calls.append((domain, service, data))
        return self._svc.get(f"{domain}.{service}", {"success": True})


def _ent(eid, state, name):
    return {"entity_id": eid, "state": state, "attributes": {"friendly_name": name}}


def _tgt(nm, dom="light", area="办公室"):
    return {"target": [{"area": area, "devices": [{"name": nm, "domains": [dom]}]}]}


def _chain(first, legs, utterance="链测试", source="t0"):
    """首腿可写 (intent, args) 或 (intent, args, source)；次腿一律三元组。"""
    fi, fa = first[0], first[1]
    fs = first[2] if len(first) > 2 else source
    return Plan(intent=fi, args=fa, source=fs, utterance=utterance,
                extra_steps=[{"name": n, "args": a, "source": s} for n, a, s in legs])


def _run(plan, states, results=None, svc=None, area=None):
    ha = Ha(results=results or {}, states=states, entity_area=area or {},
            svc_results=svc)
    ok, reply = asyncio.run(Executor(ha).run(plan))
    return ok, reply


# ── E1：entity_id 形分句也能证伪 ────────────────────────────────
def test_entity_id_leg_noop_is_named():
    """klar 直调腿对已 off 的灯说"关"⇒ 空操作要点名，不得混进"都办妥了"。"""
    ok, reply = _run(
        _chain(("TurnDeviceOff", _tgt("台灯")),
               [("HassTurnOff", dict(LAMP), "klar")]),
        {"light.ban_gong_shi_she_deng": _ent("light.ban_gong_shi_she_deng", "off", "射灯"),
         "light.desk": _ent("light.desk", "off", "台灯")},
        results={"TurnDeviceOff": {"success": True}},
        svc={"homeassistant.turn_off": {"success": True}})
    assert "射灯" in reply and "本来就在要求的状态上" in reply, reply
    assert "都办妥了" not in reply, reply


def test_entity_id_leg_absent_from_snapshot_says_so_without_reading_ids():
    """快照非空却查无这台 ⇒ 按数量如实说，绝不把 entity_id 念进播报。"""
    ok, reply = _run(
        _chain(("TurnDeviceOff", _tgt("台灯")), [("HassTurnOff", dict(GHOST), "klar")]),
        {"light.desk": _ent("light.desk", "off", "台灯")},
        results={"TurnDeviceOff": {"success": True}},
        svc={"homeassistant.turn_off": {"success": True}})
    assert "找不到对应的设备" in reply, reply
    assert "light.bedside_lamp" not in reply, reply        # 念 ID 是噪音
    assert "都办妥了" not in reply, reply


def test_entity_id_leg_unknown_state_is_not_judged():
    """'unknown'（未首 poll 瞬态）不判——同 _availability_refuse 的口径。"""
    ok, reply = _run(
        _chain(("TurnDeviceOff", _tgt("台灯")),
               [("HassTurnOff", {"entity_id": "light.x"}, "klar")]),
        {"light.x": _ent("light.x", "unknown", "射灯"),
         "light.desk": _ent("light.desk", "off", "台灯")},
        results={"TurnDeviceOff": {"success": True}},
        svc={"homeassistant.turn_off": {"success": True}})
    assert "本来就在要求的状态上" not in reply and "找不到" not in reply, reply


def test_entity_id_leg_cover_domain_not_judged_as_noop():
    """cover 的状态词不是 on/off ⇒ 不判空操作（防把"开到头"说成没动）。"""
    ok, reply = _run(
        _chain(("TurnDeviceOff", _tgt("台灯")),
               [("HassTurnOff", {"entity_id": "cover.w"}, "klar")]),
        {"cover.w": _ent("cover.w", "open", "平开窗"),
         "light.desk": _ent("light.desk", "off", "台灯")},
        results={"TurnDeviceOff": {"success": True}},
        svc={"homeassistant.turn_off": {"success": True}})
    assert "本来就在要求的状态上" not in reply, reply


def test_entity_id_list_leg_noop_named_by_friendly_name():
    """列表形 entity_id：全部已在要求状态才算空操作，名字取 friendly_name。"""
    ok, reply = _run(
        _chain(("TurnDeviceOff", _tgt("台灯")),
               [("HassTurnOn", {"entity_id": ["light.a", "light.b"]}, "klar")]),
        {"light.a": _ent("light.a", "on", "射灯"), "light.b": _ent("light.b", "on", "筒灯"),
         "light.desk": _ent("light.desk", "off", "台灯")},
        results={"TurnDeviceOff": {"success": True}},
        svc={"homeassistant.turn_on": {"success": True}})
    assert "射灯" in reply and "本来就在要求的状态上" in reply, reply


def test_entity_id_list_leg_partially_off_is_not_noop():
    """列表里有一台还没到位 ⇒ 不是空操作，不许点名（正向对照，防空口白话）。"""
    ok, reply = _run(
        _chain(("TurnDeviceOff", _tgt("台灯")),
               [("HassTurnOn", {"entity_id": ["light.a", "light.b"]}, "klar")]),
        {"light.a": _ent("light.a", "on", "射灯"), "light.b": _ent("light.b", "off", "筒灯"),
         "light.desk": _ent("light.desk", "off", "台灯")},
        results={"TurnDeviceOff": {"success": True}},
        svc={"homeassistant.turn_on": {"success": True}})
    assert "本来就在要求的状态上" not in reply, reply


# ── E2：前置闸早退也要说清动了几步 ──────────────────────────────
def test_gate_on_second_leg_reports_first_leg_already_done():
    """首腿（窗户）已真执行、次腿被开关族能力闸拦下 ⇒ 必须报"前面 1 步已完成"。

    句面「…平开窗…」+ 次腿 target 形 TurnDeviceOff 正是 v1.0.69 闸的触发形。"""
    states = {"light.ban_gong_shi_she_deng": _ent("light.ban_gong_shi_she_deng", "off", "射灯")}
    ok, reply = _run(
        _chain(("ControlWindow", {**_tgt("平开窗", dom="cover"), "action": "open"}),
               [("TurnDeviceOff", _tgt("射灯"), "t0")],
               utterance="打开办公室平开窗关闭办公室射灯"),
        states, results={"ControlWindow": {"success": True,
                                           "control_targets": [{"name": "平开窗",
                                                                "area": "办公室"}]}},
        area={"light.ban_gong_shi_she_deng": "办公室"})
    assert ok is False
    assert "前面 1 步已完成" in reply and "第 2 步" in reply, reply


def test_gate_on_first_leg_says_stop_before_rest():
    ok, reply = _run(
        _chain(("TurnDeviceOff", _tgt("射灯"), ),
               [("TurnDeviceOn", _tgt("台灯"), "t0")],
               utterance="打开平开窗然后关闭办公室射灯"),
        {"light.ban_gong_shi_she_deng": _ent("light.ban_gong_shi_she_deng", "off", "射灯")})
    assert ok is False
    assert "第 1 步没成功，后面的步骤先不执行了" in reply, reply


def test_single_step_gate_wording_untouched():
    """反向（防过度修正）：单步计划的闸话术一字不改，仍"抱歉，+ 原文"。"""
    ok, reply = _run(Plan(intent="TurnDeviceOff", args=_tgt("射灯"), source="t0",
                          utterance="打开平开窗关闭射灯"),
                     {"light.ban_gong_shi_she_deng": _ent("light.ban_gong_shi_she_deng",
                                                          "off", "射灯")})
    assert ok is False
    assert reply.startswith("抱歉，") and "步" not in reply, reply


# ── E3：点了名却没进 control_targets 的要揪出来 ─────────────────
def test_named_device_missing_from_control_targets_is_called_out():
    """同一腿点名两台、回执只有一台 ⇒ 另一台必须点名，不许"都办妥了"。"""
    states = {"light.ban_gong_shi_she_deng": _ent("light.ban_gong_shi_she_deng", "off", "射灯"),
              "light.desk": _ent("light.desk", "off", "台灯")}
    ok, reply = _run(
        _chain(("HassToggle", {"entity_id": "light.x"}, "klar"),
               [("TurnDeviceOff", {"target": [{"area": "办公室",
                                               "devices": [{"name": "射灯", "domains": ["light"]},
                                                          {"name": "台灯", "domains": ["light"]}]}]},
                "t0")]),
        states,
        results={"TurnDeviceOff": {"success": True,
                                   "control_targets": [{"name": "射灯", "area": "办公室"}]}},
        svc={"homeassistant.toggle": {"success": True}},
        area={"light.ban_gong_shi_she_deng": "办公室", "light.desk": "办公室"})
    assert ok is True                                   # 成败口径不动（与 D2 同）
    assert "「台灯」没拿到执行回执" in reply, reply
    assert "「射灯」没拿到执行回执" not in reply, reply      # 有回执的那台不许被点名
    assert "都办妥了" not in reply, reply


def test_resolved_longer_name_counts_as_answered():
    """集成回的是解析后的实体名（「平开窗 开窗器」）⇒ 包含即命中，不误报。"""
    ok, reply = _run(
        _chain(("HassToggle", {"entity_id": "light.x"}, "klar"),
               [("ControlWindow", {**_tgt("平开窗", dom="cover"), "action": "open"}, "t0")]),
        {"cover.w": _ent("cover.w", "closed", "平开窗 开窗器")},
        results={"ControlWindow": {"success": True,
                                   "control_targets": [{"name": "平开窗 开窗器",
                                                        "area": "办公室"}]}},
        svc={"homeassistant.toggle": {"success": True}})
    assert "没拿到执行回执" not in reply, reply


def test_generic_or_area_only_slot_not_compared():
    """泛称槽（name 为空）没有可比对象 ⇒ 跳过，绝不凭"回执里名字不同"喊没落地。"""
    ok, reply = _run(
        _chain(("HassToggle", {"entity_id": "light.x"}, "klar"),
               [("TurnDeviceOff", {"target": [{"area": "办公室",
                                               "devices": [{"name": "", "domains": ["light"]}]}]},
                "t0")]),
        {"light.a": _ent("light.a", "on", "射灯")},
        results={"TurnDeviceOff": {"success": True,
                                   "control_targets": [{"name": "射灯", "area": "办公室"}]}},
        svc={"homeassistant.toggle": {"success": True}})
    assert "没拿到执行回执" not in reply, reply


def test_direct_call_result_has_no_dialect_to_compare():
    """反向：直调回来的 {"success": true} 没有 control_targets 方言 ⇒ 不凭空判半执行。"""
    ok, reply = _run(
        _chain(("TurnDeviceOff", _tgt("台灯")),
               [("HassTurnOn", {"entity_id": "light.a"}, "klar")]),
        {"light.a": _ent("light.a", "off", "射灯"),
         "light.desk": _ent("light.desk", "on", "台灯")},
        results={"TurnDeviceOff": {"success": True}},
        svc={"homeassistant.turn_on": {"success": True}})
    assert "没拿到执行回执" not in reply, reply
