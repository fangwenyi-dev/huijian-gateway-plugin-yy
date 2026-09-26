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


def _head(port, path):
    import aiohttp

    async def go():
        async with aiohttp.ClientSession() as s:
            async with s.head(f"http://127.0.0.1:{port}{path}") as r:
                return r.status
    return _run(go())


def _drop(store, name, data, age=30):
    """投递口放包（v1.0.65 静止闸适配）：mtime 拨到 age 秒前=拷贝已完成形态。"""
    import os
    p = store.import_dir / name
    p.write_bytes(data)
    t = time.time() - age
    os.utime(p, (t, t))
    return p


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
         "urls": ["http://127.0.0.1:1/a.bin"], "sha256": "0" * 64,
         "size": 10, "notes_zh": "测试版"},
        {"version": "3.0.0", "file": "huijian-s3-3.0.0.bin",
         "urls": ["http://127.0.0.1:1/b.bin"], "size": 10},   # 故意缺 sha256
    ]}), encoding="utf-8")
    st = FirmwareStore(root=tmp_path / "data", lock_path=lock)
    return st


def test_import_requires_version_in_name(store):
    _drop(store, "nonsense.bin", b"x")
    _drop(store, "huijian-s3-2.9.7.bin", b"y" * 40)
    assert store.scan_import() == 1
    assert (store.import_dir / "nonsense.bin").is_file(), "无版本名不得误收编"
    assert (store.public / "huijian-s3-2.9.7.bin").is_file()
    lat = store.latest()
    assert lat and lat["version"] == "2.9.7" and lat["source"] == "imported"
    assert lat["sha256"] and len(lat["sha256"]) == 64


def test_import_quiet_gate_and_guards(store):
    """v1.0.65 审查批：半截拷贝不收编（F-OTA-02）、0 字节/超长名拒收（F-07/F-06）。"""
    p = store.import_dir / "huijian-s3-2.9.7.bin"
    p.write_bytes(b"a" * 32)                      # mtime=now → 拷贝未静止
    assert store.scan_import() == 0, "静止闸：mtime 距今 <2s 不得收编"
    assert p.is_file()
    import os
    t = time.time() - 30
    os.utime(p, (t, t))                           # 静止后收编
    assert store.scan_import() == 1
    assert (store.public / p.name).is_file()
    _drop(store, "huijian-s3-2.9.6.bin", b"")     # 0 字节
    assert store.scan_import() == 0
    assert (store.import_dir / "huijian-s3-2.9.6.bin").is_file(), "0 字节包拒收留投递口"
    _drop(store, "x" * 200 + "-2.9.5.bin", b"b")  # 文件名超 BLE URL 预算
    assert store.scan_import() == 0, "超长文件名拒收编"


def test_issue_take_one_shot_and_filename_bound(store):
    _drop(store, "huijian-s3-2.9.7.bin", b"z" * 8)
    res = store.issue("2.9.7", "AA:BB")
    assert res and res["file"] == "huijian-s3-2.9.7.bin"
    # 名不符：拒绝且令牌同样被消费（不可换名重放）
    assert store.take(res["token"], "other.bin") is None
    assert store.take(res["token"], res["file"]) is None, "令牌必须消费即废"
    assert store.take("", res["file"]) is None
    assert store.issue("3.0.0") is None, "lock 登记但未拉取的版本不得发放"


def test_issue_rejects_changed_size_on_disk(store):
    """v1.0.65 F-OTA-02 第二道闸：收编后文件被外部改动 → 拒签（不发放损坏包）。"""
    _drop(store, "huijian-s3-2.9.7.bin", b"c" * 16)
    assert store.issue("2.9.7"), "正常包可签发"
    # 换内容同 size 改不掉 size 差异——追加字节模拟外部篡改
    p = store.public / "huijian-s3-2.9.7.bin"
    p.write_bytes(b"c" * 99)
    assert store.issue("2.9.7") is None, "在盘 size 与账目不符必须拒签"


def test_download_refuses_without_sha(store):
    ok, msg = store.download("3.0.0")
    assert not ok and "sha256" in msg, "缺校验值必须拒下载（供应链闸）"
    ok, msg = store.download("9.9.9")
    assert not ok, "lock 未登记版本拒绝"
    # 唯一源失败（127.0.0.1:1 连接被拒，秒失败）→ 换源耗尽，如实失败。
