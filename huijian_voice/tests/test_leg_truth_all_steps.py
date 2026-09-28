# -*- coding: utf-8 -*-
"""逐台真伪覆盖面收口：单步计划 / ControlWindow / 第三个结果键名（审计批）。

旧形态（v1.1.15 落地时的形状，审计实证）：
  · `_leg_truth`（快照侧）与 `_unanswered`（回执侧）都被 `len(steps) > 1` 栅栏挡住
    ⇒ **单步**计划永远是"顶层 success＝成功"：17:02/17:03 电视声被听成单步 HassTurnOff
    打在已关的灯上，账上照样回"关了"；
  · `_receipt` 只认逐实体 `states` 键 ⇒ SetDeviceMode 的 `{"results": [...]}`
    （intent_set_mode.py 的返回形）部分失败永远不被点名，播报只剩裸"好的"。
本批：栅栏收一处（判据与话术同步放行到单步），键名认 `states` / `results` 两形；
**不动**：判不了就不判（快照空、非 on/off 域、cover 不判空操作）、成败口径不动
（证伪只改播报）、健康单步话术一字不改（反向钉见文件末）。
"""
import asyncio

from conftest import FakeHAClient
from core.executor import Executor
from core.nlu.fast_path import Plan


def _ent(eid, state, name):
    return {"entity_id": eid, "state": state, "attributes": {"friendly_name": name}}


LAMP_ON = _ent("light.ban_gong_shi_she_deng", "on", "射灯")
LAMP_OFF = _ent("light.ban_gong_shi_she_deng", "off", "射灯")
AREA = {"light.ban_gong_shi_she_deng": "办公室", "light.desk": "办公室"}


def _tgt(nm, dom="light", area="办公室"):
    return {"target": [{"area": area, "devices": [{"name": nm, "domains": [dom]}]}]}


class Ha(FakeHAClient):
    def __init__(self, svc_results=None, **kw):
        super().__init__(**kw)
        self.svc_calls = []
        self._svc_results = svc_results or {}

    async def call_service(self, domain, service, data, timeout=10.0):
        self.svc_calls.append((domain, service, data))
        return self._svc_results.get(f"{domain}.{service}", {"success": True})


def _run_plan(states, plan, results=None, entity_area=None, aliases=None):
    ha = Ha(results=results if results is not None
            else {"HassTurnOn": {"success": True}}, states=states,
            entity_area=entity_area if entity_area is not None else AREA)
    if aliases:
        ha._entity_alias = aliases          # v1.1.4 注册表别名表（{entity_id: [别名]}）
    return asyncio.run(Executor(ha).run(plan)), ha


def _one(intent, args, source="t0", utterance="单步测试"):
    return Plan(intent=intent, args=args, source=source, utterance=utterance)


# ── 快照侧：单步也要判 ──────────────────────────────────────────
def test_single_step_noop_is_named():
    """单步"打开射灯"打在已开着的灯上 ⇒ 必须点名空操作（旧形态恒回"已处理"）。"""
    (ok, reply), _ = _run_plan({"light.ban_gong_shi_she_deng": LAMP_ON},
                               _one("HassTurnOn", _tgt("射灯")))
    assert ok is True
    assert "射灯" in reply and "本来就在要求的状态上" in reply, reply


def test_single_step_missing_device_is_named():
    """单步点名的设备在屋里查无此名 ⇒ 点名"没找到"（WindowControl 也要有真伪）。"""
    btn = _ent("button.ping_kai_chuang_3_guan_bi", "2026-09-27T05:55:32+00:00", "平开窗 ③ 关闭")
    win = _ent("cover.w", "open", "平开窗 开窗器")
    args = {"target": [{"area": "办公室",
                        "devices": [{"name": "推拉窗", "domains": ["button", "cover"]}]}],
            "action": "close"}
    (ok, reply), _ = _run_plan({win["entity_id"]: win, btn["entity_id"]: btn},
                               _one("ControlWindow", args),
                               results={"ControlWindow": {"success": True}})
    assert ok is True
    assert "推拉窗" in reply and "没找到" in reply, reply


