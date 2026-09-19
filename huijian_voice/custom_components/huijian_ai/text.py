"""Support for esphome texts."""

from __future__ import annotations

import logging
import importlib
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

        # v1.0.98（VM 真机联测 2026-09-19 定罪）：旧路=edge-tts 云合成 mp3 →
        # 写 www → media_player.play_media(URL) → 设备 http 自取。三处实锤：
        #   ① /local 静态路由在 www 首建前不注册——全新环境首播报必 404，
        #      重启 HA 才自愈（VM 台架逐字复现：anon/auth HEAD 均 404 len=14）；
        #   ② edge-tts 依赖微软云+证书栈（backlog NoAudioReceived 同源）；
        #   ③ 设备侧 url_play 24KB 内部栈 spawn 失败（V2 内部 RAM 7680B，
        #      固件 v2.1.57 已改 PSRAM 栈根治——但 URL 路对 API 音频板本就是
        #      形态错配）。
        # 主通道改走 core assist_satellite.announce → 本集成 async_announce →
        # _do_announce：有 message 即自合成推流（huijian_speech 本地引擎，
        # 与对话应答同音色 zf_044/1.25），复用实战下行链，/local、云、
        # url_play 三座山一并绕开；v1.0.96 门控 WARN 对撞活跃轮等分因全程有效。
        # 找不到同设备卫星（或极老 core 无该服务）→ 回退旧 URL 路，行为不倒退。
        satellite_id = self._find_satellite()
        if satellite_id is not None:
            try:
                await self.hass.services.async_call(
                    "assist_satellite",
                    "announce",
                    {"entity_id": satellite_id, "message": value},
                    blocking=False,
                )
                return
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "播报语音走 assist_satellite.announce 失败，回退 edge-tts URL 路",
                    exc_info=True,
                )
        try:
            await self._play_tts(value)
        except Exception:
            _LOGGER.warning("TTS 播放失败", exc_info=True)

    def _find_satellite(self) -> str | None:
        """查同设备 assist_satellite 实体（播报语音推流主通道入口）。"""
        from homeassistant.helpers import entity_registry as er

        if self.device_entry is None or not self.device_entry.id:
            return None
        entity_reg = er.async_get(self.hass)
        for entry in er.async_entries_for_device(entity_reg, self.device_entry.id):
            if entry.domain == "assist_satellite" and not entry.disabled_by:
                return entry.entity_id
        return None

    async def _play_tts(self, text: str) -> None:
        """使用 edge-tts 生成 MP3 音频，通过 media_player 播放。"""
        try:
            # 惰性 import 但必须离环：edge_tts 顶层级联加载 certifi 并执行
            # ssl.load_verify_locations——在事件循环内是阻塞调用，HA 2026.8
            # 实发钉出（"Detected blocking call to load_verify_locations …
            # text.py, line 100: import edge_tts"）。import-executor-job 是
            # HA 官方给定的循环内导入通道。
            edge_tts = await self.hass.async_add_import_executor_job(
                importlib.import_module, "edge_tts"
            )
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
