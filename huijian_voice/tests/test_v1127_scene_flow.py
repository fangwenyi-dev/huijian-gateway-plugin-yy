# -*- coding: utf-8 -*-
"""v1.1.27 场景/自动化/解析/条目生命周期 7 项缺陷回归钉（2026-10 深审批批7）。

钉桩纪律（沿用 test_v1043/test_v1064/test_v1097 先例）：
  · 能真 import 的模块全部真 import 真执行（HA 替身经 bench._install_ha_stubs
    幂等回装）——intent_voice_scene / intent_automation / custom_llm_api 跑的是
    将随集成出厂的那份源码，替身只补 homeassistant 与存储后端；
  · manager.py / config_flow.py 依赖 esphome 库与 HA data_entry_flow，整包不可
    导入 → ast 摘单函数执行（不复制逻辑），替身 self 记账。

七项待修（本文件先红后绿）：
 1 custom_llm_api._call_intent 不看 response.success ⇒ 执行失败播「办好了」；
 2 免确认解锁闸只扫顶层 target ⇒ 场景 actions 里的 TurnDeviceOff×lock 可绕；
 3 场景 ID 秒级时间戳同秒互覆 + 脏 actions 炸 500 + 空 actions 假成功；
 4 自动化 update 不解析 entity_id / 不校 days + async_stop 全仓零调用；
 5 detect_classes 先于区域切分（「门厅的温度」吞「门」成 door）+ 「的」只去名侧；
 6 不可达 issue 实例态永不删 + id 漂 + create_task(async_schedule_reload) 吞错；
 7 qrcode_done 把 None 归一成 {} ⇒ 超时引导表单/rewait 通道不可达。
"""
import ast
import asyncio
import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/（conftest 惯例）

import test_window_speed_behavior as bench  # noqa: E402  触发替身装载

CC = HERE / "custom_components" / "huijian_ai"


# ── HA 替身扩展（幂等；bench 已建 homeassistant 家底） ─────────────
class _AV:
    def __init__(self, s="1.2.3"):
        p = str(s).split(".")
        self.major = p[0]
        self.minor = p[1] if len(p) > 1 else "0"

    def __str__(self):
        return f"{self.major}.{self.minor}"


def _install_stubs():
    ha = bench._install_ha_stubs()
    av = bench._stub("awesomeversion")
    if not hasattr(av, "AwesomeVersion"):
        av.AwesomeVersion = _AV
    ha.const.EVENT_STATE_CHANGED = "state_changed"
    core = sys.modules["homeassistant.core"]
    if not hasattr(core, "callback"):
        core.callback = lambda f: f
    # voluptuous 替身补异常类型（custom_llm_api 的 except 元组要求）
    vol = sys.modules["voluptuous"]
    if not hasattr(vol, "Invalid"):
        vol.Invalid = type("Invalid", (Exception,), {})
    exc = bench._stub("homeassistant.exceptions")
    if not hasattr(exc, "HomeAssistantError"):
        exc.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
    # 存储后端替身：真 VoiceSceneStore/AutomationStore 跑在内存上
    st = bench._stub("homeassistant.helpers.storage")
    if not hasattr(st, "Store"):
        mem: dict = {}

        class Store:
            def __init__(self, hass, version, key):
                self._key = key

            async def async_load(self):
                return mem.get(self._key)

            async def async_save(self, data):
                mem[self._key] = data

        st.Store = Store
        st._MEM = mem
    ev = bench._stub("homeassistant.helpers.event")
    if not hasattr(ev, "async_track_time_change"):
        ev.async_track_time_change = lambda hass, cb, **kw: (lambda: None)
    # llm 面（custom_llm_api 顶层 import homeassistant.helpers.llm）
    llm = bench._stub("homeassistant.helpers.llm")
    if not hasattr(llm, "API"):

        class API:
            def __init__(self, hass=None, id=None, name=None):
                self.hass, self.id, self.name = hass, id, name

        class Tool:
            name = ""
            description = ""
            parameters = None

        class APIInstance:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class ToolInput:
            pass

        class LLMContext:
            pass

        llm.API, llm.Tool, llm.APIInstance = API, Tool, APIInstance
        llm.ToolInput, llm.LLMContext = ToolInput, LLMContext
        llm.LLM_API_ASSIST = "assist"
        llm.async_get_api = lambda *a, **k: None
    return ha


_install_stubs()

_pkg = types.ModuleType("hjv1127_pkg")
_pkg.__path__ = [str(CC)]
sys.modules.setdefault("hjv1127_pkg", _pkg)


def _load(name):
    full = f"hjv1127_pkg.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, CC / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