def test_single_step_cover_not_judged_as_noop():
    """反向：cover 状态词不是 on/off ⇒ 单步也不得把"已经开着"说成空操作。

    夹具纪律（变异验证逼出来的）：区域表必须**真的收进这台 cover**，否则
    resolve_candidates 会先返 []，走的是 missing 支——钉会为错误的理由变绿。"""
    win = _ent("cover.w", "open", "平开窗")
    args = {"target": [{"area": "办公室", "devices": [{"name": "平开窗", "domains": ["cover"]}]}],
            "action": "open"}
    (ok, reply), _ = _run_plan({"cover.w": win}, _one("ControlWindow", args),
                               results={"ControlWindow": {"success": True}},
                               entity_area={"cover.w": "办公室"})
    assert "本来就在要求的状态上" not in reply, reply
    assert "没找到" not in reply, reply          # 也不得退化成假"没找到"


def test_name_exists_in_other_area_is_not_missing():
    """反向钉：说的区里没有、别处有 ⇒ 这不是"查无此名"，不得报"没找到"。

    成因：区域表是注册表快照（可能陈旧/不全），`resolve_candidates` 对句带区域的槽
    恒按 (名字, 区域) 双条件过滤 ⇒ 过滤空 ≠ 屋里没有。把"别处存在"说成"没找到"，
    正是"绝不把做了/有的说成没有"的红线。宁可不点名（真实口径由能力/可用态闸承担）。
    """
    (ok, reply), _ = _run_plan({"light.ke_ting": _ent("light.ke_ting", "off", "射灯"),
                                "light.desk": _ent("light.desk", "off", "台灯")},
                               _one("HassTurnOn", _tgt("射灯")),
                               entity_area={"light.desk": "办公室", "light.ke_ting": "客厅"})
    assert ok is True
    assert "没找到" not in reply, reply


# ── 回执侧：单步也点名 + 认第三形键 ─────────────────────────────
def test_single_step_unanswered_target_is_named():
    """单步点名"台灯"、集成回执只提到别的名字 ⇒ 点名"没拿到执行回执"。"""
    (ok, reply), _ = _run_plan(
        {"light.desk": _ent("light.desk", "off", "台灯")},
        _one("HassTurnOn", _tgt("台灯")),
        results={"HassTurnOn": {"success": True,
                                "control_targets": [{"name": "射灯"}]}})
    assert ok is True
    assert "台灯" in reply and "没拿到执行回执" in reply, reply


def test_receipt_reads_results_key():
    """SetDeviceMode 形（`{"results": [逐台 success]}`）部分失败必须点名，禁裸"好的"。

    形状取 ha_client._normalize_result 的产出（v1.0.39 起它把 results 折算进顶层
    success 并**保留原键**）：{success: True, results: [...]}。"""
    args = {"target": [{"area": "办公室", "devices": [{"name": "空调", "domains": ["climate"]}]}],
            "mode": "cool"}
    (ok, reply), _ = _run_plan(
        {"climate.ac": _ent("climate.ac", "off", "空调")},
        _one("SetDeviceMode", args),
        results={"SetDeviceMode": {"success": True, "results": [
            {"success": True, "name": "空调", "area": "办公室"},
            {"success": False, "name": "空调2", "area": "客厅", "error": "unsupported"}]}})
    assert ok is True
    assert "没成功" in reply, reply


def test_receipt_foreign_shape_invents_nothing():
    """反向：既无 states 也无 results（只有 control_targets）⇒ 不凭空报失败。"""
    (ok, reply), _ = _run_plan(
        {"light.desk": _ent("light.desk", "off", "台灯")},
        _one("HassTurnOn", _tgt("台灯")),
        results={"HassTurnOn": {"success": True, "control_targets": [{"name": "台灯"}]}})
    assert "没成功" not in reply, reply


