# -*- coding: utf-8 -*-
"""第四轮全仓审计修复批（P0→P1）行为钉——先钉后修，每条执行真实行为。

P0-① intent_window_control：显式 action 槽被设备名里的动词语素压过。
    name="开窗器"（开窗器/开窗机/电动开窗器 同族）经 find_action_in_text
    剥名后残「开器」命中 open 关键词「开」，**槽位里的 close 根本不读** ⇒
    「关闭开窗器」按「开启」键、返回 success:True、播报按请求形说「已关闭」。
    本钉：槽位 close + 名含动词语素 ⇒ 只许压关闭键，且开启键零调用。
    兼容位：槽位缺席（旧客户端只有名字）时保留"名称推动作"回落。
"""
import asyncio
import ast
import logging
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_audit4_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

import test_window_speed_behavior as bench  # noqa: E402  触发替身装载

wctl = bench.wctl
_State, _Entry = bench._State, bench._Entry

import copy  # noqa: E402

from core.nlu.fast_path import FastPath, Plan  # noqa: E402
from core.nlu.textcnn import TextCNN  # noqa: E402
from core.settings import DEFAULTS  # noqa: E402


@pytest.fixture(autouse=True)
def _stubs_in_place():
    bench._install_ha_stubs()
    yield


class _Device:
    def __init__(self, name, area_id=None):
        self.name = name
        self.name_by_user = None
        self.area_id = area_id


class _ER:
    def __init__(self, entries):
        self.by_id = {e.entity_id: e for e in entries}

    def async_get(self, entity_id):
        return self.by_id.get(entity_id)


class _DR:
    def __init__(self, devices):
        self.by_id = devices

    def async_get(self, device_id):
        return self.by_id.get(device_id)


class _AR:
    def __init__(self, mapping):
        self._m = mapping

    def async_get_area_by_name(self, name):
        aid = self._m.get(name)
        return types.SimpleNamespace(id=aid, name=name, aliases=set()) if aid else None

    def async_list_areas(self):
        return [types.SimpleNamespace(id=i, name=n, aliases=set())
                for n, i in self._m.items()]


class _States:
    def __init__(self, states):
        self._states = states

    def async_all(self):
        return list(self._states)

    def get(self, entity_id):
        for s in self._states:
            if s.entity_id == entity_id:
                return s
        return None


class _Services:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, service_data=None,
                         blocking=False, context=None, **kw):
        self.calls.append((domain, service, dict(service_data or {})))


def _build(dev_id, dev_name, dev_area_id, btn_names):
    """单设备多按钮：按钮名形如「办公室开窗器 开启 / 关闭」（网关 has_entity_name）。"""
    states, entries, devs = [], [], {}
    devs[dev_id] = _Device(dev_name, dev_area_id)
    for j, fname in enumerate(btn_names):
        eid = f"button.{dev_id}_{j}"
        states.append(_State(eid, name=fname))
        entries.append(_Entry(eid, "button", dev_id, f"{dev_id}_{j}", None))
    h = types.SimpleNamespace()
    h._er = _ER(entries)
    h._dr = _DR(devs)
    h._ar = _AR({"办公室": "area_office"})
    h.states = _States(states)
    h.services = _Services()
    return h


def _handle(hass, name, area, action=None, **extra):
    slots = {"target": {"value": [{"area": area,
                                   "devices": [{"name": name, "domains": []}]}]}}
    if action is not None:
        slots["action"] = {"value": action}
    for k, v in extra.items():
        slots[k] = {"value": v}
    ito = types.SimpleNamespace(hass=hass, context=None, slots=slots)
    return asyncio.run(wctl.ControlWindowIntent().async_handle(ito))


def _eids(calls):
    return sorted(c[2]["entity_id"] for c in calls)


# ── P0-①：显式槽位优先 ────────────────────────────────────────
OPENERS = ["开窗器", "开窗机", "电动开窗器"]


@pytest.mark.parametrize("opener", OPENERS)
def test_p0_explicit_close_beats_name_verb(opener):
    hass = _build("dev_off", f"办公室{opener}", "area_office",
                  [f"办公室{opener} 开启", f"办公室{opener} 关闭"])
    res = _handle(hass, opener, "办公室", action="close")
    assert res["success"] is True, res
    assert _eids(hass.services.calls) == ["button.dev_off_1"], (
        f"「关闭{opener}」必须压『关闭』键；"
        f"实际调用了 {_eids(hass.services.calls)}（开启键=方向反了）")


@pytest.mark.parametrize("opener", OPENERS)
def test_p0_explicit_open_still_opens(opener):
    hass = _build("dev_off", f"办公室{opener}", "area_office",
                  [f"办公室{opener} 开启", f"办公室{opener} 关闭"])
    res = _handle(hass, opener, "办公室", action="open")
    assert res["success"] is True, res
    assert _eids(hass.services.calls) == ["button.dev_off_0"]


