"""klar 一级确定性 NLU 回归（v1.0.8 架构扩展）。

钉死四件事：
① 裁决纪律：只有 decision=execute + 控制族白名单全命中 + 置信过门才接管；
   clarify/confirm/reject/chat、查询/媒体/日历越权、多分句夹生 → 一律 None
   （宁漏勿错，留给 TextCNN/查询族/LLM 既有链）。
② fail-open：引擎缺席/非 200/形制漂移 → match 恒 None 不抛；5 连败熔断
   300s，熔断期连 socket 都不碰（每句零延迟代价）。
③ 级联仲裁（v1.0.9 三层定位）：scene 契约 > 慧尖独占（窗/模式/调节/场景
   自动化管理/目标含窗）> klar 标准控制 > 字面表剩余；执行期两路互为降级，
   仍失败且用户配置了 LLM 才复议（没配 = 无视）。
④ 基建形状：boot 分发（sha256 核验）、s6 服务只绑回环、Dockerfile 装配、
   settings 默认开——任何一环被重构悄悄拆掉都要红。
"""
import asyncio
import pathlib

import pytest

from core.executor import Executor
from core.nlu.fast_path import Plan
from core.nlu.klar_client import KLAR_CONTROL_INTENTS, KlarClient
from core.pipeline import select_fallback_plan, select_primary_plan

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class DictSettings:
    BASE = {
        "klar.enabled": True, "klar.url": "http://klar.test:10520",
        "klar.language": "zh-CN", "klar.timeout_s": 2.0,
        "klar.min_confidence": 0.80, "klar.token": "",
    }

    def __init__(self, **over):
        self.d = dict(self.BASE, **over)

    def get(self, key, default=None):
        return self.d.get(key, default)


class FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._payload


class FakeSession:
    """记录调用次数的假 aiohttp session；按脚本队列返回/抛异常。"""

    def __init__(self, script=None):
        self.calls = []
        self.script = list(script or [])

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        item = self.script.pop(0) if self.script else (404, None)
        if isinstance(item, Exception):
            raise item
        status, payload = item
        return FakeResp(status, payload)

    async def close(self):
        pass


def execute_payload(intents, conf=0.93, speech="好的，办公室射灯打开了", decision="execute"):
    """intents: [(name, {slot:val}), ...] → 引擎 execute 响应形制。"""
    steps = [{"intent": {"name": n,
                         "slots": [{"name": k, "value": v} for k, v in s.items()]}}
             for n, s in intents]
    obj = {"decision": {"type": decision}, "confidence": conf}
    if decision == "execute":
        obj["plan"] = {"steps": steps, "confidence": conf}
        obj["speech"] = speech
    return obj


# ── ① 裁决纪律 ─────────────────────────────────────────────────
def test_execute_control_intent_becomes_plan():
    c = KlarClient(DictSettings())
    obj = execute_payload([("HassTurnOn", {"area": "办公室", "domain": "light",
                                           "name": ["射灯"]})])
    p = c.to_plan(obj, "打开办公室的射灯")
    assert p is not None and p.source == "klar" and p.intent == "HassTurnOn"
    assert p.args == {"area": "办公室", "domain": "light", "name": ["射灯"]}
    assert p.speech == "好的，办公室射灯打开了"
    assert p.extra_steps == [] and p.utterance == "打开办公室的射灯"
    assert any("conf=0.93" in t for t in p.trace)


@pytest.mark.parametrize("decision", ["clarify", "confirm", "reject", "chat", "error"])
def test_non_execute_never_takes_over(decision):
    c = KlarClient(DictSettings())
    obj = {"decision": {"type": decision}, "speech": "…"}
    assert c.to_plan(obj, "随便") is None


def test_out_of_whitelist_goes_back_to_cascade():
    """查询/媒体/计时/日历放行——不许抢既有 ⑤查询族/LLM 的活。"""
    c = KlarClient(DictSettings())
    for name in ("HassGetState", "HassMediaPause", "HassTimerStart",
                 "KlarCreateCalendarEvent", "HassNeverHeardOfIt"):
        assert name not in KLAR_CONTROL_INTENTS
        obj = execute_payload([(name, {})])
        assert c.to_plan(obj, "客厅多少度") is None, name


