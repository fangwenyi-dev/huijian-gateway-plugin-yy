"""v1.0.61 审查批：全量缺陷复审确认的 5 项修复钉桩（C1/C2/C3/P3-a/P3-b）。

背景（2026-09-22 全仓复审，病灶均在修复前复现过）：
- C1(P2) LlmSession 抢占孤儿 end：detect 顶替旧回合时，被 cancel 的旧 _turn 在
  except CancelledError 里**无条件**补发 end——客户端 llm_transport.await_message
  以 state=="end" 断流，新回合 start 刚出就被这条孤儿 end 掐死（空/半截答案 +
  后续帧错位）。修复=TtsSession 同款 _gen 代次守卫；on_close 不 bump，断连
  清理路径保持旧行为（cancel 也收 end）。
- C2(P1) 风险确认环三形态旁路（D7：对锁 TurnOff/Toggle=解锁）：旧判据只查
  设备名含「锁」——
  ① klar grounded `HassTurnOff + entity_id=lock.*`（args 只有拼音实体 id、无
     任何中文，与窗户闸门「查不到窗」同病灶）「解锁大门」直通拔锁；
  ② 全屋形 `TurnDeviceOff + devices[].domains=["lock"]`（"关闭所有门锁"，名空）；
  ③ 多步 plan 的 extra_steps 解锁步完全不设防（"关灯并且解锁大门"主步不风险）。
  另 LLM 工具通道 _tool 无风险闸 = 免确认拔锁后门。
  修复=T.args_target_lock 单一判据罩三形态；_risky 整案扫描（含 extra_steps）；
  _confirm_ask 按风险步取目标；agent._tool 拒办并指回本地确认流程。
- C3(P2) settings 嵌套脏节点：{"stt":{"cloud":"x"}} 过 _deep_merge（dict→scalar
  覆写）落盘，旧 _repair_nodes 只查顶层 → masked() 的 c.get AttributeError →
  GET /api/settings 恒 500、Web 设置面板永久打不开（S9 同族，当时只修了
  security 叶子位）。修复=按 DEFAULTS 结构递归核验 + masked 读侧 isinstance 纵深。
- P3-a 指纹包身份：local 指纹只认 sid 数值；Kokoro 换包（v1_0→v1_1 真实发生过：
  同 sid 不同嗓、sid47/52 已移除）不换键=HA 消息哈希盘缓存（无 TTL）永远命中
  旧包音频——speed 漏入（09-21 教训）同族。修复=尾挂 `+m{lock sha256[:8]}`，
  取不到回落 "u"。test_v1048 的本地指纹钉桩同批更新（+mu 尾）。
- P3-b 查询族错标签：("净化器","湿度")→("aqi",) 把 AQI 数值报成「湿度是 35」，
  量纲错标签即假成功。修复=只认真实 humidity 属性，未发布返回 None 让位通用
  湿度传感器分支（宁缺勿错）。
"""
import asyncio
import json
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


class _Settings:
    def __init__(self, data=None):
        self._d = data or {}

    def get(self, path, default=None):
        return self._d.get(path, default)


# ── C1 LlmSession 抢占代次守卫 ─────────────────────────────────
class _WS:
    """aiohttp WS 最小面：closed + send_str（BaseSession.send_json 用）。"""

    def __init__(self):
        self.sent = []
        self.closed = False

    async def send_str(self, data):
        self.sent.append(json.loads(data))


class _Pipe:
    """handle 挂起直到 gate.set；进入即发一句（模拟 P2-15 流式）。"""

    def __init__(self, stream=True):
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.stream = stream

    async def handle(self, text, *, origin=None, on_sentence=None):
        self.started.set()
        if self.stream and on_sentence:
            await on_sentence(f"{text}-半句")
        await self.gate.wait()
        return types.SimpleNamespace(text=f"{text}-全答", streamed=self.stream)


