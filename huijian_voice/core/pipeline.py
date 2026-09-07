"""理解级联编排（v1.0.9 三层定位）：
  scene 契约 > 慧尖独占意图(窗/模式/调节/场景自动化管理) > klar 标准控制 >
  字面表剩余(t0/T1) > 查询族 > LLM(用户配置才启用) > 固定兜底；
  执行期两路互为降级（慧尖意图挂→klar 直调兜底；klar 挂→字面表回退）。
外加执行结果旁路：huijian_voice_utterance 事件（回合留痕，HA 自动化可消费）。
本级联是 LLM 通道 detect 的业务内核；STT/TTS 通道不经过它。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import const
from .nlu.fast_path import FastPath, Plan
from .nlu.klar_client import KlarClient

logger = logging.getLogger("huijian.pipeline")

# ── 级联仲裁（v1.0.9 三层定位，用户拍板）──────────────────────────
# ① scene = 用户触发词契约，恒最高优先；
# ② 慧尖意图只负责 klar 做不了的类型：窗户（开合器=按钮按压语义）、模式
#    （能力探测+剔除 off）、相对/属性调节、语音场景与自动化管理、实时上下文；
#    目标名含窗类词（窗帘/纱窗除外——它们是标准 cover）同样归慧尖意图；
# ③ 标准控制（开/关/调亮/温度…）klar 恒优先：引擎 grounded entity_id 直调
#    服务，不依赖 huijian_ai 集成——集成没加载的路也有（指令①）；
# ④ 两路互为降级（select_fallback_plan），仍失败且用户配了 LLM 才复议（指令③）。
HUIJIAN_ONLY_INTENTS = frozenset({
    "ControlWindow", "SetDeviceMode", "AdjustDeviceAttribute",
    "HassTriggerVoiceScene", "HassCreateVoiceScene", "HassDeleteVoiceScene",
    "HassListVoiceScenes", "HassCreateAutomation", "HassDeleteAutomation",
    "HassListAutomations", "HassUpdateAutomation", "HuijianGetLiveContext",
})

_INTEGRATION_HINTS = ("集成",)


def _mentions_window_device(args: Any) -> bool:
    """慧尖场景里「窗」多为开合器按钮，cover 服务表达不了内倒/暂停。
    先剔除窗帘/纱窗（标准 cover，klar 干得好）。永不抛。"""
    try:
        t = json.dumps(args or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    t = t.replace("窗帘", "").replace("纱窗", "")
    return any(w in t for w in ("窗", "开合器", "内倒", "推拉门"))


def select_primary_plan(fp: Optional[Plan], kl: Optional[Plan]) -> Optional[Plan]:
    """纯裁决函数（可单测）：scene 契约 > 慧尖独占 > klar 标准 > 字面表剩余。"""
    if fp is not None:
        if fp.source == "scene":
            return fp
        if fp.intent in HUIJIAN_ONLY_INTENTS or _mentions_window_device(fp.args):
            return fp
    if kl is not None:
        return kl
    return fp


def select_fallback_plan(primary: Optional[Plan], fp: Optional[Plan],
                         kl: Optional[Plan], speech: str) -> Optional[Plan]:
    """纯函数：主计划执行失败后的降级选择；None = 不降级（如实报+LLM 复议）。"""
    if primary is None:
        return None
    if primary.source != "klar":
        # 慧尖意图失败：常见根因就是「集成还没加载」——klar 直调不依赖集成，
        # 只要 klar 同句有命中（裁决时让位给契约/独占类），值得一试。
        return kl if (kl is not None and kl is not primary) else None
    # klar 失败（实体漂移/服务拒绝）：非场景类的字面表命中可作替代路径。
    return fp if (fp is not None and fp is not primary and fp.source != "scene") else None


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
        # ⓪①②③④ klar 引擎与 T0/T1/场景并行判定，三层裁决（见模块头）
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
            trace = list(plan.trace)
            if not ok:
                fb = select_fallback_plan(plan, fp_plan, kl_plan, speech)
                if fb is not None:
                    logger.info("[级联] %s 执行失败 → 降级 %s:%s（%r）",
                                plan.source, fb.source, fb.intent, speech[:24])
                    ok2, speech2 = await self.executor.run(fb)
                    trace.append(f"降级→{fb.source}:{fb.intent}" + ("✓" if ok2 else "✗"))
                    if ok2:
                        return Reply(speech2, fb.source, True, trace)
                    # 两路全挂：降级话术点破「集成」根因者更可用（用户指令①的诊断价值）
                    if (any(h in speech2 for h in _INTEGRATION_HINTS)
                            and not any(h in speech for h in _INTEGRATION_HINTS)):
                        speech = speech2
            if ok:
                return Reply(speech, plan.source, True, trace)
            # 快速通道（klar+慧尖意图）双双用尽：LLM 配置了就复议，没配如实播失败
            if self.agent and self.agent.enabled:
                logger.info("[级联] 快速通道执行失败 → LLM 复议: %s", speech)
                llm = await self._llm(text)
                if llm:
                    return llm
            return Reply(speech, plan.source, False, trace)
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

        v1.0.9：plan 展示**真实会被执行的裁决结果**（scene>慧尖独占>klar>
        字面表剩余），并附 fast_path / klar 两路原始命中，调试面板直视分派依据。"""
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