vs = _load("intent_voice_scene")
ia = _load("intent_automation")
claw = _load("custom_llm_api")
erc = _load("entity_resolve_cn")

_IT = sys.modules["homeassistant.helpers.intent"]
_ST = sys.modules["homeassistant.helpers.storage"]
_State = sys.modules["homeassistant.core"].State

# 替身模块身份快照：别的钉文件可能把 sys.modules 条目换成新对象（本仓先例
# test_window_speed_behavior「不赌顺序」注释）。一旦被换掉，本文件打在旧对象上
# 的记账/打桩对被测代码不可见（真 import 的模块在调用点重查 sys.modules）。
# 每条用例前把本文件认下的替身钉回 sys.modules，零顺序依赖。
_STUB_SNAPSHOT = {
    name: mod for name, mod in list(sys.modules.items())
    if name.split(".")[0] in ("homeassistant", "voluptuous", "awesomeversion")
}


@pytest.fixture(autouse=True)
def _stubs_in_place():
    for name, mod in _STUB_SNAPSHOT.items():
        sys.modules[name] = mod
    _install_stubs()
    yield


def _plain_hass(states=(), **kw):
    h = types.SimpleNamespace(states=bench._States(list(states)), **kw)
    return h


def _fresh_scene_store(hass=None):
    """真 VoiceSceneStore + 干净内存后端 + 干净全局单例。"""
    _ST._MEM.clear()
    vs.reset_voice_scene_globals()
    return vs.VoiceSceneStore(hass if hass is not None else _plain_hass())


# ══ 项1：_call_intent 如实折算 response 成败 ═══════════════════════
def _call_intent(intent_type, args, response, hass=None):
    """真 HuijianControlAPI._call_intent 跑一次；返回 (结果, intent.async_handle 记账)。"""
    calls = []

    async def _handle(**kw):
        calls.append(kw)
        return response

    _IT.async_handle = _handle
    h = hass if hass is not None else _plain_hass()
    api = claw.HuijianControlAPI(h)
    ctx = types.SimpleNamespace(device_id=None, assistant=None)
    out = asyncio.run(api._call_intent(h, intent_type, args, ctx))
    return out, calls


LIGHT_ARGS = {"target": [{"devices": [{"name": "筒灯", "domains": ["light"]}]}]}


def test_01a_intent_response_object_failure_reported():
    """HA core async_handle 返回 IntentResponse 对象（success False）——旧实现
    一律 success True + str(response)，失败被播成「办好了」。"""
    resp = types.SimpleNamespace(success=False, error="设备离线，未执行")
    out, _ = _call_intent("TurnDeviceOn", LIGHT_ARGS, resp)
    assert out["success"] is False, out
    assert "设备离线" in str(out.get("error") or "") + str(out.get("result") or ""), out


def test_01b_folded_dict_failure_reported():
    out, _ = _call_intent("TurnDeviceOn", LIGHT_ARGS,
                          {"success": False, "error": "no match"})
    assert out["success"] is False, out
    assert "no match" in str(out.get("error") or ""), out


def test_01c_success_keeps_readable_text():
    out, _ = _call_intent("TurnDeviceOn", LIGHT_ARGS,
                          {"success": True, "message": "已打开筒灯"})
    assert out["success"] is True and "已打开筒灯" in out.get("result", ""), out


# ══ 项2：免确认解锁闸递归扫 actions（创建 + 触发两链） ═════════════
LOCK_ACTION = {"intent": "TurnDeviceOff",
               "params": {"target": [{"devices": [{"name": "大门",
                                                   "domains": ["lock"]}]}]}}
BENIGN_ACTION = {"intent": "TurnDeviceOn",
                 "parameters": {"target": [{"devices": [{"name": "筒灯",
                                                         "domains": ["light"]}]}]}}


class _FakeSceneStore:
    def __init__(self, scenes):
        self._scenes = scenes

    async def get_scene_by_trigger(self, phrase):
        return self._scenes.get(phrase)


@pytest.fixture()
def _clean_scene_globals():
    vs.reset_voice_scene_globals()
    yield
    vs.reset_voice_scene_globals()


def test_02a_create_scene_with_lock_action_gated(_clean_scene_globals):
    """创建链：actions[].params.target 里的 TurnDeviceOff×lock = 免确认解锁。"""
    out, calls = _call_intent("HassCreateVoiceScene",
                              {"trigger_phrase": "晚安", "actions": [LOCK_ACTION]},
                              {"success": True})
    assert out["success"] is False, out
    assert "确认" in str(out.get("error") or ""), out
    assert calls == [], "风险动作已下发执行面（闸未拦）"


