"""v1.0.44 区域注册表 WebSocket 化根治钉。

现场实证（2026-09-11 15:10 客户日志）：「现在办公室温度传感器多少」被
「家里有多个温度传感器，但还没同步到房间信息」拒掉——HA 里明明有 7 个房间
（klar 同机 WS 读到），慧尖却拿不到：现代 HA（2024.4 起）**删除**了
/api/config/* REST，旧 _load_registries 的 404 被 `if r.status == 200` 静默
吞掉 → _areas 恒空 → 查询族永久走 Q2 降级。且照话术绑定区域也无效（读不到），
现场无解——本版根治。

钉层（真协议自证，用户验证铁律；服务器基建同款 test_protocol_ws 的
「loop 线程 + 真 aiohttp server」范式）：
 A. 真 aiohttp WS 服务器复刻 HA 协议：auth_required 消息认证形态 + header
    Bearer 静默认证形态；config/area_registry/list + config/entity_registry/list
    双命令回真数据。
 B. 现代 HA 形态（REST config 端点 404）下 _load_registries 成功填充；
    失败时不再静默（last_error 可见）。
 C. 老 HA 形态（无 WS 端点、REST 200）回落兼容。
 D. supervisor 端点派生顺序（官方文档实证 ws://supervisor/core/websocket 在前）。
 E. query 层名称兜底：区域数据缺失时实体名含区域词且唯一 → 照答不拒。
"""
from __future__ import annotations

import asyncio
import json
import threading
import sys
from pathlib import Path

import pytest
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.ha_client import HAClient  # noqa: E402
from core.nlu.query import QueryZone  # noqa: E402

AREAS = [{"area_id": "office", "name": "办公室"}, {"area_id": "bed", "name": "卧室"}]
ENTITIES = [
    {"entity_id": "sensor.t1", "area_id": "office"},
    {"entity_id": "sensor.t2", "area_id": None},
]


def _result(mid, payload):
    return {"id": mid, "type": "result", "success": True, "result": payload}


def _ha_ws_app(mode: str) -> web.Application:
    """mode: authmsg=auth_required 消息认证 | header=Bearer 头静默认证 |
    refuse=认证必拒（观察降级行为）。REST config 端点回 404＝现代 HA 形态。"""
    app = web.Application()

    async def ws_handler(request: web.Request):
        if mode == "header":
            assert request.headers.get("Authorization") == "Bearer test-token"
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if mode == "authmsg":
            await ws.send_json({"type": "auth_required", "ha_version": "2026.8.3"})
            auth = await ws.receive_json()
            if auth.get("type") == "auth" and auth.get("access_token") == "test-token":
                await ws.send_json({"type": "auth_ok", "ha_version": "2026.8.3"})
            else:
                await ws.send_json({"type": "auth_invalid", "message": "no"})
                await ws.close()
                return ws
        elif mode == "refuse":
            await ws.send_json({"type": "auth_required", "ha_version": "x"})
            await ws.receive_json()
            await ws.send_json({"type": "auth_invalid", "message": "bad token"})
            await ws.close()
            return ws
        async for msg in ws:
            data = json.loads(msg.data)
            t = data.get("type")
            if t == "config/area_registry/list":
                await ws.send_json(_result(data["id"], AREAS))
            elif t == "config/entity_registry/list":
                await ws.send_json(_result(data["id"], ENTITIES))
        return ws

    async def rest_404(request):   # 现代 HA：config REST 已删
        return web.json_response({"message": "Not found"}, status=404)

    app.router.add_get("/api/websocket", ws_handler)
    app.router.add_get("/api/config/area_registry/list", rest_404)
    app.router.add_get("/api/config/entity_registry/list", rest_404)
    return app


def _old_ha_app() -> web.Application:
    """老 HA（<2024.4）：无 WS 路由（404），REST config 端点 200。"""
    app = web.Application()

    async def areas(request):
        return web.json_response(AREAS)

    async def ents(request):
        return web.json_response(ENTITIES)

    app.router.add_get("/api/config/area_registry/list", areas)
    app.router.add_get("/api/config/entity_registry/list", ents)
    async def states(request):
        return web.json_response([])

    app.router.add_get("/api/states", states)
    return app


