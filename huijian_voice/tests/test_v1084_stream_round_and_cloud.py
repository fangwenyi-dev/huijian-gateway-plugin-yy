"""v1.0.84 修复批钉桩（B1 省电档轮中卸载 / O3 stop 漏发留痕 / O4 云短产出防毒）。

B1（v1.0.83 新触达）：整轮墙钟 52s→总闸 660s 放开后，长播报轮可跨越 reaper
（core/main.py._loop_reaper 按 last_used 卸载，而 last_used 只在轮首/轮尾/缓存
命中刷新、_busy 只罩单句 generate 瞬间）——轮中被卸，后续每句 _synb 空产=
整轮缺尾。修法：stream_opus 整轮在飞计数 _round_busy（事件循环内自增自减，
无锁；executor 侧 unload 只读），unload() 与 _busy 同闸让路。

O3：finally 的 stop 帧 send_json 返回值弃用——漏发=本 detect 无 stop，集成侧
60s 间隙窗超时虽自愈，但归因静默。补点名 WARN。

O4：云端"正常收束但只合成前半"（服务端限长、字节自洽）当前记完整成功+解钉+
入 HA 无 TTL 盘缓存=同句永久缺尾。补 音频时长/文本预期 比值闸（0.35×，60 字
以下短文本豁免——英文/符号文本预期虚高不误杀）。
"""
import asyncio
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from core import const  # noqa: E401
from core.tts import TtsEngine  # noqa: E401


def _mk_engine(pcm16=b"\x00\x00" * 960, packets=(b"pkt",)):
    """本地档引擎裸壳：不触真模型/真编码器。"""
    settings = {
        "tts.provider": "local_kokoro",
        "tts.sid": 18,
        "tts.speed": 1.0,
        "tts.cache_enabled": False,
    }
    eng = TtsEngine(settings, model_store=None)
    eng._tts = types.SimpleNamespace(num_speakers=103)  # ready()=True

    def _synth(sent, sid, speed):
        return pcm16

    def _encode(pcm):
        return list(packets)

    eng._synth = _synth
    eng._encode = _encode
    return eng


# ── B1：轮在飞期间 unload 必须让路 ──────────────────────────────────
def test_stream_round_blocks_power_save_unload():
    eng = _mk_engine()
    seen = []

    async def scenario():
        agen = eng.stream_opus("句子一二三。这是第二句。", engine_out=None).__aiter__()
        seen.append(await agen.__anext__())        # 第一句首帧 = 轮进行中
        # 句间空隙（旧形态的漏窗：_busy 已落、last_used 未新）reaper 视角：
        assert eng.unload() is False, "B1 回潮：轮在飞时 unload 未被挡住"
        assert eng.ready(), "轮中被卸载，余句将空产缺尾"
        async for pkt in agen:
            seen.append(pkt)
        # 轮收束后卸载恢复通行
        assert eng.unload() is True
    asyncio.run(scenario())
    assert seen, "无帧产出"


def test_stream_round_busy_released_on_exception():
    eng = _mk_engine()

    def boom(sent, sid, speed):
        raise RuntimeError("synth boom")

    eng._synth = boom

    async def scenario():
        agen = eng.stream_opus("坏句。", engine_out=None)
        with pytest.raises(RuntimeError):          # _synth 异常穿出=生成器关闭
            await agen.__anext__()
        assert eng.unload() is True, "异常收口后在飞计数未释放（轮闸变死闸）"
    asyncio.run(scenario())


def test_stream_round_busy_released_on_aclose():
    eng = _mk_engine()

    async def scenario():
        agen = eng.stream_opus("第一句。第二句。第三句。", engine_out=None)
        it = agen.__aiter__()
        await it.__anext__()
        await agen.aclose()                        # 消费者提前关停（取消路）
        assert eng.unload() is True, "aclose 路径在飞计数未释放"
    asyncio.run(scenario())


# ── O3：stop 漏发必须点名 ───────────────────────────────────────────
def test_stop_send_failure_logs_named_warning(caplog):
    from core.session import TtsSession

    s = TtsSession.__new__(TtsSession)
    s._gen = 0
    s._task = None

    async def send_json(frame):
        return frame.get("state") != "stop"       # stop 帧发送失败

    async def send_bytes(b):
        return True

    class Eng:
        async def stream_opus(self, text, engine_out=None):
            yield b"\xf8a"
            yield b"\xf8b"

    s.ctx = types.SimpleNamespace(tts=Eng())
    s.send_json = send_json
    s.send_bytes = send_bytes
    with caplog.at_level("WARNING", logger="huijian.session"):
        asyncio.run(s._stream("漏发收口", 0))
    assert any("stop 帧" in r.getMessage() and "失败" in r.getMessage()
               for r in caplog.records), \
        "O3 回潮：stop 漏发无痕=集成白等间隙窗且归因静默"


# ── O4：云"干净收尾只前半"必须按缺尾收口 ──────────────────────────
def _cloud_engine(frames):
    eng = _mk_engine()
    eng.settings["tts.provider"] = "cloud:test"

    async def _cloud_stream(text):
        for i in range(frames):
            yield b"\xf8" + bytes([i % 256])
    eng._cloud_stream = _cloud_stream
    return eng


def test_cloud_short_output_marks_truncated_and_pins():
    eng = _cloud_engine(2)          # 0.12s 音频 / 80 字（预期 ≈17.8s）→ 0.007×
    out = {}

    async def scenario():
        got = [p async for p in eng.stream_opus("句" * 80, engine_out=out)]
        assert len(got) == 2, "已产出帧须照常交付（前半句仍出声）"
    asyncio.run(scenario())
    assert out.get("truncated") is True, (
        f"O4 回潮：云限长前半产出不置旗={out.get('engine')} 收口，"
        f"缺尾音频将进 HA 无 TTL 盘缓存（同句永久缺尾）")
    assert eng._cloud_fail_ts > 0, "短产出=云异常，须开钉扎窗防下轮再吞"


def test_cloud_normal_output_not_false_flagged():
    eng = _cloud_engine(300)        # 18s / 80 字 → 0.99× 预期
    out = {}
    asyncio.run(_collect(eng, "句" * 80, out))
    assert "truncated" not in out, "正常产出被比值闸误杀"
    assert eng._cloud_fail_ts == 0.0


def test_cloud_short_text_exempt():
    eng = _cloud_engine(2)
    out = {}
    asyncio.run(_collect(eng, "太短了。", out))    # <60 字豁免（符号/英文虚高）
    assert "truncated" not in out


def test_cloud_speed_slow_ratio_uses_speed_floor():
    # speed=2.0：80 字预期音频 = 80/(4.5×2)≈8.9s → 150 帧(9s)=1.01× 不杀；
    # 帧数按 speed 折算后仍过 0.35 线。
    eng = _cloud_engine(150)
    eng.settings["tts.speed"] = 2.0
    out = {}
    asyncio.run(_collect(eng, "句" * 80, out))
    assert "truncated" not in out


async def _collect(eng, text, out):
    async for _ in eng.stream_opus(text, engine_out=out):
        pass