# 旧形态用 example.invalid：.invalid 走真 DNS，本机解析超时 11s×2 占掉
# 全量回归的 31%——失败语义相同，但没有理由让每次都付网络超时。
    ok, msg = store.download("2.9.9")
    assert not ok and "失败" in msg


def test_download_writeside_path_and_scheme_gates(tmp_path):
    """v1.0.65 安全 F1：lock 是现场热修手编面——file 逃逸形态与 file:// 源必须拒。"""
    lock = tmp_path / "firmware.lock.json"
    esc = [
        {"version": "1.0.1", "file": "/tmp/pwn-1.0.1.bin",
         "urls": ["https://example.invalid/a"], "sha256": "0" * 64},
        {"version": "1.0.2", "file": "../esc-1.0.2.bin",
         "urls": ["https://example.invalid/a"], "sha256": "0" * 64},
        {"version": "1.0.3", "file": "sub/dir-1.0.3.bin",
         "urls": ["https://example.invalid/a"], "sha256": "0" * 64},
        {"version": "1.0.4", "file": "ok-1.0.4.bin",
         "urls": ["file:///etc/hostname"], "sha256": "0" * 64},
    ]
    lock.write_text(json.dumps({"releases": esc}), encoding="utf-8")
    st = FirmwareStore(root=tmp_path / "data", lock_path=lock)
    for v in ("1.0.1", "1.0.2", "1.0.3"):
        ok, msg = st.download(v)
        assert not ok and ("file" in msg or "非法" in msg), f"{v} 路径逃逸必须拒"
    assert not Path("/tmp/pwn-1.0.1.bin").exists()
    ok, msg = st.download("1.0.4")
    assert not ok and ("http" in msg or "file" in msg), "file:// 源必须拒（本地文件读外泄面）"


def test_download_byte_gate(tmp_path):
    """v1.0.65 安全 F2：声明 size 谎报小、源灌大 → 字节闸中止（护 /data 共享卷）。"""
    import hashlib
    payload = b"m" * (2 << 20)          # 2MB
    sha = hashlib.sha256(payload).hexdigest()
    lock = tmp_path / "firmware.lock.json"

    async def _mk_app():
        app = web.Application()
        async def h(r):
            return web.Response(body=payload)
        app.router.add_get("/big.bin", h)
        return app

    app = _run(_mk_app())
    srv = _serve(app); port = next(srv)
    lock.write_text(json.dumps({"releases": [
        {"version": "8.8.8", "file": "h-8.8.8.bin",
         "urls": [f"http://127.0.0.1:{port}/big.bin"], "sha256": sha, "size": 10},
    ]}), encoding="utf-8")
    st = FirmwareStore(root=tmp_path / "data", lock_path=lock)
    ok, msg = st.download("8.8.8")
    assert not ok, "超字节闸（声明 10B×1.2 < 实收 2MB）必须中止"
    assert not (st.public / "h-8.8.8.bin").exists(), "被闸断的包不得入库"
    assert not list(st.public.glob("*.part.*")), "tmp 必须清理"


def test_vkey_junk_safe():
    assert vkey("garbage") == (0, 0, 0)
    assert vkey("2.10.0") > vkey("2.9.9")


# ── ws :8000 /firmware 领取口 ───────────────────────────────────────
def test_ws_firmware_route(store):
    ctx = AppContext(settings=SettingsFake(), firmware=store, host="10.0.0.9")
    srv = _serve(make_ws_app(ctx)); port = next(srv)
    _drop(store, "huijian-s3-2.9.7.bin", b"P" * 64)
    res = store.issue("2.9.7")
    st, body = _get(port, f"/firmware/{res['file']}")
    assert st == 404, "无令牌不得发文件"
    # v1.0.65 F-OTA-03：HEAD（链接预览器行为）不得消费一次性令牌
    st = _head(port, f"/firmware/{res['file']}?t={res['token']}")
    assert st == 405, "HEAD 必须 405（allow_head=False），不得进 handler 烧令牌"
    st, body = _get(port, f"/firmware/{res['file']}?t={res['token']}")
    assert st == 200 and body == b"P" * 64
    st, _ = _get(port, f"/firmware/{res['file']}?t={res['token']}")
    assert st == 404, "第二次消费必须已废"
    st, _ = _get(port, f"/firmware/../../etc/passwd?t={res['token']}")
    assert st in (404, 400, 422), "穿越拒绝"


