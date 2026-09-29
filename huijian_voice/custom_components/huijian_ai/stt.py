import logging
from collections.abc import AsyncIterable

import opuslib_next as opuslib
from homeassistant.components.stt import DOMAIN as ENTITY_DOMAIN
from homeassistant.components.stt import (AudioBitRates, AudioChannels,
                                          AudioCodecs, AudioFormats,
                                          AudioSampleRates, SpeechMetadata,
                                          SpeechResult, SpeechResultState)
from homeassistant.components.stt import SpeechToTextEntity as BaseEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .huijian import stt_transport
from .huijian.audio import wav_to_opus

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities
):
    """Set up entities."""
    async_add_entities([HuijianSttEntity(hass, config_entry)])


class HuijianSttEntity(BaseEntity):
    domain = ENTITY_DOMAIN
    opus_channels = 1
    opus_sample_rate = 16000
    opus_frame_duration = 60
    opus_frame_samples = int(opus_sample_rate * opus_frame_duration / 1000)

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.entity_id = f"{self.domain}.huijian_asr"
        self._attr_name = "huijian AI 语音识别"
        self._attr_unique_id = f"{self.entry.entry_id}-{ENTITY_DOMAIN}"
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name="huijian AI",
            manufacturer="huijian",
            entry_type=dr.DeviceEntryType.SERVICE,
        )
        self._attr_supported_languages = ["en", "zh", "zh-Hans"]
        # v1.1.27（申报面对齐实现，H7 假成功同族）：旧形态申报**全量**
        # codecs/formats/channels/bit_rates/sample_rates（含 OPUS/OGG/48k/双声道），
        # 而下游 `wav_to_opus` 只剥 RIFF 头、之后一律按 16bit PCM 解帧——交
        # 48k/OGG/OPUS 时音频被当乱码编码，端到端仍回 SUCCESS（管线播"乱码
        # 转写"，现场只看见"识别瞎"。OPUS 并不真走 wav_to_opus：本引擎的
        # opus 出口是编码侧，入口只认 WAV/PCM）。故申报面收窄到实现真正支持
        # 的形态；不支持形态在 `async_process_audio_stream` 入口如实 ERROR。
        self._attr_supported_codecs = [AudioCodecs.PCM]
        self._attr_supported_formats = [AudioFormats.WAV]
        self._attr_supported_channels = [AudioChannels.CHANNEL_MONO]
        self._attr_supported_bit_rates = [AudioBitRates.BITRATE_16]
        self._attr_supported_sample_rates = [AudioSampleRates.SAMPLERATE_16000]
        self.opus_encoder = opuslib.Encoder(
            self.opus_sample_rate, self.opus_channels, opuslib.APPLICATION_VOIP
        )

    @property
    def supported_languages(self):
        return self._attr_supported_languages

    @property
    def supported_codecs(self):
        return self._attr_supported_codecs

    @property
    def supported_formats(self):
        return self._attr_supported_formats

    @property
    def supported_channels(self):
        return self._attr_supported_channels

    @property
    def supported_bit_rates(self):
        return self._attr_supported_bit_rates

    @property
    def supported_sample_rates(self):
        return self._attr_supported_sample_rates

    async def async_added_to_hass(self):
        await super().async_added_to_hass()

    async def async_process_audio_stream(
        self, metadata: SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> SpeechResult:
        _LOGGER.info(
            "Processing audio stream: language=%s, format=%s, codec=%s, bit_rate=%s, sample_rate=%s",
            metadata.language,
            metadata.format,
            metadata.codec,
            metadata.bit_rate,
            metadata.sample_rate,
        )
        unsupported = [
            f"{name}={got}"
            for name, got, want in (
                ("format", metadata.format, AudioFormats.WAV),
                ("codec", metadata.codec, AudioCodecs.PCM),
                ("channel", metadata.channel, AudioChannels.CHANNEL_MONO),
                ("bit_rate", metadata.bit_rate, AudioBitRates.BITRATE_16),
                ("sample_rate", metadata.sample_rate,
                 AudioSampleRates.SAMPLERATE_16000),
            )
            if got != want
        ]
        if unsupported:
            # v1.1.27：申报面（supported_*）已收窄，但调用方仍可能绕过声明把
            # 48k/OGG/OPUS 直接塞进来（旧形态会按 16k/mono/PCM 硬解=乱码，却
            # 照样回 SUCCESS）。入口如实拒收，不再制造假成功。
            _LOGGER.error(
                "STT 收到未支持的音频形态（%s）——本引擎只支持 16kHz/mono/16bit "
                "PCM-WAV（wav_to_opus 仅剥 RIFF 头后按 16bit PCM 解帧，其它形态会被"
                "当乱码编码）。按 ERROR 如实收口。",
                "、".join(unsupported),
            )
            return SpeechResult(None, SpeechResultState.ERROR)
        # H7/M9/M10（2026-09-23 深审批5）：整轮听写交给 transport.recognize
        # 统一持锁+发送超时+残帧清算（TTS v1.0.45 三件套迁移）。旧形态三罪：
        # ①发送裸调无超时，悬挂 writer 永久阻塞；②超时/连接死仍回
        # SUCCESS(None)——管线播"空话"假成功；③每帧一条 INFO 刷屏。
        # 帧级日志收进 transport（聚合 DEBUG），此处只留结论一条。
        transport = stt_transport.get_entry_transport(self.hass, self.entry)
        text, error = await transport.recognize(wav_to_opus(stream), timeout=60)
        if error:
            _LOGGER.error("STT 失败（如实报 ERROR，不再假成功）: %s", error)
            return SpeechResult(None, SpeechResultState.ERROR)
        if text is None:
            # 引擎无字可回=故障面（正常空识别是 ""），按 ERROR 收口
            _LOGGER.error("STT 未获转录消息（服务端未回），按 ERROR 收口")
            return SpeechResult(None, SpeechResultState.ERROR)
        _LOGGER.info("STT 完成: %r", (text or "")[:40])
        return SpeechResult(text, SpeechResultState.SUCCESS)  # type: ignore
