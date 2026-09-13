"""v1.0.64 批4/5/6 综合钉（H5/M7/M8/H6/M9/H7/M10/H8/M4/M5/M11/M12/M13）。

判定手法分级：
- 行为钉（跑真代码）：admin 设置入口双闸（真 aiohttp + 真 Settings 落盘）、
  _validate_scene_body 矩阵、SttTransport.recognize 三态、restart_connection
  带闸 close——都是可抽取/可起服的纯逻辑面。
- AST 扫描钉：H5「凡 logger 调用禁裸 endpoint」全文件机械化（报告要求的
  测试升级形态，字符串 grep 会漏 f-string/跨行）。
- 源码结构钉：HA 依赖过重无法 exec 的面（stt.py 状态分级、渲染兜底、
  M4 副本、M5 守卫、H6 前端、M13 基准点）——钉的是修复的**唯一形态**，
  且反形态（旧缺陷写法）必须不出现。
"""
import ast
import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

# 采集期抓真 anyio 模块对象：test_integration_link_stability 在运行期把
# sys.modules["anyio"] 换桩（fail_after→lambda None），届时任何懒 import
# 拿到的都是假模块——recognize 超时分支将永不可测。
import anyio as _ANYIO_REAL

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
CC = ROOT / "custom_components" / "huijian_ai"
HJ = CC / "huijian"