def test_ws_firmware_take_guards(store):
    """v1.0.65 F-OTA-13：take 的 resolve-containment 守卫真测（HTTP 层 fname 单段
    永远不含 /，守卫只能单元直测）+ public 内符号链接外指必须拒且不泄内容。"""
    ctx = AppContext(settings=SettingsFake(), firmware=store, host="10.0.0.9")
    srv = _serve(make_ws_app(ctx)); port = next(srv)
    # 令牌 file 字段被做成绝对路径（手编 lock+index crafted 态）→ parents 闸拦下
    store._tokens["tk-path"] = {"file": "/etc/passwd", "exp": time.time() + 60}
    assert store.take("tk-path", "/etc/passwd") is None, "绝对路径越界必须拒"
    # 符号链接指向 public 外
    (store.public / "evil-2.9.8.bin").symlink_to("/etc/passwd")
    store._tokens["tk-link"] = {"file": "evil-2.9.8.bin", "exp": time.time() + 60}
    st, body = _get(port, "/firmware/evil-2.9.8.bin?t=tk-link")
    assert st == 404 and b"root:" not in body, "public 内符号链接外指必须拒发"


# ── admin :8002 /api/devices /api/firmware* ─────────────────────────
def _admin_ctx(store, ha_rest=None, host="10.0.0.9"):
    ha = FakeHAClient(rest=ha_rest or {})
    ctx = AppContext(settings=SettingsFake(), ha=ha, firmware=store,
                     started_at=time.time(), host=host)
    return ctx


def _sat_ledger():
    return {"/api/huijian-ai/satellites": {"devices": [
        {"mac": "aa", "name": "展厅东", "area": "展厅", "online": True,
         "fw_version": "2.9.6", "ota_services": []},
        {"mac": "bb", "name": "展厅西", "area": "展厅", "online": False,
         "fw_version": "2.9.7", "ota_services": []},
        {"mac": "cc", "name": "新店", "area": "", "online": True,
         "fw_version": "", "ota_services": []},
    ]}}


def test_devices_endpoint_annotations(store):
    ctx = _admin_ctx(store, _sat_ledger())
    _drop(store, "huijian-s3-2.9.7.bin", b"q" * 9)
    srv = _serve(make_admin_app(ctx)); port = next(srv)
    st, j = _jget(port, "/api/devices")
    assert st == 200 and j["latest"] == "2.9.7"
    d = {x["mac"]: x for x in j["devices"]}
    assert d["aa"]["updatable"] is True      # 2.9.6 < 2.9.7
    assert d["bb"]["updatable"] is False     # 已最新
    assert d["cc"]["updatable"] is False     # 未上报不瞎判


def test_devices_endpoint_fail_closed(store):
    ctx = _admin_ctx(store, {})               # 集成不可达：rest_get 折叠 None
    srv = _serve(make_admin_app(ctx)); port = next(srv)
    st, j = _jget(port, "/api/devices")
    assert st == 200 and j["devices"] == [] and j.get("error"), \
        "集成不可达必须空表+如实 error，不得 500/不得假数据"


def test_firmware_issue_endpoint(store, monkeypatch):
    # v1.0.65 契约 F-09：host 改签发实时取值——钉住桩 IP，不依赖 runner 网络
    monkeypatch.setattr("core.ota_api.local_ip", lambda: "10.0.0.9")
    ctx = _admin_ctx(store, _sat_ledger())
    _drop(store, "huijian-s3-2.9.7.bin", b"w" * 9)
    srv = _serve(make_admin_app(ctx)); port = next(srv)
    st, j = _jpost(port, "/api/firmware/issue", {"mac": "aa"})
    assert st == 200 and j["success"], j
    assert j["url"].startswith("http://10.0.0.9:8000/firmware/")
    assert "t=" in j["url"] and j["version"] == "2.9.7" and j["warn_zh"]
    # 话术如实（v1.0.65 契约 F-01/F-05）：重签指引 + 代发未就绪 + 仅内网
    for kw in ("重新签发", "小程序", "局域网"):
        assert kw in j["warn_zh"], f"warn_zh 缺如实要素：{kw}"
    st, j = _jpost(port, "/api/firmware/issue", {"version": "9.9.9", "mac": "aa"})
    assert st == 400 and not j["success"], "未在盘版本不得发放"
    st, j = _jget(port, "/api/firmware")
    assert st == 200 and j["latest"] == "2.9.7" and "import" in j["import_dir"]


