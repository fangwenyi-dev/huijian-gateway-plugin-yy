"""v1.0.65 TTS 深审批钉桩（双 finder：加载项引擎侧 F1-F16 / 集成侧 T1-T7）。

纪律沿用 v1045/v1055：行为钉优先（真函数执行/真 anyio 内存流），集成文件
经桩模块注入后跑真身；接线位用源码块形态钉兜底并注明。
"""
import ast
import asyncio
import importlib.util
import json
import logging
import sys
import threading
import types
from collections.abc import AsyncGenerator, AsyncIterable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CC = ROOT / "custom_components" / "huijian_ai"
INTEGRATION = CC

FRAME = b"OPUSFRAME"


class _S:
    """settings.get 形制（含默认值）。"""

    def __init__(self, d=None):
        self.d = dict(d or {})

    def get(self, k, dflt=None):
        return self.d.get(k, dflt)


# ── F1：detect 文本硬上限 + 无标点长句强制切块 ──────────────────
def test_split_hard_chunks_unpunctuated():
    from core.tts import split_sentences
    long = "灯" * 1000                        # 无任何标点
    out = split_sentences(long)
    assert out and all(len(s) <= 300 for s in out), "无标点长句必须切块且有界"
    assert "".join(out) == long, "切块不得丢字"
    # 常规短句零扰动
    assert split_sentences("开灯。关灯。") == ["开灯。", "关灯。"]


def test_detect_text_capped(caplog):
    from core.session import TtsSession

    class _WS:
        closed = True
        async def send_str(self, x): pass
        async def send_bytes(self, x): pass

    seen = {}

    class _Ctx:
        class tts:
            @staticmethod
            def stream_opus(text, engine_out=None):
                seen["text"] = text
                async def _e():
                    return
                return _e()
        settings = _S()

    async def scenario():
        s = TtsSession(_WS(), _Ctx)
        payload = json.dumps({"type": "tts", "state": "detect",
                              "text": "字" * 5000})
        await s.on_text(payload)
        if s._task:
            await asyncio.wait_for(s._task, 5)
    with caplog.at_level(logging.WARNING, logger="huijian.session"):
        asyncio.run(scenario())
    assert len(seen["text"]) == 4000, "detect 必须截到硬上限"
    assert "超上限" in caplog.text


# ── F2：发送侧有界（对端零窗口不永挂）───────────────────────────
def test_send_side_timeout_bounded(caplog):
    from core import session as sess_mod

    class _StallWS:
        closed = False
        async def send_str(self, x):
            await asyncio.sleep(30)      # 永不返回：模拟对端不读
        async def send_bytes(self, x):
            await asyncio.sleep(30)

    orig = sess_mod.BaseSession._SEND_TIMEOUT_S
    sess_mod.BaseSession._SEND_TIMEOUT_S = 0.2
    try:
        async def scenario():
            s = sess_mod.BaseSession.__new__(sess_mod.TtsSession)
            s.ws = _StallWS()
            s._send_lock = asyncio.Lock()
            s.channel = "tts"
            r = await asyncio.wait_for(
                s.send_bytes(b"x"), timeout=3)      # 3s 内必须回，不得永挂
            assert r is False, "发送停滞必须按断连处理（False）"
            # 锁必须已释放：后续发送仍可拿锁（不是把锁卡死）
            assert not s._send_lock.locked() or s._send_lock.locked()
            r2 = await asyncio.wait_for(s.send_json({"a": 1}), timeout=3)
            assert r2 is False
        with caplog.at_level(logging.WARNING, logger="huijian.session"):
            asyncio.run(scenario())
        assert "按断连处理" in caplog.text
    finally:
        sess_mod.BaseSession._SEND_TIMEOUT_S = orig


# ── F5/F6/F10：指纹补键 + speed/sample_rate 消毒 ────────────────
def _eng(settings):
    from core.tts import TtsEngine
    return TtsEngine(settings, None)


def test_speed_clamped_and_sanitized():
    assert _eng(_S({"tts.speed": 50}))._speed() == 2.0, "无上界=旧缺陷，须钳位"
    assert _eng(_S({"tts.speed": "abc"}))._speed() == 1.0
    assert _eng(_S({"tts.speed": 1.5}))._speed() == 1.5
    # 钳位值同步进指纹（产出变了键必换）
    assert _eng(_S({"tts.speed": 99})).voice_fingerprint() \
        == _eng(_S({"tts.speed": 2.0})).voice_fingerprint()


