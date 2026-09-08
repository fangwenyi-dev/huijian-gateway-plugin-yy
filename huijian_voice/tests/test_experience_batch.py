# -*- coding: utf-8 -*-
"""体验批（P0×4/P1×5/P2×8）行为钉测。

覆盖：并行判定、事件旁路、去重三修、STT 抢占收束、场景后台刷新、
跨轮上下文/空间化/复合链/风险确认环、LLM 流式钩子、礼貌归一、
klar 半开熔断、TTS 句缓存、查询族属性/聚合。
"""
import asyncio
import json
import time
from collections import OrderedDict

import pytest

from core.nlu.fast_path import Plan, is_pronoun, normalize_polite, split_compound
from core.pipeline import DEDUP_MAX_ENTRIES, Pipeline, Reply


def arun(coro):
    return asyncio.run(coro)


# ── 通用假件 ────────────────────────────────────────────────────
class Lane:
    """fast_path / klar 替身：dict 查表，未命中回落 single（默认 None）。"""
    def __init__(self, table=None, single=None):
        self.table, self.single = table or {}, single
        self.seen = []

    async def match(self, text):
        self.seen.append(text)
        return self.table.get(text, self.single)


class RecExecutor:
    def __init__(self, result=(True, "好的，办好了")):
        self.result = result
        self.plans = []

    async def run(self, plan):
        self.plans.append(plan)
        return self.result


class GateExecutor(RecExecutor):
    """闸门执行器：run 卡在 event 上，用于制造在飞回合。"""
    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()

    async def run(self, plan):
        self.plans.append(plan)
        await self.gate.wait()
        return self.result


class NullQuery:
    async def answer(self, text):
        return None


class HA:
    def __init__(self, states=None):
        self._states = states or {}
        self.events = []

    async def fire_event(self, name, data):
        self.events.append((name, data))


class PSettings:
    def __init__(self, d=None):
        self.d = {"dialog.dedup_window_s": 2.0, "dialog.context_enabled": True,
                  "dialog.chain_enabled": True, "dialog.confirm_risky": True,
                  "spatial.satellite_areas": {}, "llm.history_rounds": 10,
                  "dialog.fallback_text": "我还不太确定这个指令"}
        self.d.update(d or {})

    def get(self, k, default=None):
        return self.d.get(k, default)


def _pipe(fp=None, kl=None, ex=None, settings=None, ha=None, agent=None):
    """Pipeline 手工装配（绕开重量级 __init__；与 _mk_pipe 系一致）。"""
    p = Pipeline.__new__(Pipeline)
    p.settings = settings or PSettings()
    p.ha = ha or HA()
    p.executor = ex or RecExecutor()
    p.agent = agent
    p.query = NullQuery()
    p.fast_path = fp or Lane()
    p.klar = kl or Lane()
    p.scenes = None
    p._last = OrderedDict()
    p._turns, p._last_target, p._origin_ts, p._confirm = {}, {}, {}, {}
    p._pending = set()
    p._vocab_ts = time.time()          # 抑制动态词表同步（单测独立性）
    return p


def _p(intent, args=None, source="t0", trace=None):
    return Plan(intent=intent, args=args if args is not None else {},
                source=source, utterance="", trace=trace or [])


def _tgt(area="客厅", name="灯"):
    return {"target": [{"area": area, "devices": [{"name": name}]}]}


# ── P0-1 并行判定 ──────────────────────────────────────────────
def test_match_pair_runs_fp_and_klar_concurrently():
    order = []

    class Slow:
        def __init__(self, tag, delay, plan):
            self.tag, self.delay, self.plan = tag, delay, plan

        async def match(self, text):
            order.append(self.tag + ":s")
            await asyncio.sleep(self.delay)
            order.append(self.tag + ":e")
            return self.plan

    p = _pipe(fp=Slow("fp", 0.05, None),
              kl=Slow("kl", 0.05, _p("HassTurnOn", source="klar")))
    t0 = time.monotonic()
    r = arun(p.handle("开灯"))
    assert time.monotonic() - t0 < 0.09, "串行需 0.1s+（P0-1 回归）"
    assert order[0] == "fp:s" and order[1] == "kl:s"   # 两路同时起跑
    assert r.source == "klar"


def test_event_sidechannel_does_not_block_reply():
    """P0-3：fire_event 慢也不再拖回复（旁路 task 化）。"""
    class SlowEventHA(HA):
        async def fire_event(self, name, data):
            await asyncio.sleep(1.0)
            self.events.append((name, data))

    async def scenario():
        p = _pipe(fp=Lane(single=_p("TurnDeviceOn", _tgt())), ha=SlowEventHA())
        t0 = time.monotonic()
        r = await p.handle("开客厅灯")
        assert r.ok and time.monotonic() - t0 < 0.5
        await asyncio.gather(*p._pending)               # 旁路收尾后才留痕
        assert p.ha.events and p.ha.events[0][1]["source"] == "t0"
    asyncio.run(scenario())