def test_firmware_issue_no_routable_host(store, monkeypatch):
    """v1.0.65 契约 F-09：无可路由局域网地址（回环）→ 如实 503，不发废链接。"""
    monkeypatch.setattr("core.ota_api.local_ip", lambda: "127.0.0.1")
    ctx = _admin_ctx(store, _sat_ledger())
    _drop(store, "huijian-s3-2.9.7.bin", b"w" * 9)
    srv = _serve(make_admin_app(ctx)); port = next(srv)
    st, j = _jpost(port, "/api/firmware/issue", {"mac": "aa"})
    assert st == 503 and not j["success"] and "局域网" in j["error"]


def test_status_scrubs_url_credentials(store):
    """v1.0.65 安全 F3：lock urls 可内嵌一次性访问串——对外数据面只给主机名。"""
    lock = store.lock_path
    data = json.loads(lock.read_text(encoding="utf-8"))
    data["releases"][0]["urls"] = [
        "https://ghprivate.example/token=SECRETVAL123/releases/f.bin"]
    lock.write_text(json.dumps(data), encoding="utf-8")
    srv = _serve(make_admin_app(_admin_ctx(store, {})))
    port = next(srv)
    st, j = _jget(port, "/api/firmware")
    assert st == 200
    blob = json.dumps(j)
    assert "SECRETVAL123" not in blob, "访问串/查询参数不得进对外 JSON"
    assert "ghprivate.example" in blob, "主机名可留（容灾源可辨识）"
    row = next(r for r in j["items"] if r["version"] == "2.9.9")
    assert row["urls_count"] == 1 and row["urls_hosts"] == ["ghprivate.example"]


def test_versions_survive_dirty_size(store, monkeypatch):
    """v1.0.65 F-OTA-05/04：手编 lock 的脏 size 一条坏行不再毒死整表/500。"""
    monkeypatch.setattr("core.ota_api.local_ip", lambda: "10.0.0.9")
    lock = store.lock_path
    lock.write_text(json.dumps({"releases": [
        {"version": "5.5.5", "file": "d-5.5.5.bin",
         "urls": ["http://127.0.0.1:1/d"], "sha256": "0" * 64, "size": "10KB"},
    ]}), encoding="utf-8")
    srv = _serve(make_admin_app(_admin_ctx(store, {})))
    port = next(srv)
    st, j = _jget(port, "/api/firmware")
    assert st == 200 and len(j["items"]) == 1 and j["items"][0]["size"] == 0, \
        "脏 size 折叠 0，面板不整表灭"
    st, j = _jpost(port, "/api/firmware/issue", {"version": "5.5.5"})
    assert st == 400, "无在盘包签发走结构化 400 而非 500"
    st, j = _jpost(port, "/api/firmware/download", {"version": "5.5.5"})
    assert st == 200 and j["success"] is False, "下载失败如实 JSON 不 500"


def test_index_rebuild_from_public(store):
    """v1.0.65 F-OTA-10：index.json（唯一账本）损坏后按 public 实盘重算，
    已收编固件不再变幽灵。"""
    _drop(store, "huijian-s3-2.9.7.bin", b"r" * 48)
    assert store.issue("2.9.7"), "前置：正常可签发"
    store.index_file.write_text("{broken json", encoding="utf-8")
    rows = store.versions()
    assert any(r["version"] == "2.9.7" and r["on_disk"] and len(r["sha256"]) == 64
               for r in rows), "损坏账本必须触发现盘重建而非空表"


# ── 集成侧关键形态（源码文本级钉，仿 config_flow 先例）──────────────
HTTP_PY = Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai" / "huijian" / "http.py"


def test_panel_ota_js_contract():
    """v1.0.65：面板 JS 与后端新数据形绑定——urls 已从对外 JSON 剥离（安全 F3），
    拉取按钮必须改读 urls_count；设备行必须呈现 fw_source（陈旧版本如实标注）。"""
    html = (Path(__file__).resolve().parents[1] / "www" / "index.html").read_text(
        encoding="utf-8")
    i = html.index("async function loadOta")
    block = html[i:html.index("/* ── 调试 ── */")]
    assert "(it.urls_count||0)" in block and "it.urls||[]" not in block, \
        "拉取按钮条件须用脱敏后的 urls_count"
    assert "it.urls_hosts" in block
    assert "d.fw_source" in block, "「入驻时」兜底版本必须在 UI 区分标注"
    assert "空包!" in block, "0 字节在盘包必须标红（设备按 content_length==0 拒收）"
    assert "(fw.items||[]).slice(0, 2)" in block, \
        "固件仓表显示面钉：只列最新 2 版（items 已 vkey 降序；API 全量供排障不动）"