def test_02b_create_scene_nested_lock_action_gated(_clean_scene_globals):
    """递归：风险动作藏在嵌套值里同样命中（非顶层 target 即旁路是旧洞）。"""
    nested = {"intent": "TurnDeviceOn", "params": {"target": LIGHT_ARGS["target"]},
              "on_fail": [LOCK_ACTION]}
    out, calls = _call_intent("HassCreateVoiceScene",
                              {"trigger_phrase": "晚安", "actions": [nested]},
                              {"success": True})
    assert out["success"] is False and calls == [], out


def test_02c_create_scene_benign_passes(_clean_scene_globals):
    """不扩伤：纯灯光场景照常穿透。"""
    out, calls = _call_intent("HassCreateVoiceScene",
                              {"trigger_phrase": "观影",
                               "actions": [BENIGN_ACTION]},
                              {"success": True})
    assert out["success"] is True and len(calls) == 1, out


def test_02d_trigger_scene_with_lock_action_gated(_clean_scene_globals):
    """触发链：回放存量场景里的解锁动作同样免确认（args 只有 trigger_phrase，
    必须回查场景库的动作面）。"""
    vs._store_instance = _FakeSceneStore({
        "晚安": {"scene_id": "s1", "trigger_phrase": "晚安",
                 "actions": [BENIGN_ACTION, LOCK_ACTION]}})
    out, calls = _call_intent("HassTriggerVoiceScene", {"trigger_phrase": "晚安"},
                              {"success": True})
    assert out["success"] is False and calls == [], out


def test_02e_trigger_benign_scene_passes(_clean_scene_globals):
    vs._store_instance = _FakeSceneStore({
        "观影": {"scene_id": "s2", "trigger_phrase": "观影",
                 "actions": [BENIGN_ACTION]}})
    out, calls = _call_intent("HassTriggerVoiceScene", {"trigger_phrase": "观影"},
                              {"success": True})
    assert out["success"] is True and len(calls) == 1, out


# ══ 项3：场景 ID 唯一化 / 脏动作韧性 / 空动作拒回放 ═════════════════
_FROZEN = datetime(2026, 9, 10, 14, 36, 42, tzinfo=timezone.utc)


class _FrozenDatetime:
    """秒级冻结时钟：同秒两次 create 必须仍在（旧实现互相覆盖）。"""

    @staticmethod
    def now(tz=None):
        return _FROZEN if tz is not None else _FROZEN.replace(tzinfo=None)


def test_03a_same_second_scene_ids_unique(monkeypatch):
    monkeypatch.setattr(vs, "datetime", _FrozenDatetime)
    store = _fresh_scene_store()
    ok1, id1 = asyncio.run(store.create_scene("晚安", [BENIGN_ACTION]))
    ok2, id2 = asyncio.run(store.create_scene("观影", [BENIGN_ACTION]))
    assert ok1 and ok2, (ok1, ok2)
    assert id1 != id2, f"同秒场景 ID 冲突：{id1}"
    data = asyncio.run(store._load_data())
    assert set(data["scenes"]) == {id1, id2}, data["scenes"].keys()
    assert data["trigger_index"]["晚安"] == id1 and data["trigger_index"]["观影"] == id2, \
        "trigger_index 反指（两条触发词指向同一 scene）"


def _trigger(scene_actions):
    vs._store_instance = _FakeSceneStore({
        "晚安": {"scene_id": "s1", "trigger_phrase": "晚安",
                 "actions": scene_actions}})
    calls = []

    async def _handle(**kw):
        calls.append(kw)
        return {"success": True, "message": "ok"}

    _IT.async_handle = _handle
    handler = vs.HassTriggerVoiceSceneIntent()
    ito = types.SimpleNamespace(hass=_plain_hass(),
                                slots={"trigger_phrase": {"value": "晚安"}},
                                assistant=None, device_id=None)
    out = asyncio.run(handler.async_handle(ito))
    return out, calls


def test_03b_dirty_non_dict_actions_no_crash(_clean_scene_globals):
    """存量脏形态（PUT 早期版本/手工改 .storage）：非 dict 动作必须跳过而非
    AttributeError 炸 500，合法动作照常执行。"""
    out, calls = _trigger(["垃圾字符串", 3, None, BENIGN_ACTION])
    assert out["success"] is True, out
    assert len(out["executed_actions"]) == 1 and len(calls) == 1, out


def test_03c_empty_actions_not_fake_success(_clean_scene_globals):
    """PUT 可写 actions: []；回放空动作 all([])==True 会假报「已执行场景」。"""
    out, calls = _trigger([])
    assert out["success"] is False, out
    assert "已执行场景" not in str(out.get("message") or ""), out
    assert calls == [], "空场景不该下发任何执行"


