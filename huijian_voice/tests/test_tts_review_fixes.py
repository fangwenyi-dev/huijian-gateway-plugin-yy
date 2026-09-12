"""2026-09-21 TTS 审查修复批——实体/整包/HTTP 面钉桩（真栈实证）。

审查报告六项里的本文件职责：
④ 实体侧空文本 fail-loud（空串不再消耗一整轮 WS 往返+通道锁）；
⑤ 逐帧 `Received bytes` INFO 降 DEBUG，且 INFO 级下连 hex 都不分配；
⑥ 云响应短于嗅探窗（<12B）不得判成裸 PCM——旧形态 4B "RIFF" 残响应实测
   产 1 帧垃圾还被记"云成功"解除钉扎（流式 _decide 与整包 _unwrap_audio 同闸）;
⑦ http 视图 options 参数复活（旧版直传 str，core options.pop 即 AttributeError
   =该参数一传必 400 的死参）+ 多条目默认实体 first-wins（旧=末条覆盖）。

指纹入 speed / generate 互斥 / 空 detect 收束 stop 三项分别钉在
test_v1048_fp_and_guards.py、test_concurrency_guards.py、test_protocol_ws.py。
纪律：全部真身执行（AST 摘取不复制逻辑），不用字符串哨兵冒充证据。
"""
import ast
import asyncio
import contextlib
import json
import logging
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CC = ROOT / "custom_components" / "huijian_ai"


class _HAError(Exception):
    """HomeAssistantError 占位（不 import homeassistant，仓内既有惯例）。"""


def _extract(path: Path, name: str, ns: dict):
    """从真实源码按名摘出函数执行（不复制逻辑），返回真身。"""
    src = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name):
            g = dict(ns)
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), g)
            return g[name]
    raise AssertionError(f"{path.name} 里找不到 {name}")


# ── ④⑤ 实体侧 _async_pcm_stream ───────────────────────────────

class _HexBytes(bytes):
    """解码帧替身：统计 hex() 分配——INFO 级下必须一次都不发生。"""
    calls = 0

    def hex(self, *a):
        type(self).calls += 1
        return super().hex(*a)


def _entity_stream_fn(transport_holder):
    class _Dec:
        def decode(self, packet, frame_samples):
            return _HexBytes(b"\x01\x02" * 960)

    class _T:
        async def ensure_connected(self):
            return True

        async def stream(self, message):
            transport_holder.setdefault("streamed", []).append(message)
            for i in range(3):
                yield b"OPUS" + str(i).encode()

    ns = {
        "HomeAssistantError": _HAError,
        "tts_transport": types.SimpleNamespace(
            get_entry_transport=lambda h, e: _T()),
        "opuslib": types.SimpleNamespace(Decoder=lambda r, c: _Dec()),
        "logging": logging,
        "contextlib": contextlib,
        "_LOGGER": logging.getLogger("huijian_tts_pin"),
    }
    fn = _extract(CC / "tts.py", "_async_pcm_stream", ns)
    self_stub = types.SimpleNamespace(hass=None, entry=None, opus_sample_rate=16000,
                                      opus_channels=1, opus_frame_samples=960)
    return fn, self_stub


def test_entity_empty_message_fails_loud_before_ws():
    """空/纯空白/None message → 当场 HomeAssistantError，且 transport 一次都
    不碰（旧形态：detect 发出去、服务端静默、客户端 60s 超时锁通道）。"""
    holder = {}
    fn, self_stub = _entity_stream_fn(holder)
    for probe in ("", "   ", None):
        async def drive(msg=probe):
            agen = fn(self_stub, msg)
            return await agen.__anext__()
        with pytest.raises(_HAError) as ei:
            asyncio.run(drive())
        assert "空播报文本" in str(ei.value)
    assert holder.get("streamed", []) == [], "空文本不得发起 WS 轮"