def test_satellites_view_registered_and_authed():
    src = HTTP_PY.read_text(encoding="utf-8")
    assert "register_view(HuijianSatellitesView)" in src
    i = src.index("class HuijianSatellitesView")
    block = src[i:i + 2600]
    assert "requires_auth = True" in block, "卫星台账含内网拓扑，必须 HA 令牌"
    assert '/api/huijian-ai/satellites"' in block
    assert "available" in block and "fw_version" in block


def test_ledger_fw_version_chain(tmp_path):
    """v1.0.65 契约 F-02 断链修复的形态钉：固件唯一带 fw_version 的 POST 落
    SetupView（/setup/qrcode）→ speak_id 台账；建账时版本进 entry.data 持久化；
    SatellitesView 两级回退输出（speak_id 台账→entry.data；死改名链移除后 mac 台账已删）。
    集成文件不可在本 venv import HA，按仓内先例走源码形态钉。"""
    src = HTTP_PY.read_text(encoding="utf-8")
    i = src.index("class HuijianSetupView")
    setup_block = src[i:src.index("class HuijianRemoveView")]
    assert 'satellite_ledger_by_speakid' in setup_block, \
        "CMD20 入驻 POST 的 fw_version 必须按 speak_id 入账（断链主修点）"
    assert 'setup_data.get("fw_version")' in setup_block
    assert '"noise_psk"' not in setup_block.split("Setup qrcode")[1][:200] \
        or "if k != " in setup_block, "setup_data 整包日志必须剔除 noise_psk（凭据面）"
    j = src.index("class HuijianSatellitesView")
    view_block = src[j:j + 4200]
    assert "satellite_ledger_by_speakid" in view_block and \
        'entry.data.get("fw_version"' in view_block, "视图必须两级回退（入驻→建账）"
    assert '"fw_source"' in view_block, "陈旧版本必须如实标源，面板不作假"
    cf = (HTTP_PY.parents[1] / "config_flow.py").read_text(encoding="utf-8")
    assert '"fw_version":' in cf, "config_flow 建账时持久化 fw_version"


def test_orphan_endpoints_stay_deleted():
    """v1.1.11（A1）反向钉：删掉的孤儿端点不得复活，存活的 View 一个不得少。

    `/api/huijian-ai/device-info`（当年为小程序 queryHaDevice 而加，但该云函数本身
    已在小程序 v1.4.29 删除）与 `/api/huijian-ai/update/speakname`（固件触发点 CMD30
    PROPERTY_DEVICE_NAME 已随固件 A3 删除）三端零调用方。

    **钉语法、不钉裸标识符**：判"类是否真被定义 / URL 是否真被挂上去"，而不是判名字
    有没有在文件里出现过——否则一句记录本次删除的注释（CHANGELOG 里就有这两条路由）
    会把钉弄红；红过一次，下一个人就会把钉删掉，钉等于没有。
    **必须双向判**：只钉"不得复活"的话，把整个 http.py 清空也能全绿——所以同时钉
    存活 6 个 View 逐个仍在注册块里，且注册总数正好 6（防"删一个偷偷加一个"抵消）。
    """
    import re

    src = HTTP_PY.read_text(encoding="utf-8")
    for cls in ("HuijianDeviceInfoView", "HuijianSetNameView"):
        assert not re.search(rf"^\s*class\s+{cls}\b", src, re.M), \
            f"class {cls} 是 v1.1.11 删掉的孤儿端点，不得复活"
        assert f"register_view({cls})" not in src, f"{cls} 不得被重新注册"
    for route in ('url = "/api/huijian-ai/device-info"',
                  'url = "/api/huijian-ai/update/speakname"'):
        assert route not in src, f"{route} 是 v1.1.11 删掉的孤儿路由，不得复活"
    # mac 维度台账随死改名链一并移除：唯一写点在被删的 speakname 口，且它只带
    # speak_name/speak_id、从不带 fw_version ⇒ 该 tier 恒空（留着还会以 `or` 短路
    # 屏蔽掉真有版本的 speak_id 台账）。钉的是**取用语法**，注释里提它不算。
    assert not re.search(r'get\(\s*"satellite_ledger"\s*[,)]', src), \
        "mac 台账已删，运行期只剩 satellite_ledger_by_speakid"

    alive = ["HuijianSetupView", "HuijianRemoveView", "HuijianTtsSttView",
             "HuijianSatellitesView", "HuijianSatelliteOtaView",
             "HuijianSatelliteContinuousView"]
    setup = src[src.index("async def async_setup_https"):src.index("class HuijianHttpView")]
    for v in alive:
        assert f"hass.http.register_view({v})" in setup, f"{v} 必须仍被注册"
    assert setup.count("register_view") == len(alive), (
        f"注册数应为 {len(alive)}，实得 {setup.count('register_view')}——"
        "多出来的是谁？新端点必须先证明有调用方（A1 的教训：两个口挂了多年零调用）")