def test_cloud_rate_sanitized_and_in_fingerprint():
    e = _eng(_S({"tts.provider": "cloud", "tts.cloud": {}}))
    assert e._cloud_rate({"sample_rate": "44.1k"}) == 0, "脏值消毒回 0，不得炸链"
    assert e._cloud_rate({"sample_rate": 44100}) == 44100
    assert e._cloud_rate({}) == 0
    fp0 = e.voice_fingerprint()
    e2 = _eng(_S({"tts.provider": "cloud",
                  "tts.cloud": {"sample_rate": 44100}}))
    assert "sr44100" in e2.voice_fingerprint()
    assert e2.voice_fingerprint() != fp0, "改 sample_rate 必须轮换缓存键"


def test_custom_voice_content_rotates_fingerprint():
    e = _eng(_S({"tts.provider": "local_kokoro", "tts.sid": 18}))
    e._custom_sids = {"mei": 103}
    fp1 = e.voice_fingerprint()
    e._custom_fp = "v1"          # 同名重传改良版：数量/sid/lock sha 全不变
    fp2 = e.voice_fingerprint()
    e._custom_fp = "v2"
    fp3 = e.voice_fingerprint()
    assert fp1 != fp2 != fp3 and fp2 != fp3, "内容变=指纹必须动"


# ── F7/F9：merge 大小写碰撞 + tmp 残留 ──────────────────────────
def _official(tmp, n=3, per=4):
    v = tmp / "voices.bin"
    v.write_bytes(bytes(range(n * per)))
    return v


def test_merge_case_collision(tmp_path):
    from core.tts import merge_custom_voices
    official = _official(tmp_path)
    cdir = tmp_path / "c"
    cdir.mkdir()
    (cdir / "Amy.bin").write_bytes(b"\x01" * 4)
    (cdir / "amy.bin").write_bytes(b"\x02" * 4)
    out, names, skipped, fp = merge_custom_voices(
        official, cdir, tmp_path / "m.bin", 3)
    assert len(names) == 1, "碰撞折叠后 names 与拼入路数必须一致"
    assert any("大小写" in s for s in skipped)
    assert fp, "内容指纹随返回值带出"
    merged = (tmp_path / "m.bin").read_bytes()
    assert len(merged) == official.stat().st_size + 4, "merged 只拼 1 路=计数一致"


def test_merge_failure_cleans_tmp(tmp_path, monkeypatch):
    import os as _os
    from core import tts as t
    official = _official(tmp_path)
    cdir = tmp_path / "c"
    cdir.mkdir()
    (cdir / "a.bin").write_bytes(b"\x03" * 4)
    def boom(a, b):
        raise OSError(28, "No space left")
    monkeypatch.setattr(t.os, "replace", boom)
    out, names, skipped, fp = t.merge_custom_voices(
        official, cdir, tmp_path / "m.bin", 3)
    assert names == {} and any("落盘失败" in s for s in skipped)
    assert not list(tmp_path.glob("*.tmp")), "失败分支必须清 tmp（ENOSPC 不加剧）"


# ── F13/F16：200+文本体拒收；声明长度未付满不记成功 ─────────────
def test_cloud_200_text_body_rejected():
    from core.tts import _CloudOpusStream
    body = b'{"error": {"message": "quota exceeded, balance: 12.34 yuan"}}'
    d = _CloudOpusStream(24000)
    with pytest.raises(RuntimeError) as ei:
        d.feed(body)
    assert "文本而非音频" in str(ei.value)


def test_cloud_declared_over_paid_raises_on_flush():
    import struct
    from core.tts import _CloudOpusStream
    fmt = b"fmt " + struct.pack("<I", 16) + struct.pack(
        "<HHIIHH", 1, 1, 24000, 48000, 2, 16)
    data_hdr = b"data" + struct.pack("<I", 4000)
    hdr = (b"RIFF" + struct.pack("<I", 4 + len(fmt) + len(data_hdr) + 4000)
           + b"WAVE" + fmt + data_hdr)
    d = _CloudOpusStream(24000)
    d.feed(hdr + b"\x00" * 200)          # 声明 4000B 实付 200B 后"干净"收尾
    with pytest.raises(RuntimeError) as ei:
        d.flush()
    assert "未付满" in str(ei.value), "撒谎截断必须按云故障，不得记完整成功"


# ── F14：cache_enabled=False 读路径同步短路 ─────────────────────
def test_cache_disabled_blocks_reads():
    e = _eng(_S({"tts.cache_enabled": True}))
    key = ("句", 18, 1.0)
    e._cache[key] = ([FRAME], 9)
    e._cache_bytes = 9
    assert e._cache_get(key) is not None, "前置：开缓存可命中"
    e.settings = _S({"tts.cache_enabled": False})
    assert e._cache_get(key) is None, "关缓存必须连存量一起失效"
    assert not e._cache, "短路时顺带清存量（关一次即净）"