def test_p0_name_derived_fallback_kept_for_legacy_payload():
    """兼容位：槽位缺席（旧客户端）时名称推动作的回落不许被一并删除。"""
    hass = _build("dev_off", "办公室开窗器", "area_office",
                  ["办公室开窗器 开启", "办公室开窗器 关闭"])
    res = _handle(hass, "开窗器", "办公室")     # 无 action 槽
    assert res["success"] is True, res
    assert _eids(hass.services.calls) == ["button.dev_off_0"], \
        "无槽位时沿用名称推动作（旧行为，非正确性承诺）"


def test_p0_real_topology_opener_naming_shape():
    """真机命名形态（.91 只读探测 2026-09-30）：设备名含「开窗器」+ 按钮名
    「开窗器 123f-020A ① 开启 / ③ 关闭」（①②③ 序号 + 动作词）。"""
    dev_name = "开窗器 123f-020A"
    hass = _build("dev_020a", dev_name, "area_office",
                  [f"{dev_name} ① 开启", f"{dev_name} ② 暂停", f"{dev_name} ③ 关闭"])
    res = _handle(hass, dev_name, "办公室", action="close")
    assert res["success"] is True, res
    assert _eids(hass.services.calls) == ["button.dev_020a_2"], (
        f"真机形态下「关闭」也不得被名中动词压成开启；实际 {_eids(hass.services.calls)}")


# ══ P1-⑤/P1-⑥：属性车道（湿度跨域改写 + 三条口径缺口）═══════════════════
class _AttrScenes:
    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def refresh(self, force=False):
        pass

    def check(self, text):
        return None

    async def verify_or_refresh(self, phrase):
        return False


@pytest.fixture(scope="module")
def fp():
    class _S:
        def get(self, dotted, default=None):
            cur = copy.deepcopy(DEFAULTS)
            for k in dotted.split("."):
                if not isinstance(cur, dict) or k not in cur:
                    return default
                cur = cur[k]
            return cur

    tc = TextCNN(os.environ["HUIJIAN_NLU_DATA"])
    tc._ensure()
    return FastPath(_AttrScenes(), tc, _S())


def _m(fp, text):
    return asyncio.run(fp.match(text))


def _dev0(p):
    tgt0 = (p.args.get("target") or [{}])[0]
    return tgt0, (tgt0.get("devices") or [{}])[0]


@pytest.mark.parametrize("text", ["客厅湿度调到60", "客厅湿度调高一点"])
def test_p1_humidity_area_sentence_keeps_humidity_attribute(fp, text):
    """集成 register_adjustment 只给 humidifier 注册了 humidity——
    属性名一旦被上游档跨域改写成 temperature，整句恒 unsupported。
    钉内部名：不许被跨域覆写（裸「湿度调到60」本来就对，这里钉带区域形态）。"""
    p = _m(fp, text)
    assert p is not None and p.intent == "AdjustDeviceAttribute", (text, p)
    assert p.args["attribute"] == "humidity", (text, p.args)
    _, dev0 = _dev0(p)
    assert "humidifier" in (dev0.get("domains") or []), (text, p.args)


ATTR_GAP = [
    # (句, 期望区域, 期望域)
    ("客厅亮度调到五十", "客厅", "light"),      # 中文数词
    ("次卧亮度调到五十", "次卧", "light"),      # 中文数词 + 非基础区域类
    ("阳台亮度调到50", "阳台", "light"),        # 区域类缺「台」
    ("玄关色温调成4000", "玄关", "light"),      # 区域类缺「关」
    ("客厅风速调到3档", "客厅", "climate"),     # 单位类缺「档」
    ("风速调到三档", "", "climate"),            # 档 + 中文数词（旧行为：整句 MISS）
    ("客厅风速调到三档", "客厅", "climate"),
]


@pytest.mark.parametrize(("text", "area", "dom"), ATTR_GAP)
def test_p1_attr_lane_gaps_no_device_name_promotion(fp, text, area, dom):
    """旧行为：这三类整句退化成"属性词升格成设备名"（name='亮度调到五十'、
    domains=[]，按名查无）或整句 MISS。正确形态=属性词快捷车道：name 空 +
    属性域过滤 + 区域就位。"""
    p = _m(fp, text)
    assert p is not None and p.intent == "AdjustDeviceAttribute", (text, p)
    tgt0, dev0 = _dev0(p)
    assert dom in (dev0.get("domains") or []), (text, p.args)
    assert not (dev0.get("name") or ""), f"属性词不得升格成设备名: {p.args}"
    assert (tgt0.get("area") or "") == area, (text, p.args)


# ══ P1-②：链式首腿判据不得吃整句（v1.1.21 只修了次腿）═══════════════════
class _ChainLane:
    def __init__(self, table):
        self.table = table

    async def match(self, text):
        return self.table.get(text)


class _ChainSettings:
    def __init__(self):
        self.d = {"dialog.dedup_window_s": 2.0, "dialog.context_enabled": True,
                  "dialog.chain_enabled": True, "dialog.confirm_risky": True,
                  "spatial.satellite_areas": {}, "llm.history_rounds": 10,
                  "dialog.fallback_text": "我还不太确定这个指令"}

    def get(self, k, default=None):
        return self.d.get(k, default)


class _NullQuery:
    async def answer(self, text):
        return None


