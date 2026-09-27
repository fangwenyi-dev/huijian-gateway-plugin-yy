"""v1.1.15 钉桩（办公 .91 实锤 D6）：链里每一步按**本步来源**选外发通道。

现场（板 32b8 / 加载项 1.1.14 / SenseVoice，HA .91）：
    [级联] '打开办公室平台窗关闭办公室射灯' → [chain] '好的，都办妥了'
    [执行] ControlWindow {'target': [{'area': '办公室', 'devices': [{'name': '平开窗',
           'domains': ['button', 'cover', 'number']}]}], 'action': 'open'}(+1步) → 成功
HA history 硬证：cover.ban_gong_shi_ping_kai_chuang_kai_chuang_qi 14:05:28 open→closed
（首腿真动），light.ban_gong_shi_she_deng 自 14:00:31 起零状态变化（次腿没落地）。

病灶：链的 `source` 取自**首分句**的裁决（pipeline._try_compound 的 merged Plan），
执行层却拿它当整链口径——`_klar_direct(...) if plan.source == "klar"`。而分句是逐路
裁决的：窗户句恒由字面表胜出（`HUIJIAN_ONLY_INTENTS` 不含 Turn*，见
select_primary_plan），灯句通常让 klar 胜（引擎 full 模式已把「办公室射灯」grounded
成 entity_id）。于是混形链的第二腿带着 entity_id 被原样丢进 /api/intent/handle——
`HassTurnOn/HassTurnOff` 根本不在慧尖集成注册面内（custom_components/huijian_ai/
intent.py 只登记 TurnDevice*/PauseDevice/SetDeviceMode/AdjustDeviceAttribute/
ControlWindow/HassLock/HassUnlock/场景/自动化），HA 内置 handler 又不吃这个形制
⇒ 第二腿"发出去了但没人按它动"，顶层 success 照收，账上报"都办妥了"。

纪律：反向钉同样重要——**不得**把直调权扩给非 klar 来源的步骤（LLM 工具通道可吐
任意 entity_id，D7 锁语义下"直调"= 少一道确认环的可达面，见 agent._tool）。
"""
import asyncio
import json

from conftest import FakeHAClient
from core.executor import Executor
from core.nlu.fast_path import Plan

from test_experience_batch import Lane, PSettings, RecExecutor, _pipe, _p

WIN_ARGS = {"target": [{"area": "办公室",
                        "devices": [{"name": "平开窗",
                                     "domains": ["button", "cover", "number"]}]}],
            "action": "open"}
LAMPER = {"entity_id": "light.ban_gong_shi_she_deng", "domain": "light", "area": "办公室"}


class Ha(FakeHAClient):
    """两条外发通道都记账：intent 通道走 FakeHAClient.calls，直调走 svc_calls。

    `svc_results` 按 "domain.service" 注入回执——修好后第二腿走的是直调通道，
    只配 `results=`（intent 侧）就永远测不到它的失败支。"""

    def __init__(self, svc_results=None, **kw):
        super().__init__(**kw)
        self.svc_calls = []
        self._svc_results = svc_results or {}

    async def call_service(self, domain, service, data, timeout=10.0):
        self.svc_calls.append((domain, service, data))
        return self._svc_results.get(f"{domain}.{service}", {"success": True})


def _chain(first_intent, first_args, first_source, leg2,
           utterance="打开办公室平开窗关闭办公室射灯"):
    return Plan(intent=first_intent, args=first_args, source=first_source,
                utterance=utterance, extra_steps=[leg2])


def _run(plan, results=None, svc_results=None):
    # 快照空＝能力/可用态/真伪各闸一律放行（判不了就不判），本文件只看通道
    ha = Ha(results=results or {}, states={}, svc_results=svc_results)
    return asyncio.run(Executor(ha).run(plan)), ha


def test_mixed_chain_second_leg_still_gets_direct_call():
    """修案主钉：慧尖首腿 + klar 次腿 ⇒ 次腿必须走 /api/services/*（旧代码必红）。"""
    (ok, _), ha = _run(_chain("ControlWindow", WIN_ARGS, "t0",
                              {"name": "HassTurnOff", "args": dict(LAMPER), "source": "klar"}))
    assert ok is True
    assert ha.svc_calls == [("homeassistant", "turn_off",
                             {"entity_id": "light.ban_gong_shi_she_deng"})], ha.svc_calls
    assert [c[0] for c in ha.calls] == ["ControlWindow"], ha.calls


