# -*- coding: utf-8 -*-
"""v1.1.27 修批 E1 钉桩：传输契约 / 凭据面 / 前端九条（2026-09-30 深审判定）。

八项各配"真改坏必红"的钉：多为 AST 抽**真实源码**执行（真 anyio / 真函数），
其余为结构字面量（前端与传输层是契约面）。禁止裸标识符钉。

① mcp_transport._process_text_message 恒 None ⇒ 基类 reader 首帧即 break
   （ws_transport:320 `if not await …: break`）= MCP 通道永久不可用 + 重连风暴；
② restart_connection 不校验代次 ⇒ 旧轮收口关掉**新**连接（`_create_streams`
   随之把 `_proto` 归零）；
③ __init__ INFO 整包 entry.data（password/noise_psk/内嵌 ?token= 端点）+
   diagnostics 只按精确键名掩码 ⇒ 端点内 `?token=` 原样外泄；
④ text.py 播报音频落免鉴权 /local 且名字可猜、早退路径永不清理；device_entry.id
   无守卫；announce(blocking=False)+吞错 ⇒ 两路全挂仍返成功；
⑤ stt.py 申报全量（含 OPUS/OGG/48k）而实现写死 16k/mono/16bit-PCM ⇒ 乱码仍 SUCCESS；
⑥ www/index.html 九条（CRLF 文件，钉的是单行契约字面量）；
⑦ conversation.py anyio.fail_after 横跨 yield（tts_transport 根因①同款）+ INFO 打全文；
⑧ encryption_key_storage 把"读取失败"当"首次空表"固化 ⇒ 一次损坏抹掉全部历史 PSK。

跑法：cd huijian_voice && python -m pytest tests/test_v1127_transport_creds_ui.py -q
（音频/TTS 相关需 export PATH=/e/AI/huijian-gateway-plugin-yy/_winlibs:$PATH）
"""
import ast
import asyncio
import importlib.util
import logging
import os
import re
import sys
import textwrap
import time
import types
from collections.abc import AsyncIterable
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
CC = ROOT / "custom_components" / "huijian_ai"
HJ = CC / "huijian"
WWW = ROOT / "www" / "index.html"


# ── 通用：AST 抽真实源码执行（不复制逻辑）────────────────────────────
def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _func_src(path: Path, name: str) -> str:
    """按函数名抽源码段（模块级或类方法皆可，首个命中）。"""
    src = _src(path)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return textwrap.dedent(ast.get_source_segment(src, node))
    raise AssertionError(f"{path.name} 里找不到函数 {name}")


def _method_src(path: Path, cls_name: str, name: str) -> str:
    src = _src(path)
    tree = ast.parse(src)
    cls = next((n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == cls_name), None)
    assert cls is not None, f"{path.name} 找不到类 {cls_name}"
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return textwrap.dedent(ast.get_source_segment(src, node))
    raise AssertionError(f"{cls_name} 里找不到方法 {name}（钉桩失去对象）")


def _exec_fn(src: str, ns: dict, path: Path, name: str):
    ns = dict(ns)
    exec(compile(src, str(path), "exec"), ns)  # noqa: S102
    assert name in ns, f"抽取段里没有 {name}"
    return ns[name]


def _module_consts(path: Path, names) -> dict:
    """抽源码里的模块级常量**真值**执行（判据不得在钉里复写一份）。"""
    src = _src(path)
    ns: dict = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in names for t in node.targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), ns)  # noqa: S102
    missing = set(names) - set(ns)
    assert not missing, f"{path.name} 缺模块级常量 {sorted(missing)}"
    return ns


def _loadota_block(h: str) -> str:
    i = h.index("async function loadOta")
    return h[i:h.index("/* ── 调试 ── */", i)]


def _www() -> str:
    return WWW.read_text(encoding="utf-8")


# ── 真 anyio + huijian 包桩（镜像 test_v1045 纪律：退出时清干净）──────
@pytest.fixture()
def hj_modules():
    """真 anyio 上装载 huijian.{ws,tts,stt}_transport（HA 侧只给最小桩）。"""
    keys = [k for k in list(sys.modules)
            if k == "anyio" or k.startswith("anyio.")
            or k == "huijian" or k.startswith("huijian.")]
    saved = {k: sys.modules[k] for k in keys}
    for k in keys:
        del sys.modules[k]
    mods = _load_hj()
    yield mods
    for k in [k for k in list(sys.modules)
              if k == "anyio" or k.startswith("anyio.")
              or k == "huijian" or k.startswith("huijian.")]:
        del sys.modules[k]
    sys.modules.update(saved)