class _HASink:
    def __init__(self):
        self.events = []

    async def fire_event(self, name, data):
        self.events.append((name, data))


def _chain_pipeline():
    """真 Pipeline + 真 Executor + FakeHAClient：走 handle() 全链，判 ha 实收。"""
    from collections import OrderedDict

    from conftest import FakeHAClient
    from core.executor import Executor
    from core.pipeline import Pipeline

    states = {
        "light.keting_deng": {"friendly_name": "客厅灯",
                              "attributes": {"friendly_name": "客厅灯"}},
        "cover.tuila": {"friendly_name": "推拉窗",
                        "attributes": {"friendly_name": "推拉窗"}},
    }
    ha = FakeHAClient(states=states, areas={"a1": "客厅"},
                      entity_area={"light.keting_deng": "客厅", "cover.tuila": "客厅"})
    ex = Executor(ha, None)
    p = Pipeline.__new__(Pipeline)
    p.settings = _ChainSettings()
    p.ha = _HASink()
    p.executor = ex
    p.agent = None
    p.query = _NullQuery()
    p.klar = _ChainLane({})
    p.fast_path = _ChainLane({
        "打开客厅的灯": Plan("TurnDeviceOn",
                             {"target": [{"area": "客厅",
                                          "devices": [{"name": "灯"}]}]}, "t0",
                             utterance="打开客厅的灯"),
        "关上推拉窗": Plan("ControlWindow",
                           {"action": "close",
                            "target": [{"area": "客厅",
                                        "devices": [{"name": "推拉窗"}]}]}, "t0",
                           utterance="关上推拉窗"),
    })
    p.scenes = None
    p._last = OrderedDict()
    p._turns, p._last_target, p._origin_ts, p._confirm = {}, {}, {}, {}
    p._pending = set()
    import time as _time
    p._vocab_ts = _time.time()
    return p, ha, ex


def test_p1_chain_first_leg_uses_own_clause():
    """灯腿（TurnDeviceOn，无窗词）+ 窗腿（ControlWindow）一句链发：
    首腿判据若吃整句，窗腿的「窗」把灯腿误拒 ⇒ 整链零下发（现场实测形）。"""
    p, ha, ex = _chain_pipeline()
    reply = asyncio.run(p.handle("打开客厅的灯然后关上推拉窗", origin="devA"))
    assert [c[0] for c in ha.calls] == ["TurnDeviceOn", "ControlWindow"], \
        f"整链必须两腿都下发；播报={reply.text!r} 实收={ha.calls}"
    assert ex.last_run["applied"] == 2, ex.last_run


def test_p1_chain_first_leg_window_sentence_still_refused():
    """反向守卫：首腿**自己**带窗族词（无实体）时，闸照旧拦（不许为了修链
    把整句判据一起放开）。"""
    p, ha, ex = _chain_pipeline()
    p.fast_path = _ChainLane({
        "打开展厅推拉窗": Plan("TurnDeviceOn",
                               {"target": [{"area": "展厅",
                                            "devices": [{"name": "推拉窗"}]}]}, "t0",
                               utterance="打开展厅推拉窗"),
        "关上推拉窗": Plan("ControlWindow",
                           {"action": "close",
                            "target": [{"area": "客厅",
                                        "devices": [{"name": "推拉窗"}]}]}, "t0",
                           utterance="关上推拉窗"),
    })
    reply = asyncio.run(p.handle("打开展厅推拉窗然后关上推拉窗", origin="devB"))
    assert ha.calls == [], f"首腿原话含窗族词 ⇒ 能力闸仍须拦（不得放开）: {ha.calls}"
    assert "冒按" in reply.text, reply.text


# ══ P1-③：TTS 换绑先验新档——未就绪不得先卸旧嗓 ═══════════════════════
def test_p1_tts_rebind_probes_target_before_unload():
    from core.tts import TtsEngine

    class _Store:
        def __init__(self):
            self.probe_when_loaded = []
            self.ensure_calls = []

        def lock_entry(self, key):
            return {}

        def model_dir_for(self, key):
            self.probe_when_loaded.append(engine._tts is not None)
            return None          # 目标档未就绪（缺 vocos / 未下完 同形）

        def ensure(self, key):
            self.ensure_calls.append(key)
            return False

        def voices_count_for(self, key):
            return 0

    class _Conf:
        def get(self, k, dv=None):
            return {"tts.provider": "local_matcha", "tts.sid": 0,
                    "tts.speed": 1.0, "tts.cache_enabled": True}.get(k, dv)

    store = _Store()
    engine = TtsEngine(_Conf(), store)
    old = object()
    engine._tts = old
    engine._loaded_prov = "local_melo"      # 在载=旧嗓，配置档=matcha（换绑态）
    assert engine.ensure_loaded() is True
    assert engine._tts is old, \
        "目标档未就绪时不得卸掉在载旧嗓（旧序：卸完再验 ⇒ 两档皆无=彻底哑）"
    assert store.probe_when_loaded and store.probe_when_loaded[0] is True, \
        "就绪性探测必须发生在卸旧嗓之前（顺序钉）"
    assert store.ensure_calls, "未就绪要走备料，否则下一轮等不到换绑"
    assert not engine.ready_for_current_provider()