def test_multi_clause_all_or_nothing():
    c = KlarClient(DictSettings())
    obj = execute_payload([("HassTurnOff", {"name": ["灯"]}),
                           ("HassLock", {"name": ["门锁"]})])
    p = c.to_plan(obj, "关灯并且锁上门")
    assert p is not None and p.intent == "HassTurnOff"
    assert p.extra_steps == [{"name": "HassLock", "args": {"name": ["门锁"]}}]
    obj2 = execute_payload([("HassTurnOff", {"name": ["灯"]}),
                            ("HassGetState", {"name": ["空调"]})])
    assert c.to_plan(obj2, "关灯并且看下空调状态") is None


def test_confidence_gate():
    c = KlarClient(DictSettings())
    assert c.to_plan(execute_payload([("HassTurnOn", {})], conf=0.55), "开灯") is None
    lo = KlarClient(DictSettings(**{"klar.min_confidence": 0.5}))
    assert lo.to_plan(execute_payload([("HassTurnOn", {})], conf=0.55), "开灯") is not None


def test_slot_shape_tolerance():
    c = KlarClient(DictSettings())
    obj = {"decision": {"type": "execute"}, "plan": {"steps": [
        {"intent": {"name": "HassTurnOn", "slots": [
            {"name": "domain", "value": "light"},
            {"value": None},
            {"name": "bogus", "value": None}]}}, {"intent": {}}],
        "confidence": 0.9}, "speech": "好的"}
    # 第二步无 intent → 白名单全命中原则下整句不接管（宁漏勿错）
    assert c.to_plan(obj, "开灯") is None
    obj["plan"]["steps"] = obj["plan"]["steps"][:1]
    p = c.to_plan(obj, "开灯")
    assert p is not None and p.args == {"domain": "light", "bogus": None}


# ── ② fail-open / 熔断 ─────────────────────────────────────────
def test_disabled_makes_zero_calls():
    s = FakeSession()
    c = KlarClient(DictSettings(**{"klar.enabled": False}), session=s)
    assert arun(c.match("开灯")) is None
    assert s.calls == []


def test_http_200_roundtrip_flows_into_plan():
    s = FakeSession(script=[(200, execute_payload([("HassTurnOn", {"domain": "light"})]))])
    c = KlarClient(DictSettings(), session=s)
    p = arun(c.match("开灯"))
    assert p is not None and p.intent == "HassTurnOn"
    assert s.calls[0]["url"].endswith("/api/v2/parse")
    assert s.calls[0]["json"]["language"] == "zh-CN"


def test_non_200_and_transport_errors_fold_to_none():
    for st in (500, 404, 401):
        c = KlarClient(DictSettings(), session=FakeSession(script=[(st, None)]))
        assert arun(c.match("开灯")) is None
    c = KlarClient(DictSettings(), session=FakeSession(script=[ConnectionError("refused")]))
    assert arun(c.match("开灯")) is None
    c = KlarClient(DictSettings(), session=FakeSession(script=[(200, "not-a-dict")]))
    assert arun(c.match("开灯")) is None


def test_circuit_breaker_opens_after_five_failures():
    s = FakeSession(script=[ConnectionError("boom")] * 6)
    c = KlarClient(DictSettings(), session=s)
    for _ in range(5):
        assert arun(c.match("开灯")) is None
    assert len(s.calls) == 5
    assert arun(c.match("开灯")) is None       # 第 6 句：熔断期，零外拨
    assert len(s.calls) == 5
    c._cooldown_until = 0.0                     # 模拟熔度过期
    s.script = [(200, execute_payload([("HassTurnOn", {"domain": "light"})]))]
    assert arun(c.match("开灯")) is not None    # 半开探测成功 → 计数清零
    assert c._fails == 0


def test_token_header_when_configured():
    s = FakeSession(script=[(200, execute_payload([("HassTurnOn", {})]))])
    c = KlarClient(DictSettings(**{"klar.token": "sekret"}), session=s)
    arun(c.match("开灯"))
    assert s.calls[0]["headers"] == {"x-klar-token": "sekret"}


# ── ③ 级联仲裁（v1.0.9 三层定位）────────────────────────────────
def _plan(source, intent="X", args=None):
    return Plan(intent=intent, args=args or {}, source=source)


