# -*- coding: utf-8 -*-
"""v1.1.27 core 运维批：7 项真行为钉（先钉后修——每钉在修复前必须红）。

项1 executor 多腿链 applied：前腿已真执行（_receipt 跳过无逐实体行的腿）而末腿逐台
     全败时不得写 0，否则 pipeline._exec_risk 只认 applied>0 ⇒ 放行降级重放/LLM 复议
     （已执行的腿再做一遍）。
项2 pipeline 歧义闸 `_AMB_INTENTS` 补 ControlWindow/HassLock/HassUnlock（docstring
     自述的实锤案「平开窗/测试平开窗」正是窗形）；风险闸 `_RISKY_INTENTS` 补 HassLock
     （同族锁动作都要确认环，话术必须是"上锁"）。
项3 pipeline 查询闸不得吞掉 scene 计划（模块头 :302／fast_path:1060-1066 都写
     "scene 契约恒最高优先"）。
项4 capability 与 executor 两份"不可开关"域表并集单一来源；`domains` 传字符串不得
     炸成单字符（候选恒空＝静默失效）。
项5 firmware_store 拒因必须覆盖全部拒签支路（小体积合并镜像）；status()["latest"]
     与 latest() 同判据（不得显示不可 OTA 版本）。
项6 ota_api `bridge_ok` 用 `.reachable`（同 admin_api:75 口径）；capacity_error 走
     to_thread（同函数其余 store 调用）。
项7 admin_api 「强制重取」单飞未受理必须如实回执；tts 试听 text=null 不得合成 "None"。
"""
import asyncio
import hashlib
import threading
import time

from conftest import FakeHAClient
from core import firmware_store as fs
from core.admin_api import make_admin_app
from core.executor import Executor
from core.nlu.fast_path import Plan
from core.pipeline import Pipeline, select_primary_plan
from core.ws_server import AppContext

from test_experience_batch import _p, _pipe
from test_ota_firmware import SettingsFake, _drop, _jget, _jpost, _serve, _write_lock


def _ent(eid, state, name):
    return {"entity_id": eid, "state": state, "attributes": {"friendly_name": name}}


class _Ha(FakeHAClient):
    """两条外发通道都记账（同 test_chain_leg_channel.Ha 手法）：直调走 svc_calls。"""

    def __init__(self, svc_results=None, **kw):
        super().__init__(**kw)
        self.svc_calls = []
        self._svc_results = svc_results or {}

    async def call_service(self, domain, service, data, timeout=10.0):
        self.svc_calls.append((domain, service, data))
        return self._svc_results.get(f"{domain}.{service}", {"success": True})


LAMPER = "light.ban_gong_shi_she_deng"
_ALL_FAIL = {"success": True, "states": [
    {"name": "射灯", "success": False, "error": "does not support turn_on"}]}


# ── 项1：多腿链 applied 语义 ──────────────────────────────────────
def test_multileg_front_leg_effect_counted_in_applied():
    """前腿真执行 + 末腿逐台全败 ⇒ applied 必须＝本次"已生效步数"（≥1），禁重放闸才亮。

    前腿走 klar 直调（call_service），回执无逐实体行 ⇒ `_receipt`（:944-945）跳过它，
    旧写法在 :781 恒写 applied:0 ⇒ `_exec_risk()` 假 ⇒ 降级重放(:760)/LLM 复议(:807)
    把已执行的前腿再做一遍。
    """
    ha = _Ha(results={"HassTurnOn": _ALL_FAIL})
    plan = Plan(
        intent="HassTurnOff", args={"entity_id": LAMPER}, source="klar",
        utterance="关灯然后打开射灯",
        extra_steps=[{"name": "HassTurnOn",
                      "args": {"target": [{"area": "办公室",
                                           "devices": [{"name": "射灯",
                                                        "domains": ["light"]}]}]},
                      "source": "t0", "utterance": "打开射灯"}])
    ex = Executor(ha)
    ok, _reply = asyncio.run(ex.run(plan))
    # 成功口径不动：末腿逐台全败＝如实失败（既有行为钉）
    assert ok is False
    assert ha.svc_calls == [("homeassistant", "turn_off", {"entity_id": LAMPER})], ha.svc_calls
    assert ex.last_run["applied"] == 1, f"前腿已真执行却记 applied={ex.last_run}"
    assert _pipe(ex=ex)._exec_risk() is True, "已生效的一轮被当成没生效 ⇒ 会被重放"