def test_klar_first_huijian_second_stays_on_intent_channel():
    """反向：慧尖自有意图没有服务映射，即便整链来源是 klar 也只能走 intent 通道。

    句面刻意不带"窗"字——带窗时第二腿的 TurnDeviceOff 会先被 v1.0.69 开关族能力闸
    整链拦下（那是 _turn_gate 的行为），本钉要看的是通道选择。"""
    (ok, _), ha = _run(_chain("HassTurnOff", dict(LAMPER), "klar",
                              {"name": "TurnDeviceOff",
                               "args": {"target": [{"area": "办公室",
                                                    "devices": [{"name": "台灯",
                                                                 "domains": ["light"]}]}]},
                               "source": "t0"},
                              utterance="关闭办公室射灯和打开办公室台灯"),
                       results={"TurnDeviceOff": {"success": True,
                                                  "control_targets": [{"name": "台灯",
                                                                       "area": "办公室"}]}})
    assert ok is True
    assert ha.svc_calls and ha.svc_calls[0][0] == "homeassistant"   # 只有首腿直调
    assert [c[0] for c in ha.calls] == ["TurnDeviceOff"], ha.calls


def test_llm_channel_does_not_gain_direct_call():
    """反向（防回潮）：非 klar 来源的 entity_id 步骤绝不因本改动获得直调权。

    LLM 工具通道 `Plan(intent=name, args=args, source="llm")` 可吐任意 JSON；
    把直调按"整链来源"放开等于给 D7 锁语义多开一条免确认可达路径。
    """
    (ok, _), ha = _run(Plan(intent="HassTurnOff", args=dict(LAMPER), source="llm",
                            utterance="关闭办公室射灯"))
    assert ok is True
    assert ha.svc_calls == [], ha.svc_calls
    assert [c[0] for c in ha.calls] == ["HassTurnOff"], ha.calls


def test_legless_source_falls_back_to_plan_source():
    """老形制兼容：extra_steps 项不带 source（他处装配/历史夹具）⇒ 退回整链口径。"""
    (ok, _), ha = _run(_chain("HassTurnOff", dict(LAMPER), "klar",
                              {"name": "HassTurnOn",
                               "args": {"entity_id": "light.desk"}}))
    assert ok is True
    assert ("homeassistant", "turn_on", {"entity_id": "light.desk"}) in ha.svc_calls


def test_klar_leg_failure_in_t0_chain_not_blamed_on_integration():
    """失败归因按本步来源：klar 腿失败不得播"慧尖集成还没生效"（该通道与集成无关）。

    回执从 `svc_results` 注入：修好后这条腿走的是直调服务通道，intent 侧配失败
    已经打不到它了。"""
    (ok, reply), _ = _run(
        _chain("ControlWindow", WIN_ARGS, "t0",
               {"name": "HassTurnOff", "args": dict(LAMPER), "source": "klar"}),
        svc_results={"homeassistant.turn_off": {"success": False, "error": "Unknown intent"}})
    assert ok is False
    assert "集成" not in reply, reply          # 集成话术只在非 klar 步骤失败时用
    assert "第 2 步没成功" in reply, reply      # 步序定位照常


def test_chain_assembly_tags_each_leg_with_its_own_source():
    """装配面：分句逐路裁决 ⇒ merged.extra_steps 每项带该分句自己的 source。"""
    ex = RecExecutor()
    fp = Lane(table={"打开办公室平开窗": _p("ControlWindow", WIN_ARGS, source="t0")})
    kl = Lane(table={"关闭办公室射灯": _p("HassTurnOff", dict(LAMPER), source="klar")})
    p = _pipe(fp=fp, kl=kl, ex=ex, settings=PSettings())
    r = asyncio.run(p.handle("打开办公室平开窗关闭办公室射灯", origin="bench-d6"))
    assert r.source == "chain", r.trace
    merged = ex.plans[0]
    assert merged.source == "t0"                       # 首腿来源（旧代码据此判全链）
    assert merged.extra_steps[0]["source"] == "klar", merged.extra_steps
    assert merged.extra_steps[0]["name"] == "HassTurnOff"
    assert merged.extra_steps[0]["args"] == LAMPER


def test_leg_log_names_the_channel(caplog):
    """可观测：只印参数分不出两条外发路，D6 这个洞正是被"看不出走了哪条"藏住的。"""
    with caplog.at_level("INFO"):
        _run(_chain("ControlWindow", WIN_ARGS, "t0",
                    {"name": "HassTurnOff", "args": dict(LAMPER), "source": "klar"}))
    assert "第 1/2 步 ControlWindow" in caplog.text
    assert "直调服务" in caplog.text and "intent" in caplog.text, caplog.text[-600:]


def test_steps_shape_is_json_safe():
    """steps 三元组不得漏进对外载荷（dry_run/回执按 dict 形制消费 extra_steps）。"""
    leg = {"name": "HassTurnOff", "args": dict(LAMPER), "source": "klar"}
    plan = _chain("ControlWindow", WIN_ARGS, "t0", leg)
    assert json.loads(json.dumps(plan.extra_steps, ensure_ascii=False))[0]["source"] == "klar"