def test_03d_all_dirty_actions_not_fake_success(_clean_scene_globals):
    out, calls = _trigger(["垃圾", 3])
    assert out["success"] is False and calls == [], out


# ══ 项4：自动化 update 解析/校验 + 监听注销 ════════════════════════
class _Entry:
    def __init__(self, entity_id, area_id=None, device_id=None):
        self.entity_id, self.area_id, self.device_id = entity_id, area_id, device_id


class _Area:
    def __init__(self, area_id, name, aliases=()):
        self.area_id, self.name, self.aliases = area_id, name, aliases


_HASS_POOL = []


class _AutoHass:
    """自动化链最小 hass：states + 区域/实体注册表。"""

    def __init__(self, states, areas=(), entries=()):
        self.states = bench._States(list(states))
        self._er = bench._ER(list(entries))
        self._dr = bench._DR({})
        self._ar = types.SimpleNamespace(
            async_list_areas=lambda: list(areas),
            async_get_area=lambda aid: next(
                (a for a in areas if a.area_id == aid), None))
        self.pending = []
        _HASS_POOL.append(self)

        def _create_task(coro, *a, **k):
            if asyncio.iscoroutine(coro):
                self.pending.append(coro)
            return None

        self.async_create_task = _create_task
        self.config_entries = types.SimpleNamespace(
            async_entries=lambda dom: [])


@pytest.fixture(autouse=True)
def _close_unawaited():
    yield
    for h in _HASS_POOL:
        for coro in h.pending:
            coro.close()
        h.pending.clear()
    _HASS_POOL.clear()


def _state(eid, name, dc=""):
    return _State(eid, name=name, attributes={"friendly_name": name,
                                              "device_class": dc})


def _auto_hass_office():
    return _AutoHass(
        [_state("sensor.office_temp", "温度", "temperature")],
        areas=[_Area("a_office", "办公室")],
        entries=[_Entry("sensor.office_temp", "a_office")])


ACT = [dict(BENIGN_ACTION)]


def _update_handler():
    return ia.HassUpdateAutomationIntent()


def _update_ito(hass, aid, trigger):
    return types.SimpleNamespace(
        hass=hass, assistant=None, device_id=None,
        slots={"automation_id": {"value": aid}, "trigger": {"value": trigger}})


def test_04a_update_resolves_desc_entity_id():
    """update 路径不解析 ⇒ 描述原文（「办公室的温度」）当 entity_id 入库、
    永不触发却回「已更新」。create 走 _resolve_entity_id，update 必须同规。"""
    ia.reset_automation_globals()
    _ST._MEM.clear()
    hass = _auto_hass_office()
    store = ia.get_automation_store(hass)
    ok, aid = asyncio.run(store.create_automation(
        {"entity_id": "sensor.office_temp", "above": 30}, ACT))
    assert ok
    out = asyncio.run(_update_handler().async_handle(
        _update_ito(hass, aid, {"entity_id": "办公室的温度", "above": 31})))
    assert out["success"] is True, out
    saved = asyncio.run(store.get_automation(aid))
    assert saved["trigger"]["entity_id"] == "sensor.office_temp", saved["trigger"]


def test_04b_update_rejects_unresolvable_entity():
    """解析不出必须如实拒，而不是把描述原文写进库（永不触发的假成功）。"""
    ia.reset_automation_globals()
    _ST._MEM.clear()
    hass = _auto_hass_office()
    store = ia.get_automation_store(hass)
    ok, aid = asyncio.run(store.create_automation(
        {"entity_id": "sensor.office_temp", "above": 30}, ACT))
    out = asyncio.run(_update_handler().async_handle(
        _update_ito(hass, aid, {"entity_id": "车库里的加湿器", "above": 31})))
    assert out["success"] is False, out
    saved = asyncio.run(store.get_automation(aid))
    assert saved["trigger"]["entity_id"] == "sensor.office_temp", saved["trigger"]


def test_04c_update_validates_days_range():
    """create 校 days 1-7，update 不校 ⇒ days:[8] 永不触发还回「已更新」。"""
    ia.reset_automation_globals()
    _ST._MEM.clear()
    hass = _auto_hass_office()
    store = ia.get_automation_store(hass)
    ok, aid = asyncio.run(store.create_automation(
        {"at": "07:00", "days": [1, 2]}, ACT))
    out = asyncio.run(_update_handler().async_handle(
        _update_ito(hass, aid, {"at": "07:30", "days": [8]})))
    assert out["success"] is False, out
    assert "1-7" in str(out.get("error") or ""), out
    saved = asyncio.run(store.get_automation(aid))
    assert saved["trigger"] == {"at": "07:00", "days": [1, 2]}, saved["trigger"]


