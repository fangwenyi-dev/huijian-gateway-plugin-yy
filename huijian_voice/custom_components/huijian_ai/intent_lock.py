"""慧尖解锁/上锁意图处理器（v1.0.20 音乐批前补：fast_path 开解锁车道执行面）。

背景：fast_path 自 v1.0.20 产 HassUnlock/HassLock——但 HA core **没有**这两个
内置意图（intent_builtin 文档 + core 源码全仓检索零命中，2026-09-12 实查），
加载项 /api/intent/handle 打过来必然 Unknown。故在集成端按慧尖 target 形态
注册同族 handler（与 TurnDeviceOn/Off 同一分层：实体解析归集成、执行归服务）。

安全纪律：解锁令在加载项侧已过确认环（_RISKY_INTENTS）；本处理器只认 lock
域实体（名字命中非锁域不误按）；空目标="解锁（全屋）"只枚举 lock 域，
且绝不吞别的域。
"""
import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.lock.const import DOMAIN as LOCK_DOMAIN
from homeassistant.const import (ATTR_ENTITY_ID, SERVICE_LOCK,
                                 SERVICE_UNLOCK)
from homeassistant.helpers import intent
from homeassistant.util.json import JsonObjectType

from .intent_helper import (match_intent_entities,
                            target_parameter_type)

_LOGGER = logging.getLogger(__name__)


