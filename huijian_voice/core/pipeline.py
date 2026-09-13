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
  P2-13 风险操作确认环：**解锁族**先问后办（dialog.confirm_risky）——含 t0 的
      HassUnlock、慧尖形 TurnDeviceOff×名含锁、klar grounded HassTurnOff/Toggle×
      lock.* 实体与多步 plan 的解锁步（2026-09-22 审查批 C2 补全三形态+extra_steps）；
      LLM 工具通道对锁目标直接拒办并指回本地确认流程。删场景/删自动化不走本环：
      创建通道要求精准命中触发词/ID（多义与未命中一律拒办并列表引导），命中即
      执行、落点由播报复述点名（test_pipeline_creation 钉死现行为，本注释此前超售）；
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
from .nlu.fast_path import FastPath, Plan, is_pronoun, is_whole_house, split_compound
from .nlu import targets as T
from .nlu import music
from .nlu import creation
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

# 空间化兜底域（无目标位时补 target 用；绝不发 area-only——集成端
# target["devices"] 会 KeyError，2026-09-12 实锤）
_SPATIAL_DOMAIN = {
    "ControlWindow": ["cover"],
    "AdjustDeviceAttribute": ["light"],
    "SetDeviceMode": ["climate"],
}

# 泛类设备词：这类目标（"开灯/关窗帘"）才做卫星区域限定；指名道姓的设备句
# 零影响（v1.0.20 原设计语义，2026-09-12 补实现）
_GENERIC_DEVICE_WORDS = frozenset({
    "灯", "灯光", "灯带", "筒灯", "射灯", "吊灯", "窗帘", "空调", "风扇",
    "插座", "开关", "电视", "音箱", "净化器", "加湿器", "扫地机", "热水器",
})

_INTEGRATION_HINTS = ("集成",)

# 本地 NLU 总开关（nlu.enabled=false）关掉时的兜底话术：两端都关必须说人话，
# 不能拿"这句话我还不会"糊弄（那会把配置问题伪装成理解失败）。
_NLU_OFF_TEXT = ("本地理解已关闭，而且还没配置大模型——请在设置里打开"
                 "「启用本地理解」，或填好大模型端点再试")

# 触发条件形状（"每天晚上8点"/"当客厅温度超过30度"）：改自动化时用户只给条件、
# 不重复动作 → 动作沿用原样，只换触发条件。
_TRIGGER_ONLY_RE = re.compile(r"^(?:每天|当|如果|要是|假如)")


# 区域继承的重排动词表（"打开空调"+客厅 → "打开客厅空调"，fast_path 认的
# 动词+区域+设备语序；单字动词排最后，避免把"开合度"类名词拦腰切断）
_AREA_VERB_RE = re.compile(
    r"^(打开|开启|开一下|关闭|关掉|关上|关了|拉上|拉下|调到|调成|调高|调低|"
    r"调整|调节|设为|设成|设定|设置|锁上|解锁|停止|暂停|开|关|调|拉|锁)")


def _area_as_name(plan: Optional[Plan], area: str) -> bool:
    """区域被当成了设备名（target 无 area、name=区域、domains 空）——执行侧按
    名字子串命中（"客厅"∈"客厅射灯/客厅空调/客厅窗帘"）会把整个区域的设备都开了，
    属于"过宽目标"，绝不允许进场景/自动化，也不许直接执行。"""
    tgt = (plan.args or {}).get("target") if plan is not None else None
    if not isinstance(tgt, list) or not tgt:
        return True
    for t in tgt:
        if not isinstance(t, dict):
            return True
        if str(t.get("area") or "").strip():
            continue                              # 有区域限定 → 窄目标，放行
        for d in (t.get("devices") or []):
            if not isinstance(d, dict):
                return True
            if str(d.get("name") or "").strip() == area and not (d.get("domains") or []):
                return True
    return False


def _reorder_area(clause: str, area: str) -> Optional[str]:
    """「打开空调」→「打开客厅空调」；「把空调打开」→「客厅空调打开」。
    两条都是 fast_path 认得的语序，产出 区域+设备+domains 的规范目标；
    无法安全重排（无动词可切）返回 None，交由调用方如实拒收。"""
    s = (clause or "").strip()
    m = _AREA_VERB_RE.match(s)
    if m:
        rest = s[m.end():].strip()
        return f"{m.group(1)}{area}{rest}" if rest else None
    s2 = re.sub(r"^[把将]\s*", "", s)
    if s2 and s2 != s:
        return f"{area}{s2}"                      # 设备在前：区域+设备+动作
    return None


