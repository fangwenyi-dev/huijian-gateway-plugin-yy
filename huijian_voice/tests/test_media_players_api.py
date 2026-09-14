# -*- coding: utf-8 -*-
"""播放器候选列表端点钉测（现场反馈 2026-09：设置-音乐 手填 entity_id 太苦）。

覆盖：media_player 过滤与排序、friendly_name 回退、区域名表、fail-open
（HA 缺席/无实体 → 空列表 + note，永不 500）、路由挂载（make_admin_app）。
"""
import asyncio
from types import SimpleNamespace

from aiohttp import web, ClientSession

from core import media_players_api
from core.admin_api import make_admin_app


class FakeHa:
    def __init__(self, states=None, areas=None, err=""):
        self._states = states or {}
        self._areas = areas or {}
        self.last_error = err

    async def states(self):
        return dict(self._states)

    async def area_names(self):
        return sorted(set(self._areas.values()))


def _get(ctx):
    async def go():
        app = web.Application()
        media_players_api.setup(app, ctx)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with ClientSession() as s:
                async with s.get(f"http://127.0.0.1:{port}/api/media_players") as r:
                    return r.status, await r.json()
        finally:
            await runner.cleanup()
    return asyncio.run(go())


def test_filters_and_sorts_media_players():
    ha = FakeHa(states={
        "light.a": {"state": "on", "attributes": {"friendly_name": "灯"}},
        "media_player.zebra": {"state": "playing",
                               "attributes": {"friendly_name": "书房音箱"}},
        "media_player.客厅": {"state": "idle",
                             "attributes": {"friendly_name": "客厅音箱"}},
        "media_player.no_name": {"state": "off", "attributes": {}},
        "sensor.b": None,
    }, areas={"a1": "客厅", "a2": "卧室"})
    code, j = _get(SimpleNamespace(ha=ha))
    assert code == 200
    ids = [p["entity_id"] for p in j["players"]]
    # 按名称码点排序（ASCII 先于汉字）：no_name（回退实体后缀）/书房/客厅
    assert [p["name"] for p in j["players"]] == ["no_name", "书房音箱", "客厅音箱"]
    assert set(ids) == {"media_player.zebra", "media_player.客厅",
                        "media_player.no_name"}
    assert {p["entity_id"]: p["state"] for p in j["players"]}[
        "media_player.zebra"] == "playing"
    assert j["areas"] == ["卧室", "客厅"]          # sorted() 中文按码点
    assert j["note"] == ""


def test_fail_open_empty_and_no_ha():
    code, j = _get(SimpleNamespace(ha=FakeHa(err="states 401")))
    assert code == 200 and j["players"] == [] and "states 401" in j["note"]
    code, j = _get(SimpleNamespace(ha=None))
    assert code == 200 and j["players"] == [] and j["note"]


def test_states_raises_never_500():
    class Boom(FakeHa):
        async def states(self):
            raise RuntimeError("炸")
        async def area_names(self):
            raise RuntimeError("炸")
    code, j = _get(SimpleNamespace(ha=Boom()))
    assert code == 200 and j["players"] == [] and j["areas"] == []


def test_mounted_in_admin_app():
    app = make_admin_app(SimpleNamespace(
        ha=FakeHa(states={"media_player.x": {"state": "idle",
                                             "attributes": {"friendly_name": "X"}}}),
        settings=None, store=None, tts=None))
    paths = {r.resource.canonical for r in app.router.routes()}
    assert "/api/media_players" in paths
