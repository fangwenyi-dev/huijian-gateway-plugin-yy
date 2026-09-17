"""v1.0.89 F3 钉：下行预灌水位按固件能力分流（project_version → 0.384 / 1.536s）。

病灶承接（2026-09-16 双案）：
  • 16:33 案＝固件 vendored api_connection 每拍只读 1 包 → 11.46s 音频摊成 53.17s
    到达（0.216×），设备 1.28s 播放队列反复抽干＝"一句话分好几次说完"；固件 v2.1.51
    修为"每拍 ≤10 包 + 队列 64 块(2.048s)"，真机复测 7.02s 音频 6.40s 到达(≈1.1×)。
  • 但发送侧水位仍写死 0.384s ＝ 旧板 1.28s×75%。新板容量翻倍后，水位不跟着抬，
    上游一慢（本地档 RTF>1）就仍会饿——0.384 对 v2.1.51 是"unnecessary 薄"。
一刀切抬水位不可行的理由（本批方向纪律）：现场绝大多数存量板 ≤v2.1.50，队列仍
1.28s，超发到 1.5s 会撞它"满则丢最旧"＝「缺头/丢头」症状族，比句间停顿更难查。
故按设备自报的 project_version（v2.1.37 起随 PROJECT_VER 编译进 bin、经
DeviceInfoResponse 上报）分流，**读不到/畸形/低版本一律回 0.384s（fail-open）**。

钉面：①源级形态（默认先赋、suppress 后抬、公式改用变量、收口行带水位）；
     ②纯函数真值表（AST 摘真身跑，含 2.10.0 位权与带后缀版本）；
     ③端到端——真 _stream_tts_audio 跑完，收口 INFO 里的水位必须随固件版本变化。
单钉 main（yyjicheng 镜像落后代次按发布仪式整批同步，勿夹带单文件混代）。
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

OLD_S = 0.384
NEW_S = 1.536
GATE = (2, 1, 51)


class _AnnMeta(type):
    def __getattr__(cls, item):
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


# ── ① 源级形态钉 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", _PATHS)
def test_water_level_constants_and_shape(path):
    src = (path / SAT).read_text(encoding="utf-8")

    # 旧值字面不动：test_v1052_tts_stream 的 "_DEVICE_BUFFER_TARGET_S = 0.384" 钉继续绿
    assert "_DEVICE_BUFFER_TARGET_S = 0.384" in src, "旧水位常量被改=存量设备行为变化"
    assert "_DEVICE_BUFFER_TARGET_S_V2151 = 1.536" in src
    assert "_PREBUF_FW_MIN = (2, 1, 51)" in src

    # 1.536 不是拍的数：新板容量 64 块 × 32ms = 2.048s，取上游同款 75% 水位。
    assert abs(NEW_S - 0.75 * 64 * 0.032) < 1e-9, "新水位与设备容量口径不再自洽"
    # 旧 0.384 = 上游按"512ms 环缓冲 × 75%"继承下来的字面值（12 块 × 32ms）；
    # 落到本板 1.28s 只等于 30%——这正是"水位太薄、上游一慢就饿"的量化根据。
    assert abs(OLD_S - 0.75 * 16 * 0.032) < 1e-9, "旧水位锚点（上游 512ms 口径）被挪"
    assert OLD_S < 0.75 * 40 * 0.032, "旧值若已等于 1.28s 的 75%，本批分流失义"

    # 背压公式改用变量，且**不得**再直接引用旧常量（禁回潮）
    assert "(audio_duration_sent - buffer_target_s) - elapsed" in src, \
        "背压公式没用分流后的水位"
    assert "(audio_duration_sent - _DEVICE_BUFFER_TARGET_S) - elapsed" not in src, \
        "公式回潮成写死 0.384：新板吃不到翻倍容量"

    # fail-open 形态：默认值必须先于分流块出现（桩缺符号也要落在旧值上）
    body = src[src.index("async def _stream_tts_audio"):]
    d = body.index("buffer_target_s = _DEVICE_BUFFER_TARGET_S")
    u = body.index("buffer_target_s = _prebuf_target_for_version(")
    assert d < u, "必须先赋旧值再尝试抬高——否则读不到版本时水位未定义/取到新值"
    assert "contextlib.suppress(Exception)" in body[:u], "分流块没包 suppress=坏 device_info 会炸播报"

    # 收口行带水位（现场对账分流是否落地）
    assert "最大块隙 %dms 水位 %.3fs" in src, "水位不上日志=现场无法判定走了哪条分支"


# ── ② 纯函数真值表 ───────────────────────────────────────────────────────


def _gate(path):
    f = path / SAT
    src = f.read_text(encoding="utf-8")
    m = re.search(r"_RE_PROJECT_VER\s*=\s*re\.compile\(r\"(.+?)\"\)", src)
    assert m, "版本解析正则失踪"
    pv = _extract_func(f, "_parse_project_version", {"re": re, "_RE_PROJECT_VER": re.compile(m.group(1))})
    fn = _extract_func(f, "_prebuf_target_for_version", {
        "_parse_project_version": pv,
        "_DEVICE_BUFFER_TARGET_S": OLD_S,
        "_DEVICE_BUFFER_TARGET_S_V2151": NEW_S,
        "_PREBUF_FW_MIN": GATE,
    })
    return fn


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("raw,want", [
    ("2.1.51", NEW_S),                      # 门槛当版
    ("2.1.52", NEW_S),
    ("2.2.0", NEW_S),                       # 次版本进位
    ("2.10.0", NEW_S),                      # 位权：字符串比较会把 2.10.0 判成 <2.1.51
    ("2.1.51 (Mar  2 2026 12:00:00)", NEW_S),   # HA 侧 sw_version 形态带后缀
    ("v2.1.51", NEW_S),
    ("2.1.50", OLD_S),                      # 门槛前一版（现场存量主力）
    ("2.1.47", OLD_S),
    ("2.1", OLD_S),                         # 畸形：缺段
    ("", OLD_S),                            # 未上报（<v2.1.37 固件）
    (None, OLD_S),                          # 字段缺失
    ("unknown", OLD_S),                     # 非本项目
    ("1.9.9", OLD_S),                       # 别的 project_version 体系
])
def test_water_level_truth_table(path, raw, want):
    """读得到且够新才抬；一切未知/畸形/低版本＝今天的行为。"""
    assert _gate(path)(raw) == want, f"project_version={raw!r} 的水位判定不符"


# ── ③ 端到端：真推流循环报出的水位随固件版本变 ──────────────────────────


class _VevType(IntEnum):
    VOICE_ASSISTANT_TTS_STREAM_START = 1
    VOICE_ASSISTANT_TTS_STREAM_END = 2


class _Cli:
    def __init__(self):
        self.events = []

    def send_voice_assistant_event(self, t, data):
        self.events.append(t)

    def send_voice_assistant_audio(self, chunk):
        pass


class _DI:
    def __init__(self, pv):
        self.project_version = pv


class _ED:
    def __init__(self, pv):
        self.client = _Cli()
        self.device_info = _DI(pv)
        self.flags = []

    def async_set_assist_pipeline_state(self, v):
        self.flags.append(v)


class _Sat:
    def __init__(self, pv):
        self._is_running = True
        self._udp_server = None
        self._entry_data = _ED(pv)
        self.cli = self._entry_data.client
        self.finished = 0

    def tts_response_finished(self):
        self.finished += 1


class _Result:
    extension = "wav"

    def __init__(self, gen):
        self._gen = gen

    def async_stream_result(self):
        return self._gen


def _wav_header(n):
    import struct
    h = b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE"
    h += b"fmt " + struct.pack("<I", 16)
    h += struct.pack("<HHIIHH", 1, 1, 16000, 32000, 2, 16)
    return h + b"data" + struct.pack("<I", n)


def _run_stream(path, pv):
    import struct
    f = path / SAT
    src = f.read_text(encoding="utf-8")
    mh = re.search(r"_MAX_WAV_HEADER_BYTES\s*=\s*(\d+)", src)
    mre = re.search(r"_RE_PROJECT_VER\s*=\s*re\.compile\(r\"(.+?)\"\)", src)
    pv_fn = _extract_func(f, "_parse_project_version",
                          {"re": re, "_RE_PROJECT_VER": re.compile(mre.group(1))})
    gate = _extract_func(f, "_prebuf_target_for_version", {
        "_parse_project_version": pv_fn,
        "_DEVICE_BUFFER_TARGET_S": OLD_S,
        "_DEVICE_BUFFER_TARGET_S_V2151": NEW_S,
        "_PREBUF_FW_MIN": GATE,
    })
    it = _extract_func(f, "_iter_wav_pcm_chunks", {
        "_parse_wav_header": _extract_func(f, "_parse_wav_header", {"struct": struct}),
        "_MAX_WAV_HEADER_BYTES": int(mh.group(1)),
    })
    fn = _extract_func(f, "_stream_tts_audio", {
        "asyncio": asyncio, "contextlib": contextlib,
        "_LOGGER": logging.getLogger("hj.test_v1089"),
        "VoiceAssistantEventType": _VevType,
        "_iter_wav_pcm_chunks": it,
        "_DEVICE_BUFFER_TARGET_S": OLD_S,
        "_prebuf_target_for_version": gate,
    })

    pcm = bytes(6 * 1024)          # 6 块 ≈ 192ms，远小于任何水位 ⇒ 零 sleep

    async def gen():
        yield _wav_header(len(pcm))
        for i in range(0, len(pcm), 1024):
            yield pcm[i:i + 1024]

    sat = _Sat(pv)

    async def go():
        await fn(sat, _Result(gen()), None)

    asyncio.run(go())
    return sat


@pytest.mark.parametrize("path", _PATHS)
def test_stream_reports_level_2_1_51(path, caplog):
    caplog.set_level(logging.INFO, logger="hj.test_v1089")
    with caplog.at_level(logging.INFO):
        sat = _run_stream(path, "2.1.51")
    line = [r for r in caplog.records if "推流" in r.getMessage()]
    assert line, "推流收口行没打出来"
    assert "水位 1.536s" in line[-1].getMessage(), "新固件应走新水位"
    assert sat.finished == 1 and sat.cli.events[-1] == _VevType.VOICE_ASSISTANT_TTS_STREAM_END


@pytest.mark.parametrize("path", _PATHS)
def test_stream_reports_level_old_fw_and_missing_info(path, caplog):
    caplog.set_level(logging.INFO, logger="hj.test_v1089")
    for pv in ("2.1.50", ""):
        caplog.clear()
        _run_stream(path, pv)
        line = [r.getMessage() for r in caplog.records if "推流" in r.getMessage()]
        assert line and f"水位 {OLD_S:.3f}s" in line[-1], \
            f"project_version={pv!r} 必须落回旧水位（fail-open）"
    caplog.clear()
    _run_stream(path, None)           # 属性为 None：不得抛，落旧值
    assert any("水位 0.384s" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("path", _PATHS)
def test_stream_survives_broken_device_info(path, caplog):
    """device_info 整块缺失（建连早期/替身夹具）⇒ 吞掉并走旧水位，播报绝不哑。"""
    caplog.set_level(logging.INFO, logger="hj.test_v1089")
    import struct
    f = path / SAT
    src = f.read_text(encoding="utf-8")
    mh = re.search(r"_MAX_WAV_HEADER_BYTES\s*=\s*(\d+)", src)
    it = _extract_func(f, "_iter_wav_pcm_chunks", {
        "_parse_wav_header": _extract_func(f, "_parse_wav_header", {"struct": struct}),
        "_MAX_WAV_HEADER_BYTES": int(mh.group(1)),
    })
    fn = _extract_func(f, "_stream_tts_audio", {
        "asyncio": asyncio, "contextlib": contextlib,
        "_LOGGER": logging.getLogger("hj.test_v1089"),
        "VoiceAssistantEventType": _VevType,
        "_iter_wav_pcm_chunks": it,
        "_DEVICE_BUFFER_TARGET_S": OLD_S,
        # 刻意不给 _prebuf_target_for_version：NameError 必须被 suppress 兜住
    })

    class _NoInfo:
        def __init__(self):
            self.client = _Cli()
            self.flags = []

        def async_set_assist_pipeline_state(self, v):
            self.flags.append(v)

    class _S:
        _is_running = True
        _udp_server = None
        finished = 0

        def __init__(self):
            self._entry_data = _NoInfo()
            self.cli = self._entry_data.client

        def tts_response_finished(self):
            self.finished += 1

    pcm = bytes(4 * 1024)

    async def gen():
        yield _wav_header(len(pcm))
        for i in range(0, len(pcm), 1024):
            yield pcm[i:i + 1024]

    s = _S()
    asyncio.run(fn(s, _Result(gen()), None))
    assert s.finished == 1, "桩里没有分流符号却把播报做哑了——suppress 形态被破坏"
