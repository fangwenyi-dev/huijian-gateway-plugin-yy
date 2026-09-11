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
import re
import time
from typing import Any, Optional

import aiohttp

from . import const

logger = logging.getLogger("huijian.ha")

# v1.0.41 安全（审查 S2）：意图名合法形态（HA 意图均为 CamelCase 标识符）。
# 一切含 `.`/`/`/空格/控制字符的名字在入口即拒——封死 legacy 回落的
# /api/intent/<name> 拼接被 dot-segment 归一化穿越的路径。
_INTENT_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


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

    # ── URL 拼接唯一真源 ────────────────────────────────────────
    def _url(self, path: str) -> str:
        """base + path 恒一个 /api（网关 gateway_discovery_proxy.ha_api 同款定式）。

        2026-09-11 真机实锤：默认 base=http://supervisor/core/api 本身就是 API 根，
        此前 7 处调用点又各拼 `/api/...` → 每个请求都变 /api/api/... → HA 404
        → 话术层把 "Not Found" 翻成「抱歉，没找到这个设备」，状态页/区域列表同灭，
        且 status<500 判据还把 404 点亮成"已连通"。此处把 base 尾部 /api 剥掉、
        由 path 统一带上：supervisor 形态（…/core/api）与直连形态（http://h:8123，
        HUIJIAN_HA_API 旧配置不设防）都恰好落一个 /api。
        """
        base = self.base[:-4] if self.base.endswith("/api") else self.base
        return f"{base}{path}"

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
        # v1.0.41 安全（审查 S2 第二层，全调用方兜底）：意图名必须是纯标识符。
        # legacy 回落把名字**裸拼进 URL 路径**（_url 是字符串拼接，yarl 会归一化
        # dot-segment），携 Supervisor 全权 token 即可被诱导打任意 HA REST 写端点
        # （实测 `x/../../services/light/turn_on` → /api/services/light/turn_on）。
        # 第一层在 agent._tool 白名单；这里覆盖 executor/管理页等一切直呼路径。
        if not isinstance(name, str) or not _INTENT_NAME_RE.fullmatch(name):
            logger.warning("[HA] 非法意图名已拒绝: %r", str(name)[:80])
            return {"success": False, "message": "意图名不合法", "raw": None}
        if not self.ok or self._session is None:
            return {"success": False, "message": "HA 通道未就绪", "raw": None}
        body = {"name": name, "data": data}
        try:
            async with self._session.post(
                    self._url("/api/intent/handle"), json=body,
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
                    self._url(f"/api/intent/{name}"), json=data,
                    timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                payload = await r.text()
                return self._normalize_result(r.status, payload, name)
        except Exception as e:
            self.last_error = str(e)
            return {"success": False, "message": "HA 通道异常", "raw": None}

    async def call_service(self, domain: str, service: str, data: dict,
                           timeout: float = 10.0) -> dict:
        """POST /api/services/<domain>/<service>。klar grounded 步骤（引擎已把
        名称解析成 entity_id）绕开 intent handler 直调服务——与 klar 自家集成
        dispatch.py 同路线。永不抛：失败折叠 {success:False, message}。"""
        if not self.ok or self._session is None:
            return {"success": False, "message": "HA 通道未就绪", "raw": None}
        try:
            async with self._session.post(
                    self._url(f"/api/services/{domain}/{service}"), json=data,
                    timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                self._reachable = r.status < 500
                payload = await r.text()
                if r.status in (200, 201):
                    return {"success": True, "message": "", "raw": payload}
                try:
                    msg = (json.loads(payload) or {}).get("message", "") \
                        if payload else ""
                except Exception:
                    msg = payload[:200]
                return {"success": False,
                        "message": msg or f"HA 拒绝({r.status})", "raw": payload}
        except asyncio.TimeoutError:
            self.last_error = f"{domain}.{service} 执行超时"
            return {"success": False, "message": "执行超时", "raw": None}
        except Exception as e:
            self._reachable = False
            self.last_error = str(e)
            logger.warning("[HA] service %s.%s 调用异常: %s", domain, service, e)
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
        if status >= 500:
            # 5xx body 多为 aiohttp 纯文本网页（无信息量）——统一给结构化中文
            # 错误；2026-09-11 事故：500 洗成空 message 让话术只剩空括号。
            # （staticmethod 不碰 self.last_error——由调用方语义承载）
            return {"success": False, "message": f"HA 内部错误({status})", "raw": obj}
        if status in (400, 401, 403, 404):
            msg = obj.get("message") if isinstance(obj, dict) else str(obj)
            return {"success": False, "message": msg or f"HA 拒绝({status})", "raw": obj}
        if isinstance(obj, dict):
            if "success" in obj:
                return {**obj, "raw": obj}
            # v1.0.34（2026-09-10 实发）：旧版集成 AdjustDeviceAttribute 返回无
            # 顶层 success（只有 success_count/states）→ 此前被当"非载荷"整体
            # 折进 raw，话术层全空、只剩裸「好的」。无 success 键也认载荷，
            # 成败按 success_count/states 实体判定折算，不掩盖失败。
            # v1.0.39（2026-09-10 跨版本 sweep 实锤）：SetDeviceMode 同型——只回
            # {"results":[逐实体 success]}（intent_set_mode:208），此前整单失败也会
            # 被折成 success=True（假成功）。同一折算口径覆盖三族。
            if "success_count" in obj or "states" in obj or "results" in obj:
                ok = obj.get("success_count")
                if ok is None:
                    items = [x for x in (obj.get("states") or obj.get("results") or [])
                             if isinstance(x, dict)]
                    ok = any(x.get("success", True) for x in items) if items else False
                return {"success": bool(ok), **obj, "raw": obj}
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
                async with self._session.get(self._url("/api/states")) as r:
                    # 404=路径/部署错误（states 端点恒存在），不得点亮连通；
                    # 401/403=URL 对而鉴权失败，算可达。此前 <500 把双重
                    # /api 的 404 也点亮成已连通，状态灯失明整整一个版本。
                    self._reachable = r.status in (200, 401, 403)
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
        # v1.0.44 根治：现代 HA（2024.4 起）已**删除** /api/config/* REST——区域/
        # 实体注册表只剩 WebSocket 通道。旧实现 `if r.status == 200` 静默吞 404，
        # _areas 恒空 → 查询族永远走「还没同步到房间信息」降级（多颗温度传感器
        # 拒答，且用户绑定区域也无效——现场 2026-09-11 15:10 实证，klar 同机
        # 走 WS 读到 7 个房间为对照）。新次序：WS 主通道（Bearer header /
        # auth_required 消息双形态）→ REST 兼容老 HA；双双失败不再静默：
        # WARN + last_error（状态页可见），杜绝"恒空但看似正常"。
        try:
            areas, ent_map = await self._ws_registries()
            self._apply_registries(areas, ent_map)
            return
        except Exception as e:
            logger.warning("[HA] 注册表 WebSocket 拉取失败，回落 REST（老 HA 形态）: %s", e)
        areas, ent_map, err = await self._rest_registries()
        if areas is None:
            self.last_error = f"registry: ws+rest 均失败（rest: {err}）"
            return
        self._apply_registries(areas, ent_map)

    def _apply_registries(self, areas: dict, ent_map: dict) -> None:
        self._areas = areas
        self._entity_area = ent_map
        self._reg_ts = time.time()

    def _ws_endpoints(self) -> list[str]:
        """由 base 派生 WS 端点候选（_url 唯一真源同款定式）。官方实证两形态
        （developers.home-assistant.io/docs/add-ons/communication/）：
        supervisor http://supervisor/core/api → http://supervisor/core/websocket
          （专用代理，SUPERVISOR_TOKEN 走 auth_required 消息认证）；
        直连     http://h:8123(/api)          → http://h:8123/api/websocket。
        多候选顺序试探，端点差异不赌形态。
        """
        b = self.base[:-4] if self.base.endswith("/api") else self.base.rstrip("/")
        eps = []
        if "supervisor" in b:
            eps.append(f"{b}/websocket")          # 官方 supervisor WS 代理（实证形态）
        eps.append(f"{b}/api/websocket")          # 直连 HA core 形态
        return eps

    async def _ws_cmd(self, ws, mtype: str) -> Any:
        self._ws_id = getattr(self, "_ws_id", 0) + 1
        mid = self._ws_id
        await ws.send_json({"id": mid, "type": mtype})
        while True:
            m = await ws.receive_json()
            if m.get("id") == mid:
                if m.get("type") == "result" and m.get("success"):
                    return m.get("result")
                raise RuntimeError(f"{mtype} → {str(m)[:200]}")
            # 其余帧为 event 推送（订阅制下不该出现），忽略继续等本命令结果

    async def _ws_registries(self) -> tuple[dict, dict]:
        """一次连接拉区域+实体注册表。auth 双形态：新版 HA 接受 ws 请求携带
        Authorization header（服务端静默不发首帧）；老式连接首帧 auth_required →
        消息认证。首帧 1s 探测区分两态。
        """
        last_err: Optional[Exception] = None
        for url in self._ws_endpoints():
            try:
                async with self._session.ws_connect(url, timeout=10) as ws:
                    try:
                        first = await asyncio.wait_for(ws.receive_json(), timeout=1.0)
                    except asyncio.TimeoutError:
                        first = {}
                    if first.get("type") == "auth_required":
                        await ws.send_json({"type": "auth", "access_token": self.token})
                        a = await ws.receive_json()
                        if a.get("type") != "auth_ok":
                            raise RuntimeError(f"WS 认证失败: {str(a)[:120]}")
                    elif first.get("type") in ("auth_invalid", "auth_error"):
                        raise RuntimeError(f"WS 认证被拒: {str(first)[:120]}")
                    areas = {a["area_id"]: (a.get("name") or a["area_id"])
                             for a in (await self._ws_cmd(ws, "config/area_registry/list") or [])}
                    ent_map = {}
                    for e in (await self._ws_cmd(ws, "config/entity_registry/list") or []):
                        aid = e.get("area_id")
                        if aid:
                            ent_map[e["entity_id"]] = areas.get(aid, aid)
                    return areas, ent_map
            except Exception as e:  # 端点形态差异：换下一个候选
                last_err = e
                continue
        raise last_err or RuntimeError("无可用 WS 端点")

    async def _rest_registries(self) -> tuple[Optional[dict], Optional[dict], str]:
        """老 HA（<2024.4）REST 兼容通道。非 200 不再静默——回传原因入 last_error。"""
        try:
            async with self._session.get(self._url("/api/config/area_registry/list")) as r:
                if r.status != 200:
                    return None, None, f"areas {r.status}"
                areas = {a["area_id"]: a.get("name", a["area_id"]) for a in await r.json()}
            ent_map = {}
            async with self._session.get(self._url("/api/config/entity_registry/list")) as r:
                if r.status == 200:
                    for e in await r.json():
                        aid = e.get("area_id")
                        if aid:
                            ent_map[e["entity_id"]] = areas.get(aid, aid)
            return areas, ent_map, ""
        except Exception as e:
            return None, None, str(e)

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
            async with self._session.get(self._url("/api/config")) as r:
                if r.status == 200:
                    return await r.json()
        except Exception as e:
            logger.debug("[HA] config 读取失败: %s", e)
        return {}

    async def rest_get(self, path: str, timeout: float = 6.0):
        """通用带鉴权 GET（Web 场景页数据源；path 自带 /api 前缀，同 _url 纪律）。
        一切失败折叠 None，永不抛。"""
        if not self.ok or self._session is None:
            return None
        try:
            async with self._session.get(
                    self._url(path),
                    timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                logger.debug("[HA] GET %s → HTTP %s", path, r.status)
        except Exception as e:
            logger.debug("[HA] GET %s 失败: %s", path, e)
        return None

    async def rest_write(self, method: str, path: str, body: dict | None = None,
                         timeout: float = 8.0) -> dict:
        """带鉴权写通道（v1.0.34 场景/自动化页内操作：测试/改名/编辑）。
        恒返回结构化 dict（失败折叠 {"success": False, "error": …}），永不抛。"""
        if not self.ok or self._session is None:
            return {"success": False, "error": "HA 连接不可用"}
        try:
            async with self._session.request(
                    method, self._url(path), json=body or {},
                    timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                try:
                    j = await r.json(content_type=None)
                except Exception:
                    j = None
                if isinstance(j, dict):
                    if r.status >= 400 and "success" not in j:
                        j["success"] = False
                    j.setdefault("success", r.status < 400)
                    return j
                if r.status < 400:
                    return {"success": True}
                return {"success": False, "error": f"HTTP {r.status}"}
        except Exception as e:
            logger.debug("[HA] %s %s 失败: %s", method, path, e)
            return {"success": False, "error": str(e)[:120]}

    # ── 事件旁路（v4 §2 旁路：回合留痕）──────────────────────────
    async def fire_event(self, event_type: str, data: dict) -> None:
        if not self.ok or self._session is None:
            return
        try:
            async with self._session.post(self._url(f"/api/events/{event_type}"),
                                          json={"event_data": data}) as r:
                await r.read()
        except Exception as e:
            logger.debug("[HA] 事件 %s 发送失败: %s", event_type, e)
