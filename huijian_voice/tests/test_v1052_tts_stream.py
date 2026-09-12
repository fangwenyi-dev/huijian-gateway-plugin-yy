"""v1.0.52 钉桩：TTS 下行真流式（端到端"首音 = 首块"，而不是"整段合成完再发"）。

现场（2026-09-21）：设备在 `TTS_STREAM_START` 之后静音 31 秒才出声——
`12:54:32 Downlink audio start: 1024 bytes` → `12:54:47 Set output enable to false`
（15s 无输出才关）→ `12:55:03` 才收到其余音频 → `12:55:04 TTS stream end:
downlink 39168 bytes`（整段仅 1.22s）。

根因两层（都在我们自己的代码里）：
  ① 卫星层 `assist_satellite.py:_stream_tts_audio` 旧实现
     `data = b"".join([chunk async for chunk in tts_result.async_stream_result()])`
     ——把整段 WAV 收完才按固定 0.9 倍速发声（逐字抄自上游旧版；上游 HA 2026.8
     已改成 stream_wav(...) 边走边解 + 按设备环形缓冲水位背压）。
  ② 实体层 `tts.py` 旧实现 `audio += chunk` 把 HA 的流攒成 bytes 再
     `return fmt, audio`——而 HA 的 `TtsAudioType = tuple[str|None, bytes|None]`
     只收 bytes，实体侧唯一流式出口是 `async_stream_tts_audio →
     TTSAudioResponse(extension, data_gen)`（父类以"子类是否重写"自动判定）。
     修好后 HA 的 TTSCache 边读边 put_nowait，一路流到设备。

本钉桩三件事：①增量 WAV 解析行为（含 A/B 对照：旧批式形态**不可能**满足
"源未喂完就已出块"）；②卫星层形态钉（不得再出现 b"".join / 必须有水位背压）；
③实体层形态钉（流式方法 + peek 空判 + 软依赖）。两份副本同钉。
"""
import ast
import asyncio
import struct
from collections.abc import AsyncGenerator, AsyncIterable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CC = ROOT / "custom_components" / "huijian_ai"
STORE_CC = ROOT.parent / "yyjicheng" / "custom_components" / "huijian_ai"

_COPIES = [pytest.param(CC, id="main")]
if STORE_CC.exists():
    _COPIES.append(pytest.param(STORE_CC, id="store"))

SAT = "assist_satellite.py"
TTS = "tts.py"


class _Ann:
    """注解占位（≤3.13 在 def 时求值；非内建裸名字需可下标）。"""

    def __class_getitem__(cls, item):
        return None


