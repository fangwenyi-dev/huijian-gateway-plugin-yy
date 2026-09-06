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

logger = logging.getLogger("huijian.pipeline")


@dataclass
class Reply:
    text: str
    source: str = ""                 # t0|t0_strip|t0_prefix|scene|t1|query|llm|fallback|dedup
    ok: bool = True
    trace: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(self, settings, ha, scenes, textcnn, executor, agent=None):
        self.settings = settings
        self.ha = ha
        self.fast_path = FastPath(scenes, textcnn, settings)
        self.scenes = scenes
        self.executor = executor
        self.agent = agent
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
        # ①②③④ T0/T1/场景
        try:
            plan = await self.fast_path.match(text)
        except Exception:
            logger.exception("[级联] fast_path 异常（视为未命中）")
            plan = None
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
        """调试面板内核：只跑理解，不执行设备指令；查询族为只读 REST，可放心真答。"""
        plan = await self.fast_path.match(text)
        out = {"plan": None if plan is None else {"intent": plan.intent, "args": plan.args,
               "source": plan.source, "trace": plan.trace}}
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