# ── v1.0.77 仓库真 lock 完整性钉（发布账本手滑防线）──────────────────

REPO_LOCK = Path(__file__).resolve().parents[1] / "firmware.lock.json"


def test_repo_lock_integrity():
    """_doc 纪律机械化：结构健全 + 现行版本条目与构建实测对账（禁凭记忆填值）。"""
    import re
    data = json.loads(REPO_LOCK.read_text(encoding="utf-8"))
    rels = data["releases"]
    assert rels, "发布锁不得为空表"
    for r in rels:
        v = str(r.get("version", ""))
        assert re.fullmatch(r"\d+\.\d+\.\d+", v), f"version 须 x.y.z：{v!r}"
        assert r.get("file") == f"huijian-s3-{v}.bin", "落盘名与版本必须对应"
        assert re.fullmatch(r"[0-9a-f]{64}", str(r.get("sha256", ""))), "sha256 缺=拒上架"
        assert isinstance(r.get("size"), int) and r["size"] > 0, "size 须正整数"
        urls = r.get("urls") or []
        assert urls and all(str(u).startswith("https://") for u in urls), \
            "至少一源且全 https（GitHub→Gitee 容灾序）"
        assert r.get("notes_zh"), "对外话术必填（内部文档禁发约束下的最小必要说明）"
    cur = next(r for r in rels if r["version"] == "2.1.48")
    assert cur["sha256"] == \
        "9a4c2685884ee35631a831ade8476fb4238d06f317bdea772ceb0a7de3a0b9e7", \
        "2.1.48 sha 与固件仓构建实测对账不符（源：0513gujian commit a6cdf91 消息）"
    assert cur["size"] == 2859488


# 历史例外：2.1.65 登记成了产线用的合并出厂镜像（8,786,984B），装不进 app 槽。
# 不改写已发布历史，但由运行时容量闸挡住并回精确拒因；新条目一律不得超限。
OTA_LEGACY_OVERSIZE = {"2.1.65"}


def test_ota_capacity_gate():
    """F1（2026-09-26）：OTA 载荷尺寸闸。设计文档 L88「size ≤ 槽容量-10% 服务端预检」
    与 L92「CI 加 bin 尺寸闸(>3.7MB 红)」两条**从未实现**，于是 lock 把合并出厂镜像
    登记成 OTA 载荷（2.1.65/2.1.66 均 8,786,984B）；设备侧唯一在岗的 B3 分区闸把它
    确定性拒绝（`exceeds OTA partition 4128768`），而面板只会显示"已下发"。
    本钉同时充当文档要求的那道 CI 闸（Lint job 跑全量 pytest）。"""
    from core import firmware_store as fs
    assert fs.ota_capacity_error("9.9.9", fs.OTA_MAX_BYTES) == "", "恰好等于上限必须放行"
    r = fs.ota_capacity_error("9.9.9", fs.OTA_MAX_BYTES + 1)
    assert r and "app 镜像" in r, "超 1B 必须拒，且拒因要点名「登记错了载荷类型」"
    assert fs.ota_capacity_error("9.9.9", "脏值") == "", "size 脏值折叠为 0 时不得误拒"
    data = json.loads(REPO_LOCK.read_text(encoding="utf-8"))
    for rel in data["releases"]:
        v, sz = str(rel.get("version")), int(rel.get("size") or 0)
        if v in OTA_LEGACY_OVERSIZE:
            assert v == "2.1.65" and sz > fs.OTA_MAX_BYTES, f"历史例外表跑偏：{v} size={sz}"
            continue
        assert sz <= fs.OTA_MAX_BYTES, (
            f"v{v} 登记了 {sz}B > OTA 槽可用上限 {fs.OTA_MAX_BYTES}B：OTA 载荷必须是 app "
            f"镜像，合并出厂镜像只给产线 flash_tool 用（超槽会被设备 B3 闸拒绝）")


