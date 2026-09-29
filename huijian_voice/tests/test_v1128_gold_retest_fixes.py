# -*- coding: utf-8 -*-
"""v1.1.28 真实 HA 金标复测（192.168.1.91）三条缺陷的回归钉。

三条都在真机上跑出来的误动作/误播报，不是纯推理：

① **区域只挂在设备层** ⇒ `HAClient._entity_area` 恒空（旧的 `_ws_registries`
   只拉 `config/entity_registry/list`，而本机该表 `area_id` **全为 null**），
   一切按区域收窄的逻辑（歧义闸/能力预裁/过宽目标闸/查询族）全部退化。
   修：补拉设备注册表做**区域继承**（实体自带区域恒优先）。

② **离线孪生污染播报**：`light.she_deng`（客厅，unavailable）与
   `light.ban_gong_shi_she_deng`（办公室）同名。执行「打开射灯」时办公室那台
   确实开了（复核 state=on），播报却是「好的，「射灯」现在离线、这条没执行」。
   修：离线点名改为**逐设备槽全票通过**才成立（同名里还有可用 ⇒ 不许点名）。

③ **畸形区域闸 vs 英文回捞**：英文句被双语桥拆成两个 target 槽，第二个槽已正确
   解析到实体，整句却被第一个槽的英文区域名连坐拒掉。
   修：判据从"任一槽非法 ⇒ 整句拒"改为**逐槽**——有槽能解析就不拦整句，不合格的
   槽**就地剪除**（未知区域绝不下发，本闸"防跨区误抓"的原意不许丢）。

钉层原则（同作业纪律）：每条断言都对**真机事实**负责，不许出现"放行=通过"的
假绿；凡是"信息不足 ⇒ 不点名/不收敛"的方向，一律按宁漏勿错侧写断言。
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_v1128_"))

from conftest import FakeHAClient                       # noqa: E402
from core import capability                             # noqa: E402
from core.executor import Executor                      # noqa: E402
from core.ha_client import HAClient                     # noqa: E402
from core.nlu.fast_path import Plan                     # noqa: E402
from core.pipeline import Pipeline                      # noqa: E402


# ── ① 区域继承（设备层 → 实体层）─────────────────────────────────
# 本机实况：entity_registry.area_id 全为 null，区域只挂在 device_registry
_ENT_ROWS = [
    {"entity_id": "sensor.own", "area_id": "office"},              # 实体自带
    {"entity_id": "sensor.inherit", "area_id": None, "device_id": "dev_kitchen"},
    {"entity_id": "sensor.nodev", "area_id": None},                # 无设备可继承
    {"entity_id": "sensor.gone", "area_id": None, "device_id": "dev_gone"},
    {"entity_id": "sensor.off", "device_id": "dev_kitchen", "disabled_by": "user"},
]
_DEV_ROWS = [
    {"id": "dev_kitchen", "area_id": "bed"},
    {"id": "dev_gone", "area_id": "deleted_area"},     # 区域已删：不得回填 uuid
]
_AREAS = {"office": "办公室", "bed": "卧室"}


def test_device_area_is_inherited_when_entity_has_no_area():
    """区域挂在设备上时，实体必须继承到（本机 `area_id` 全 null ⇒ 旧码恒空）。"""
    d2a = HAClient._device_area_map(_DEV_ROWS, _AREAS)
    assert d2a == {"dev_kitchen": "卧室"}, d2a          # 已删区域被丢弃（不回填 uuid）
    ent_map, _alias, _dc = HAClient._parse_registry(_ENT_ROWS, _AREAS, d2a)
    assert ent_map["sensor.own"] == "办公室"             # 实体自带：恒优先
    assert ent_map["sensor.inherit"] == "卧室"           # 继承生效
    assert "sensor.nodev" not in ent_map                 # 无设备可继承 ⇒ 不强填
    assert "sensor.gone" not in ent_map                  # 区域已删 ⇒ 不给脏数据
    assert "sensor.off" not in ent_map                   # 停用实体不进表


def test_device_registry_is_only_fetched_when_someone_needs_it():
    """替身/极老 HA 不发 device_id 时不得多拉一次设备注册表（省一次命令级往返）。"""
    assert HAClient._needs_device_area(_ENT_ROWS) is True
    assert HAClient._needs_device_area([{"entity_id": "a.b", "area_id": "x"}]) is False
    assert HAClient._needs_device_area([]) is False


def test_parse_registry_without_device_map_keeps_old_behaviour():
    """device_area 缺省（旧调用方/设备注册表拉取失败）⇒ 逐位等于旧行为，零降级。"""
    ent_map, _alias, _dc = HAClient._parse_registry(_ENT_ROWS, _AREAS)
    assert ent_map == {"sensor.own": "办公室"}, ent_map


# ── ② 离线孪生不得污染播报 ──────────────────────────────────────
_TWIN_STATES = {
    "light.ban_gong_shi_she_deng": {
        "entity_id": "light.ban_gong_shi_she_deng", "state": "on",
        "attributes": {"friendly_name": "射灯"}},
    "light.she_deng": {
        "entity_id": "light.she_deng", "state": "unavailable",
        "attributes": {"friendly_name": "射灯"}},
    "switch.other": {
        "entity_id": "switch.other", "state": "unavailable",
        "attributes": {"friendly_name": "别的开关"}},
}


def _ex(states):
    return Executor(FakeHAClient(states=states), None)


def test_offline_twin_is_not_named_when_its_namesake_acted():
    """「打开射灯」实况：同名孪生离线，真执行的那台好好的 ⇒ 播报不许说离线。"""
    args = {"target": [{"devices": [{"name": "射灯", "domains": ["light"]}]}]}
    got = asyncio.run(_ex(_TWIN_STATES)._offline_names("TurnDeviceOn", args))
    assert got == [], got


def test_all_namesakes_offline_is_still_named():
    """同名（含同名槽）**全台**确证 unavailable ⇒ 这台确实没动成，必须点名。"""
    states = dict(_TWIN_STATES)
    states["light.ban_gong_shi_she_deng"] = {
        "entity_id": "light.ban_gong_shi_she_deng", "state": "unavailable",
        "attributes": {"friendly_name": "射灯"}}
    args = {"target": [{"devices": [{"name": "射灯", "domains": ["light"]}]}]}
    got = asyncio.run(_ex(states)._offline_names("TurnDeviceOn", args))
    assert got == ["射灯"], got


def test_other_named_offline_device_still_named():
    """部分离线的原始动机不丢：点了 3 台、其中 1 台确证离线 ⇒ 那一台照旧点名。"""
    args = {"target": [{"devices": [{"name": "射灯", "domains": ["light"]},
                                    {"name": "别的开关", "domains": ["switch"]}]}]}
    got = asyncio.run(_ex(_TWIN_STATES)._offline_names("TurnDeviceOn", args))
    assert got == ["别的开关"], got


def test_unnamed_slot_still_names_confirmed_offline_entities():
    """未点名槽（整区/纯域）不适用孪生规则：其中确证离线的每一台照旧点名。

    v1.1.17 的原意是"部分离线必须点名"；v1.1.28 的"同名全票通过"只管**点名**的槽
    （那里才有"按名重解析"这一步、才有孪生误捞）。未点名槽的区域+域本身就是真下发
    集合，其中离线的每一台确实没动成 ⇒ 照点，不许跟着孪生口径一起丢。
    """
    ea = {"light.ban_gong_shi_she_deng": "客厅", "light.she_deng": "客厅"}
    ex = Executor(FakeHAClient(states=dict(_TWIN_STATES), entity_area=ea), None)
    args = {"target": [{"area": "客厅", "devices": [{"domains": ["light"]}]}]}
    assert asyncio.run(ex._offline_names("TurnDeviceOn", args)) == ["射灯"]


def test_area_prefix_is_not_duplicated_when_the_name_already_carries_it():
    """区域继承补好后 friendly_name 自带房间名 ⇒ 播报不许念成「办公室的办公室空调」。

    真机实况（#9/#10）：`control_targets=[{name:'办公室空调 Air Conditioner',
    area:'办公室'}]`。区域此前恒空 ⇒ 前缀不出现；补上后若照旧拼，TTS 会把房间名
    念两遍。只改播报，不改目标与成败。
    """
    ex = _ex({})
    plan = Plan(intent="TurnDeviceOn", args={}, source="t0", utterance="打开办公室空调")
    say = ex.speech(plan, {"success": True, "control_targets": [
        {"name": "办公室空调 Air Conditioner", "area": "办公室"}]})
    assert say == "好的，办公室空调 Air Conditioner打开了", say
    # 名字不含区域时前缀照旧保留（同房间不同设备靠前缀区分）
    say2 = ex.speech(plan, {"success": True, "control_targets": [
        {"name": "射灯", "area": "办公室"}]})
    assert say2 == "好的，办公室的射灯打开了", say2


def test_entity_id_form_names_only_dispatched_offline_entity():
    """entity_id 形＝真下发集合：只点其中确证 unavailable 的那几台。"""
    args = {"entity_id": ["light.ban_gong_shi_she_deng", "light.she_deng"]}
    got = asyncio.run(_ex(_TWIN_STATES)._offline_names("TurnDeviceOn", args))
    assert got == ["射灯"], got
    ok_args = {"entity_id": ["light.ban_gong_shi_she_deng"]}
    assert asyncio.run(_ex(_TWIN_STATES)._offline_names("TurnDeviceOn", ok_args)) == []


# ── ③ 畸形区域闸：逐槽判定（英文回捞不得被连坐）──────────────────
_REG = {"办公室", "卧室"}


def test_two_slot_target_is_not_blocked_when_one_slot_resolves():
    """`turn on the office light` 实况：office 不在册，但办公室那槽能解析到实体。"""
    states = {"light.ban_gong_shi_she_deng": {
        "entity_id": "light.ban_gong_shi_she_deng", "state": "off",
        "attributes": {"friendly_name": "射灯"}}}
    ea = {"light.ban_gong_shi_she_deng": "办公室"}
    tgt = [{"area": "office", "devices": [{"name": "light", "domains": []}]},
           {"area": "办公室", "devices": [{"name": "灯", "domains": ["light"]}]}]
    bad = capability.bad_target_area(tgt, _REG, states, ea)
    assert bad is None, bad
    slots = capability.bad_area_slots(tgt, _REG, states, ea)
    assert [s["area"] for s in slots] == ["office"], slots      # 只有不合格槽被剪除


def test_all_slots_unknown_area_still_blocks_the_whole_sentence():
    """原语义保住：**所有**带区域的槽都解析不到 ⇒ 整句拦下（畸形区域不外发）。"""
    states = {"light.a": {"entity_id": "light.a", "state": "off",
                          "attributes": {"friendly_name": "射灯"}}}
    tgt = [{"area": "办公室的射灯办公室", "devices": [{"name": "灯", "domains": ["light"]}]}]
    assert capability.bad_target_area(tgt, _REG, states, {}) == "办公室的射灯办公室"
    # 两个槽都不可解析 ⇒ 也不放行
    tgt2 = [{"area": "阁楼", "devices": [{"name": "灯"}]},
            {"area": "地下室", "devices": [{"name": "灯"}]}]
    assert capability.bad_target_area(tgt2, _REG, states, {}) in ("阁楼", "地下室")


def test_unknown_area_never_grabs_devices_from_other_areas():
    """未知区域不得靠设备名跨区误抓：区域不在册时该槽解析结果必须为空。"""
    states = {"light.a": {"entity_id": "light.a", "state": "off",
                          "attributes": {"friendly_name": "灯"}}}
    slot = {"area": "阁楼", "devices": [{"name": "灯", "domains": ["light"]}]}
    assert capability._slot_resolvable(slot, states, {"light.a": "卧室"}) is False
    assert capability.bad_target_area([slot], _REG, states, {"light.a": "卧室"}) == "阁楼"


def test_creation_side_stays_strict_when_one_slot_is_unknown():
    """创建侧不许跟着放宽：任一槽区域不在册 ⇒ 拦下（入库后是无人值守的自动执行）。

    即时执行侧可以"剪掉不合格槽、其余照常"；但入库剪槽＝把用户点的动作偷偷丢一个，
    所以 `bad_area_slots` 在 states 缺省（只看注册表）时的**任一命中即拦**口径
    保持不变。
    """
    tgt = [{"area": "办公室", "devices": [{"name": "灯"}]},
           {"area": "阁楼", "devices": [{"name": "灯"}]}]
    assert capability.bad_area_slots(tgt, _REG) and \
        capability.bad_area_slots(tgt, _REG)[0]["area"] == "阁楼"
    # 对照：即时执行侧（`bad_target_area` 带快照）对同一份 target 不拦整句
    states = {"light.a": {"entity_id": "light.a", "state": "off",
                          "attributes": {"friendly_name": "灯"}}}
    assert capability.bad_target_area(tgt, _REG, states,
                                      {"light.a": "办公室"}) is None


def test_registry_not_synced_keeps_fail_open():
    """注册表未同步 ⇒ 判不了，一律放行（与模块头同一纪律，不许误拦）。"""
    assert capability.bad_target_area([{"area": "阁楼"}], set()) is None


def test_plan_area_problem_prunes_bad_slot_and_lets_the_rest_run():
    """管线侧端到端：不合格槽就地剪除、合格槽照常执行（或不拦 ⇒ 返回 None）。"""
    states = {"light.ban_gong_shi_she_deng": {
        "entity_id": "light.ban_gong_shi_she_deng", "state": "off",
        "attributes": {"friendly_name": "射灯"}}}

    class _HA:
        def __init__(self):
            self._states = states
            self._areas = {"office": "办公室"}
            self._entity_area = {"light.ban_gong_shi_she_deng": "办公室"}

        async def states(self):
            return dict(self._states)

        async def refresh_states(self, force=False):
            return None

    p = Pipeline.__new__(Pipeline)
    p.ha = _HA()
    args = {"target": [{"area": "office", "devices": [{"name": "light", "domains": []}]},
                       {"area": "办公室", "devices": [{"name": "灯", "domains": ["light"]}]}]}
    plan = Plan(intent="TurnDeviceOn", args=args, source="t0",
                utterance="turn on the office light")
    assert asyncio.run(p._plan_area_problem(plan)) is None
    assert [t.get("area") for t in plan.args["target"]] == ["办公室"]
    # 全不可解析 ⇒ 仍按原话术拦下（不得因为改了判据就悄悄放宽）
    bad_plan = Plan(intent="TurnDeviceOn",
                    args={"target": [{"area": "阁楼", "devices": [{"name": "灯"}]}]},
                    source="t0", utterance="打开阁楼的灯")
    assert asyncio.run(p._plan_area_problem(bad_plan)) == "阁楼"


# ── 歧义闸：三级证据（全等名 > 同区域 > 主域）────────────────────
def _pipe(states, entity_area, extra=None):
    class _S:
        def __init__(self, d):
            self.d = d

        def get(self, k, default=None):
            return self.d.get(k, default)

    p = Pipeline.__new__(Pipeline)
    p.settings = _S(extra or {})
    p._confirm = {}
    p._origin_ts = {}
    p.ha = types.SimpleNamespace(_states=dict(states), _entity_area=dict(entity_area))
    return p


_WINDOW_STATES = {
    "button.pk_1": {"entity_id": "button.pk_1", "state": "2026-01-01T00:00:00",
                    "attributes": {"friendly_name": "平开窗 ① 开启"}},
    "number.pk_speed": {"entity_id": "number.pk_speed", "state": "unknown",
                        "attributes": {"friendly_name": "平开窗 速度"}},
    "cover.pk": {"entity_id": "cover.pk", "state": "open",
                 "attributes": {"friendly_name": "平开窗 开窗器"}},
}


def test_window_plan_converges_to_cover_without_asking():
    """#5「关闭平开窗」：主域证据把一台开窗器的全部零件兄弟收窄到 cover 那一台。

    收敛后必须同时写回主域——留着 t0 的域并集（button+cover+number）下发，
    等于把这台窗的开/停/关按钮与速度数值一起交给集成（#9 同族语义）。
    """
    p = _pipe(_WINDOW_STATES, {k: "办公室" for k in _WINDOW_STATES})
    args = {"target": [{"area": "办公室",
                        "devices": [{"name": "平开窗",
                                     "domains": ["button", "cover", "number"]}]}],
            "action": "close"}
    plan = Plan(intent="ControlWindow", args=args, source="t0", utterance="关闭平开窗")
    assert p._ambiguity_ask(plan, "o") is None, "已 uniquely 定位 ⇒ 不得打扰用户"
    assert args["target"][0]["devices"][0]["name"] == "平开窗 开窗器"
    assert args["target"][0]["devices"][0]["domains"] == ["cover"]


def test_no_evidence_yields_clarify_and_leaves_the_plan_untouched():
    """三条证据全落空 ⇒ 挂 clarify 列出候选，且不改写目标、不留执行桩。"""
    p = _pipe({"cover.a": {"entity_id": "cover.a", "state": "closed",
                           "attributes": {"friendly_name": "平开窗 开窗器"}},
               "cover.b": {"entity_id": "cover.b", "state": "closed",
                           "attributes": {"friendly_name": "测试平开窗 开窗器"}}},
              {})
    args = {"target": [{"devices": [{"name": "平开窗", "domains": ["cover"]}]}]}
    plan = Plan(intent="TurnDeviceOn", args=args, source="t0", utterance="打开平开窗")
    r = p._ambiguity_ask(plan, "o")
    assert r is not None and r.source == "clarify" and r.ok is False
    assert "平开窗 开窗器" in r.text and "测试平开窗 开窗器" in r.text
    assert args["target"][0]["devices"][0]["name"] == "平开窗"
    assert p._confirm == {}


# #17 反向钉（真机：会议室/办公室空调指示灯与两台「射灯」；t0 把「指示灯」归一成
# name=灯）——两台候选**同名**，旧前置闸按"异名"计数（len(distinct)<2）直接放行，
# 等于按 name=灯 交给集成挑一台。修后：非全等名 ⇒ 进证据环，收不住就列候选。
_TWIN_LIGHTS = {
    "light.ban_gong_shi_she_deng": {
        "entity_id": "light.ban_gong_shi_she_deng", "state": "off",
        "attributes": {"friendly_name": "射灯"}},
    "light.she_deng": {
        "entity_id": "light.she_deng", "state": "unavailable",
        "attributes": {"friendly_name": "射灯"}},
}


def test_same_name_twins_with_non_exact_word_ask_instead_of_dispatching():
    """用户词与候选名不全等 ⇒ 列候选、零下发、目标不改写（#17 收口）。"""
    p = _pipe(_TWIN_LIGHTS, {k: "办公室" for k in _TWIN_LIGHTS})
    args = {"target": [{"devices": [{"name": "灯", "domains": ["light"]}]}]}
    plan = Plan(intent="TurnDeviceOn", args=args, source="t0", utterance="打开指示灯")
    r = p._ambiguity_ask(plan, "o")
    assert r is not None and r.source == "clarify" and r.ok is False, r
    assert "射灯" in (r.text or "") and "2 台" in (r.text or ""), r.text
    assert p._confirm == {}, "未收敛却挂了执行桩"
    assert args["target"][0]["devices"][0]["name"] == "灯", "目标名被偷偷改写"


def test_same_name_twins_with_exact_word_still_dispatch():
    """对照（防误伤 #12/#13「打开射灯」）：说的词就是这两台的全等名 ⇒ 照旧按名下发。"""
    p = _pipe(_TWIN_LIGHTS, {k: "办公室" for k in _TWIN_LIGHTS})
    args = {"target": [{"devices": [{"name": "射灯", "domains": ["light"]}]}]}
    plan = Plan(intent="TurnDeviceOn", args=args, source="t0", utterance="打开射灯")
    assert p._ambiguity_ask(plan, "o") is None
    assert p._confirm == {} and args["target"][0]["devices"][0]["name"] == "射灯"