# ══ P1-④：turn 族逐台真值（服务调用失败不得进 control_targets）═════════
# 装载纪律：借 bench 家底 + v1097 的 intent_turn 扩展面（hjspeed_pkg 命名空间），
# **不得**调 hv1127ia 的 load()——它会替换 homeassistant.helpers.* 模块对象，
# 污染同会话后面的 test_v1097（实测 3 红）。这里只用 IA 的纯替身类/函数。
def _turn_harness():
    import test_v1097_intent_500_guard as v1097
    import test_v1127_intent_actions as ia

    bench._install_ha_stubs()
    v1097._extend_stubs_for_turn()
    it = sys.modules["homeassistant.helpers.intent"]
    it.MatchTargetsConstraints = ia.MatchTargetsConstraints
    it.async_match_targets = ia._async_match_targets
    return ia, v1097._load("intent_turn")


class _TurnStates:
    def __init__(self, items):
        self._items = list(items)

    def async_all(self, domain=None):
        if not domain:
            return list(self._items)
        doms = {domain} if isinstance(domain, str) else set(domain)
        return [s for s in self._items
                if s.entity_id.split(".", 1)[0] in doms]

    def get(self, entity_id):
        for s in self._items:
            if s.entity_id == entity_id:
                return s
        return None


class _TurnER:
    def __init__(self, mapping):
        self.by_id = dict(mapping)
        self.entities = self.by_id

    def async_get(self, entity_id):
        return self.by_id.get(entity_id)

    def async_entries_for_device(self, device_id):
        return [e for e in self.by_id.values()
                if getattr(e, "device_id", None) == device_id]


class _TurnDR:
    def __init__(self, mapping):
        self.by_id = dict(mapping)
        self.devices = self.by_id

    def async_get(self, device_id):
        return self.by_id.get(device_id)


class _TurnAR:
    def __init__(self, mapping):
        self.by_name = dict(mapping)

    def async_get(self, area_id):
        return None

    def async_get_area(self, area_id):
        return None

    def async_get_area_by_name(self, name):
        return self.by_name.get(name)

    def async_list_areas(self):
        return list(self.by_name.values())


class _TurnServices:
    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)

    def has_service(self, domain, service):
        return True

    async def async_call(self, domain, service, service_data=None, context=None,
                         blocking=False, target=None, return_response=None, **kw):
        data = dict(service_data or {})
        self.calls.append((domain, service, data))
        if (domain, service, data.get("entity_id")) in self.fail_on:
            raise RuntimeError(f"{domain}.{service} 拒绝了 {data.get('entity_id')}")
        return None


class _TurnHass:
    """双风格注册表（`_er` = bench 家底口径 / `entity_registry` = IA 匹配口径）。"""

    def __init__(self, states, entities=None, devices=None, areas=None, fail_on=()):
        self.states = _TurnStates(states)
        self._er = _TurnER(entities or {})
        self.entity_registry = self._er
        self._dr = _TurnDR(devices or {})
        self.device_registry = self._dr
        self._ar = _TurnAR(areas or {})
        self.area_registry = self._ar
        self.services = _TurnServices(fail_on)
        self.tasks = []
        self.assistant = None

    def async_create_task(self, coro, *a, **kw):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    def async_create_task_internal(self, coro, name=None, *a, **kw):
        return self.async_create_task(coro, *a, **kw)


def _turn_case(fail_on):
    ia, mod = _turn_harness()
    states = [ia._st("media_player.tv", "电视", "on"),
              ia._st("light.tai_deng", "台灯", "off")]
    ents = {"media_player.tv": ia._entry(area_id="ke_ting"),
            "light.tai_deng": ia._entry(area_id="ke_ting")}
    areas = {"客厅": ia._area("ke_ting", "客厅")}
    hass = _TurnHass(states, ents, {}, areas, fail_on=fail_on)
    slots = {"target": {"value": [
        {"area": "客厅", "devices": [{"name": "电视"}]},
        {"area": "客厅", "devices": [{"name": "台灯"}]},
    ]}}
    ito = types.SimpleNamespace(hass=hass, context=None, assistant=None,
                                language="zh-CN", slots=slots)
    return hass, asyncio.run(mod.TurnDeviceOffIntent().async_handle(ito))


def test_p1_turn_failed_call_not_counted_as_controlled():
    """电视（media_player）调用抛错、台灯成功 ⇒ 电视不得进 control_targets，
    且 partial_error 必须点名（旧形：失败台照进成功面、整单 success:True）。"""
    calls = []
    for dom, svc in (("media_player", "turn_off"), ("homeassistant", "turn_off")):
        calls.append((dom, svc, "media_player.tv"))
    hass, result = _turn_case(calls)
    assert result.get("success") is True, result      # 台灯成功 ⇒ 整单不失败
    names = [c.get("name") for c in result.get("control_targets") or []]
    assert "电视" not in names, f"失败的电视不得进 control_targets: {result}"
    assert "台灯" in names, result
    assert result.get("partial_error") and "电视" in str(result["partial_error"]), result