def test_04d_update_ok_days_still_works():
    ia.reset_automation_globals()
    _ST._MEM.clear()
    hass = _auto_hass_office()
    store = ia.get_automation_store(hass)
    ok, aid = asyncio.run(store.create_automation({"at": "07:00", "days": [1]}, ACT))
    out = asyncio.run(_update_handler().async_handle(
        _update_ito(hass, aid, {"at": "07:30", "days": [7, 1]})))
    assert out["success"] is True, out
    saved = asyncio.run(store.get_automation(aid))
    assert saved["trigger"] == {"at": "07:30", "days": [1, 7]}, saved["trigger"]


class _BusRecorder:
    def __init__(self):
        self.unsubs = []

    def async_listen(self, evt, cb):
        rec = {"evt": evt, "called": 0}

        def _unsub():
            rec["called"] += 1

        self.unsubs.append(rec)
        return _unsub


def test_04e_reset_unregisters_listeners_idempotent():
    """unload 只 reset_automation_globals()（丢引用不注销）⇒ reload 后旧监听
    与新监听双执行。reset 必须真注销，且幂等（重复 reset/stop 不炸不多调）。"""
    ia.reset_automation_globals()
    bus = _BusRecorder()
    hass = _AutoHass([], )
    hass.bus = bus
    mgr = ia.get_automation_manager(hass)
    for coro in list(hass.pending):
        hass.pending.remove(coro)
        coro.close()
    asyncio.run(mgr.async_start())
    assert len(bus.unsubs) == 1 and bus.unsubs[0]["called"] == 0
    ia.reset_automation_globals()
    assert bus.unsubs[0]["called"] == 1, "reset 未注销监听（旧监听永活=双执行）"
    ia.reset_automation_globals()
    assert bus.unsubs[0]["called"] == 1, "reset 非幂等"
    asyncio.run(ia.AutomationManager(hass).async_stop())
    asyncio.run(mgr.async_stop())          # 已停再停：幂等
    assert bus.unsubs[0]["called"] == 1


def test_04f_new_manager_rearms_listeners():
    """reset 真注销后，懒建的新管理器必须重新武装——否则条目 reload 后
    自动化静默失效（旧实现靠泄漏的旧监听"碰巧还活着"）。"""
    ia.reset_automation_globals()
    bus = _BusRecorder()
    hass = _AutoHass([])
    hass.bus = bus
    mgr = ia.get_automation_manager(hass)
    assert mgr is not None
    pend = list(hass.pending)
    hass.pending.clear()
    for coro in pend:
        asyncio.run(coro)
    assert len(bus.unsubs) == 1, "新管理器未重新武装监听"
    ia.reset_automation_globals()


# ══ 项5：区域优先切分 + 「的」两侧统一 ════════════════════════════
def _c(eid, name, dc="", area=""):
    return {"entity_id": eid, "name": name, "dc": dc, "area": area}


def test_05a_area_word_not_swallowed_as_class():
    """「门厅的温度」：旧顺序先 detect_classes，「门」被单字兜底吞成 door →
    候选池混入门磁，区域里唯一「厅」名字命中者反成目标（错绑且报成功）。"""
    cands = [_c("binary_sensor.hall_door", "门厅门磁", "door", "门厅"),
             _c("sensor.hall_temp", "温度", "temperature", "门厅")]
    r = erc.pick("门厅的温度", cands, {"门厅": "门厅"})
    assert r["classes"] == ["temperature"], r
    assert r["best"] == "sensor.hall_temp", r


def test_05b_de_word_matched_both_sides():
    """「的」只在实体名侧剥离、desc 侧折成空格 ⇒ 两侧都不中（死解析）。"""
    cands = [_c("sensor.l1", "客厅空调", "", "")]
    r = erc.pick("客厅的空调", cands, {})
    assert r["best"] == "sensor.l1", r


def test_05c_de_word_in_residue_disambiguates():
    """区域未登记时「的」残串仍须两侧可比：旧实现残串「客厅 空调温度」（的
    折成空格）与实体名「客厅空调温度」两侧都不中 → 唯一可判的候选被判成
    「找到多个」（用户被迫重新指定，实际只有一个是名字命中）。"""
    cands = [_c("sensor.o1", "客厅空调温度", "temperature", "客厅"),
             _c("sensor.o2", "客厅桌上温度计", "temperature", "客厅")]
    r = erc.pick("客厅的空调温度", cands, {})
    assert r["best"] == "sensor.o1", r


# ══ 项6：repair issue 生命周期 + 看门狗重载 ═══════════════════════
MGR = CC / "manager.py"