# ── P1-5/6/7 去重三修 ─────────────────────────────────────────
def test_dedup_inflight_shares_real_result():
    """重复句撞上在飞执行：共享真实结果，且物理执行只发生一次。"""
    async def scenario():
        ex = GateExecutor()
        p = _pipe(fp=Lane(single=_p("TurnDeviceOn", _tgt())), ex=ex)
        a = asyncio.create_task(p.handle("开灯"))
        await asyncio.sleep(0.02)
        b = asyncio.create_task(p.handle("开灯"))
        await asyncio.sleep(0.02)
        ex.gate.set()
        ra, rb = await asyncio.gather(a, b)
        assert len(ex.plans) == 1
        assert ra.text == rb.text == "好的，办好了"
        assert rb.source == "dedup" and rb.ok
    asyncio.run(scenario())


def test_dedup_window_anchored_at_first_not_completion():
    """窗口锚定首见：慢执行完成不顺延窗口——first 出窗后同句可再执行。"""
    ex = RecExecutor()
    p = _pipe(fp=Lane(single=_p("TurnDeviceOn", _tgt())), ex=ex)
    arun(p.handle("开灯"))
    r2 = arun(p.handle("开灯"))                         # 窗内 → 复述不重跑
    assert r2.source == "dedup" and len(ex.plans) == 1
    p._last["开灯"]["first"] = time.time() - 3          # 时间流过（锚在 first）
    r3 = arun(p.handle("开灯"))
    assert r3.source == "t0" and len(ex.plans) == 2     # 出窗重执行


def test_dedup_sweep_bounded():
    p = _pipe()
    now = time.time()
    for i in range(DEDUP_MAX_ENTRIES + 200):
        p._last[f"句{i}"] = {"first": now - 10, "fut": None, "reply": Reply("x")}
    p._dedup_sweep(now, 2.0)
    assert len(p._last) == 0
    # 在飞条目永不被裁（共享方不饿死）
    inflight = {"first": now - 99, "fut": None, "reply": None}
    p._last["在飞"] = inflight
    for i in range(DEDUP_MAX_ENTRIES + 1):
        p._last[f"完{i}"] = {"first": now - 0.01, "fut": None, "reply": Reply("y")}
    p._dedup_sweep(now, 2.0)
    assert "在飞" in p._last and len(p._last) <= DEDUP_MAX_ENTRIES


def test_dedup_abandon_on_cancel_frees_waiters():
    async def scenario():
        ex = GateExecutor()
        p = _pipe(fp=Lane(single=_p("TurnDeviceOn", _tgt())), ex=ex)
        task = asyncio.create_task(p.handle("开灯"))
        await asyncio.sleep(0.02)
        fut = p._last["开灯"]["fut"]
        waiter = asyncio.create_task(asyncio.wait_for(fut, 2))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        shared = await waiter                           # 不永挂：abandon 结算
        assert not shared.ok and shared.source == "fallback"
        ex.gate.set()
    asyncio.run(scenario())


# ── P1-8 STT 抢占收束 ─────────────────────────────────────────
def test_stt_preempted_stop_still_gets_one_frame():
    """被抢占/取消的旧识别任务必须以一条空 stt 帧收束（契约 §1.4 每 stop 必回）。"""
    from core.session import SttSession

    async def scenario():
        class Ws:
            closed = False

            def __init__(self):
                self.sent = []

            async def send_str(self, s):
                self.sent.append(json.loads(s))

        async def slow(pcm):
            await asyncio.sleep(5)
            return "不该出现"

        ws = Ws()
        ctx = type("C", (), {"asr": type("A", (), {"transcribe_pcm": staticmethod(slow)})()})()
        s = SttSession(ws, ctx)
        t1 = asyncio.create_task(s._run(b"x"))
        await asyncio.sleep(0.02)
        t1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t1
        assert ws.sent == [{"type": "stt", "text": ""}]  # 恰好一条空文本收束帧
    asyncio.run(scenario())


# ── P2-10 上下文继承 ───────────────────────────────────────────
def test_context_pronoun_inherits_previous_target():
    ex = RecExecutor()
    fp = Lane(table={
        "关掉客厅的灯": _p("TurnDeviceOff", _tgt()),
        # 真实 fast_path 形态：代词句产出 args={}（非 {"target": []}）
        "把它打开": _p("TurnDeviceOn", {}, trace=["代词目标:它打开→待上下文注入"]),
    })
    p = _pipe(fp=fp, ex=ex)
    arun(p.handle("关掉客厅的灯", origin="192.168.1.31"))
    arun(p.handle("把它打开", origin="192.168.1.31"))
    assert ex.plans[1].args["target"] == [{"area": "客厅", "devices": [{"name": "灯"}]}]
    assert any("上下文:继承目标" in t for t in ex.plans[1].trace)


