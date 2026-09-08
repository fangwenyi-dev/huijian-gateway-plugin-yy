"""理解级联编排（v1.0.9 三层定位）：
  scene 契约 > 慧尖独占意图(窗/模式/调节/场景自动化管理) > klar 标准控制 >
  字面表剩余(t0/T1) > 查询族 > LLM(用户配置才启用) > 固定兜底；
  执行期两路互为降级（慧尖意图挂→klar 直调兜底；klar 挂→字面表回退）。
外加执行结果旁路：huijian_voice_utterance 事件（回合留痕，HA 自动化可消费）。
本级联是 LLM 通道 detect 的业务内核；STT/TTS 通道不经过它。

体验批（2026-09）新增：
  P0-1 fp/klar 两路并行判定（关键路径不再串行吃 klar HTTP 往返）；
  P0-3 事件旁路 task 化（不占回复延迟）；
  P1 去重三修：in-flight 共享结果（不再空回）、窗口锚定首见（完成不顺延）、
      有界清扫（不再无界增长）；
  P2-10 跨轮上下文：会话级环形历史（喂 LLM）+ 目标继承（代词/回指副词触发，
      Adjust* 无目标句兜底继承）——代码注释里的 M2 就此落地；
  P2-11 空间化：satellite_areas 把卫星 IP 映射到区域，无目标句默认落本区域；
  P2-12 复合句切分：fp/klar 分句全命中才链发（all-or-nothing 同 klar 纪律）；
  P2-13 风险操作确认环：解锁/删场景/删自动化先问后办（dialog.confirm_risky）；
  P2-15 LLM 流式钩子：on_sentence 逐句回调，Reply.streamed 防重复播报；
  P2-17 触发 targets 动态词表节流同步（friendly_name 派生）。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional

from . import const
from .nlu.fast_path import FastPath, Plan, is_pronoun, split_compound
from .nlu import targets as T
from .nlu import music
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
        if kl is None or kl is primary:
            return None
        # v1.0.12 窗户误动作闸（2026-09-08 实机：ControlWindow 未注册时
        # 「打开 办公室平开窗」降级 klar 命中办公室灯，真把灯点亮——比
        # 礼貌失败糟糕得多）。窗户是慧尖独占语义（开合器=按钮按压），
        # klar 兜底只允许同样打在窗类目标上；否则不降级，用主话术如实
        # 报「集成未运行」（v1.0.9 指令①诊断话术已点破根因）。
        if (primary.intent == "ControlWindow"
                or _mentions_window_device(primary.args)) \
                and not _mentions_window_device(kl.args):
            return None
        return kl
    # klar 失败（实体漂移/服务拒绝）：非场景类的字面表命中可作替代路径。
    return fp if (fp is not None and fp is not primary and fp.source != "scene") else None


@dataclass
class Reply:
    text: str
    source: str = ""                 # t0|t0_strip|t0_prefix|scene|t1|query|llm|fallback|dedup|confirm*|chain
    ok: bool = True
    trace: list[str] = field(default_factory=list)
    streamed: bool = False           # P2-15：on_sentence 已逐句送达，调用方勿重播


# ── 体验批常量 ──────────────────────────────────────────────────
DEDUP_MAX_ENTRIES = 512              # P1-7 去重表有界
CONTEXT_TTL_S = 90.0                 # P2-10 目标继承/历史窗口
CONTEXT_MAX_TURNS = 8                # 每 origin 环形缓冲（4 回合 ×2 条）
CONFIRM_TTL_S = 30.0                 # P2-13 待确认存活
_VOCAB_SYNC_S = 30.0                 # P2-17 动态词表节流

# P2-13 风险意图：解锁（含 D7 反转形态 TurnDeviceOff×锁）与删除族。
_RISKY_INTENTS = frozenset({"HassUnlock", "HassDeleteVoiceScene", "HassDeleteAutomation"})
_CONFIRM_YES = frozenset({"确认", "确定", "是的", "是", "对", "对呀", "嗯", "好", "好的",
                          "好吧", "执行", "继续吧", "yes", "ok", "y"})
_CONFIRM_NO = frozenset({"取消", "不", "不要", "不用", "别", "算了", "不确认", "no", "n"})

def _strip_punct(text: str) -> str:
    return re.sub(r"[\s。，,！!？?~～.]+$", "", (text or "").strip()).strip()


def _target_names(args: dict) -> list[str]:
    """从两种 args 形态提设备名（慧尖 target 列表 / klar 平铺 name）。永不抛。"""
    out: list[str] = []
    try:
        for ent in args.get("target") or []:
            for dev in (ent or {}).get("devices") or []:
                if (dev or {}).get("name"):
                    out.append(str(dev["name"]))
        if not out and args.get("name"):
            out.append(str(args["name"]))
    except Exception:
        pass
    return out


def _target_areas(args: dict) -> list[str]:
    out = []
    try:
        for ent in args.get("target") or []:
            if (ent or {}).get("area"):
                out.append(str(ent["area"]))
        if not out and args.get("area"):
            out.append(str(args["area"]))
    except Exception:
        pass
    return out


def _has_explicit_target(args: dict) -> bool:
    """target 已含区域或设备名 = 明示目标，不做继承/区域注入。"""
    return bool(_target_names(args) or _target_areas(args) or args.get("entity_id"))


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
        # P1-5/6/7 去重：text → {first, fut, reply}，有序有界（窗口锚首见）
        self._last: "OrderedDict[str, dict]" = OrderedDict()
        # P2-10 会话上下文（按 origin=卫星 IP / "panel" 分桶）
        self._turns: dict[str, deque] = {}
        self._last_target: dict[str, dict] = {}
        self._origin_ts: dict[str, float] = {}      # LRU 清扫用
        # P2-13 待确认环
        self._confirm: dict[str, dict] = {}
        # P0-3/P2-15 后台 task 强引用袋（session F7b 纪律）
        self._pending: set[asyncio.Task] = set()
        self._vocab_ts = 0.0

    # ── 后台 task 助手 ──────────────────────────────────────────
    def _spawn(self, coro: Coroutine) -> None:
        t = asyncio.create_task(coro)
        self._pending.add(t)
        t.add_done_callback(self._pending.discard)

    # ── 入口 ───────────────────────────────────────────────────
    async def handle(self, text: str, origin: str = "",
                     on_sentence: Optional[Callable[[str], Any]] = None) -> Reply:
        t0 = time.time()
        text = (text or "").strip()
        if not text:
            return Reply("", "fallback", ok=False)
        self._sync_vocab()
        dup = await self._dedup_gate(text)
        if dup is not None:
            return dup
        try:
            reply = await self._cascade(text, origin, on_sentence)
        except asyncio.CancelledError:
            self._dedup_abandon(text)            # 在飞方被杀：结算防共享方永挂
            raise
        except Exception:            # 级联永不冒泡：future 必须先结算再重抛语义（去重共享方）
            logger.exception("[级联] 意外异常（按兜底收束）")
            reply = Reply(self.settings.get("dialog.fallback_text", const.FALLBACK_TEXT),
                          "fallback", ok=False)
        self._dedup_settle(text, reply)
        # P0-3：事件旁路出回复路径（回合留痕对时序无强要求）
        self._spawn(self.ha.fire_event(const.EVENT_NAME, {
            "utterance": text, "reply": reply.text, "source": reply.source,
            "ok": reply.ok, "origin": origin,
            "ms": int((time.time() - t0) * 1000)}))
        logger.info("[级联] %r → [%s] %r (%.0fms)", text, reply.source, reply.text,
                    (time.time() - t0) * 1000)
        return reply

    # ── P1-5/6/7 去重三修 ──────────────────────────────────────
    def _dedup_sweep(self, now: float, win: float) -> None:
        # 已完成且出窗 → 删；在飞条目保留（其结果由 settle/abandon 结算）
        stale = [k for k, v in self._last.items()
                 if v.get("reply") is not None and now - v["first"] >= win]
        for k in stale:
            self._last.pop(k, None)
        if len(self._last) > DEDUP_MAX_ENTRIES:
            # 病态兜底：强行裁最早的已完成项（在飞项绝不裁，防共享方饿死）
            victims = [k for k, v in self._last.items() if v.get("reply") is not None]
            for k in victims[:max(1, len(victims) - DEDUP_MAX_ENTRIES // 2)]:
                self._last.pop(k, None)

    async def _dedup_gate(self, text: str) -> Optional[Reply]:
        """契约 §1.4-② 短时去重的成熟形态：
        ① 窗口内已完成 → 复述上次结果；② 窗口内在飞 → 共享同一 future 的真实结果
        （不再返回空串把 TTS 打成静默/兜底）；③ 窗口锚定首见时间，执行完成不顺延。"""
        win = float(self.settings.get("dialog.dedup_window_s", 2.0))
        if win <= 0:
            return None
        now = time.time()
        self._dedup_sweep(now, win)
        e = self._last.get(text)
        if e is not None and now - e["first"] < win:
            if e.get("reply") is not None:
                logger.info("[级联] 去重命中(%0.1fs 内重复): %s", now - e["first"], text)
                return Reply(e["reply"].text, "dedup", e["reply"].ok)
            # 在飞：等它的结果（封顶等待，超时报"正在处理"，绝不重复执行）
            try:
                r = await asyncio.wait_for(asyncio.shield(e["fut"]), timeout=min(10.0, win * 5))
                logger.info("[级联] 去重共享在飞结果: %s", text)
                return Reply(r.text, "dedup", r.ok)
            except asyncio.TimeoutError:
                logger.info("[级联] 去重在飞等待超时: %s", text)
                return Reply("这条指令我正在处理，请稍候", "dedup")
            except Exception:
                return Reply("刚才那条指令处理时出了点问题，可以再试一次", "dedup", ok=False)
        fut = asyncio.get_running_loop().create_future()
        self._last[text] = {"first": now, "fut": fut, "reply": None}
        self._last.move_to_end(text)
        return None

    def _dedup_settle(self, text: str, reply: Reply) -> None:
        e = self._last.get(text)
        if e is not None:
            e["reply"] = reply
            if not e["fut"].done():
                e["fut"].set_result(reply)

    def _dedup_abandon(self, text: str) -> None:
        """在飞执行被取消（超时/断连）：以兜底 Reply 结算，共享方拿真实文本，
        否则队头永远 in-flight，清扫与后续同句全部饿死。"""
        e = self._last.pop(text, None)
        if e is not None and not e["fut"].done():
            e["fut"].set_result(
                Reply(self.settings.get("dialog.fallback_text", const.FALLBACK_TEXT),
                      "fallback", ok=False))

    # ── P2-17 动态词表节流 ─────────────────────────────────────
    def _sync_vocab(self) -> None:
        now = time.time()
        if now - self._vocab_ts < _VOCAB_SYNC_S:
            return
        self._vocab_ts = now
        try:
            # 直读 ha 状态缓存（私有属性同进程只读；无 await，关键路径零成本）。
            T.sync_vocab(getattr(self.ha, "_states", {}) or {})
        except Exception:
            logger.debug("[词表] 动态同步异常", exc_info=True)

    # ── 两路并行判定（P0-1）────────────────────────────────────
    async def _match_fp(self, text: str) -> Optional[Plan]:
        try:
            return await self.fast_path.match(text)
        except Exception:
            logger.exception("[级联] fast_path 异常（视为未命中）")
            return None

    async def _match_klar(self, text: str) -> Optional[Plan]:
        try:
            return await self.klar.match(text)
        except Exception:
            logger.exception("[级联] klar 异常（fail-open，视为未命中）")
            return None

    async def _match_pair(self, text: str) -> tuple[Optional[Plan], Optional[Plan]]:
        """fp 与 klar 互不依赖：gather 并行，关键路径不再串行吃 klar 的 HTTP 往返。"""
        return await asyncio.gather(self._match_fp(text), self._match_klar(text))  # type: ignore[return-value]

    # ── 级联主流程 ─────────────────────────────────────────────
    async def _cascade(self, text: str, origin: str = "",
                       on_sentence: Optional[Callable[[str], Any]] = None) -> Reply:
        # P2-13 确认环优先：有 pending 时本句是对问句的回答（是/否/改口）
        answered = await self._confirm_answer(text, origin)
        if answered is not None:
            return answered

        # P2-12 复合句：分句全命中才链发，否则原样回退单发路径
        chain = await self._try_compound(text, origin)
        if chain is not None:
            return chain

        # ⓪①②③④ klar 引擎与 T0/T1/场景并行判定，三层裁决（见模块头）
        fp_plan, kl_plan = await self._match_pair(text)
        plan = select_primary_plan(fp_plan, kl_plan)
        plan = self._apply_context(plan, text, origin)
        if plan:
            ask = self._confirm_ask(plan, origin)
            if ask is not None:
                return ask
            ok, speech = await self.executor.run(plan)
            trace = list(plan.trace)
            if not ok:
                fb = select_fallback_plan(plan, fp_plan, kl_plan, speech)
                if fb is not None:
                    fb = self._apply_context(fb, text, origin)
                    ask = self._confirm_ask(fb, origin)   # 降级计划同样过风险闸
                    if ask is not None:
                        return ask
                    logger.info("[级联] %s 执行失败 → 降级 %s:%s（%r）",
                                plan.source, fb.source, fb.intent, speech[:24])
                    ok2, speech2 = await self.executor.run(fb)
                    trace.append(f"降级→{fb.source}:{fb.intent}" + ("✓" if ok2 else "✗"))
                    if ok2:
                        self._note_target(origin, fb)
                        return Reply(speech2, fb.source, True, trace)
                    # 两路全挂：降级话术点破「集成」根因者更可用（用户指令①的诊断价值）
                    if (any(h in speech2 for h in _INTEGRATION_HINTS)
                            and not any(h in speech for h in _INTEGRATION_HINTS)):
                        speech = speech2
            if ok:
                self._note_target(origin, plan)
                self._remember_turn(origin, text, speech)
                return Reply(speech, plan.source, True, trace)
            # 快速通道（klar+慧尖意图）双双用尽：LLM 配置了就复议，没配如实播失败
            if self.agent and self.agent.enabled:
                logger.info("[级联] 快速通道执行失败 → LLM 复议: %s", speech)
                llm = await self._llm(text, origin, on_sentence)
                if llm:
                    return llm
            self._remember_turn(origin, text, speech)
            return Reply(speech, plan.source, False, trace)
        # ⑤ 查询族
        try:
            ans = await self.query.answer(text)
        except Exception:
            logger.exception("[级联] 查询族异常")
            ans = None
        if ans:
            return Reply(ans, "query", True, [f"query:{text}"])
        # ⑤b 音乐过渡带（零改动，用户定向 2026-09-12）：点歌/播控直连 HA 标准
        # media_player 服务，置于 LLM 前——点歌令绝不落入闲聊吞掉
        mcmd = music.parse_music(text)
        if mcmd is not None:
            return await self._music(mcmd, text, origin)
        # ⑥ LLM
        if self.agent and self.agent.enabled:
            llm = await self._llm(text, origin, on_sentence)
            if llm:
                return llm
        # ⑦ 固定兜底
        return Reply(self.settings.get("dialog.fallback_text", const.FALLBACK_TEXT), "fallback")

    _MUSIC_SVC = {"pause": "media_pause", "resume": "media_play",
                  "stop": "media_stop", "next": "media_next_track",
                  "prev": "media_previous_track"}
    _MUSIC_SAY = {"pause": "好的，先暂停了", "resume": "继续播放",
                  "stop": "已停止播放", "next": "来，下一首",
                  "prev": "退回上一首"}

    async def _music(self, cmd: dict, text: str, origin: str) -> Reply:
        """音乐过渡带执行：HA core 标准 media_player 服务族（永不抛，ha_client
        已折叠）。端点未配置=一句配置指引；失败话术保「抱歉」前缀纪律。"""
        entity = str(self.settings.get("music.player_entity", "") or "").strip()
        if not entity:
            return Reply("想点歌的话，先到 设置-音乐 里配置播放端点"
                         "（Music Assistant 托管的音箱实体）。",
                         "music", ok=False, trace=["music:未配置端点"])
        act = cmd["action"]
        if act == "play":
            if not cmd["query"]:
                return Reply("想听点什么？说歌名或歌手就行。",
                             "music", trace=["music:泛点歌"])
            res = await self.ha.call_service("media_player", "play_media", {
                "entity_id": entity, "media_content_type": "music",
                "media_content_id": cmd["query"]})
            ok = bool(res.get("success"))
            speech = f"好的，正在播放《{cmd['query']}》" if ok \
                else "抱歉，播放端点没有响应"
        else:
            res = await self.ha.call_service(
                "media_player", self._MUSIC_SVC[act], {"entity_id": entity})
            ok = bool(res.get("success"))
            speech = self._MUSIC_SAY[act] if ok else "抱歉，播放端点没有响应"
        self._remember_turn(origin, text, speech)
        return Reply(speech, "music", ok, [f"music:{act}"])

    async def _llm(self, text: str, origin: str = "",
                   on_sentence: Optional[Callable[[str], Any]] = None) -> Optional[Reply]:
        parts: list[str] = []
        streamed = False
        try:
            agen = self.agent.answer(text, self._history_snapshot(origin))
            async for sent in agen:
                parts.append(sent)
                if on_sentence is not None:
                    await on_sentence(sent)
                    streamed = True
        except Exception as e:
            logger.warning("[级联] LLM 失败: %s", e)
            if streamed:
                # 半途而废：已流式送达片段，只补一句收束（返 None 会整段重播兜底）
                try:
                    await on_sentence("抱歉，这个回答中断了。")
                except Exception:
                    pass
                return Reply("", "llm", ok=False, streamed=True)
            return None
        if not parts:
            return None
        reply = Reply("".join(parts), "llm", streamed=streamed)
        self._remember_turn(origin, text, reply.text)
        return reply

    # ── P2-12 复合句 ───────────────────────────────────────────
    async def _try_compound(self, text: str, origin: str) -> Optional[Reply]:
        if not self.settings.get("dialog.chain_enabled", True):
            return None
        clauses = split_compound(text)
        if not clauses:
            return None
        pairs = await asyncio.gather(*[self._match_pair(c) for c in clauses])
        plans: list[Plan] = []
        chain_spec: Optional[dict] = None       # 链内回指：同句先行分句的具名目标
        for (fpp, klp), clause in zip(pairs, clauses):
            p = select_primary_plan(fpp, klp)
            if p is None:
                return None                          # 任一分句不中 → 整句回退单发
            # 上下文注入按分句文本（先前误用整句文本，"它"会误标到首句）；
            # 链内先行目标优先，跨轮目标/卫星区域兜底。
            p = self._apply_context(p, clause, origin, seed=chain_spec)
            if self._risky(p):
                return None                          # 链中藏风险操作 → 不链发
            # risky 判定放到注入后：代词分句继承出「锁」类目标同样要拦
            s = self._spec_of(p)
            if s is not None:
                chain_spec = s                       # 本句最新明示目标滚入下一分句
            plans.append(p)
        first = plans[0]
        chain_notes = [t for p in plans[1:] for t in p.trace if "链内回指" in t]
        merged = Plan(intent=first.intent, args=first.args, source=first.source,
                      utterance=text,
                      trace=list(first.trace) + chain_notes + [f"复合x{len(plans)}"],
                      extra_steps=[{"name": p.intent, "args": p.args} for p in plans[1:]])
        ok, speech = await self.executor.run(merged)
        if ok:
            for p in plans:
                self._note_target(origin, p)         # 末个具名目标定格为跨轮上下文
        self._remember_turn(origin, text, speech)
        return Reply(speech, merged.source if not ok else "chain", ok, merged.trace)

    # ── P2-10/11 上下文与空间注入 ──────────────────────────────
    def _apply_context(self, plan: Optional[Plan], text: str,
                       origin: str, seed: Optional[dict] = None) -> Optional[Plan]:
        """明示目标零改动；无目标句按 代词/回指>上轮目标 > 卫星区域 > 全屋 兜底。
        seed：链内回指注入的同句先行目标（视为最新鲜，绕过跨轮 TTL）。"""
        if plan is None or not self.settings.get("dialog.context_enabled", True):
            return plan
        if plan.source == "scene":
            return plan
        args = plan.args
        if args is None:                       # 坑：`plan.args or {}` 对空 dict 会
            args = plan.args = {}              # 另造孤儿 dict，注入写进去等于没写
        if _has_explicit_target(args):
            return plan
        if seed is not None:
            spec, fresh = seed, True           # 链内先行分句，天然新鲜
        else:
            spec = self._last_target.get(origin or "panel")
            now = time.time()
            ttl = float(self.settings.get("dialog.context_ttl_s", CONTEXT_TTL_S))
            fresh = bool(spec) and (now - spec["ts"] <= ttl)
        # 回指标记：fast_path 已裁定的"代词目标/回指"trace 最可靠；裸代词句与
        # 句首副词句式（"再打开"/"把它关了"）兜底文本级判定。
        marked = (any(("代词目标" in t or "回指→" in t) for t in plan.trace)
                  or is_pronoun(text) or "它" in text or "们" in text
                  or any(t in text for t in ("再", "还是", "继续", "也")))
        injected = False
        tag = "链内回指" if seed is not None else "上下文"
        if fresh and (marked or plan.intent in ("AdjustDeviceAttribute", "SetDeviceMode")):
            # 目标继承：慧尖 target 形态（含基础 Turn*；空 args 也算——代词句
            # _build_plan 产 args={}）直接复装；klar 平铺走 area/entity_id 支路
            if spec["kind"] == "target" and (plan.source != "klar" or "target" in args):
                args["target"] = copy.deepcopy(spec["target"])
                plan.trace.append(f"{tag}:继承目标 {args['target']}")
                injected = True
            elif spec["kind"] == "area" and "area" in args and not args.get("area"):
                args["area"] = spec["target"]
                plan.trace.append(f"{tag}:继承区域 {args['area']}")
                injected = True
            if not injected and spec["kind"] == "entity_id" and plan.source == "klar":
                args["entity_id"] = spec["target"]
                plan.trace.append(f"{tag}:沿用实体 {spec['target']}")
                injected = True
        if not injected:
            area = (self.settings.get("spatial.satellite_areas") or {}).get(origin)
            if area:
                if plan.intent in HUIJIAN_ONLY_INTENTS or "target" in args:
                    args["target"] = [{"area": area}]
                    plan.trace.append(f"空间化:卫星→{area}")
                elif "area" in args and not args.get("area"):
                    args["area"] = area
                    plan.trace.append(f"空间化:卫星→{area}")
        return plan

    def _spec_of(self, plan: Plan) -> Optional[dict]:
        """从计划抽「明示目标」三形态 spec（不带 ts）；无明示目标返回 None。
        链内回指与跨轮继承共用（同一抽取纪律，行为可对齐单测钉）。"""
        args = plan.args or {}
        try:
            if (plan.source == "klar" and isinstance(args.get("entity_id"), str)
                    and "." in args["entity_id"]):
                return {"kind": "entity_id", "target": args["entity_id"]}
            if _target_names(args) or _target_areas(args):
                tgt = copy.deepcopy(args.get("target")) or None
                if tgt is None and args.get("area"):
                    return {"kind": "area", "target": str(args["area"])}
                if tgt:
                    return {"kind": "target", "target": tgt}
        except Exception:
            return None
        return None

    def _note_target(self, origin: str, plan: Plan) -> None:
        """执行成功的明示目标入上下文。只存不改写（空目标句不覆写，保留最近具名目标）。"""
        if not origin:
            origin = "panel"
        spec = self._spec_of(plan)
        if spec:
            spec["ts"] = time.time()
            self._last_target[origin] = spec
            self._gc_origins()

    def _remember_turn(self, origin: str, user: str, assistant: str) -> None:
        if not origin:
            origin = "panel"
        dq = self._turns.get(origin)
        if dq is None:
            dq = self._turns[origin] = deque(maxlen=CONTEXT_MAX_TURNS)
        now = time.time()
        dq.append((now, user, assistant))
        self._origin_ts[origin] = now
        self._gc_origins()

    def _gc_origins(self) -> None:
        """origin 桶有界（64 台卫星封顶），超量按最久未用裁。"""
        for table in (self._turns, self._last_target, self._confirm):
            if len(table) <= 64:
                continue
            victims = sorted(self._origin_ts.items(), key=lambda kv: kv[1])
            for k, _ in victims[:max(1, len(table) - 64)]:
                table.pop(k, None)

    def _history_snapshot(self, origin: str) -> list[dict]:
        """P2-10：LLM 跨轮记忆（会话级环形缓冲，TTL 内的 user/assistant 对）。"""
        dq = self._turns.get(origin or "panel")
        if not dq:
            return []
        now = time.time()
        rounds = int(self.settings.get("llm.history_rounds", 10) or 10)
        msgs: list[dict] = []
        for ts, u, a in list(dq)[-rounds:]:
            if now - ts > float(self.settings.get(
                    "dialog.context_ttl_s", CONTEXT_TTL_S)) * 4:   # 历史比目标继承耐存一点
                continue
            msgs.append({"role": "user", "content": u})
            if a:
                msgs.append({"role": "assistant", "content": a})
        return msgs

    # ── P2-13 风险操作确认环 ───────────────────────────────────
    def _risky(self, plan: Plan) -> bool:
        if not self.settings.get("dialog.confirm_risky", True):
            return False
        if plan.intent in _RISKY_INTENTS:
            return True
        if plan.intent in ("TurnDeviceOff", "HassTurnOff") \
                and any("锁" in n for n in _target_names(plan.args or {})):
            return True                           # D7 反转语义：关锁=解锁
        return False

    def _confirm_ask(self, plan: Plan, origin: str) -> Optional[Reply]:
        if not self._risky(plan):
            return None
        origin = origin or "panel"
        args = plan.args or {}
        if plan.intent == "HassDeleteVoiceScene":
            phrase = str(args.get("trigger_phrase") or "").strip()
            act = f"删除场景「{phrase}」" if phrase else "删除该语音场景"
        elif plan.intent == "HassDeleteAutomation":
            act = "删除该自动化"
        else:
            what = "、".join(_target_names(args) or _target_areas(args) or ["该设备"])
            act = f"解锁{what}"
        self._confirm[origin] = {"plan": plan, "ts": time.time()}
        self._origin_ts[origin] = time.time()
        return Reply(f"接下来要{act}，说「确认」执行，或说「取消」放弃", "confirm",
                     ok=True, trace=list(getattr(plan, "trace", [])) + ["确认环:挂起"])

    async def _confirm_answer(self, text: str, origin: str) -> Optional[Reply]:
        origin = origin or "panel"
        pend = self._confirm.get(origin)
        if pend is None:
            return None
        if time.time() - pend["ts"] > float(
                self.settings.get("dialog.confirm_ttl_s", CONFIRM_TTL_S)):
            self._confirm.pop(origin, None)
            return None
        token = _strip_punct(text).lower()
        if token in _CONFIRM_YES:
            self._confirm.pop(origin, None)
            plan = pend["plan"]
            ok, speech = await self.executor.run(plan)
            if ok:
                self._note_target(origin, plan)
            self._remember_turn(origin, text, speech)
            return Reply(speech, "confirm_exec", ok,
                         list(getattr(plan, "trace", [])) + ["确认环:已确认"])
        if token in _CONFIRM_NO:
            self._confirm.pop(origin, None)
            return Reply("好的，已取消", "confirm_cancel", True, ["确认环:取消"])
        # 改口：撤挂起计划，本句按新指令走级联
        self._confirm.pop(origin, None)
        return None

    # ── Web UI「理解调试」用：只走级联不执行 ────────────────────
    async def dry_run(self, text: str) -> dict:
        """调试面板内核：只跑理解，不执行设备指令；查询族为只读 REST，可放心真答。

        v1.0.9：plan 展示**真实会被执行的裁决结果**（scene>慧尖独占>klar>
        字面表剩余），并附 fast_path / klar 两路原始命中，调试面板直视分派依据。"""
        fp_plan, kl_plan = await self._match_pair(text)   # 与真流量同构（并行）
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
