"""HA Core 桥：意图执行（/api/intent/handle）+ 状态缓存 + 事件旁路。

凭证与端点（网关 discovery_proxy.py:44-46 同款定式）：
  base  = env HUIJIAN_HA_API  或 http://supervisor/core/api（homeassistant_api:true 注入）
  token = env HUIJIAN_HA_TOKEN 或 SUPERVISOR_TOKEN
执行通道实证：《语音集成源码盘点》§3.1——huijian_ai 的 14 个 handler 返回裸 dict，
async_handle 原样透传，因此本桥拿到的是结构化 {success, message/control_targets/actions…}
（非语句文本）。REST 端点按项目定案 POST /api/intent/handle {name,data}；若目标 HA 不
提供该形式（版本差异），自动回落 POST /api/intent/{name}（slots 平铺）。两式响应都
做防御式归一（_normalize_result）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

import aiohttp

from . import const

logger = logging.getLogger("huijian.ha")


class HAClient:
    def __init__(self, session: Optional[aiohttp.ClientSession] = None):
        self._session = session
        self._owns_session = session is None
        self.base = os.environ.get("HUIJIAN_HA_API", const.HA_API_DEFAULT).rstrip("/")
        self.token = os.environ.get(const.HA_TOKEN_ENV) or os.environ.get("SUPERVISOR_TOKEN", "")
        # 状态缓存
        self._states: dict[str, dict] = {}
        self._areas: dict[str, str] = {}          # area_id → 名称
        self._entity_area: dict[str, str] = {}    # entity_id → area 名
        self._cache_ts = 0.0
        self._CACHE_TTL = 5.0
        self._REG_TTL = 300.0
        self._reg_ts = 0.0
        self._lock = asyncio.Lock()
        self.last_error = ""
        self._reachable = False   # 真连通判据（区别于 ok=已配置；UI/状态面用这个）

    # ── 基础设施 ────────────────────────────────────────────────
    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15, connect=5),
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
            )
        await self.refresh_states(force=True)

    async def close(self) -> None:
        if self._owns_session and self._session:
            await self._session.close()
            self._session = None

    @property
    def ok(self) -> bool:
        """凭证已配置（调用前置条件；不代表可达——可达看 .reachable）。"""
        return bool(self.token)

    @property
    def reachable(self) -> bool:
        """最近一次 HA REST 是否有响应。初值 False，任何成功调用点亮（自愈式）。"""
        return self._reachable

    # ── 意图执行 ────────────────────────────────────────────────
    async def handle_intent(self, name: str, data: dict, timeout: float = 10.0) -> dict:
        """执行一个慧尖意图。返回归一化 dict：{success, message?, data?, raw}。
        永不抛异常——失败折叠成 {success:False, message:...}（供话术层用）。"""
        if not self.ok or self._session is None:
            return {"success": False, "message": "HA 通道未就绪", "raw": None}
        body = {"name": name, "data": data}
        try:
            async with self._session.post(
                    f"{self.base}/api/intent/handle", json=body,
                    timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status == 404:
                    return await self._handle_intent_legacy(name, data, timeout)
                self._reachable = r.status < 500
                payload = await r.text()
                return self._normalize_result(r.status, payload, name)
        except asyncio.TimeoutError:
            self.last_error = f"{name} 执行超时"
            return {"success": False, "message": "执行超时", "raw": None}
        except Exception as e:
            self._reachable = False
            self.last_error = str(e)
            logger.warning("[HA] intent %s 调用异常: %s", name, e)
            return {"success": False, "message": "HA 通道异常", "raw": None}

    async def _handle_intent_legacy(self, name: str, data: dict, timeout: float) -> dict:
        """回落形态：POST /api/intent/<name>，body=slots 平铺（HA REST 传统式）。"""
        try:
            async with self._session.post(
                    f"{self.base}/api/intent/{name}", json=data,
                    timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                payload = await r.text()
                return self._normalize_result(r.status, payload, name)
        except Exception as e:
            self.last_error = str(e)
            return {"success": False, "message": "HA 通道异常", "raw": None}

    @staticmethod
    def _normalize_result(status: int, payload: str, name: str) -> dict:
        """防御式归一。可能的响应形制：
        A) handler 裸 dict（定案实证）: {"success":true,"control_targets":[...]}
        B) IntentResponse 包裹: {"name":..,"response":{"success":..}} 或 {"speech_result":..}
        C) REST 错误: {"message":"..."} / 非 JSON
        """
        try:
            obj = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            obj = {"message": payload[:200]}
        if status in (400, 401, 403, 404):
            msg = obj.get("message") if isinstance(obj, dict) else str(obj)
            return {"success": False, "message": msg or f"HA 拒绝({status})", "raw": obj}
        if isinstance(obj, dict):
            if "success" in obj:
                return {**obj, "raw": obj}
            for key in ("response", "data", "result"):
                inner = obj.get(key)
                if isinstance(inner, dict) and ("success" in inner or "message" in inner):
                    return {**inner, "raw": obj}
            speech = obj.get("speech_result") or obj.get("speech")
            if isinstance(speech, list) and speech:
                speech = speech[0]
            return {"success": status < 300, "message": speech or "", "raw": obj}
        return {"success": status < 300, "message": str(obj), "raw": obj}

    # ── 状态缓存（查询族/LLM 设备表用）───────────────────────────
    async def refresh_states(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._cache_ts < self._CACHE_TTL:
            return
        async with self._lock:
            if not force and time.time() - self._cache_ts < self._CACHE_TTL:
                return
            if self._session is None:
                return
            try:
                async with self._session.get(f"{self.base}/api/states") as r:
                    self._reachable = r.status < 500
                    if r.status == 200:
                        states = await r.json()
                        self._states = {e["entity_id"]: e for e in states}
                        self._cache_ts = time.time()
                    else:
                        self.last_error = f"states {r.status}"
            except Exception as e:
                self._reachable = False
                self.last_error = str(e)
                logger.debug("[HA] states 刷新失败: %s", e)
            if time.time() - self._reg_ts > self._REG_TTL or not self._areas:
                await self._load_registries()

    async def _load_registries(self) -> None:
        try:
            async with self._session.get(f"{self.base}/api/config/area_registry/list") as r:
                if r.status == 200:
                    self._areas = {a["area_id"]: a.get("name", a["area_id"]) for a in await r.json()}
            async with self._session.get(f"{self.base}/api/config/entity_registry/list") as r:
                if r.status == 200:
                    ent_map = {}
                    for e in await r.json():
                        aid = e.get("area_id")
                        if aid:
                            ent_map[e["entity_id"]] = self._areas.get(aid, aid)
                    self._entity_area = ent_map
            self._reg_ts = time.time()
        except Exception as e:
            logger.debug("[HA] registry 加载失败: %s", e)

    async def states(self) -> dict[str, dict]:
        await self.refresh_states()
        return dict(self._states)

    def apply_state_event(self, event: dict) -> None:
        """WS 订阅回灌（state_changed）。"""
        if not event:
            return
        new = event.get("data", {}).get("new_state")
        eid = event.get("data", {}).get("entity_id")
        if eid:
            if new:
                self._states[eid] = new
            else:
                self._states.pop(eid, None)

    # ── 查询族支撑：按区域/域找实体 ──────────────────────────────
    async def find_entities(self, area: str = "", domains: tuple = (), name_contains: str = "") -> list[dict]:
        states = await self.states()
        out = []
        for eid, ent in states.items():
            dom = eid.split(".", 1)[0]
            if domains and dom not in domains:
                continue
            if area and self._entity_area.get(eid) != area and area not in (ent.get("attributes", {}) or {}).get("friendly_name", ""):
                continue
            if name_contains and name_contains not in (ent.get("attributes", {}) or {}).get("friendly_name", ""):
                continue
            out.append(ent)
        return out


    async def area_names(self) -> list[str]:
        await self.refresh_states()
        return sorted(set(self._areas.values()))

    async def get_config(self) -> dict:
        """/api/config（time_zone、version 等；失败返回 {}）。"""
        if not self.ok or self._session is None:
            return {}
        try:
            async with self._session.get(f"{self.base}/api/config") as r:
                if r.status == 200:
                    return await r.json()
        except Exception as e:
            logger.debug("[HA] config 读取失败: %s", e)
        return {}

    # ── 事件旁路（v4 §2 旁路：回合留痕）──────────────────────────
    async def fire_event(self, event_type: str, data: dict) -> None:
        if not self.ok or self._session is None:
            return
        try:
            async with self._session.post(f"{self.base}/api/events/{event_type}",
                                          json={"event_data": data}) as r:
                await r.read()
        except Exception as e:
            logger.debug("[HA] 事件 %s 发送失败: %s", event_type, e)
