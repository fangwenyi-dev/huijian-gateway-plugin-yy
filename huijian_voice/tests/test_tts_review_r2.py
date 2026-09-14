"""2026-09-27 TTS 深审第二轮（R2 #1–#11）行为钉。

纪律沿用 v1065：行为钉优先（真函数执行），接线位用源码形态钉兜底并注明。
每条对应深审报告一项：speed 下界/合成排队有界/声道闸/率域闸/多付侦账/
短 RIFF 同闸/加载单飞/指纹 :fb 与推送/预览折叠/None 文本/Content-Type。
"""
import asyncio
import json
import logging
import struct
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.tts import (TtsEngine, _CloudOpusStream, _SPEED_MIN, _SPEED_MAX,
                      _RATE_MIN, _RATE_MAX, _DROP_TOLERANCE)


class _S:
    def __init__(self, d=None):
        self.d = dict(d or {})

    def get(self, k, dflt=None):
        return self.d.get(k, dflt)


class _Store:
    def model_dir_for(self, key):
        return None

    def ensure(self, key):
        return False

    def lock_entry(self, key):
        return {}

    def voices_count_for(self, key):
        return 0


def _eng(d=None):
    return TtsEngine(_S(d), _Store())


def _mk_wav(rate=24000, nch=1, bits=16, declared=None, audio=b""):
    """构造 RIFF/WAVE（fmt+data）。declared=None 用实长。"""
    fmt = (b"fmt " + struct.pack("<I", 16)
           + struct.pack("<HHIIHH", 1, nch, rate,
                         rate * nch * bits // 8, nch * bits // 8, bits))
    csz = len(audio) if declared is None else declared
    data = b"data" + struct.pack("<I", csz) + audio
    riff = b"RIFF" + struct.pack("<I", 4 + len(fmt) + len(data)) + b"WAVE"
    return riff + fmt + data


# ── R2 #1：speed 下界钳位（F10 上界的镜像）──────────────────────
def test_speed_lower_clamp():
    assert _eng({"tts.speed": 0.05})._speed() == _SPEED_MIN
    assert _eng({"tts.speed": 0.49})._speed() == _SPEED_MIN
    assert _eng({"tts.speed": 0.6})._speed() == 0.6   # 滑条合法值不受扰
    assert _eng({"tts.speed": 50})._speed() == _SPEED_MAX
    assert _eng({"tts.speed": -1})._speed() == 1.0
    assert _eng({"tts.speed": "abc"})._speed() == 1.0


# ── R2 #1b：_gen_lock 排队有界（坏前手不再拖穿 executor 池）────
def test_synth_gen_lock_wait_bounded(monkeypatch):
    e = _eng()
    e._tts = types.SimpleNamespace(
        generate=lambda *a, **k: pytest.fail("排队超时后不得再触 generate"))
    monkeypatch.setattr("core.tts._GEN_WAIT_S", 0.05)
    assert e._gen_lock.acquire(blocking=False)
    try:
        t0 = time.monotonic()
        out = e._synth("你好", 18, 1.0)      # 前手占锁 → 有界等待 → 按失败收
        took = time.monotonic() - t0
    finally:
        e._gen_lock.release()
    assert out == b""
    assert took < 2.0, f"超预算等待 {took:.2f}s"
    assert e._busy == 0, "有界失败路径也必须归还 busy 计数"


# ── R2 #3：声道闸（流式+整包两路，bits 已校验的对称补齐）────────
def test_stereo_wav_rejected_stream():
    d = _CloudOpusStream(24000)
    with pytest.raises(RuntimeError) as ei:
        d.feed(_mk_wav(nch=2, audio=b"\x00" * 64))
    assert "声道" in str(ei.value)


def test_stereo_wav_rejected_batch():
    body = _mk_wav(nch=2, audio=b"\x00" * 64)
    with pytest.raises(RuntimeError) as ei:
        TtsEngine._unwrap_audio(body, 24000)
    assert "声道" in str(ei.value)


# ── R2 #4：采样率合理域闸（fmt 任意 uint32 / 配置写坏都不放行）──
def test_crazy_rate_wav_rejected():
    d = _CloudOpusStream(24000)
    with pytest.raises(RuntimeError) as ei:
        frames = d.feed(_mk_wav(rate=1, audio=b"\x00" * 64))
    assert "合理域" in str(ei.value) or "采样率" in str(ei.value)
    with pytest.raises(RuntimeError):
        TtsEngine._unwrap_audio(_mk_wav(rate=1, audio=b"\x00" * 64), 24000)


def test_cloud_rate_out_of_domain_sanitized():
    e = _eng()
    assert e._cloud_rate({"sample_rate": 1}) == 0        # 域下
    assert e._cloud_rate({"sample_rate": 999999}) == 0   # 域上
    assert e._cloud_rate({"sample_rate": 44100}) == 44100
    assert e._cloud_rate({"sample_rate": "44.1k"}) == 0  # 旧 F10 消毒不回归


# ── R2 #5：data 块多付侦账（F16 的反方向）──────────────────────
def test_overpaid_data_raises_on_flush():
    audio = b"\x01" * (70000)
    body = _mk_wav(declared=100, audio=audio)  # 声明 100 实付 70000=撒谎声明
    d = _CloudOpusStream(24000)
    d.feed(body)
    with pytest.raises(RuntimeError) as ei:
        d.flush()
    assert "多付" in str(ei.value)


def test_overpaid_within_tolerance_ok():
    audio = b"\x01" * 200
    body = _mk_wav(declared=150, audio=audio)  # 尾巴 50B=合法元数据量级
    d = _CloudOpusStream(24000)
    d.feed(body)
    d.flush()                                       # 不得误杀


# ── R2 A1（finder 初报，逐字节复核成立）：整包路短 RIFF 同闸 ────
def test_batch_short_riff_rejected():
    stub = b"RIFF" + struct.pack("<I", 99999) + b"WAVE" + b"\x00" * 10
    assert 12 <= len(stub) < 44
    with pytest.raises(RuntimeError) as ei:
        TtsEngine._unwrap_audio(stub, 24000)
    assert "过短" in str(ei.value)


# ── R2 #6：ensure_loaded 引擎级单飞（F3 池尽形态的新入口）───────
def test_ensure_loaded_single_flight():
    e = _eng()
    called = []
    e._ensure_loaded_inner = lambda: called.append(1) or False
    e._loading = True                                # 模拟他轮在下载中
    assert e.ensure_loaded() is False
    assert not called, "单飞闸失守：后来者仍进了加载体（陪等占线程）"


def test_ensure_loaded_resets_flag_and_notifies():
    e = _eng()
    notes = []
    e.on_fp_change = lambda: notes.append(1)
    e._ensure_loaded_inner = lambda: True
    assert e.ensure_loaded() is True
    assert e._loading is False, "收尾必须归还单飞门（异常路径也要还=finally）"
    assert notes == [1], "首载完成=指纹输入变更，必须触发重算推送（R2 #7）"
    e._tts = object()
    assert e.ensure_loaded() is True and notes == [1], "热态不再触发"


def test_ensure_loaded_flag_reset_on_crash():
    e = _eng()

    def boom():
        raise RuntimeError("加载炸了")
    e._ensure_loaded_inner = boom
    with pytest.raises(RuntimeError):
        e.ensure_loaded()
    assert e._loading is False, "崩溃路径必须归还单飞门，否则引擎永久未就绪"


# ── R2 #2/#7：钉扎窗口指纹 :fb 后缀 + 解钉/置钉通知 ─────────────
_CLOUD = {"tts.provider": "cloud_openai",
          "tts.cloud": {"base_url": "http://x/v1", "voice": "v"}}


def test_fingerprint_pinned_suffix():
    e = _eng(_CLOUD)
    assert not e.voice_fingerprint().endswith(":fb")
    e._cloud_fail_ts = time.monotonic()
    assert e.voice_fingerprint().endswith(":fb"), \
        "钉扎窗口内实际产出是本地兜底嗓，必须与云键隔离（无 TTL 盘缓存毒化）"
    e._cloud_fail_ts = time.monotonic() - 301.0     # 超 _CLOUD_PIN_S=300 窗口
    assert not e.voice_fingerprint().endswith(":fb"), "钉扎窗口过期即回收"


def test_reset_cloud_pin_notifies():
    e = _eng(_CLOUD)
    notes = []
    e.on_fp_change = lambda: notes.append(1)
    e._cloud_fail_ts = time.monotonic()
    e.reset_cloud_pin("测试")
    assert e._cloud_fail_ts == 0.0
    assert notes == [1]
    e.reset_cloud_pin("未钉扎")
    assert notes == [1], "未钉扎时零副作用不空推"


# ── R2 #11：Content-Type 闸（200+text/plain 不再当裸 PCM）──────
def test_content_type_text_rejected(monkeypatch):
    import aiohttp

    class _Content:
        async def read(self, n):
            return b"Error: rate limit exceeded"

    class _Resp:
        status = 200
        content_type = "text/plain"
        content = _Content()                        # 无 iter_chunked

    class _ReqCtx:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *a):
            return False

    class _Sess:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            return _ReqCtx()

    monkeypatch.setattr(aiohttp, "ClientSession", _Sess)
    e = _eng({**_CLOUD})

    async def go():
        return [x async for x in e._cloud_stream("你好")]
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(go())
    assert "Content-Type" in str(ei.value)


# ── R2 #8：main.py 降级分支 NameError（形态钉：作用域里没有 `log`）─
def test_main_firmware_fallback_uses_logger():
    src = (ROOT / "core" / "main.py").read_text(encoding="utf-8")
    i = src.index("FirmwareStore()")
    block = src[i:i + 400]
    assert "log.error(" not in block, \
        "__init__ 作用域没有 log（只是 run() 的 banner 局部名）→ /data 只读时崩启动"
    assert "logger.error(" in block


# ── R2 #9：投递目录预览复刻 merge 的 F7 大小写折叠跳数 ──────────
def test_preview_fold_collision_sid(tmp_path, monkeypatch):
    from core import const
    official_n, per = 103, 64
    mdir = tmp_path / "model"
    mdir.mkdir()
    (mdir / "voices.bin").write_bytes(b"\x00" * official_n * per)
    vdir = tmp_path / "voices"
    vdir.mkdir()
    for name in ("Amy.bin", "amy.bin", "bob.bin"):
        (vdir / name).write_bytes(b"\x01" * per)
    monkeypatch.setattr(const, "TTS_VOICES_DIR", vdir)
    e = TtsEngine(_S({}), types.SimpleNamespace(
        voices_count_for=lambda k: official_n,
        model_dir_for=lambda k: mdir))
    st = e.voices_status()
    by = {p["name"]: p for p in st["preview"]}
    assert by["Amy"]["valid"] and by["Amy"]["sid"] == official_n
    assert not by["amy"]["valid"], "折叠碰撞后者不得展示 sid（与 merge 跳后者对齐）"
    assert by["bob"]["sid"] == official_n + 1, \
        "碰撞对不得让后续合法音色的预览 sid 错位 +1"


# ── R2 #10：{"text": null} 不播字面 "None" ──────────────────────
def test_detect_null_text_not_spoken(caplog):
    from core.session import TtsSession

    class _WS:
        closed = True

        async def send_str(self, x):
            pass

        async def send_bytes(self, x):
            pass

    seen = {}

    class _Ctx:
        class tts:
            @staticmethod
            def stream_opus(text, engine_out=None):
                seen["text"] = text

                async def _e():
                    yield b""
                return _e()
        settings = _S()

    async def scenario():
        s = TtsSession(_WS(), _Ctx)
        await s.on_text(json.dumps({"type": "tts", "state": "detect",
                                    "text": None}))
        if s._task:
            await asyncio.wait_for(s._task, 5)
    asyncio.run(scenario())
    assert seen.get("text") in ("", None), \
        f"null 文本必须折叠为空，实际传给引擎的是 {seen.get('text')!r}"