def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _load_hj():
    pytest.importorskip("anyio", reason="传输层行为钉需真 anyio（内存流语义前提）")
    import anyio  # noqa: F401  真库必须在桩清除后再导入
    for name in ("homeassistant", "homeassistant.config_entries",
                 "homeassistant.core", "homeassistant.exceptions"):
        _stub(name)
    sys.modules["homeassistant"].__path__ = []
    _stub("homeassistant.config_entries",
          ConfigEntry=type("ConfigEntry", (), {}))
    _stub("homeassistant.core", HomeAssistant=type("HomeAssistant", (), {}))
    _stub("homeassistant.exceptions",
          ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (Exception,), {}),
          HomeAssistantError=type("HomeAssistantError", (Exception,), {}))

    class Dict(dict):
        def __getattr__(self, item):
            return self.get(item)

    pkg = types.ModuleType("huijian")
    pkg.Dict = Dict
    pkg.EntryAuthFailedError = RuntimeError
    pkg.get_entry_data = lambda hass, entry, **kw: {}
    pkg.__path__ = [str(HJ)]
    sys.modules["huijian"] = pkg

    mods = {"Dict": Dict}
    for name in ("ws_transport", "tts_transport", "stt_transport"):
        spec = importlib.util.spec_from_file_location(f"huijian.{name}", HJ / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"huijian.{name}"] = mod
        spec.loader.exec_module(mod)
        mods[name] = mod
    return mods


class _FakeEntry:
    def async_create_background_task(self, hass, coro, name):
        coro.close()
        return SimpleNamespace(done=lambda: True)


class _FakeWS:
    def __init__(self):
        self.closed = False
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1
        self.closed = True


def _bare_tts(mod):
    return mod.TtsTransport(hass=None, entry=_FakeEntry(),
                            endpoint="ws://h:8000/tts", attr_endpoint="tts_endpoint")


def _mk_streams(t, size=32):
    """真 anyio 内存流对（须在事件循环内创建）。"""
    import anyio
    t._recv_writer, t._recv_reader = anyio.create_memory_object_stream(size)
    t._send_writer, t._send_reader = anyio.create_memory_object_stream(size)


# ══ ① mcp_transport 文本帧返回契约（基类 False=收口，None=判死）═══════
class _McpMsg:
    def __init__(self, payload, boom=False):
        self.data = '{"jsonrpc":"2.0","id":1,"method":"ping"}'
        self._payload = payload
        self._boom = boom

    def json(self):
        if self._boom:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class _FakeJSONRPC:
    @staticmethod
    def model_validate(payload):
        return SimpleNamespace(payload=payload)


def _mcp_process_text():
    src = _method_src(HJ / "mcp_transport.py", "McpTransport", "_process_text_message")
    ns = {
        "aiohttp": SimpleNamespace(WSMessage=type("WSMessage", (), {})),
        "SessionMessage": None,
        "types": SimpleNamespace(JSONRPCMessage=_FakeJSONRPC),
        "logging": logging,
    }
    return _exec_fn(src, ns, HJ / "mcp_transport.py", "_process_text_message")


class _McpSelf:
    def __init__(self, deliver_ok=True):
        self._recv_writer = object()
        self.logger = logging.getLogger("t.v1127.mcp")
        self.deliver_ok = deliver_ok
        self.delivered = []

    async def _deliver(self, writer, item):
        self.delivered.append(item)
        return self.deliver_ok