def test_single_leg_all_fail_keeps_applied_zero():
    """反向钉：单腿逐台全败不得虚增 applied（没有"前腿"这回事）。"""
    ha = _Ha(results={"HassTurnOn": _ALL_FAIL})
    plan = Plan(intent="HassTurnOn",
                args={"target": [{"area": "办公室",
                                  "devices": [{"name": "射灯", "domains": ["light"]}]}]},
                source="t0", utterance="打开射灯")
    ex = Executor(ha)
    ok, _reply = asyncio.run(ex.run(plan))
    assert ok is False and ex.last_run["applied"] == 0, ex.last_run


# ── 项2：歧义闸/风险闸族覆盖 ─────────────────────────────────────
class _AmbHa:
    def __init__(self, states):
        self._states = states
        self._entity_area = {}


def _amb_states():
    return {
        "cover.ping_kai_chuang": _ent("cover.ping_kai_chuang", "closed", "平开窗"),
        "cover.ce_shi_ping_kai_chuang": _ent("cover.ce_shi_ping_kai_chuang",
                                             "closed", "测试平开窗"),
        "lock.da_men": _ent("lock.da_men", "locked", "大门"),
        "lock.da_men_ce": _ent("lock.da_men_ce", "locked", "大门侧"),
    }


def test_ambiguity_ask_covers_window_and_lock_families():
    """点名声命中多台就必须先问——窗形（实锤案）与锁两族此前整族旁路。"""
    p = _pipe(ha=_AmbHa(_amb_states()))
    cases = (
        Plan(intent="ControlWindow",
             args={"target": [{"area": "", "devices": [{"name": "平开窗",
                                                        "domains": ["cover", "button"]}]}],
                   "action": "open"}, source="t0", utterance="打开平开窗"),
        Plan(intent="HassUnlock",
             args={"target": [{"area": "", "devices": [{"name": "大门",
                                                        "domains": ["lock"]}]}]},
             source="t0", utterance="解锁大门"),
        Plan(intent="HassLock",
             args={"target": [{"area": "", "devices": [{"name": "大门",
                                                        "domains": ["lock"]}]}]},
             source="t0", utterance="锁上大门"),
    )
    for plan in cases:
        p._confirm.clear()
        r = p._ambiguity_ask(plan, "o")
        assert r is not None and r.source == "confirm", f"{plan.intent} 歧义未先问：{r}"


def test_risky_ring_covers_hasslock_and_says_lock():
    """同族锁动作都要确认环；问句必须说「上锁」，不得沿用解锁话术（语义反转）。"""
    p = _pipe()
    plan = _p("HassLock", {"target": [{"area": "门口",
                                       "devices": [{"name": "大门", "domains": ["lock"]}]}]})
    assert p._risky(plan) is True, "上锁未进风险闸＝无确认环直拔门禁"
    r = p._confirm_ask(plan, "o")
    assert r is not None and "上锁" in r.text and "解锁" not in r.text, r and r.text


def test_create_note_names_lock_for_hasslock_action():
    """入库点名尾注同源：HassLock 动作必须写「上锁」（旧表只认解锁族，恒无注）。"""
    note = Pipeline._risky_actions_note([
        {"intent": "HassLock",
         "params": {"target": [{"area": "", "devices": [{"name": "大门"}]}]}}])
    assert "上锁" in note and "解锁" not in note, note


