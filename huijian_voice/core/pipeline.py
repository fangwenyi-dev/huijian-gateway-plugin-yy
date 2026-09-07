"""理解级联编排（v4.1 §2-E）：
  T0 正则 → T1 TextCNN → 场景触发（含于 T0/T1 内） → 查询族 → LLM(默认关) → 固定兜底
外加执行结果旁路：huijian_voice_utterance 事件（回合留痕，HA 自动化可消费）。
本级联是 LLM 通道 detect 的业务内核；STT/TTS 通道不经过它。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from . import const
from .nlu.fast_path import FastPath, Plan
from .nlu.klar_client import KlarClient

logger = logging.getLogger("huijian.pipeline")

# 级联仲裁（v1.0.8 klar 一级 NLU）：字面表恒优先于任何模型/引擎——场景触发词/
# 正则表是产品契约（用户配了就必须 100% 命中）。klar = "一级 NLU"（先于
# TextCNN T1），与字面表不是一层竞品。来源以 t0/scene 命名者 = 字面表族。
LITERAL_SOURCES = frozenset({"t0", "t0_strip", "t0_prefix", "t0_pinyin", "scene"})


def select_primary_plan(fp: Optional[Plan], kl: Optional[Plan]) -> Optional[Plan]:
    """纯裁决函数（可单测）：字面表 > klar > TextCNN T1。"""
    if fp is not None and fp.source in LITERAL_SOURCES:
        return fp
    if kl is not None:
        return kl
    return fp


@dataclass
class Reply:
    text: str
    source: str = ""                 # t0|t0_strip|t0_prefix|scene|t1|query|llm|fallback|dedup
    ok: bool = True
    trace: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(self, settings, ha, scenes, textcnn, executor, agent=None, klar=None):
        self.settings = settings
        self.ha = ha
        self.fast_path = FastPath(scenes, textcnn, settings)
        self.scenes = scenes
        self.executor = executor
        self.agent = agent
        # 一级确定性 NLU（fail-open：引擎缺失/熔断恒 None，级联照旧）
        self.klar = klar if klar is not None else KlarClient(settings)
        from .nlu.query import QueryZone
        self.query = QueryZone(ha, settings)
        self._last: dict[str, tuple[float, str]] = {}   # utterance → (ts, reply) 短时去重

    async def handle(self, text: str) -> Reply:
        t0 = time.time()
        text = (text or "").strip()
        if not text:
            return Reply("", "fallback", ok=False)
        # 短时去重（契约 §1.4-②：相同文本 2s 窗口防重复执行）
        win = float(self.settings.get("dialog.dedup_window_s", 2.0))
        if win > 0:
            prev = self._last.get(text)
            now = time.time()
            if prev and now - prev[0] < win:
                logger.info("[级联] 去重命中(%0.1fs 内重复): %s", now - prev[0], text)
                return Reply(prev[1], "dedup")
            self._last[text] = (now, "")
        reply = await self._cascade(text)
        self._last[text] = (time.time(), reply.text)
        # 旁路事件（fire-and-forget，见 main 的后台任务化；此处直发低峰可接受）
        await self.ha.fire_event(const.EVENT_NAME, {
            "utterance": text, "reply": reply.text, "source": reply.source,
            "ok": reply.ok, "ms": int((time.time() - t0) * 1000)})
        logger.info("[级联] %r → [%s] %r (%.0fms)", text, reply.source, reply.text, (time.time() - t0) * 1000)
        return reply

    async def _cascade(self, text: str) -> Reply:
        # ⓪①②③④ klar 一级 NLU 与 T0/T1/场景并行判定，按字面表 > klar > T1 仲裁
        try:
            fp_plan = await self.fast_path.match(text)
        except Exception:
            logger.exception("[级联] fast_path 异常（视为未命中）")
            fp_plan = None
        try:
            kl_plan = await self.klar.match(text)
        except Exception:
            logger.exception("[级联] klar 异常（fail-open，视为未命中）")
            kl_plan = None
        plan = select_primary_plan(fp_plan, kl_plan)
        if plan:
            ok, speech = await self.executor.run(plan)
            if ok:
                return Reply(speech, plan.source, True, plan.trace)
            # 执行失败：LLM 开着则让 LLM 再尝试一次理解，否则如实播失败
            if self.agent and self.agent.enabled:
                logger.info("[级联] 快速通道执行失败 → LLM 复议: %s", speech)
                llm = await self._llm(text)
                if llm:
                    return llm
            return Reply(speech, plan.source, False, plan.trace)
        # ⑤ 查询族
        try:
            ans = await self.query.answer(text)
        except Exception:
            logger.exception("[级联] 查询族异常")
            ans = None
        if ans:
            return Reply(ans, "query", True, [f"query:{text}"])
        # ⑥ LLM
        if self.agent and self.agent.enabled:
            llm = await self._llm(text)
            if llm:
                return llm
        # ⑦ 固定兜底
        return Reply(self.settings.get("dialog.fallback_text", const.FALLBACK_TEXT), "fallback")

    async def _llm(self, text: str) -> Optional[Reply]:
        parts: list[str] = []
        try:
            agen = self.agent.answer(text, self._history_snapshot())
            async for sent in agen:
                parts.append(sent)
        except Exception as e:
            logger.warning("[级联] LLM 失败: %s", e)
            return None
        if not parts:
            return None
        return Reply("".join(parts), "llm")

    def _history_snapshot(self) -> list[dict]:
        # M1 单句形态；M2 引入跨轮记忆（会话级环形缓冲）
        return []

    # Web UI「理解调试」用：只走级联不执行
    async def dry_run(self, text: str) -> dict:
        """调试面板内核：只跑理解，不执行设备指令；查询族为只读 REST，可放心真答。

        v1.0.8：plan 展示**真实会被执行的裁决结果**（字面表>klar>T1），并附
        fast_path / klar 两路原始命中，调试面板可直视分派依据。"""
        fp_plan = await self.fast_path.match(text)
        try:
            kl_plan = await self.klar.match(text)
        except Exception:
            logger.exception("[级联] dry_run klar 异常（按未命中显示）")
            kl_plan = None
        plan = select_primary_plan(fp_plan, kl_plan)

        def _dump(p):
            return None if p is None else {
                "intent": p.intent, "args": p.args, "source": p.source,
                "trace": p.trace, "speech": getattr(p, "speech", ""),
                "extra_steps": getattr(p, "extra_steps", [])}
        out = {"plan": _dump(plan), "fast_path": _dump(fp_plan), "klar": _dump(kl_plan)}
        if plan is None:
            try:
                out["query_answer"] = await self.query.answer(text)
            except Exception as exc:
                out["query_answer"] = None
                out["query_error"] = str(exc)
            out["llm_enabled"] = bool(self.agent and self.agent.enabled)
            if not out["llm_enabled"]:
                out["final"] = const.FALLBACK_TEXT
        return out