def test_mcp_text_frame_contract_is_bool_not_none():
    """None 会被基类当"消费端已消失"（ws_transport:320）⇒ 首帧即拆任务组。"""
    fn = _mcp_process_text()

    async def run():
        s = _McpSelf()
        ok = await fn(s, _McpMsg({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
        assert ok is True, "正常文本帧必须返回 True（None ⇒ 基类 break，MCP 永久不可用）"
        bad = await fn(s, _McpMsg(None, boom=True))
        assert bad is True, "解析失败的帧必须返回 True 保链路（留痕即可），不得拆任务组"
        assert s.delivered, "合法帧必须真交付给 MCP server 的 reader"
        gone = await fn(_McpSelf(deliver_ok=False), _McpMsg({"jsonrpc": "2.0"}))
        assert gone is False, "只有消费端消失（_deliver False）才允许 False 收口重连"

    asyncio.run(run())


def test_mcp_process_text_uses_base_deliver_gate():
    src = _method_src(HJ / "mcp_transport.py", "McpTransport", "_process_text_message")
    assert "_deliver(self._recv_writer" in src, \
        "MCP 通道须走基类交付闸（否则 buffer-0 reader 上裸 send 会无限挂）"


# ══ ② restart_connection 代次闸 ═══════════════════════════════════
def test_restart_connection_skips_stale_generation(hj_modules):
    """旧轮收口晚到时（新连接已建）不得关新连接、不得把新连接置未连接。"""
    t = _bare_tts(hj_modules["tts_transport"])
    ws = _FakeWS()
    t._current_ws = ws
    t._is_connected = True
    t._conn_gen = 7

    asyncio.run(t.restart_connection("旧轮收口（gen=5）", generation=5))
    assert ws.close_calls == 0, "旧代次收口关掉了**新**连接（_proto 随之归零）"
    assert t._is_connected is True, "旧代次收口不得把新连接标记为断开"

    asyncio.run(t.restart_connection("当前代次收口", generation=7))
    assert ws.close_calls == 1 and t._is_connected is False, \
        "当前代次必须照常拆连（既有承诺不倒退）"


def test_tts_round_teardown_tagged_with_round_generation(hj_modules):
    """TTS 轮收口清算必须带本轮认领的代次（旧形态裸调 = 拆掉新连接）。"""
    mod = hj_modules["tts_transport"]

    async def scenario():
        t = _bare_tts(mod)
        _mk_streams(t)

        async def _ok():
            return True
        t.ensure_connected = _ok
        sent = []

        async def send(msg):
            sent.append(msg)
        t.send_message = send
        restarts = []

        async def restart(reason="", **kw):
            restarts.append((reason, kw))
        t.restart_connection = restart
        t._conn_gen = 5
        await t._recv_reader.aclose()      # 本轮立刻 EOF 收口（确定性，不用并发）
        agen = t.stream("你好", timeout=5)
        first = await agen.__anext__()
        assert isinstance(first, dict) and "提前断开" in first.get("error", ""), first
        t._conn_gen = 6                    # 收口走到 finally 前，新连接已建
        with pytest.raises(StopAsyncIteration):
            await agen.__anext__()
        assert restarts, "非 stop 收口必须断连清算（v1.0.45 既有承诺）"
        assert restarts[-1][1].get("generation") == 5, \
            "清算没带代次 ⇒ 旧轮 finally 会拆掉新连接（新连接 _proto 归零、新轮白等同步窗）"
    asyncio.run(scenario())


def test_stt_round_teardown_tagged_with_round_generation(hj_modules):
    """STT 未以转录收口的清算同样必须带本轮代次。"""
    mod = hj_modules["stt_transport"]

    async def scenario():
        t = mod.SttTransport(hass=None, entry=_FakeEntry(),
                             endpoint="ws://h:8000/stt", attr_endpoint="stt_endpoint")
        _mk_streams(t, size=1)

        async def _ok():
            return True
        t.ensure_connected = _ok
        sent = []

        async def send(msg):
            sent.append(msg)
            if isinstance(msg, dict) and msg.get("state") == "stop":
                t._conn_gen += 1           # 停止帧发完时新连接已建（换连）
        t.send_message = send
        restarts = []

        async def restart(reason="", **kw):
            restarts.append((reason, kw))
        t.restart_connection = restart

        async def chunks():
            yield b"\x00" * 192
        text, err = await t.recognize(chunks(), timeout=0.05)
        assert text is None and err, "无转录必须如实错误收口"
        assert restarts, "未以转录收口必须断连清算"
        assert restarts[-1][1].get("generation") == 0, \
            "清算没带本轮开局代次 ⇒ 换连后旧账会拆新连接"
        assert t._conn_gen == 1, "前提：清算时连接代次确已前进"
    asyncio.run(scenario())


# ══ ③ 凭据面：条目日志 + 诊断端点值级脱敏 ══════════════════════════
def test_entry_setup_log_keeps_keys_and_whitelist_only():
    ns = _module_consts(CC / "__init__.py", {"_SAFE_LOG_FIELDS"})
    fn = _exec_fn(_func_src(CC / "__init__.py", "_safe_entry_fields"),
                  {"_SAFE_LOG_FIELDS": ns["_SAFE_LOG_FIELDS"]},
                  CC / "__init__.py", "_safe_entry_fields")
    entry = SimpleNamespace(data={
        "config_type": "assist",
        "speak_name": "huijian AI 语音引擎",
        "llm_endpoint": "ws://192.168.1.9:8000/xiaozhi/v1/llm?token=SEKRET-TOKEN",
        "stt_endpoint": "ws://192.168.1.9:8000/xiaozhi/v1/stt?token=SEKRET-TOKEN",
        "tts_endpoint": "ws://192.168.1.9:8000/xiaozhi/v1/tts",
        "mcp_endpoint": "wss://gw.example/mcp?token=SEKRET-TOKEN",
        "password": "p@ssw0rd",
        "noise_psk": "UFBLVFlQU0s=",
    })
    blob = repr(fn(entry))
    for secret in ("SEKRET-TOKEN", "p@ssw0rd", "UFBLVFlQU0s="):
        assert secret not in blob, f"条目日志摘要泄漏凭据：{secret}"
    assert "config_type" in blob and "speak_name" in blob, "白名单非敏感字段要保留（排障用）"
    assert "keys" in blob and "llm_endpoint" in blob, \
        "键名清单要留（排障要看条目有哪些配置面；凭据是**值**，不外流）"
    assert "ws://" not in blob and "wss://" not in blob, \
        "端点值（可内嵌 ?token=）不得进日志——只有键名可以"


def test_setup_entry_log_call_uses_safe_summary():
    src = _src(CC / "__init__.py")
    tree = ast.parse(src)
    call = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "info" and node.args
                and isinstance(node.args[0], ast.Constant)
                and "Setup entry" in str(node.args[0].value)):
            call = node
    assert call is not None, "找不到 Setup entry 日志（钉桩失去对象）"
    seg = ast.get_source_segment(src, call)
    assert "_safe_entry_fields" in seg, "INFO 必须只打白名单摘要"
    assert "entry.data" not in seg, "INFO 不得整包打印 entry.data（password/noise_psk/?token= 端点）"


def test_diagnostics_endpoints_redacted_by_value():
    src = _src(CC / "diagnostics.py")
    for key in ("llm_endpoint", "stt_endpoint", "tts_endpoint", "mcp_endpoint"):
        assert key in src, f"端点键 {key} 未纳入值级脱敏（精确键名掩码管不到 ?token=）"
    assert "_redact_endpoints(diag)" in src, "值级脱敏未接进导出主路径"
    assert src.count("_redact_endpoints(diag)") >= 2, \
        "「条目未加载」的早退路径同样要脱敏（config 在前已填好）"

    consts = _module_consts(CC / "diagnostics.py", {"ENDPOINT_KEYS"})
    mask = _exec_fn(_func_src(CC / "diagnostics.py", "_mask_endpoint_value"),
                    {"Any": object}, CC / "diagnostics.py", "_mask_endpoint_value")
    fn = _exec_fn(_func_src(CC / "diagnostics.py", "_redact_endpoints"),
                  {"Any": object, "ENDPOINT_KEYS": consts["ENDPOINT_KEYS"],
                   "_mask_endpoint_value": mask},
                  CC / "diagnostics.py", "_redact_endpoints")
    payload = {
        "config": {
            "data": {
                "llm_endpoint": "ws://192.168.1.9:8000/xiaozhi/v1/llm?token=SEKRET-TOKEN",
                "mcp_endpoint": "wss://gw.example/mcp?token=SEKRET-TOKEN",
                "password": "p@ssw0rd",
                "speak_name": "客厅",
            },
            "options": {"stt_endpoint": "ws://h:8000/stt?token=SEKRET-TOKEN",
                        "tts_endpoint": "ws://h:8000/tts"},
            "entry_id": "abc123",
        },
    }
    out = fn(payload)
    blob = repr(out)
    assert "SEKRET-TOKEN" not in blob and "token=" not in blob, "端点内嵌 ?token= 原样外泄"
    assert out["config"]["entry_id"] == "abc123", "非端点字段不得被误改"
    assert out["config"]["data"]["speak_name"] == "客厅"
    dumped = out["config"]["data"]["llm_endpoint"]
    assert isinstance(dumped, str) and "len=" in dumped and "head=w" in dumped, \
        "端点脱敏纪律=只留长度+首字节（值级）"
    assert payload["config"]["data"]["mcp_endpoint"].endswith("SEKRET-TOKEN"), \
        "脱敏不得就地改写入参（诊断组装可能复用它）"


# ══ ④ text.py：/local 可猜文件名 + 早退不清理 + 守卫 + 假成功 ════════
def test_tts_audio_file_sweep_is_ttl_based(tmp_path):
    src = _src(CC / "text.py")
    assert "secrets.token_urlsafe" in src, \
        "播报音频仍在免鉴权 /local 用可猜名（tts_<毫秒>.mp3 可枚举探测播报内容）"
    assert "int(time.time() * 1000)" not in src, "毫秒时间戳命名=可猜"
    fn = _exec_fn(_func_src(CC / "text.py", "_cleanup_old_tts_files"),
                  {"time": time, "Path": Path, "_TTS_FILE_TTL_S": 180,
                   "_LOGGER": logging.getLogger("t.v1127.text")},
                  CC / "text.py", "_cleanup_old_tts_files")
    d = tmp_path / "huijian_tts"
    d.mkdir()
    stale = d / "tts_aaaa.mp3"
    fresh = d / "tts_bbbb.mp3"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"y")
    os.utime(stale, (time.time() - 3600, time.time() - 3600))
    fn(SimpleNamespace(), d)
    assert not stale.exists(), \
        "早退/取消路径留下的旧播报音频必须被 TTL 清扫（旧形态只裁「最新 10 个」=永不清理）"
    assert fresh.exists(), "在播/刚写的文件不得被误删"


def test_play_tts_cleans_up_on_early_exit_and_deletes_after_play():
    body = _func_src(CC / "text.py", "_play_tts")
    assert "finally:" in body, "早退路径（无 edge-tts/空音频/无播放器）必须走 finally 清理"
    assert "_cleanup_old_tts_files" in body, "清扫必须挂在 _play_tts 的收口上"
    assert "_expire_tts_file" in body, "播完即删：设备取流预算后删除该文件"
    assert "return False" in body and "return True" in body, \
        "_play_tts 必须如实返回成败（旧形态早退 return None=调用方当成功）"


def test_play_tts_reports_failure_when_edge_tts_missing():
    fn = _exec_fn(_func_src(CC / "text.py", "_play_tts"),
                  {"importlib": importlib, "Path": Path, "time": time,
                   "secrets": __import__("secrets"), "partial": __import__("functools").partial,
                   "_LOGGER": logging.getLogger("t.v1127.text"),
                   "_get_edge_tts_voice": lambda lang: "v",
                   "_TTS_FILE_TTL_S": 180},
                  CC / "text.py", "_play_tts")

    class _Hass:
        class config:  # noqa: N801
            language = "zh-CN"

            @staticmethod
            def path(*a):
                return "/tmp"

        async def async_add_import_executor_job(self, fn_, *a):
            raise ImportError("no edge_tts")

    self_ = SimpleNamespace(hass=_Hass())
    assert asyncio.run(fn(self_, "你好")) is False, \
        "edge-tts 不可用必须如实 False（旧形态静默 return None，两路全挂仍报成功）"


@pytest.fixture()
def _ha_registry_stub():
    names = ["homeassistant", "homeassistant.helpers",
             "homeassistant.helpers.entity_registry"]
    saved = {n: sys.modules.get(n) for n in names}
    _stub("homeassistant")
    sys.modules["homeassistant"].__path__ = []
    _stub("homeassistant.helpers")
    _stub("homeassistant.helpers.entity_registry",
          async_get=lambda hass: SimpleNamespace(async_entries_for_device=lambda r, d: []))
    yield
    for n, m in saved.items():
        if m is None:
            sys.modules.pop(n, None)
        else:
            sys.modules[n] = m


def test_find_media_player_guards_missing_device_entry(_ha_registry_stub):
    fn = _exec_fn(_func_src(CC / "text.py", "_find_media_player"),
                  {"_LOGGER": logging.getLogger("t.v1127.text")},
                  CC / "text.py", "_find_media_player")
    self_ = SimpleNamespace(device_entry=None, hass=object())
    assert asyncio.run(fn(self_)) is None, \
        "device_entry 缺失必须守卫（同文件 _find_satellite:136 同款），不得 AttributeError"


def test_announce_path_is_awaited_and_both_fail_raises():
    """AST 掷真：announce 的 async_call 必须 blocking=True（不可观测=假成功）。"""
    src = _src(CC / "text.py")
    tree = ast.parse(src)
    setv = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_set_value")
    blocking_vals = []
    for node in ast.walk(setv):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "async_call"):
            kv = {k.arg: k.value for k in node.keywords if k.arg}
            if "blocking" in kv:
                blocking_vals.append(ast.literal_eval(kv["blocking"]))
    assert blocking_vals, "async_set_value 里找不到带 blocking 的服务调用（钉桩失去对象）"
    assert True in blocking_vals, \
        "announce 必须可等待（blocking=False ⇒ 派发失败不可观测，两路全挂仍返成功）"
    assert blocking_vals[0] is True, "播报主通道必须 blocking=True"
    assert False not in blocking_vals, "非阻塞派发不得回购"
    seg = ast.get_source_segment(src, setv)
    assert "raise HomeAssistantError" in seg, "两路全挂必须如实抛错/置失败态"
    assert "return False" in _func_src(CC / "text.py", "_play_tts"), \
        "回退路的成败必须可判（否则「全挂」无从判定）"