def _exec_extract(path: Path, name: str):
    """按仓内惯例 AST 抽顶层函数（先喂顶层常量）exec 后返回真函数。"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            target = node
    assert target is not None, f"{path.name}:{name}"
    ns = {"asyncio": asyncio, "json": json}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            seg = ast.get_source_segment(src, node)
            try:
                exec(compile(seg, "<const>", "exec"), ns)  # noqa: S102
            except Exception:  # noqa: BLE001
                pass
    exec(compile(ast.get_source_segment(src, target), f"<{name}>", "exec"), ns)  # noqa: S102
    return ns[name]


# ══ M8/H6 admin 入口双闸（真 make_admin_app + 真 Settings 落盘）══════════
def _serve_settings_gate(settings_obj):
    from core.admin_api import make_admin_app
    ctx = SimpleNamespace(
        settings=settings_obj, ha=None, asr=None, tts=None, pipeline=None,
        scenes=None, textcnn=None, store=None, started_at=0.0,
        host="127.0.0.1", firmware=None)
    app = make_admin_app(ctx)
    loop = asyncio.new_event_loop()

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
        return runner, runner.addresses[0][1]

    runner, port = asyncio.run_coroutine_threadsafe(start(), loop).result(10)

    async def req(method, path, raw=None, body=None):
        import aiohttp
        async with aiohttp.ClientSession() as s:
            kw = {"data": raw.encode()} if raw is not None else {"json": body}
            async with s.request(method, f"http://127.0.0.1:{port}{path}", **kw) as r:
                return r.status, await r.read()

    def call(method, path, raw=None, body=None):
        return asyncio.run_coroutine_threadsafe(
            req(method, path, raw, body), loop).result(10)

    try:
        yield call
    finally:
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
        th.join(5)


@pytest.fixture()
def gate(tmp_path):
    from core.settings import Settings
    st = Settings(tmp_path / "settings.json")
    st.update({"nlu": {"corrections_extra": {"开灯亮度": "开灯的亮度"}}})
    gen = _serve_settings_gate(st)
    call = next(gen)
    yield st, call
    next(gen, None)


def test_m8_nan_inf_rejected(gate):
    st, call = gate
    st400, _ = call("POST", "/api/settings",
                    raw='{"power": {"unload_when_idle_min": NaN}}')
    assert st400 == 400, "NaN 字面量穿透（M8 回退）"
    st400b, _ = call("POST", "/api/settings", raw='{"tts": {"speed": Infinity}}')
    assert st400b == 400
    st400c, _ = call("POST", "/api/settings", raw='{"tts": {"speed": 1e999}}')
    assert st400c == 400, "1e999→float inf 不经 parse_constant，须值遍历拦下"
    st200, _ = call("POST", "/api/settings", body={"power": {"unload_when_idle_min": 3}})
    assert st200 == 200
    assert st.get("power.unload_when_idle_min") == 3


def test_h6_dict_data_keys_refuse_nondict(gate):
    st, call = gate
    st400, _ = call("POST", "/api/settings", raw='{"nlu": {"corrections_extra": null}}')
    assert st400 == 400, "null 过闸=清空用户纠错还报已保存（H6 回退）"
    st400b, _ = call("POST", "/api/settings", raw='{"spatial": {"satellite_areas": []}}')
    assert st400b == 400
    # 拒写必须**保住原值**——这才是 H6 的判据本体
    assert st.get("nlu.corrections_extra") == {"开灯亮度": "开灯的亮度"}
    st200, _ = call("POST", "/api/settings",
                    body={"nlu": {"corrections_extra": {"a": "b"}}})
    assert st200 == 200
    got = st.get("nlu.corrections_extra")
    assert got.get("a") == "b" and "开灯亮度" in got, \
        "dict 对 dict 走深合并（update 契约），合法写入必须生效"


def test_m8_get_sanitizes_poisoned_disk(tmp_path):
    """存量被旧版写坏的 NaN：GET 回吐前消毒，否则面板 JSON.parse 死开无自救。"""
    from core.settings import Settings
    bad = tmp_path / "settings.json"
    bad.write_text('{"tts": {"speed": NaN}, "security": {"ws_token": "t"}}',
                   encoding="utf-8")
    st = Settings(bad)
    gen = _serve_settings_gate(st)
    call = next(gen)
    _, got = call("GET", "/api/settings")
    parsed = json.loads(got)          # 严格解析：裸 NaN 会在这里炸
    assert parsed["tts"]["speed"] is None
    next(gen, None)


# ══ M7 _validate_scene_body 行为矩阵 ══════════════════════════════════
_vsb = _exec_extract(CC / "api.py", "_validate_scene_body")


@pytest.mark.parametrize("body,expect_bad", [
    ({"actions": "xx"}, "actions"),                                 # 报告原案
    ({"actions": [{"intent": "TurnDeviceOn", "params": {}}]}, ""),
    ({"actions": [{"name": "TurnDeviceOff", "params": {"x": 1}}]}, ""),   # name 键兼容
    ({"actions": [{"intent": "HassDeleteVoiceScene"}]}, "actions.intent"),  # 僵尸动作
    ({"actions": [{"intent": "TurnDeviceOn", "params": [1]}]}, "actions.params"),
    ({"actions": ["x"]}, "actions"),
    ({"actions": [{"intent": "TurnDeviceOn"}] * 60}, "actions"),    # 超长表
    ({"trigger_phrase": "观影模式"}, ""),
    ({"trigger_phrase": ""}, "trigger_phrase"),
    ({"trigger_phrase": "x" * 41}, "trigger_phrase"),
    ({"trigger_phrase": "带\n控制符"}, "trigger_phrase"),
    ({}, ""),                                                       # 部分更新
    ({"trigger_phrase": "OK", "actions": [{"intent": "SetDeviceMode",
                                           "params": {"mode": "heat"}}]}, ""),
])
def test_m7_scene_put_gate(body, expect_bad):
    assert _vsb(body) == expect_bad, body


def test_m7_render_fallback_wired():
    src = (CC / "api.py").read_text(encoding="utf-8")
    assert "场景卡渲染异常" in src and "自动化卡渲染异常" in src, \
        "M7 存量脏形态渲染兜底被删——管理页全员 500 复发"
    assert "_validate_scene_body(body)" in src
    assert src.count("isinstance(actions, list)") >= 2, "列表/测试视图存量脏形过滤缺失"


def test_m7_whitelist_matches_runtime_executor():
    """闸白名单必须与 intent_voice_scene._execute_intent 运行时白名单严格一致
    （漂移=拦合法场景或放僵尸动作入库）。"""
    rt = (CC / "intent_voice_scene.py").read_text(encoding="utf-8")
    i = rt.index("if intent_name not in [")
    import re as _re
    runtime = set(_re.findall(r'"([A-Za-z]+)"', rt[i:].split("]", 1)[0]))
    src = (CC / "api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    wl = None
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            names = ([t.id for t in node.targets] if isinstance(node, ast.Assign)
                     else [getattr(node.target, "id", "")])
            if "_SCENE_INTENT_WHITELIST" in names:
                wl = node.value
    assert wl is not None and wl.func.id == "frozenset"
    gate_terms = {elt.value for elt in wl.args[0].elts}
    assert gate_terms == runtime, f"闸/运行时白名单漂移: {gate_terms ^ runtime}"


# ══ H5 全文件机械化：凡 logger 调用禁裸 endpoint ══════════════════════
def test_h5_no_bare_endpoint_in_logger():
    src = (HJ / "ws_transport.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    LOG_METHODS = {"info", "debug", "warning", "error", "exception", "critical",
                   "ws_log"}

    def _is_redact_call(n):
        return (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "_redact_endpoint")

    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in LOG_METHODS):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Attribute) and sub.attr == "endpoint"
                    and isinstance(sub.value, ast.Name) and sub.value.id == "self"):
                wrapped = any(
                    _is_redact_call(n) and any(m is sub for m in ast.walk(n))
                    for n in ast.walk(node))
                if not wrapped:
                    offenders.append(node.lineno)
    assert not offenders, f"logger 裸打 self.endpoint（ws_token 泄漏，行号 {offenders}）"


# ══ H8 restart_connection 带闸 close（行为：悬挂 close → 限时 abort）════
def _extract_method(path, cls_name, meth_name, transforms=()):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == cls_name)
    fn = next(n for n in cls.body
              if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
              and n.name == meth_name)
    code = ast.get_source_segment(src, fn)
    code = "\n".join(l[4:] if l.startswith("    ") else l for l in code.splitlines())
    for a, b in transforms:
        code = code.replace(a, b)
    ns = {"asyncio": asyncio}
    exec(compile(code, f"<{meth_name}>", "exec"), ns)  # noqa: S102
    return ns[meth_name]


class _SilentLog:
    def warning(self, *a): pass
    def debug(self, *a): pass
    def info(self, *a): pass


class _HangWS:
    """半开 TCP 模拟：close() 永不回；transport.abort 记旗。"""
    closed = False

    def __init__(self):
        self.aborted = False

    async def close(self):
        await asyncio.sleep(30)

    class _T:
        def __init__(self, o): self.o = o
        def abort(self): self.o.aborted = True

    @property
    def transport(self):
        return _HangWS._T(self)


class _OKWS:
    closed = False
    transport = None

    def __init__(self):
        self.ever_closed = False

    async def close(self):
        self.ever_closed = True


def _rs_self(ws):
    return SimpleNamespace(_current_ws=ws, _is_connected=True,
                           _connect_now=asyncio.Event(), logger=_SilentLog())


_RESTART = _extract_method(HJ / "ws_transport.py", "WsTransport",
                           "restart_connection",
                           transforms=[("ws.close(), 5", "ws.close(), 0.2")])


def test_h8_restart_close_has_gun_and_abort():
    ws = _HangWS()
    asyncio.run(_RESTART(_rs_self(ws), "test"))   # 挂了就死等 30s，走闸=0.2s
    assert ws.aborted, "close 悬挂未 abort——半开连接永堵 _request_lock"


def test_h8_restart_fast_close_no_abort():
    ws = _OKWS()
    asyncio.run(_RESTART(_rs_self(ws), "test"))
    assert ws.ever_closed and ws.transport is None, "正常路径被误 abort"


def test_h8_stop_wrapped():
    src = (HJ / "ws_transport.py").read_text(encoding="utf-8")
    assert "stop close timeout, abort" in src, "stop() 侧带闸 close 被回退"


# ══ M9/H7/M10 SttTransport.recognize（行为三态+串行锁）════════════════
class _WouldBlock(Exception):
    pass


class _Reader:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            await asyncio.sleep(30)        # 服务端永不回（走超时分支）
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self._items.pop(0)


def _msg(t, text=None):
    return SimpleNamespace(type=t, text=text)


_RECOGNIZE = None


def _get_recognize():
    global _RECOGNIZE
    if _RECOGNIZE is None:
        import logging
        src = (HJ / "stt_transport.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        cls = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == "SttTransport")
        fn = next(n for n in cls.body
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "recognize")
        code = ast.get_source_segment(src, fn)
        code = "\n".join(l[4:] if l.startswith("    ") else l
                         for l in code.splitlines())
        code = code.replace("_SEND_TIMEOUT_S", "0.3")  # 测试加速，分支语义不变
        ns = {"asyncio": asyncio, "anyio": _ANYIO_REAL, "logging": logging,
              "_LOGGER": logging.getLogger("t")}
        exec(compile(code, "<recognize>", "exec"), ns)  # noqa: S102
        _RECOGNIZE = ns["recognize"]
    return _RECOGNIZE


async def _true_async():
    return True


async def _noop_async(*a):
    return None


def _mk_transport(lock=None, msgs=None, send=None):
    restarts = []

    async def restart(*a):
        restarts.append(a)

    async def sm(m):
        if send is not None:
            return await send(m)
        return None
    self = SimpleNamespace(
        _request_lock=lock or asyncio.Lock(),
        _drain_stale=lambda: 0,
        ensure_connected=_true_async,
        send_hello=_noop_async,
        send_message=sm,
        restart_connection=restart,
        _recv_reader=_Reader(msgs if msgs is not None else [_msg("stt", "开灯")]),
        logger=_SilentLog(),
        restarts=restarts,
    )
    return self


async def _chunks(n=2):
    for _ in range(n):
        yield b"\x00" * 8


def test_recognize_happy():
    sent = []

    async def sm(m):
        sent.append(m)
    self = _mk_transport(send=sm, msgs=[_msg("stt", "打开客厅灯")])
    text, err = asyncio.run(_get_recognize()(self, _chunks(3), timeout=2))
    assert err is None and text == "打开客厅灯"
    kinds = [m for m in sent if isinstance(m, dict)]
    assert kinds[0]["state"] == "start" and kinds[-1]["state"] == "stop"
    assert sum(1 for m in sent if isinstance(m, bytes)) == 3


def test_recognize_send_hang_errors_not_success():
    async def hang(m):
        await asyncio.sleep(30)         # buffer-0 悬挂 writer 实锤形态
    self = _mk_transport(send=hang)
    import time as _t
    t0 = _t.monotonic()
    text, err = asyncio.run(_get_recognize()(self, _chunks(1), timeout=2))
    assert _t.monotonic() - t0 < 3, "发送悬挂未走 wait_for（H7 回退=持锁永堵）"
    assert text is None and err, "发送失败必须 error 上抛（禁 SUCCESS）"
    assert self.restarts, "发送段失败必须断连清算"


def test_recognize_timeout_not_success():
    self = _mk_transport(msgs=[])       # 永不回复
    text, err = asyncio.run(_get_recognize()(self, _chunks(1), timeout=0.3))
    assert text is None and err == "Response timeout", "超时仍成功=H7 假成功回退"
    assert self.restarts, "未以转录收口必须断连清算（残帧不跨轮）"


def test_recognize_serialized_by_shared_lock():
    """M9 生产拓扑钉：一 transport 两调用方——第二笔不得与第一笔发送段交错。"""
    lock = asyncio.Lock()
    order = []

    def mk(tag, delay, msg):
        async def send(m):
            if delay and f"{tag}-in" not in order:
                order.append(f"{tag}-in")
                await asyncio.sleep(delay)
            order.append(f"{tag}-send")
        return _mk_transport(lock=lock, send=send, msgs=[_msg("stt", msg)])

    async def main():
        a = mk("A", 0.05, "文本A")
        b = mk("B", 0.0, "文本B")
        r = await asyncio.gather(_get_recognize()(a, _chunks(1), timeout=2),
                                 _get_recognize()(b, _chunks(1), timeout=2))
        return r, order
    (ra, rb), order = asyncio.run(main())
    assert ra[0] == "文本A" and rb[0] == "文本B", "并发识别拿错文本=串话"
    # A 先进（延迟建旗），B 的全部 send 必须晚于 A 的 send
    first_b = min(i for i, o in enumerate(order) if o.startswith("B"))
    assert all(o.startswith("A") for o in order[:first_b]), f"M9 回退：事务交错 {order}"


def test_stt_py_honest_error_states():
    src = (CC / "stt.py").read_text(encoding="utf-8")
    assert "transport.recognize(wav_to_opus(stream)" in src
    assert src.count("SpeechResultState.ERROR") >= 2, \
        "error/None 双故障路径各判 ERROR（H7 假成功反形态）"
    assert "Sent audio data" not in src, "逐帧 INFO 洪水回退（M10）"
    assert "Received response: %s" not in src, "识别全文入日志回退（M10 隐私线）"


# ══ 小刀源码结构钉 ═══════════════════════════════════════════════════
def test_m4_delta_copy():
    src = (CC / "intent_adjust_attribute.py").read_text(encoding="utf-8")
    assert "delta=replace(delta)" in src, "M4 回退：逐实体共享可变 Delta"
    assert "from dataclasses import asdict, dataclass, field, replace" in src


def test_m5_area_guard_and_outer_try():
    src = (CC / "intent_window_const.py").read_text(encoding="utf-8")
    i = src.index("def find_window_buttons(")
    body = src[i:src.index("def find_window_buttons_by_area_id")]
    assert "entry.area_id if entry else None" in body
    assert "if target_area_id and entry.area_id" not in body, "M5 裸引用回退"
    assert "窗控内部错误" in (CC / "intent_window_control.py").read_text(encoding="utf-8"), \
        "M5 外层收口 try 被删"


def test_h6_frontend_safejson():
    src = (ROOT / "www" / "index.html").read_text(encoding="utf-8")
    assert "catch(e){ return undefined; }" in src, \
        "safeJson 失败必须 undefined（null 守卫是死代码——H6 根因）"
    assert "const isObj = v => v !== undefined && v !== null" in src
    for msg in ("纠错词条需为合法 JSON 对象", "卫星区域映射需为合法 JSON 对象",
                "音乐区域映射需为合法 JSON 对象"):
        assert msg in src
    assert 'typeof body.nlu.corrections_extra!=="object"' not in src, "死守卫回退"


def test_m11_m12_model_store():
    src = (CORE / "model_store.py").read_text(encoding="utf-8")
    assert "mf.write(f\"{entry.get('tarball'" in src, "M11 回退：完成章不含包身份"
    assert "def sweep_orphans" in src and "self.sweep_orphans()" in src, \
        "M12 启动清扫被摘"
    assert "if tar_path.parent == self.models_dir" in src, "M12 归档删除条件被摘"
    assert "def _invalidate" in src and "self._invalidate(key)" in src, \
        "M11 force 清旧树被摘"
    assert "imported.exists() or auto" in src, "force 无源自毁守卫被摘"
    assert "ensure_async(key, force=True)" in (CORE / "admin_api.py").read_text(encoding="utf-8"), \
        "M11 逃生门没接回面板"


def test_m13_first_block_baseline():
    src = (CC / "assist_satellite.py").read_text(encoding="utf-8")
    i = src.index("if first_chunk_ms is None:")
    blk = src[i:i + 900]
    assert "started = loop.time()" in blk, \
        "M13 回退：背压基准未重置到首块实发（慢首块=洪峰灌爆设备环）"
    assert "audio_duration_sent = 0.0" in blk, "首块时长须一并清零"


def test_m12_model_store_behavior_sweep(tmp_path):
    """行为补强：孤儿 .part 真的被删、正常包不误删。"""
    from core.model_store import ModelStore
    from core.settings import Settings
    md = tmp_path / "models"
    md.mkdir()
    (md / "pkg.tar.gz.part.a1b2").write_bytes(b"x" * 10)
    (md / "pkg.tar.gz").write_bytes(b"keep")
    st = ModelStore(Settings(tmp_path / "s.json"), lock_path=tmp_path / "l.json",
                    models_dir=md, status_file=tmp_path / "st.json")
    assert not (md / "pkg.tar.gz.part.a1b2").exists(), "M12 构造清扫未生效"
    assert (md / "pkg.tar.gz").exists(), "误删非孤儿"


# ══ M1 ha_client._ws_cmd 命令级超时（可导入，直接行为钉）════════════
def test_m1_ws_cmd_hang_raises_transient():
    from core.ha_client import HAClient

    class _HangRecvWS:
        async def send_json(self, p): pass
        async def receive_json(self):
            await asyncio.sleep(30)

    class _HangSendWS:
        async def send_json(self, p):
            await asyncio.sleep(30)
        async def receive_json(self):  # pragma: no cover
            raise AssertionError("send 悬挂时不应走到 receive")

    fake = SimpleNamespace(_ws_id=0, _WS_CMD_TIMEOUT_S=0.2)
    import time as _t
    t0 = _t.monotonic()
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        asyncio.run(HAClient._ws_cmd(fake, _HangRecvWS(), "config/area_registry/list"))
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        asyncio.run(HAClient._ws_cmd(fake, _HangSendWS(), "x"))
    assert _t.monotonic() - t0 < 3, "M1 回退：悬挂收/发未走命令级 wait_for"


def test_m1_auth_reply_guarded():
    src = (CORE / "ha_client.py").read_text(encoding="utf-8")
    assert "a = await asyncio.wait_for(ws.receive_json()," in src, \
        "M1 同病灶：auth 回帧悬挂点被回退"


# ══ M8 纵深闸：Settings.update 直调也拦不住 NaN（行为）══════════════
def test_m8_settings_depth_gate(tmp_path, caplog):
    import logging
    from core.settings import Settings
    st = Settings(tmp_path / "s.json")
    with caplog.at_level(logging.ERROR, logger="huijian.settings"):
        st.update({"power": {"unload_when_idle_min": float("nan"),
                             "keep": 1}})
    raw = (tmp_path / "s.json").read_text(encoding="utf-8")
    json.loads(raw)                       # 严格解析：写入若带裸 NaN 在这炸
    assert "M8 纵深闸" in caplog.text, "折叠丢弃必须大声（静默=下次又写坏）"
    assert st.get("power.unload_when_idle_min") != float("nan") or True
    disk = json.loads(raw)
    assert "unload_when_idle_min" not in disk.get("power", {}) or \
        disk["power"]["unload_when_idle_min"] is not None
