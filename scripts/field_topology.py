# -*- coding: utf-8 -*-
"""现场拓扑盘点：area registry + entity registry（WS 一次性，HA 2024.4 后 REST 已删）。

用法：
    python scripts/field_topology.py                 # 读 ~/gates/.hatok + .haurl
    HUIJIAN_HA_URL=http://192.168.1.91 HUIJIAN_HA_TOKEN=... python scripts/field_topology.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import aiohttp


def _secret(name: str, filename: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        path = Path.home() / "gates" / filename
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
    if not value:
        sys.exit(f"缺 {name}：设环境变量或写 {Path.home() / 'gates' / filename}")
    return value


HA = _secret("HUIJIAN_HA_URL", ".haurl")
TOK = _secret("HUIJIAN_HA_TOKEN", ".hatok")


async def main():
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect(HA.replace("http", "ws") + "/api/websocket")
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": TOK})
        assert (await ws.receive_json())["type"] == "auth_ok"
        mid = 0

        async def sub(msg):
            nonlocal mid
            mid += 1
            msg["id"] = mid
            await ws.send_json(msg)
            while True:
                r = await ws.receive_json()
                if r.get("id") == mid:
                    if not r.get("success", True):
                        print("ERR", msg["type"], json.dumps(r.get("error"), ensure_ascii=False)[:200])
                        return []
                    return r.get("result") or []
        areas_raw = await sub({"type": "area_registry/list"})
        if not areas_raw:
            areas_raw = await sub({"type": "config/area_registry/list"})
        areas = {a["area_id"]: a["name"] for a in areas_raw}
        ents = await sub({"type": "entity_registry/list"})
        if not ents:
            ents = await sub({"type": "config/entity_registry/list"})
        print("== areas ==")
        for aid, nm in areas.items():
            print(f"  {aid} = {nm}")
        print("== entities by area (可交互域) ==")
        by_area = {}
        for e in ents:
            if not e.get("area_id"):
                continue
            dom = (e["entity_id"].split(".", 1)[0])
            if dom not in ("light", "switch", "cover", "climate", "fan", "button",
                           "number", "sensor", "binary_sensor", "media_player",
                           "assist_satellite", "vacuum", "lock"):
                continue
            by_area.setdefault(e["area_id"], []).append((dom, e["entity_id"], e.get("name") or e.get("original_name") or ""))
        for aid, lst in sorted(by_area.items()):
            print(f"-- {areas.get(aid, aid)} ({aid}) --")
            for dom, eid, nm in sorted(lst):
                print(f"   [{dom:16s}] {eid:55s} {nm}")
        print("== 无 area 的 cover/climate（潜在盲区）==")
        for e in ents:
            if e.get("area_id"):
                continue
            dom = e["entity_id"].split(".", 1)[0]
            if dom in ("cover", "climate", "light", "switch"):
                print(f"   [{dom}] {e['entity_id']} {e.get('name') or e.get('original_name')}")
        await ws.close()


asyncio.run(main())
