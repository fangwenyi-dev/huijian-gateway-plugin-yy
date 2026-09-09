"""v1.0.19 卫星播报/上行对齐钉桩（台架×固件协议审计实锤三断点，防回退）。

审计结论：v1.0.18×固件 v2.1.11 组合下「灯会开、播报必哑」——
①固件只报 API_AUDIO 不报 SPEAKER，而集成推流门控只认 SPEAKER（AS:349/515）；
②慧尖 tts 实体只读 "audio_format" 键（core 实际传 preferred_format）恒回
   mp3，_stream_tts_audio「Only WAV」早退；
③aioesphomeapi≥45 双参调用 handle_audio(data, data2)，旧单参签名每帧
   TypeError 上行全断（实证：venv aioesphomeapi 46.3.0 client 源码 +
   上游 esphome/assist_satellite.py L615 已改双参）。
"""
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
CC = HERE / "custom_components" / "huijian_ai"


def _read(p):
    return (CC / p).read_text(encoding="utf-8")


def _extract_func(path: Path, name: str, extra_ns: dict | None = None):
    """按仓惯例从真实源码抽出纯函数执行（不复制逻辑）。"""
    import ast
    import io
    import wave

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            ns: dict = {"io": io, "wave": wave}
            if extra_ns:
                ns.update(extra_ns)
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), ns)
            return ns[name]
    raise AssertionError(f"{path} 中未找到 {name}")


def test_stream_gates_accept_api_audio():
    asat = _read("assist_satellite.py")
    # 事件门（TTS_START 起流）与选项门（preferred wav 声明）都必须
    # SPEAKER|API_AUDIO 并集，且 UDP 门保持 SPEAKER and not API_AUDIO 原样。
    n_union = asat.count("VoiceAssistantFeature.SPEAKER | VoiceAssistantFeature.API_AUDIO")
    assert n_union >= 2, f"两处 API_AUDIO 并集门控缺失（现 {n_union}）"
    assert "(feature_flags & VoiceAssistantFeature.SPEAKER) and not (" in asat, \
        "UDP 门控语义被误改（应仍排除 API_AUDIO 设备）"


def test_handle_audio_dual_signature():
    asat = _read("assist_satellite.py")
    assert "async def handle_audio(self, data: bytes, data2: bytes | None = None)" in asat, \
        "handle_audio 未对齐 aioesphomeapi≥45 双参（上行每帧 TypeError）"


def test_tts_honors_preferred_format():
    tts = _read("tts.py")
    assert 'options.get("preferred_format")' in tts, "preferred_format 键未消费（恒 mp3 回退）"
    assert '"wav"' in tts and "to_sample_rate" in tts, "wav 路径缺 16k/mono/16bit 参数化"


# ── v1.0.25：播报链路逐跳可观测 + 去 ffmpeg 依赖（2026-09-09 静音排查） ──
def test_wav_wrap_matches_satellite_contract():
    """s16le→WAV 纯 Python 直封必须与 _stream_tts_audio 的 16k/16bit/mono 检查同构。"""
    import io
    import wave

    fn = _extract_func(CC / "huijian" / "audio.py", "wrap_pcm_as_wav")
    pcm = b"\x00\x01" * 1600                      # 1600 samples @16k = 0.1s
    wav = fn(pcm, 16000, 1)
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getsampwidth() == 2
        assert w.getnchannels() == 1
        assert w.getnframes() == 1600
        assert w.readframes(1600) == pcm, "PCM 字节必须原样入容器"


def test_wav_fastpath_bypasses_ffmpeg():
    """直封分支必须在 get_ffmpeg_manager 之前——HA 未配 ffmpeg 集成时
    后者抛 RuntimeError，播报静默且报错离病因很远。"""
    src = (CC / "huijian" / "audio.py").read_text(encoding="utf-8")
    fast = src.index('if from_extension == "s16le" and to_extension == "wav":')
    # 用带赋值的代码行定位真实调用（注释里也提到该符号，不能误锚）
    ffmpeg = src.index("ffmpeg_manager = ffmpeg.get_ffmpeg_manager(hass)")
    assert fast < ffmpeg, "wav 直封被挪到 ffmpeg 之后（关键路径重新依赖 ffmpeg）"