# ── 项3：scene 契约不受查询闸影响 ────────────────────────────────
def test_scene_contract_survives_query_gate():
    """疑问形触发词的场景：scene 计划必须原样胜出（旧码在 :312 之前把 fp 置 None）。"""
    fp = Plan(intent="HassTriggerVoiceScene", args={"trigger_phrase": "哪些灯开着"},
              source="scene", utterance="哪些灯开着")
    kl = _p("HassTurnOn", {"entity_id": "light.a"}, source="klar")
    assert select_primary_plan(fp, kl) is fp, "场景契约被查询闸吞掉"
    # 反向钉：非 scene 的字面表查询句照旧不得执行（闸不得整体失效）
    q = Plan(intent="TurnDeviceOn", args={"target": []}, source="t0",
             utterance="哪些灯开着")
    assert select_primary_plan(q, None) is None


# ── 项4：不可开关域单一来源 + domains str 形态 ───────────────────
def test_candidate_domains_str_form_and_union_single_source():
    from core import capability
    states = {"light.a": _ent("light.a", "on", "客厅的灯")}
    got = capability.resolve_candidates(
        states, {}, [{"area": "", "devices": [{"name": "灯", "domains": "light"}]}])
    assert [e["entity_id"] for e in got] == ["light.a"], \
        "domains 传字符串被 tuple() 炸成单字符 ⇒ 候选恒空（静默失效）"
    # "空 domains=放行"语义保持
    got2 = capability.resolve_candidates(
        states, {}, [{"area": "", "devices": [{"name": "灯", "domains": ""}]}])
    assert [e["entity_id"] for e in got2] == ["light.a"]
    # 单一来源 + 并集（更严）：capability 表 ∩ executor 表逐位一致
    assert capability.UNTOGGLEABLE_DOMAINS == Executor._UNTOGGLEABLE_DOMAINS, \
        "两份不可开关域表又漂移了"
    assert {"number", "select", "button", "datetime"} <= capability.UNTOGGLEABLE_DOMAINS
    assert {"weather", "person", "calendar", "zone"} <= capability.UNTOGGLEABLE_DOMAINS


def test_turn_gate_blocks_union_domains():
    """预检真跑：并集里两族都拦，media_player/light 照旧放行（不扩大误杀面）。"""
    ex = Executor.__new__(Executor)
    for eid in ("weather.home", "person.zhang", "calendar.jia", "number.deng_liang",
                "select.mo_shi", "button.ping_kai_chuang_kai", "datetime.qi_chuang"):
        assert ex._turn_gate("HassTurnOff", {"entity_id": eid}, "关一下"), eid
    assert ex._turn_gate("HassTurnOff", {"entity_id": "light.a"}, "关灯") is None
    assert ex._turn_gate("HassTurnOff", {"entity_id": "media_player.tv"}, "关电视") is None


# ── 项5：固件仓拒因与 latest 判据 ────────────────────────────────
_FACTORY = b"\xe9" + b"\x00" * 0x1f + b"\x50\x00\x00\x00" + b"x" * 400


def _factory_store(tmp_path):
    """小体积产线合并镜像（容量闸放行、形态闸必拒）——真机 2.1.65 案的小体积形。"""
    lock = _write_lock(tmp_path, [
        {"version": "2.1.99", "file": "huijian-s3-2.1.99.bin", "urls": [],
         "sha256": hashlib.sha256(_FACTORY).hexdigest(), "size": len(_FACTORY),
         "notes_zh": "假装的产线合并镜像（小体积）"}])
    st = fs.FirmwareStore(root=tmp_path / "data", lock_path=lock)
    _drop(st, "huijian-s3-2.1.99.bin", _FACTORY)
    assert st.scan_import() == 1, "夹具没收编进位，后面全是空转"
    return st


def test_capacity_error_covers_image_form_refusal(tmp_path):
    """签发口拒签的**真因**必须能被 API 层拿到（旧实现只查容量闸 ⇒ 面板回"不在盘"）。"""
    st = _factory_store(tmp_path)
    assert st.issue("2.1.99") is None, "形态闸（既有行为）必须先拒"
    why = st.capacity_error("2.1.99")          # 修前：只查容量闸 ⇒ ""（真因被糊成"不在盘"）
    assert "合并出厂镜像" in why, f"小体积合并镜像被拒时仍拿不到真因：{why!r}"
    # 单一来源名（API 层已切换）与历史名逐字同源，不得退化成容量闸子集
    assert st.ota_block_reason("2.1.99") == why


