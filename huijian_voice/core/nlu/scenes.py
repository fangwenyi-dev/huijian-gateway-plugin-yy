"""语音场景触发缓存（HassListVoiceScenes，60s TTL + 未命中强刷一次）。

契约（盘点 §3.1/§3.2）：huijian_ai 的列表意图返回裸 dict {success, scenes:[{trigger_phrase,...}]}；
触发走 HassTriggerVoiceScene {trigger_phrase}。fast_path v1.5 的 _refresh_scenes/_check_scene
逐语义移植：MCP 调用 → HA REST（ha_client），文本缓存判定（相等或前缀）不变。
"""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger("huijian.scenes")

TTL_S = 60.0


class SceneCache:
    def __init__(self, ha):
        self.ha = ha
        self._triggers: list[str] = []
        self._scenes: list[dict] = []
        self._last_refresh = 0.0
        self._lock = asyncio.Lock()

    async def refresh(self, force: bool = False) -> None:
        async with self._lock:
            now = time.time()
            if not force and now - self._last_refresh < TTL_S:
                return
            result = await self.ha.handle_intent("HassListVoiceScenes", {}, timeout=5.0)
            if result.get("success") and isinstance(result.get("scenes"), list):
                self._scenes = [s for s in result["scenes"] if isinstance(s, dict)]
                self._triggers = [s.get("trigger_phrase", "") for s in self._scenes if s.get("trigger_phrase")]
                self._last_refresh = now
                logger.debug("[场景] 缓存 %d 个触发词", len(self._triggers))
            # 失败保留旧缓存（HA 重启窗口期不误清空）

    def check(self, text: str) -> str | None:
        """等值或前缀命中（fast_path _check_scene 原语义）。"""
        for s in self._triggers:
            if text == s or text.startswith(s):
                return s
        return None

    async def verify_or_refresh(self, phrase: str) -> bool:
        """触发词最终核验：缓存命中，或强刷一次后命中（原双查语义）。"""
        if self.check(phrase) == phrase or phrase in self._triggers:
            return True
        await self.refresh(force=True)
        return phrase in self._triggers

    @property
    def triggers(self) -> list[str]:
        return list(self._triggers)

    def name_for(self, phrase: str) -> str:
        for s in self._scenes:
            if s.get("trigger_phrase") == phrase:
                return s.get("name") or phrase
        return phrase