# ══ ⑤ stt.py：申报面对齐实现 + 不支持形态如实 ERROR ═══════════════════
def test_stt_declaration_matches_implementation():
    src = _src(CC / "stt.py")
    # 成员拼写与仓内实测面一致（huijian/http.py:462 与 _bench/ha_core_ref 的
    # SpeechMetadata 构造同款：BITRATE_16 / SAMPLERATE_16000 / CHANNEL_MONO）
    assert "self._attr_supported_codecs = [AudioCodecs.PCM]" in src, \
        "申报含 OPUS 而 wav_to_opus 只剥 RIFF 头按 PCM 解 ⇒ 交 OPUS 会当乱码仍回 SUCCESS"
    assert "self._attr_supported_formats = [AudioFormats.WAV]" in src, "OGG 不在实现内"
    assert "self._attr_supported_channels = [AudioChannels.CHANNEL_MONO]" in src, "实现写死 mono"
    assert "self._attr_supported_bit_rates = [AudioBitRates.BITRATE_16]" in src, "16bit PCM"
    assert "self._attr_supported_sample_rates = [AudioSampleRates.SAMPLERATE_16000]" in src, \
        "实现写死 16k（交 48k 会变速乱码）"
    assert "for x in AudioChannels" not in src and "for x in AudioBitRates" not in src
    assert "for x in AudioSampleRates" not in src, "全量申报未收窄"