def test_status_latest_same_criteria_as_latest(tmp_path):
    """status()["latest"]（面板数据面）必须与 latest() 同判据：不可 OTA 版本不得当"最新"。"""
    st = _factory_store(tmp_path)
    assert st.latest() is None
    assert st.status()["latest"] == "", f"面板把不可 OTA 版本当最新：{st.status()['latest']!r}"


# ── 项6：ota_api 桥可达判据 + capacity_error 放线程 ──────────────
def test_devices_panel_reports_ha_offline_not_outdated_integration():
    """凭证已配置但 HA 不可达：面板必须说"桥未连接"，不得误报"集成要升级"。"""
    ha = FakeHAClient(rest={})
    ha.ok = True            # 旧判据只看这个（=凭证已配置）
    ha.reachable = False    # 真连通判据
    ctx = AppContext(settings=SettingsFake(), ha=ha, started_at=time.time(),
                     host="10.0.0.9")
    srv = _serve(make_admin_app(ctx))
    port = next(srv)
    try:
        st, j = _jget(port, "/api/devices")
    finally:
        next(srv, None)
    assert st == 200 and j["devices"] == []
    assert "桥未连接" in j["error"], j["error"]
    assert "升级" not in j["error"], "HA 掉线被误报成集成版本旧（.ok ≠ .reachable）"


def test_bridge_requires_credentials_too():
    """反向钉：凭证没配好（`.ok=False`，ha_client 一切请求原地返回）同样算桥不可用
    ——不得只认 reachable 而放行签发（旧钉 test_dispatch_bridge_down_no_issue 的形态）。"""
    ha = FakeHAClient(rest={})
    ha.ok = False
    ha.reachable = True
    ctx = AppContext(settings=SettingsFake(), ha=ha, started_at=time.time(),
                     host="10.0.0.9")
    srv = _serve(make_admin_app(ctx))
    port = next(srv)
    try:
        st, j = _jget(port, "/api/devices")
    finally:
        next(srv, None)
    assert st == 200 and "桥未连接" in j["error"], j["error"]


def test_issue_and_dispatch_refusal_read_off_event_loop(tmp_path, monkeypatch):
    """capacity_error 同步直调＝在 :8000 的事件循环线程上扫盘（同函数其余 6 处都 to_thread）。

    行为判据：`asyncio.run` 在**运行中的 loop 线程**里必抛（⇒ 旧码被 except 折叠成
    "该版本不在盘"）；to_thread 工作线程里则正常返回，真因如实到面板。
    """
    st = _factory_store(tmp_path)

    def _caps(version):
        asyncio.run(asyncio.sleep(0))
        return f"形状闸：v{version} 是产线合并镜像，OTA 只收 app 镜像"

    # 两个名字都替：修前 API 层读 capacity_error、修后读 ota_block_reason（同一洞）
    monkeypatch.setattr(st, "ota_block_reason", _caps)
    monkeypatch.setattr(st, "capacity_error", _caps)
    ctx = AppContext(settings=SettingsFake(), ha=FakeHAClient(rest={}), firmware=st,
                     started_at=time.time(), host="10.0.0.9")
    srv = _serve(make_admin_app(ctx))
    port = next(srv)
    try:
        _s1, j1 = _jpost(port, "/api/firmware/issue", {"version": "2.1.99", "mac": "aa"})
        _s2, j2 = _jpost(port, "/api/firmware/dispatch",
                         {"version": "2.1.99", "mac": "aa"})
    finally:
        next(srv, None)
    assert "合并镜像" in j1.get("error", ""), f"issue 拒因未如实回吐：{j1}"
    assert "合并镜像" in j2.get("error", ""), f"dispatch 拒因未如实回吐：{j2}"