def test_p1_turn_all_calls_failed_is_not_success():
    """全部目标都调用失败 ⇒ 整单必须如实失败（旧形：全进成功面 + success:True）。"""
    calls = [("media_player", "turn_off", "media_player.tv"),
             ("homeassistant", "turn_off", "media_player.tv"),
             ("light", "turn_off", "light.tai_deng"),
             ("homeassistant", "turn_off", "light.tai_deng")]
    hass, result = _turn_case(calls)
    assert result.get("success") is False, result
    assert not result.get("control_targets"), result
    assert "没操作成功" in str(result.get("error")), result


# ══ P1-⑫：executor 代打留痕必须按轮隔离（单例跨会话串播）═══════════════
def test_p1_executor_repoint_notes_are_per_run():
    from conftest import FakeHAClient
    from core.executor import Executor

    class _Gated(Executor):
        """A 轮卡在能力闸后的可用性闸上，给 B 轮留出完整跑完的窗口。"""

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self._gated = False

        async def _availability_refuse(self, name, args):
            if not self._gated:
                self._gated = True
                self.entered.set()
                await self.release.wait()
            return await super()._availability_refuse(name, args)

    states = {
        "light.she_deng": {"state": "unavailable",
                           "attributes": {"friendly_name": "射灯"}},
        "light.bg_she_deng": {"state": "on",
                              "attributes": {"friendly_name": "射灯"}},
        "light.tai_deng": {"state": "on",
                           "attributes": {"friendly_name": "台灯"}},
    }
    ha = FakeHAClient(states=states,
                      entity_area={"light.she_deng": "客厅",
                                   "light.bg_she_deng": "办公室",
                                   "light.tai_deng": "客厅"})
    ex = _Gated(ha, None)
    plan_a = Plan("TurnDeviceOn", {"entity_id": "light.tai_deng"}, "t0",
                  utterance="打开台灯")
    plan_b = Plan("TurnDeviceOn", {"entity_id": "light.she_deng"}, "t0",
                  utterance="打开射灯")

    async def scenario():
        ta = asyncio.create_task(ex.run(plan_a))
        await ex.entered.wait()          # A 已在飞（此刻其桶已置）
        _ok_b, sp_b = await ex.run(plan_b)   # B 完整跑完，写下"已改指"留痕
        ex.release.set()
        _ok_a, sp_a = await ta
        return sp_a, sp_b

    sp_a, sp_b = asyncio.run(scenario())
    assert "已改指" in sp_b, f"B 轮未产生改指留痕（替身形态不对）: {sp_b!r}"
    assert "已改指" not in sp_a, f"A 轮播报串入了 B 轮的改指注: {sp_a!r}"


# ══ P1-⑧：条目 reload 后语音自动化重新武装 ═══════════════════════════
def _entry_setup_fn(name):
    import ast
    src = (Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai"
           / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    return src, next(n for n in ast.walk(tree)
                     if isinstance(n, ast.AsyncFunctionDef) and n.name == name)


def test_p1_entry_setup_rearms_automation_listeners():
    """async_unload_entry 会 reset_automation_globals()（真注销状态监听+整点 tick），
    而重武装只在 get_automation_manager 懒建时发生——条目 reload 后若没人再调它，
    全屋传感器/时间自动化静默停摆到下次重启 HA。钩子必须在 async_setup_entry 内、
    且**无条件**（对抗复核 A3：放在 !=assist 分支里 ⇒ assist 条目 reload 不补挂）。"""
    src, fn = _entry_setup_fn("async_setup_entry")
    direct = [n for n in fn.body
              if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
              and isinstance(n.value.func, ast.Name)
              and n.value.func.id == "get_automation_manager"]
    assert direct, ("async_setup_entry 顶层（非 if 内）必须调 get_automation_manager "
                    "重新武装（reload 后静默停摆；assist 条目也要补挂）")


def test_p1_remove_entry_rearms_automation_listeners():
    """删除条目走 remove（不经 setup）——unload 已 reset 监听，remove 必须补挂。"""
    src, fn = _entry_setup_fn("async_remove_entry")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "get_automation_manager"]
    assert calls, "async_remove_entry 未补挂自动化监听（删设备后全屋自动化停摆）"