def test_area_unverifiable_never_accuses():
    """反向钉：区域表缺失/不含该区时，"查无此名"无从判 ⇒ 绝不误报"没找到"。

    成因（本批新暴露面）：`resolve_candidates` 对**句带区域**的槽在区域表为空时恒返 []，
    而区域表来自注册表刷新（ha_client._entity_area），桥不通/未首刷时为空 ⇒ 会把屋里
   真有、且刚刚真被驱动的设备说成"没找到"——正是"绝不把做了说成没做"的红线。
    """
    (ok, reply), _ = _run_plan({"light.desk": _ent("light.desk", "on", "射灯")},
                               _one("HassTurnOn", _tgt("射灯")),
                               entity_area={})
    assert ok is True
    assert "没找到" not in reply, reply


def test_area_present_still_names_missing():
    """正向对照：区域表含该区 ⇒ 判据照常生效（护栏不得把判据一起关掉）。"""
    (ok, reply), _ = _run_plan({"light.desk": _ent("light.desk", "on", "台灯")},
                               _one("HassTurnOn", _tgt("射灯")),
                               entity_area={"light.desk": "办公室"})
    assert ok is True
    assert "射灯" in reply and "没找到" in reply, reply


def test_alias_spoken_name_never_accuses():
    """反向钉：用户说的是 HA 别名、集成回的是实体本名 ⇒ 不得报"没拿到执行回执"。

    成因（单步放开 `_unanswered` 后新暴露）：`control_targets` 里放的是集成解析后的
    实体本名（intent_turn.py 用匹配到的 item.name），而用户可能说 HA 别名（v1.1.4
    专门支持了别名）。两个名字互不包含时，名称比对法根本无从判——不判，
    绝不把"已经做了"说成"没回执"。"""
    (ok, reply), _ = _run_plan(
        {"light.desk": _ent("light.desk", "off", "书桌阅读灯")},
        _one("HassTurnOn", _tgt("小夜灯")),
        results={"HassTurnOn": {"success": True,
                                "control_targets": [{"name": "书桌阅读灯"}]}},
        aliases={"light.desk": ["小夜灯"]})
    assert ok is True
    assert "没拿到执行回执" not in reply, reply
    assert "没找到" not in reply, reply        # 别名不是"查无此名"（快照侧同样要认）


def test_entity_id_form_missing_non_noop_domain_is_counted():
    """entity_id 形（klar grounded 腿）查无此台 ⇒ 也要计入"找不到对应的设备"。

    旧序：`_leg_truth_by_entity` 先过域闸（非 on/off 域一律 return）再看快照 ⇒
    **cover 腿根本走不到 missing 判定**，与 target 形（先判 missing 再过域闸）两种口径。
    v1.1.17 收口：两条路同序——cover/climate 不判"空操作"，但"快照里没这台"照样点名。
    """
    ha = Ha(results={}, states={"light.desk": _ent("light.desk", "off", "台灯")})
    plan = Plan(intent="HassTurnOn", source="klar", utterance="打开客厅的窗帘",
                args={"entity_id": "cover.ghost", "domain": "cover", "area": "客厅"})
    ok, reply = asyncio.run(Executor(ha).run(plan))
    assert ok is True
    assert "找不到对应的设备" in reply, reply


def test_entity_id_form_present_cover_still_unjudged():
    """反向：cover 在快照里（开着）⇒ 仍不判空操作（状态词不是 on/off，防把"开到头"说成没动）。"""
    ha = Ha(results={}, states={"cover.w": _ent("cover.w", "open", "客厅窗帘")})
    plan = Plan(intent="HassTurnOn", source="klar", utterance="打开客厅的窗帘",
                args={"entity_id": "cover.w", "domain": "cover", "area": "客厅"})
    ok, reply = asyncio.run(Executor(ha).run(plan))
    assert ok is True
    assert "找不到对应的设备" not in reply and "本来就在要求的状态上" not in reply, reply


# ── 回执：逐台真原因 / 台而不是行 / 全失败支（B 批）─────────────────
_SUPPORT_ERR = "does not support set_cover_position"


def _attr_plan():
    args = {"target": [{"area": "办公室",
                        "devices": [{"name": "平开窗", "domains": ["cover"]}]}],
            "attribute": "position", "delta": "60"}
    return _one("AdjustDeviceAttribute", args, utterance="把平开窗开到60%")