def _extract_func(path: Path, name: str, extra_ns: dict | None = None):
    """从真实源码按名摘出函数执行（仓内既有惯例：不 import homeassistant）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            ns: dict = {}
            args = node.args
            eager = [a.annotation for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)]
            if node.returns is not None:
                eager.append(node.returns)
            eager += list(args.defaults) + list(args.kw_defaults)
            for expr in eager:
                if expr is None:
                    continue
                for sub in ast.walk(expr):
                    if (isinstance(sub, ast.Name) and sub.id not in ns
                            and not hasattr(__import__("builtins"), sub.id)):
                        ns[sub.id] = _Ann
            if extra_ns:
                ns.update(extra_ns)
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), ns)
            return ns[name]
    raise AssertionError(f"{path} 中未找到函数 {name}")


# ── WAV 构造（16k/mono/s16）─────────────────────────────────────────────

def _wav(pcm: bytes, rate: int = 16000, width: int = 2, channels: int = 1,
         extra_chunk: bool = False, declared_size: int | None = None) -> bytes:
    hdr = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    if extra_chunk:
        # 真实文件常见的 LIST 块（长度 4，偶数对齐）——解析器必须能跳过
        hdr += b"LIST" + struct.pack("<I", 4) + b"INFO"
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate,
                                 rate * width * channels, width * channels, width * 8)
    hdr += b"data" + struct.pack("<I", len(pcm) if declared_size is None else declared_size)
    return hdr + pcm


async def _drain(gen, chunks, fed_counter, **kwargs):
    """消费生成器，返回 [(len(chunk), is_last, 当时已喂入的源块数)]。"""
    async def src():
        for c in chunks:
            fed_counter[0] += 1
            yield c
            await asyncio.sleep(0)     # 给消费者先跑的机会（增量性的判据）

    params = {"sample_rate": 16000, "sample_width": 2, "sample_channels": 1,
              "samples_per_chunk": 512}
    params.update(kwargs)
    out = []
    async for chunk, is_last in gen(src(), **params):
        out.append((len(chunk), is_last, fed_counter[0]))
    return out


def _gen_fn(path: Path):
    """摘出增量解析器（并把同模块依赖的 _parse_wav_header 一并注入命名空间）。"""
    return _extract_func(
        path / SAT, "_iter_wav_pcm_chunks",
        {
            "AsyncIterable": AsyncIterable,
            "AsyncGenerator": AsyncGenerator,
            "_parse_wav_header": _parse_fn(path),
            "_MAX_WAV_HEADER_BYTES": 64 * 1024,
        },
    )


def _parse_fn(path: Path):
    return _extract_func(path / SAT, "_parse_wav_header")


# ── ① 增量性 + A/B 反证 ─────────────────────────────────────────────────

@pytest.mark.parametrize("path", _COPIES)
def test_streams_before_source_exhausted(path):
    """真流式：源还没喂完，就已经出块（首音 = 首块，与总时长无关）。"""
    pcm = bytes(range(256)) * 8                    # 2048 字节 = 1024 样本 = 2 块(512)
    wav = _wav(pcm)
    chunks = [wav[:100], wav[100:600], wav[600:1200], wav[1200:]]
    fed = [0]
    out = asyncio.run(_drain(_gen_fn(path), chunks, fed))
    assert out, "没有产出任何块"
    first_len, _, fed_at_first = out[0]
    assert first_len == 1024, f"首块应为 512 样本(1024B)，得到 {first_len}"
    assert fed_at_first < len(chunks), (
        f"首块在源喂完({len(chunks)})后才产出（fed_at_first={fed_at_first}）"
        "——说明又退回整段缓冲"
    )
    assert sum(o[0] for o in out) == len(pcm), "音频总量必须与载荷一致"
    assert out[-1][1] is True, "末块必须带 is_last"


@pytest.mark.parametrize("path", _COPIES)
def test_old_batch_form_cannot_stream(path):
    """A/B 对照：旧的 `b"".join(...)` 批式形态**不可能**在源喂完前出块。

    若此测试反而失败，说明本批对根因的归罪不成立，须重新排查。
    """
    pcm = bytes(range(256)) * 8
    wav = _wav(pcm)
    chunks = [wav[:100], wav[100:600], wav[600:1200], wav[1200:]]
    fed = [0]
    parse_wav_header = _parse_fn(path)
    buf = bytearray()

    async def _batch():
        async def src():
            for c in chunks:
                fed[0] += 1
                yield c
                await asyncio.sleep(0)
        data = b"".join([chunk async for chunk in src()])       # 旧形态
        buf.extend(data)
        return parse_wav_header(buf, (16000, 2, 1))

    off = asyncio.run(_batch())
    assert off is not None and fed[0] == len(chunks), (
        "批式路径必然先吃满全部源块——这正是首音延迟被 1:1 放大的原因"
    )


@pytest.mark.parametrize("path", _COPIES)
def test_header_robustness(path):
    """头解析：奇数长度分块、额外 LIST 块、data 声明长度、形态不符报错。"""
    gen = _gen_fn(path)
    parse = _parse_fn(path)
    pcm = b"\x01\x02" * 700

    # 逐字节喂（最恶劣的分块边界）
    wav = _wav(pcm, extra_chunk=True)
    fed = [0]
    out = asyncio.run(_drain(gen, [wav[i:i + 1] for i in range(len(wav))], fed))
    assert sum(o[0] for o in out) == len(pcm), "逐字节喂入也必须得到完整载荷"

    # data 声明长度小于实际（容器填充字节必须被忽略）
    wav2 = _wav(pcm, declared_size=len(pcm) - 100)
    fed2 = [0]
    out2 = asyncio.run(_drain(gen, [wav2[i:i + 7] for i in range(0, len(wav2), 7)], fed2))
    assert sum(o[0] for o in out2) == len(pcm) - 100, "必须按 data 声明长度收尾"

    # 形态不符 / 非 WAV → fail-loud（≥12 字节才可判定容器；不足则=需要更多字节）
    with pytest.raises(ValueError):
        parse(_wav(pcm, rate=24000), (16000, 2, 1))
    assert parse(b"ID3\x04\x00\x00\x00\x00\x00\x00", (16000, 2, 1)) is None, \
        "不足 12 字节不能判定容器，必须返回 None 等待更多字节（增量语义）"
    with pytest.raises(ValueError):
        parse(b"ID3\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00", (16000, 2, 1))


@pytest.mark.parametrize("path", _COPIES)
def test_empty_stream_is_loud(path):
    """空流（头都没凑齐就结束）必须报错，而不是静默收流。"""
    gen = _gen_fn(path)
    with pytest.raises(ValueError):
        asyncio.run(_drain(gen, [], [0]))


# ── ② 卫星层形态钉（不得退回整段缓冲）──────────────────────────────────

def _method_src(path: Path, cls: str, name: str) -> str:
    tree = ast.parse((path / SAT).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == name:
                    return ast.unparse(sub)
    raise AssertionError(f"未找到 {cls}.{name}")


def _method_node(path: Path, cls: str, name: str) -> ast.AST:
    tree = ast.parse((path / SAT).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == name:
                    return sub
    raise AssertionError(f"未找到 {cls}.{name}")


def _has_bytes_join(node: ast.AST) -> bool:
    """AST 级判定 `b"".join(...)`（不受 docstring/注释里的字样干扰）。"""
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "join"
            and isinstance(sub.func.value, ast.Constant)
            and isinstance(sub.func.value.value, (bytes, bytearray))
        ):
            return True
    return False


@pytest.mark.parametrize("path", _COPIES)
def test_satellite_streams_incrementally(path):
    body = _method_src(path, "EsphomeAssistSatellite", "_stream_tts_audio")
    assert not _has_bytes_join(_method_node(path, "EsphomeAssistSatellite", "_stream_tts_audio")), (
        "卫星层又出现整段 join（b\"\".join）——首音会退化成整段合成时间"
    )
    assert "_iter_wav_pcm_chunks" in body, "未使用增量 WAV 解析器"
    assert "_DEVICE_BUFFER_TARGET_S" in body, "缺少按设备环形缓冲水位的背压"
    assert "Only WAV audio can be streamed" in body, "非 WAV 早退 fail-loud 丢失"
    assert "音频 0 帧" in body, "0 帧告警 fail-loud 丢失"
    assert "VOICE_ASSISTANT_TTS_STREAM_END" in body, "收尾事件丢失（finally 必须补发）"


@pytest.mark.parametrize("path", _COPIES)
def test_watermark_matches_device_buffer(path):
    """水位常数必须有设备侧依据（随代码注释可查），且量级合理。"""
    src = (path / SAT).read_text(encoding="utf-8")
    assert "_DEVICE_BUFFER_TARGET_S = 0.384" in src, "水位值被改动，需重新核算"
    assert "MAX_PLAYBACK_TASKS_IN_QUEUE=40" in src, "设备缓冲依据注释丢失"


# ── ③ 实体层形态钉（HA 原生流式出口）───────────────────────────────────

@pytest.mark.parametrize("path", _COPIES)
def test_entity_exposes_native_streaming_api(path):
    src = (path / TTS).read_text(encoding="utf-8")
    assert "async def async_stream_tts_audio(" in src, (
        "实体未重写 async_stream_tts_audio——HA 判定它不支持流式，首音仍会等整段"
    )
    assert "TTSAudioResponse(fmt, _stream())" in src, "未返回 (extension, data_gen) 流式应答"
    assert "_HA_STREAMING_TTS" in src, "缺少老 HA 软依赖判定"
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_stream_tts_audio":
            guards = [
                n for n in ast.walk(node)
                if isinstance(n, ast.If) and ast.unparse(n.test).strip() == "not first"
            ]
            assert guards, "缺少 peek 首块的空判（空音频会进缓存 → 同一句永远静音）"
            assert any(isinstance(x, ast.Raise) for x in ast.walk(guards[0])), \
                "空判分支必须抛错（HA 捕获后 pop 缓存，等价 v1.0.25 纪律）"
            break
    else:
        pytest.fail("async_stream_tts_audio 未以 AsyncFunctionDef 形态存在")


@pytest.mark.parametrize("path", _COPIES)
def test_first_chunk_telemetry_present(path):
    """P2 逐跳首块遥测：实体侧与卫星侧各一行，便于现场定位"首音慢在哪一跳"。"""
    assert "[TTS] 首块就绪 %dms" in (path / TTS).read_text(encoding="utf-8")
    assert "[TTS] 首块 %dms 后发出" in (path / SAT).read_text(encoding="utf-8")
