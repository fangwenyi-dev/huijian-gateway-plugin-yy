"""Support for assist satellites in ESPHome."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import socket
import wave
from collections.abc import AsyncIterable
from functools import partial
from itertools import chain
from pathlib import Path
from typing import Any, cast

import voluptuous as vol
from aioesphomeapi import (MediaPlayerFormatPurpose,
                           MediaPlayerSupportedFormat,
                           VoiceAssistantAnnounceFinished,
                           VoiceAssistantAudioSettings,
                           VoiceAssistantCommandFlag, VoiceAssistantEventType,
                           VoiceAssistantExternalWakeWord,
                           VoiceAssistantFeature, VoiceAssistantTimerEventType)
from homeassistant.components import assist_satellite, tts
from homeassistant.components.assist_pipeline import (PipelineEvent,
                                                      PipelineEventType,
                                                      PipelineStage)
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.intent import (TimerEventType, TimerInfo,
                                             async_register_timer_handler)
from homeassistant.components.media_player import async_process_play_media_url
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import \
    AddConfigEntryEntitiesCallback
from homeassistant.helpers.network import get_url
from homeassistant.helpers.singleton import singleton
from homeassistant.util.hass_dict import HassKey
from voluptuous.humanize import humanize_error

from .const import DOMAIN, WAKE_WORDS_API_PATH, WAKE_WORDS_DIR_NAME
from .entity import EsphomeAssistEntity, convert_api_error_ha_error
from .entry_data import ESPHomeConfigEntry
from .enum_mapper import EsphomeEnumMapper
from .ffmpeg_proxy import async_create_proxy_url

PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

_VOICE_ASSISTANT_EVENT_TYPES: EsphomeEnumMapper[
    VoiceAssistantEventType, PipelineEventType
] = EsphomeEnumMapper(
    {
        VoiceAssistantEventType.VOICE_ASSISTANT_ERROR: PipelineEventType.ERROR,
        VoiceAssistantEventType.VOICE_ASSISTANT_RUN_START: PipelineEventType.RUN_START,
        VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END: PipelineEventType.RUN_END,
        VoiceAssistantEventType.VOICE_ASSISTANT_STT_START: PipelineEventType.STT_START,
        VoiceAssistantEventType.VOICE_ASSISTANT_STT_END: PipelineEventType.STT_END,
        VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_START: PipelineEventType.INTENT_START,
        VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_PROGRESS: PipelineEventType.INTENT_PROGRESS,
        VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_END: PipelineEventType.INTENT_END,
        VoiceAssistantEventType.VOICE_ASSISTANT_TTS_START: PipelineEventType.TTS_START,
        VoiceAssistantEventType.VOICE_ASSISTANT_TTS_END: PipelineEventType.TTS_END,
        VoiceAssistantEventType.VOICE_ASSISTANT_WAKE_WORD_START: PipelineEventType.WAKE_WORD_START,
        VoiceAssistantEventType.VOICE_ASSISTANT_WAKE_WORD_END: PipelineEventType.WAKE_WORD_END,
        VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_START: PipelineEventType.STT_VAD_START,
        VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_END: PipelineEventType.STT_VAD_END,
    }
)

_TIMER_EVENT_TYPES: EsphomeEnumMapper[VoiceAssistantTimerEventType, TimerEventType] = (
    EsphomeEnumMapper(
        {
            VoiceAssistantTimerEventType.VOICE_ASSISTANT_TIMER_STARTED: TimerEventType.STARTED,
            VoiceAssistantTimerEventType.VOICE_ASSISTANT_TIMER_UPDATED: TimerEventType.UPDATED,
            VoiceAssistantTimerEventType.VOICE_ASSISTANT_TIMER_CANCELLED: TimerEventType.CANCELLED,
            VoiceAssistantTimerEventType.VOICE_ASSISTANT_TIMER_FINISHED: TimerEventType.FINISHED,
        }
    )
)

_ANNOUNCEMENT_TIMEOUT_SEC = 5 * 60  # 5 minutes
_CONFIG_TIMEOUT_SEC = 5

# v1.0.40：上行音频队列**必须有界**。设备按 32ms/帧（≈31 帧/秒、≈1KB/帧）推，
# 入队由设备消息驱动（handle_audio / UDP datagram_received），而消费方是 HA 管线
# ——一旦管线停顿（HA 事件循环被占、STT 引擎慢），旧的 `asyncio.Queue()`（无
# maxsize）就按 ≈32KB/s 无界涨 HA 内存。上限取 ≈5 秒语音，溢出丢**最旧**保最新：
# 宁可丢开头也不丢刚说的话，且绝不无界涨。
_MAX_AUDIO_QUEUE_CHUNKS = 160
_AUDIO_DROP_LOG_EVERY = 100       # 丢包告警限频（1 次 + 每 100 块）
# v1.0.43：wake_word select 扫描硬上限（本集成只建 0/1 两个，留足余量）。
# 挑索引环以此兜底——注册表异常也绝不可能无限推进（防事件循环挂死类事故复发）。
_MAX_WAKE_WORD_SELECTS = 16
_STREAM_END_POLL_S = 0.5          # v1.0.41 审查 S4：哨兵被挤丢后"pending+排空"兜底的观察节拍


def _queue_audio_chunk(queue: "asyncio.Queue[bytes | None]", item) -> bool:
    """把音频块/结束哨兵放进有界队列：满则丢最旧。返回是否发生了丢弃。

    **哨兵绝不丢**：`None` 是 `_wrap_audio_stream` 的结束信号，丢了会让管线永不
    收束（设备侧只能等自己的会话超时）。本实现先腾格再入队，故单事件循环内
    （get→put 之间无 await，原子）哨兵必然入队。
    """
    try:
        queue.put_nowait(item)
        return False
    except asyncio.QueueFull:
        pass
    try:
        queue.get_nowait()          # 丢最旧
    except asyncio.QueueEmpty:      # 并发腾空竞态：让下面的 put 再试一次
        pass
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:       # 理论不可达（刚腾出一格）
        return True
    return True
_WAKE_WORD_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required("type"): str,
        vol.Required("wake_word"): str,
    },
    extra=vol.ALLOW_EXTRA,
)
_DATA_WAKE_WORDS: HassKey[dict[str, VoiceAssistantExternalWakeWord]] = HassKey(
    "wake_word_cache"
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ESPHomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Assist satellite entity."""
    entry_data = entry.runtime_data
    assert entry_data.device_info is not None
    if entry_data.device_info.voice_assistant_feature_flags_compat(
        entry_data.api_version
    ):
        async_add_entities([EsphomeAssistSatellite(entry)])