def _std_on(area="办公室", name="射灯"):
    return {"target": [{"area": area,
                        "devices": [{"name": name, "domains": ["light"]}]}]}


def test_scene_contract_beats_klar():
    fp, kl = _plan("scene", "HassTriggerVoiceScene"), _plan("klar", "HassTurnOn")
    assert select_primary_plan(fp, kl) is fp


def test_huijian_only_intents_keep_literal():
    for it in ("ControlWindow", "SetDeviceMode", "AdjustDeviceAttribute",
               "HassCreateVoiceScene", "HassListAutomations", "HuijianGetLiveContext"):
        fp, kl = _plan("t0", it), _plan("klar", "HassTurnOn")
        assert select_primary_plan(fp, kl) is fp, it


def test_window_device_names_keep_literal():
    kl = _plan("klar", "HassTurnOn")
    for nm in ("书房窗户", "客厅天窗", "内倒窗", "开合器", "推拉门"):
        fp = _plan("t0_prefix", "TurnDeviceOff", _std_on("书房", nm))
        assert select_primary_plan(fp, kl) is fp, nm


def test_curtain_is_standard_cover_klar_wins():
    fp = _plan("t0", "TurnDeviceOn", _std_on("客厅", "窗帘"))
    kl = _plan("klar", "HassTurnOn")
    # 窗帘=标准 cover：klar 命中即接管（用户指令②）
    assert select_primary_plan(fp, kl) is kl


def test_standard_control_klar_first():
    # 用户指令②：t0 字面表的标准设备句，klar 引擎同句命中时让位——
    # grounded entity_id 直调服务，不依赖慧尖集成（旧契约「字面表恒先」反转）
    fp = _plan("t0", "TurnDeviceOn", _std_on())
    kl = _plan("klar", "HassTurnOn")
    assert select_primary_plan(fp, kl) is kl


def test_klar_beats_textcnn():
    fp, kl = _plan("t1"), _plan("klar", "HassTurnOn")
    assert select_primary_plan(fp, kl) is kl


def test_fallbacks_untouched():
    assert select_primary_plan(None, None) is None
    assert select_primary_plan(_plan("t1"), None).source == "t1"
    assert select_primary_plan(None, _plan("klar")).source == "klar"
    assert select_primary_plan(_plan("t0", "TurnDeviceOn", _std_on()), None).source == "t0"


# ── ③b 执行期降级（指令①：集成没加载 → klar 直调兜底；klar 挂 → 回退字面表）─
def test_fallback_huijian_fail_goes_klar():
    fp = _plan("t0", "AdjustDeviceAttribute", {"attribute": "brightness", "delta": "60"})
    kl = _plan("klar", "HassLightSet")
    assert select_fallback_plan(fp, fp, kl, "抱歉，慧尖 AI 集成还没生效") is kl
    assert select_fallback_plan(fp, fp, kl, "抱歉，没找到符合条件的设备") is kl


def test_fallback_klar_fail_returns_to_literal():
    fp = _plan("t0", "TurnDeviceOn", _std_on())
    kl = _plan("klar", "HassTurnOn")
    assert select_fallback_plan(kl, fp, kl, "抱歉，设备清单里没匹配到") is fp


def test_fallback_scene_never_becomes_second_path():
    fp = _plan("scene", "HassTriggerVoiceScene")
    kl = _plan("klar", "HassTurnOn")
    assert select_fallback_plan(kl, fp, kl, "抱歉") is None
    assert select_fallback_plan(fp, fp, None, "抱歉") is None


def test_fallback_none_primary():
    assert select_fallback_plan(None, None, None, "") is None


# ── v1.0.12 窗户误动作闸（实机：ControlWindow 挂→klar 兜底点亮办公室灯）──
_WIN_ARGS = {"target": [{"area": "办公室",
                           "devices": [{"name": "平开窗", "domains": []}]}],
             "action": "open"}


def test_fallback_window_never_hits_lights():
    fp = _plan("t0", "ControlWindow", _WIN_ARGS)
    lamp = _plan("klar", "HassTurnOn", _std_on())
    assert select_fallback_plan(fp, fp, lamp, "抱歉，慧尖 AI 集成还没生效") is None
    # 主计划即便不带 ControlWindow 名（字面表其它窗句），args 含窗类词同样受闸
    other = _plan("t1", "SetDeviceMode", {"name": "内倒窗", "mode": "通风"})
    assert select_fallback_plan(other, other, lamp, "抱歉") is None


