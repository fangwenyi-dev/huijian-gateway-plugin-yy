"""设备台账与固件仓（OTA 方案 Phase 2 加载项侧，2026-09-23）行为钉。

覆盖三层真请求/真调用（admin :8002 与 ws :8000 路由 harness 同
test_admin_api 先例；集成侧文件不可在本 venv import HA，按仓内
test_integration_config_flow 的「源码文本级」先例钉关键形态）。
钉桩动机：面板是发布链路第一道供应链闸+面向客户的操作面——
「一次性令牌消费即废」「无 sha256 拒下载」「后台零自动公网拉取」
「集成不可达如实报不 500」四条一旦回退，客户现场无法自证。
"""
import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from aiohttp import web

from conftest import FakeHAClient
from core.admin_api import make_admin_app
from core.firmware_store import FirmwareStore, vkey
from core.ws_server import AppContext, make_ws_app


# ── harness（同 test_admin_api 形态）────────────────────────────────
def _serve(app):
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
    try:
        yield port
    finally:
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
                return r.status, await r.read()
    return _run(go())


def _jget(port, path):
    st, body = _get(port, path)
    try:
        return st, json.loads(body)
    except Exception:
        return st, {}


def _jpost(port, path, body=None):
    import aiohttp

    async def go():
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://127.0.0.1:{port}{path}", json=body or {}) as r:
                return r.status, await r.json(content_type=None)
    return _run(go())


class SettingsFake:
    def __init__(self):
        self.data = {"security": {"ws_token": "tok", "require_token": False},
                     "nlu": {}, "spatial": {}}

    def get(self, k, d=None):
        cur = self.data
        for p in k.split("."):
            if not isinstance(cur, dict) or p not in cur:
                return d
            cur = cur[p]
        return cur

    def masked(self):
        return json.loads(json.dumps(self.data))

    def update(self, patch, persist=True):
        self.data.update(patch)

    def add_listener(self, fn):
        pass


# ── FirmwareStore 单元 ──────────────────────────────────────────────
@pytest.fixture()
def store(tmp_path):
    lock = tmp_path / "firmware.lock.json"
    lock.write_text(json.dumps({"releases": [
        {"version": "2.9.9", "file": "huijian-s3-2.9.9.bin",
         "urls": ["https://example.invalid/a.bin"], "sha256": "0" * 64,
         "size": 10, "notes_zh": "测试版"},
        {"version": "3.0.0", "file": "huijian-s3-3.0.0.bin",
         "urls": ["https://example.invalid/b.bin"], "size": 10},   # 故意缺 sha256
    ]}), encoding="utf-8")
    st = FirmwareStore(root=tmp_path / "data", lock_path=lock)
    return st


def test_import_requires_version_in_name(store):
    (store.import_dir / "nonsense.bin").write_bytes(b"x")
    (store.import_dir / "huijian-s3-2.9.9.bin").write_bytes(b"y" * 40)
    assert store.scan_import() == 1
    assert (store.import_dir / "nonsense.bin").is_file(), "无版本名不得误收编"
    assert (store.public / "huijian-s3-2.9.9.bin").is_file()
    lat = store.latest()
    assert lat and lat["version"] == "2.9.9" and lat["source"] == "imported"
    assert lat["sha256"] and len(lat["sha256"]) == 64


def test_issue_take_one_shot_and_filename_bound(store):
    (store.import_dir / "huijian-s3-2.9.9.bin").write_bytes(b"z" * 8)
    res = store.issue("2.9.9", "AA:BB")
    assert res and res["file"] == "huijian-s3-2.9.9.bin"
    # 名不符：拒绝且令牌同样被消费（不可换名重放）
    assert store.take(res["token"], "other.bin") is None
    assert store.take(res["token"], res["file"]) is None, "令牌必须消费即废"
    assert store.take("", res["file"]) is None
    assert store.issue("3.0.0") is None, "lock 登记但未拉取的版本不得发放"


def test_download_refuses_without_sha(store):
    ok, msg = store.download("3.0.0")
    assert not ok and "sha256" in msg, "缺校验值必须拒下载（供应链闸）"
    ok, msg = store.download("9.9.9")
    assert not ok, "lock 未登记版本拒绝"
    # 唯一源失败（example.invalid）→ 换源耗尽，如实失败
    ok, msg = store.download("2.9.9")
    assert not ok and "失败" in msg


def test_vkey_junk_safe():
    assert vkey("garbage") == (0, 0, 0)
    assert vkey("2.10.0") > vkey("2.9.9")


