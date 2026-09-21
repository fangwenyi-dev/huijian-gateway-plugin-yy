"""v1.0.85 审计二轮钉桩（云流双闸补漏 / 编码跳帧毒缓存 / 试听轮闸 / 同代保缓存）。

P2：data 声明"谎小"（provider 把样本数当字节数写=恰好 50%）时，多付量=实付的
50%——短句（实付 ≤128KB）落在 _DROP_TOLERANCE=64KB 绝对容忍内、又短于 O4 比值
闸的 60 字豁免线 → 双闸齐失守，缺尾云音频记完整成功+解钉+进 HA 无 TTL 盘缓存
（同句永久半速缺尾……不，永久缺一半）。多付闸相对化：`dropped > max(16KB,
20%×实付)`——样本数谎言=50% 必 Catch；真尾元数据（LIST/INFO 量级 <10KB）恒过。

P3：OpusPcmEncoder.encode_stream 单帧失败 debug 跳帧——句中 60ms 空洞随
"整句完整"进句级 LRU 与 HA 盘缓存=永久可闻爆点，违"缺尾必自报"全链纪律。
改 raise：本句失败沿 stream_opus 既有异常口收（truncated+stop，集成 error 收口
不毒缓存）。

P5：synthesize_pcm（管理台试听）不在 _round_busy 闸内——试听半程被省电档
unload，_synth 快照 None 空产，半截 wav 以 200 无告警发回。纳入同闸。

P6a：ensure_loaded 每次成功加载都 `_cache.clear()`，与 unload 侧"同代重载
输出逐比特一致、缓存刻意保留（省电档秒回旧帧）"的承诺直接矛盾——省电档首个
含 miss 的轮一过，整张 LRU 白丢。改为按模型代次指纹（主模型+voices 的
size/mtime）判定，仅换代清。
"""
import asyncio
import struct
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import const  # noqa: E402
from core.tts import _CloudOpusStream, _StreamResampler, TtsEngine  # noqa: E402
from core.audio import OpusPcmEncoder  # noqa: E402


def _mk_demux():
    """不触真编码器（opus 在 CI 缺位）的流解包壳：直通记账即可验闸。"""
    d = _CloudOpusStream(24000)

    def fake_start(self, rate):
        self._res = _StreamResampler(rate, const.SAMPLE_RATE)
        self._enc = None
        self._state = "payload"
    d._start_payload = fake_start.__get__(d)
    d._frame_pcm = lambda pcm: []
    return d


def _hdr(csz_declared):
    fmt = b"fmt " + struct.pack("<I", 16) + struct.pack(
        "<HHIIHH", 1, 1, 24000, 48000, 2, 16)
    data_hdr = b"data" + struct.pack("<I", csz_declared)
    return (b"RIFF" + struct.pack("<I", 4 + len(fmt) + len(data_hdr) + csz_declared)
            + b"WAVE" + fmt + data_hdr)


# ── P2：谎小（样本数当字节=50%）必须被相对闸点名 ─────────────────
def test_lie_half_small_payload_caught_by_relative_gate():
    d = _mk_demux()
    d.feed(_hdr(40000) + b"\x00" * 40000)      # 声明 40000B
    d.feed(b"\x00" * 40000)                      # 又 40000B 被钳为多付
    with pytest.raises(RuntimeError) as ei:
        d.flush()
    assert "多付" in str(ei.value), \
        f"P2 回潮：50% 谎小被绝对 64KB 容忍放行（缺尾记完整成功毒盘缓存）"


def test_legit_trailing_metadata_still_passes():
    d = _mk_demux()
    d.feed(_hdr(40000) + b"\x00" * 40000)
    info = b"INFO" + struct.pack("<I", 12000) + b" " * 12000
    d.feed(info)                                 # 尾元数据 12KB < 16KB 底线
    d.flush()                                    # 不得 raise


def test_moderate_tail_not_false_flagged():
    d = _mk_demux()
    d.feed(_hdr(200000) + b"\x00" * 200000)
    d.feed(b"\x00" * 30000)                      # 30000/200000=15% < 20% 相对项
    d.flush()


# ── P3：编码跳帧=句中爆点进缓存，必须 raise ──────────────────────
def test_encode_frame_failure_raises_not_skips():
    e = OpusPcmEncoder.__new__(OpusPcmEncoder)

    class FE:
        n = 0

        def encode(self, chunk, frame_size=None):
            FE.n += 1
            if FE.n == 2:
                raise RuntimeError("encoder boom")
            return b"\xfc"

    e._e = FE()
    with pytest.raises(RuntimeError):
        list(e.encode_stream(b"\x00" * const.FRAME_BYTES * 3))
    assert FE.n == 2


# ── P5：试听在飞必须纳入 _round_busy（省电档让路同轮语义）────────
def test_preview_holds_round_busy_gate():
    settings = {"tts.provider": "local_kokoro", "tts.sid": 18,
                "tts.speed": 1.0, "tts.cache_enabled": False}
    eng = TtsEngine(settings, model_store=None)
    eng._tts = types.SimpleNamespace(num_speakers=103)
    seen = []

    def spy(sent, sid, speed):
        seen.append(eng._round_busy)
        return b"\x00\x00" * 960
    eng._synth = spy
    asyncio.run(eng.synthesize_pcm("句子一。句子二。"))
    assert seen and all(v >= 1 for v in seen), (
        f"P5 回潮：试听不被整轮在飞闸罩护，句间可被省电档 unload（半截 wav 无告警）：{seen}")
    assert eng._round_busy == 0, "试听收口后闸未释放"


# ── P6a：同代重载保缓存、换代照清（形态+单元两级）───────────────
def test_model_gen_key_tracks_file_identity(tmp_path):
    m = tmp_path / "model.onnx"
    m.write_bytes(b"a" * 10)
    v = tmp_path / "voices.bin"
    v.write_bytes(b"b" * 5)
    k1 = TtsEngine._model_gen_key(str(m), str(v))
    k2 = TtsEngine._model_gen_key(str(m), str(v))
    assert k1 == k2, "同代判定必须稳定（size/mtime）"
    m.write_bytes(b"c" * 999)
    k3 = TtsEngine._model_gen_key(str(m), str(v))
    assert k3 != k1, "主模型内容长度变化必须换代"
    assert TtsEngine._model_gen_key(str(tmp_path / "missing"), None) != \
        TtsEngine._model_gen_key(str(m), str(v))


def test_ensure_loaded_clears_cache_only_on_generation_change():
    src = (ROOT / "core" / "tts.py").read_text(encoding="utf-8")
    i = src.index("def ensure_loaded")
    body = src[i:src.index("def unload", i)]
    # v1.1.5：合法清缓存口=两类换代——引擎换绑（跨引擎）与模型换代（gen-key）。
    # P6a 纪律不变：**任何** _cache.clear() 前后必须紧邻这两类换代判据之一。
    idxs = [n for n, ln in enumerate(body.splitlines()) if "_cache.clear()" in ln]
    assert idxs, "清缓存口整体消失？"
    lines = body.splitlines()
    for n in idxs:
        ctx = "\n".join(lines[max(0, n - 8):n + 4])
        assert ("引擎换绑" in ctx or "_loaded_gen_key" in ctx or "换代" in ctx), (
            "P6a 回潮：_cache.clear() 脱离换代判据裸执行（同代重载白丢整张 LRU，"
            "违背 unload 侧『省电档秒回旧帧』承诺）")