class _Meta:
    def __init__(self, format, codec, bit_rate, sample_rate, channel, language="zh"):
        self.language = language
        self.format = format
        self.codec = codec
        self.bit_rate = bit_rate
        self.sample_rate = sample_rate
        self.channel = channel


def _stt_process_fn():
    class F:
        WAV = "wav"
        OGG = "ogg"

    class C:
        PCM = "pcm"
        OPUS = "opus"

    class BR:
        BITRATE_16 = 16
        BITRATE_8 = 8

    class SR:
        SAMPLERATE_16000 = 16000
        SAMPLERATE_48000 = 48000

    class CH:
        CHANNEL_MONO = 1
        CHANNEL_STEREO = 2

    class State:
        SUCCESS = "success"
        ERROR = "error"

    class Result:
        def __init__(self, text, state):
            self.text = text
            self.state = state

    calls = []

    class _Transport:
        async def recognize(self, chunks, timeout=60):
            calls.append(chunks)
            return "你好", None

    ns = {
        "SpeechMetadata": _Meta, "SpeechResult": Result, "SpeechResultState": State,
        "AsyncIterable": AsyncIterable, "_LOGGER": logging.getLogger("t.v1127.stt"),
        "stt_transport": SimpleNamespace(get_entry_transport=lambda hass, entry: _Transport()),
        "wav_to_opus": lambda stream, **kw: stream,
        "AudioFormats": F, "AudioCodecs": C, "AudioBitRates": BR,
        "AudioSampleRates": SR, "AudioChannels": CH,
        "calls": calls,
    }
    fn = _exec_fn(_func_src(CC / "stt.py", "async_process_audio_stream"),
                  ns, CC / "stt.py", "async_process_audio_stream")
    return fn, ns