def test_context_origin_isolation():
    """不同卫星各记各的目标（A 房间代词不串到 B 房间目标）。"""
    ex = RecExecutor()
    fp = Lane(table={
        "关掉客厅的灯": _p("TurnDeviceOff", _tgt(area="客厅")),
        "关掉书房的灯": _p("TurnDeviceOff", _tgt(area="书房")),
        "把它打开": _p("TurnDeviceOn", {"target": []},
                       trace=["代词目标:它→待上下文注入"]),
    })
    p = _pipe(fp=fp, ex=ex)
    arun(p.handle("关掉客厅的灯", origin="satA"))
    arun(p.handle("关掉书房的灯", origin="satB"))
    arun(p.handle("把它打开", origin="satB"))
    assert ex.plans[2].args["target"] == [{"area": "书房", "devices": [{"name": "灯"}]}]


def test_context_ttl_is_configurable():
    """dialog.context_ttl_s：把窗口拉宽到 1h，999s 前的目标仍可继承（默认 90s 会拒）。"""
    ex = RecExecutor()
    st = PSettings({"dialog.context_ttl_s": 3600})
    p = _pipe(ex=ex, settings=st)
    p._last_target["o"] = {"kind": "target", "target": [{"area": "走廊"}],
                           "ts": time.time() - 999}
    p.fast_path = Lane(single=_p("TurnDeviceOn", {},
                                 trace=["代词目标:它→待上下文注入"]))
    arun(p.handle("把它打开", origin="o"))
    assert ex.plans[0].args["target"] == [{"area": "走廊"}]


def test_context_expired_spec_not_inherited():
    ex = RecExecutor()
    p = _pipe(ex=ex)
    p._last_target["o1"] = {"kind": "target", "target": [{"area": "睡房"}],
                            "ts": time.time() - 999}
    p.fast_path = Lane(single=_p("TurnDeviceOn", {"target": []},
                                 trace=["代词目标:它→待上下文注入"]))
    arun(p.handle("把它打开", origin="o1"))
    assert ex.plans[0].args["target"] == []              # 陈旧目标不复用


def test_history_snapshot_roles_and_isolation():
    p = _pipe()
    p._remember_turn("sat1", "开灯", "好的，灯开了")
    p._remember_turn("sat1", "调亮", "好的，亮了")
    msgs = p._history_snapshot("sat1")
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert p._history_snapshot("nobody") == []


# ── P2-11 空间化 ───────────────────────────────────────────────
def test_spatial_area_injected_for_targetless_satellite():
    ex = RecExecutor()
    st = PSettings({"spatial.satellite_areas": {"10.0.0.9": "卧室"}})
    p = _pipe(fp=Lane(single=_p("TurnDeviceOn", {"target": []})), ex=ex, settings=st)
    arun(p.handle("开灯", origin="10.0.0.9"))
    assert ex.plans[0].args["target"] == [{"area": "卧室"}]
    assert any("空间化:卫星→卧室" in t for t in ex.plans[0].trace)


def test_spatial_does_not_touch_explicit_target():
    ex = RecExecutor()
    st = PSettings({"spatial.satellite_areas": {"10.0.0.9": "卧室"}})
    p = _pipe(fp=Lane(single=_p("TurnDeviceOn", _tgt(area="书房"))), ex=ex, settings=st)
    arun(p.handle("开书房的灯", origin="10.0.0.9"))
    assert ex.plans[0].args == _tgt(area="书房")


# ── P2-12 复合链 ───────────────────────────────────────────────
def test_compound_chains_when_all_clauses_match():
    ex = RecExecutor()
    fp = Lane(table={
        "打开灯": _p("TurnDeviceOn", _tgt(name="灯")),
        "关闭窗帘": _p("TurnDeviceOff", _tgt(name="窗帘")),
    })
    p = _pipe(fp=fp, ex=ex)
    r = arun(p.handle("打开灯，然后关闭窗帘"))
    assert r.ok and r.source == "chain"
    merged = ex.plans[0]
    assert merged.intent == "TurnDeviceOn" and len(merged.extra_steps) == 1
    assert merged.extra_steps[0]["name"] == "TurnDeviceOff"