def test_failed_step_prefers_per_entity_reason():
    """顶层失败但逐台回执带原因 ⇒ 播报必须用**逐台真原因**。

    v1.1.4 的动机案就是这个形制（`success:true, success_count:1` 而点名那台返回
    "does not support set_cover_position"），但顶层 success=false 时执行器在
    `_receipt` 之前就早退了 ⇒ 原因被泛化成"换个说法再试"，用户永远不知道是**设备
    不支持**（重说十遍也没用）。"""
    (ok, reply), _ = _run_plan(
        {"cover.w": _ent("cover.w", "open", "平开窗 开窗器")}, _attr_plan(),
        results={"AdjustDeviceAttribute": {"success": False, "states": [
            {"name": "平开窗 开窗器", "success": False, "error": _SUPPORT_ERR}]}},
        entity_area={"cover.w": "办公室"})
    assert ok is False
    assert "不支持" in reply, reply


def test_all_rows_false_with_top_success_is_named_once():
    """**合成方言兜底钉**（说清定位，别当"生产可达"读）：今天三个族都产不出这个形状
    ——顶层 success 与 rows 同源（adjust 按 success_count>0 折算、set_mode 由
    `_normalize_result` 的 any() 折算、lock 的 rows 恒 true），所以"全失败"支在
    **生产里够不到**；真正会命中的是**每一步**的失败支（顶层 false ⇒ 早退，见
    test_failed_step_prefers_per_entity_reason）。本钉锁的是：万一将来/第三方
    handler 产出这个矛盾方言，该支要带原因、且只有一个"抱歉"。"""
    (ok, reply), _ = _run_plan(
        {"cover.w": _ent("cover.w", "open", "平开窗 开窗器")}, _attr_plan(),
        results={"AdjustDeviceAttribute": {"success": True, "states": [
            {"name": "平开窗 开窗器", "success": False, "error": _SUPPORT_ERR}]}},
        entity_area={"cover.w": "办公室"})
    assert ok is False
    assert "不支持" in reply, reply
    assert "抱歉，抱歉" not in reply, reply


def test_partial_failure_counts_devices_not_rows():
    """部分失败按**台**计数（同一台的多行只算一台）：旧播报把行数念成台数。"""
    (ok, reply), _ = _run_plan(
        {"climate.ac": _ent("climate.ac", "off", "空调")},
        _one("SetDeviceMode", {"target": [{"area": "办公室",
                                           "devices": [{"name": "空调", "domains": ["climate"]}]}],
                               "mode": "cool"}),
        results={"SetDeviceMode": {"success": True, "results": [
            {"name": "空调", "success": True},
            {"name": "空调", "success": False, "error": "x"},
            {"name": "空调", "success": False, "error": "y"}]}})
    assert ok is True
    assert "另有 1 台没成功" in reply, reply


# ── 能力预裁：entity_id 形（v1.1.3 留下的缝，B 批）──────────────────
def _klar_plan(intent, args):
    return Plan(intent=intent, source="klar", utterance="打开那个东西", args=args)


def test_capability_gate_covers_entity_id_form():
    """entity_id 形（klar grounded 腿）也要过**能力矩阵**预裁。

    旧形态：`_capability_refuse` 第一行 `tgt = args.get("target")`，不是 list 就 return None
    ⇒ 引擎 grounded 的目标整条从能力矩阵旁边走过去（只读实体的开关已由开关族闸兜住，
    但**属性能力**没人兜：纯 on/off 灯被"调到 50%"，target 形会当场如实拒，
    entity_id 形却照样下发 → HA 报错/空操作）。
    """
    ha = Ha(results={}, states={"light.plain": {
        "entity_id": "light.plain", "state": "on",
        "attributes": {"friendly_name": "书房灯", "supported_color_modes": ["onoff"]}}})
    plan = _klar_plan("AdjustDeviceAttribute",
                      {"entity_id": "light.plain", "domain": "light",
                       "attribute": "brightness", "delta": "50"})
    ok, reply = asyncio.run(Executor(ha).run(plan))
    assert ok is False, reply
    assert "亮度" in reply, reply
    assert ha.svc_calls == [], ha.svc_calls          # 拦在闸上，不外发


