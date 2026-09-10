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
    assert a1 == {"kind": "core", "id": "auto1", "alias": "回家开灯",
                  "description": "地理围栏到家",
                  "last_triggered": "2026-09-12T10:00:00", "state": "on"}
    assert a2["state"] == ""                                  # 无 automation.auto2 实体


def test_automations_huijian_merge():
    """v1.0.32 双引擎合并：语音自动化（.storage 经集成视图）必须出现在
    加载项「场景」tab 列表，trigger/action 压成人读中文。"""
    import asyncio
    from types import SimpleNamespace
    from core.admin_api import CTX_KEY, _automations
    rest = {
        "/api/huijian-ai/automations": {"automations": [
            {"automation_id": "automation_1", "trigger": {"at": "07:00"},
             "actions": [{"intent": "TurnDeviceOn",
                          "params": {"target": [{"area": "客厅",
                                                 "devices": [{"name": "窗帘"}]}]}}],
             "last_triggered": "2026-09-14T07:00:00"},
            {"automation_id": "automation_2",
             "trigger": {"entity_id": "办公室温湿度传感器温度", "above": 30},
             "actions": [{"intent": "SetDeviceMode",
                          "params": {"mode": "sleep"}}]},
            {"automation_id": "automation_3",
             "trigger": {"entity_id": "书房人体", "to": "on"}, "actions": []},
        ]},
        "/api/config/automations/config": {"automations": []},
    }
    ctx = AppContext(settings=SettingsFake(), ha=FakeHAClient(rest=rest), asr=None,
                     tts=TtsFake(), pipeline=PipelineFake(), scenes=ScenesFake(),
                     textcnn=None, store=StoreSnap(), started_at=time.time())
    resp = asyncio.run(_automations(SimpleNamespace(app={CTX_KEY: ctx})))
    j = json.loads(resp.body)
    hv = [a for a in j["automations"] if a.get("kind") == "huijian"]
    assert len(hv) == 3 and j["error"] == ""
    assert hv[0]["alias"] == "每天早上7点" and hv[0]["description"] == "打开客厅窗帘"
    assert hv[1]["alias"] == "办公室温湿度传感器温度（高于30）"
    assert hv[1]["description"] == "设为睡眠模式"
    assert hv[2]["alias"] == "书房人体 有人"


def test_hv_helpers_never_raise():
    from core.admin_api import _hv_trigger_cn, _hv_action_cn
    assert isinstance(_hv_trigger_cn({"at": "垃圾"}), str)     # 脏值兜底不死
    assert _hv_trigger_cn({}) == "?"
    assert _hv_action_cn({"intent": "TurnDeviceOff", "params": {"target": [
        {"area": "卧室", "devices": [{"name": "灯"}]}]}}) == "关闭卧室灯"


def test_manage_page_render_three_trigger_shapes():
    """集成 manage-page 源码级钉（HA 依赖不可本地导入；行为在 CI e2e）：
    at/to 形态必须有渲染分支，且不给编辑弹窗（防保存覆盖丢字段）。"""
    src = (Path(__file__).resolve().parents[1] / "custom_components"
           / "huijian_ai" / "api.py").read_text(encoding="utf-8")
    assert "每天 {at} 自动执行" in src and "时间自动化" in src
    assert "检测到有人" in src and "状态自动化" in src
    assert src.count('edit_btn_html = ""') == 2               # at 与 to 双分支
    assert '{kind_tag}' in src and "{edit_btn_html}" in src


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