def test_compound_chain_anaphora_same_sentence():
    """链内回指（用户令 2026-09-12）：「打开客厅的灯，然后再把它关掉」——
    第二分句的"它"必须指向同句第一分句，不是上上轮。"""
    ex = RecExecutor()
    fp = Lane(table={
        "打开客厅的灯": _p("TurnDeviceOn", _tgt()),
        "把它关掉": _p("TurnDeviceOff", {}, trace=["代词目标:它关掉→待上下文注入"]),
    })
    p = _pipe(fp=fp, ex=ex)
    r = arun(p.handle("打开客厅的灯，然后再把它关掉"))
    assert r.source == "chain", r.trace
    step2 = ex.plans[0].extra_steps[0]
    assert step2["name"] == "TurnDeviceOff"
    assert step2["args"]["target"] == _tgt()["target"]      # 继承首句 客厅/灯


def test_compound_chain_explicit_clause_not_overridden():
    """后分句自带明示目标时链不越权改写。"""
    ex = RecExecutor()
    fp = Lane(table={
        "打开客厅的灯": _p("TurnDeviceOn", _tgt()),
        "关闭书房窗帘": _p("TurnDeviceOff", _tgt(area="书房", name="窗帘")),
    })
    p = _pipe(fp=fp, ex=ex)
    arun(p.handle("打开客厅的灯，然后再关闭书房窗帘"))
    assert ex.plans[0].extra_steps[0]["args"] == _tgt(area="书房", name="窗帘")


def test_compound_chain_beats_previous_turn():
    """同句先行目标优先于跨轮目标（上轮书房台灯在场也不许抢）。"""
    ex = RecExecutor()
    fp = Lane(table={
        "打开客厅的灯": _p("TurnDeviceOn", _tgt()),
        "把它关掉": _p("TurnDeviceOff", {}, trace=["代词目标:它关掉→待上下文注入"]),
    })
    p = _pipe(fp=fp, ex=ex)
    p._last_target["o"] = {"kind": "target", "target": _tgt(area="书房", name="台灯")["target"],
                           "ts": time.time()}
    arun(p.handle("打开客厅的灯，然后再把它关掉", origin="o"))
    got = ex.plans[0].extra_steps[0]["args"]["target"]
    assert got == _tgt()["target"] and "链内回指" in str(ex.plans[0].trace)


def test_compound_risky_gate_runs_after_injection():
    """代词分句注入出「锁」类目标后绝不直执行（判序=注入→risky；链否决回退
    单发同样收口：整句先被复合链全有全无拒绝，再走单发上下文注入）。"""
    ex = RecExecutor()
    fp = Lane(table={"把它关了，然后再打开": _p(
        "TurnDeviceOff", {}, trace=["代词目标:它关了→待上下文注入"])})
    p = _pipe(fp=fp, ex=ex)
    p._last_target["o"] = {"kind": "target",
                           "target": _tgt(area="", name="大门门锁")["target"],
                           "ts": time.time()}
    r = arun(p.handle("把它关了，然后再打开", origin="o"))
    assert not ex.plans                                   # 绝不直执行
    assert "o" in p._confirm and "确认" in r.text          # 转入确认环


def test_compound_all_or_nothing_falls_back_to_single():
    ex = RecExecutor()
    fp = Lane(table={"打开灯": _p("TurnDeviceOn", _tgt())})   # 第二分句不命中
    p = _pipe(fp=fp, ex=ex)
    arun(p.handle("打开灯，然后讲个笑话"))
    assert all(not getattr(pl, "extra_steps", []) for pl in ex.plans)


def test_compound_refuses_risky_clause():
    ex = RecExecutor()
    fp = Lane(table={
        "打开灯": _p("TurnDeviceOn", _tgt()),
        "解锁门锁": _p("HassUnlock", _tgt(name="门锁")),
    })
    p = _pipe(fp=fp, ex=ex)
    arun(p.handle("打开灯，然后解锁门锁"))
    assert not ex.plans                                   # 链否决 + 整句不中 → 无人被执行


# ── P2-13 风险确认环 ───────────────────────────────────────────
def test_unlock_requires_confirm_then_yes_executes():
    ex = RecExecutor()
    p = _pipe(fp=Lane(single=_p("HassUnlock", _tgt(name="大门锁"))), ex=ex)
    r1 = arun(p.handle("解锁大门", origin="sat"))
    assert r1.source == "confirm" and not ex.plans and "确认" in r1.text
    r2 = arun(p.handle("确认", origin="sat"))
    assert r2.source == "confirm_exec" and len(ex.plans) == 1
    assert ex.plans[0].intent == "HassUnlock"


