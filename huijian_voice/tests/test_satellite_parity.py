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