def _llm_sess(ws, pipe):
    import core.session as s
    ctx = types.SimpleNamespace(pipeline=pipe)
    return s.LlmSession(ws, ctx)


def test_llm_session_preemption_no_orphan_end():
    import json as _j

    async def go():
        ws, pipe = _WS(), _Pipe()
        sess = _llm_sess(ws, pipe)
        await sess.on_text(_j.dumps({"type": "listen", "state": "detect",
                                     "text": "旧问题"}, ensure_ascii=False))
        await pipe.started.wait()
        # 抢占：新 detect 顶替（旧回合仍挂在 handle 里）
        await sess.on_text(_j.dumps({"type": "listen", "state": "detect",
                                     "text": "新问题"}, ensure_ascii=False))
        for _ in range(30):
            await asyncio.sleep(0)     # 让旧 task 走完 CancelledError 分支
        states = [m["state"] for m in ws.sent]
        assert "end" not in states, f"旧回合孤儿 end 泄漏，会掐死新回合: {states}"
        assert states.count("start") == 2
        pipe.gate.set()                # 放行新回合
        for _ in range(50):
            await asyncio.sleep(0)
            if any(m["state"] == "end" for m in ws.sent):
                break
        ends = [m for m in ws.sent if m["state"] == "end"]
        assert len(ends) == 1, "全链路只允许新回合自己收的那一条 end"
    _run(go())


def test_llm_session_normal_turn_still_closes():
    # 非抢占正常回合：start→sentences→end 闭环不变（守卫不吃正常路径）。
    import json as _j

    async def go():
        ws, pipe = _WS(), _Pipe(stream=False)
        sess = _llm_sess(ws, pipe)
        await sess.on_text(_j.dumps({"type": "listen", "state": "detect",
                                     "text": "关灯"}, ensure_ascii=False))
        await pipe.started.wait()
        pipe.gate.set()
        for _ in range(50):
            await asyncio.sleep(0)
            if any(m["state"] == "end" for m in ws.sent):
                break
        states = [m["state"] for m in ws.sent]
        assert states[0] == "start" and states[-1] == "end"
        assert states.count("start") == 1 and states.count("end") == 1
    _run(go())


def test_llm_session_close_path_keeps_end_semantics():
    # on_close（close_work 直接 cancel，不 bump gen）：旧行为保持——取消分支
    # 仍发 end（写不进由 send_json 静默兜底）。
    import json as _j

    async def go():
        ws, pipe = _WS(), _Pipe()
        sess = _llm_sess(ws, pipe)
        await sess.on_text(_j.dumps({"type": "listen", "state": "detect",
                                     "text": "空调几度"}, ensure_ascii=False))
        await pipe.started.wait()
        sess.close_work()
        for _ in range(30):
            await asyncio.sleep(0)
        states = [m["state"] for m in ws.sent]
        assert states[-1] == "end"
    _run(go())


# ── C2 风险确认环：三形态 + extra_steps + agent 通道 ───────────
def _pipe(confirm=True):
    from core.pipeline import Pipeline
    p = Pipeline.__new__(Pipeline)
    p.settings = _Settings({"dialog.confirm_risky": confirm})
    p._confirm = {}
    p._origin_ts = {}
    return p


def _plan(intent, args, extra=None):
    from core.nlu.fast_path import Plan
    p = Plan(intent=intent, args=args, source="klar", utterance="解锁大门")
    if extra is not None:
        p.extra_steps = extra
    return p


def test_risky_covers_klar_grounded_lock_entity():
    p = _pipe()
    assert p._risky(_plan("HassTurnOff", {"entity_id": "lock.da_men_suo"}))
    assert p._risky(_plan("HassToggle", {"entity_id": ["light.a", "lock.b"]}))
    assert not p._risky(_plan("HassTurnOff", {"entity_id": "light.a"}))
    # HassTurnOn×lock=上锁（D7 安全向），不在闸内
    assert not p._risky(_plan("HassTurnOn", {"entity_id": "lock.a"}))