# ══ 对抗复核 A4：close 超时的 abort 兜底是死码 ════════════════════════
def test_p1_ws_abort_helper_actually_aborts():
    """aiohttp 的 ClientWebSocketResponse 没有 .transport（实测）——旧写法
    `ws.transport and ws.transport.abort()` 恒不执行＝死码。新助手必须能沿
    `_response.connection.transport` 找到真传输并 abort。"""
    import ast

    cc = Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai"
    src = (cc / "huijian" / "ws_transport.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == "abort_ws_transport")
    ns: dict = {}
    exec(compile(ast.get_source_segment(src, node), "<abort>", "exec"), ns)  # noqa: S102
    abort = ns["abort_ws_transport"]

    class _Tr:
        def __init__(self):
            self.n = 0

        def abort(self):
            self.n += 1

    class _WS:
        def __init__(self, tr):
            self._response = type("R", (), {"connection": type("C", (), {"transport": tr})()})()

    tr = _Tr()
    assert abort(_WS(tr)) is True and tr.n == 1, "真路径 _response.connection.transport 未 abort"
    tr2 = _Tr()
    ws2 = type("W", (), {"transport": tr2})()
    assert abort(ws2) is True and tr2.n == 1, "确带 .transport 的实现/替身兼容"
    assert abort(object()) is False, "无传输应返回 False 且不抛"

    # 结构面：四处收口点都改用助手，旧死码形态不得回潮
    import re as _re
    for f in ("huijian/ws_transport.py", "huijian/mcp_transport.py"):
        s = (cc / f).read_text(encoding="utf-8")
        body = _re.sub(r'\"\"\".*?\"\"\"', "", s, flags=_re.S)   # 剥 docstring（示例文本别扫自己）
        assert "ws.transport.abort()" not in body and             "t and t.abort()" not in body, f"{f} 回潮死码 abort 形态"
        assert "abort_ws_transport(" in s, f"{f} 未接 abort 助手"
    assert "from .ws_transport import WsTransport, abort_ws_transport" in \
        (cc / "huijian" / "mcp_transport.py").read_text(encoding="utf-8")


# ══ P1-⑨：MCP 通道日志纪律 / 拆连带闸（基类同口径）═════════════════════
_MCP_SRC = (Path(__file__).resolve().parents[1] / "custom_components"
            / "huijian_ai" / "huijian" / "mcp_transport.py")


def test_p1_mcp_endpoint_logs_redacted():
    """mcp endpoint 可内嵌 ?token=：三处 INFO/异常日志必须走脱敏助手，
    出帧全文不得进 INFO（基类 v1.0.48 纪律，本通道此前漏网）。"""
    src = _MCP_SRC.read_text(encoding="utf-8")
    assert "endpoint=%s" in src and "_redact_endpoint(endpoint)" in src, \
        "Set mcp endpoint 日志未脱敏"
    assert 'self.logger.info("Connecting to: %s", self.endpoint)' not in src, \
        "Connecting to 裸打含 token 的 endpoint 回潮"
    assert "self.logger.info(\"Send message: %s\", message)" not in src, \
        "出帧全文回潮 INFO（含工具参数/家居文本）"
    assert '"Send message: %d chars%s"' in src, "出帧日志缺长度形制（基类同款）"
    assert "_redact_endpoint(self.endpoint)" in src


def test_p1_mcp_writer_close_is_bounded():
    """基类 T3：半开 TCP 上裸 close 无限挂 = 僵尸连接签名。mcp 覆写的 writer
    finally 必须 wait_for(5)+abort 收口（旧形裸 await close()）。"""
    src = _MCP_SRC.read_text(encoding="utf-8")
    i = src.index("Websocket writer stopped")
    seg = src[i:i + 1200]
    assert "asyncio.wait_for(self._current_ws.close(), 5)" in seg, \
        "mcp writer close 缺带闸收口（半开 TCP 无限挂）"
    assert "abort_ws_transport(" in seg, "close 超时未 abort（拆链路径留无限 await）"
    assert "import asyncio" in src, "缺 asyncio 导入"


# ══ P1-⑦：assist_pipeline_state 取消/摘实体路径必须收口 ═══════════════
_SAT = (Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai"
        / "assist_satellite.py")


def _exec_sat_class(names, base=None):
    """抽真源码方法组一个类执行（同 test_v1055 的 _extract 手法）。"""
    import ast
    import textwrap

    src = _SAT.read_text(encoding="utf-8")
    tree = ast.parse(src)
    segs = {}
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in names):
            segs.setdefault(node.name, ast.get_source_segment(src, node))
    assert set(segs) == set(names), f"缺方法源码: {set(names) - set(segs)}"
    body = "\n".join(textwrap.indent(s, "    ") for s in segs.values())
    header = "class _Sat(_BASE):\n" if base else "class _Sat:\n"
    ns = {"_BASE": base or object, "_LOGGER": logging.getLogger("sat"),
          "asyncio": asyncio}
    exec(compile(header + body, "<sat-extract>", "exec"), ns)  # noqa: S102 抽真源码
    return ns["_Sat"]


class _EntryDataSpy:
    def __init__(self, state=True):
        self.assist_pipeline_state = state
        self.calls = []

    def async_set_assist_pipeline_state(self, v):
        self.assist_pipeline_state = v
        self.calls.append(v)


class _LiveTask:
    def done(self):
        return False


def _sat_instance(cls, *, streaming=None):
    s = cls()
    s._entry_data = _EntryDataSpy()
    s._pipeline_task = object()
    s._round_outer_task = s._pipeline_task
    s._active_pipeline_index = 5
    s._stop_udp_server = lambda: None
    s._tts_streaming_task = streaming
    return s