async def _agen(data=b"\x00"):
    yield data


def test_stt_rejects_undeclared_audio_shape():
    fn, ns = _stt_process_fn()
    self_ = SimpleNamespace(hass=object(), entry=object())

    bad = _Meta("ogg", "opus", 16, 48000, 2)
    res = asyncio.run(fn(self_, bad, _agen()))
    assert res.state == "error" and res.text is None, \
        "OPUS/OGG/48k 不在申报面（也不在实现内）必须如实 ERROR"
    assert ns["calls"] == [], "不支持形态不得进 recognize（旧形态交乱码也回 SUCCESS）"

    good = _Meta("wav", "pcm", 16, 16000, 1)
    res2 = asyncio.run(fn(self_, good, _agen()))
    assert res2.state == "success" and res2.text == "你好", "申报形态必须照常走通（不倒退）"


# ══ ⑥ www/index.html 九条（CRLF：只钉单行契约）═══════════════════════
def test_www_stt_provider_cloud_prefix_match():
    h = _www()
    i = h.index('$("#stt_provider").value = ')
    line = h[i:h.index("\n", i)]
    assert 'startsWith("cloud")' in line, \
        "STT 云档严格判等会把 cloud_openai_compat 显示成本地（TTS 侧同款已修）"
    assert '"local_paraformer";' in line, "本地回落档必须保留"


def test_www_unknown_tts_provider_falls_back_to_melo():
    h = _www()
    i = h.index('$("#tts_provider").value = String(S.tts.provider')
    seg = h[i:i + 400]
    assert "local_melo" in seg and '? S.tts.provider : "local_melo"' in seg, \
        "未知 tts.provider 的前端回落必须与后端 core/tts.py _provider() 一致（melo）"
    assert 'S.tts.provider : "local_kokoro"' not in seg, \
        "未知值静默回落 Kokoro=显示与实跑引擎不一致，且保存即写回改写配置"
    j = h.index("const voiceEngine")
    vseg = h[j:h.index("function renderVoiceTable", j)]
    assert ': "local_melo"' in vseg, "voiceEngine 未知档同样回落 melo（与后端同判据）"
    assert ': "local_kokoro"' not in vseg, "voiceEngine 仍回落 Kokoro（表里没有的引擎档）"