def test_risky_covers_wholehouse_lock_domain():
    assert _pipe()._risky(_plan("TurnDeviceOff", {
        "target": [{"devices": [{"name": "", "domains": ["lock"]}]}]}))
    assert _pipe()._risky(_plan("TurnDeviceOff", {
        "target": [{"devices": [{"name": "大门", "domains": ["lock"]}]}]}))
    assert not _pipe()._risky(_plan("TurnDeviceOff", {
        "target": [{"devices": [{"name": "大灯", "domains": ["light"]}]}]}))
    assert _pipe()._risky(_plan("TurnDeviceOff", {     # 原有名含锁判据保持
        "target": [{"devices": [{"name": "门锁"}]}]}))


def test_risky_scans_extra_steps():
    p = _pipe()
    assert p._risky(_plan("HassTurnOff", {"entity_id": "light.ke_ting"}, extra=[
        {"name": "HassUnlock", "args": {"name": ["大门"]}}]))
    assert p._risky(_plan("HassTurnOff", {"entity_id": "light.ke_ting"}, extra=[
        {"name": "HassTurnOff", "args": {"entity_id": "lock.da_men_suo"}}]))
    assert not p._risky(_plan("HassTurnOff", {"entity_id": "light.ke_ting"}, extra=[
        {"name": "HassTurnOn", "args": {"entity_id": "lock.a"}}])   )   # 上锁安全
    assert not p._risky(_plan("HassTurnOn", {"entity_id": "light.a"}, extra=[
        {"name": "HassTurnOff", "args": {"entity_id": "light.b"}}]))


def test_confirm_ask_names_the_risky_step():
    p = _pipe()
    pl = _plan("HassTurnOff", {"entity_id": "light.ke_ting"}, extra=[
        {"name": "HassTurnOff", "args": {"name": ["大门锁"]}}])
    r = p._confirm_ask(pl, "panel")
    assert r is not None and "大门锁" in r.text
    assert "该设备" not in r.text          # 问句按风险步取目标，不指错对象
    assert p._confirm["panel"]["plan"] is pl
    assert _pipe(confirm=False)._confirm_ask(
        _plan("HassUnlock", {}), "panel") is None      # 总开关随配置放行


def test_agent_tool_refuses_unlock_bypass():
    from core.agent import Agent

    async def go():
        a = Agent.__new__(Agent)
        a.settings = _Settings({"dialog.confirm_risky": True})

        async def _never(plan):
            raise AssertionError("锁目标不该走到执行器")
        a.executor = types.SimpleNamespace(run=_never)
        for args in ({"target": [{"devices": [{"name": "大门锁"}]}]},
                     {"target": [{"devices": [{"name": "", "domains": ["lock"]}]}]},
                     {"name": "大门锁"}):
            ok, say = await a._tool("TurnDeviceOff", args)
            assert ok is False and "解锁" in say, args
        # 同开关=false：用户明示免确认 → 放行（与 pipeline._risky 同一开关同一语义）
        a.settings = _Settings({"dialog.confirm_risky": False})

        async def _ok(plan):
            return True, "办了"
        a.executor = types.SimpleNamespace(run=_ok)
        ok, say = await a._tool("TurnDeviceOff", {"name": "大门锁"})
        assert ok is True and say == "办了"
    _run(go())


# ── C3 settings 递归修复 ───────────────────────────────────────
def test_settings_nested_dirty_cloud_node_no_500(tmp_path):
    # 病灶原样复现：{"stt":{"cloud":"x"}} 落盘。旧 _repair_nodes 只查顶层，
    # masked() 里 c.get AttributeError → GET /api/settings 恒 500。
    from core.settings import Settings
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"stt": {"cloud": "garbage"},
                             "tts": {"cloud": {"api_key": "sk-live"}}}),
                 encoding="utf-8")
    st = Settings(f)
    m = st.masked()                       # 修复前：这里直接抛 AttributeError
    assert isinstance(m["stt"]["cloud"], dict)
    assert m["tts"]["cloud"]["api_key"] == "****"     # 干净子树原样脱敏