class LockIntentBase(intent.IntentHandler):
    """HassUnlock / HassLock 共用骨架：target 形态 → lock 域实体 → 域服务。"""

    lock_service = SERVICE_LOCK          # 子类覆写
    service_timeout = 10

    @property
    def slot_schema(self) -> dict | None:
        # Optional：裸"解锁/上锁"（全屋锁）由本处理器枚举 lock 域，不报错
        return {vol.Optional("target"): target_parameter_type()}

    async def async_handle(self, intent_obj: intent.Intent) -> JsonObjectType:
        hass = intent_obj.hass
        try:
            slots = self.async_validate_slots(intent_obj.slots)
            targets = (slots.get("target") or {}).get("value") or []
            if targets:
                # 只认锁域：加载项对"大门/门锁"这类词的 domain_hint 为空
                # （2026-09-12 实测 14 个常见设备名），空 domains 在真机匹配会
                # 直接 DOMAIN 失败；这里统一收窄到 lock，既修匹配又绝不误动
                # 同名非锁实体（如名为"大门"的开关）。
                narrowed = []
                for t in targets:
                    t = dict(t)
                    devices = t.get("devices") or [{}]
                    t["devices"] = [dict(d, domains=[LOCK_DOMAIN])
                                    for d in devices]
                    narrowed.append(t)
                error_msg, candidates = await match_intent_entities(
                    intent_obj, narrowed)
                if error_msg:
                    return error_msg
                states = [c.state for c in (candidates or [])]
            else:
                states = list(hass.states.async_all(LOCK_DOMAIN))
            entity_ids = sorted({s.entity_id for s in states
                                 if s.domain == LOCK_DOMAIN})
            if not entity_ids:
                return {"success": False, "error": "未找到可用的门锁设备"}
            names: dict[str, str] = {}
            for s in states:
                if s.entity_id in entity_ids:
                    names.setdefault(
                        s.entity_id,
                        str((s.attributes or {}).get("friendly_name")
                            or s.entity_id),
                    )
            # v1.1.27（P0 根修 + 逐台记账）：
            # ① `_run_then_background` 只活在 HA core 的 DynamicServiceIntentHandler
            #    上，而本类继承的是 intent.IntentHandler ⇒ 旧版此处必 AttributeError、
            #    **锁服务从未下发**（参数求值在属性查找之后，协程都没建），返回体
            #    里还把 "'HassUnlockIntent' object has no attribute ..." 当话术；
            # ② 旧版整批一次 async_call + 逐台硬编码 {"success": True}：服务失败
            #    （ServiceValidationError/实体不支持/超时…）也报成功，且失败归不到
            #    具体哪台锁。现改为逐台下发、按真实调用结果回填——加载项 executor
            #    的解锁话术正是按 states[].success 取名字播报，谎报直达用户耳朵。
            results: list[dict[str, Any]] = []
            for entity_id in entity_ids:
                reason = await self._lock_one(hass, intent_obj.context, entity_id)
                row: dict[str, Any] = {
                    "name": names.get(entity_id, entity_id),
                    "success": reason is None,
                }
                if reason:
                    row["error"] = reason
                results.append(row)
            ok = [r for r in results if r["success"]]
            out: dict[str, Any] = {"success": bool(ok), "states": results}
            if len(ok) != len(results):
                out["partial_error"] = "；".join(
                    f"{r['name']}：{r.get('error') or '未成功'}"
                    for r in results if not r["success"]
                )
                if not ok:
                    out["error"] = out["partial_error"]
            return out
        except Exception as exc:            # 永不抛（2026-09-11 真机 500 教训）
            _LOGGER.exception("Lock intent %s failed", self.intent_type)
            return {"success": False, "error": str(exc) or "锁服务调用异常"}

    async def _lock_one(self, hass, context, entity_id: str) -> str | None:
        """单台下发：None = 已确认成功；否则给可复述的失败原因（绝不谎报）。"""
        task = hass.async_create_task(
            hass.services.async_call(
                LOCK_DOMAIN, self.lock_service,
                {ATTR_ENTITY_ID: entity_id},
                context=context,
                blocking=True,
            )
        )
        await self._run_then_background(task)
        if not task.done():
            return "服务调用超时未确认"        # 已转后台续跑，但本次不谎报成功
        if task.cancelled():
            return "服务调用被取消"
        exc = task.exception()
        if exc is not None:
            return str(exc) or exc.__class__.__name__
        return None

    async def _run_then_background(self, task: asyncio.Task[Any]) -> None:
        """与 intent_turn.py:467 同款的**本地**实现（超时后任务转后台续跑）。

        本类继承 intent.IntentHandler：core 的 `_run_then_background` 挂在
        DynamicServiceIntentHandler 上，拿不到（这正是旧版锁服务从未下发的根因），
        故本地补齐：超时支 add_done_callback 消费异常、"秒失败"支当场消费异常
        （免得刷成全局 "Task exception was never retrieved"）、CancelledError 支
        先取消再取一次。

        注：`asyncio.wait(timeout=…)` 的等待上限是**本地**超时，不是
        ServiceRegistry.async_call 的形参（core 2025.1.0 无该形参，
        test_ha_api_contract 有字面钉），故用具名局部量承载，避免与该钉的
        字面量撞车。
        """
        wait_timeout = self.service_timeout
        try:
            done, pending = await asyncio.wait({task}, timeout=wait_timeout)
            if pending:
                _LOGGER.error("Service call timed out: %s", task.get_name())

                def _log_exception(t: asyncio.Task) -> None:
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc:
                        _LOGGER.error(
                            "Background service call %s eventually failed: %s",
                            t.get_name(), exc, exc_info=exc,
                        )

                task.add_done_callback(_log_exception)
            elif done:
                for t in done:
                    if t.cancelled():
                        continue
                    exc = t.exception()
                    if exc is not None:
                        _LOGGER.warning(
                            "服务调用失败（已消费，不再产生未取回异常）%s: %s",
                            t.get_name(), exc,
                        )
        except asyncio.CancelledError:
            _LOGGER.debug("Service call was cancelled: %s", task.get_name())
            task.cancel()
            await asyncio.wait({task}, timeout=5)
            raise


class HassUnlockIntent(LockIntentBase):
    intent_type = "HassUnlock"
    description = (
        "Unlocks locks. Target format: "
        "target=[{devices: [{domains: ['lock'], name: '大门'}], area: ''}]. "
        "Empty/absent target unlocks all lock-domain entities."
    )
    lock_service = SERVICE_UNLOCK


class HassLockIntent(LockIntentBase):
    intent_type = "HassLock"
    description = (
        "Locks locks. Same target semantics as HassUnlock."
    )
    lock_service = SERVICE_LOCK