def test_ota_capacity_refusal(tmp_path, monkeypatch):
    """issue() 真拒签超槽包。最后那条"同尺寸放行"是对照——没有它，前面红的可能只是
    on_disk/sha_mismatch 之类更早的分支，容量闸等于没被验过。"""
    import hashlib

    from core import firmware_store as fs
    blob = b"z" * 40
    lock = tmp_path / "firmware.lock.json"
    lock.write_text(json.dumps({"releases": [
        {"version": "2.9.9", "file": "huijian-s3-2.9.9.bin",
         "urls": ["http://127.0.0.1:1/a.bin"], "sha256": hashlib.sha256(blob).hexdigest(),
         "size": 40, "notes_zh": "容量闸测试"}]}), encoding="utf-8")
    st = fs.FirmwareStore(root=tmp_path / "data", lock_path=lock)
    st.import_dir.mkdir(parents=True, exist_ok=True)
    _drop(st, "huijian-s3-2.9.9.bin", blob)      # 走投递口（含 mtime 静止闸适配）
    assert st.scan_import() == 1, "测试夹具没把包收编进 public，后面全是空转"
    monkeypatch.setattr(fs, "OTA_MAX_BYTES", 39)
    assert st.issue("2.9.9") is None, "超槽载荷必须拒签（白烧一次性令牌没意义）"
    assert "超 OTA 槽可用上限" in st.capacity_error("2.9.9"), "API 层拿到的必须是容量原因"
    monkeypatch.setattr(fs, "OTA_MAX_BYTES", 40)
    assert st.issue("2.9.9"), "同尺寸必须放行——否则上面的红不是容量闸造成的"


# ── v1.0.74 OTA 真下发：/api/firmware/dispatch + 集成中继视图 + 面板接线 ──

def _dispatch_ctx(store, ha):
    return AppContext(settings=SettingsFake(), ha=ha, firmware=store,
                      started_at=time.time(), host="10.0.0.9")


def test_dispatch_happy_path(store, monkeypatch):
    """全链闭环：mac 必填→issue 签一次性链接→rest_write 中继集成→
    返回体含 note；且签出的 URL 物理可领取（take 一次成、二次废）。"""
    monkeypatch.setattr("core.ota_api.local_ip", lambda: "10.0.0.9")
    ha = FakeHAClient(rest=_sat_ledger())
    _drop(store, "huijian-s3-2.9.7.bin", b"d" * 9)
    srv = _serve(make_admin_app(_dispatch_ctx(store, ha)))
    port = next(srv)
    st, j = _jpost(port, "/api/firmware/dispatch", {"mac": "aa"})
    assert st == 200 and j["success"] and j["version"] == "2.9.7" and j["note"], j
    assert len(ha.written) == 1
    m, p, b = ha.written[0]
    assert (m, p) == ("POST", "/api/huijian-ai/satellites/ota")
    assert b["mac"] == "aa" and b["url"].startswith("http://10.0.0.9:8000/firmware/")
    from urllib.parse import parse_qs, urlparse
    fname = urlparse(b["url"]).path.rsplit("/", 1)[-1]
    tok = parse_qs(urlparse(b["url"]).query)["t"][0]
    assert store.take(tok, fname) is not None, "签出的链接必须真实可领取"
    assert store.take(tok, fname) is None, "一次性：二次领取必废"


def test_dispatch_requires_mac(store):
    ctx = _dispatch_ctx(store, FakeHAClient(rest=_sat_ledger()))
    srv = _serve(make_admin_app(ctx))
    port = next(srv)
    st, j = _jpost(port, "/api/firmware/dispatch", {})
    assert st == 400 and not j["success"]
    assert not ctx.ha.written, "无 mac 不得进中继（防发错机在签发前就掐）"


def test_dispatch_bridge_down_no_issue(store):
    """HA 桥断：不签发、不中继、502 结构化——防废令牌空烧与静默假成功。"""
    ha = FakeHAClient(rest=_sat_ledger())
    ha.ok = False
    _drop(store, "huijian-s3-2.9.7.bin", b"d" * 9)
    srv = _serve(make_admin_app(_dispatch_ctx(store, ha)))
    port = next(srv)
    st, j = _jpost(port, "/api/firmware/dispatch", {"mac": "aa"})
    assert st == 502 and not j["success"] and not ha.written