# ── 项7：admin_api 如实回执 + null text ──────────────────────────
class _StoreSingleFlight:
    """model_store.ensure_async 真形态替身：同键活线程在场 ⇒ 静默 return（什么都没做）。"""

    def __init__(self, key, busy):
        self.key = key
        self.calls = []
        self._stop = threading.Event()
        self._threads = {}
        if busy:
            t = threading.Thread(target=self._stop.wait, daemon=True)
            t.start()
            self._threads[key] = t

    def keys(self):
        return [self.key]

    def is_ready(self, k):
        return False

    def snapshot(self):
        return {}

    def ensure_async(self, key, force=False):
        self.calls.append((key, force))
        t = self._threads.get(key)
        if t is not None and t.is_alive():
            return                      # 真实现同款单飞：静默 return
        nt = threading.Thread(target=self._stop.wait, daemon=True)
        nt.start()
        self._threads[key] = nt


def _admin_port(store):
    ctx = AppContext(settings=SettingsFake(), store=store, started_at=time.time(),
                     host="10.0.0.9")
    srv = _serve(make_admin_app(ctx))
    return next(srv), srv


def test_model_download_reports_single_flight_refusal():
    """单飞未受理时不得回 ok:true——「强制重取」被报成已受理＝M11 逃生门被抵消。"""
    key = "asr_paraformer_bilingual"
    store = _StoreSingleFlight(key, busy=True)
    port, srv = _admin_port(store)
    try:
        st, j = _jpost(port, "/api/models/download", {"key": key})
    finally:
        next(srv, None)
        store._stop.set()
    assert store.calls == [(key, True)], "force=True 的强制重取意图必须真传下去（M11）"
    assert not (st == 200 and j.get("ok") is True), f"未受理却回 ok:true：{st} {j}"
    assert j.get("accepted") is False and j.get("message"), j


def test_model_download_accepted_when_idle():
    """反向钉：同键没有在飞任务时必须真受理（不得把逃生门焊死）。"""
    key = "tts_kokoro_multilang"
    store = _StoreSingleFlight(key, busy=False)
    port, srv = _admin_port(store)
    try:
        st, j = _jpost(port, "/api/models/download", {"key": key})
    finally:
        next(srv, None)
        store._stop.set()
    assert st == 200 and j.get("ok") is True and j.get("accepted") is True, (st, j)


class _TtsRec:
    def __init__(self):
        self.texts = []

    def ready(self):
        return True

    async def synthesize_pcm(self, text):
        self.texts.append(text)
        return b"\x00\x01" * 8000


def _raw_post(port, path, body):
    """原样回字节（试听成功支是 WAV，按 JSON 解析会先炸在 aiohttp 里）。"""
    import aiohttp

    async def go():
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://127.0.0.1:{port}{path}", json=body) as r:
                return r.status, await r.read()
    return asyncio.run(go())


def test_tts_test_null_text_not_synthesized_as_none():
    """`{"text": null}` 曾被 `str(body.get("text", 默认))` 派成字面 "None" 去合成。"""
    tts = _TtsRec()
    ctx = AppContext(settings=SettingsFake(), tts=tts, started_at=time.time(),
                     host="10.0.0.9")
    srv = _serve(make_admin_app(ctx))
    port = next(srv)
    try:
        st, raw = _raw_post(port, "/api/tts/test", {"text": None})
    finally:
        next(srv, None)
    assert not tts.texts, f"null 被送去合成了：{tts.texts!r}"
    assert st == 400, f"text=null 应如实拒绝，实得 {st} {raw[:60]!r}"


# ── r2（金标复测 2 轮）：集成 partial_error 必须进话术 ──────────────
def test_partial_error_from_intent_reaches_speech():
    """集成侧把窗侧失败折进 `partial_error` 返回；core 若无消费者，用户永远
    只听「好的，…关了」——本钉要求这部分如实进话术。"""
    ha = _Ha(results={"HassTurnOff": {"success": True,
                                      "partial_error": "另有 1 台没关成功"}})
    plan = Plan(intent="HassTurnOff",
                args={"target": [{"area": "办公室",
                                  "devices": [{"name": "射灯",
                                               "domains": ["light"]}]}]},
                source="t0", utterance="关射灯")
    ex = Executor(ha)
    ok, reply = asyncio.run(ex.run(plan))
    assert ok is True, (ok, reply)
    assert ("另有" in reply) or ("没关成功" in reply), reply