def test_confirm_no_and_rephrase_paths():
    ex = RecExecutor()
    unlock = _p("HassUnlock", _tgt(name="大门锁"))
    lane = Lane(table={"解锁大门": unlock, "大门开锁": unlock})
    p = _pipe(fp=lane, ex=ex)
    arun(p.handle("解锁大门", origin="s2"))
    r = arun(p.handle("取消", origin="s2"))
    assert r.source == "confirm_cancel" and not ex.plans and not p._confirm
    arun(p.handle("大门开锁", origin="s2"))               # 重新挂起（避开去重窗）
    assert p._confirm
    r = arun(p.handle("今天天气怎么样", origin="s2"))      # 改口：挂起作废，级联照常
    assert not p._confirm and not ex.plans and r.source == "fallback"


def test_turn_off_lock_risky_normal_device_not():
    ex = RecExecutor()
    p = _pipe(fp=Lane(single=_p("TurnDeviceOff", _tgt(name="门锁"))), ex=ex)
    assert arun(p.handle("关闭门锁")).source == "confirm"          # D7 反转语义
    p2 = _pipe(fp=Lane(single=_p("TurnDeviceOff", _tgt(name="台灯"))), ex=RecExecutor())
    assert arun(p2.handle("关闭台灯")).source == "t0"              # 正常关设备不拦


def test_confirm_ttl_expiry():
    p = _pipe()
    assert p._confirm_ask(_p("HassUnlock", _tgt(name="大门锁")), "o") is not None
    p._confirm["o"]["ts"] = time.time() - 31
    assert arun(p._confirm_answer("确认", "o")) is None
    assert "o" not in p._confirm


# ── P2-15 流式钩子 ─────────────────────────────────────────────
class StreamAgent:
    enabled = True

    def __init__(self, sents=("第一句。", "第二句。"), fail_after=None):
        self.sents, self.fail_after = list(sents), fail_after
        self.hists = []

    async def answer(self, text, hist):
        self.hists.append(hist)
        for i, s in enumerate(self.sents):
            if self.fail_after is not None and i == self.fail_after:
                raise RuntimeError("上游断流")
            yield s


def test_llm_streamed_sentences_call_on_sentence_once_each():
    async def scenario():
        got = []
        p = _pipe(agent=StreamAgent())

        async def cb(s):
            got.append(s)
        r = await p.handle("客厅现在适合观影吗", on_sentence=cb)
        assert got == ["第一句。", "第二句。"]
        assert r.streamed is True and r.source == "llm"
        assert r.text == "第一句。第二句。"
    asyncio.run(scenario())


def test_llm_partial_stream_failure_does_not_replay():
    async def scenario():
        got = []
        p = _pipe(agent=StreamAgent(fail_after=1))

        async def cb(s):
            got.append(s)
        r = await p.handle("讲个故事", on_sentence=cb)
        assert got == ["第一句。", "抱歉，这个回答中断了。"]   # 只补收束语，不重播
        assert r.streamed and not r.ok
    asyncio.run(scenario())


def test_llm_receives_history_of_previous_turn():
    async def scenario():
        agent = StreamAgent()
        p = _pipe(agent=agent)
        await p.handle("关掉客厅灯", origin="h1")             # 未命中→LLM→记历史
        await p.handle("再来一次", origin="h1")
        assert len(agent.hists) == 2
        assert any(m["content"] == "关掉客厅灯" for m in agent.hists[1])
    asyncio.run(scenario())


# ── P2-16 礼貌归一 / P2-12 切分 / 代词（函数级）───────────────
def test_normalize_polite_strips_head_and_tail():
    assert normalize_polite("请帮我把灯打开") == "把灯打开"
    assert normalize_polite("麻烦把空调关一下好吗") == "把空调关一下"
    assert normalize_polite("开灯谢谢") == "开灯"
    assert normalize_polite("请问现在几点了") == "现在几点了"
    assert normalize_polite("你能把窗帘拉上吗") == "把窗帘拉上吗"
    assert normalize_polite("好吗") == ""                     # 纯语气词交上层


def test_split_compound_bounded_and_safe():
    assert split_compound("打开灯然后关闭窗帘") == ["打开灯", "关闭窗帘"]
    assert split_compound("打开灯，再关闭窗帘，再关掉空调") == ["打开灯", "关闭窗帘", "关掉空调"]
    assert split_compound("打开灯，再关闭窗帘，再关空调，顺便拉帘") == []  # >3 段不链
    assert split_compound("再见") == []
    assert split_compound("开灯") == []


def test_is_pronoun():
    assert is_pronoun("它") and is_pronoun("那个。")
    assert not is_pronoun("它的灯") and not is_pronoun("开灯")


