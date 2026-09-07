"""云通道平台预设与音频解包测试（v1.0.8 平台预设接线）。

覆盖：① _unwrap_audio RIFF 拆封/奇数块对齐/mp3-ogg 拒收/位深校验；
② _cloud_stream 请求体透传 response_format/sample_rate 与 wav 实际采样率优先；
③ Web UI 预设目录形状钉桩；④ settings 云档默认键。
"""
import asyncio
import struct
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.tts import TtsEngine as TTS  # noqa: E402

WWW = Path(__file__).resolve().parents[1] / "www" / "index.html"
CORE = Path(__file__).resolve().parents[1] / "core"


def arun(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class SettingsStub:
    BASE = {"tts.speed": 1.0, "tts.cloud": {}, "stt.language": "zh-CN"}

    def __init__(self, **over):
        self.d = dict(self.BASE, **over)

    def get(self, key, default=None):
        return self.d.get(key, default)


def make_wav(rate=16000, bits=16, ch=1, data=b"AB" * 80, extra=None):
    fmt = struct.pack("<HHIIHH", 1, ch, rate, rate * ch * bits // 8, ch * bits // 8, bits)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    if extra is not None:
        cid, body = extra
        chunks += cid + struct.pack("<I", len(body)) + body
        if len(body) % 2:
            chunks += b"\x00"   # 真实写手按偶数对齐补 pad 字节
    chunks += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


class FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self):
        return self._payload

    async def text(self):
        return "boom"


class FakeSession:
    def __init__(self, script=None):
        self.calls = []
        self.script = list(script or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, headers=None, **kw):
        self.calls.append({"url": url, "json": json, "headers": headers})
        item = self.script.pop(0) if self.script else (200, b"RAWPCM")
        if isinstance(item, Exception):
            raise item
        return FakeResp(*item)


async def collect(agen):
    out = []
    async for x in agen:
        out.append(x)
    return out


def tts_with(monkeypatch, cloud, captured, script=None):
    t = TTS(SettingsStub(**{"tts.cloud": cloud}), None)
    monkeypatch.setattr(t, "_resample_encode",
                        staticmethod(lambda raw, rate: captured.append((raw, rate)) or []))
    sess = FakeSession(script)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kw: sess)
    return t, sess


# ── ① 音频解包 ──────────────────────────────────────────────

def test_unwrap_wav_header():
    data = b"CD" * 50
    pcm, rate = TTS._unwrap_audio(make_wav(rate=16000, data=data), 24000)
    assert pcm == data and rate == 16000

def test_unwrap_wav_odd_extra_chunk_aligned():
    data = b"EF" * 40
    wav = make_wav(rate=24000, data=data, extra=(b"LIST", b"x" * 25))
    pcm, rate = TTS._unwrap_audio(wav, 8000)
    assert pcm == data and rate == 24000

def test_unwrap_raw_passthrough():
    pcm, rate = TTS._unwrap_audio(b"01" * 32, 22050)
    assert pcm == b"01" * 32 and rate == 22050

def test_unwrap_id3_rejected():
    try:
        TTS._unwrap_audio(b"ID3" + bytes([4, 0]) + b"j" * 40, 24000)
        assert False
    except RuntimeError as e:
        assert "mp3" in str(e)

def test_unwrap_mp3_frame_sync_rejected():
    try:
        TTS._unwrap_audio(bytes([255, 251, 144, 0]) + b"0" * 60, 24000)
        assert False
    except RuntimeError as e:
        assert "mp3" in str(e)

def test_unwrap_ogg_rejected():
    try:
        TTS._unwrap_audio(b"OggS" + bytes([0, 2]) + b"j" * 40, 24000)
        assert False
    except RuntimeError as e:
        assert "opus" in str(e)

def test_unwrap_non16bit_rejected():
    try:
        TTS._unwrap_audio(make_wav(bits=32, data=b"0" * 64), 24000)
        assert False
    except RuntimeError as e:
        assert "16-bit" in str(e)

def test_unwrap_stubby_riff_passthrough():
    pcm, rate = TTS._unwrap_audio(b"RIFF", 24000)
    assert pcm == b"RIFF" and rate == 24000