def test_p1_pipeline_state_cleared_when_round_finishes():
    """轮结束（含取消/断连路径）必须收口状态位——旧形只有 RUN_END(无TTS) 与
    _converge_response 清，取消支两者都不走 ⇒ 卡 True ⇒ 之后每条播报被判
    「撞活跃轮」= API 音频板 0 字节。"""
    sat = _exec_sat_class({"handle_pipeline_finished"})
    s = _sat_instance(sat)
    s.handle_pipeline_finished(None)      # 当前轮结束（非 stale）
    assert s._entry_data.calls == [False], \
        "轮结束必须收口 assist_pipeline_state（卡 True = 播报 0 字节）"


def test_p1_pipeline_state_not_cleared_while_streaming():
    """反向守卫：推流还在飞时不得清——清了 announce 会抢走下行（护栏②）。"""
    sat = _exec_sat_class({"handle_pipeline_finished"})
    s = _sat_instance(sat, streaming=_LiveTask())
    s.handle_pipeline_finished(None)
    assert s._entry_data.calls == [], "在飞推流期间清状态 = announce 抢下行回归面"
    assert s._entry_data.assist_pipeline_state is True


def test_p1_pipeline_state_cleared_on_entity_removal():
    class _Base:
        async def async_will_remove_from_hass(self):
            pass

    sat = _exec_sat_class({"async_will_remove_from_hass"}, base=_Base)
    s = _sat_instance(sat)
    s._is_running = True
    s.entity_id = "assist_satellite.x"
    s._stop_pipeline = lambda: None
    asyncio.run(s.async_will_remove_from_hass())
    assert s._entry_data.calls == [False], \
        "摘实体必须收口状态位（否则骑到下次 setup 的新实体）"


# ══ P1-⑪：自定义 LLM 通道——自动化两链的动作级风险闸 ═════════════════
def test_p1_llm_automation_action_chains_scanned_for_lock():
    """HassCreateAutomation/HassUpdateAutomation 的 actions 与场景同形：一句
    「当温度超30就把大门解锁」在此通道此前不经任何闸（LLM 面连点名都没有）。"""
    import test_v1127_scene_flow as sc

    sc._install_stubs()
    claw = sc.claw
    lock_actions = [{
        "intent": "TurnDeviceOff",
        "params": {"target": [{"devices": [{"name": "大门锁", "domains": ["lock"]}]}]},
    }]
    assert claw.scene_actions_hit_risk(lock_actions) is True, "基础扫描面（既有）"
    api = claw.HuijianControlAPI.__new__(claw.HuijianControlAPI)
    for name in ("HassCreateAutomation", "HassUpdateAutomation"):
        hit = asyncio.run(api._scene_chain_hits_risk(None, name, {"actions": lock_actions}))
        assert hit is True, f"{name} 的锁动作未进风险闸（免确认解锁后门）"
    # 反向守卫：无锁动作的普通自动化不得被误拒
    safe_actions = [{"intent": "TurnDeviceOn",
                     "params": {"target": [{"devices": [{"name": "台灯",
                                                         "domains": ["light"]}]}]}}]
    hit = asyncio.run(api._scene_chain_hits_risk(
        None, "HassCreateAutomation", {"actions": safe_actions}))
    assert hit is False, "普通自动化动作被误拒（闸放太宽）"


def test_p1_llm_risk_gate_no_chinese_name_false_positive():
    """对抗复核 A2：动作链闸早期版本对「大门灯」「卷帘门」这类**名字含中文词但域
    非锁**的目标误拒（实测恒 True）⇒ 普通自动化直接建不了。现判据只看域/实体
    证据（通道在闸前已 enrich 回填真实域），下列两条必须放行。"""
    import test_v1127_scene_flow as sc

    sc._install_stubs()
    claw = sc.claw
    fps = [
        [{"intent": "TurnDeviceOff",
          "params": {"target": [{"devices": [{"name": "大门灯",
                                              "domains": ["light"]}]}]}}],
        [{"intent": "TurnDeviceOff",
          "params": {"target": [{"devices": [{"name": "卷帘门",
                                              "domains": ["cover"]}]}]}}],
    ]
    for acts in fps:
        assert claw.scene_actions_hit_risk(acts) is False, (
            f"域非锁的动作被中文名子串误拒: {acts}")
    # 真风险形态仍必须命中：域闭包 / 别名 / entity_id / 安防域
    hits = [
        [{"intent": "TurnDeviceOff",
          "params": {"target": [{"devices": [{"name": "大门锁",
                                              "domains": ["lock"]}]}]}}],
        [{"intent": "TurnDeviceOff",
          "params": {"target": [{"devices": [{"name": "大门",
                                              "domains": ["door"]}]}]}}],
        [{"intent": "HassUnlock",
          "params": {"entity_id": "lock.da_men"}}],
        [{"intent": "TurnDeviceOff",
          "params": {"target": [{"devices": [{"name": "家庭安防",
                                              "domains": ["alarm_control_panel"]}]}]}}],
    ]
    for acts in hits:
        assert claw.scene_actions_hit_risk(acts) is True, f"真锁动作漏网: {acts}"