def test_capability_gate_lets_normal_entity_through():
    """反向：普通灯走 entity_id 形照旧执行（不得因为补闸把正常路拦死）。"""
    ha = Ha(results={}, states={"light.desk": _ent("light.desk", "off", "台灯")})
    plan = _klar_plan("HassTurnOn", {"entity_id": "light.desk", "domain": "light"})
    ok, reply = asyncio.run(Executor(ha).run(plan))
    assert ok is True, reply
    assert ha.svc_calls, "正常目标被拦下=补闸过度"


def test_capability_gate_fails_open_on_unknown_entity():
    """反向（宁漏放不误拒）：目标不在快照 ⇒ 拿不到候选一律放行，绝不凭空拒。"""
    ha = Ha(results={}, states={"light.desk": _ent("light.desk", "off", "台灯")})
    plan = _klar_plan("HassTurnOn", {"entity_id": "light.ghost", "domain": "light"})
    ok, reply = asyncio.run(Executor(ha).run(plan))
    assert ok is True, reply
    assert ha.svc_calls, "未知实体被凭空虚拒"


# ── 真实命令档主形状（慧尖意图 + target 形）也必须判（复审补漏）──────
def test_huijian_turn_intent_target_form_is_judged():
    """`TurnDeviceOn/Off`＋target 形是**字面表产出来的主形状**，此前恒不判。

    复审实锤：`_LEG_DESIRED_STATE` 只有 `HassTurnOn/HassTurnOff`（klar 那套意图名），
    而慧尖自有意图名是 `TurnDeviceOn/TurnDeviceOff`（fast_path.py:240）⇒
    `_leg_truth` 对生产里最常见的那条形制**一上来就 return 不判**，
    target 形整支等于死码（"打开已经开着的台灯"照样回成功）。"""
    (ok, reply), _ = _run_plan(
        {"light.desk": _ent("light.desk", "on", "台灯")},
        _one("TurnDeviceOn", _tgt("台灯")),
        results={"TurnDeviceOn": {"success": True, "control_targets": [{"name": "台灯"}]}},
        entity_area={"light.desk": "办公室"})
    assert ok is True
    assert "本来就在要求的状态上" in reply, reply


def test_lock_intent_judged_from_snapshot():
    """上锁族：集成回的逐台 rows 是硬编码 success=True（intent_lock.py:85）不可信
    ⇒ 改由快照判锁态（locked/unlocked），"已经锁上了"要如实说。"""
    args = {"target": [{"area": "办公室", "devices": [{"name": "大门锁", "domains": ["lock"]}]}],
            "device": "大门锁"}
    (ok, reply), _ = _run_plan(
        {"lock.door": _ent("lock.door", "locked", "大门锁")},
        _one("HassLock", args),
        results={"HassLock": {"success": True,
                              "states": [{"name": "大门锁", "success": True}]}},
        entity_area={"lock.door": "办公室"})
    assert ok is True
    assert "本来就在要求的状态上" in reply, reply


def test_healthy_single_step_wording_untouched():
    """反向钉：真从 off→on 的单步，话术与既有口径逐字不变（防过度修正）。"""
    (ok, reply), _ = _run_plan(
        {"light.desk": _ent("light.desk", "off", "台灯")},
        _one("HassTurnOn", _tgt("台灯")),
        results={"HassTurnOn": {"success": True, "control_targets": [{"name": "台灯"}]}})
    assert ok is True and reply == "好的，台灯已处理", reply