def _extract(path, name, extra_ns=None):
    class _Ann:
        def __class_getitem__(cls, item):
            return None

    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            ns = {}
            eager = [a.annotation for a in list(node.args.posonlyargs) + list(node.args.args)
                     + list(node.args.kwonlyargs)]
            if node.returns is not None:
                eager.append(node.returns)
            eager += list(node.args.defaults) + list(node.args.kw_defaults)
            for expr in eager:
                if expr is None:
                    continue
                for sub in ast.walk(expr):
                    if (isinstance(sub, ast.Name) and sub.id not in ns
                            and not hasattr(__import__("builtins"), sub.id)):
                        ns[sub.id] = _Ann
            if extra_ns:
                ns.update(extra_ns)
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         str(path), "exec"), ns)
            fn = ns[name]
            return fn.fget if isinstance(fn, property) else fn
    raise AssertionError(f"{path} 未找到 {name}")


class _FakeEntry:
    def __init__(self, entry_id, unique_id=None, title="HUIJIAN-0BD0"):
        self.entry_id, self.unique_id, self.title = entry_id, unique_id, title
        self.data = {"port": 6053}


def _issue_ns(recorder):
    return {
        "time": types.SimpleNamespace(monotonic=lambda: 0.0),
        "_LOGGER": sys.modules["logging"].getLogger("pin_v1127"),
        "async_delete_issue": lambda *a, **k: recorder.append(a),
        "async_create_issue": lambda *a, **k: recorder.append(a),
        "DOMAIN": "huijian_ai",
        "SATELLITE_UNREACHABLE_ISSUE_FORMAT": "satellite_unreachable-{}",
        "UNREACHABLE_ISSUE_THRESHOLD_S": 300.0,
        "UNREACHABLE_WARN_INTERVAL_S": 300.0,
        "IssueSeverity": types.SimpleNamespace(WARNING="warning"),
        "CONF_PORT": "port",
        "DEFAULT_PORT": 6053,
        "InvalidAuthAPIError": type("InvalidAuthAPIError", (Exception,), {}),
        "APIConnectionError": type("APIConnectionError", (Exception,), {}),
    }


def _manager_stub(rec, entry, connected):
    """真 on_connect + 真 id 属性（含 legacy 形态）装在替身 self 上。"""
    ns = _issue_ns(rec)

    class _Stub:
        pass

    _Stub._unreachable_issue_id = property(_extract(MGR, "_unreachable_issue_id", ns))
    _Stub._unreachable_legacy_issue_id = property(
        _extract(MGR, "_unreachable_legacy_issue_id", ns))
    me = _Stub()
    me.hass = object()
    me.entry = entry
    me._conn_fail_since = None
    me._conn_fail_count = 0
    me._unreachable_issue_open = False
    me.cli = types.SimpleNamespace()
    me._link_up = False

    async def _on_connect():
        connected.append(True)

    async def _start_reauth_and_disconnect():
        connected.append("reauth")

    me._on_connect = _on_connect
    me._arm_va_link_watchdog = lambda: None
    me._start_reauth_and_disconnect = _start_reauth_and_disconnect
    return me, _extract(MGR, "on_connect", ns)


def test_06a_issue_id_stable_after_unique_id_set():
    """id 取 unique_id or entry_id：unique_id 迟到/变更即漂 → 旧 issue 成孤儿。"""
    rec = []
    ns = _issue_ns(rec)

    class _Stub:
        pass

    _Stub._unreachable_issue_id = property(_extract(MGR, "_unreachable_issue_id", ns))
    _Stub._unreachable_legacy_issue_id = property(
        _extract(MGR, "_unreachable_legacy_issue_id", ns))
    self = _Stub()
    self.entry = _FakeEntry("entry1", unique_id="MAC-AABB")
    assert self._unreachable_issue_id == "satellite_unreachable-entry1", \
        self._unreachable_issue_id
    assert self._unreachable_legacy_issue_id == "satellite_unreachable-MAC-AABB", \
        self._unreachable_legacy_issue_id


def test_06b_on_connect_deletes_issue_unconditionally():
    """issue 是持久化实据，_unreachable_issue_open 只是实例态：reload/重启后
    新实例标志为假 → 「设备不可达」repair 长挂说谎（连接明明已恢复）。"""
    rec, connected = [], []
    me, fn = _manager_stub(rec, _FakeEntry("entry1", unique_id="MAC-AABB"), connected)
    asyncio.run(fn(me))
    ids = [t[2] for t in rec]
    assert "satellite_unreachable-entry1" in ids, rec
    assert connected == [True]