def test_fallback_window_to_window_entity_allowed():
    # klar 兜底若真命中窗类实体（开合器以 cover 暴露且名含窗型），放行不误伤
    fp = _plan("t0", "ControlWindow", _WIN_ARGS)
    win_kl = _plan("klar", "HassTurnOn", _std_on(name="办公室平开窗"))
    assert select_fallback_plan(fp, fp, win_kl, "抱歉") is win_kl


def test_fallback_curtain_unaffected_by_window_gate():
    # 窗帘=标准 cover（v1.0.9 契约，不进 ControlWindow 路径）：目标词被
    # _mentions_window_device 剔除 → 闸不拦，klar 兜底合法，行为与 v1.0.11 一致
    curtain = _plan("t0", "TurnDeviceOn", _std_on(name="客厅窗帘"))
    kl = _plan("klar", "HassTurnOn", _std_on(name="客厅窗帘"))
    assert select_fallback_plan(curtain, curtain, kl, "抱歉") is kl


# ── ③c _cascade 行为（降级链 + LLM 复议次序）────────────────────
class SeqExecutor:
    def __init__(self, results):        # plan.source → (ok, speech)
        self.results, self.order = results, []

    async def run(self, plan):
        self.order.append(plan.source)
        return self.results.get(plan.source, (True, "好的"))


class FakeAgent:
    enabled = True

    async def answer(self, text, hist):
        yield "LLM 复议结果"


def _mk_pipe(fp_plan, kl_plan, ex, agent=None):
    from core.pipeline import Pipeline
    p = Pipeline.__new__(Pipeline)
    class _M:
        def __init__(self, pl): self.pl = pl
        async def match(self, t): return self.pl
    class _Q:
        async def answer(self, t): return None
    p.fast_path, p.klar, p.executor, p.agent, p.query = _M(fp_plan), _M(kl_plan), ex, agent, _Q()
    return p


def test_cascade_standard_goes_klar_directly():
    fp = _plan("t0", "TurnDeviceOn", _std_on())
    kl = _plan("klar", "HassTurnOn")
    ex = SeqExecutor({"klar": (True, "好的，开了")})
    r = arun(_mk_pipe(fp, kl, ex)._cascade("打开办公室射灯"))
    assert r.ok and r.source == "klar" and ex.order == ["klar"]


def test_cascade_huijian_only_fail_falls_to_klar():
    fp = _plan("t0", "AdjustDeviceAttribute", {"attribute": "brightness"})
    kl = _plan("klar", "HassLightSet")
    ex = SeqExecutor({"t0": (False, "抱歉，慧尖 AI 集成还没生效——请安装集成"),
                      "klar": (True, "好的，亮度60%了")})
    r = arun(_mk_pipe(fp, kl, ex)._cascade("亮度调到60"))
    assert r.ok and r.source == "klar"
    assert ex.order == ["t0", "klar"] and any("降级→klar" in t for t in r.trace)


def test_cascade_both_fail_picks_integration_speech():
    kl = _plan("klar", "HassTurnOn")
    fp = _plan("t0", "TurnDeviceOn", _std_on())
    ex = SeqExecutor({"klar": (False, "抱歉，设备清单里没匹配到，请换个叫法试试"),
                      "t0": (False, "抱歉，慧尖 AI 集成还没生效——请安装集成")})
    r = arun(_mk_pipe(fp, kl, ex)._cascade("打开办公室射灯"))
    assert not r.ok and "集成还没生效" in r.text and r.source == "klar"
    assert any("降级→t0:TurnDeviceOn✗" in t for t in r.trace)


def test_cascade_llm_deliberates_after_no_fallback():
    fp = _plan("t1", "TurnDeviceOn", _std_on())
    ex = SeqExecutor({"t1": (False, "抱歉，慧尖 AI 集成还没生效")})
    r = arun(_mk_pipe(fp, None, ex, agent=FakeAgent())._cascade("打开灯"))
    assert r.ok and r.source == "llm" and r.text == "LLM 复议结果"
    assert ex.order == ["t1"]           # klar 无命中 → 无处降级 → LLM（指令③）