def test_dispatch_relay_reject_collapse(store, monkeypatch):
    """中继端拒绝（无接收口/离线）→ 200 结构化如实回显，绝不 500/不吞。"""
    monkeypatch.setattr("core.ota_api.local_ip", lambda: "10.0.0.9")
    ha = FakeHAClient(rest=_sat_ledger(), writes={
        ("POST", "/api/huijian-ai/satellites/ota"):
            {"success": False, "error": "设备无 ota_upgrade 接收口"}})
    _drop(store, "huijian-s3-2.9.7.bin", b"d" * 9)
    srv = _serve(make_admin_app(_dispatch_ctx(store, ha)))
    port = next(srv)
    st, j = _jpost(port, "/api/firmware/dispatch", {"mac": "aa"})
    assert st == 200 and j["success"] is False and "接收口" in j["error"]


def test_dispatch_capacity_refusal_names_reason(store, monkeypatch):
    """HTTP 层双向钉（F1 的另一半）：容量闸在 store 里拒了签，但**面板看到的**是
    `/api/firmware/issue|dispatch` 的回执文字——此前两者一律折叠成"该版本不在盘"，
    把"登记错了载荷类型（合并出厂镜像超槽）"说成"包没放对地方"，两头指错路。
    对照分支（抬上限后放行）证明红的那次真是容量闸造成的。"""
    from core import firmware_store as fs
    monkeypatch.setattr("core.ota_api.local_ip", lambda: "10.0.0.9")
    _drop(store, "huijian-s3-2.9.7.bin", b"d" * 9)

    srv = _serve(make_admin_app(_dispatch_ctx(store, FakeHAClient(rest=_sat_ledger()))))
    port = next(srv)
    monkeypatch.setattr(fs, "OTA_MAX_BYTES", 8)
    st, j = _jpost(port, "/api/firmware/issue", {"version": "2.9.7", "mac": "aa"})
    assert st == 400 and j["success"] is False
    assert "超 OTA 槽可用上限" in j["error"], f"issue 未回具名拒因：{j}"
    assert "不在盘" not in j["error"], "不得再拿『版本不在盘』掩盖容量拒因"

    ha = FakeHAClient(rest=_sat_ledger())
    srv2 = _serve(make_admin_app(_dispatch_ctx(store, ha)))
    port2 = next(srv2)
    st, j = _jpost(port2, "/api/firmware/dispatch", {"version": "2.9.7", "mac": "aa"})
    assert st == 400 and j["success"] is False and "超 OTA 槽可用上限" in j["error"]
    assert not ha.written, "超槽载荷不得进中继（发了也是设备侧确定性拒绝）"

    # 对照：把上限抬到真实尺寸 ⇒ 两条口都应正常放行（否则上面的红不是容量闸）
    monkeypatch.setattr(fs, "OTA_MAX_BYTES", 9)
    st, j = _jpost(port, "/api/firmware/issue", {"version": "2.9.7", "mac": "aa"})
    assert st == 200 and j["success"] and j["url"], j
    st, j = _jpost(port2, "/api/firmware/dispatch", {"version": "2.9.7", "mac": "aa"})
    assert st == 200 and j["success"] and ha.written, j


def test_ota_relay_view_form():
    """集成视图形态钉（HA 不可 import，源码级）：注册在案、HA 令牌闸
    （写命令通道，同台账面口径）、服务发现与台账 ota_services 同源过滤、
    调用作废参 {"url": url} 逐字、全路径折叠无裸 raise。"""
    src = HTTP_PY.read_text(encoding="utf-8")
    assert "register_view(HuijianSatelliteOtaView)" in src
    i = src.index("class HuijianSatelliteOtaView")
    block = src[i:src.index("def parse_tts_stt_options")]
    assert "requires_auth = True" in block
    assert '/api/huijian-ai/satellites/ota"' in block
    assert 'execute_service(svc, {"url": url})' in block
    assert '"ota" in name.lower() or "upgrade" in name.lower()' in block, \
        "接收口发现必须与 satellites 台账 ota_services 同判定源"
    assert "raise" not in block.split('"""')[-1], "视图主体禁裸 raise——永不抛折叠 200 JSON"


def test_panel_dispatch_wiring():
    html = (Path(__file__).resolve().parents[1] / "www" / "index.html").read_text(
        encoding="utf-8")
    assert "async function dispatchOta" in html
    assert '"/api/firmware/dispatch"' in html
    assert "data-remote" in html and 'b.dataset.remote==="1"?dispatchOta' in html, \
        "按钮按接收口存在性双分支：真下发 / 签发备存"
    assert "代发能力未上线" not in html, "旧备存话术须随真下发同步"