def test_wav_fastpath_requires_matching_output_request():
    """直封只在「输出要求 == 源 PCM 形态」时成立，否则必须回退 ffmpeg。

    v1.0.25 的直封只按 input_params 推源形态，忽略 to_sample_rate/
    to_sample_channels/to_sample_bytes：一旦调用方要求立体声或异率，就会封出
    **头字段与要求不符**的 WAV——卫星按 WAV 头校验（16k/16bit/mono）直接拒收，
    又是一场静音；媒体播放器则变速播放。正反两向都钉住。
    """
    import asyncio
    import io
    import logging
    import types
    import wave

    audio_py = CC / "huijian" / "audio.py"
    reached: list[int] = []

    def _boom(_hass):
        reached.append(1)
        raise RuntimeError("stub: HA 未配置 ffmpeg 集成")

    convert = _extract_func(
        audio_py,
        "async_convert_audio",
        extra_ns={
            "ffmpeg": types.SimpleNamespace(get_ffmpeg_manager=_boom),
            "_LOGGER": logging.getLogger("pin"),
            "wrap_pcm_as_wav": _extract_func(audio_py, "wrap_pcm_as_wav"),
        },
    )

    pcm = b"\x00\x01" * 160

    async def _src():
        yield pcm

    async def _run(**kw):
        chunks = []
        async for c in convert(
            None,
            _src(),
            "s16le",
            "wav",
            input_params=["-ar", "16000", "-ac", "1"],
            **kw,
        ):
            chunks.append(c)
        return b"".join(chunks)

    # ① 卫星契约形态：必须直封，一次 ffmpeg 都不碰
    wav = asyncio.run(
        _run(to_sample_rate=16000, to_sample_channels=1, to_sample_bytes=2)
    )
    assert reached == [], "卫星契约形态竟回退了 ffmpeg（直封失效＝v1.0.25 退化）"
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        assert w.getnframes() == 160
        assert w.readframes(160) == pcm, "PCM 必须原样入容器"

    # ② 要求立体声：绝不许封一个头字段不符的 WAV 糊过去，必须走 ffmpeg
    try:
        asyncio.run(
            _run(to_sample_rate=16000, to_sample_channels=2, to_sample_bytes=2)
        )
    except RuntimeError:
        assert reached == [1], "未落到 ffmpeg 分支（异常来源可疑）"
    else:
        raise AssertionError("异形态请求仍走直封 → 会产出头字段与要求不符的 WAV")


def test_tts_and_satellite_fail_loud():
    """空音频/0 帧必须在两端各留一行 ERROR/WARNING，成功也留 INFO 供逐跳对账。"""
    tts_src = _read("tts.py")
    assert "合成结果为空" in tts_src, "集成端空音频不再点名"
    assert "音频就绪" in tts_src, "集成端成功无痕，链路无法对账"
    asat = _read("assist_satellite.py")
    assert "音频 0 帧" in asat, "卫星端 0 帧静音不再告警"
    assert "推流 %d 帧" in asat, "卫星端推流无痕"


def test_tts_declares_preferred_format_options():
    """supported_options 必须声明 preferred_*。

    HA core 的 _async_generate_tts_audio 对「不在 supported_options 里」的
    preferred_* 一律 `options.pop(...)`——引擎收不到首选格式就恒回 mp3，
    再由 HA 用 ffmpeg 二次转码（v1.0.25 的 s16le→wav 直封成死代码）。
    2026-09-09 抓 HA core tts/__init__.py 源码实锤。
    """
    tts = _read("tts.py")
    for key in (
        "preferred_format",
        "preferred_sample_rate",
        "preferred_sample_channels",
        "preferred_sample_bytes",
    ):
        assert f'"{key}"' in tts, f"supported_options 缺 {key}（HA 会弹掉它）"
    assert "_attr_supported_options = []" not in tts, "supported_options 仍为空表"


def test_tts_empty_audio_never_poisons_cache():
    """空音频必须返回 (None, None)。

    返回 (fmt, b"") 会被 HA 写进 TTS 缓存，此后同一句话永远命中空缓存
    （连 TTS 引擎都不再被调用），现场形态即"灯开了、永远没声音、日志一片
    安静"（2026-09-09 实锤）。返回 None 让 HA 报错并跳过缓存。
    """
    import ast

    tree = ast.parse((CC / "tts.py").read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "async_get_tts_audio"
    )
    guard = next(
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.If) and ast.unparse(n.test).strip() == "not audio"
    )
    rets = [ast.unparse(s) for s in guard.body if isinstance(s, ast.Return)]
    # ast.unparse 可能渲染成 "return (None, None)"
    norm = {r.replace("(", "").replace(")", "").strip() for r in rets}
    assert "return None, None" in norm, f"空音频分支未返回 (None, None)：{rets}"


def test_tts_end_suppressed_for_api_audio():
    """v1.0.27：API 推流设备不再收到抢跑的 TTS_END{url} 事件。

    实机 2026-09-09 17:21:36：TTS_END 事件先到、1.14s 音频（35×1024+640B）
    后到，固件据此把刚起的会话拆掉、整段播报当"状态不对"丢光——现场表现
    仍是"灯开了没声音"。慧尖板只宣告 API_AUDIO、无 media_player 自取能力，
    url 对它无意义，流的生死由 STREAM_START/STREAM_END 表达即可。
    门必须收紧在 API_AUDIO：若沿用 SPEAKER|API_AUDIO 并集，会把真 ESPHome
    喇叭设备（靠 url 播放）的播报一并打死。
    """
    n = " ".join(_read("assist_satellite.py").split())
    assert "if feature_flags & VoiceAssistantFeature.API_AUDIO:" in n, \
        "抑制门未收紧到 API_AUDIO（会误伤 SPEAKER 型设备的 url 播报）"
    assert "suppress_event = True" in n, "API 推流分支未置抑制标志"
    assert (
        "if not suppress_event: "
        "self.cli.send_voice_assistant_event(event_type, data_to_send)" in n
    ), "事件发送尾巴未受抑制标志约束"