def _mentions_window_device(args: Any) -> bool:
    """慧尖场景里「窗」多为开合器按钮，cover 服务表达不了内倒/暂停。
    先剔除窗帘/纱窗（标准 cover，klar 干得好）。永不抛。"""
    try:
        t = json.dumps(args or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    t = t.replace("窗帘", "").replace("纱窗", "")
    return any(w in t for w in ("窗", "开合器", "内倒", "推拉门"))


# v1.0.55 主裁决窗闸（2026-09-12 现场 16:10/16:41 双案实锤）：
# 「办公室瓶盖窗速度设为百分之三十五」被 klar 回放兜底（引擎 draft.rs：未知
# 目标+任意数字 → 硬套上一个可见灯 + HassLightSet+brightness）点亮了摄影灯。
# v1.0.12 的窗闸只护**降级方向**且查 args——klar grounded args 里只有 pinyin
# entity_id（无任何中文），查不到窗。此闸补主裁决方向：**原话文本** × **目标
# 域** 交叉核验。误伤面刻意收得很窄：
#   · 窗帘/纱窗先行剔除（它们是 klar 该干的标准 cover）；
#   · 目标本来就是 cover/fan 放行（窗/风扇的速度语义合法）；
#   · 句内同时提了灯（「窗户旁边的灯」）放行——用户真在说灯；
#   · 只 veto 明确的"开关/亮度类意图 × light/switch 实体"，其余（空调温度等）
#     不动。弃用后走级联下层（TextCNN/查询/LLM），宁可不执行也绝不错开灯。
_KLAR_WINDOW_GUARD_INTENTS = frozenset({
    "HassLightSet", "HassSetPosition",
    "HassTurnOn", "HassTurnOff", "HassToggle",
})


def _klar_window_lamp_conflict(kl: Optional[Plan]) -> bool:
    """True = 该 klar 计划与句内窗/速度语义冲突，主裁决必须弃用。永不抛。"""
    try:
        if kl is None or kl.intent not in _KLAR_WINDOW_GUARD_INTENTS:
            return False
        eid = str((kl.args or {}).get("entity_id") or "")
        if "." not in eid:
            return False                      # 未 grounded 步 → intent 通道自理，不在此闸职责
        dom = eid.split(".", 1)[0]
        if dom in ("cover", "fan"):
            return False                      # 窗/风扇的速度·位置语义合法
        t = (kl.utterance or "").replace("窗帘", "").replace("纱窗", "")
        if not any(w in t for w in ("窗", "开合器", "内倒", "推拉门", "速度", "力度")):
            return False
        if dom == "light" and any(w in t for w in ("灯", "照明", "亮")):
            return False                      # 句里同时点了灯：用户真在说灯
        return dom in ("light", "switch")
    except Exception:  # noqa: BLE001 —— 守卫自身故障不得拦正常句
        return False


def select_primary_plan(fp: Optional[Plan], kl: Optional[Plan]) -> Optional[Plan]:
    """纯裁决函数（可单测）：scene 契约 > 慧尖独占 > klar 标准 > 字面表剩余。"""
    if fp is not None:
        if fp.source == "scene":
            return fp
        if fp.intent in HUIJIAN_ONLY_INTENTS or _mentions_window_device(fp.args):
            return fp
    if kl is not None:
        # v1.0.55：见 _klar_window_lamp_conflict——句在说窗、klar 却指向
        # 灯/开关时**整条弃用**（返回 None 落级联下层，宁可不执行）。
        if _klar_window_lamp_conflict(kl):
            return None
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


def _is_wholehouse_args(args: dict) -> bool:
    """target 仅由「空 name + domains 过滤」构成 = 显式全屋。

    v1.0.40 修复（A2）：这类目标必须**免于上下文继承**——旧实现下 `_has_explicit_target`
    对它是 False（没有 name/area/entity_id），于是「再打开所有灯」会被上一轮的目标
    （如「客厅的灯」）静默替换：用户说全屋、实际只动一个房间，而 trace 里还写着
    "全屋显式"。真机实测：覆盖前 `[{'devices':[{'name':'','domains':['light']}]}]`
    → 覆盖后 `[{'area':'客厅','devices':[{'name':'灯'}]}]`。
    """
    tgt = args.get("target")
    if not isinstance(tgt, list) or not tgt:
        return False
    saw_domain = False
    for ent in tgt:
        if not isinstance(ent, dict) or ent.get("area") or ent.get("entity_id"):
            return False
        devs = ent.get("devices")
        if not isinstance(devs, list) or not devs:
            return False
        for d in devs:
            if not isinstance(d, dict) or d.get("name"):
                return False
            if not d.get("domains"):
                return False
            saw_domain = True
    return saw_domain


def _at_say(at: str) -> str:
    """'07:00' → '早上7点'、'22:30' → '晚上10点半'（耳朵友好，不回显 ISO 格式）。"""
    try:
        h, m = int(at[:2]), at[3:5]
    except (ValueError, IndexError):
        return at
    seg = ("凌晨" if h < 6 else "早上" if h < 11 else "中午" if h < 13
           else "下午" if h < 18 else "晚上")
    hh = h if 1 <= h <= 12 else (h - 12 if h >= 13 else 12)
    if m == "30":
        return f"{seg}{hh}点半"
    return f"{seg}{hh}点" if m == "00" else f"{seg}{hh}点{int(m)}分"


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
        self._last_list: Optional[str] = None   # 上次清单播报对象（"删第2条"回指）
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

        # nlu.enabled=false：本地理解全线让位（快速通道/场景契约/查询族/场景与
        # 自动化本地承接一律不参与），只剩 LLM 兜底——开关必须说到做到，否则
        # 用户"关了 NLU 却还被本地拦截"就是配置与行为打架（本次修订核心之一）。
        if not self.settings.get("nlu.enabled", True):
            if self.agent and self.agent.enabled:
                llm = await self._llm(text, origin, on_sentence)
                if llm:
                    return llm
            return Reply(_NLU_OFF_TEXT, "fallback", ok=False, trace=["NLU已关闭"])

        # v1.0.30 语音创建承接（零 LLM）：「当我说X就Y」/「当[事件]就Y」。
        # 必须前置于复合切分——创建句内的"并/然后"属于 Y 子句内容，不能被链发。
        created = await self._voice_creation(text, origin)
        if created is not None:
            return created

        # P2-12 复合句：分句全命中才链发，否则原样回退单发路径
        chain = await self._try_compound(text, origin)
        if chain is not None:
            return chain

        # ⓪①②③④ klar 引擎与 T0/T1/场景并行判定，三层裁决（见模块头）
        fp_plan, kl_plan = await self._match_pair(text)
        plan = select_primary_plan(fp_plan, kl_plan)
        plan = self._apply_context(plan, text, origin)
        if plan:
            ob = self._overbroad_area_target(plan)
            if ob:
                logger.info("[级联] 过宽目标拦截（%s 全部设备）：%s", ob, plan.args)
                return Reply(self._overbroad_say(ob), "clarify", ok=False,
                             trace=[f"过宽目标拦截:{ob}"])
            ask = self._confirm_ask(plan, origin)
            if ask is not None:
                return ask
            ok, speech = await self.executor.run(plan)
            exec_risk = self._exec_risk()          # 本次执行是否可能已生效（防复议重放）
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
                    exec_risk = exec_risk or self._exec_risk()
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
            # 快速通道（klar+慧尖意图）双双用尽：LLM 配置了就复议，没配如实播失败。
            # 安全闸（本次修订）：已部分生效（多步链）或结果不确定（超时/连接/5xx）
            # → 绝不复议——LLM 拿原句重做会把相对量动作（+10 亮度）叠加第二遍，
            # "多一层兜底"不能变成"多一次动作"。
            if self.agent and self.agent.enabled and not exec_risk:
                logger.info("[级联] 快速通道执行失败 → LLM 复议: %s", speech)
                llm = await self._llm(text, origin, on_sentence)
                if llm:
                    return llm
            elif exec_risk and self.agent and self.agent.enabled:
                trace.append("复议跳过:可能已执行")
                logger.info("[级联] 跳过 LLM 复议（本地执行可能已部分生效/结果不确定）")
            self._remember_turn(origin, text, speech)
            return Reply(speech, plan.source, False, trace)
        # ⑤ 查询族
        try:
            ans = await self.query.answer(text)
        except Exception:
            logger.exception("[级联] 查询族异常")
            ans = None
        if ans:
            self._remember_turn(origin, text, ans)   # 查询轮也进 LLM 历史，防跨轮失忆
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

    # ── 语音创建（v1.0.30 零 LLM，060401 收编优化）────────────────
    # 旧架构这两句式的解析归 LLM function calling（custom_llm_api 时代）；
    # 本地级联承接后，动作子句 Y 复用 fast_path 判定——「能执行的句子才能
    # 进场景」，任一子句听不懂整单拒绝（不建半成品），全中才产
    # HassCreateVoiceScene / HassCreateAutomation 走既有 handle_intent 入库。
    async def _voice_creation(self, text: str, origin: str) -> Optional[Reply]:
        if not self.settings.get("nlu.creation_enabled", True):
            return None
        c = creation.parse(text)
        if c is None:
            return None
        if c["kind"] == "delete_scene":
            return await self._scene_delete(c, text, origin)
        if c["kind"] in ("list_scenes", "list_automations"):
            return await self._object_list(c, text, origin)
        if c["kind"] == "delete_automation":
            return await self._automation_delete(c, text, origin)
        if c["kind"] == "modify_scene":
            return await self._scene_modify(c, text, origin)
        if c["kind"] == "modify_automation":
            return await self._automation_modify(c, text, origin)
        if c["kind"] == "delete_index":
            return await self._index_delete(c, text, origin)
        built = await self._build_actions(c, text)
        if isinstance(built, Reply):
            return built
        actions, y_say = built
        if c["kind"] == "scene":
            x = c["trigger_phrase"]
            if x in (self.scenes.triggers or []):
                # 集成侧 create_scene 有权威 dup 闸，这里只是缓存命中的友好前置
                return Reply(f"「{x}」这个场景已经有了，要换动作就说"
                             f"「把场景{x}改成……」",
                             "creation", ok=False, trace=[f"场景重名:{x}"])
            plan = Plan(intent="HassCreateVoiceScene",
                        args={"trigger_phrase": x, "actions": actions},
                        source="creation", utterance=text,
                        trace=[f"创建场景:{x}→{len(actions)}动作"])
            ok, _ = await self.executor.run(plan)
            if not ok:
                return Reply("抱歉，场景没创建成功，稍后再试", "creation",
                             ok=False, trace=plan.trace)
            await self.scenes.refresh(force=True)   # 触发词即刻可用，不等 60s 缓存
            say = f"好的，语音场景已创建，以后说「{x}」，就{y_say}"
        else:
            plan = Plan(intent="HassCreateAutomation",
                        args={"trigger": c["trigger"], "actions": actions},
                        source="creation", utterance=text,
                        trace=[f"创建自动化:{c['trigger']}→{len(actions)}动作"])
            ok, _ = await self.executor.run(plan)
            if not ok:
                return Reply("抱歉，自动化没创建成功，稍后再试", "creation",
                             ok=False, trace=plan.trace)
            idx = await self._automation_count()      # 播报编号=删除锚点（fail-open）
            say = f"好的，语音自动化已创建：{self._cond_say(c)}，就{y_say}"
            if idx:
                say += f"。要改它就说「删除自动化{idx}」再说一句新的"
        self._remember_turn(origin, text, say)
        return Reply(say, "creation", True, plan.trace)

    def _scope_action_area(self, plan: Plan, clause: str, c: dict) -> Plan:
        """动作句没写区域时，继承触发条件里的区域（2026-09-15 用户令）：
        「当客厅温度超过28度就开灯」＝**客厅的灯**；只有用户明说「打开所有灯/
        全部灯/全屋的灯」才保留全屋语义。设备名自带区域（"书房空调"）或句子本身
        已写区域的零改动；推断出的"区域"不是真区域（如"客厅灯温度"）也不注入。"""
        if plan is None or getattr(plan, "whole_house", False) or is_whole_house(clause):
            return plan
        desc = str(c.get("desc") or "")
        if not desc:
            return plan
        area, _rest = T.extract_prefix(desc)
        if not area:
            return plan
        known = self._known_areas()
        if known and area not in known:
            return plan
        targets = (plan.args or {}).get("target")
        if not isinstance(targets, list) or not targets:
            return plan
        for t in targets:
            if not isinstance(t, dict) or str(t.get("area") or "").strip():
                continue
            devs = [d for d in (t.get("devices") or []) if isinstance(d, dict)]
            if not devs:
                continue
            names = [str(d.get("name") or "") for d in devs]
            if any(n and any(a in n for a in known) for n in names):
                continue                     # 设备名自带区域（"书房空调"）→ 不覆盖
            t["area"] = area
            plan.trace = (plan.trace or []) + [f"动作区域继承:{area}"]
        return plan

    async def _scene_delete(self, c: dict, text: str, origin: str) -> Reply:
        """语音删场景（v1.0.33 本地句，零 LLM）：只认带名字的精准删除；
        触发词表在缓存里查无 → 如实报不瞎删。裸删（不带名字）本地列清单+
        编号引导，不推到 LLM（无 LLM 时也必须能用）。"""
        x = c["trigger_phrase"]
        if not x:
            listed = await self._object_list({"kind": "list_scenes"}, text, origin)
            if self._last_list is None:              # 空清单：已给创建引导
                return listed
            listed.text = (f"要删哪个场景？{listed.text}。"
                           f"说「删除场景名字」或「删第N条」都行")
            return listed
        if x not in (self.scenes.triggers or []):
            return Reply(f"没有找到叫「{x}」的语音场景；全部场景可在管理页"
                         f"「场景/自动化」查看。", "creation", ok=False,
                         trace=[f"删除未命中:{x}"])
        plan = Plan(intent="HassDeleteVoiceScene", args={"trigger_phrase": x},
                    source="creation", utterance=text, trace=[f"删除场景:{x}"])
        ok, _ = await self.executor.run(plan)
        if not ok:
            return Reply(f"抱歉，场景「{x}」没删除成功，稍后再试。", "creation",
                         ok=False, trace=plan.trace)
        await self.scenes.refresh(force=True)
        say = f"好的，语音场景「{x}」已删除"
        self._remember_turn(origin, text, say)
        return Reply(say, "creation", True, plan.trace)

    async def _build_actions(self, c: dict, text: str):
        """Y 子句 → 可执行动作表（能执行的句子才能进场景）。
        返回 (actions, y_say)，任一子句听不懂 → 直接返回拒收 Reply。"""
        actions: list[dict] = []
        echo_parts: list[str] = []
        for clause in creation.split_actions(c["y"]):
            plan = await self._match_fp(clause)
            ob = self._overbroad_area_target(plan)
            if ob:
                # "当我说回家就客厅开灯"这类子句：区域被当设备名 → 会把整片区域
                # 的设备（含门锁/开关）都写进场景，绝不入库
                logger.info("[创建] 子句过宽目标拒收 %r（%s 全部设备）", clause, ob)
                return Reply(self._overbroad_say(ob), "creation", ok=False,
                             trace=[f"创建拒收:过宽目标:{ob}"])
            if plan is None or plan.intent not in creation.ACTIONABLE_INTENTS:
                plan = await self._retry_with_area(clause, c)
            if plan is None or plan.intent not in creation.ACTIONABLE_INTENTS:
                logger.info("[创建] 子句拒收 %r → 整单作废（原句 %r）", clause, text)
                return Reply(self._creation_reject(c, clause), "creation",
                             ok=False, trace=[f"创建拒收:{clause!r}"])
            plan = self._scope_action_area(plan, clause, c)
            actions.append({"intent": plan.intent, "params": copy.deepcopy(plan.args)})
            echo_parts.append(clause)
        return actions, "，".join(echo_parts)

    async def _automation_rows(self):
        """拉语音自动化列表（store 序=创建序，编号锚点）。走 run_raw 拿原始
        dict（executor.run 会把结果折叠成话术串，列表数据不能走它）。失败 None。"""
        plan = Plan(intent="HassListAutomations", args={}, source="creation",
                    utterance="", trace=["列出自动化"])
        ok, res = await self.executor.run_raw(plan)
        if not ok or not isinstance(res, dict):
            return None
        rows = res.get("automations")
        return rows if isinstance(rows, list) else None

    async def _automation_count(self) -> int:
        try:
            rows = await self._automation_rows()
            return len(rows) if rows is not None else 0
        except Exception:
            return 0

    @staticmethod
    def _auto_index(rows: list, row: dict) -> int:
        """行在列表里的 1-based 编号——按**对象身份**取，不用 list.index：两条内容
        完全相同的自动化（同 trigger/同动作）用 == 比较会指到先出现那条，播报编号
        就张冠李戴（删除仍按 automation_id 走，不会删错，但话术会骗人）。"""
        for i, r in enumerate(rows, 1):
            if r is row:
                return i
        try:
            return rows.index(row) + 1
        except ValueError:
            return 0

    def _auto_say(self, i: int, a: dict) -> str:
        """单条自动化 → 「1，当…就…」（编号=删除锚点）。"""
        from .admin_api import _hv_trigger_cn, _hv_action_cn
        trig = _hv_trigger_cn(a.get("trigger") or {})
        acts = "；".join(filter(None, (_hv_action_cn(x)
                                   for x in (a.get("actions") or [])[:2])))
        head = f"{i}，" if i else ""
        return f"{head}{trig}的时候{acts or '执行动作'}"

    async def _object_list(self, c: dict, text: str, origin: str) -> Reply:
        """语音列场景/列自动化（v1.0.34 本地，零 LLM）。"""
        if c["kind"] == "list_scenes":
            rows = self.scenes.all() if hasattr(self.scenes, "all") else []
            if not rows:
                await self.scenes.refresh(force=True)
                rows = self.scenes.all() if hasattr(self.scenes, "all") else []
            if not rows:
                self._last_list = None
                say = "你还没有创建过语音场景，说「当我说晚安，就关闭客厅灯」就能创建一个"
            else:
                from .admin_api import _hv_action_cn
                parts = []
                # 编号必须用**行位置**（enumerate 从 1 起），与 _index_delete 的
                # rows[n-1] 同一套序号；否则「删第N条」会指向另一条场景。
                # 1.0.34/1.0.36 公告与 _scene_delete 引导都写着"带编号"，播报此前漏了。
                for i, s in enumerate(rows[:5], 1):
                    if not isinstance(s, dict):
                        continue
                    acts = "、".join(filter(None, (_hv_action_cn(a)
                                              for a in (s.get("actions") or [])[:2])))
                    tp = str(s.get("trigger_phrase") or s.get("name") or "")
                    body = f"{tp}就{acts}" if acts else tp
                    parts.append(f"{i}，{body}")
                more = f"；其余{len(rows)-5}个在管理页看" if len(rows) > 5 else ""
                say = f"目前有{len(rows)}个语音场景：{'；'.join(parts)}{more}"
                self._last_list = "scene"
            self._remember_turn(origin, text, say)
            return Reply(say, "creation", True, [f"列场景:{len(rows)}"])
        rows = await self._automation_rows()
        if rows is None:
            return Reply("暂时读不到自动化列表，稍后再试", "creation", ok=False,
                         trace=["列自动化失败"])
        if not rows:
            self._last_list = None
            say = ("你还没有语音自动化，说「当客厅温度超过28度就打开空调」"
                   "或「每天早上7点帮我打开客厅窗帘」就能创建")
        else:
            items = "；".join(self._auto_say(i, a)
                              for i, a in enumerate(rows[:5], 1)
                              if isinstance(a, dict))
            more = f"，其余{len(rows)-5}条在管理页看" if len(rows) > 5 else ""
            say = f"目前有{len(rows)}条语音自动化：{items}{more}"
            self._last_list = "automation"
        self._remember_turn(origin, text, say)
        return Reply(say, "creation", True, [f"列自动化:{len(rows)}"])

    async def _index_delete(self, c: dict, text: str, origin: str) -> Reply:
        """「删第N条」= 上一次清单播报的第 N 项（清单说完紧跟着删的自然交互）。
        无上下文如实反问；执行后清上下文（清单已失效，防旧编号误删）。"""
        n = c["n"]
        kind = self._last_list
        if kind is None:
            return Reply(f"要删的第{n}条是什么？先说「有哪些自动化」或「有哪些场景」"
                         f"听一遍编号清单，再说「删第{n}条」",
                         "creation", ok=False, trace=["删第N条:无清单上下文"])
        if kind == "automation":
            self._last_list = None
            return await self._automation_delete({"target": str(n)}, text, origin)
        rows = self.scenes.all() if hasattr(self.scenes, "all") else []
        if not rows or n > len(rows):
            await self.scenes.refresh(force=True)
            rows = self.scenes.all() if hasattr(self.scenes, "all") else []
        if n > len(rows):
            return Reply(f"场景一共{len(rows)}个，没有第{n}条",
                         "creation", ok=False, trace=[f"删第{n}条:越界"])
        x = str(rows[n - 1].get("trigger_phrase") or "")
        self._last_list = None
        return await self._scene_delete({"trigger_phrase": x}, text, origin)

    def _auto_hits(self, rows: list, tgt) -> list:
        """序号/关键词 → 命中的自动化行（删与改共用同一套匹配纪律）。"""
        hit: list = []
        if isinstance(tgt, int):
            if 1 <= tgt <= len(rows):
                hit = [rows[tgt - 1]]
            return hit
        from .admin_api import _hv_action_cn
        kw = str(tgt)
        for a in rows:
            if not isinstance(a, dict):
                continue
            trig = a.get("trigger") or {}
            hay = str(trig.get("entity_id") or "") + str(trig.get("at") or "") + \
                "；".join(filter(None, (_hv_action_cn(x)
                                      for x in (a.get("actions") or []))))
            if kw in hay:
                hit.append(a)
        return hit

    async def _automation_delete(self, c: dict, text: str, origin: str) -> Reply:
        """语音删自动化（v1.0.34）：序号/关键词精准命中才删；裸删列编号引导。"""
        rows = await self._automation_rows()
        if rows is None:
            return Reply("暂时读不到自动化列表，稍后再试", "creation", ok=False,
                         trace=["删自动化:列表失败"])
        if not rows:
            return Reply("你还没有语音自动化，不用删除", "creation", True,
                         trace=["删自动化:空"])
        tgt = creation.auto_target(c.get("target", ""))
        if tgt is None:                            # 裸删：列编号，让用户点名
            items = "；".join(self._auto_say(i, a) for i, a in enumerate(rows[:5], 1)
                              if isinstance(a, dict))
            self._last_list = "automation"          # 清单编号即锚点，「删第N条」直接可用
            return Reply(f"要删哪一条？你说「删除自动化序号」：{items}",
                         "creation", True, trace=["删自动化:引导"])
        hit = self._auto_hits(rows, tgt)
        if not hit:
            return Reply(f"没有找到和「{tgt}」对应的语音自动化，"
                         f"说「有哪些自动化」可以看清单",
                         "creation", ok=False, trace=[f"删自动化未命中:{tgt}"])
        if len(hit) > 1:
            items = "；".join(self._auto_say(self._auto_index(rows, h), h)
                              for h in hit[:5])
            return Reply(f"「{tgt}」匹配到{len(hit)}条，说得再具体些：{items}",
                         "creation", True, trace=[f"删自动化多义:{tgt}"])
        aid = str(hit[0].get("automation_id") or "")
        plan = Plan(intent="HassDeleteAutomation", args={"automation_id": aid},
                    source="creation", utterance=text, trace=[f"删自动化:{aid}"])
        ok, _ = await self.executor.run(plan)
        if not ok:
            return Reply("抱歉，自动化没删除成功，稍后再试", "creation", ok=False,
                         trace=plan.trace)
        say = f"好的，{self._auto_say(self._auto_index(rows, hit[0]), hit[0])}这条自动化已删除"
        self._remember_turn(origin, text, say)
        return Reply(say, "creation", True, plan.trace)

    async def _automation_modify(self, c: dict, text: str, origin: str) -> Reply:
        """语音改自动化（本地闭环补全，零 LLM）：新句是完整条件句 → 连触发条件
        一起换；只给动作 → 保留原触发条件只换动作；只给条件（"每天…点"）→ 只换
        条件动作不变。动作子句听不懂 → 整单拒绝，旧数据零改动（同创建纪律）。
        执行走集成 HassUpdateAutomation（trigger/actions 可给其一），不删旧建新。"""
        rows = await self._automation_rows()
        if rows is None:
            return Reply("暂时读不到自动化列表，稍后再试", "creation", ok=False,
                         trace=["改自动化:列表失败"])
        if not rows:
            return Reply("你还没有语音自动化，不用修改。说「当客厅温度超过28度"
                         "就打开空调」就能创建一个", "creation", True,
                         trace=["改自动化:空"])
        tgt = creation.auto_target(c.get("target", ""))
        if tgt is None:
            return Reply("要改哪一条？说「有哪些自动化」听一遍编号，再说"
                         "「把自动化1改成每天早上8点打开客厅灯」",
                         "creation", ok=False, trace=["改自动化:无目标"])
        hit = self._auto_hits(rows, tgt)
        if not hit:
            return Reply(f"没有找到和「{tgt}」对应的语音自动化，"
                         f"说「有哪些自动化」可以看清单",
                         "creation", ok=False, trace=[f"改自动化未命中:{tgt}"])
        if len(hit) > 1:
            items = "；".join(self._auto_say(self._auto_index(rows, h), h)
                              for h in hit[:5])
            return Reply(f"「{tgt}」匹配到{len(hit)}条，说得再具体些：{items}",
                         "creation", True, trace=[f"改自动化多义:{tgt}"])
        row = hit[0]
        idx = self._auto_index(rows, row)
        new_text = str(c.get("y") or "").strip()
        inner = creation.parse(new_text)
        trigger = desc = None
        y_text = new_text
        if isinstance(inner, dict) and inner.get("kind") == "automation":
            trigger = inner["trigger"]
            desc = str(inner.get("desc") or "")
            y_text = inner["y"]
        elif _TRIGGER_ONLY_RE.match(new_text):
            # 只给触发条件：借一次"条件+占位动作"解析取 trigger，动作表不动
            probe = creation.parse(new_text + "就打开客厅灯")
            if isinstance(probe, dict) and probe.get("kind") == "automation":
                trigger = probe["trigger"]
                desc = str(probe.get("desc") or "")
                y_text = ""
        actions: Optional[list] = None
        y_say = ""
        if y_text:
            if trigger is None:
                # 仅改动作：旧触发条件里的区域名（"客厅温度"→客厅）作为无目标
                # 动作句的区域继承来源，让「把自动化1的动作改成打开空调」可执行
                desc = str((row.get("trigger") or {}).get("entity_id") or "")
            built = await self._build_actions(
                {"kind": "automation", "y": y_text, "desc": desc or ""}, text)
            if isinstance(built, Reply):
                return built                        # 动作听不懂：旧数据原样不动
            actions, y_say = built
        if trigger is None and actions is None:
            return Reply(self._creation_reject(
                {"kind": "automation"},
                f"{new_text}（要带上触发条件或动作）"), "creation", ok=False,
                trace=["改自动化:无可改内容"])
        args: dict = {"automation_id": str(row.get("automation_id") or "")}
        if trigger:
            args["trigger"] = trigger
        if actions is not None:
            args["actions"] = actions
        plan = Plan(intent="HassUpdateAutomation", args=args, source="creation",
                    utterance=text,
                    trace=[f"改自动化:{idx}" + ("（含触发条件）" if trigger else "（仅动作）")])
        ok, _ = await self.executor.run(plan)
        if not ok:
            return Reply("抱歉，自动化没改成功，稍后再试", "creation", ok=False,
                         trace=plan.trace)
        if not y_say:                               # 只换触发条件
            say = (f"好的，自动化{idx}的触发条件已改成："
                   f"{self._cond_say({'trigger': trigger, 'desc': desc or ''})}"
                   f"（动作不变）")
        elif trigger:
            say = (f"好的，自动化{idx}已改成："
                   f"{self._cond_say({'trigger': trigger, 'desc': desc or ''})}"
                   f"，就{y_say}")
        else:
            say = f"好的，自动化{idx}的动作已改成：就{y_say}（触发条件不变）"
        self._remember_turn(origin, text, say)
        return Reply(say, "creation", True, plan.trace)

    async def _scene_modify(self, c: dict, text: str, origin: str) -> Reply:
        """语音改场景（v1.0.34）：预检新动作全可执行 → 删旧 → 建新。
        删成建败的窄窗如实报并提示重建（预检已把失败面压到集成掉线级别）。"""
        x = c["trigger_phrase"]
        if x not in (self.scenes.triggers or []):
            return Reply(f"没有找到叫「{x}」的语音场景；说「有哪些场景」可以看清单",
                         "creation", ok=False, trace=[f"改场景未命中:{x}"])
        built = await self._build_actions(c, text)
        if isinstance(built, Reply):
            return built                            # 新动作听不懂：旧场景原样不动
        actions, y_say = built
        dp = Plan(intent="HassDeleteVoiceScene", args={"trigger_phrase": x},
                  source="creation", utterance=text, trace=[f"改场景删旧:{x}"])
        ok, _ = await self.executor.run(dp)
        if not ok:
            return Reply(f"抱歉，场景「{x}」没改成交（旧的还在，没动它）",
                         "creation", ok=False, trace=dp.trace)
        cp = Plan(intent="HassCreateVoiceScene",
                  args={"trigger_phrase": x, "actions": actions},
                  source="creation", utterance=text, trace=[f"改场景建新:{x}"])
        ok2, _ = await self.executor.run(cp)
        await self.scenes.refresh(force=True)
        if not ok2:
            say = (f"场景「{x}」的旧动作已删除，但新动作没保存成功——"
                   f"请再说一句「当我说{x}，就{y_say}」")
            return Reply(say, "creation", ok=False, trace=cp.trace)
        say = f"好的，场景「{x}」已改成：就{y_say}"
        self._remember_turn(origin, text, say)
        return Reply(say, "creation", True, cp.trace)

    async def _retry_with_area(self, clause: str, c: dict):
        """Y 子句无目标而事件描述带区域（"当客厅温度超28度就打开空调"）→
        区域继承重试一次；仍听不懂照旧拒绝。

        ⚠ 区域继承必须产出**窄目标**：`f"{area}{clause}"` 在 fast_path 会走
        t0 前缀分支，把区域当成设备名（"客厅打开空调"→ name=客厅/domains 空），
        执行侧按名字子串命中整个客厅的设备——2026-09-15 实证：这种目标会把客厅
        所有设备一起打开。故命中"区域当名字"的形态一律作废，改用动词+区域+设备
        语序重排（"打开客厅空调"）；重排不成 → 不继承、如实拒收。"""
        desc = str(c.get("desc") or "")
        if not desc:
            return None
        area, rest = T.extract_prefix(desc)
        if not area:
            return None
        merged = await self._match_fp(f"{area}{clause}")
        if merged is not None and (merged.intent not in creation.ACTIONABLE_INTENTS
                                   or _area_as_name(merged, area)):
            merged = None
        if merged is None:
            alt = _reorder_area(clause, area)
            if alt:
                merged = await self._match_fp(alt)
                if merged is not None and (
                        merged.intent not in creation.ACTIONABLE_INTENTS
                        or _area_as_name(merged, area)):
                    merged = None
        if merged is None:
            return None
        merged.trace = (merged.trace or []) + [f"区域继承:{area}"]
        return merged

    def _cond_say(self, c: dict) -> str:
        """trigger 结构 → 自含播报短语（每分支带"的时候"，不回显 JSON）。"""
        trig = c["trigger"]
        if trig.get("at"):
            return f"每天{_at_say(trig['at'])}的时候"
        d = str(c.get("desc") or trig.get("entity_id") or "")
        if trig.get("to") is not None:
            return f"{d}检测到人的时候" if trig["to"] == "on" else f"{d}没人的时候"
        if "above" in trig:
            u = "度" if "温度" in d else ""
            return f"{d}超过{trig['above']:g}{u}的时候"
        if "below" in trig:
            u = "度" if "温度" in d else ""
            return f"{d}低于{trig['below']:g}{u}的时候"
        return f"{d}变化的时候"

    def _creation_reject(self, c: dict, clause: str) -> str:
        demo = ("可以这样说：当我说晚安，就关闭卧室灯"
                if c["kind"] == "scene" else
                "可以这样说：当客厅温度超过28度，就打开空调")
        return f"抱歉，「{clause}」这句我没听懂具体要做什么，先不创建了。{demo}"

    # ── P2-12 复合句 ───────────────────────────────────────────
    async def _try_compound(self, text: str, origin: str) -> Optional[Reply]:
        if not self.settings.get("dialog.chain_enabled", True):
            return None
        # 场景契约恒最高优先（模块头裁决①）：整句就是某个触发词时**绝不切分**——
        # 契约句必须走单发通路交给 fast_path 的 scene 判定，否则「当我说X」会被
        # 连排切分当设备指令做掉（与 fast_path 侧同一纪律）。
        # getattr：部分单测手工装配的 Pipeline 没有 scenes 字段（真机恒有）。
        _sc = getattr(self, "scenes", None)
        if _sc is not None and _sc.check(text) == text:
            return None
        clauses = split_compound(text)
        if not clauses:
            # 无连接词的动词连排（2026-09-10 真机：连排双动作只执行了后一个）
            clauses = creation.serial_clauses(text)
        if not clauses:
            # 并列宾语「打开展厅内倒窗和推拉窗」（2026-09-21 用户令第③点）：
            # 共享动词多设备句必须链发；T0 单发只吃一个并谎报「办好了」=半执行。
            # 任一分句听不懂→整句拒猜（fast_path 同形守卫兜住回退单发那一步）。
            clauses = T.coord_clauses(text)
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
            ob = self._overbroad_area_target(p)
            if ob:
                # 链中分句过宽：整句不执行，直接引导（同单发口径）
                return Reply(self._overbroad_say(ob), "clarify", ok=False,
                             trace=[f"链内过宽目标拦截:{ob}"])
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
        if _is_wholehouse_args(args):
            # v1.0.40（A2）：显式全屋绝不被上一轮目标替换（说"所有灯"就必须是全屋）
            return plan
        if _has_explicit_target(args):
            # 明示目标（非代词/回指解析）也补卫星区域——v1.0.20 空间化此前被
            # 这道早退挡住，"开灯"永不落本区域（2026-09-12 探针实锤）。
            if not self._is_anaphoric(plan, text):
                self._apply_spatial(plan, args, origin)
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
            self._apply_spatial(plan, args, origin)
        return plan

    @staticmethod
    def _is_anaphoric(plan: Plan, text: str) -> bool:
        """该计划的目标是否来自代词/回指解析（此类目标不得再叠卫星区域）。"""
        return (any(("代词目标" in t or "回指→" in t or "链内回指" in t)
                    for t in plan.trace)
                or is_pronoun(text) or "它" in text or "们" in text)

    def _apply_spatial(self, plan: Plan, args: dict, origin: str) -> None:
        """卫星区域空间化：只补缺（明示区域优先），永不发 area-only 目标。

        2026-09-12 三端深挖两坑同修：①此前 HUIJIAN_ONLY 分支发 {"area": x}
        无 devices 键 → 集成端 target["devices"] 必 KeyError；②Turn* 因早退
        恒不命中 → "开灯"不落本区域。
        """
        area = (self.settings.get("spatial.satellite_areas") or {}).get(origin)
        if not area:
            return
        if getattr(plan, "whole_house", False):
            return                       # 显式全屋（"打开所有灯"）绝不被缩回本房间
        targets = args.get("target")
        if isinstance(targets, list) and targets:
            # 只给"泛类词"目标补区域（灯/窗帘/空调…），指名道姓的设备句零影响
            # ——原设计语义如此，且避免把「打开射灯」这类明示句误缩到本房间。
            changed = False
            for t in targets:
                if not isinstance(t, dict) or t.get("area"):
                    continue
                names = [str(d.get("name") or "") for d in (t.get("devices") or [])
                         if isinstance(d, dict)]
                if names and all(n in _GENERIC_DEVICE_WORDS for n in names if n):
                    t["area"] = area
                    changed = True
            if changed:
                plan.trace.append(f"空间化:卫星→{area}")
            return
        if plan.intent in HUIJIAN_ONLY_INTENTS or "target" in args:
            args["target"] = [{"area": area,
                               "devices": [{"domains": _SPATIAL_DOMAIN.get(plan.intent, [])}]}]
            plan.trace.append(f"空间化:卫星→{area}")
        elif "area" in args and not args.get("area"):
            args["area"] = area
            plan.trace.append(f"空间化:卫星→{area}")

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

    def _exec_risk(self) -> bool:
        """最近一次 Executor.run 是否"可能已经生效"——部分步骤已落地（多步链
        中途失败）或失败原因不确定（超时/连接/5xx：HA 可能已执行只是回执丢了）。
        这类回合禁止交给 LLM 复议重做。执行桩无该状态时按 False（保守放行）。"""
        st = getattr(self.executor, "last_run", None)
        if not isinstance(st, dict):
            return False
        try:
            if int(st.get("applied") or 0) > 0:
                return True
            return bool(st.get("indeterminate"))
        except (TypeError, ValueError):
            return False

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

    def _known_areas(self) -> set:
        """已知区域名集合（HA 区域注册表缓存 + 卫星区域映射）。取不到=空集，
        闸门随之失效（宁可不拦，也不误拦）。"""
        areas: set = set()
        try:
            areas.update(str(v) for v in (getattr(self.ha, "_areas", {}) or {}).values())
        except Exception:
            pass
        try:
            areas.update(str(v) for v in
                         (self.settings.get("spatial.satellite_areas", {}) or {}).values())
        except Exception:
            pass
        return {a.strip() for a in areas if a and str(a).strip()}

    def _overbroad_area_target(self, plan: Optional[Plan]) -> Optional[str]:
        """target 只有"区域名当设备名"+空 domains（"客厅开灯"→name=客厅/domains=[]）
        → 执行侧按名字子串命中**该区域所有设备**（灯、窗帘、开关、门锁一起动，
        2026-09-15 实测复现）。命中返回区域名，安全返回 None。

        这类目标一律不执行、不入库，改用一句引导让用户说清设备——按项目既定
        纪律「比礼貌失败糟糕得多」处理。"""
        if plan is None:
            return None
        areas = self._known_areas()
        if not areas:
            return None
        tgt = (plan.args or {}).get("target")
        if not isinstance(tgt, list) or not tgt:
            return None
        for t in tgt:
            if not isinstance(t, dict) or str(t.get("area") or "").strip():
                continue
            devs = t.get("devices")
            if not isinstance(devs, list):
                continue
            # 逐台检查（不假设"只有一台"）：klar 多目标/复合目标里混进一个
            # "区域当设备名"同样会把整片区域带开，必须一并拦下
            for d in devs:
                if not isinstance(d, dict) or d.get("domains"):
                    continue
                nm = str(d.get("name") or "").strip()
                if nm and nm in areas:
                    return nm
        return None

    @staticmethod
    def _overbroad_say(area: str) -> str:
        return (f"「{area}」里设备不止一台，我不确定你要哪一台，这次先不动。"
                f"说具体点就行，比如「打开{area}的灯」或「打开{area}空调」")

    # ── P2-13 风险操作确认环 ───────────────────────────────────
    def _risky(self, plan: Plan) -> bool:
        if not self.settings.get("dialog.confirm_risky", True):
            return False
        return self._plan_has_risky_step(plan)

    @staticmethod
    def _plan_has_risky_step(plan: Plan) -> bool:
        """整案风险扫描（主步骤 + extra_steps 全查）。

        2026-09-22 审查批 C2 两修：
        1) 目标判据升级为 T.args_target_lock——klar grounded 形（args 只有
           entity_id=lock.* 的拼音实体 id、无中文）与全屋域形（domains 含 lock、
           名为空）此前旁路确认环，「解锁大门」被引擎接地后直接拔锁；
        2) 多分句 plan 的 extra_steps 此前完全不设防——「关灯并且解锁大门」
           主步 HassTurnOff light.x 不风险，第二步解锁裸奔。
        HassTurnOn×lock=上锁（D7 安全向），不在闸内。"""
        pairs = [(plan.intent, plan.args or {})]
        pairs += [(st.get("name"), st.get("args") or {})
                  for st in (getattr(plan, "extra_steps", None) or [])
                  if isinstance(st, dict)]
        for intent, args in pairs:
            if intent in _RISKY_INTENTS:
                return True
            if intent in ("TurnDeviceOff", "HassTurnOff", "HassToggle") \
                    and T.args_target_lock(args):
                return True                       # D7 反转语义：关锁=解锁
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
            # C2 配套：多步 plan 的问句按**风险步**取目标——主步是灯、第二步
            # 才解锁时，拿主步 args 问「解锁该设备」会问错对象。
            rargs = args
            for st in (getattr(plan, "extra_steps", None) or []):
                if not isinstance(st, dict):
                    continue
                sn, sa = st.get("name"), st.get("args") or {}
                if sn in _RISKY_INTENTS or (
                        sn in ("TurnDeviceOff", "HassTurnOff", "HassToggle")
                        and T.args_target_lock(sa)):
                    rargs = sa
                    break
            what = "、".join(_target_names(rargs) or _target_areas(rargs)
                             or ["该设备"])
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
        llm_on = bool(self.agent and self.agent.enabled)
        if not self.settings.get("nlu.enabled", True):
            # 本地理解已关：面板必须如实说"没有任何本地命中"，否则调试结论全错
            out: dict = {"nlu_enabled": False, "plan": None, "fast_path": None,
                         "klar": None, "llm_enabled": llm_on}
            if not llm_on:
                out["final"] = _NLU_OFF_TEXT
            return out
        fp_plan, kl_plan = await self._match_pair(text)   # 与真流量同构（并行）
        plan = select_primary_plan(fp_plan, kl_plan)

        def _dump(p):
            return None if p is None else {
                "intent": p.intent, "args": p.args, "source": p.source,
                "trace": p.trace, "speech": getattr(p, "speech", ""),
                "extra_steps": getattr(p, "extra_steps", []),
                "whole_house": bool(getattr(p, "whole_house", False))}
        out = {"nlu_enabled": True, "plan": _dump(plan),
               "fast_path": _dump(fp_plan), "klar": _dump(kl_plan)}
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
