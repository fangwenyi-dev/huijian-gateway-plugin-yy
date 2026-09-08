"""Klar 一级确定性 NLU 客户端（v1.0.8 三端复核后架构扩展）。

klar-ha-nlu（MIT, FABBricate-IT-Solutions/klar-ha-nlu）= Rust 规则引擎，
镜像内以 s6 服务跑在 loopback :10520（boot.sh 分发二进制、klar-engine.sh 拉起，
home graph 直读 /homeassistant/.storage）。本客户端只做 POST /api/v2/parse 的
消费与裁决，契约按 docs/en/api.md：

  decision ∈ execute|clarify|confirm|reject|chat|error——只有 execute 携带
  plan.steps（意图名 + 平铺 slots），非 execute 不得出现任何意图数据。

分派纪律（一级 ≠ 独占）：
  • 白名单接管：仅标准「控制族」（KLAR_CONTROL_INTENTS，HA 内置 handler 直接
    执行，不依赖 huijian_ai 集成）。查询/媒体/计时/日历（HassGetState、
    HassMedia*、HassTimer*、Klar*Calendar* 等）一律放行给既有 查询族/LLM 链，
    不抢 ⑤ 查询族的中文专责。
  • decision≠execute 视为未命中：本产品语音链没有 clarify/confirm 二轮会话
    UX，宁漏勿错。
  • 多分句 plan 全 or 无：任一步不在白名单 → 整句不接管（部分执行丢语义）。
  • fail-open + 熔断：引擎缺失（首装无网/用户关下载）或连续挂点时 match()
    恒 None，级联照常走 TextCNN；5 连败熔断 300s，熔断期连 socket 都不碰，
    每句零延迟代价。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import aiohttp

from .fast_path import Plan

logger = logging.getLogger("huijian.nlu.klar")

DEFAULT_URL = "http://127.0.0.1:10520"
DEFAULT_LANGUAGE = "zh-CN"
DEFAULT_TIMEOUT_S = 2.0
DEFAULT_MIN_CONFIDENCE = 0.80
_MAX_TEXT = 500          # 引擎上限 4096，语音句远小于；防御截断
_FAIL_THRESHOLD = 5      # 连败 N 次进熔断
_COOLDOWN_S = 300.0

# 标准控制族：slot 直传 HA 内置 intent handler 即可执行。
# 名单外的 Klar 输出（查询/媒体/计时/日历/清单…）不接管，留给既有级联。
KLAR_CONTROL_INTENTS = frozenset({
    "HassTurnOn", "HassTurnOff", "HassToggle",
    "HassLightSet",
    "HassClimateSetTemperature", "HassClimateSetHumidity",
    "HassSetPosition",
    "HassLock", "HassUnlock",
    "HassFanSetSpeed", "HassFanSetPresetMode",
    "HassVacuumStart", "HassVacuumPause", "HassVacuumReturnToBase",
})


class KlarClient:
    """/api/v2/parse 消费者。永不抛异常：任何失败折叠成 None（未命中）。"""

    def __init__(self, settings, session=None):
        self.settings = settings
        self._session = session
        self._owns = session is None
        self._fails = 0
        self._cooldown_until = 0.0
        self._probing = False          # P1-9 半开单飞：冷却到期后只放一条快速探针
        self.last_error = ""
        self.parsed_ok = 0     # 观测计数（状态页/排障用）

    # ── 配置（点键，热改随 settings 保存即时生效）────────────────
    def _opt(self):
        s = self.settings
        return (
            bool(s.get("klar.enabled", True)),
            str(s.get("klar.url") or DEFAULT_URL).rstrip("/"),
            str(s.get("klar.language") or DEFAULT_LANGUAGE),
            float(s.get("klar.timeout_s") or DEFAULT_TIMEOUT_S),
            float(s.get("klar.min_confidence")
                  if s.get("klar.min_confidence") is not None
                  else DEFAULT_MIN_CONFIDENCE),
            str(s.get("klar.token") or ""),
        )

    # ── 解析 ────────────────────────────────────────────────────
    async def parse(self, text: str) -> Optional[dict]:
        enabled, url, lang, timeout, _min_conf, token = self._opt()
        if not enabled or not (text or "").strip():
            return None
        probe = False
        if self._fails >= _FAIL_THRESHOLD:
            now = time.monotonic()
            if now < self._cooldown_until:
                return None
            # P1-9 半开：冷却到期不再全量放行（引擎仍死时每句白付 2s 超时），
            # 单飞探针 + 收紧超时；失败立即重回冷却，成功即全量恢复。
            if self._probing:
                return None
            self._probing = probe = True
            timeout = min(timeout, 1.0)
        body: dict[str, Any] = {"text": text[:_MAX_TEXT], "language": lang}
        headers = {"x-klar-token": token} if token else {}
        try:
            if self._session is None:
                self._session = aiohttp.ClientSession()
            async with self._session.post(
                    f"{url}/api/v2/parse", json=body, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status != 200:
                    # 非 200 也计数为失败：持续 404 = 引擎版本漂移（V1 已退役）
                    # 或 URL 配错，和连不上一样应该熔断降级。
                    self._note_fail(f"HTTP {r.status}")
                    return None
                obj = await r.json(content_type=None)
                self._note_ok()
                if not isinstance(obj, dict):
                    return None
                self.parsed_ok += 1
                return obj
        except Exception as e:  # noqa: BLE001 —— fail-open 是本层的定义
            self._note_fail(f"{type(e).__name__}: {str(e)[:100]}")
            return None
        finally:
            if probe:
                self._probing = False

    def _note_fail(self, err: str) -> None:
        self.last_error = err
        self._fails += 1
        if self._fails >= _FAIL_THRESHOLD and time.monotonic() >= self._cooldown_until:
            self._cooldown_until = time.monotonic() + _COOLDOWN_S
            logger.warning("[Klar] 连续 %d 次失败（%s）→ 熔断 %.0fs，一级 NLU 暂退、"
                           "级联走本地 TextCNN（不影响语音链）",
                           self._fails, err, _COOLDOWN_S)

    def _note_ok(self) -> None:
        self._fails = 0
        self._cooldown_until = 0.0

    # ── 裁决 → Plan ─────────────────────────────────────────────
    @staticmethod
    def _slots_to_args(intent: dict) -> dict:
        args: dict[str, Any] = {}
        for slot in intent.get("slots") or []:
            if isinstance(slot, dict) and slot.get("name"):
                args[slot["name"]] = slot.get("value")
        return args

    def to_plan(self, obj: Optional[dict], text: str) -> Optional[Plan]:
        """契约裁决（纯函数，可单测）：execute + 白名单全命中 + 置信门 → Plan。"""
        if not obj:
            return None
        _en, _url, _lang, _to, min_conf, _tk = self._opt()
        decision = obj.get("decision")
        if not isinstance(decision, dict) or decision.get("type") != "execute":
            return None                      # clarify/confirm/reject/chat：宁漏勿错
        plan = obj.get("plan") or {}
        steps = [st for st in (plan.get("steps") or []) if isinstance(st, dict)]
        if not steps:
            return None
        conf = None
        for src in (plan, obj):
            if isinstance(src.get("confidence"), (int, float)):
                conf = float(src["confidence"])
                break
        if conf is not None and conf < min_conf:
            return None
        picked: list[tuple[str, dict]] = []
        for st in steps:
            intent = st.get("intent") or {}
            name = str(intent.get("name") or "")
            if name not in KLAR_CONTROL_INTENTS:
                return None                  # 多分句 all-or-nothing
            picked.append((name, self._slots_to_args(intent)))
        if not picked:
            return None
        speech = str(obj.get("speech") or "")
        first_name, first_args = picked[0]
        extra = [{"name": n, "args": a} for n, a in picked[1:]]
        trace = [f"klar:conf={conf:.2f}" if conf is not None else "klar:conf=?",
                 f"klar:steps={len(picked)}"]
        if speech:
            trace.append("klar:speech")
        return Plan(intent=first_name, args=first_args, source="klar",
                    utterance=text, trace=trace, speech=speech, extra_steps=extra)

    async def match(self, text: str) -> Optional[Plan]:
        try:
            obj = await self.parse(text)
        except Exception:  # noqa: BLE001 —— parse 本应不抛；双保险
            logger.exception("[Klar] parse 意外异常（fail-open）")
            return None
        try:
            return self.to_plan(obj, text)
        except Exception:  # noqa: BLE001 —— 响应形制漂移也不能伤语音链
            logger.exception("[Klar] 响应归一意外异常（fail-open）")
            return None

    async def close(self) -> None:
        if self._owns and self._session is not None:
            await self._session.close()
            self._session = None