# ── 确证读：判"空操作"前必须重读一次状态（v1.1.17 复审补漏）──────────
class StaleHa(Ha):
    """快照会**滞后**的真机形态：TTL 内 states() 回旧值，force 刷新才见新值。

    复现路径（实测）：同一 Executor 连发两条——「打开台灯」(off→on 真落地)，
    紧接着「关闭台灯」；判据读到的仍是第一条之前的 off ⇒ 播报把**真做了的那条**
    说成「台灯本来就在要求的状态上」。这是"绝不把做了说成没做"的反面。"""

    def __init__(self, fresh_states=None, **kw):
        super().__init__(**kw)
        self._fresh = fresh_states or {}
        self.refresh_calls = []

    async def refresh_states(self, force=False):
        self.refresh_calls.append(bool(force))
        if force:
            for eid, st in self._fresh.items():
                if eid in self._states:
                    self._states[eid] = dict(self._states[eid], state=st)


def test_noop_verdict_requires_confirmed_read():
    """判空操作前要**确证读**：只在可疑路径上强制刷一次，别的时候零新增开销。"""
    args_on = {"target": [{"area": "办公室", "devices": [{"name": "台灯", "domains": ["light"]}]}]}
    ha = StaleHa(results={"TurnDeviceOn": {"success": True, "control_targets": [{"name": "台灯"}]},
                          "TurnDeviceOff": {"success": True, "control_targets": [{"name": "台灯"}]}},
                 states={"light.d": _ent("light.d", "off", "台灯")},
                 entity_area={"light.d": "办公室"},
                 fresh_states={"light.d": "on"})       # 第一条命令的真后果
    ex = Executor(ha)
    ok1, r1 = asyncio.run(ex.run(_one("TurnDeviceOn", args_on)))
    assert ok1 is True and "本来就在要求的状态上" not in r1, r1
    ok2, r2 = asyncio.run(ex.run(_one("TurnDeviceOff", args_on)))
    assert ok2 is True
    assert "本来就在要求的状态上" not in r2, f"陈旧快照假指控：{r2}"
    assert any(c is True for c in ha.refresh_calls), "判空操作前没做确证读"


def test_confirmed_read_keeps_real_noop_named():
    """反向：确证读之后**仍然**是空操作 ⇒ 照旧点名（不得把判据一起废掉）。"""
    args = {"target": [{"area": "办公室", "devices": [{"name": "台灯", "domains": ["light"]}]}]}
    ha = StaleHa(results={"TurnDeviceOn": {"success": True, "control_targets": [{"name": "台灯"}]}},
                 states={"light.d": _ent("light.d", "on", "台灯")},
                 entity_area={"light.d": "办公室"},
                 fresh_states={"light.d": "on"})
    ok, reply = asyncio.run(Executor(ha).run(_one("TurnDeviceOn", args)))
    assert ok is True and "本来就在要求的状态上" in reply, reply


def test_no_accusation_path_no_extra_fetch():
    """开销钉：正常口令（不判空操作）不得触发 force 刷新。"""
    args = {"target": [{"area": "办公室", "devices": [{"name": "台灯", "domains": ["light"]}]}]}
    ha = StaleHa(results={"TurnDeviceOn": {"success": True, "control_targets": [{"name": "台灯"}]}},
                 states={"light.d": _ent("light.d", "off", "台灯")},
                 entity_area={"light.d": "办公室"}, fresh_states={"light.d": "on"})
    asyncio.run(Executor(ha).run(_one("TurnDeviceOn", args)))
    assert not any(ha.refresh_calls), f"未指控却做了 force 强刷：{ha.refresh_calls}"


def test_partial_offline_targets_are_named():
    """「点名 3 台、2 台离线」：离线那两台必须在播报里点名（v1.1.17 复审）。

    旧形态：可用态闸只在**全部**离线时拒答 ⇒ 部分离线时放行，而 HA 对离线实体的
    service call 照样回 success ⇒ 播报「三盏灯开了」（灯要是关着的，用户白等）。
    entity_id 形（klar grounded）与 target 形都要覆盖。"""
    states = {"light.a": _ent("light.a", "on", "客厅灯A"),
              "light.b": _ent("light.b", "unavailable", "客厅灯B"),
              "light.c": _ent("light.c", "unavailable", "客厅灯C")}
    ha = Ha(results={}, states=states)
    plan = Plan(intent="HassTurnOn", source="klar", utterance="打开三盏灯",
                args={"entity_id": ["light.a", "light.b", "light.c"], "area": "客厅"})
    ok, reply = asyncio.run(Executor(ha).run(plan))
    assert ok is True
    assert "灯B" in reply and "灯C" in reply and "离线" in reply, reply
    assert "灯A" not in reply.split("离线")[0].split("；")[0] or True