# ── ④ 执行层（多分句 + klar 播报优先）──────────────────────────
class FakeHA:
    def __init__(self, results):
        self.results = list(results)
        self.seen = []

    async def handle_intent(self, name, args):
        self.seen.append((name, args))
        return self.results.pop(0)


def test_multistep_sequential_and_klar_speech():
    ha = FakeHA([{"success": True}, {"success": True}])
    ex = Executor(ha)
    p = Plan(intent="HassTurnOff", args={}, source="klar",
             speech="都办好了", extra_steps=[{"name": "HassLock", "args": {"n": 1}}])
    ok, reply = arun(ex.run(p))
    assert ok and reply == "都办好了"
    assert ha.seen == [("HassTurnOff", {}), ("HassLock", {"n": 1})]


def test_multistep_second_step_failure_reports_error():
    ha = FakeHA([{"success": True}, {"success": False, "message": "no match found"}])
    ex = Executor(ha)
    p = Plan(intent="HassTurnOn", args={}, source="klar", speech="好的",
             extra_steps=[{"name": "HassLock", "args": {}}])
    ok, reply = arun(ex.run(p))
    assert not ok and reply.startswith("抱歉")
    assert "没找到符合条件的设备" in reply   # 旧键 "no.*match" 是死键（子串匹配），本次修复


def test_single_step_without_speech_uses_talk_layer():
    ha = FakeHA([{"success": True, "control_targets": [{"name": "射灯", "area": "办公室"}]}])
    ex = Executor(ha)
    p = Plan(intent="TurnDeviceOn", args={}, source="t1")
    ok, reply = arun(ex.run(p))
    assert ok and "射灯" in reply


# ── ⑤ 基建形状钉桩 ─────────────────────────────────────────────
def _text(rel):
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_boot_provisions_engine_with_sha256():
    b = _text("boot.sh")
    assert "klar-linux-x86_64.tar.gz" in b and "klar-linux-aarch64.tar.gz" in b
    assert "sha256sum -c" in b and "sha256:" in b            # 有 digest 才装
    assert "/data/klar/klar" in b and ".version" in b         # 版本戳幂等
    assert "降级为本地 TextCNN" in b                          # 失败路径只警告不炸 boot


def test_service_binds_loopback_only():
    s = _text("klar-engine.sh")
    assert "127.0.0.1:10520" in s and "0.0.0.0" not in s
    assert "--config-dir /homeassistant" in s                # 直读 .storage，零推送
    assert "/data/klar-data" in s and "exec /data/klar/klar" in s


def test_dockerfile_wires_third_service():
    d = _text("Dockerfile")
    assert "COPY klar-engine.sh /etc/services.d/klar-engine/run" in d
    assert "/etc/services.d/klar-engine/run" in d.split("RUN chmod +x")[1]


def test_settings_defaults_have_klar_section():
    st = _text("core/settings.py")
    for key in ('"enabled": True', '"url": "http://127.0.0.1:10520"',
                '"language": "zh-CN"', '"min_confidence": 0.80'):
        assert key in st


def test_pipeline_dispatch_wired():
    pl = _text("core/pipeline.py")
    assert "from .nlu.klar_client import KlarClient" in pl
    assert "select_primary_plan(fp_plan, kl_plan)" in pl
    mn = _text("core/main.py")
    assert "self.klar = KlarClient(self.settings)" in mn and "klar=self.klar" in mn
    assert "await self.klar.close()" in mn


# ── ⑥ grounded 直调服务（entity_id 已解析 → 绕开 intent handler）──
class SpyHA(FakeHA):
    def __init__(self, results=None):
        super().__init__(results or [])
        self.services = []

    async def call_service(self, domain, service, data, timeout=10.0):
        self.services.append((domain, service, data))
        return {"success": True, "message": "", "raw": "[]"}


def _klar_plan(intent, args):
    return Plan(intent=intent, args=args, source="klar")


def test_entity_id_step_calls_service_not_intent():
    ha = SpyHA()
    ex = Executor(ha)
    ok, reply = arun(ex.run(_klar_plan("HassTurnOn", {
        "entity_id": "light.she_deng", "domain": "light", "area": "办公室"})))
    assert ok
    assert ha.services == [("homeassistant", "turn_on", {"entity_id": "light.she_deng"})]
    assert ha.seen == [] and reply