def test_entity_frames_flow_and_no_info_per_frame(caplog):
    """正常流：3 帧全部产出；INFO 级下零条逐帧日志、零次 hex 分配；
    DEBUG 级下恢复恰 3 条（排障能力不丢）。"""
    holder = {}
    fn, self_stub = _entity_stream_fn(holder)
    _HexBytes.calls = 0

    async def drain():
        return [c async for c in fn(self_stub, "你好世界")]

    with caplog.at_level(logging.INFO, logger="huijian_tts_pin"):
        chunks = asyncio.run(drain())
    assert len(chunks) == 3
    assert _HexBytes.calls == 0, "INFO 级热路径不得做逐帧 hex 分配"
    assert not [r for r in caplog.records
                if "Received bytes" in r.getMessage() and r.levelno == logging.INFO], \
        "逐帧 INFO 回潮=五分钟播报 5000 行，违 v1.0.48 自家日志纪律"

    with caplog.at_level(logging.DEBUG, logger="huijian_tts_pin"):
        asyncio.run(drain())
    hits = [r for r in caplog.records if "Received bytes" in r.getMessage()]
    assert len(hits) == 3 and all(r.levelno == logging.DEBUG for r in hits), \
        "DEBUG 级必须仍可逐帧对账（只降频不改义）"


# ── ⑥ 云短响应闸（流式 + 整包同判据）──────────────────────────

def test_cloud_short_response_not_misread_as_pcm_stream():
    from core.tts import _CloudOpusStream
    s = _CloudOpusStream(24000)
    assert s.feed(b"RIFF") == []          # 只进嗅探窗
    with pytest.raises(RuntimeError) as ei:
        s.flush()
    assert "过短" in str(ei.value), "4B 'RIFF' 残响应旧形态产 1 帧垃圾还记云成功"

    # 12B 完整容器头不误伤（RIFF/WAVE 判定正常走）
    hdr = b"RIFF" + (36 + 1920).to_bytes(4, "little") + b"WAVE"
    s2 = _CloudOpusStream(24000)
    s2.feed(hdr)
    assert s2._state == "riff_hdr"

    # ≥12B 裸 PCM 维持旧缺省（闸只拦"无法判形"的长度）
    s3 = _CloudOpusStream(16000)
    s3.feed(b"\x00" * 1920)              # 不抛即通过
    s3.flush()


def test_cloud_short_response_whole_packet_gate():
    from core.tts import TtsEngine
    with pytest.raises(RuntimeError) as ei:
        TtsEngine._unwrap_audio(b"RI", 24000)
    assert "过短" in str(ei.value)
    # 0B 维持旧语义（零帧收束政策兜底），不当"过短"炸
    raw, rate = TtsEngine._unwrap_audio(b"", 24000)
    assert raw == b"" and rate == 24000


# ── ⑦ http 视图：options 复活 + first-wins 默认实体 ────────────

def test_parse_tts_stt_options():
    ns = {"json": json}
    f = _extract(CC / "huijian" / "http.py", "parse_tts_stt_options", ns)
    assert f(None) == {} and f("") == {}
    assert f('{"preferred_format": "wav"}') == {"preferred_format": "wav"}
    for bad in ("not json", '"just a string"', "[1,2]", "123"):
        with pytest.raises(ValueError):
            f(bad)


def test_pick_default_entities_first_wins():
    ns = {"CONF_TTS_ENTITY_ID": "tts_entity_id", "CONF_STT_ENTITY_ID": "stt_entity_id"}
    f = _extract(CC / "huijian" / "http.py", "pick_default_entities", ns)
    e = lambda **kw: types.SimpleNamespace(options=kw)
    # 旧循环=末条覆盖→tts.b；first-wins→tts.a（各键独立取首个显式配置）
    got = f([e(tts_entity_id="tts.a"), e(tts_entity_id="tts.b", stt_entity_id="stt.b")])
    assert got == ("tts.a", "stt.b")
    assert f([e(), e()]) == ("tts.huijian_speech", "stt.huijian_asr")
    assert f([]) == ("tts.huijian_speech", "stt.huijian_asr")