def test_settings_update_nested_dirty_keeps_service(tmp_path):
    from core.settings import Settings
    st = Settings(tmp_path / "s.json")
    st.update({"stt": {"cloud": "garbage"}})          # 写侧脏值落盘
    assert isinstance(st.masked()["stt"]["cloud"], dict)   # 读侧仍可用（递归修复）
    assert isinstance(st.get("stt"), dict)            # 不崩即达标；脏节点回默认


def test_settings_top_level_dirty_still_repaired(tmp_path):
    # v1.0.40 A4 原语义不回退：顶层非 dict 节点照样恢复默认。
    from core.settings import Settings
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"security": None}), encoding="utf-8")
    st = Settings(f)
    assert isinstance(st.get("security"), dict)


# ── P3-a 指纹含模型包身份 ──────────────────────────────────────
def _eng(store, sid=18):
    import core.tts as m
    return m.TtsEngine(_Settings({"tts.provider": "local_kokoro",
                                  "tts.sid": sid}), store)


def test_fingerprint_rotates_with_model_package():
    e1 = _eng(types.SimpleNamespace(lock_entry=lambda k: {"sha256": "a" * 64}))
    e2 = _eng(types.SimpleNamespace(lock_entry=lambda k: {"sha256": "b" * 64}))
    e3 = _eng(types.SimpleNamespace())               # 假件/无 lock_entry → u
    e4 = _eng(types.SimpleNamespace(lock_entry=lambda k: {}))
    assert e1.voice_fingerprint() == "local:sid18+c0+s1+maaaaaaaa"
    assert e1.voice_fingerprint() != e2.voice_fingerprint()   # 换包必换键
    assert e3.voice_fingerprint().endswith("+mu")    # 取不到：行为确定回落
    assert e4.voice_fingerprint().endswith("+mu")


def test_fingerprint_ships_real_lock_identity():
    # 真 models.lock.json 上「包身份」链路接通（不是回落 u）；sha 不锁具体值，
    # 升级换包属**预期轮换**（钉的是链路，不是快照）。
    from core.model_store import ModelStore
    store = ModelStore(_Settings(), models_dir=Path("/tmp/hj_v1061_m"),
                       status_file=Path("/tmp/hj_v1061_s.status"))
    sha = store.lock_entry("tts_kokoro_multilang").get("sha256")
    assert sha and len(sha) == 64
    assert _eng(store).voice_fingerprint() == f"local:sid18+c0+s1+m{sha[:8]}"


# ── P3-b 查询族不再把 AQI 报成湿度 ─────────────────────────────
class _Ha:
    def __init__(self, ents):
        self._ents = ents

    async def find_entities(self, area="", domains=(), name_contains=""):
        return [e for e in self._ents
                if not domains or e["entity_id"].split(".", 1)[0] in domains]


def _qa(ents):
    from core.nlu.query import QueryZone
    return QueryZone(_Ha(ents), _Settings())


def test_purifier_humidity_answers_real_humidity_only():
    q = _qa([{"entity_id": "fan.p", "attributes": {
        "friendly_name": "净化器", "aqi": 35, "humidity": 55}}])
    ans = _run(q._attr_answer("", "净化器", "湿度"))
    assert ans and "55" in ans and "35" not in ans   # 只报真湿度，不冠名错位
    q2 = _qa([{"entity_id": "fan.p", "attributes": {
        "friendly_name": "净化器", "aqi": 35}}])      # 无 humidity 属性
    assert _run(q2._attr_answer("", "净化器", "湿度")) is None
    # ↑ 让位下方通用湿度传感器分支=宁缺勿错；档位映射不受牵连
    q3 = _qa([{"entity_id": "fan.p", "attributes": {
        "friendly_name": "净化器", "fan_speed": 2}}])
    assert _run(q3._attr_answer("", "净化器", "档位")) is not None