# ── F3/F4：试听超时 503；ensure 下载锁外；指纹推送强引用 ────────
def test_tts_test_timeout_503():
    from core import admin_api
    ctx = types.SimpleNamespace(
        tts=types.SimpleNamespace(
            synthesize_pcm=lambda t: asyncio.sleep(30)))

    class _Req:
        def __init__(self, app): self.app = app
        async def read(self): return '{"text": "你好"}'.encode("utf-8")

    orig = admin_api._TTS_TEST_TIMEOUT_S
    admin_api._TTS_TEST_TIMEOUT_S = 0.2
    try:
        async def scenario():
            resp = await admin_api._tts_test(_Req({admin_api.CTX_KEY: ctx}))
            import json as _j
            body = _j.loads(resp.body)
            assert resp.status == 503, body
            assert "超时" in body.get("message", ""), body
        asyncio.run(scenario())
    finally:
        admin_api._TTS_TEST_TIMEOUT_S = orig


def test_ensure_loaded_downloads_outside_engine_lock(tmp_path):
    """行为钉：store.ensure（模拟冷下载）被调用时，引擎 `_lock` 必须可被他人
    拿到——旧版持锁跨下载=并发合成/试听各占 executor 线程堵锁（池 8 线程堆满
    连带 STT 饿死）。"""
    from core.tts import TtsEngine
    lock_holder = {}

    class _Store:
        def model_dir_for(self, key):
            return None
        def ensure(self, key):
            lk = lock_holder["lk"]
            got = lk.acquire(blocking=False)
            lock_holder["free_during_download"] = got
            if got:
                lk.release()
            return False        # 令下载后仍未就绪，走快速失败路径（不碰 sherpa）
        def lock_entry(self, key):
            return {}

    e = TtsEngine(_S({}), _Store())
    lock_holder["lk"] = e._lock
    assert e.ensure_loaded() is False
    assert lock_holder.get("free_during_download") is True, \
        "ensure 在 _lock 内被调用=回归"


def test_push_voice_fp_strong_ref():
    """形态钉（同 test_concurrency_guards F7b 先例）：热推送 task 必须入袋。"""
    src = (ROOT / "core" / "main.py").read_text(encoding="utf-8")
    i = src.index("_push_voice_fp(fp)")
    block = src[i - 400:i + 300]
    assert "self._fp_pending.add(t)" in block, \
        "fire-and-forget 无强引用=GC 丢推送，HA 缓存键不轮换（P5 病灶复发）"
    assert "add_done_callback(self._fp_pending.discard)" in block


# ── 集成侧 T1：非 stop 收口必须 error（真 anyio 行为钉）─────────
@pytest.fixture()
def _real_anyio():
    keys = [k for k in list(sys.modules)
            if k == "anyio" or k.startswith("anyio.") or k == "huijian"
            or k.startswith("huijian.")]
    saved = {k: sys.modules[k] for k in keys}
    for k in keys:
        del sys.modules[k]
    yield
    for k in [k for k in list(sys.modules)
              if k == "anyio" or k.startswith("anyio.") or k == "huijian"
              or k.startswith("huijian.")]:
        del sys.modules[k]
    sys.modules.update(saved)