def test_06c_on_connect_deletes_legacy_issue_too():
    """旧 id 形态（unique_id 版）残留的 repair 也必须一并清掉。"""
    rec, connected = [], []
    me, fn = _manager_stub(rec, _FakeEntry("entry1", unique_id="MAC-AABB"), connected)
    asyncio.run(fn(me))
    ids = [t[2] for t in rec]
    assert "satellite_unreachable-MAC-AABB" in ids, ids


class _TimeoutCli:
    def __init__(self, fail_disconnect=True):
        self.fail_disconnect = fail_disconnect
        self.info_calls = 0
        self.disconnects = 0

    async def device_info(self):
        self.info_calls += 1
        raise asyncio.TimeoutError

    async def disconnect(self):
        self.disconnects += 1
        if self.fail_disconnect:
            raise asyncio.TimeoutError


class _StopLoop(Exception):
    pass


def test_06d_watchdog_reload_not_swallowed():
    """async_schedule_reload 是 @callback 返 None → create_task(None) TypeError
    被 except 吞成 DEBUG（重载排期形同空转、现场无痕）。"""
    rec = {"scheduled": [], "tasks": [], "bad": [], "warns": []}
    sleeps = {"n": 0}

    async def _sleep(sec, *a, **k):
        sleeps["n"] += 1
        if sleeps["n"] >= 4:      # 首轮 45s/二次探针 1s/循环 45s 后退出
            raise _StopLoop

    fake_asyncio = types.SimpleNamespace(
        sleep=_sleep, wait_for=asyncio.wait_for, CancelledError=asyncio.CancelledError,
        TimeoutError=asyncio.TimeoutError)
    ns = {
        "asyncio": fake_asyncio,
        "_LOGGER": types.SimpleNamespace(
            warning=lambda *a, **k: rec["warns"].append(a),
            debug=lambda *a, **k: None, info=lambda *a, **k: None,
            error=lambda *a, **k: None),
    }
    fn = _extract(MGR, "_va_link_watchdog", ns)
    hass = types.SimpleNamespace(
        config_entries=types.SimpleNamespace(
            async_schedule_reload=lambda eid: rec["scheduled"].append(eid)))

    def _create_task(coro, *a, **k):
        if not asyncio.iscoroutine(coro):
            rec["bad"].append(coro)          # 真 HA：loop.create_task(None) TypeError
            raise TypeError("a coroutine was expected, got None")
        rec["tasks"].append(coro)
        coro.close()

    hass.async_create_task = _create_task
    self = types.SimpleNamespace(
        entry=_FakeEntry("entry1"), hass=hass, _link_up=True,
        cli=_TimeoutCli(fail_disconnect=True))
    with pytest.raises(_StopLoop):
        asyncio.run(fn(self))
    assert rec["scheduled"] == ["entry1"], rec
    assert rec["bad"] == [], \
        f"create_task 收到非协程=async_schedule_reload 被套壳（TypeError 被吞）: {rec}"


# ══ 项7：config_flow 进度完成 vs 表单提交两态 ═════════════════════
CFG = CC / "config_flow.py"


class _FlowSelf:
    def __init__(self, setup_data, timed_out=False):
        self.setup_data = setup_data
        self._setup_wait_timed_out = timed_out
        self._wait_task = object()
        self.hass = object()
        self._extra = {"tip": "x"}
        self.shown = []
        self.aborted = []
        self.qrcode_calls = 0

    def async_show_form(self, **kw):
        self.shown.append(kw)
        return {"type": "form", **kw}

    def async_abort(self, **kw):
        self.aborted.append(kw)
        return {"type": "abort", **kw}

    async def async_step_qrcode(self, user_input=None):
        self.qrcode_calls += 1
        return {"type": "progress"}


def _qrcode_done_fn():
    class _Schema:
        def __init__(self, *a, **k):
            self.schema = a[0] if a else {}

    class _Bool:
        pass

    ns = {
        "vol": types.SimpleNamespace(
            Schema=_Schema,
            Required=lambda *a, **k: f"req:{a[0] if a else ''}",
            Optional=lambda *a, **k: f"opt:{a[0] if a else ''}",
            Boolean=bool, boolean=bool, string=str, All=lambda *a, **k: a[0],
            Coerce=lambda f: f),
        "selector": types.SimpleNamespace(BooleanSelector=_Bool),
        "_LOGGER": sys.modules["logging"].getLogger("pin_v1127_cfg"),
        # v1.1.27-r2：本步新增日志脱敏调用（行为由 test_v1127_r2_verify 的
        # _redact_setup_for_log 钉覆盖）；本组只验表单分支，用透传替身即可。
        "_redact_setup_for_log": lambda d: d,
    }

    async def _get_haid(hass):
        return "haid"

    ns["get_haid"] = _get_haid
    return _extract(CFG, "async_step_qrcode_done", ns)