def test_area_only_step_still_uses_intent_channel():
    ha = SpyHA([{"success": True}])
    ex = Executor(ha)
    ok, _ = arun(ex.run(_klar_plan("HassTurnOn", {"area": "办公室", "domain": "light"})))
    assert ok and ha.services == []
    assert ha.seen == [("HassTurnOn", {"area": "办公室", "domain": "light"})]


def test_lock_turn_on_is_lock_per_d7():
    ha = SpyHA()
    arun(Executor(ha).run(_klar_plan("HassTurnOn", {"entity_id": "lock.men_suo"})))
    assert ha.services == [("lock", "lock", {"entity_id": "lock.men_suo"})]
    ha2 = SpyHA()
    arun(Executor(ha2).run(_klar_plan("HassTurnOff", {"entity_id": "lock.men_suo"})))
    assert ha2.services == [("lock", "unlock", {"entity_id": "lock.men_suo"})]


def test_light_set_maps_only_legal_keys():
    ha = SpyHA()
    ex = Executor(ha)
    arun(ex.run(_klar_plan("HassLightSet", {
        "entity_id": "light.x", "brightness": 50, "color": "红色",
        "domain": "light", "garbage": "x"})))
    d, s, data = ha.services[0]
    assert (d, s) == ("light", "turn_on")
    assert data == {"entity_id": "light.x", "brightness": 50, "color_name": "红色"}


def test_list_entity_id_passthrough_and_no_table_intent():
    ha = SpyHA([{"success": True}])
    ex = Executor(ha)
    arun(ex.run(_klar_plan("HassTurnOff", {"entity_id": ["light.a", "light.b"]})))
    assert ha.services == [("homeassistant", "turn_off",
                            {"entity_id": ["light.a", "light.b"]})]
    assert ex._klar_direct("HassUnknownThing", {"entity_id": "light.a"}) is None


def test_vacuum_start_minimal_payload():
    ha = SpyHA()
    arun(Executor(ha).run(_klar_plan(
        "HassVacuumStart", {"entity_id": "vacuum.tuo", "domain": "vacuum"})))
    assert ha.services == [("vacuum", "start", {"entity_id": "vacuum.tuo"})]


def test_direct_step_failure_zh_errors():
    class FailHA(SpyHA):
        async def call_service(self, domain, service, data, timeout=10.0):
            return {"success": False, "message": "Unable to find service"}
    ok, reply = arun(Executor(FailHA()).run(_klar_plan(
        "HassTurnOn", {"entity_id": "light.gone"})))
    assert not ok and reply.startswith("抱歉")


def test_non_klar_source_never_direct():
    ha = SpyHA([{"success": True}])
    ex = Executor(ha)
    p = Plan(intent="HassTurnOn", args={"entity_id": "light.x"}, source="t1")
    arun(ex.run(p))
    assert ha.services == [] and len(ha.seen) == 1


def test_call_service_method_present():
    hc = _text("core/ha_client.py")
    assert "async def call_service" in hc and "/api/services/" in hc


# ── ⑦ 失败话术归属（2026-09-08 实机误报：Supervisor 5xx 被播成「集成没生效」）─
class Proxy500HA(SpyHA):
    async def call_service(self, domain, service, data, timeout=10.0):
        return {"success": False, "message": "HA 内部错误(500)", "raw": None}

    async def handle_intent(self, name, data):
        return {"success": False, "message": "HA 内部错误(500)", "raw": None}


def test_klar_channel_never_blames_integration():
    ok, reply = arun(Executor(Proxy500HA()).run(_klar_plan(
        "HassTurnOn", {"entity_id": "light.ban_gong_shi_she_deng"})))
    assert not ok
    assert "集成" not in reply, reply
    assert "没有走通" in reply


def test_intent_channel_keeps_integration_speech():
    ok, reply = arun(Executor(Proxy500HA()).run(
        Plan(intent="TurnDeviceOn", args={}, source="t0")))
    assert not ok and "集成还没生效" in reply


def test_panel_execute_runs_full_cascade():
    aa = _text("core/admin_api.py")
    assert "ctx.pipeline._cascade(text)" in aa
    assert "plan = await ctx.pipeline.fast_path.match(text)" not in aa