def test_www_ota_latest_strict_source_not_overridden_by_lax():
    blk = _loadota_block(_www())
    assert "dv.latest || fw.latest" not in blk, \
        "宽松源（/api/firmware.latest=只按在盘+非哈希不符）不得顶掉严格源"
    i = blk.index("_otaLatest =")
    seg = blk[i:i + 200]
    assert "dv.latest" in seg and "dv.error" in seg and "fw.latest" in seg, \
        "严格源（/api/devices.latest=仅可 OTA 版本）优先；宽松源只在严格源不可得时兜底"
    assert seg.index("dv.latest") < seg.index("dv.error") < seg.index("fw.latest"), \
        "兜底顺序：严格源 → 严格源不可得（请求失败）→ 才看宽松源"


def test_www_no_package_is_not_reported_up_to_date():
    blk = _loadota_block(_www())
    assert "无可升级包" in blk, "无在架包时设备行显示「已最新」=假话"
    i_new = blk.index("已最新")
    assert "_otaLatest ?" in blk[i_new - 140:i_new], "「已最新」必须以严格源非空为守卫"
    i_none = blk.index("无可升级包")
    assert "_otaLatest" in blk[i_none - 200:i_none], "「无可升级包」须由 _otaLatest 为空触发"


def test_www_status_poll_has_inflight_latch_and_gray_card():
    h = _www()
    i = h.index("async function refreshStatus")
    blk = h[i:h.index("setInterval(refreshStatus", i)]
    assert "if (_stBusy) return;" in blk, "轮询缺在飞闩：慢响应下多轮并发互踩冻结计数/DOM"
    assert "_stBusy = false" in blk, "闩必须在 finally 释放（否则一次卡死=状态页永冻）"
    assert "finally" in blk, "释放必须走 finally（早退/异常路径都要放）"
    assert blk.count("#statusGrid") >= 2, \
        "拉不到 status.json 必须把状态卡置灰/清空（旧形态留着上一轮绿字「正常」继续骗人）"


def test_www_tts_speed_blank_not_silently_rewritten_to_1():
    h = _www()
    assert 'parseFloat($("#tts_speed").value)||1' not in h, \
        "留空被改写成 1.0（旧写法 parseFloat(\"\")||1）——把当前 1.25 悄悄改成 1.0"
    assert "Number.isFinite(_spd)" in h and "_spd < 0.6" in h and "_spd > 2" in h, \
        "语速必须过有限性+区间校验后再谈保存"
    assert "speed:_spd" in h, "保存体必须用校验过的值"


def test_www_preset_dropdown_has_unselected_placeholder():
    h = _www()
    i = h.index("function wirePresets()")
    j = h.index("PRESETS[kind].forEach", i)
    assert 'new Option("（未选）"' in h[i:j], \
        "预设下拉缺「（未选）」占位 ⇒ 首项被隐式选中，用户以为已选平台"


def test_www_innerhtml_error_messages_escaped():
    h = _www()
    blk1 = h[h.index("async function loadFunnel"):h.index('$("#funnelBtn")')]
    assert "esc0(e.message)" in blk1 and "+e.message+" not in blk1, \
        "漏斗读取失败话术进 innerHTML 必须先 esc0（错误串可含 <>&\"）"
    blk2 = h[h.index('$("#ttsBtn").onclick'):h.index("async function loadScenes")]
    assert "esc0(e.message)" in blk2 and "+e.message+" not in blk2, \
        "TTS 试听失败话术进 innerHTML 必须先 esc0"


# ══ ⑦ conversation.py：超时 scope 不横跨 yield + 全文降 DEBUG ════════
@pytest.fixture()
def _real_anyio():
    keys = [k for k in list(sys.modules) if k == "anyio" or k.startswith("anyio.")]
    saved = {k: sys.modules[k] for k in keys}
    for k in keys:
        del sys.modules[k]
    yield
    for k in [k for k in list(sys.modules) if k == "anyio" or k.startswith("anyio.")]:
        del sys.modules[k]
    sys.modules.update(saved)


def _conv_timeout_fn(anyio, ns_extra=None):
    ns = {"anyio": anyio, "time": time, "_LOGGER": logging.getLogger("t.v1127.conv")}
    ns.update(ns_extra or {})
    return _exec_fn(_method_src(CC / "conversation.py", "HuijianConversationEntity",
                                "_await_message_with_timeout"),
                    ns, CC / "conversation.py", "_await_message_with_timeout")


def test_conversation_cross_task_close_no_cancel_scope_error(_real_anyio):
    """tts_transport 根因①同款：任一 anyio scope 绝不横跨 yield。"""
    import anyio

    fn = _conv_timeout_fn(anyio)

    class FakeT:
        async def await_message(self):
            yield {"role": "assistant", "content": "第一句"}
            await anyio.sleep(30)

    async def scenario():
        agen = fn(SimpleNamespace(), FakeT(), timeout=5)
        first = await agen.__anext__()
        assert first == {"role": "assistant", "content": "第一句"}

        async def closer():
            try:
                await agen.aclose()
                return None
            except BaseException as e:  # noqa: BLE001
                return e

        err = await asyncio.create_task(closer())
        assert err is None, f"跨任务收口仍炸（根因①现场原文）: {err!r}"

    asyncio.run(scenario())