def _serve(app) -> str:
    """loop 线程起真服务器（同款 test_protocol_ws 范式），返回 base url。"""
    loop = asyncio.new_event_loop()
    holder = {}

    def run():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    threading.Thread(target=run, daemon=True).start()

    async def start():
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        holder["runner"] = runner
        return site._server.sockets[0].getsockname()[1]  # noqa: SLF001

    port = asyncio.run_coroutine_threadsafe(start(), loop).result(10)

    def teardown():
        asyncio.run_coroutine_threadsafe(holder["runner"].cleanup(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)

    _serve._teardowns.append(teardown)  # type: ignore[attr-defined]
    return f"http://127.0.0.1:{port}"


_serve._teardowns = []  # type: ignore[attr-defined]


@pytest.fixture()
def ha_base(request):
    mode = getattr(request, "param", "authmsg")
    app = _old_ha_app() if mode == "oldrest" else _ha_ws_app(mode)
    yield _serve(app)
    while _serve._teardowns:  # type: ignore[attr-defined]
        _serve._teardowns.pop()()  # type: ignore[attr-defined]


def _load(base: str):
    async def go():
        c = HAClient()
        c.base = base
        c.token = "test-token"
        await c.start()
        try:
            await c._load_registries()
            return dict(c._areas), dict(c._entity_area), c.last_error
        finally:
            await c.close()
    return asyncio.run(go())


# ─────────────────────────── A/B：WS 双认证形态 ───────────────────────────
@pytest.mark.parametrize("ha_base", ["authmsg", "header"], indirect=True)
def test_ws_registries_both_auth_modes(ha_base):
    """真 HA 协议 WS：消息认证与 header 静默认证两形态都拿到注册表。"""
    areas, ent_area, err = _load(ha_base)
    assert areas == {"office": "办公室", "bed": "卧室"}
    assert ent_area == {"sensor.t1": "办公室"}
    assert "registry" not in err, err   # states 404 为路架噪音；注册表通道必须零错误


# ──────────────────────── B：失败不再静默 ────────────────────────
@pytest.mark.parametrize("ha_base", ["refuse"], indirect=True)
def test_failure_not_silent_anymore(ha_base):
    """认证被拒 → WS 与 REST（现代 HA 404）双双失败：last_error 必须可见，
    绝不重蹈"恒空但看似正常"。"""
    areas, _, err = _load(ha_base)
    assert areas == {}
    assert "registry" in err, err


# ─────────────────────────── C：老 HA REST 回落 ───────────────────────────
@pytest.mark.parametrize("ha_base", ["oldrest"], indirect=True)
def test_old_ha_rest_fallback(ha_base):
    """无 WS 端点（404）→ REST 兼容通道照常填充。"""
    areas, ent_area, _ = _load(ha_base)
    assert areas == {"office": "办公室", "bed": "卧室"}
    assert ent_area == {"sensor.t1": "办公室"}


# ─────────────────────────── D：端点派生 ───────────────────────────
def test_ws_endpoints_supervisor_official_proxy_first():
    """官方文档实证（developers HA add-on communication）：supervisor 形态
    首选 http://supervisor/core/websocket 专用代理。"""
    c = HAClient(session=None)
    c.base = "http://supervisor/core/api"
    eps = c._ws_endpoints()
    assert eps[0] == "http://supervisor/core/websocket"
    assert "http://supervisor/core/api/websocket" in eps


def test_ws_endpoints_direct_form():
    c = HAClient(session=None)
    c.base = "http://192.168.1.5:8123"
    assert c._ws_endpoints() == ["http://192.168.1.5:8123/api/websocket"]


# ─────────────────────────── E：query 层名称兜底 ───────────────────────────
class _StatesHA:
    """替身：_areas/_entity_area 恒空（复刻 WS+REST 双失败的极端形态）。"""

    def __init__(self, states):
        self._states = states
        self._areas = {}
        self._entity_area = {}

    async def states(self):
        return dict(self._states)


def _temp(name, val):
    return {"state": str(val),
            "attributes": {"device_class": "temperature", "friendly_name": name}}


def _answer(states, area):
    q = QueryZone(_StatesHA(states), settings=None)
    return asyncio.run(q._sensor_answer(area, "temperature", "温度"))


def test_no_area_data_name_unique_answers_instead_of_refusing():
    """现场场景：区域数据缺失 + 说「办公室」+ 恰好一颗名字含办公室 → 照答。"""
    ans = _answer({
        "sensor.office_temp": _temp("办公室温度", 26.5),
        "sensor.bed_temp": _temp("卧室温度", 22.0),
    }, "办公室")
    assert ans is not None and "26.5" in ans and "没同步" not in ans, ans


def test_no_area_data_name_ambiguous_still_honest():
    """名字也不含区域词时保持诚实引导（宁缺勿滥不猜）。"""
    ans = _answer({
        "sensor.a": _temp("温度一", 26.5),
        "sensor.b": _temp("温度二", 22.0),
    }, "客厅")
    assert ans is not None and "没同步到房间信息" in ans
