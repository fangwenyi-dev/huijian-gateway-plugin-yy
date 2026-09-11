"""语音场景触发缓存（HassListVoiceScenes，60s TTL + 未命中强刷一次）。

契约（盘点 §3.1/§3.2）：huijian_ai 的列表意图返回裸 dict {success, scenes:[{trigger_phrase,...}]}；
触发走 HassTriggerVoiceScene {trigger_phrase}。fast_path v1.5 的 _refresh_scenes/_check_scene
逐语义移植：MCP 调用 → HA REST（ha_client），文本缓存判定（相等或前缀）不变。

体验批（2026-09）：TTL 到期后的刷新转后台（refresh_soon），不再压在语音关键路径上
——每 60s 有一句要同步等 HassListVoiceScenes 最坏 5s 的尖刺就此消掉。冷缓存例外：
从未成功加载过时仍同步拉一次（needs_blocking），只发生在启动后的头几句。
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
        self._last_attempt = 0.0
        self._loaded = False          # 是否成功加载过一次（冷启动判据）
        self._lock = asyncio.Lock()
        self._bg: asyncio.Task | None = None   # 后台刷新单飞（强引用，session F7b 纪律）

    def needs_blocking(self) -> bool:
        """仅「从未加载成功 且 距上次尝试超 TTL」才允许同步刷新（冷启动兜底）；
        稳态一律走后台，语音路径零等待。"""
        return (not self._loaded) and (time.time() - self._last_attempt >= TTL_S)

    async def refresh(self, force: bool = False) -> bool:
        """v1.0.41（F5）：返回显式成败。handle_intent 恒折叠不抛，上层 try/except
        是死代码——「集成掉线如实说明」必须看返回值：True=拉取成功（或缓存尚在
        TTL 内被跳过）；False=本轮拉取失败（旧缓存照常保留，HA 重启窗口不误清）。"""
        async with self._lock:
            now = time.time()
            if not force and now - self._last_refresh < TTL_S:
                return True
            self._last_attempt = now
            result = await self.ha.handle_intent("HassListVoiceScenes", {}, timeout=5.0) or {}
            if result.get("success") and isinstance(result.get("scenes"), list):
                self._scenes = [s for s in result["scenes"] if isinstance(s, dict)]
                self._triggers = [s.get("trigger_phrase", "") for s in self._scenes if s.get("trigger_phrase")]
                self._last_refresh = now
                self._loaded = True
                logger.debug("[场景] 缓存 %d 个触发词", len(self._triggers))
                return True
            # 失败保留旧缓存（HA 重启窗口期不误清空）
            return False

    def refresh_soon(self) -> None:
        """TTL 到期 → 后台单飞刷新（不 await，不抛，主路径立即用陈旧缓存）。"""
        if self._bg is not None and not self._bg.done():
            return
        if time.time() - self._last_refresh < TTL_S:
            return

        async def _job():
            try:
                await self.refresh(force=True)
            except Exception:      # noqa: BLE001 —— 后台刷新失败静默，下句再触发
                logger.debug("[场景] 后台刷新异常", exc_info=True)

        try:
            self._bg = asyncio.get_running_loop().create_task(_job())
        except RuntimeError:       # 无运行循环（单测直调 check()）：静默跳过
            pass

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

    def all(self) -> list[dict]:
        """场景全量原始 dict（Web「语音场景与自动化」页展示用；只读不回写）。"""
        return list(self._scenes)

    def name_for(self, phrase: str) -> str:
        for s in self._scenes:
            if s.get("trigger_phrase") == phrase:
                return s.get("name") or phrase
        return phrase