def test_07a_timeout_guide_form_reachable():
    """进度完成（user_input is None）且等待超时：必须渲染「再等一轮」表单；
    旧实现把 None 归一成 {} 后 `is not None` 恒真 → 直接 abort，表单不可达。"""
    fn = _qrcode_done_fn()
    me = _FlowSelf(setup_data=None, timed_out=True)
    out = asyncio.run(fn(me, None))
    assert me.aborted == [], f"超时引导表单不可达（直接 abort）: {me.aborted}"
    assert len(me.shown) == 1 and me.shown[0]["step_id"] == "qrcode_done", me.shown
    assert me.shown[0]["data_schema"].schema, "超时表单必须带 rewait 勾选"
    assert out.get("type") == "form"


def test_07b_timeout_without_flag_plain_guide():
    """未置超时标志（理论上不会到达）也必须是表单而非 abort。"""
    fn = _qrcode_done_fn()
    me = _FlowSelf(setup_data=None, timed_out=False)
    asyncio.run(fn(me, None))
    assert me.aborted == [] and len(me.shown) == 1, (me.aborted, me.shown)


def test_07c_rewait_resubmits_and_rearms():
    """rewait 路径必须可用：清标志、重挂等待任务、复用同一 uuid 通道。"""
    fn = _qrcode_done_fn()
    me = _FlowSelf(setup_data=None, timed_out=True)
    out = asyncio.run(fn(me, {"rewait": True}))
    assert me.qrcode_calls == 1, "rewait 未回 qrcode 续等"
    assert me._setup_wait_timed_out is False and me._wait_task is None
    assert out.get("type") == "progress"


def test_07d_unchecked_rewait_exits():
    fn = _qrcode_done_fn()
    me = _FlowSelf(setup_data=None, timed_out=True)
    asyncio.run(fn(me, {"rewait": False}))
    assert me.aborted and me.aborted[0]["reason"] == "no_setup_data", me.aborted
    me2 = _FlowSelf(setup_data=None, timed_out=True)
    asyncio.run(fn(me2, {}))
    assert me2.aborted and me2.aborted[0]["reason"] == "no_setup_data", me2.aborted


# ══ r2（金标复测 2 轮）：_call_intent 另两形态 + 只读面不得武装监听 ══
def test_01b_dict_results_all_failed_is_failure():
    """SetDeviceMode 族只回 {"results":[…]}（无 success 键）——全失败仍被播「办好了」。"""
    resp = {"results": [{"name": "射灯", "success": False,
                         "error": "does not support set_mode"}]}
    out, _ = _call_intent("SetDeviceMode", dict(LIGHT_ARGS, mode="cool"), resp)
    assert out["success"] is False, out
    assert "set_mode" in (out.get("error") or out.get("result") or ""), out


def test_01c_real_intent_response_error_type_is_failure():
    """真 HA IntentResponse 无 .success 属性（失败走 response_type=ERROR）——
    旧判据 `getattr(response,'success',True)` 恒真 ⇒ 失败报成功。"""
    class _RT:
        name = "ERROR"

    resp = types.SimpleNamespace(response_type=_RT(),
                                 speech={"plain": {"speech": "设备离线，未执行"}})
    out, _ = _call_intent("TurnDeviceOn", LIGHT_ARGS, resp)
    assert out["success"] is False, out
    assert "设备离线" in (out.get("error") or out.get("result") or ""), out


def test_01d_action_done_object_uses_plain_speech_text():
    """成功形要带可读文本（真 IntentResponse 的 speech['plain']['speech']），
    不得回 '<…object at 0x…>'。"""
    class _RT:
        name = "ACTION_DONE"

    resp = types.SimpleNamespace(response_type=_RT(),
                                 speech={"plain": {"speech": "好的，已打开筒灯"}})
    out, _ = _call_intent("TurnDeviceOn", LIGHT_ARGS, resp)
    assert out["success"] is True and "已打开筒灯" in out["result"], out


def test_08_peek_automation_manager_never_arms():
    """匿名 GET /api/huijian-ai/automation-logs（requires_auth=False）只读访问
    不得建实例、不得排 async_start 任务（否则一次匿名请求即拉起状态监听+整点 tick）。"""
    ia._manager_instance = None
    tasks = []

    class _H:
        def async_create_task(self, coro, *a, **k):
            coro.close()
            tasks.append(coro)
            return None

    assert ia.peek_automation_manager(_H()) is None
    assert tasks == [], "只读访问把监听/整点 tick 拉起了"
