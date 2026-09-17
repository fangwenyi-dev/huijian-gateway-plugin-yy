"""v1.0.76 钉桩：TTS 推流收口归因遥测（跨度/速率/最大块隙，一行定凶手）。

现场两案（2026-09-15 09:38 / 10:37，均同一句 62 字三场景清单）：服务器秒级
发完 191 帧（1.2 语速完整长度，本地真引擎对账钉死），设备却以 0.42~0.44×
摊拍收货（26~27s 到达 11.46s 音频）→ 队列饿拍 → "播报像断开"。既有收口行
「[TTS] 推流 N 帧 X.XXs」不含跨度，无法区分"HA 推流循环被饿"与
"aioesphomeapi writer/TCP/设备侧被拖慢"。本批收口行补 `跨度/速率/最大块隙`
三字段：
  速率≈1× 而设备跨度大 → writer/网络/设备侧；速率<1× → 推流循环饥饿，
  max_gap 直接暴露最长一次等待（上游数据没来 or 事件循环没被调度）。

行为钉走仓内 AST 摘真身惯例（test_v1052 同款 harness）：快上游与饿上游
各测一遍真实 _stream_tts_audio，断言日志三字段能把两种形态分开。
单钉 main：yyjicheng/ 商店镜像按发布仪式整批同步（当前镜像代次 v1.0.60，
落后属既定节奏）——双钉由镜像同步批收编，勿夹带单文件混代。
"""
import asyncio
import contextlib
import logging
import re
from enum import IntEnum
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CC = ROOT / "custom_components" / "huijian_ai"
SAT = "assist_satellite.py"
_PATHS = [pytest.param(CC, id="main")]


class _AnnMeta(type):
    def __getattr__(cls, item):   # 注解里的 tts.ResultStream 之类属性链兜底
        return cls


class _Ann(metaclass=_AnnMeta):
    def __class_getitem__(cls, item):
        return cls


def _extract_func(path, name, extra_ns=None):
    """AST 摘真身（≤3.13 注解求值兼容，仓内惯例）。"""
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            ns: dict = {}
            args = node.args
            eager = [a.annotation for a in
                     list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)]
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


def _wav(pcm: bytes, rate: int = 16000, width: int = 2, channels: int = 1) -> bytes:
    import struct
    hdr = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    hdr += b"fmt " + struct.pack("<I", 16)
    hdr += struct.pack("<HHIIHH", 1, channels, rate,
                       rate * channels * width, channels * width, width * 8)
    hdr += b"data" + struct.pack("<I", len(pcm))
    return hdr + pcm


class _VevType(IntEnum):
    VOICE_ASSISTANT_TTS_STREAM_START = 1
    VOICE_ASSISTANT_TTS_STREAM_END = 2


class _Cli:
    def __init__(self):
        self.events = []
        self.audio_chunks = 0

    def send_voice_assistant_event(self, ev, data):
        self.events.append(ev)

    def send_voice_assistant_audio(self, chunk):
        self.audio_chunks += 1


class _ED:
    def __init__(self):
        self.pipeline_flags = []

    def async_set_assist_pipeline_state(self, v):
        self.pipeline_flags.append(v)


class _Sat:
    def __init__(self):
        self._is_running = True
        self._udp_server = None
        self.cli = _Cli()
        self._entry_data = _ED()
        self.response_finished = 0

    def tts_response_finished(self):
        self.response_finished += 1


class _Result:
    extension = "wav"

    def __init__(self, gen):
        self._gen = gen

    def async_stream_result(self):
        return self._gen