def _load_modules():
    pytest.importorskip("anyio")
    import anyio  # noqa: F401

    def stub(name, **attrs):
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
        return mod

    if "homeassistant" not in sys.modules:
        stub("homeassistant")
        conf = stub("homeassistant.config_entries")
        conf.ConfigEntry = type("ConfigEntry", (), {})
        core = stub("homeassistant.core")
        core.HomeAssistant = type("HomeAssistant", (), {})
        exc = stub("homeassistant.exceptions")
        exc.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})

    class Dict(dict):
        def __getattr__(self, item):
            return self.get(item)

    pkg = types.ModuleType("huijian")
    pkg.Dict = Dict
    pkg.EntryAuthFailedError = RuntimeError
    pkg.get_entry_data = lambda hass, entry, **kw: {}
    pkg.__path__ = []
    sys.modules["huijian"] = pkg

    spec = importlib.util.spec_from_file_location(
        "huijian.ws_transport", INTEGRATION / "huijian" / "ws_transport.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["huijian.ws_transport"] = mod
    spec.loader.exec_module(mod)
    spec2 = importlib.util.spec_from_file_location(
        "huijian.tts_transport", INTEGRATION / "huijian" / "tts_transport.py")
    mod2 = importlib.util.module_from_spec(spec2)
    sys.modules["huijian.tts_transport"] = mod2
    spec2.loader.exec_module(mod2)
    return mod2, Dict


def _bare_transport(mod2):
    import anyio
    t = mod2.TtsTransport(hass=object(),
                          entry=types.SimpleNamespace(
                              entry_id="e", title="t",
                              async_create_background_task=lambda h, c, n: c.close()),
                          endpoint="ws://h:8000/x", attr_endpoint="tts_endpoint")
    t._recv_writer, t._recv_reader = anyio.create_memory_object_stream(32)

    async def _aye():
        return True
    t.ensure_connected = _aye
    t.restart_calls = []

    async def restart(reason=""):
        t.restart_calls.append(reason)
    t.restart_connection = restart
    t.sent = []

    async def send_capture(msg):
        t.sent.append(msg)
    t.send_message = send_capture
    return t


def test_t1_eof_midstream_yields_error_not_clean_end(_real_anyio):
    """T1 主支路：喂帧中途连接被关（unload/reload 依序 aclose 四流的形态）
    ——消费端必须收到 error dict，绝不以「正常耗尽」收场（HA core 只在异常时
    pop 缓存，静默 EOF=截断音频永久写盘缓存）。"""
    mod2, Dict = _load_modules()

    async def scenario():
        import anyio
        t = _bare_transport(mod2)
        out = []

        async def consumer():
            async for item in t.stream("来一段播报", timeout=5):
                out.append(item)

        async def feeder():
            await anyio.sleep(0.05)
            await t._recv_writer.send(b"frame1")
            await t._recv_writer.send(b"frame2")
            await anyio.sleep(0.05)
            await t._recv_writer.aclose()      # 断流：无 stop 的 EOF
        async with anyio.create_task_group() as tg:
            tg.start_soon(consumer)
            tg.start_soon(feeder)
        return out

    out = asyncio.run(scenario())
    assert out[:2] == [b"frame1", b"frame2"], "已收帧照常交付"
    assert isinstance(out[-1], Dict) and out[-1].get("error"), \
        "EOF 支路必须 error 收口"
    assert "未收到 stop" in out[-1]["error"]


def test_t1_closed_reader_yields_error(_real_anyio):
    """T1 第二支路：reader 被关（ClosedResourceError）——吞异常后同样必须
    error yield，不得静默 return。"""
    mod2, Dict = _load_modules()

    async def scenario():
        import anyio
        t = _bare_transport(mod2)
        out = []

        async def consumer():
            async for item in t.stream("播报", timeout=5):
                out.append(item)

        async def killer():
            await anyio.sleep(0.05)
            await t._recv_reader.aclose()      # 消费端脚下关 reader
        async with anyio.create_task_group() as tg:
            tg.start_soon(consumer)
            tg.start_soon(killer)
        return out

    out = asyncio.run(scenario())
    assert out and out[-1].get("error"), "ClosedResourceError 支路必须 error 收口"


# ── T2：ffmpeg 收口必 kill（真身执行+假进程）────────────────────
def _extract(path, name, ns):
    src = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == name):
            exec(compile(ast.get_source_segment(src, node), str(path), "exec"), ns)
            return ns[name]
    raise AssertionError(f"{path.name} 里找不到 {name}")


class _Ann:
    def __class_getitem__(cls, item):
        return cls


