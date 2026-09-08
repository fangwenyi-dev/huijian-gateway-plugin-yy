import logging

import opuslib_next as opuslib
from homeassistant.components.tts import TextToSpeechEntity as BaseEntity
from homeassistant.components.tts import TtsAudioType
from homeassistant.components.tts.const import DOMAIN as ENTITY_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .huijian import tts_transport
from .huijian.audio import async_convert_audio

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities
):
    """Set up entities."""
    async_add_entities([HuijianTtsEntity(hass, config_entry)])


class HuijianTtsEntity(BaseEntity):
    domain = ENTITY_DOMAIN
    opus_channels = 1
    opus_sample_rate = 16000
    opus_frame_duration = 60
    opus_frame_samples = int(opus_sample_rate * opus_frame_duration / 1000)

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        _LOGGER.info("huijianTtsEntity.__init__")
        self.hass = hass
        self.entry = entry
        self.entity_id = f"{self.domain}.huijian_speech"
        self._attr_name = "huijian AI 语音合成"
        self._attr_unique_id = f"{self.entry.entry_id}-{ENTITY_DOMAIN}"
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name="huijian AI",
            manufacturer="huijian",
            entry_type=dr.DeviceEntryType.SERVICE,
        )
        self._attr_default_language = "zh-Hans"
        self._attr_supported_languages = ["en", "zh", "zh-Hans"]
        self._attr_supported_options = []
        self._attr_extra_state_attributes = {}

    async def async_added_to_hass(self):
        _LOGGER.info("huijianTtsEntity.async_added_to_hass")
        await super().async_added_to_hass()

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict
    ) -> TtsAudioType:
        _LOGGER.info(
            "huijianTtsEntity.async_get_tts_audio: message=%s, language=%s, options=%s",
            message,
            language,
            options,
        )
        transport = tts_transport.get_entry_transport(self.hass, self.entry)
        if not await transport.ensure_connected():
            _LOGGER.error("Failed to establish WebSocket connection for TTS")
            return None, None

        # v1.0.19：消费 core 下发的首选格式（assist_satellite 对可推流设备传
        # preferred_format="wav"/16k/mono/16bit，卫星流式通道 AS:_stream_tts_audio
        # 只认 wav——此前只读 "audio_format" 键（core 从不传）恒回 mp3，播报在
        # 卫星端必「Only WAV」早退，台架×固件协议审计实锤）。无 preferred 时
        # 保持旧行为 mp3。
        fmt = options.get("preferred_format") or options.get("audio_format") or "mp3"
        await transport.send_message(
            {
                "type": "tts",
                "state": "detect",
                "text": message,
            }
        )

        async def data_gen():
            decoder = opuslib.Decoder(self.opus_sample_rate, self.opus_channels)
            async for resp in transport.await_message():
                if isinstance(resp, bytes):
                    try:
                        resp = decoder.decode(resp, self.opus_frame_samples)
                    except Exception as e:
                        _LOGGER.error("Decode opus failed: %s", e)
                    _LOGGER.info("Received bytes: %s %s", len(resp), resp.hex()[0:64])
                    yield resp
                else:
                    if getattr(resp, "error", None):
                        raise RuntimeError(resp.error)
                    _LOGGER.info("Received response: %s", resp)

        audio = b""
        converting = async_convert_audio(
            self.hass,
            data_gen(),
            "s16le",
            to_extension=fmt,
            input_params=[
                "-ar",
                str(self.opus_sample_rate),
                "-ac",
                str(self.opus_channels),
            ],
            **({"to_sample_rate": int(options.get("preferred_sample_rate") or 16000),
                "to_sample_channels": int(options.get("preferred_sample_channels") or 1),
                "to_sample_bytes": int(options.get("preferred_sample_bytes") or 2)}
               if fmt == "wav" else {}),
        )
        async for chunk in converting:
            audio += chunk
        return fmt, audio
