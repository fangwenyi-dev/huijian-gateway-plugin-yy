"""Support for esphome texts."""

from __future__ import annotations

import logging
import time
from functools import partial
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

from aioesphomeapi import EntityInfo, TextInfo
from aioesphomeapi import TextMode as EsphomeTextMode
from aioesphomeapi import TextState
from homeassistant.components.text import TextEntity, TextMode
from homeassistant.core import callback

from .entity import (EsphomeEntity, convert_api_error_ha_error,
                     esphome_state_property, platform_async_setup_entry)
from .enum_mapper import EsphomeEnumMapper

PARALLEL_UPDATES = 0

TEXT_MODES: EsphomeEnumMapper[EsphomeTextMode, TextMode] = EsphomeEnumMapper(
    {
        EsphomeTextMode.TEXT: TextMode.TEXT,
        EsphomeTextMode.PASSWORD: TextMode.PASSWORD,
    }
)

EDGE_TTS_VOICES = {
    "zh-CN": "zh-CN-XiaoxiaoNeural",
    "zh-HK": "zh-HK-HiuGaaiNeural",
    "zh-TW": "zh-TW-HsiaoChenNeural",
    "en": "en-US-AriaNeural",
    "en-US": "en-US-AriaNeural",
    "en-GB": "en-GB-SoniaNeural",
    "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural",
    "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural",
    "es": "es-ES-AlvaroNeural",
    "it": "it-IT-ElsaNeural",
    "pt": "pt-BR-FranciscaNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "ar": "ar-SA-ZariyahNeural",
    "hi": "hi-IN-SwaraNeural",
    "th": "th-TH-PremwadeeNeural",
    "vi": "vi-VN-HoaiMyNeural",
}


def _get_edge_tts_voice(hass_language: str) -> str:
    """根据 HA 语言设置返回合适的 edge-tts 语音角色."""
    if hass_language in EDGE_TTS_VOICES:
        return EDGE_TTS_VOICES[hass_language]
    base_lang = hass_language.split("-")[0] if "-" in hass_language else hass_language
    return EDGE_TTS_VOICES.get(base_lang, "zh-CN-XiaoxiaoNeural")


class EsphomeText(EsphomeEntity[TextInfo, TextState], TextEntity):
    """A text implementation for esphome."""

    @callback
    def _on_static_info_update(self, static_info: EntityInfo) -> None:
        """Set attrs from static info."""
        super()._on_static_info_update(static_info)
        static_info = self._static_info
        self._attr_native_min = static_info.min_length
        self._attr_native_max = static_info.max_length
        self._attr_pattern = static_info.pattern
        self._attr_mode = TEXT_MODES.from_esphome(static_info.mode) or TextMode.TEXT

    @property
    @esphome_state_property
    def native_value(self) -> str | None:
        """Return the state of the entity."""
        state = self._state
        return None if state.missing_state else state.state

    @convert_api_error_ha_error
    async def async_set_value(self, value: str) -> None:
        """Update the current value."""
        static_info = self._static_info
        if (
            not hasattr(static_info, "object_id")
            or static_info.object_id != "play_voice_text"
        ):
            self._client.text_command(self._key, value, device_id=static_info.device_id)
            return

        try:
            await self._play_tts(value)
        except Exception:
            _LOGGER.warning("TTS 播放失败", exc_info=True)

    async def _play_tts(self, text: str) -> None:
        """使用 edge-tts 生成 MP3 音频，通过 media_player 播放。"""
        try:
            import edge_tts
        except ImportError:
            _LOGGER.warning("edge-tts 未安装，无法播放 TTS")
            return
        voice = _get_edge_tts_voice(self.hass.config.language)
        communicate = edge_tts.Communicate(text, voice)
        mp3_data = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_data += chunk["data"]

        if not mp3_data:
            _LOGGER.warning("edge-tts 生成的音频为空: %s", text)
            return

        www_dir = Path(self.hass.config.path("www"), "huijian_tts")
        await self.hass.async_add_executor_job(
            partial(www_dir.mkdir, parents=True, exist_ok=True)
        )

        timestamp = int(time.time() * 1000)
        mp3_path = www_dir / f"tts_{timestamp}.mp3"
        await self.hass.async_add_executor_job(mp3_path.write_bytes, mp3_data)

        from homeassistant.helpers.network import get_url

        try:
            base_url = get_url(self.hass, prefer_external=False)
        except Exception:
            base_url = (
                f"http://{self.hass.config.api.host}:{self.hass.http.server_port}"
            )

        url = f"{base_url}/local/huijian_tts/tts_{timestamp}.mp3"

        media_player_entity_id = await self._find_media_player()
        if media_player_entity_id is None:
            _LOGGER.warning("未找到媒体播放器实体，无法播放 TTS")
            return

        await self.hass.services.async_call(
            "media_player",
            "play_media",
            {
                "entity_id": media_player_entity_id,
                "media_content_id": url,
                "media_content_type": "music",
                "announce": True,
            },
            blocking=False,
        )

        await self.hass.async_add_executor_job(self._cleanup_old_tts_files, www_dir)

    async def _find_media_player(self) -> str | None:
        """查找与本设备关联的媒体播放器实体ID。"""
        from homeassistant.helpers import entity_registry as er

        device_id = self.device_entry.id
        if not device_id:
            return None

        entity_reg = er.async_get(self.hass)

        for entry in er.async_entries_for_device(entity_reg, device_id):
            if entry.domain == "media_player":
                return entry.entity_id

        for state in self.hass.states.async_all("media_player"):
            reg_entry = entity_reg.async_get(state.entity_id)
            if reg_entry and reg_entry.device_id == device_id:
                return state.entity_id

        device_name = self.device_entry.name
        if device_name:
            for state in self.hass.states.async_all("media_player"):
                if device_name.lower() in state.entity_id.lower():
                    return state.entity_id

        return None

    def _cleanup_old_tts_files(self, www_dir: Path) -> None:
        """删除旧的TTS文件，只保留最新10个。"""
        files = sorted(www_dir.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files[10:]:
            f.unlink(missing_ok=True)


async_setup_entry = partial(
    platform_async_setup_entry,
    info_type=TextInfo,
    entity_type=EsphomeText,
    state_type=TextState,
)