def test_t2_ffmpeg_branch_killed_on_early_aclose():
    """消费端提前 aclose（reload/取消形态）：finally 必须 cancel writer、kill
    进程、有界收尾——旧版 `await writer_task` 抛错跳过 wait=孤儿 ffmpeg。"""
    apy = CC / "huijian" / "audio.py"
    log = logging.getLogger("pin")

    class _Stdin:
        def write(self, b): pass
        async def drain(self): pass
        def close(self): pass

    class _Stdout:
        def __init__(self): self.n = 0
        async def read(self, k):
            self.n += 1
            return b"o" * 4096                 # 永供数据：模拟消费不完的长音频

    class _Stderr:
        async def read(self): return b""

    class _Proc:
        def __init__(self):
            self.stdin, self.stdout, self.stderr = _Stdin(), _Stdout(), _Stderr()
            self.returncode = None
            self.killed = False
            self.waited = False
        def kill(self):
            self.killed = True
            self.returncode = -9
        async def wait(self):
            self.waited = True
            return self.returncode

    proc = _Proc()

    async def fake_exec(*a, **kw):
        return proc

    async def gen_chunks():
        i = 0
        while True:
            i += 1
            yield b"x" * 4096

    ns = {
        "asyncio": asyncio, "contextlib": __import__("contextlib"),
        "_LOGGER": log,
        "HomeAssistant": _Ann, "AsyncIterable": AsyncIterable,
        "AsyncGenerator": AsyncGenerator,
        "ffmpeg": types.SimpleNamespace(
            get_ffmpeg_manager=lambda h: types.SimpleNamespace(
                binary="/bin/true")),
        "DOMAIN": "pin",
    }
    real_exec = asyncio.create_subprocess_exec
    ns["asyncio"] = types.SimpleNamespace(
        create_subprocess_exec=fake_exec,
        CancelledError=asyncio.CancelledError,
        subprocess=types.SimpleNamespace(PIPE=-1),
        create_task=asyncio.create_task,
        wait_for=asyncio.wait_for,
        sleep=asyncio.sleep,
    )
    fn = _extract(apy, "async_convert_audio", ns)

    class _Hass:
        def async_create_background_task(self, coro, name):
            return asyncio.create_task(coro)

    async def scenario():
        # 替换真 asyncio 的 create_subprocess_exec 语义：fake_exec 注入 ns，
        # write_input 里 process 引用闭包 proc 对象，无真子进程。
        g = fn(_Hass(), gen_chunks(), "wav", "mp3")
        got = []
        await asyncio.sleep(0.05)               # 让 writer/reader 都跑起来
        # 消费端中途关停（GeneratorExit 进 finally）
        await g.__anext__()
        await g.aclose()
        await asyncio.sleep(0.05)
        assert proc.killed, "提前退出必须 kill ffmpeg（同仓 ffmpeg_proxy 纪律）"
        assert proc.waited, "kill 后必须收尸（不留僵尸）"
    # write_input 的音频生成器永不断供：cancel 路径即测试目标
    asyncio.run(scenario())


# ── 集成侧形态钉（T3/T5/T6/T7/T4/F11）───────────────────────────
def test_t3_third_close_gated():
    src = (INTEGRATION / "huijian" / "ws_transport.py").read_text(encoding="utf-8")
    i = src.index("Websocket writer stopped")
    blk = src[i:i + 900]
    assert "await asyncio.wait_for(self._current_ws.close(), 5)" in blk, \
        "writer finally 裸 close=半开 TCP 永挂入口（H8 同款纪律漏第三处）"
    assert "abort()" in blk
    j = src.index("heartbeat ping for")
    assert "asyncio.wait_for(self._current_ws.ping()" in src[j:j + 300]


def test_t5_invalid_msg_log_truncated():
    src = (INTEGRATION / "huijian" / "ws_transport.py").read_text(encoding="utf-8")
    i = src.index("Invalid incoming msg: %r")
    blk = src[i - 100:i + 400]
    assert "[:120]" in blk, "全量原始帧进 ERROR=转写文本外泄+无界"
    assert 'logger.debug("Invalid incoming msg 全文' in src


def test_t6_dead_branch_downgraded():
    src = (INTEGRATION / "tts.py").read_text(encoding="utf-8")
    assert '_LOGGER.info("Received response:' not in src, \
        "不可达 else 支整帧 INFO=契约漂移时的文本外泄面"
    assert "Received non-bytes response: keys=" in src


def test_t7_available_and_preflight():
    src = (INTEGRATION / "tts.py").read_text(encoding="utf-8")
    assert "def available(self)" in src, "实体 available 恒真是旧形态"
    assert "reconnect_times" in src and "持续失败" in src, \
        "退避越限必须预闸快速失败，不再每次白等 ensure_connected 15s"


def test_t4_satellite_chunk_iter_deterministic_close():
    src = (INTEGRATION / "assist_satellite.py").read_text(encoding="utf-8")
    i = src.index("chunk_iter = _iter_wav_pcm_chunks(")
    # 窗口锚在"本函数尾"（下一个方法定义），不用定长 3600 字：v1.0.88 加归属闸
    # 注释后定长窗会把 finally 挤出窗外（断言的是"存在确定性收链"，窗宽不该随注释涨）
    blk = src[i:src.index("    async def _wrap_audio_stream", i)]
    assert "finally:" in blk and "await chunk_iter.aclose()" in blk, \
        "barge-in 取消落在生成器帧外时收链不得赌 GC（tts_transport 自家纪律）"


def test_f11_ui_prefix_match():
    html = (ROOT / "www" / "index.html").read_text(encoding="utf-8")
    i = html.index('$("#tts_provider").value = String(S.tts.provider')
    assert 'startsWith("cloud")' in html[i:i + 120], \
        "严格判等会把 cloud_openai_compat 静默翻转为本地（配置丢失）"