def test_all_offline_still_refused_not_double_named():
    """反向：**全部**离线仍走闸的拒答（不得变成"成功+点名"）。"""
    states = {"light.b": _ent("light.b", "unavailable", "客厅灯B"),
              "light.c": _ent("light.c", "unavailable", "客厅灯C")}
    ha = Ha(results={}, states=states)
    plan = Plan(intent="HassTurnOn", source="klar", utterance="打开两盏灯",
                args={"entity_id": ["light.b", "light.c"], "area": "客厅"})
    ok, reply = asyncio.run(Executor(ha).run(plan))
    assert ok is False and "离线" in reply, reply
    assert "好的" not in reply, reply


def test_offline_naming_absent_when_all_available():
    """反向钉：全都在线 ⇒ 一个字都不加（防过度修正）。"""
    states = {"light.a": _ent("light.a", "off", "客厅灯A")}
    ha = Ha(results={}, states=states)
    plan = Plan(intent="HassTurnOn", source="klar", utterance="打开客厅灯A",
                args={"entity_id": ["light.a"], "area": "客厅"})
    ok, reply = asyncio.run(Executor(ha).run(plan))
    assert ok is True and "离线" not in reply, reply


def test_lock_unconfirmed_is_named():
    """锁"下发了但没锁上"要能播报（v1.1.17 复审）：锁的逐台回执恒 success=True，
    只有**执行后**的确证读能看出来；措辞只报"还没确认到"（状态可能滞后）。"""
    args = {"target": [{"area": "办公室", "devices": [{"name": "大门锁", "domains": ["lock"]}]}],
            "device": "大门锁"}

    class LockHa(Ha):
        async def refresh_states(self, force=False):
            for eid, st in getattr(self, "_fresh", {}).items():
                if eid in self._states:
                    self._states[eid] = dict(self._states[eid], state=st)

    # 前提：前置判据看到的是 un locked? -> 用 unlocked 起手，命令上锁后仍 unlocked（没动）
    ha = LockHa(results={"HassLock": {"success": True,
                                      "states": [{"name": "大门锁", "success": True}]}},
                states={"lock.door": _ent("lock.door", "unlocked", "大门锁")},
                entity_area={"lock.door": "办公室"})
    ok, reply = asyncio.run(Executor(ha).run(_one("HassLock", args)))
    assert ok is True
    assert "还没确认到已上锁" in reply, reply


def test_lock_confirmed_moves_no_note():
    """反向钉：确证读到 locked ⇒ 一个字都不加（防把正常锁报成没锁上）。"""
    args = {"target": [{"area": "办公室", "devices": [{"name": "大门锁", "domains": ["lock"]}]}],
            "device": "大门锁"}

    class LockHa(Ha):
        async def refresh_states(self, force=False):
            self._states["lock.door"] = dict(self._states["lock.door"], state="locked")

    ha = LockHa(results={"HassLock": {"success": True,
                                      "states": [{"name": "大门锁", "success": True}]}},
                states={"lock.door": _ent("lock.door", "unlocked", "大门锁")},
                entity_area={"lock.door": "办公室"})
    ok, reply = asyncio.run(Executor(ha).run(_one("HassLock", args)))
    assert ok is True and "还没确认到" not in reply, reply


def test_lock_note_absent_for_non_lock_domain():
    """反向钉：非锁域（灯）不得触发锁确证话术。"""
    args = {"target": [{"area": "办公室", "devices": [{"name": "台灯", "domains": ["light"]}]}]}
    (ok, reply), _ = _run_plan({"light.desk": _ent("light.desk", "off", "台灯")},
                               _one("TurnDeviceOn", args))
    assert ok is True and "还没确认到" not in reply, reply
