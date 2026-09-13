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
        self._attr_supported_codecs = [AudioCodecs.PCM, AudioCodecs.OPUS]
        self._attr_supported_formats = [AudioFormats.WAV, AudioFormats.OGG]
        self._attr_supported_channels = [x for x in AudioChannels]
        self._attr_supported_bit_rates = [x for x in AudioBitRates]
        self._attr_supported_sample_rates = [x for x in AudioSampleRates]
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
