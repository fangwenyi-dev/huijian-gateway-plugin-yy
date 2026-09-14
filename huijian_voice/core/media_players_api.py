"""播放器候选列表端点（独立小模块——同 tts_voices_api 惯例，避开 admin_api 热区）。

背景（现场 2026-09-14 日志 + 用户定向）：设置-音乐 的播放端点要用户手填
entity_id，拼错/删过实体 → 点歌永远"播放端点没有响应"死循环。HA 全量实体
states 本就缓存在 ha_client（查询族/executor 共用），列个下拉是顺手的事。

挂载：admin_api.make_admin_app 内 `media_players_api.setup(app, ctx)` 一行。
契约：
  GET /api/media_players → {players:[{entity_id,name,state}], areas:[...], note}
  数据源 = ha_client.states()/area_names()（公开面，TTL 缓存内零额外 HTTP）。
  fail-open：HA 不可达 → players 空 + note 说明原因，永不抛（面板照常渲染，
  已存的 stale entity_id 由前端原样保留展示，绝不静默丢值）。
"""
from __future__ import annotations

import logging

from aiohttp import web

logger = logging.getLogger("huijian.admin")


async def players_payload(ctx) -> dict:
    """media_player 候选 + HA 区域名表。永不抛（ha_client 已折叠，双保险）。"""
    players: list[dict] = []
    areas: list[str] = []
    note = ""
    ha = getattr(ctx, "ha", None)
    if ha is None:
        return {"players": [], "areas": [], "note": "HA 通道未初始化"}
    try:
        states = await ha.states()
    except Exception as e:
        states = {}
        logger.debug("[播放器] states 读取失败：%s", e)
    for eid, ent in (states or {}).items():
        if not str(eid).startswith("media_player."):
            continue
        ent = ent or {}
        attrs = ent.get("attributes") or {}
        players.append({
            "entity_id": eid,
            "name": str(attrs.get("friendly_name") or eid.split(".", 1)[1]),
            "state": str(ent.get("state") or ""),
        })
    players.sort(key=lambda p: (p["name"], p["entity_id"]))
    if not players:
        note = str(getattr(ha, "last_error", "") or "") or \
            "HA 里暂无 media_player 实体（未接音箱或集成未连通）"
    try:
        areas = [str(a) for a in await ha.area_names() if a]
    except Exception:
        areas = []
    return {"players": players, "areas": areas, "note": note}


def setup(app, ctx):
    async def _list(request):
        return web.json_response(await players_payload(ctx))

    app.router.add_get("/api/media_players", _list)