class EsphomeAssistSatellite(
    EsphomeAssistEntity, assist_satellite.AssistSatelliteEntity
):
    """Satellite running ESPHome."""

    entity_description = assist_satellite.AssistSatelliteEntityDescription(
        key="assist_satellite", translation_key="assist_satellite"
    )

    def __init__(self, entry: ESPHomeConfigEntry) -> None:
        """Initialize satellite."""
        super().__init__(entry.runtime_data)

        self.config_entry = entry
        self.cli = self._entry_data.client

        self._is_running: bool = True
        self._pipeline_task: asyncio.Task | None = None
        # v1.0.40：有界（≈5s 语音）——消费停顿时丢最旧，绝不无界涨内存
        self._audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=_MAX_AUDIO_QUEUE_CHUNKS
        )
        self._stream_end_pending = False   # v1.0.41 审查 S4：哨兵兜底标记（随新流清零）
        self._audio_dropped_chunks: int = 0
        self._tts_streaming_task: asyncio.Task | None = None
        self._udp_server: VoiceAssistantUDPServer | None = None

        # Empty config. Updated when added to HA.
        self._satellite_config = assist_satellite.AssistSatelliteConfiguration(
            available_wake_words=[], active_wake_words=[], max_active_wake_words=1
        )

        self._active_pipeline_index = 0

    def _get_entity_id(self, suffix: str) -> str | None:
        """Return the entity id for pipeline select, etc."""
        if self._entry_data.device_info is None:
            return None

        ent_reg = er.async_get(self.hass)
        return ent_reg.async_get_entity_id(
            Platform.SELECT,
            DOMAIN,
            f"{self._entry_data.device_info.mac_address}-{suffix}",
        )

    @property
    def pipeline_entity_id(self) -> str | None:
        """Return the entity ID of the primary pipeline to use for the next conversation."""
        return self.get_pipeline_entity(self._active_pipeline_index)

    def get_pipeline_entity(self, index: int) -> str | None:
        """Return the entity ID of a pipeline by index."""
        id_suffix = "" if index < 1 else f"_{index + 1}"
        return self._get_entity_id(f"pipeline{id_suffix}")

    def get_wake_word_entity(self, index: int) -> str | None:
        """Return the entity ID of a wake word by index."""
        id_suffix = "" if index < 1 else f"_{index + 1}"
        return self._get_entity_id(f"wake_word{id_suffix}")

    @property
    def vad_sensitivity_entity_id(self) -> str | None:
        """Return the entity ID of the VAD sensitivity to use for the next conversation."""
        return self._get_entity_id("vad_sensitivity")

    @callback
    def async_get_configuration(
        self,
    ) -> assist_satellite.AssistSatelliteConfiguration:
        """Get the current satellite configuration."""
        return self._satellite_config

    async def async_set_configuration(
        self, config: assist_satellite.AssistSatelliteConfiguration
    ) -> None:
        """Set the current satellite configuration."""
        await self.cli.set_voice_assistant_configuration(
            active_wake_words=config.active_wake_words
        )
        _LOGGER.debug("Set active wake words: %s", config.active_wake_words)

        # Ensure configuration is updated
        await self._update_satellite_config()

    async def _update_satellite_config(self) -> None:
        """Get the latest satellite configuration from the device."""
        wake_words = await async_get_custom_wake_words(self.hass)
        if wake_words:
            _LOGGER.debug("Found custom wake words: %s", sorted(wake_words.keys()))

        try:
            config = await self.cli.get_voice_assistant_configuration(
                _CONFIG_TIMEOUT_SEC,
                external_wake_words=list(wake_words.values()),
            )
        except TimeoutError:
            # Placeholder config will be used
            return

        # Update available/active wake words
        self._satellite_config.available_wake_words = [
            assist_satellite.AssistSatelliteWakeWord(
                id=model.id,
                wake_word=model.wake_word,
                trained_languages=list(model.trained_languages),
            )
            for model in config.available_wake_words
        ]
        self._satellite_config.active_wake_words = list(config.active_wake_words)
        self._satellite_config.max_active_wake_words = config.max_active_wake_words
        _LOGGER.debug("Received satellite configuration: %s", self._satellite_config)

        # Inform listeners that config has been updated
        self._entry_data.async_assist_satellite_config_updated(self._satellite_config)

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()

        assert self._entry_data.device_info is not None
        feature_flags = (
            self._entry_data.device_info.voice_assistant_feature_flags_compat(
                self._entry_data.api_version
            )
        )
        if feature_flags & VoiceAssistantFeature.API_AUDIO:
            # TCP audio
            self.async_on_remove(
                self.cli.subscribe_voice_assistant(
                    handle_start=self.handle_pipeline_start,
                    handle_stop=self.handle_pipeline_stop,
                    handle_audio=self.handle_audio,
                    handle_announcement_finished=self.handle_announcement_finished,
                )
            )
        else:
            # UDP audio
            self.async_on_remove(
                self.cli.subscribe_voice_assistant(
                    handle_start=self.handle_pipeline_start,
                    handle_stop=self.handle_pipeline_stop,
                    handle_announcement_finished=self.handle_announcement_finished,
                )
            )

        if feature_flags & VoiceAssistantFeature.TIMERS:
            # Device supports timers
            assert (self.registry_entry is not None) and (
                self.registry_entry.device_id is not None
            )
            self.async_on_remove(
                async_register_timer_handler(
                    self.hass, self.registry_entry.device_id, self.handle_timer_event
                )
            )

        assert self._attr_supported_features is not None
        if feature_flags & VoiceAssistantFeature.ANNOUNCE:
            # Device supports announcements
            self._attr_supported_features |= (
                assist_satellite.AssistSatelliteEntityFeature.ANNOUNCE
            )

            # Block until config is retrieved.
            # If the device supports announcements, it will return a config.
            _LOGGER.debug("Waiting for satellite configuration")
            await self._update_satellite_config()

        if not (feature_flags & VoiceAssistantFeature.SPEAKER):
            # Will use media player for TTS/announcements
            self._update_tts_format()

        if feature_flags & VoiceAssistantFeature.START_CONVERSATION:
            self._attr_supported_features |= (
                assist_satellite.AssistSatelliteEntityFeature.START_CONVERSATION
            )

        # Update wake word select when config is updated
        self.async_on_remove(
            self._entry_data.async_register_assist_satellite_set_wake_words_callback(
                self.async_set_wake_words
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        await super().async_will_remove_from_hass()

        self._is_running = False
        self._stop_pipeline()

    def on_pipeline_event(self, event: PipelineEvent) -> None:
        """Handle pipeline events."""
        try:
            event_type = _VOICE_ASSISTANT_EVENT_TYPES.from_hass(event.type)
        except KeyError:
            _LOGGER.debug("Received unknown pipeline event type: %s", event.type)
            return

        data_to_send: dict[str, Any] = {}
        # v1.0.27：API 推流设备的 TTS_END{url} 与推流并发发出，实测抢跑会把
        # 固件刚起的会话拆掉（详见固件 v2.1.16）。此类设备本就无 media_url
        # 自取能力，流的生死由 STREAM_START/STREAM_END 这对消息表达即可。
        suppress_event = False
        if event_type == VoiceAssistantEventType.VOICE_ASSISTANT_STT_START:
            self._entry_data.async_set_assist_pipeline_state(True)
        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_STT_END:
            assert event.data is not None
            data_to_send = {"text": event.data["stt_output"]["text"]}
        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_PROGRESS:
            if (
                not event.data
                or ("tts_start_streaming" not in event.data)
                or (not event.data["tts_start_streaming"])
            ):
                # ESPHome only needs to know if early TTS streaming is available
                return

            data_to_send = {"tts_start_streaming": "1"}
        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_END:
            assert event.data is not None
            data_to_send = {
                "conversation_id": event.data["intent_output"]["conversation_id"],
                "continue_conversation": str(
                    int(event.data["intent_output"]["continue_conversation"])
                ),
            }
        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_TTS_START:
            assert event.data is not None
            data_to_send = {"text": event.data["tts_input"]}
        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_TTS_END:
            assert event.data is not None
            if tts_output := event.data["tts_output"]:
                path = tts_output["url"]
                url = async_process_play_media_url(self.hass, path)
                data_to_send = {"url": url}

                assert self._entry_data.device_info is not None
                feature_flags = (
                    self._entry_data.device_info.voice_assistant_feature_flags_compat(
                        self._entry_data.api_version
                    )
                )
                # v1.0.19：SPEAKER 或 API_AUDIO 任一即接受 API 推流——卫星流式
                # 通道只有这一条（本 fork 设备无 media_url 自取能力）。上游按
                # SPEAKER 门控，而慧尖固件 v2.1.11 只宣告 API_AUDIO（有喇叭、
                # on_audio 播 16k PCM，只是不报 SPEAKER）→旧门控下播报永静默
                # （台架×固件协议审计实锤）。两 flag 同报时 port 仍 0（AS:501
                # 条件含 not API_AUDIO），UDP 语义不变。
                if feature_flags & (
                    VoiceAssistantFeature.SPEAKER | VoiceAssistantFeature.API_AUDIO
                ) and (stream := tts.async_get_stream(self.hass, tts_output["token"])):
                    self._tts_streaming_task = (
                        self.config_entry.async_create_background_task(
                            self.hass,
                            self._stream_tts_audio(stream),
                            "esphome_voice_assistant_tts",
                        )
                    )
                    if feature_flags & VoiceAssistantFeature.API_AUDIO:
                        # 只吃 API 音频、无 media_player 自取能力的设备（慧尖板
                        # v2.1.11 起只宣告 API_AUDIO）：url 事件对它无意义，
                        # 还会与推流赛跑 → 抑制。SPEAKER 型（真 ESPHome 喇叭）
                        # 仍靠 url 播放，保持原样发送。
                        suppress_event = True
                        _LOGGER.debug(
                            "[TTS] API 推流设备：抑制抢跑的 TTS_END{url} 事件"
                        )
        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_WAKE_WORD_END:
            assert event.data is not None
            if not event.data["wake_word_output"]:
                event_type = VoiceAssistantEventType.VOICE_ASSISTANT_ERROR
                data_to_send = {
                    "code": "no_wake_word",
                    "message": "No wake word detected",
                }
        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_ERROR:
            assert event.data is not None
            if event.data.get("code") == "validation-error" and not any(
                e.data.get("config_type") == "assist"
                for e in self.hass.config_entries.async_entries(DOMAIN)
            ):
                # 台架实发（2026-09-08）：assist 引擎条目被用户删除后按键，pipeline
                # validate 抛 validation-error。v1.0.16 起设备条目每次 setup 幂等
                # 补建（缺则重建、有则不碰）——重启 HA 即自愈；本指引覆盖"尚未
                # 重启"的窗口期，让两端日志都直接说出病因与药方，不再各剩一条
                # 无上下文告警。
                _LOGGER.warning(
                    "慧尖语音会话 pipeline 校验失败：域内无 assist 引擎条目。"
                    "修复：重启 HA 由设备条目自动补建，或在 设置→语音助手 手动添加"
                )
            data_to_send = {
                "code": event.data["code"],
                "message": event.data["message"],
            }
        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_RUN_START:
            assert event.data is not None
            if tts_output := event.data.get("tts_output"):
                path = tts_output["url"]
                url = async_process_play_media_url(self.hass, path)
                data_to_send = {"url": url}
        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END:
            if self._tts_streaming_task is None:
                # No TTS
                self._entry_data.async_set_assist_pipeline_state(False)

        if not suppress_event:
            self.cli.send_voice_assistant_event(event_type, data_to_send)

    @convert_api_error_ha_error
    async def async_announce(
        self, announcement: assist_satellite.AssistSatelliteAnnouncement
    ) -> None:
        """Announce media on the satellite.

        Should block until the announcement is done playing.
        """
        await self._do_announce(announcement, run_pipeline_after=False)

    @convert_api_error_ha_error
    async def async_start_conversation(
        self, start_announcement: assist_satellite.AssistSatelliteAnnouncement
    ) -> None:
        """Start a conversation from the satellite."""
        await self._do_announce(start_announcement, run_pipeline_after=True)

    async def _do_announce(
        self,
        announcement: assist_satellite.AssistSatelliteAnnouncement,
        run_pipeline_after: bool,
    ) -> None:
        """Announce media on the satellite.

        Optionally run a voice pipeline after the announcement has finished.
        """
        _LOGGER.debug(
            "Waiting for announcement to finished (message=%s, media_id=%s)",
            announcement.message,
            announcement.media_id,
        )
        media_id = announcement.media_id
        is_media_tts = announcement.media_id_source == "tts"
        preannounce_media_id = announcement.preannounce_media_id
        if (not is_media_tts) or preannounce_media_id:
            # Route media through the proxy
            format_to_use: MediaPlayerSupportedFormat | None = None
            for supported_format in chain(
                *self._entry_data.media_player_formats.values()
            ):
                if supported_format.purpose == MediaPlayerFormatPurpose.ANNOUNCEMENT:
                    format_to_use = supported_format
                    break

            if format_to_use is not None:
                assert (self.registry_entry is not None) and (
                    self.registry_entry.device_id is not None
                )

                make_proxy_url = partial(
                    async_create_proxy_url,
                    hass=self.hass,
                    device_id=self.registry_entry.device_id,
                    media_format=format_to_use.format,
                    rate=format_to_use.sample_rate or None,
                    channels=format_to_use.num_channels or None,
                    width=format_to_use.sample_bytes or None,
                )

                if not is_media_tts:
                    media_id = async_process_play_media_url(
                        self.hass, make_proxy_url(media_url=media_id)
                    )

                if preannounce_media_id:
                    preannounce_media_id = async_process_play_media_url(
                        self.hass, make_proxy_url(media_url=preannounce_media_id)
                    )

        await self.cli.send_voice_assistant_announcement_await_response(
            media_id,
            _ANNOUNCEMENT_TIMEOUT_SEC,
            announcement.message,
            start_conversation=run_pipeline_after,
            preannounce_media_id=preannounce_media_id or "",
        )

    def _pick_wake_word_pipeline_index(self, wake_word_phrase: str | None) -> int:
        """按唤醒词挑激活的 pipeline 索引（0=默认）。

        v1.0.43 挂死根治：本方法替换 handle_pipeline_start 里的内联 while 环。
        旧内联版在「select 实体在注册表但无状态」（用户禁用该 CONFIG 实体，或
        HA 重启窗口 select 尚未 added）时走 `continue` 而**不推进索引**——
        get_wake_word_entity 对同一 index 恒返回同一 entity_id，while True 即
        成死循环，且它在 `while not queue.empty(): await` 之前没有任何挂起点
        → **整个 HA 事件循环冻结**：VoiceAssistantResponse 永不发出（设备侧
        "HA did not answer Request in time"×2 → 强拆连接），client 连断开都
        读不到、更不重连（设备侧 "No API client …" 久无回连）——正是现场
        "看得见连接、听不见回答、永远连不回"的签名。上游（official esphome）
        语义是**每轮无条件推进**；本实现照抄并另加硬上限，注册表怎么烂都挂不了。
        """
        index = 0
        while index < _MAX_WAKE_WORD_SELECTS:
            ww_entity_id = self.get_wake_word_entity(index)
            if not ww_entity_id:
                break
            ww_state = self.hass.states.get(ww_entity_id)
            if ww_state is not None and ww_state.state == wake_word_phrase:
                # First match
                return index
            # Try next wake word select
            index += 1
        return 0

    async def handle_pipeline_start(
        self,
        conversation_id: str,
        flags: int,
        audio_settings: VoiceAssistantAudioSettings,
        wake_word_phrase: str | None,
    ) -> int | None:
        """Handle pipeline run request.

        v1.0.43 外层异常兜底：aioesphomeapi 只在 start 回调**正常完成**时才回
        VoiceAssistantResponse——回调抛异常 = 设备侧 8s 黑屏「HA did not answer
        Request」，与事件循环挂死在现场不可区分。返回 None 让 client 回
        error=True → 设备走 failed_to_start 拿到显式拒绝（不计半开熔断），
        异常整栈落 HA 日志直接可归因。
        """
        try:
            return await self._handle_pipeline_start_impl(
                conversation_id, flags, audio_settings, wake_word_phrase
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception(
                "慧尖卫星 pipeline 启动异常 → 已向设备回 error（conversation_id=%s "
                "wake_word=%r）",
                conversation_id,
                wake_word_phrase,
            )
            return None

    async def _handle_pipeline_start_impl(
        self,
        conversation_id: str,
        flags: int,
        audio_settings: VoiceAssistantAudioSettings,
        wake_word_phrase: str | None,
    ) -> int | None:
        """Handle pipeline run request."""
        # Clear audio queue
        self._stream_end_pending = False   # v1.0.41 审查 S4：新一轮流，清上轮兜底标记
        while not self._audio_queue.empty():
            await self._audio_queue.get()

        if self._tts_streaming_task is not None:
            # Cancel current TTS response
            self._tts_streaming_task.cancel()
            self._tts_streaming_task = None

        # API or UDP output audio
        port: int = 0
        assert self._entry_data.device_info is not None
        feature_flags = (
            self._entry_data.device_info.voice_assistant_feature_flags_compat(
                self._entry_data.api_version
            )
        )
        if (feature_flags & VoiceAssistantFeature.SPEAKER) and not (
            feature_flags & VoiceAssistantFeature.API_AUDIO
        ):
            port = await self._start_udp_server()
            _LOGGER.debug("Started UDP server on port %s", port)

        # Device triggered pipeline (wake word, etc.)
        if flags & VoiceAssistantCommandFlag.USE_WAKE_WORD:
            start_stage = PipelineStage.WAKE_WORD
        else:
            start_stage = PipelineStage.STT

        end_stage = PipelineStage.TTS

        if feature_flags & (
            VoiceAssistantFeature.SPEAKER | VoiceAssistantFeature.API_AUDIO
        ):
            # v1.0.19：API_AUDIO 设备同样经 API 推 16k WAV（见 on_pipeline_event
            # 流式门控注释）——须向 TTS 引擎声明 wav 首选，否则慧尖 tts 回 mp3
            # 在 _stream_tts_audio「Only WAV」早退。
            # Stream WAV audio
            self._attr_tts_options = {
                tts.ATTR_PREFERRED_FORMAT: "wav",
                tts.ATTR_PREFERRED_SAMPLE_RATE: 16000,
                tts.ATTR_PREFERRED_SAMPLE_CHANNELS: 1,
                tts.ATTR_PREFERRED_SAMPLE_BYTES: 2,
            }
        else:
            # ANNOUNCEMENT format from media player
            self._update_tts_format()

        # Run the appropriate pipeline. v1.0.43：挑索引逻辑移入
        # _pick_wake_word_pipeline_index（原内联环在实体无状态时不推进索引
        # →冻结事件循环，见该方法 docstring）。
        self._active_pipeline_index = self._pick_wake_word_pipeline_index(
            wake_word_phrase
        )

        _LOGGER.debug(
            "Running pipeline %s from %s to %s",
            self._active_pipeline_index + 1,
            start_stage,
            end_stage,
        )
        self._pipeline_task = self.config_entry.async_create_background_task(
            self.hass,
            self.async_accept_pipeline_from_satellite(
                audio_stream=self._wrap_audio_stream(),
                start_stage=start_stage,
                end_stage=end_stage,
                wake_word_phrase=wake_word_phrase,
            ),
            "esphome_assist_satellite_pipeline",
        )
        self._pipeline_task.add_done_callback(self.handle_pipeline_finished)

        return port

    async def handle_audio(self, data: bytes, data2: bytes | None = None) -> None:
        """Handle incoming audio chunk from API.

        v1.0.19：对齐 aioesphomeapi≥45 双参调用 handle_audio(data, data2)
        （45.13.1 起实证；旧单参签名在 HA 2026.6+ 每帧 TypeError→上行全断，
        台架×固件协议审计实锤）。data2=增强音频第二通道，本固件只发单声道
        取偶通道后的流（data2 恒 None），收到即忽略；保留参数以兼容双通道设备。
        """
        if _queue_audio_chunk(self._audio_queue, data):
            self._audio_dropped_chunks += 1
            if (self._audio_dropped_chunks == 1
                    or self._audio_dropped_chunks % _AUDIO_DROP_LOG_EVERY == 0):
                _LOGGER.warning(
                    "上行音频队列已满（上限 %d 块），丢最旧保最新——管线消费停顿"
                    "（HA 事件循环被占 / STT 慢）；累计丢弃 %d 块",
                    _MAX_AUDIO_QUEUE_CHUNKS, self._audio_dropped_chunks,
                )

    async def handle_pipeline_stop(self, abort: bool) -> None:
        """Handle request for pipeline to stop."""
        if abort:
            self._abort_pipeline()
        else:
            self._stop_pipeline()

    def handle_pipeline_finished(self, task: asyncio.Task | None = None) -> None:
        """Handle when pipeline has finished running.

        v1.0.49：只允许"当前这一轮"结束时复位状态。本方法是 _pipeline_task 的
        done-callback，而 barge-in 的时序是"cancel 旧轮 → 250ms 后开新轮"：
        老任务随后完成时会走到这里，旧实现**无条件**把 _active_pipeline_index
        归零，而新一轮已在 _handle_pipeline_start_impl 里按唤醒词选好了索引
        （:653）——归零后新一轮会落到默认管道（唤醒词→管道映射静默失效）。
        现在用"完成的就是当前任务"作判据，陈旧回调只留一行 debug。
        """
        if task is not None and task is not self._pipeline_task:
            _LOGGER.debug("Stale pipeline task finished; leaving state untouched")
            return
        self._stop_udp_server()
        self._active_pipeline_index = 0
        _LOGGER.debug("Pipeline finished")

    def handle_timer_event(
        self, event_type: TimerEventType, timer_info: TimerInfo
    ) -> None:
        """Handle timer events."""
        try:
            native_event_type = _TIMER_EVENT_TYPES.from_hass(event_type)
        except KeyError:
            _LOGGER.debug("Received unknown timer event type: %s", event_type)
            return

        self.cli.send_voice_assistant_timer_event(
            native_event_type,
            timer_info.id,
            timer_info.name,
            timer_info.created_seconds,
            timer_info.seconds_left,
            timer_info.is_active,
        )

    async def handle_announcement_finished(
        self, announce_finished: VoiceAssistantAnnounceFinished
    ) -> None:
        """Handle announcement finished message (also sent for TTS)."""
        self.tts_response_finished()

    @callback
    def async_set_wake_words(self, wake_word_ids: list[str]) -> None:
        """Set active wake words and update config on satellite."""
        self._satellite_config.active_wake_words = wake_word_ids
        self.config_entry.async_create_background_task(
            self.hass,
            self.async_set_configuration(self._satellite_config),
            "esphome_voice_assistant_set_config",
        )
        _LOGGER.debug("Setting active wake word(s): %s", wake_word_ids)

    def _update_tts_format(self) -> None:
        """Update the TTS format from the first media player."""
        for supported_format in chain(*self._entry_data.media_player_formats.values()):
            # Find first announcement format
            if supported_format.purpose == MediaPlayerFormatPurpose.ANNOUNCEMENT:
                self._attr_tts_options = {
                    tts.ATTR_PREFERRED_FORMAT: supported_format.format,
                }

                if supported_format.sample_rate > 0:
                    self._attr_tts_options[tts.ATTR_PREFERRED_SAMPLE_RATE] = (
                        supported_format.sample_rate
                    )

                if supported_format.num_channels > 0:
                    self._attr_tts_options[tts.ATTR_PREFERRED_SAMPLE_CHANNELS] = (
                        supported_format.num_channels
                    )

                if supported_format.sample_bytes > 0:
                    self._attr_tts_options[tts.ATTR_PREFERRED_SAMPLE_BYTES] = (
                        supported_format.sample_bytes
                    )

                break

    async def _stream_tts_audio(
        self,
        tts_result: tts.ResultStream,
        sample_rate: int = 16000,
        sample_width: int = 2,
        sample_channels: int = 1,
        samples_per_chunk: int = 512,
    ) -> None:
        """Stream TTS audio chunks to device via API or UDP."""
        self.cli.send_voice_assistant_event(
            VoiceAssistantEventType.VOICE_ASSISTANT_TTS_STREAM_START, {}
        )

        try:
            if not self._is_running:
                return

            if tts_result.extension != "wav":
                _LOGGER.error(
                    "Only WAV audio can be streamed, got %s", tts_result.extension
                )
                return

            data = b"".join([chunk async for chunk in tts_result.async_stream_result()])

            with io.BytesIO(data) as wav_io, wave.open(wav_io, "rb") as wav_file:
                if (
                    (wav_file.getframerate() != sample_rate)
                    or (wav_file.getsampwidth() != sample_width)
                    or (wav_file.getnchannels() != sample_channels)
                ):
                    _LOGGER.error("Can only stream 16Khz 16-bit mono WAV")
                    return

                frames = wav_file.getnframes()
                if frames <= 0:
                    # v1.0.25 fail-loud：0 帧 WAV 会让设备端「起流即收流」——
                    # 灯照常执行、播报全哑，此前这里只有 debug 行，排查无从下手。
                    _LOGGER.warning(
                        "[TTS] 音频 0 帧（%d 字节 WAV），设备将静音", len(data)
                    )
                else:
                    _LOGGER.info(
                        "[TTS] 推流 %d 帧 %.2fs", frames, frames / sample_rate
                    )

                while self._is_running:
                    chunk = wav_file.readframes(samples_per_chunk)
                    if not chunk:
                        break

                    if self._udp_server is not None:
                        self._udp_server.send_audio_bytes(chunk)
                    else:
                        self.cli.send_voice_assistant_audio(chunk)

                    # Wait for 90% of the duration of the audio that was
                    # sent for it to be played.  This will overrun the
                    # device's buffer for very long audio, so using a media
                    # player is preferred.
                    samples_in_chunk = len(chunk) // (sample_width * sample_channels)
                    seconds_in_chunk = samples_in_chunk / sample_rate
                    await asyncio.sleep(seconds_in_chunk * 0.9)
        except asyncio.CancelledError:
            return  # Don't trigger state change
        finally:
            self.cli.send_voice_assistant_event(
                VoiceAssistantEventType.VOICE_ASSISTANT_TTS_STREAM_END, {}
            )

        # State change
        self.tts_response_finished()
        self._entry_data.async_set_assist_pipeline_state(False)

    async def _wrap_audio_stream(self) -> AsyncIterable[bytes]:
        """Yield audio chunks from the queue until None."""
        while True:
            try:
                chunk = await asyncio.wait_for(
                    self._audio_queue.get(), _STREAM_END_POLL_S)
            except asyncio.TimeoutError:
                # v1.0.41 审查 S4：「哨兵绝不丢」只在入队瞬间成立——哨兵已入队后
                # 若消费者（管线）停摆且又灌入 ≥160 个数据块，哨兵自己会作为最旧
                # 被踢出（探针实证），`get()` 将永挂：僵尸 pipeline task 与下一轮
                # 唤醒管线交替分食同一队列（音频劈裂→STT 乱码）。收尾判据：
                # 已请求停止/中止（pending）且队列排空 ≡ 哨兵要表达的"数据终"，
                # 等价收束，多付一个 poll 周期的延迟。
                if self._stream_end_pending and self._audio_queue.empty():
                    self._stream_end_pending = False
                    break
                continue
            if not chunk:
                break

            yield chunk

    def _stop_pipeline(self) -> None:
        """Request pipeline to be stopped by ending the audio stream and continue processing."""
        _queue_audio_chunk(self._audio_queue, None)   # 哨兵保入队（满则腾最旧）
        self._stream_end_pending = True               # v1.0.41 审查 S4：哨兵可能被挤丢的兜底标记
        _LOGGER.debug("Requested pipeline stop")

    def _abort_pipeline(self) -> None:
        """Request pipeline to be aborted (no further processing)."""
        _LOGGER.debug("Requested pipeline abort")
        _queue_audio_chunk(self._audio_queue, None)   # 哨兵保入队（满则腾最旧）
        self._stream_end_pending = True               # v1.0.41 审查 S4：同上
        # v1.0.49：abort 必须同时掐掉 TTS 下行推流。旧实现只 cancel _pipeline_task，
        # 而 _stream_tts_audio 的循环判据是 `while self._is_running`，只有实体被移除
        # 才置 False——于是 abort 之后推流照旧按 28.8ms/帧 往设备灌 32ms 音频，
        # 直到"下一次 start"才在 :606-609 被取消。现场实锤（固件 v2.1.28 留痕）：
        # 设备 abort 回收后仍收到 20+ 帧下行，只能逐帧
        # `Discarding %u downlink bytes in state IDLE`。取消后 _stream_tts_audio 的
        # finally 会补发 TTS_STREAM_END，两端对"这条流结束了"的认知因此一致。
        if self._tts_streaming_task is not None:
            self._tts_streaming_task.cancel()
            self._tts_streaming_task = None
        if self._pipeline_task is not None:
            self._pipeline_task.cancel()

    async def _start_udp_server(self) -> int:
        """Start a UDP server on a random free port."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.bind(("", 0))  # random free port

        (
            _transport,
            protocol,
        ) = await asyncio.get_running_loop().create_datagram_endpoint(
            partial(VoiceAssistantUDPServer, self._audio_queue), sock=sock
        )

        assert isinstance(protocol, VoiceAssistantUDPServer)
        self._udp_server = protocol

        # Return port
        return cast(int, sock.getsockname()[1])

    def _stop_udp_server(self) -> None:
        """Stop the UDP server if it's running."""
        if self._udp_server is None:
            return

        try:
            self._udp_server.close()
        finally:
            self._udp_server = None

        _LOGGER.debug("Stopped UDP server")


class VoiceAssistantUDPServer(asyncio.DatagramProtocol):
    """Receive UDP packets and forward them to the audio queue."""

    transport: asyncio.DatagramTransport | None = None
    remote_addr: tuple[str, int] | None = None

    def __init__(
        self, audio_queue: asyncio.Queue[bytes | None], *args: Any, **kwargs: Any
    ) -> None:
        """Initialize protocol."""
        super().__init__(*args, **kwargs)
        self._audio_queue = audio_queue
        self._audio_dropped_chunks: int = 0    # v1.0.40：有界队列丢包计数

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Store transport for later use."""
        self.transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle incoming UDP packet."""
        if self.remote_addr is None:
            self.remote_addr = addr

        # v1.0.40：与 API 通道同规——有界队列、丢最旧，防 UDP 侧无界涨
        if _queue_audio_chunk(self._audio_queue, data):
            self._audio_dropped_chunks += 1
            if (self._audio_dropped_chunks == 1
                    or self._audio_dropped_chunks % _AUDIO_DROP_LOG_EVERY == 0):
                _LOGGER.warning(
                    "UDP 上行音频队列已满（上限 %d 块），丢最旧保最新；累计丢弃 %d 块",
                    _MAX_AUDIO_QUEUE_CHUNKS, self._audio_dropped_chunks,
                )

    def error_received(self, exc: Exception) -> None:
        """Handle when a send or receive operation raises an OSError.

        (Other than BlockingIOError or InterruptedError.)
        """
        _LOGGER.error("ESPHome Voice Assistant UDP server error received: %s", exc)

        # Stop pipeline
        _queue_audio_chunk(self._audio_queue, None)   # 哨兵保入队（满则腾最旧）

    def close(self) -> None:
        """Close the receiver."""
        if self.transport is not None:
            self.transport.close()

        self.remote_addr = None

    def send_audio_bytes(self, data: bytes) -> None:
        """Send bytes to the device via UDP."""
        if self.transport is None:
            _LOGGER.error("No transport to send audio to")
            return

        if self.remote_addr is None:
            _LOGGER.error("No address to send audio to")
            return

        self.transport.sendto(data, self.remote_addr)


async def async_get_custom_wake_words(
    hass: HomeAssistant,
) -> dict[str, VoiceAssistantExternalWakeWord]:
    """Get available custom wake words."""
    return await hass.async_add_executor_job(_get_custom_wake_words, hass)


@singleton(_DATA_WAKE_WORDS)
def _get_custom_wake_words(
    hass: HomeAssistant,
) -> dict[str, VoiceAssistantExternalWakeWord]:
    """Get available custom wake words (singleton)."""
    wake_words_dir = Path(hass.config.path(WAKE_WORDS_DIR_NAME))
    wake_words: dict[str, VoiceAssistantExternalWakeWord] = {}

    # Look for config/model files
    for config_path in wake_words_dir.glob("*.json"):
        wake_word_id = config_path.stem
        model_path = config_path.with_suffix(".tflite")
        if not model_path.exists():
            # Missing model file
            continue

        with open(config_path, encoding="utf-8") as config_file:
            config_dict = json.load(config_file)
            try:
                config = _WAKE_WORD_CONFIG_SCHEMA(config_dict)
            except vol.Invalid as err:
                # Invalid config
                _LOGGER.debug(
                    "Invalid wake word config: path=%s, error=%s",
                    config_path,
                    humanize_error(config_dict, err),
                )
                continue

            with open(model_path, "rb") as model_file:
                model_hash = hashlib.sha256(model_file.read()).hexdigest()

            model_size = model_path.stat().st_size
            config_rel_path = config_path.relative_to(wake_words_dir)

            # Only intended for the internal network
            base_url = get_url(hass, prefer_external=False, allow_cloud=False)

            wake_words[wake_word_id] = VoiceAssistantExternalWakeWord.from_dict(
                {
                    "id": wake_word_id,
                    "wake_word": config["wake_word"],
                    "trained_languages": config_dict.get("trained_languages", []),
                    "model_type": config["type"],
                    "model_size": model_size,
                    "model_hash": model_hash,
                    "url": f"{base_url}{WAKE_WORDS_API_PATH}/{config_rel_path}",
                }
            )

    return wake_words


async def async_setup(hass: HomeAssistant) -> None:
    """Set up the satellite."""
    wake_words_dir = Path(hass.config.path(WAKE_WORDS_DIR_NAME))

    # Satellites will pull model files over HTTP
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                url_path=WAKE_WORDS_API_PATH,
                path=str(wake_words_dir),
            )
        ]
    )