# ── ws :8000 /firmware 领取口 ───────────────────────────────────────
def test_ws_firmware_route(store):
    ctx = AppContext(settings=SettingsFake(), firmware=store, host="10.0.0.9")
    srv = _serve(make_ws_app(ctx)); port = next(srv)
    (store.import_dir / "huijian-s3-2.9.9.bin").write_bytes(b"P" * 64)
    res = store.issue("2.9.9")
    st, body = _get(port, f"/firmware/{res['file']}")
    assert st == 404, "无令牌不得发文件"
    st, body = _get(port, f"/firmware/{res['file']}?t={res['token']}")
    assert st == 200 and body == b"P" * 64
    st, _ = _get(port, f"/firmware/{res['file']}?t={res['token']}")
    assert st == 404, "第二次消费必须已废"
    st, _ = _get(port, f"/firmware/../../etc/passwd?t={res['token']}")
    assert st in (404, 400, 422), "穿越拒绝"


# ── admin :8002 /api/devices /api/firmware* ─────────────────────────
def _admin_ctx(store, ha_rest=None, host="10.0.0.9"):
    ha = FakeHAClient(rest=ha_rest or {})
    ctx = AppContext(settings=SettingsFake(), ha=ha, firmware=store,
                     started_at=time.time(), host=host)
    return ctx


def _sat_ledger():
    return {"/api/huijian-ai/satellites": {"devices": [
        {"mac": "aa", "name": "展厅东", "area": "展厅", "online": True,
         "fw_version": "2.9.8", "ota_services": []},
        {"mac": "bb", "name": "展厅西", "area": "展厅", "online": False,
         "fw_version": "2.9.9", "ota_services": []},
        {"mac": "cc", "name": "新店", "area": "", "online": True,
         "fw_version": "", "ota_services": []},
    ]}}


def test_devices_endpoint_annotations(store):
    ctx = _admin_ctx(store, _sat_ledger())
    (store.import_dir / "huijian-s3-2.9.9.bin").write_bytes(b"q" * 9)
    srv = _serve(make_admin_app(ctx)); port = next(srv)
    st, j = _jget(port, "/api/devices")
    assert st == 200 and j["latest"] == "2.9.9"
    d = {x["mac"]: x for x in j["devices"]}
    assert d["aa"]["updatable"] is True      # 2.9.8 < 2.9.9
    assert d["bb"]["updatable"] is False     # 已最新
    assert d["cc"]["updatable"] is False     # 未上报不瞎判


def test_devices_endpoint_fail_closed(store):
    ctx = _admin_ctx(store, {})               # 集成不可达：rest_get 折叠 None
    srv = _serve(make_admin_app(ctx)); port = next(srv)
    st, j = _jget(port, "/api/devices")
    assert st == 200 and j["devices"] == [] and j.get("error"), \
        "集成不可达必须空表+如实 error，不得 500/不得假数据"


def test_firmware_issue_endpoint(store):
    ctx = _admin_ctx(store, _sat_ledger())
    (store.import_dir / "huijian-s3-2.9.9.bin").write_bytes(b"w" * 9)
    srv = _serve(make_admin_app(ctx)); port = next(srv)
    st, j = _jpost(port, "/api/firmware/issue", {"mac": "aa"})
    assert st == 200 and j["success"], j
    assert j["url"].startswith("http://10.0.0.9:8000/firmware/")
    assert "t=" in j["url"] and j["version"] == "2.9.9" and j["warn_zh"]
    st, j = _jpost(port, "/api/firmware/issue", {"version": "9.9.9", "mac": "aa"})
    assert st == 400 and not j["success"], "未在盘版本不得发放"
    st, j = _jget(port, "/api/firmware")
    assert st == 200 and j["latest"] == "2.9.9" and "import" in j["import_dir"]


# ── 集成侧关键形态（源码文本级钉，仿 config_flow 先例）──────────────
HTTP_PY = Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai" / "huijian" / "http.py"


def test_satellites_view_registered_and_authed():
    src = HTTP_PY.read_text(encoding="utf-8")
    assert "register_view(HuijianSatellitesView)" in src
    i = src.index("class HuijianSatellitesView")
    block = src[i:i + 2600]
    assert "requires_auth = True" in block, "卫星台账含内网拓扑，必须 HA 令牌"
    assert '/api/huijian-ai/satellites"' in block
    assert "available" in block and "fw_version" in block


def test_ledger_records_fw_version_on_speakname():
    src = HTTP_PY.read_text(encoding="utf-8")
    assert 'satellite_ledger' in src and 'data.get("fw_version")' in src, \
        "CMD20 入驻上报的 fw_version 必须记入台账（历史上被整个丢弃）"
