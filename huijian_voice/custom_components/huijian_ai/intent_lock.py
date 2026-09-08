"""慧尖解锁/上锁意图处理器（v1.0.20 音乐批前补：fast_path 开解锁车道执行面）。

背景：fast_path 自 v1.0.20 产 HassUnlock/HassLock——但 HA core **没有**这两个
内置意图（intent_builtin 文档 + core 源码全仓检索零命中，2026-09-12 实查），
加载项 /api/intent/handle 打过来必然 Unknown。故在集成端按慧尖 target 形态
注册同族 handler（与 TurnDeviceOn/Off 同一分层：实体解析归集成、执行归服务）。

安全纪律：解锁令在加载项侧已过确认环（_RISKY_INTENTS）；本处理器只认 lock
域实体（名字命中非锁域不误按）；空目标="解锁（全屋）"只枚举 lock 域，
且绝不吞别的域。
"""
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.lock.const import (DOMAIN as LOCK_DOMAIN,
                                                 SERVICE_LOCK, SERVICE_UNLOCK)
from homeassistant.const import ATTR_ENTITY_ID
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
                error_msg, candidates = await match_intent_entities(
                    intent_obj, targets)
                if error_msg:
                    return error_msg
                states = [c.state for c in (candidates or [])]
            else:
                states = list(hass.states.async_all(LOCK_DOMAIN))
            entity_ids = sorted({s.entity_id for s in states
                                 if s.domain == LOCK_DOMAIN})
            if not entity_ids:
                return {"success": False, "error": "未找到可用的门锁设备"}
            await hass.services.async_call(
                LOCK_DOMAIN, self.lock_service,
                {ATTR_ENTITY_ID: entity_ids},
                context=intent_obj.context,
                blocking=True,
                timeout=self.service_timeout,
            )
            names = [str((s.attributes or {}).get("friendly_name")
                         or s.entity_id)
                     for s in states if s.entity_id in entity_ids]
            return {"success": True, "states": [
                {"name": n, "success": True} for n in names]}
        except Exception as exc:            # 永不抛（2026-09-11 真机 500 教训）
            _LOGGER.exception("Lock intent %s failed", self.intent_type)
            return {"success": False, "error": str(exc) or "锁服务调用异常"}


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