# ── ② 请求体透传 ────────────────────────────────────────────

def test_body_defaults_pcm_no_rate(monkeypatch):
    cap = []
    t, sess = tts_with(monkeypatch, {"base_url": "https://x.example/v1"}, cap)
    arun(collect(t._cloud_stream("你好")))
    assert sess.calls[0]["url"] == "https://x.example/v1/audio/speech"
    body = sess.calls[0]["json"]
    assert body["response_format"] == "pcm" and "sample_rate" not in body
    assert cap == [(b"RAWPCM", 24000)]

def test_body_passthrough_format_and_rate(monkeypatch):
    cap = []
    cloud = {"base_url": "https://x.example/v1", "model": "FunAudioLLM/CosyVoice2-0.5B",
             "voice": "anna", "response_format": "pcm", "sample_rate": 24000}
    t, sess = tts_with(monkeypatch, cloud, cap)
    arun(collect(t._cloud_stream("你好")))
    body = sess.calls[0]["json"]
    assert body["model"] == "FunAudioLLM/CosyVoice2-0.5B" and body["voice"] == "anna"
    assert body["sample_rate"] == 24000

def test_server_wav_wins_over_declared_rate(monkeypatch):
    wav = make_wav(rate=44100, data=b"GH" * 40)
    cap = []
    t, sess = tts_with(monkeypatch, {"base_url": "https://x.example/v1"}, cap, script=[(200, wav)])
    arun(collect(t._cloud_stream("你好")))
    assert cap == [(b"GH" * 40, 44100)]

def test_http_error_raises(monkeypatch):
    t, sess = tts_with(monkeypatch, {"base_url": "https://x.example/v1"}, [],
                       script=[(401, b"no key")])
    try:
        arun(collect(t._cloud_stream("你好")))
        assert False
    except RuntimeError as e:
        assert "401" in str(e)


# ── ③④ UI 与默认键钉桩 ─────────────────────────────────────

def test_preset_ids_and_fields():
    h = WWW.read_text(encoding="utf-8")
    for pid in ('stt_preset', 'tts_preset', 'llm_preset', 'stt_preset_hint',
                'tts_preset_hint', 'llm_preset_hint', 'tts_cloud_model'):
        assert pid in h, pid
    assert "const PRESETS = {" in h and "function wirePresets()" in h
    assert "\nwirePresets();" in h or "\r\nwirePresets();" in h, "初始化调用缺失"

def test_preset_base_urls_only_openai_compat():
    h = WWW.read_text(encoding="utf-8")
    for base in (
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "https://api.siliconflow.cn/v1",
        "https://api.groq.com/openai/v1",
        "https://api.302ai.cn/v1",
        "https://api.openai.com/v1",
        "https://api.deepseek.com/v1",
        "https://ark.cn-beijing.volces.com/api/v3",
        "https://open.bigmodel.cn/api/paas/v4",
        "https://api.moonshot.cn/v1",
        "https://spark-api-open.xf-yun.com/v1",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "http://192.168.1.100:11434/v1",
    ):
        assert base in h, base
    for banned in ("openspeech.bytedance.com", "tts.cloud.tencent.com", "V2/TTS"):
        assert banned not in h, banned

def test_tts_save_roundtrip_fields():
    h = WWW.read_text(encoding="utf-8")
    assert 'model:$("#tts_cloud_model").value' in h
    assert '...(ttsFmt?{response_format:ttsFmt}:{})' in h
    assert '...(ttsRate?{sample_rate:ttsRate}:{})' in h
    assert 'ttsFmt = S.tts.cloud?.response_format||""' in h

def test_settings_cloud_keys_present():
    src = (CORE / "settings.py").read_text(encoding="utf-8")
    assert '"response_format": "", "sample_rate": 0' in src

def test_tts_module_shape():
    src = (CORE / "tts.py").read_text(encoding="utf-8")
    assert "def _unwrap_audio" in src
    assert 'cloud.get("response_format")' in src
    assert 'body["sample_rate"] = int(sr_req)' in src