# ── P0-2 场景缓存后台刷新 ──────────────────────────────────────
def test_scenes_needs_blocking_and_bg_refresh():
    from core.nlu.scenes import SceneCache, TTL_S

    class Ha:
        def __init__(self): self.n = 0

        async def handle_intent(self, name, data, timeout=10.0):
            self.n += 1
            await asyncio.sleep(0)
            return {"success": True, "scenes": [{"trigger_phrase": "电影模式"}]}

    async def scenario():
        ha = Ha()
        sc = SceneCache(ha)
        assert sc.needs_blocking() is True                  # 冷缓存允许同步兜一次
        await sc.refresh()
        assert sc.needs_blocking() is False and ha.n == 1
        sc._last_refresh = time.time() - TTL_S - 1
        sc.refresh_soon()                                    # TTL 到期 → 后台单飞
        assert sc._bg is not None
        await sc._bg
        assert ha.n == 2
        sc._last_refresh = time.time() - TTL_S - 1
        sc.refresh_soon()
        sc.refresh_soon()                                    # 单飞：不叠加
        await sc._bg
        assert ha.n == 3
    asyncio.run(scenario())


# ── P1-9 klar 半开熔断 ────────────────────────────────────────
def test_klar_half_open_single_probe_after_cooldown():
    from core.nlu.klar_client import KlarClient, _FAIL_THRESHOLD

    class Boom:
        def __init__(self): self.n = 0

        def post(self, *a, **k):
            self.n += 1
            raise ConnectionError("engine down")

    async def scenario():
        c = KlarClient(PSettings())
        c.settings = type("S", (), {"get": staticmethod(lambda k, d=None: {
            "klar.enabled": True, "klar.url": "http://x", "klar.language": "zh-CN",
            "klar.timeout_s": 2.0, "klar.min_confidence": 0.8,
            "klar.token": ""}.get(k, d))})()
        boom = Boom()
        c._session = boom
        for _ in range(_FAIL_THRESHOLD):
            await c.parse("开灯")
        assert c._fails >= _FAIL_THRESHOLD and c._cooldown_until > time.monotonic()
        boom.n = 0
        assert await c.parse("开灯") is None
        assert boom.n == 0                                   # 冷却中：一枪不发
        c._cooldown_until = time.monotonic() - 1             # 冷却到期
        assert await c.parse("开灯") is None
        assert boom.n == 1 and not c._probing                # 半开：单探针且已结算
        boom.n = 0
        c._probing = True                                    # 探针在飞 → 后来者全拒
        assert await c.parse("开灯") is None
        assert boom.n == 0
        c._probing = False
        c._cooldown_until = time.monotonic() - 1
        assert await c.parse("开灯") is None                 # 探针失败 → 重回冷却
        assert c._cooldown_until > time.monotonic()
    asyncio.run(scenario())


# ── P0-4 TTS 句级缓存 ─────────────────────────────────────────
def _tts_engine():
    from core.tts import TtsEngine
    eng = TtsEngine(PSettings(), type("S", (), {})())
    eng._tts = object()                                       # 视为已加载（离线）
    return eng


def test_tts_sentence_cache_hit_skips_synth():
    async def scenario():
        calls = []
        eng = _tts_engine()
        eng._synth = lambda sent, sid, speed: calls.append(sent) or (b"\x00\x00" * 480)
        eng._encode = lambda pcm: [b"OPUS", b"FRAMES"]
        out1 = [f async for f in eng.stream_opus("好的，灯打开了。")]
        assert out1 == [b"OPUS", b"FRAMES"] and len(calls) == 1
        out2 = [f async for f in eng.stream_opus("好的，灯打开了。")]
        assert out2 == out1 and len(calls) == 1               # 命中：零重合成
        assert eng.cache_hits == 1
        eng.settings.d["tts.sid"] = 46                        # 换音色 → key 变
        [f async for f in eng.stream_opus("好的，灯打开了。")]
        assert len(calls) == 2
    asyncio.run(scenario())


def test_tts_cache_bounded():
    async def scenario():
        from core import tts as t
        eng = _tts_engine()
        eng._synth = lambda s, i, p: b"\x00" * 10
        eng._encode = lambda pcm: [b"x"]
        for i in range(t._CACHE_MAX_ITEMS + 5):
            [f async for f in eng.stream_opus(f"句子{i}")]
        assert len(eng._cache) <= t._CACHE_MAX_ITEMS
        assert eng._cache_bytes <= t._CACHE_MAX_BYTES
    asyncio.run(scenario())


def test_tts_cache_works_without_model_loaded():
    """省电档：模型卸载后模板句仍可凭缓存秒回（miss 才拉模型）。"""
    async def scenario():
        calls = []
        eng = _tts_engine()
        eng._synth = lambda s, i, p: calls.append(s) or b"\x00" * 10
        eng._encode = lambda pcm: [b"K"]
        [f async for f in eng.stream_opus("已打开。")]
        assert eng.unload() is True
        out = [f async for f in eng.stream_opus("已打开。")]
        assert out == [b"K"] and len(calls) == 1              # 卸载态命中缓存
    asyncio.run(scenario())