# ══ P1-⑩：管理页写操作必须带 HA 令牌（后端 A11 闸的消费端接线）════════
def test_p1_manage_templates_send_token_on_write():
    """后端四个写视图 v1.1.30 起 requires_auth=True，而页面此前裸 fetch ⇒
    删除/改名/试运行恒 401。两边必须成对：写面走 fetchWrite(带 Bearer)，
    只读面保持匿名裸 fetch。"""
    cc = Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai"
    mg = (cc / "templates" / "manage.html").read_text(encoding="utf-8")
    ao = (cc / "templates" / "automations.html").read_text(encoding="utf-8")
    api_src = (cc / "api.py").read_text(encoding="utf-8")

    # 后端：四个写视图仍是令牌闸（若哪天降回匿名，本钉与 A11 钉一并红）
    for cls in ("TestSceneView", "TestAutomationView",
                "VoiceSceneDeleteView", "AutomationDeleteView"):
        block = api_src[api_src.index(f"class {cls}("):][:400]
        assert "requires_auth = True" in block, f"{cls} 令牌闸丢失"

    # 前端：写调用一律 fetchWrite（自带 Bearer），裸 fetch 只许留在只读面
    for src in (mg, ao):
        assert "function fetchWrite(" in src, "缺 fetchWrite 助手"
        assert "Authorization': 'Bearer ' + haToken()" in src, "写请求未带 Bearer"
    assert "await fetch(SCENES_API" not in mg, "场景写调用回潮裸 fetch（401）"
    assert "await fetch(AUTOS_API" not in mg, "自动化写调用回潮裸 fetch（401）"
    assert "await fetch('/api/huijian-ai/test-scene'" not in mg
    assert "await fetch('/api/huijian-ai/test-automation'" not in mg
    assert "await fetch(API_BASE + '/' + automationId" not in ao
    # 只读面保持匿名（不得被顺手加令牌：读面 requires_auth=False 是明文口径）
    assert "await fetch('/api/huijian-ai/automation-logs')" in mg


# ══ P1-⑬：解绑口（remove）不再匿名 ═══════════════════════════════════
def test_p1_remove_endpoint_requires_ha_token():
    """签名密钥只是设备 MAC（BLE 广播明文）⇒ 匿名+签名=局域网可解绑。
    与同文件 satellites/ota/continuous 同口径：requires_auth=True 双闸，
    签名算法本身不动（跨端契约）。"""
    src = (Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai"
           / "huijian" / "http.py").read_text(encoding="utf-8")
    i = src.index("class HuijianRemoveView(")
    block = src[i:src.index("class ", i + 10)]
    assert "requires_auth = True" in block, "解绑口回潮匿名（局域网可解绑）"
    assert "check_sign(request, speak_id)" in block, "签名校验被删（跨端契约面）"
    # 对抗复核 A1：签名必须走**独立头**——`Authorization` 已被令牌闸占用
    # （Bearer <HA令牌>），旧实现拿同一头比裸摘要 ⇒ 认证通过则签名必失败。
    assert 'request.headers.get("X-Huijian-Sign", "")' in src, \
        "签名未改走独立头（与令牌闸同头冲突 ⇒ 端点恒 400）"
    j = src.index("async def check_sign(")
    cblock = src[j:src.index("class HuijianSetupView", j)]
    assert 'request.headers.get("Authorization")' not in cblock, \
        "签名仍拿 Authorization 比裸摘要（双闸自相矛盾）"


def test_p1_remove_sign_header_semantics():
    """行为钉：无签名头=令牌独立放行；带且错=拒；带且对=放行。"""
    import ast
    import hashlib

    cc = Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai"
    src = (cc / "huijian" / "http.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    segs = {}
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in ("check_sign", "calculate_sign")):
            segs.setdefault(node.name, ast.get_source_segment(src, node))
    import hashlib as _hl
    ns: dict = {"KEY_HASS": "hass", "DOMAIN": "huijian_ai", "hashlib": _hl,
                "web": types.SimpleNamespace(Request=object)}
    exec(compile(segs["calculate_sign"], "<cs>", "exec"), ns)  # noqa: S102 抽真源码
    exec(compile(segs["check_sign"], "<ck>", "exec"), ns)      # noqa: S102

    class _Req:
        def __init__(self, headers):
            self.app = {"hass": _Hass()}
            self.query = {}
            self.method = "DELETE"
            self.path = "/api/huijian-ai/remove"
            self.headers = headers

    class _Entry:
        data = {"speak_id": "sp1", "mac": "AA:BB:CC:DD:EE:FF"}

    class _Hass:
        class config_entries:
            @staticmethod
            def async_loaded_entries(domain):
                return [_Entry()]

    def run(headers):
        # check_sign 是真类方法（首参 self，体内不用 self）→ 传哑 self 直调
        return asyncio.run(ns["check_sign"](None, _Req(headers), "sp1"))

    assert run({}) is not False and run({}) is not None, "无签名头应放行（令牌是主闸）"
    good = ns["calculate_sign"]("/api/huijian-ai/remove", {},
                               "aa:bb:cc:dd:ee:ff", "s1")
    assert run({"X-Huijian-Sign": good, "Salt": "s1"}) is not False, "正确签名应放行"
    assert run({"X-Huijian-Sign": "deadbeef", "Salt": "s1"}) is False, "错误签名必须拒"
