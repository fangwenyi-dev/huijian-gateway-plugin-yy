"""管理 API(:8002) HTTP 级测试——每个路由真发请求。

钉桩动机（E2E-lite 实战教训）：admin 处理器群曾全体引用不存在的全局 ctx，
纯逻辑测试无一命中，真实 curl 才炸出 NameError → 路由级全量覆盖常态化。
"""
import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import ClientSession  # 仅需可用性判据

from conftest import FakeHAClient
from core.admin_api import make_admin_app
from core.ws_server import AppContext


class StoreSnap:
    def snapshot(self): return {"asr_paraformer_bilingual": {"status": "ready", "ready": True}}
    def is_ready(self, k): return True
    def keys(self): return ["asr_paraformer_bilingual", "tts_kokoro_multilang"]
    def ensure_async(self, key, cb=None): pass


class ScenesFake:
    triggers = ["观影模式"]
    async def refresh(self, force=False): pass
    def all(self):
        return [{"trigger_phrase": "观影模式", "name": "观影", "scene_id": "s1",
                 "created_at": "2026-09-01T00:00:00",
                 "actions": [{"intent": "HassTurnOff", "params": {"entity_id": "light.客厅"}}]}]


class PipelineFake:
    fast_path = None
    executor = None
    async def dry_run(self, text):
        return {"plan": {"intent": "TurnDeviceOn", "args": {"target": []},
                         "source": "t0", "trace": ["测试轨迹"]}}


class TtsFake:
    last_used = 0
    def ready(self): return True
    def unload(self): return True
    async def synthesize_pcm(self, text):
        return b"\x00\x01" * 8000
    async def stream_opus(self, text):
        yield b"x"


class SettingsFake:
    def __init__(self):
        self.data = {"security": {"ws_token": "tok123", "require_token": False},
                     "stt": {"provider": "local_paraformer"}, "tts": {"sid": 45},
                     "llm": {"enabled": False}, "nlu": {}, "power": {}, "dialog": {}}
    def get(self, k, d=None):
        cur = self.data
        for part in k.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return d
            cur = cur[part]
        return cur
    def masked(self): return json.loads(json.dumps(self.data))
    def update(self, patch, persist=True): self.data.update(patch)
    def add_listener(self, fn): pass
    def endpoint_urls(self, host):
        return {c: f"ws://{host}:8000/xiaozhi/v1/{c}?token=tok123" for c in ("stt", "tts", "llm")}


@pytest.fixture()
def admin():
    ha = FakeHAClient(
        states={"automation.auto1": {"entity_id": "automation.auto1", "state": "on",
                                     "attributes": {}},
                "light.x": {"entity_id": "light.x", "state": "off", "attributes": {}}},
        rest={"/api/config/automations/config": {"automations": [
            {"id": "auto1", "alias": "回家开灯", "description": "地理围栏到家",
             "last_triggered": "2026-09-12T10:00:00"},
            {"id": "auto2", "alias": "YAML 未重载"}]}})
    ctx = AppContext(settings=SettingsFake(), ha=ha, asr=None, tts=TtsFake(),
                     pipeline=PipelineFake(), scenes=ScenesFake(), textcnn=None,
                     store=StoreSnap(), started_at=time.time())
    app = make_admin_app(ctx)
    loop = asyncio.new_event_loop()
    holder = {}

    def run():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    th = threading.Thread(target=run, daemon=True)
    th.start()

    async def start():
        runner = web.AppRunner(app, access_logger=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        holder["runner"] = runner
        return runner.addresses[0][1]

    port = asyncio.run_coroutine_threadsafe(start(), loop).result(10)
    yield port
    asyncio.run_coroutine_threadsafe(holder["runner"].cleanup(), loop).result(10)
    loop.call_soon_threadsafe(loop.stop)
    th.join(5)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _get(port, path):
    import aiohttp
    async def go():
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{port}{path}") as r:
                return r.status, await r.text()
    return _run(go())


def _post(port, path, body=None, raw=False):
    import aiohttp
    async def go():
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://127.0.0.1:{port}{path}",
                              json=body if not raw else None,
                              data=body if raw else None) as r:
                return r.status, await r.text()
    return _run(go())


def test_health(admin):
    st, body = _get(admin, "/api/health")
    assert st == 200
    j = json.loads(body)
    assert j["ok"] and "version" in j and j["models_ready"]


def test_settings_roundtrip(admin):
    st, body = _get(admin, "/api/settings")
    assert st == 200 and "security" in json.loads(body)
    st2, b2 = _post(admin, "/api/settings", {"tts": {"sid": 47}})
    assert st2 == 200 and json.loads(b2)["ok"]


def test_models_and_download(admin):
    st, body = _get(admin, "/api/models")
    assert st == 200 and "asr_paraformer_bilingual" in json.loads(body)
    st2, b2 = _post(admin, "/api/models/download", {"key": "asr_paraformer_bilingual"})
    assert st2 == 200
    st3, _ = _post(admin, "/api/models/download", {"key": "不存在的包"})
    assert st3 == 400


def test_endpoints_and_regen(admin):
    st, body = _get(admin, "/api/endpoints")
    j = json.loads(body)
    assert st == 200 and "?token=tok123" in j["stt_endpoint"]
    st2, b2 = _post(admin, "/api/token/regenerate", {})
    assert st2 == 200 and json.loads(b2)["ok"]


def test_nlu_test_route(admin):
    st, body = _post(admin, "/api/nlu/test", {"text": "打开客厅的灯"})
    assert st == 200
    j = json.loads(body)
    assert j["cascade"]["plan"]["intent"] == "TurnDeviceOn"
    st2, _ = _post(admin, "/api/nlu/test", {})     # 空 text → 400
    assert st2 == 400


def test_tts_test_returns_wav(admin):
    import aiohttp
    async def go():
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://127.0.0.1:{admin}/api/tts/test", json={"text": "你好"}) as r:
                data = await r.read()
                assert r.status == 200 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    _run(go())


def test_scenes_and_reload(admin):
    st, body = _get(admin, "/api/scenes")
    j = json.loads(body)
    assert st == 200 and "观影模式" in j["triggers"]        # 兼容键仍在
    assert j["error"] == ""
    row = j["scenes"][0]
    assert row["trigger"] == "观影模式" and row["action_count"] == 1
    assert row["actions"] == ["HassTurnOff light.客厅"]      # 动作压成人读摘要


def test_automations_route(admin):
    st, body = _get(admin, "/api/automations")
    j = json.loads(body)
    assert st == 200 and j["error"] == ""
    a1, a2 = j["automations"]
    assert a1 == {"id": "auto1", "alias": "回家开灯", "description": "地理围栏到家",
                  "last_triggered": "2026-09-12T10:00:00", "state": "on"}
    assert a2["state"] == ""                                  # 无 automation.auto2 实体


def test_automations_no_bridge():
    """HA API 不可达（rest_get 折叠 None）→ 空表 + 诚实错误，绝不 500。"""
    import asyncio
    from types import SimpleNamespace
    from core.admin_api import CTX_KEY, _automations
    ctx = AppContext(settings=SettingsFake(), ha=FakeHAClient(), asr=None, tts=TtsFake(),
                     pipeline=PipelineFake(), scenes=ScenesFake(), textcnn=None,
                     store=StoreSnap(), started_at=time.time())
    resp = asyncio.run(_automations(SimpleNamespace(app={CTX_KEY: ctx})))
    j = json.loads(resp.body)
    assert j["automations"] == [] and "不可达" in j["error"]