# ── P2-14 查询族扩容 ──────────────────────────────────────────
class QHA:
    def __init__(self, states, areas, entity_area):
        self._states, self._areas, self._entity_area = states, areas, entity_area

    async def area_names(self):
        return sorted(set(self._areas.values()))

    async def get_config(self):
        return {"time_zone": "Asia/Shanghai"}

    async def find_entities(self, area="", domains=(), name_contains=""):
        out = []
        for eid, ent in self._states.items():
            dom = eid.split(".", 1)[0]
            if domains and dom not in domains:
                continue
            if area and self._entity_area.get(eid) != area:
                continue
            out.append(ent)
        return out


def _qha():
    states = {
        "light.zhu": {"entity_id": "light.zhu", "state": "on",
                      "attributes": {"friendly_name": "主灯", "brightness": 128}},
        "light.fu": {"entity_id": "light.fu", "state": "off",
                     "attributes": {"friendly_name": "氛围灯"}},
        "climate.kt": {"entity_id": "climate.kt", "state": "heat",
                       "attributes": {"friendly_name": "空调", "temperature": 26}},
        "cover.cl": {"entity_id": "cover.cl", "state": "closed",
                     "attributes": {"friendly_name": "窗帘"}},
    }
    return QHA(states, {"living": "客厅"},
               {"light.zhu": "客厅", "light.fu": "客厅", "climate.kt": "客厅",
                "cover.cl": "客厅"})


def test_query_device_attribute_reading():
    from core.nlu.query import QueryZone
    q = QueryZone(_qha(), PSettings())
    a = arun(q.answer("客厅空调设定温度是多少"))
    assert a and "26" in a
    b = arun(q.answer("客厅主灯的亮度是多少"))
    assert b and "50%" in b                                  # 128/255≈50%


def test_query_count_and_list():
    from core.nlu.query import QueryZone
    q = QueryZone(_qha(), PSettings())
    a = arun(q.answer("客厅有哪些灯开着"))
    assert a and "主灯" in a and "氛围灯" not in a
    b = arun(q.answer("客厅有多少个设备开着"))
    assert b and "2" in b                                    # 主灯(on) + 空调(heat)


# ── P2-17 动态词表 ────────────────────────────────────────────
def test_targets_sync_vocab_and_clear():
    from core.nlu import targets as T
    T.clear_vocab()
    try:
        T.sync_vocab({
            "light.ct1": {"attributes": {"friendly_name": "餐吊灯"}},
            "light.dup": {"attributes": {"friendly_name": "餐吊灯"}},
            "sensor.temp": {"attributes": {"friendly_name": "温度计"}},   # 域外不收
            "light.weird": {"attributes": {"friendly_name": "开关面板"}},  # 停用词裁切
        })
        assert "餐吊灯" in T.ALL_DEVICES
        assert T.ALL_DEVICES.count("餐吊灯") == 1             # 去重
        assert "温度计" not in T.ALL_DEVICES                  # 域过滤
    finally:
        T.clear_vocab()


def test_pipeline_vocab_sync_throttled():
    calls = []
    p = _pipe(ha=HA({"light.a": {"attributes": {"friendly_name": "走道灯"}}}))
    import core.nlu.targets as T
    orig = T.sync_vocab
    T.sync_vocab = lambda s: calls.append(len(s))
    try:
        p._vocab_ts = 0.0
        p._sync_vocab()
        p._sync_vocab()                                      # 30s 内节流
        assert calls == [1]
        p._vocab_ts = time.time() - 999
        p._sync_vocab()
        assert calls == [1, 1]
    finally:
        T.sync_vocab = orig
        T.clear_vocab()


# ── P2-15 agent SSE 增量解析 ──────────────────────────────────
import json as _json

from core.agent import Agent


class _Stream:
    def __init__(self, chunks):
        self.chunks = chunks

    async def read(self, n):
        return self.chunks.pop(0) if self.chunks else b""


class _Resp:
    def __init__(self, chunks, ctype="text/event-stream", status=200):
        self.chunks, self.headers, self.status = chunks, {"Content-Type": ctype}, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def content(self):
        return _Stream(self.chunks)

    async def text(self):
        return ""

    async def json(self):
        return {"choices": [{"message": {"content": "整包答案"}}]}


class _Sess:
    def __init__(self, resp):
        self.resp, self.closed = resp, False

    def post(self, url, **k):
        return self.resp


def _agent(resp):
    st = PSettings({"llm.enabled": True, "llm.base_url": "http://x/v1", "llm.stream": True})
    a = Agent(st, None, None)
    a._sess = lambda: asyncio.sleep(0, resp)           # 协程化注入（_sess 是 async）

    async def _db():
        return ""
    a._device_brief = _db                              # 免设备简报
    return a