def test_conversation_timeout_still_yields_error(_real_anyio):
    import anyio

    fn = _conv_timeout_fn(anyio)

    class Silent:
        async def await_message(self):
            await anyio.sleep(30)
            yield {"role": "assistant", "content": "永不"}

    async def scenario():
        agen = fn(SimpleNamespace(), Silent(), timeout=0.05)
        first = await agen.__anext__()
        assert isinstance(first, dict) and "error" in first, \
            "整轮超时必须照旧 yield 超时话术（行为不倒退）"
        with pytest.raises(StopAsyncIteration):
            await agen.__anext__()

    asyncio.run(scenario())


def test_conversation_llm_text_logged_at_debug():
    src = _src(CC / "conversation.py")
    assert '_LOGGER.debug("LLM response' in src, "LLM 应答全文属隐私文本，按本仓纪律降 DEBUG"
    assert '_LOGGER.info("LLM response' not in src, "INFO 落盘=隐私文本外流"


# ══ ⑧ encryption_key_storage：损坏=拒写（不得空表覆盖）═════════════════
def _load_enc_storage():
    for name in ("homeassistant", "homeassistant.core", "homeassistant.helpers",
                 "homeassistant.util"):
        _stub(name)
    sys.modules["homeassistant"].__path__ = []
    _stub("homeassistant.core", HomeAssistant=type("HomeAssistant", (), {}))

    class _FakeStore:
        """HA Store 替身：path 可指真实文件，async_load 可返 None / raise。"""
        def __init__(self, hass, version, key, encoder=None):
            self.hass = hass
            self.path = Path(os.devnull)
            self.load_result = None
            self.load_error = None
            self.saved = []
            self.load_calls = 0

        def __class_getitem__(cls, item):
            return cls

        async def async_load(self):
            self.load_calls += 1
            if self.load_error is not None:
                raise self.load_error
            return self.load_result

        async def async_save(self, data):
            self.saved.append(data)

    _stub("homeassistant.helpers.json", JSONEncoder=object)
    _stub("homeassistant.helpers.singleton",
          singleton=lambda *a, **kw: (lambda f: f))
    _stub("homeassistant.helpers.storage", Store=_FakeStore)
    _stub("homeassistant.util.hass_dict", HassKey=lambda k: k)

    spec = importlib.util.spec_from_file_location(
        "v1127_enc_storage", CC / "encryption_key_storage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _EncHass:
    async def async_add_executor_job(self, fn, *a):
        return fn(*a)


def test_encryption_storage_missing_file_is_first_run_then_writes(tmp_path):
    mod = _load_enc_storage()
    st = mod.ESPHomeEncryptionKeyStorage(_EncHass())
    st._store.path = tmp_path / "not-there.json"
    st._store.load_result = None
    asyncio.run(st.async_store_key("AA:BB", "key1"))
    assert st._store.saved == [{"keys": {"aa:bb": "key1"}}], \
        "文件不存在=首次运行：照旧建表写盘（行为不倒退）"


def test_encryption_storage_corrupt_file_refuses_write(tmp_path):
    """文件在却读不出（损坏）⇒ 拒写；旧形态固化成空表，下一写即抹掉全部历史。"""
    mod = _load_enc_storage()
    st = mod.ESPHomeEncryptionKeyStorage(_EncHass())
    bad = tmp_path / "esphome.encryption_keys"
    bad.write_text("{broken json", encoding="utf-8")
    st._store.path = bad
    st._store.load_result = None
    asyncio.run(st.async_store_key("AA:BB", "key1"))
    assert st._store.saved == [], \
        "损坏文件下写盘=用空表覆盖，全部历史 PSK 一次抹掉"
    assert asyncio.run(st.async_get_key("AA:BB")) is None, "读取失败如实回 None（不猜）"


def test_encryption_storage_load_raise_refuses_write(tmp_path):
    mod = _load_enc_storage()
    st = mod.ESPHomeEncryptionKeyStorage(_EncHass())
    st._store.path = tmp_path / "x.json"
    st._store.load_error = ValueError("Error loading esphome.encryption_keys")
    asyncio.run(st.async_store_key("CC:DD", "key2"))
    assert st._store.saved == [], "Store.async_load 抛错（现代 HA 对损坏 JSON 即如此）必须拒写"
    assert asyncio.run(st.async_remove_key("CC:DD")) is None  # 不得抛
    assert st._store.saved == []
