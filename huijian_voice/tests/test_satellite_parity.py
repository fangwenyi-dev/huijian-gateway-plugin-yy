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


def _extract_func(path: Path, name: str):
    """按仓惯例从真实源码抽出纯函数执行（不复制逻辑）。"""
    import ast
    import io
    import wave

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            ns: dict = {"io": io, "wave": wave}
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


def test_tts_and_satellite_fail_loud():
    """空音频/0 帧必须在两端各留一行 ERROR/WARNING，成功也留 INFO 供逐跳对账。"""
    tts_src = _read("tts.py")
    assert "合成结果为空" in tts_src, "集成端空音频不再点名"
    assert "音频就绪" in tts_src, "集成端成功无痕，链路无法对账"
    asat = _read("assist_satellite.py")
    assert "音频 0 帧" in asat, "卫星端 0 帧静音不再告警"
    assert "推流 %d 帧" in asat, "卫星端推流无痕"