def _ev(payload):
    return ("data: " + _json.dumps(payload, ensure_ascii=False) + "\n\n").encode()


def test_sse_incremental_sentences_emit():
    chunks = [
        _ev({"choices": [{"delta": {"content": "你好"}}]}),
        _ev({"choices": [{"delta": {"content": "，世界。"}}]}),
        _ev({"choices": [{"delta": {"content": "还有半句"}}]}),
        b"data: [DONE]\n\n",
    ]
    a = _agent(_Sess(_Resp(list(chunks))))
    got = []

    async def cb(s):
        got.append(s)
    msg = arun(a._chat_stream([], False, cb))
    assert got == ["你好，世界。", "还有半句"]          # 句读到终止标点即 emit
    assert msg["content"] == "你好，世界。还有半句" and "tool_calls" not in msg


def test_sse_tool_calls_delta_assembled():
    ev1 = {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "call1",
         "function": {"name": "TurnDeviceOn", "arguments": '{"ta'}}]}}]}
    ev2 = {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": 'rget":[]}'}}]}}]}
    chunks = [_ev(ev1), _ev(ev2), b"data: [DONE]\n\n"]
    a = _agent(_Sess(_Resp(list(chunks))))
    msg = arun(a._chat_stream([], True, lambda s: asyncio.sleep(0)))
    tc = msg["tool_calls"][0]
    assert tc["id"] == "call1" and tc["function"]["name"] == "TurnDeviceOn"
    assert _json.loads(tc["function"]["arguments"]) == {"target": []}


def test_stream_unsupported_latches_to_full_packet():
    a = _agent(_Sess(_Resp([b'{"choices":[]}'], ctype="application/json")))
    q = asyncio.Queue()

    async def scenario():
        msg, streamed = await a._one_round([], False, q)
        assert streamed is False and a._no_stream is True
        assert msg["content"] == "整包答案"              # 回退 _chat 整包
        from core.agent import _ROUND_END
        assert q.get_nowait() is _ROUND_END              # 收束哨兵必达（answer 不饿死）
    arun(scenario())


def test_answer_generator_streams_and_returns_final():
    a = _agent(_Sess(_Resp([
        _ev({"choices": [{"delta": {"content": "第一句。第二句。"}}]}),
        b"data: [DONE]\n\n"])))

    async def scenario():
        out = [s async for s in a.answer("随便聊聊", [])]
        assert out == ["第一句。", "第二句。"]
    arun(scenario())


def test_user_scenario_e2e_real_fastpath():
    """用户原句（2026-09-12 提问）：「打开客厅通道」→「关掉它」= 关闭客厅通道。
    真 TextCNN + 真 FastPath + 真 pipeline 上下文层全链（非假件裁决）。"""
    import os
    from core.nlu.textcnn import TextCNN
    from core.nlu.fast_path import FastPath
    pytest.importorskip("onnxruntime")
    tc = TextCNN(os.environ["HUIJIAN_NLU_DATA"])
    tc._ensure()
    if not tc.available:
        pytest.skip("TextCNN 资产缺失")

    class FS:
        async def refresh(self, force=False):
            pass

        def needs_blocking(self):
            return False

        def refresh_soon(self):
            pass

        def check(self, text):
            return None

    ex = RecExecutor()
    p = _pipe(ex=ex)
    p.fast_path = FastPath(FS(), tc, p.settings)
    arun(p.handle("打开客厅通道", origin="satX"))
    arun(p.handle("关掉它", origin="satX"))
    assert ex.plans[1].intent == "TurnDeviceOff"
    assert ex.plans[1].args["target"] == [
        {"area": "客厅", "devices": [{"name": "通道", "domains": []}]}], ex.plans[1].trace
    assert any("上下文:继承目标" in t for t in ex.plans[1].trace)


# ── P2-12 executor 侧链话术 ───────────────────────────────────
def test_executor_multistep_failure_localizes_step():
    from core.executor import Executor

    class HA2:
        def __init__(self): self.n = 0

        async def handle_intent(self, name, data, timeout=10.0):
            self.n += 1
            if self.n == 2:
                return {"success": False, "message": "no match found"}
            return {"success": True}

        async def call_service(self, *a, **k):
            return {"success": True}

    ex = Executor(HA2())
    plan = Plan(intent="TurnDeviceOn", args=_tgt(name="灯"), source="t0",
                extra_steps=[{"name": "TurnDeviceOff", "args": _tgt(name="窗帘")}])
    ok, reply = arun(ex.run(plan))
    assert not ok and reply.startswith("抱歉")
    assert "第 2 步没成功" in reply