def _sat_stream(path):
    """摘真身：_iter_wav_pcm_chunks（连带 _parse_wav_header）+ _stream_tts_audio。"""
    import struct
    f = path / SAT
    src = f.read_text(encoding="utf-8")
    m = re.search(r"_MAX_WAV_HEADER_BYTES\s*=\s*(\d+)", src)
    assert m, "_MAX_WAV_HEADER_BYTES 常量失踪"
    ph = _extract_func(f, "_parse_wav_header", {"struct": struct})
    it = _extract_func(f, "_iter_wav_pcm_chunks",
                       {"_parse_wav_header": ph,
                        "_MAX_WAV_HEADER_BYTES": int(m.group(1))})
    ns = {
        "asyncio": asyncio, "contextlib": contextlib,
        "_LOGGER": logging.getLogger("hj.test_v1076"),
        "VoiceAssistantEventType": _VevType,
        "_iter_wav_pcm_chunks": it,
        "_DEVICE_BUFFER_TARGET_S": 0.384,
    }
    return _extract_func(f, "_stream_tts_audio", ns)


def _parse_rate_gap(msg: str):
    m = re.search(r"速率 ([\d.]+)× 最大块隙 (\d+)ms", msg)
    assert m, f"收口行缺速率/最大块隙字段：{msg!r}"
    return float(m.group(1)), int(m.group(2))


@pytest.mark.parametrize("path", _PATHS)
def test_push_stream_telemetry_fields(path):
    """源级：收口 INFO 必须带三字段，且绝对时刻背压公式原样（改动即红）。"""
    src = (path / SAT).read_text(encoding="utf-8")
    assert "跨度 %.2fs 速率 %.2f× 最大块隙 %dms" in src
    # v1.0.89（F3）改档说明：本钉守的是"**绝对时刻背压公式不许退化成拍数计数/
    # 去掉 sleep**"这一口径，不是水位字面值。水位自 v1.0.89 起按设备固件能力分流
    # （0.384 / 1.536s，见 tests/test_v1089_downlink_prebuf.py），故变量名随之下沉。
    # 反向删除（把 sleep 去掉、或改回写死常量）本钉当场红——已实证。
    assert "(audio_duration_sent - buffer_target_s) - elapsed" in src, \
        "背压公式被动过——遥测口径失效"


@pytest.mark.parametrize("path", _PATHS)
def test_fast_stream_reports_high_rate(path, caplog):
    """快上游：背靠背出块 → 速率>1（无 sleep）；字段可读。"""
    pcm = bytes(8 * 1024)          # 8 块 × 32ms = 0.256s
    wav = _wav(pcm)

    async def fast():
        yield wav[:44]
        for i in range(0, len(pcm), 1024):
            yield pcm[i:i + 1024]

    fn = _sat_stream(path)
    sat = _Sat()
    with caplog.at_level(logging.INFO, logger="hj.test_v1076"):
        asyncio.run(fn(sat, _Result(fast())))
    line = next(r.getMessage() for r in caplog.records if "推流" in r.getMessage())
    rate, _ = _parse_rate_gap(line)
    assert rate > 0.9, f"快流应≈1×以上：{line}"
    assert sat.response_finished == 1
    assert _VevType.VOICE_ASSISTANT_TTS_STREAM_END in sat.cli.events


@pytest.mark.parametrize("path", _PATHS)
def test_starved_stream_lowers_rate_and_exposes_gap(path, caplog):
    """饿上游（块间 150ms）：速率明显<1×，最大块隙抓住 ~150ms 长等——
    这正是现场"服务器发完、设备摊拍收货"在 HA 侧的指纹形态。"""
    pcm = bytes(6 * 1024)
    wav = _wav(pcm)

    async def slow():
        yield wav[:44]
        for i in range(0, len(pcm), 1024):
            await asyncio.sleep(0.15)
            yield pcm[i:i + 1024]

    fn = _sat_stream(path)
    sat = _Sat()
    with caplog.at_level(logging.INFO, logger="hj.test_v1076"):
        asyncio.run(fn(sat, _Result(slow())))
    line = next(r.getMessage() for r in caplog.records if "推流" in r.getMessage())
    rate, max_gap = _parse_rate_gap(line)
    assert rate < 0.8, f"150ms 间隙上游应拖低速率：{line}"
    assert max_gap >= 130, f"最长间隙应被点名：{max_gap}ms"
