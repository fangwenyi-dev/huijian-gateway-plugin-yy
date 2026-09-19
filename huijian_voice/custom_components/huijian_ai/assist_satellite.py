"""Support for assist satellites in ESPHome."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import socket
from collections.abc import AsyncGenerator, AsyncIterable
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
from . import end_dialogue
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

# v1.0.86：僵尸轮 TTS 防护窗（仅在 _drain_stale_pipeline 超时时 arm）。
# 8s ≈ 一轮 STT+intent 的最短合法尾——晚于此的僵尸 TTS 已被新轮开场洗掉
# 设备端状态（v2.1.32 免疫窗/世代闸兜后续），不再需要 HA 侧拦截。
_ZOMBIE_TTS_GUARD_S = 8.0

# v1.0.88（下行流所有权）：被接管旧流的残帧丢弃告警限频（与固件 v2.1.28
# 同族纪律一致：成串丢弃不许淹没同刻其它信号）。
_DL_DROP_LOG_EVERY = 20

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


# ── v1.0.52：TTS 下行真流式（对齐上游 HA 的 stream_wav 语义，自包含实现）──────
# 现场（2026-09-21）：设备在 TTS_STREAM_START 之后静音 31 秒才出声。根因是本文件
# 里的 `data = b"".join([...])` 把整段 WAV 收完才按固定 0.9 倍速发声——上游任何
# 合成延迟被 1:1 放大成设备静音。上游 HA 2026.8 早已改为 stream_wav(...)：
# 边走边解 WAV 头、固定 512 样本块、按"设备环形缓冲水位"背压。本实现对齐其语义，
# 但不依赖 HA 内部 helper（向下兼容老 HA），并把我们自己的 fail-loud（非 WAV 早退 /
# 形态不符报错 / 0 帧告警）保留在流式路径上。
#
# 设备侧水位事实（固件 audio_service.h）：MAX_PLAYBACK_TASKS_IN_QUEUE=40 块 × 32ms
# ≈ 1.28s，满则丢最旧并计数。沿用上游"目标 75% 水位"口径取 384ms（比设备容量更
# 保守 → 抗欠载；代价是约 0.4s 预缓冲）。
# 2026-09-16 跨仓事实更新（固件 v2.1.51 批）：该 40 块已扩到 64 块（≈2.048s），
# 且固件下行改为一拍最多读 10 包（旧"一拍 1 包"把长播报摊成 0.22× 到达，就是
# "一句话分好几次说完"的真凶）。**水位分流（F3）见本块下方 v1.0.89 段**：对存量
# ≤v2.1.50 设备（容量仍 1.28s）超发会撞它"满则丢最旧"=「缺头/丢头」症状族，所以
# 抬水位必须按 project_version 分流，绝不一刀切。判据回读：设备侧 `TTS stream end`
# 时刻 − `Downlink audio start` 应≈音频时长；仍远大于时长再看本行推流收口的
# 「速率 X.XX×」与「水位 X.XXXs」（v1.0.89 起一并打印，用于对账分流是否落地）。
_DEVICE_BUFFER_TARGET_S = 0.384

# ── v1.0.89（F3）：按设备固件能力分流的下行预灌水位 ──────────────────────────
# 0.384s 不是经验值，而是**上游 512ms 环缓冲 × 75% = 12 块 × 32ms** 继承下来的
# 字面值；落到本板（固件 audio_service.h 的 MAX_PLAYBACK_TASKS_IN_QUEUE=40 块
# ×32ms = 1.28s）只等于 30%——刻意保守，代价就是"一慢就饿"。
# 固件 v2.1.51 把该队列扩到 64 块（= 2.048s）并把下行 ingest 从"每拍 1 包"改成
# "每拍 ≤10 包"（真机实测：7.02s 音频到达跨度从 0.22× 抬到 ≈1.1×），于是同一
# "75% 容量"口径的新水位＝0.75 × 2.048s ＝ **1.536s**。
# 为什么必须分流、不能一刀切抬：对存量 ≤v2.1.50 设备（容量仍 1.28s）超发到 1.5s 会
# 撞它"满则丢最旧"=「缺头/丢头」症状族，比现在的句间停顿更难查。判据源＝设备自己经
# DeviceInfoResponse 上报的 project_version（v2.1.37 起随 PROJECT_VER 编译进 bin，
# 集成 manager.py 已拿它拼 sw_version）。
# 铁律：方向恒为 **fail-open**——device_info 不在、project_version 缺失/畸形、版本
# 低于门槛，一律回 0.384s（＝今天的行为），绝不因"读不到版本"把播报做成缺头。
_DEVICE_BUFFER_TARGET_S_V2151 = 1.536
_PREBUF_FW_MIN = (2, 1, 51)   # 具备 64 块队列 + 10 包/拍批读的最低固件版本

_RE_PROJECT_VER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def _parse_project_version(raw: object) -> tuple[int, int, int] | None:
    """'2.1.51' / '2.1.51 (…)' / 'v2.1.51' → (2, 1, 51)；解析不出 → None。

    只认 `x.y.z` 数字前缀，后缀（beta/编译标记）忽略；元组比较天然对 2.10.0 这类
    位权正确，绝不做字符串大小比较。"""
    m = _RE_PROJECT_VER.match(str(raw or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _prebuf_target_for_version(raw: object) -> float:
    """project_version → 本设备的预灌水位（秒）；**未知一律旧行为**。

    纯函数=可钉可测：真值表见 tests/test_v1089_downlink_prebuf.py。"""
    ver = _parse_project_version(raw)
    if ver is not None and ver >= _PREBUF_FW_MIN:
        return _DEVICE_BUFFER_TARGET_S_V2151
    return _DEVICE_BUFFER_TARGET_S


def _announce_gate(*, api_audio: bool, has_message: bool, preannounce: bool,
                   pipeline_busy: bool) -> tuple[bool, str]:
    """v1.0.96（播报 0 字节案）：播报是否走「文本自合成推流」的纯决策函数。

    旧形态是一串布尔条件——任一不满足就**静默**走旧 URL 形态，而 API 音频卫星的
    旧形态＝设备干等首包到 90s 封顶拆流，全程没有一行日志说卡在哪道门（VM 台架
    案查因三日取不到日志）。现：每道门具名，不走必返 skip 理由由调用方 WARN 打出；
    真喇叭设备（api_audio=False）返回 ("",) 不产日志噪音，旧路行为一字不变。
    纯函数=可钉可测（tests/test_v1096_announce_gate.py exec 真值表）。"""
    if not api_audio:
        return False, ""
    if not has_message:
        return False, "无message(API音频设备无自取media URL能力)"
    if preannounce:
        return False, "preannounce前置音"
    if pipeline_busy:
        return False, "撞活跃轮(护栏②不抢下行)"
    return True, ""


def _api_audio_form(flags: int) -> bool:
    """设备形态判定：本卫星是否属「API 音频推流」形态（announce 接管闸的输入）。

    v1.0.98 根修（2026-09-19 VM+COM27 实锤链，播报三日 0 字节案真凶）：旧判据
    `API_AUDIO and not SPEAKER` 假设慧尖板恒只报 API_AUDIO——但固件 **v2.1.12
    起能力宣告补了 FEATURE_SPEAKER**（voice_assistant.h get_feature_flags()，
    当日为给对话应答那条 `SPEAKER|API_AUDIO` 任一即推流的门控放行）。并存形态
    下旧判据=False → `_announce_gate` 返回 (False, "") 具名分因为空串=**按设计
    静默** → `_do_announce` 一字不动作，设备端 on_announce 又"只记日志不抓取"
    ——两端互等对方干活=Announce 送达、下行 0 bytes、90s 拆流、全程零日志。
    判据改为 **API_AUDIO 位即接管**：
      · 慧尖板（2.1.11 起，2.1.12 后双标并存）→ True，推流自合成接管；
      · SPEAKER-only 真喇叭（官方 media_player 自取 URL 形态，无 API_AUDIO）
        → False，旧 URL 自取路一字不动；
      · 无任何音频位 → False（同上不接管）。
    与对话腿（:713 任一即推流）和固件 v2.1.12 注释「SPEAKER&&API_AUDIO 并存时
    HA 仍走 API 推」口径对齐——announce 腿此前独走相反假设。"""
    return bool(flags & VoiceAssistantFeature.API_AUDIO)


def _preannounce_rescued(*, api_audio: bool, has_message: bool, skip: str) -> bool:
    """v1.0.99（0 字节案第二层，2026-09-19 部署后 WARN 点名）：前置音不得拖死正文。

    core 2026.9 `assist_satellite/services.py`：announce 的 `preannounce` **默认
    True**——凡带 message 的 announce，core handler 一律注入 PREANNOUNCE_URL
    提示音 → `_announce_gate` 护栏③整单拒接管。对真喇叭设备这只是回退 URL 自取
    （正常）；对 API 音频板（慧尖板，无 media 自取能力）= 正文明明能走已实战的
    自合成推流，却被一声放不出来的「叮~」陪葬 90s 0 字节（v1.0.98 部署当晚
    WARN「preannounce前置音」当场抓获——api_audio 判据修通后露出的下一道门）。
    判据：**API 音频形态 + 分因正是 preannounce + 有正文** ⇒ 救援=弃前置音、
    正文照常接管。其余分因（无 message/撞活跃轮）与 SPEAKER-only 形态一概不救，
    v1.0.96 语义不动。"""
    return bool(api_audio and has_message and skip.startswith("preannounce"))

#: WAV 头最大攒量：坏流兜底（超过即判协议异常，绝不无限攒内存）
_MAX_WAV_HEADER_BYTES = 64 * 1024


def _parse_wav_header(buf: bytes | bytearray, expected: tuple[int, int, int]) -> int | None:
    """解析 WAVE 容器直到 `data` 块，返回载荷起始偏移。

    None = 还需更多字节（调用方继续累积）；形态不符 → ValueError（fail-loud）。
    任意分块边界都成立——这是"边收边解"的前提。
    """
    if len(buf) < 12:
        return None
    if bytes(buf[0:4]) != b"RIFF" or bytes(buf[8:12]) != b"WAVE":
        raise ValueError("不是 RIFF/WAVE 容器")
    exp_rate, exp_width, exp_channels = expected
    pos = 12
    fmt_seen = False
    while True:
        if len(buf) < pos + 8:
            return None
        chunk_id = bytes(buf[pos:pos + 4])
        chunk_size = int.from_bytes(buf[pos + 4:pos + 8], "little")
        body = pos + 8
        if chunk_id == b"fmt ":
            if len(buf) < body + 16:
                return None
            audio_format = int.from_bytes(buf[body:body + 2], "little")
            channels = int.from_bytes(buf[body + 2:body + 4], "little")
            rate = int.from_bytes(buf[body + 4:body + 8], "little")
            bits = int.from_bytes(buf[body + 14:body + 16], "little")
            if audio_format not in (1, 0xFFFE):
                raise ValueError("非 PCM WAV（audio_format=%d）" % audio_format)
            if (rate, bits // 8, channels) != (exp_rate, exp_width, exp_channels):
                raise ValueError(
                    "只支持 %dHz/%dbit/%dch WAV，收到 %dHz/%dbit/%dch"
                    % (exp_rate, exp_width * 8, exp_channels, rate, bits, channels)
                )
            fmt_seen = True
        elif chunk_id == b"data":
            if not fmt_seen:
                raise ValueError("WAVE data 块先于 fmt 块")
            return body
        # 跳过本块（块体按偶数字节对齐），必要时等更多字节
        pos = body + chunk_size + (chunk_size & 1)
        if len(buf) < pos:
            return None


async def _iter_wav_pcm_chunks(
    chunks: AsyncIterable[bytes],
    *,
    sample_rate: int,
    sample_width: int,
    sample_channels: int,
    samples_per_chunk: int,
) -> AsyncGenerator[tuple[bytes, bool], None]:
    """增量解析 WAV 并切块：边收边出 (pcm_chunk, is_last)。

    - 头未就绪前只攒不发；`data` 块出现即开始出块（首音 = 首块时间，与总时长无关）；
    - `data` 声明长度用尽即收尾（忽略容器的填充/尾部字节），末块带 is_last=True；
      声明长度不可知（0/0xFFFFFFFF）时按源结束收尾，同样给末块标 is_last；
      消费端按 is_last 或迭代结束任一条件收尾均可。
    """
    block_align = sample_width * sample_channels
    want = samples_per_chunk * block_align
    buf = bytearray()
    payload_ready = False
    remaining: int | None = None
    async for piece in chunks:
        if not piece:
            continue
        buf += piece
        if not payload_ready:
            offset = _parse_wav_header(buf, (sample_rate, sample_width, sample_channels))
            if offset is None:
                if len(buf) > _MAX_WAV_HEADER_BYTES:
                    raise ValueError("WAV 头异常（%d 字节仍未见到 data 块）" % len(buf))
                continue
            declared = int.from_bytes(bytes(buf[offset - 4:offset]), "little")
            remaining = declared if 0 < declared < 0xFFFFFFFF else None
            del buf[:offset]
            payload_ready = True
        while True:
            if remaining is None:
                if len(buf) < want:
                    break
                take = want
            else:
                if remaining <= 0:
                    return
                # data 声明长度是硬上限：尾块只能出声明内的整样本
                take = min(want, remaining)
                take -= take % block_align
                if take <= 0:
                    # 声明长度不足一个样本（异常容器）→ 视为结束，绝不吐多余字节
                    return
                if len(buf) < take:
                    break
            chunk = bytes(buf[:take])
            del buf[:take]
            if remaining is not None:
                remaining -= len(chunk)
                if remaining <= 0:
                    # 声明长度用尽：末块直接带 is_last（上游同语义），
                    # 消费端据此省掉最后一次背压等待（最多 0.384s）。
                    yield chunk, True
                    return
            yield chunk, False
        if remaining is not None and remaining <= 0:
            return
    if not payload_ready:
        raise ValueError("WAV 流在头部完成前结束")
    # 收尾尾巴同样受 data 声明长度约束（容器可能带填充字节）
    tail = len(buf) if remaining is None else min(len(buf), remaining)
    tail -= tail % block_align
    if tail:
        yield bytes(buf[:tail]), True


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
        # v1.0.83（#2/#3 基座）：外层轮任务（_run_pipeline_round）。core 的
        # async_accept_pipeline_from_satellite 会把 _pipeline_task **重绑**为它
        # 自己的内层 run 任务（entity.py:505），于是本文件写入的外层身份失绑——
        # done-callback 的"完成的就是当前任务"判据（v1.0.49）在内层重绑下永远
        # 失配（合法完成也走 stale 分支），且旧 run 尸事件甄别需要同时认两把
        # 身份。外层句柄单独记账，事件甄别/_pipeline_finished 复位共用。
        self._round_outer_task: asyncio.Task | None = None
        # v1.0.73 归因链：拆轮者要能报出被拆轮的年龄（单调时钟，仅日志用）
        self._pipeline_task_t0: float = 0.0
        # v1.0.40：有界（≈5s 语音）——消费停顿时丢最旧，绝不无界涨内存
        self._audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=_MAX_AUDIO_QUEUE_CHUNKS
        )
        self._stream_end_pending = False   # v1.0.41 审查 S4：哨兵兜底标记（随新流清零）
        # v1.0.86：僵尸轮 TTS 防护窗截止（仅 drain 超时 arm，见常量注释）
        self._zombie_tts_guard_until: float = 0.0
        # v1.0.87：窗的**主判据**换成"被 drain 掉的那一轮还活着"——晚到的
        # TTS_END 只可能由仍在跑的旧轮发出；旧轮一旦收口，窗即刻失效，新轮
        # 自己合法的 TTS 就不可能再被时间窗误杀（1.0.86 的 8s 纯时间窗正是
        # 这么丢的）。上面的 until 只留作硬上限兜底（旧轮永不收口的极端形态）。
        self._zombie_tts_guard_task: "asyncio.Task | None" = None
        self._audio_dropped_chunks: int = 0
        # v1.0.87：无消费者（未开轮/本轮已收口）时直投丢弃的计数，独立于队列
        # 溢出计数——两者归因完全不同（前者=设备无开轮推流，后者=消费停顿）
        self._audio_orphan_chunks: int = 0
        self._tts_streaming_task: asyncio.Task | None = None
        # ── v1.0.88：TTS 下行流所有权（Hop B「旧 run 晚到音频混入新流头部」根修）──
        # 设备侧世代 s_va_epoch 绑的是 play_reset 现值（固件 CHANGELOG v2.1.50
        # 未收口条），**认不清一帧属于 HA 哪一次推流**；而每条下行流自己的
        # START/帧/END 都由各自的后台任务发出，两个任务共用同一条 API 连接，
        # 谁先 enqueue 谁先上 wire。故旧 run 的帧完全可以落在新 run 的
        # TTS_STREAM_START 之后——设备刚 play_reset 换过世代，那批旧音频就以
        # "合法新帧"的形态进了新流头部（现场=播报开头混半句）。
        # 本集成是唯一向该设备写音频的实体，故由它 mint 所有权并把判定下沉到
        # 每帧 enqueue 之前（与 enqueue 之间零 await）。不变量：
        #   I-1 接管动作（吊销旧 seq + cancel）与新流 START 之间无 await
        #       ⇒ 旧任务此后 enqueue 的帧全部被本闸拦掉；已 enqueue 的因同连接
        #       FIFO 必在新 START 之前上 wire，被设备 play_reset 冲掉。
        #   I-2 只允许归属者向设备发 TTS_STREAM_END——旧流代发 END 会把新流
        #       当场收口（固件 :411-425 直接 STOP_PIPELINE），现场=新播报静音。
        #   I-3 只允许归属者落本轮状态收口（tts_response_finished/pipeline_state）
        #       ⇒ 旧任务不得替新流落 IDLE（那是另一型串扰）。
        self._dl_seq: int = 0             # 0 = 无人占有；每次建流 +1 吊销上一个
        self._dl_drop_total: int = 0      # 归属闸拦下的残帧累计（对账用）
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

        # v1.0.73 归因链·第②钉：订阅建立/解除各留一行 INFO——设备端
        # send_request/bounded-wait 的形态要与 HA 端订阅窗口对齐
        # （"设备以为订阅在、HA 其实刚重建"这类竞态全靠这两行卡时刻）。
        _LOGGER.info("慧尖卫星: VA 订阅建立 %s", self.entity_id)

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
        # v1.0.73 归因链·第②钉的孪生：订阅解除时刻（reload/摘除实体都会走这）
        _LOGGER.info("慧尖卫星: VA 订阅解除 %s", getattr(self, "entity_id", "?"))
        await super().async_will_remove_from_hass()

        self._is_running = False
        self._stop_pipeline()

    @callback
    def _zombie_tts_guard_active(self) -> bool:
        """v1.0.86：drain 超时（实锤旧轮还活着）后的窄防护窗是否生效。

        v1.0.83 曾用"事件任务身份"甄别旧轮事件并在 on_pipeline_event 顶部
        整扇丢弃——现场（2026-09-16 09:37 案）证明该前提在实际 core 的派发
        形态下不成立：健康轮 run-start→run-end 全序列被误杀=播报整轮消失，
        杀伤面远大于被保护的窄竞态。现收缩为：只有 _drain_stale_pipeline
        超时（WARN"半句播报请查此条"点名的真僵尸时刻）才 arm；窗内只拦
        TTS_END 建推流（僵尸灌进新轮的唯一实质damage），其余事件放行。
        """
        task = self._zombie_tts_guard_task
        if task is None:
            return False
        if task.done():
            # 旧轮已收口：晚到 TTS 的来源消失，窗即刻关（不等 8s 到期）——
            # 这就是与 1.0.86 的关键差别：新轮 TTS 不再被时间窗吃掉。
            self._zombie_tts_guard_task = None   # 只清身份即关窗（arm 点保持唯一）
            return False
        return asyncio.get_running_loop().time() < self._zombie_tts_guard_until

    @callback
    def _dl_takeover(self) -> int:
        """v1.0.88：新下行流接管本设备的 API 音频写权，返回其归属 seq。

        **调用点与新流 START 之间不得有 await**（不变量 I-1，见 __init__ 注释）：
        本方法同步吊销旧 seq 并取消旧任务，旧任务此后即使被唤醒也想 enqueue 帧，
        会被 _stream_tts_audio 的归属闸拦掉；它在吊销之前已 enqueue 的帧因同一条
        API 连接 FIFO 必在新 START 之前上 wire，被设备 play_reset 冲掉。
        旧任务被取消后由 I-2/I-3 保证它既不代发 TTS_STREAM_END（那会当场掐死
        新流）、也不替新流落状态收口。
        """
        self._dl_seq += 1
        old = self._tts_streaming_task
        if old is not None and not old.done():
            old.cancel()
            _LOGGER.info(
                "慧尖卫星: 下行流接管，旧推流已吊销（新 seq=%s，累计丢残帧 %d）",
                self._dl_seq, self._dl_drop_total,
            )
        return self._dl_seq

    def _clear_tts_streaming_task(self, task: asyncio.Task) -> None:
        """v1.0.83（修#3）：推流任务自然收口后清句柄。

        RUN_END 的"本轮无 TTS"判据是 `_tts_streaming_task is None`
        （:600 区），而旧实现句柄只在开轮/中止两处清零——播过一次 TTS 之后
        任何无播报轮的 RUN_END 永不再复位 `assist_pipeline_state`（卡 True）。
        done-callback 在协程体（含尾收口 `_converge_response`）跑完后触发，
        清零不抢在本轮 TTS 收尾之前；被替换的陈旧回调不动当前句柄。
        """
        if self._tts_streaming_task is task:
            self._tts_streaming_task = None

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
            # v1.0.93 退下旗（信号线末段）：conversation 实体按同一
            # conversation_id 记账 → 此处弹取转 kv。正向专用旗，绝不复用
            # continue_conversation 孤旗（v2.1.47 教训）；旧固件按名扫 kv、
            # 未知键天然忽略（fail-open），无能力协商。
            if end_dialogue.consume(
                    event.data["intent_output"].get("conversation_id")):
                data_to_send["end_dialogue"] = "1"
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
                    if self._zombie_tts_guard_active():
                        # v1.0.86：僵尸轮（drain 超时实锤）晚到的 TTS 不建流——
                        # 带上一轮音频的 STREAM_START 灌进当前轮正是"半句播报/
                        # 串音"的形态。API 推流设备连带抑制 TTS_END{url}（放行
                        # 会重演 v1.0.27 抢跑拆轮）。其余事件与本分支外的轮照常。
                        _LOGGER.warning(
                            "慧尖卫星: 僵尸防护窗内丢弃晚到 TTS 建流（%r）",
                            (tts_output.get("url") or "")[:40],
                        )
                        if feature_flags & VoiceAssistantFeature.API_AUDIO:
                            suppress_event = True
                    else:
                        # v1.0.88（I-1）：接管必须与新流 START 同处一个无 await
                        # 段——本行之后直到任务首行的 send STREAM_START 之间不得
                        # 插入任何 await，否则旧流有机会再 enqueue 一帧。
                        dl_seq = self._dl_takeover()
                        tts_task = self.config_entry.async_create_background_task(
                            self.hass,
                            self._stream_tts_audio(stream, dl_seq),
                            "esphome_voice_assistant_tts",
                        )
                        # v1.0.83（修#3）：完成即清句柄，RUN_END 的"本轮无 TTS"
                        # 判据不再被历史轮永久顶住。
                        tts_task.add_done_callback(self._clear_tts_streaming_task)
                        self._tts_streaming_task = tts_task
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

        # ── v1.0.93 announce 静音根修（2026-09-18 真机实锤：Announce finished
        #    (0 bytes)，REST 干等到超时）────────────────────────────────────
        # API 音频卫星（慧尖板无 URL 自取能力；2.1.11 起报 API_AUDIO，2.1.12 起
        # SPEAKER 并存申报——形态判定见 _api_audio_form）吃到 core 预合成的
        # tts_proxy media_id = 永远零包。
        # 修：有 message 文本时**本包自行合成**，复用 pipeline 应答那条已实战
        # 下行通路（_stream_tts_audio：归属闸/背压/饥饿遥测全套同享，真机
        # gapmax 110ms 健康）。三条护栏：
        #   ① API_AUDIO 位即接管（v1.0.98 判据修正，见 _api_audio_form）——
        #      SPEAKER-only 真喇叭设备自播 URL 旧路不变；
        #   ② 活跃 pipeline 轮（assist_pipeline_state=True）不接管下行——此时
        #      固件本就拒播报（on_announce busy refuse），抢代次反杀在播应答；
        #   ③ 合成建流失败不拦请求——设备按旧首包超时收口（WARN 点名），
        #      行为不劣于修前。
        # media_id 原样随请求发出（本板 on_announce 只记日志不抓取）。
        api_audio_only = False
        form_note = ""
        with contextlib.suppress(Exception):
            assert self._entry_data.device_info is not None
            _flags = (
                self._entry_data.device_info.voice_assistant_feature_flags_compat(
                    self._entry_data.api_version
                )
            )
            api_audio_only = _api_audio_form(_flags)
        # v1.0.96：device_info 竞态缺失（reload/重连窗——VM 案形态）不再一票否决：
        # 本实体无 UDP 通道且 API 版本已协商 ⇒ 慧尖客户群唯一形态=API 音频板
        # （交付约束：只装本加载项，官方 esphome 集成不装）。SPEAKER-only 真喇叭
        # 设备 device_info 必在且 flags 无 API_AUDIO，永不走本支——旧 URL 自取路
        # 一字不动。
        if (not api_audio_only and self._entry_data.device_info is None
                and self._udp_server is None and self._entry_data.api_version):
            api_audio_only = True
            form_note = "；device_info 不可得(重连竞态)，按无 UDP 通道的 API 音频形态接管"
        taken, skip = _announce_gate(api_audio=api_audio_only,
                                     has_message=bool(announcement.message),
                                     preannounce=bool(preannounce_media_id),
                                     pipeline_busy=bool(
                                         self._entry_data.assist_pipeline_state))
        # v1.0.99：preannounce 救援（判据与留痕见 _preannounce_rescued）——core 的
        # announce 默认给 message 注入「叮~」前置音，API 音频板放不出来；旧行为=
        # 整单回退 URL 形态=正文也陪葬 90s 0 字节。救援弃前置音（请求里不带、
        # 不合成），正文照常走自合成推流。
        if _preannounce_rescued(api_audio=api_audio_only,
                                has_message=bool(announcement.message),
                                skip=skip):
            taken, skip = True, ""
            preannounce_media_id = ""
            _LOGGER.warning(
                "[Announce] API 音频卫星：弃 core 默认前置音（本板无 URL 自取能力，"
                "放不出来），正文继续自合成推流接管（message=%d字）",
                len(announcement.message or ""))
        if skip:
            _LOGGER.warning(
                "[Announce] 播报未走文本自合成推流：%s（message=%d字, preannounce=%s, "
                "pipeline_state=%s%s）→ 旧 URL 形态，API 音频设备将静默至首包超时",
                skip, len(announcement.message or ""), bool(preannounce_media_id),
                self._entry_data.assist_pipeline_state,
                ("，" + form_note) if form_note else "")
        elif taken:
            engine_id = None
            ent_reg = er.async_get(self.hass)
            for eid in self.hass.states.async_entity_ids(Platform.TTS):
                ent = ent_reg.async_get(eid)
                if ent is not None and not ent.disabled_by \
                        and ent.platform == DOMAIN:
                    engine_id = eid
                    break
            if engine_id is not None:
                try:
                    lang = getattr(announcement, "language", None)
                    if lang not in ("en", "zh", "zh-Hans"):
                        lang = None          # 实体白名单外语种→用引擎默认
                    tts_stream = tts.async_create_stream(
                        hass=self.hass,
                        engine=engine_id,
                        language=lang,
                        options={
                            tts.ATTR_PREFERRED_FORMAT: "wav",
                            tts.ATTR_PREFERRED_SAMPLE_RATE: 16000,
                            tts.ATTR_PREFERRED_SAMPLE_CHANNELS: 1,
                            tts.ATTR_PREFERRED_SAMPLE_BYTES: 2,
                        },
                    )
                    tts_stream.async_set_message(announcement.message)
                    dl_seq = self._dl_takeover()
                    self.config_entry.async_create_background_task(
                        self.hass,
                        self._stream_tts_audio(tts_stream, dl_seq,
                                               announce=True),
                        "huijian_announce_tts",
                    )
                    _LOGGER.info(
                        "[Announce] API 音频卫星：文本已转合成推流（engine=%s，"
                        "%d 字，seq=%s）", engine_id,
                        len(announcement.message), dl_seq)
                except Exception:  # noqa: BLE001 ③ fail-open 见上
                    _LOGGER.warning(
                        "[Announce] API 音频播报合成建流失败，回旧形态"
                        "（设备将静默至首包超时收口）", exc_info=True)
            else:
                _LOGGER.warning(
                    "[Announce] 域内无 %s 的 TTS 引擎（加载项未运行/条目未启用"
                    "）——API 音频播报无源可推，回旧形态", DOMAIN)

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
            # v1.0.82（归因钉 B）：start 回调自计时——设备对应答只有 8s 预算。
            # 本回调 >1s 即 WARN：把"HA 忙 8 秒"从玄学变成一行日志（现场 18:14
            # 案设备侧只见"无应答"，HA 侧此前完全测不到迟滞在哪一层）。
            _t0 = asyncio.get_running_loop().time()
            result = await self._handle_pipeline_start_impl(
                conversation_id, flags, audio_settings, wake_word_phrase
            )
            _dt = asyncio.get_running_loop().time() - _t0
            if _dt > 1.0:
                _LOGGER.warning(
                    "慧尖卫星: start 回调耗时 %.1fs（逼近设备 8s 应答预算）"
                    "——HA 事件循环拥塞/订阅派发迟滞，设备侧可能判无应答",
                    _dt,
                )
            return result
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

    async def _drain_stale_pipeline(
        self, old_task: asyncio.Task | None, timeout: float = 2.0
    ) -> bool:
        """v1.0.55：新一轮开跑前确定性收掉旧一轮。

        现场形态：播报中途链路劣化/截断 → 设备发不出 stop/abort → 网络恢复后
        再次唤醒直接 start=1。core 的 accept **不防双开**（本实体 `_is_running`
        是"活着"位而非"在跑"位），两个 run 共抢同一个 `_audio_queue`、
        TTS 下行互相插帧——v1.0.45 台架实锤的"杂流/半句"在卫星端的复现。
        取消旧任务并**有界等待**其收口（2s，在设备 8s 应答预算内；本方法在
        后台任务里 await，不占 start 回调）；超时不阻塞新一轮（卡死旧 run
        是确定的坏，留 WARN 供现场对账）。
        返回 True=旧轮已收口。
        """
        if old_task is None or old_task.done():
            return True
        # v1.0.73 归因链·第③钉：拆轮者自报——"STT 事务被外部取消"的凶手名册。
        # 与 stt_transport 的取消行同刻成对出现=新轮接管；只有一边=另有其主。
        # getattr 取龄：本方法"不碰实体字段"是 v1055 立过的提取纪律。
        loop = asyncio.get_running_loop()
        t0 = getattr(self, "_pipeline_task_t0", 0.0)
        age = (loop.time() - t0) if t0 else -1.0
        _LOGGER.info(
            "慧尖卫星: 新一轮接管，取消旧 pipeline 轮（在途 %.1fs，收口预算 %.0fs）",
            age, timeout,
        )
        old_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(old_task), timeout=timeout)
        except TimeoutError:
            _LOGGER.warning(
                "慧尖卫星旧轮 pipeline 未在 %ds 内收口（已取消在途），"
                "新一轮照常开跑——僵尸轮晚到的 TTS 建流将由防护窗丢弃（8s）",
                timeout,
            )
            # v1.0.86：实锤僵尸才开窄窗（v1.0.83 的全扇身份闸在现场误杀健康轮，
            # 已撤）。窗内唯一拦截点=TTS_END 建推流（见 on_pipeline_event）。
            self._zombie_tts_guard_until = (
                asyncio.get_running_loop().time() + _ZOMBIE_TTS_GUARD_S
            )
            # v1.0.87：同时记住"是哪一轮"——窗的生死随该轮收口即刻结束。
            self._zombie_tts_guard_task = old_task
            return False
        except asyncio.CancelledError:
            # 区分两种取消：旧任务"以 cancelled 收尾"（shield 把结果抛给我们，
            # 正是我们要的收口）vs 本任务自己被撤销（实体拆除——此时绝不能继续
            # 开新轮）。cancelling()>0 说明撤销冲我们来的，如实上抛。
            cur = asyncio.current_task()
            if cur is not None and cur.cancelling() > 0:
                raise
        except Exception:  # noqa: BLE001 - 旧轮自身异常同样算收口
            _LOGGER.debug("慧尖卫星旧轮 pipeline 收口时抛错（忽略）", exc_info=True)
        return old_task.done()

    async def _handle_pipeline_start_impl(
        self,
        conversation_id: str,
        flags: int,
        audio_settings: VoiceAssistantAudioSettings,
        wake_word_phrase: str | None,
    ) -> int | None:
        """Handle pipeline run request."""
        # v1.0.73 归因链·第①钉：impl 首行 INFO = "设备请求确已到 HA 且应答必将在
        # 本 tick 发出"。与设备串口"No Response"对表一刀两断：此有彼无=下行半开
        # （应答死在路上）；此无=请求未达/订阅缺失。放 impl 不放 wrapper——
        # 保住 wrapper 零新增全局引用（v1043 提取执行纪律），且异常兜底仍回
        # error=True，证词为"处理死在①之后"。
        _LOGGER.info(
            "慧尖卫星: 收到设备开轮请求 is_wake=%s wake_word=%r conv=%s → 应答随后发出",
            bool(flags & VoiceAssistantCommandFlag.USE_WAKE_WORD),
            wake_word_phrase,
            conversation_id,
        )
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
        # v1.0.55：旧轮（若还活着）在后台协程里先接管再开跑——core 不防双开，
        # 两 run 共抢 _audio_queue = v1.0.45"杂流/半句"的卫星端复现；而设备
        # "播报截断→再唤醒"恰恰会在旧 run 收口前发来 start=1。start 回调本身
        # 保持零等待（设备 8s 应答预算）。
        old_pipeline_task = self._pipeline_task

        async def _run_pipeline_round() -> None:
            await self._drain_stale_pipeline(old_pipeline_task)
            await self.async_accept_pipeline_from_satellite(
                audio_stream=self._wrap_audio_stream(),
                start_stage=start_stage,
                end_stage=end_stage,
                wake_word_phrase=wake_word_phrase,
            )

        self._pipeline_task = self.config_entry.async_create_background_task(
            self.hass,
            _run_pipeline_round(),
            "esphome_assist_satellite_pipeline",
        )
        # v1.0.83：记住外层身份——下面的 await（async_accept_pipeline_from_
        # satellite）一旦开跑，core 会把 _pipeline_task 重绑成它的内层 run
        # 任务；事件甄别与 done-callback 都需要外层这条身份。
        self._round_outer_task = self._pipeline_task
        self._pipeline_task_t0 = asyncio.get_running_loop().time()  # ③钉的年龄基准
        self._pipeline_task.add_done_callback(self.handle_pipeline_finished)

        return port

    def _round_alive(self) -> bool:
        """v1.0.87：本轮是否有消费者在跑（判据=外层轮任务在且未完成）。

        必须用 `_round_outer_task`（本仓自有的外层身份），不能用
        `_pipeline_task`：后者被 core 的 async_accept_pipeline_from_satellite
        重绑为内层任务、并在 finally 里置 None，而 barge-in 的
        `_drain_stale_pipeline` 窗（≤2s）内它恒为 None——拿它判活会把接管期间
        真正该转写的音频误杀（v1.0.83 身份闸事故同源：那次错在事件面，这次
        若判错就错在音频面）。
        """
        task = self._round_outer_task
        return task is not None and not task.done()

    async def handle_audio(self, data: bytes, data2: bytes | None = None) -> None:
        """Handle incoming audio chunk from API.

        v1.0.19：对齐 aioesphomeapi≥45 双参调用 handle_audio(data, data2)
        （45.13.1 起实证；旧单参签名在 HA 2026.6+ 每帧 TypeError→上行全断，
        台架×固件协议审计实锤）。data2=增强音频第二通道，本固件只发单声道
        取偶通道后的流（data2 恒 None），收到即忽略；保留参数以兼容双通道设备。
        """
        # v1.0.87（现场 09:37–09:41：本 WARN 63 条 / 累计丢弃 9300 块）：本轮没有
        # 消费者时帧永远不可能被转写——旧形态照样塞进 160 格有界队列，既挤占
        # 哨兵与有效帧的容身空间，又每 100 块刷一条"管线消费停顿"的误导性归因
        # （真因是"根本没开轮"，与 STT 慢/事件循环被占无关）。下一轮开轮时
        # _handle_pipeline_start_impl 本就会清空队列，队列里这些帧注定是垃圾，
        # 故此处直投丢弃零信息损失，且把归因说准。
        if not self._round_alive():
            self._audio_orphan_chunks += 1
            if (self._audio_orphan_chunks == 1
                    or self._audio_orphan_chunks % _AUDIO_DROP_LOG_EVERY == 0):
                _LOGGER.warning(
                    "上行音频无消费者（未开轮或本轮已收口）→ 直投丢弃，累计 %d 块"
                    "：设备在无开轮状态下持续推流，HA 侧无从转写，请核对开轮时序"
                    "（连续对话轮后是否重开轮属固件行为）",
                    self._audio_orphan_chunks,
                )
            return
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
        if (task is not None and task is not self._pipeline_task
                and task is not self._round_outer_task):
            # v1.0.83：done-callback 挂在**外层**轮任务上，而 core accept 已把
            # _pipeline_task 重绑为内层 run 任务——只比 _pipeline_task 会让每轮
            # 合法完成都误判 stale（v1.0.49 的复位逻辑从此不落），真·旧轮回调
            # （barge-in）又与当前外层不同、照样拦住。两把身份都要认。
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
        dl_seq: int | None = None,
        sample_rate: int = 16000,
        sample_width: int = 2,
        sample_channels: int = 1,
        samples_per_chunk: int = 512,
        announce: bool = False,
    ) -> None:
        """Stream TTS audio chunks to device via API or UDP.

        v1.0.52：**真流式**——边收边发（旧实现 `b"".join(...)` 会等整段合成完，
        把上游延迟 1:1 放大成设备静音，见本文件顶部 _DEVICE_BUFFER_TARGET_S 注释）。
        背压口径对齐上游：保持设备环形缓冲约 75% 水位（384ms）。
        fail-loud 原样保留：非 WAV 早退 / 形态不符报错 / 0 帧告警。

        v1.0.88（下行流所有权）：`dl_seq` 是本流向设备写音频的归属凭据，由
        `_dl_takeover()` 在建流前发下；生产路径必须传（`None` 只留给直接调用/
        夹具，表示"不判归属"）。三处受它管辖：每帧 enqueue 前的归属闸、
        TTS_STREAM_END 是否由本流发（I-2）、本轮状态收口是否由本流落（I-3）。

        v1.0.93（announce 推流复用）：`announce=True` 只豁免入口 `_is_running`
        闸——该位在 pipeline run 期才置真，播报不启 run；其余闸（I-1/I-2/I-3
        归属）全数照管，被新轮接管即静默退场。设备侧收束：音频帧在 ANNOUNCING
        态照进播放队列；STREAM_START/END 两事件旧固件在 ANNOUNCING 态忽略
        （无害留痕），流尾收束由 v2.1.55 drain 判定接住。
        """
        self.cli.send_voice_assistant_event(
            VoiceAssistantEventType.VOICE_ASSISTANT_TTS_STREAM_START, {}
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        # v1.0.89（F3）：本设备的预灌水位——按固件能力分流，**逐条失败都回旧值**。
        # 写成"先赋默认、再 suppress 整段尝试"而不是 let-else：AST 摘真身的行为钉
        # 只喂部分桩（没有 device_info/_entry_data 也不许炸），NameError/AttributeError
        # 一并落回 0.384s＝今天的行为，方向恒 fail-open。
        buffer_target_s = _DEVICE_BUFFER_TARGET_S
        with contextlib.suppress(Exception):
            buffer_target_s = _prebuf_target_for_version(
                self._entry_data.device_info.project_version)
        frames_sent = 0
        first_chunk_ms: int | None = None
        # v1.0.76（归因钉）：连续块发送时刻跟踪——收口一行给出跨度/速率/最大
        # 块隙。现场 09:38/10:37 两案：服务器秒级发完，设备却 0.42~0.44× 摊拍
        # 收货——"播报像断开"的真凶在 HA→设备这一跳，但推流循环饥饿（数据晚到
        # /事件循环被抢）与 aioesphomeapi writer/TCP/设备侧两型此前不可分。
        # rate≈1.0 而设备跨度大 → writer/网络/设备侧；rate<1 → 本循环被饿，
        # max_gap 直接暴露最长一次等待（数据没来 or 没被调度）。
        _last_send_t: float | None = None
        _max_gap_ms = 0

        def _converge_response() -> None:
            # v1.0.70（深审⑭尾收口）：一切非取消早退都必须落这两行。旧形态
            # `except …: return` 直接跳过函数尾的 tts_response_finished +
            # pipeline_state，实体卡"仍在应答"，后续唤醒被卡死旧 run 无声吞
            # ——现场 12:14 案：播报半路夭折后连续两轮 8s 无应答×2 直到熔断。
            # CancelledError 支豁免（打断方自会收口，维持原语义不动）。
            # v1.0.88（I-3）：已被接管的旧流不得替新流落状态——core 的
            # tts_response_finished 就是 _set_state(IDLE)，旧流晚到一步会把新流
            # 正在播报的状态踩成空闲（同一种串扰的另一个面）。收口归现归属者。
            if dl_seq is not None and dl_seq != self._dl_seq:
                _LOGGER.info(
                    "慧尖卫星: 旧下行流让位（seq=%s 现归属=%s），状态收口交新流",
                    dl_seq, self._dl_seq,
                )
                return
            self.tts_response_finished()
            self._entry_data.async_set_assist_pipeline_state(False)

        try:
            if not announce and not self._is_running:
                return

            if tts_result.extension != "wav":
                _LOGGER.error(
                    "Only WAV audio can be streamed, got %s", tts_result.extension
                )
                _converge_response()
                return

            audio_duration_sent = 0.0
            # v1.0.65（TTS 深审 T4）：async 生成器提出来命名 + finally 确定性
            # aclose。旧版 barge-in 取消若落在背压 sleep（生成器帧外），
            # `except CancelledError: return` 直接把挂起的 _iter_wav_pcm_chunks
            # 丢给 GC——其持有的 core async_stream_result 与 ResultStream 已
            # 缓冲音频（5min 句 ≈10MB）滞留不定才释放，违反 tts_transport 自家
            # 纪律「消费端 finally aclose，不赌 GC 时机」。已耗尽时 aclose 为
            # no-op，路径无害。
            chunk_iter = _iter_wav_pcm_chunks(
                tts_result.async_stream_result(),
                sample_rate=sample_rate,
                sample_width=sample_width,
                sample_channels=sample_channels,
                samples_per_chunk=samples_per_chunk,
            )
            try:
                async for chunk, is_last in chunk_iter:
                    if not self._is_running:
                        break

                    # ── v1.0.88 归属闸（I-1 的落地点）────────────────────────
                    # 与紧随其后的 enqueue（API 侧 send_voice_assistant_audio 是
                    # 同步入队、UDP 侧 sendto 同为同步）**之间没有 await**：被
                    # 接管的旧流一旦被吊销，这一帧就是它能为这条连接写的最后一
                    # 帧。旧 run 音频混进新流头部（播报开头半句残段）自此不可能
                    # 再由 HA 侧产生——设备侧的自证要等协议批（run/stream id）。
                    if dl_seq is not None and dl_seq != self._dl_seq:
                        self._dl_drop_total += 1
                        if (self._dl_drop_total == 1
                                or self._dl_drop_total % _DL_DROP_LOG_EVERY == 0):
                            _LOGGER.warning(
                                "慧尖卫星: 旧下行流被接管后仍在吐帧，丢第 %d 帧"
                                "（本流 seq=%s 现归属=%s）——不丢即混入新流头部",
                                self._dl_drop_total, dl_seq, self._dl_seq,
                            )
                        return

                    if self._udp_server is not None:
                        self._udp_server.send_audio_bytes(chunk)
                    else:
                        self.cli.send_voice_assistant_audio(chunk)

                    samples_in_chunk = len(chunk) // (sample_width * sample_channels)
                    frames_sent += samples_in_chunk
                    now_t = loop.time()
                    if _last_send_t is not None:
                        gap_ms = int((now_t - _last_send_t) * 1000)
                        if gap_ms > _max_gap_ms:
                            _max_gap_ms = gap_ms
                    _last_send_t = now_t
                    if first_chunk_ms is None:
                        first_chunk_ms = int((loop.time() - started) * 1000)
                        # M13（2026-09-23 深审）：背压基准此前从 STREAM_START
                        # 起算——冷缓存/云延迟下首块等 G 秒，G 已计入 elapsed，
                        # wait_time 恒 ≤0 → 整段帧流零 sleep 毫秒级灌爆设备
                        # 1.28s 环缓冲（audio_service 满则丢最旧=「缺头/丢头」
                        # 症状族）。v1.0.52 真流式改造时把 started 挪前是回归
                        # （上游 HA 是 join 完才起算）。首块实发瞬间才是
                        # 「设备开始吃水」的零点：基准重置。
                        started = loop.time()
                        audio_duration_sent = 0.0
                        # 逐跳首块遥测：这一行的时间 = 上游合成/转换的首块延迟；
                        # 与设备侧 `Downlink audio start` 相减即得本跳（HA→设备）耗时。
                        _LOGGER.info(
                            "[TTS] 首块 %dms 后发出（%d 样本，流式）",
                            first_chunk_ms,
                            samples_in_chunk,
                        )

                    audio_duration_sent += samples_in_chunk / sample_rate
                    if is_last:
                        break

                    # 背压：把"已发音频时长 − 已用墙钟"压到水位以内（上游同口径）
                    elapsed = loop.time() - started
                    if (wait_time := (audio_duration_sent - buffer_target_s) - elapsed) > 0:
                        await asyncio.sleep(wait_time)
            except ValueError as err:
                # fail-loud：非 WAV / 形态不符 / 头不完整 → 当场点名（旧实现是 error 行）
                _LOGGER.error("[TTS] WAV 流不可播：%s", err)
                _converge_response()
                return
            except Exception as err:  # noqa: BLE001 —— 上游流异常也要留痕并收尾
                # 加载项断连 / opus 解码失败 / 转换器异常等：这里必须吞掉并留痕，
                # 否则异常会穿出后台任务变成 "Task exception was never retrieved"，
                # 现场既看不到归因、收尾事件也依赖 finally（本处仍会走到 finally）。
                # CancelledError 继承 BaseException，不受本分支影响。
                # v1.0.70（深审⑭）：本 return 曾跳过尾收口——卫星卡"仍在应答"
                # 的第一现场（12:14 案：夭折后两轮 8s 不应答直到熔断）。收口
                # 必须先于 return，TTS_STREAM_END 由外层 finally 保送。
                _LOGGER.error("[TTS] 下行流异常：%s", err)
                _converge_response()
                return
            finally:
                with contextlib.suppress(Exception):
                    await chunk_iter.aclose()

            if frames_sent <= 0:
                # v1.0.25 fail-loud 的流式等价物：0 帧会让设备「起流即收流」——
                # 灯照常执行、播报全哑。流式下只能在流尾判定，故在此点名。
                _LOGGER.warning("[TTS] 音频 0 帧（流结束仍无音频），设备将静音")
            else:
                # v1.0.76 归因收口：音频时长 ÷ 实发跨度 = 推流速率。健康流
                # ≈1.0×（背压水位 0.384s 会略拉高跨度）；<0.8× 即设备侧会
                # 出现可闻饿拍——与设备串口 TTS stream end 时刻对表即分锅
                # （此处<1 → HA 侧推流被饿；此处≈1 而设备仍慢 → writer/网络/设备）。
                _audio_s = frames_sent / sample_rate
                _span_s = (_last_send_t - started) if _last_send_t is not None else 0.0
                # 极短流（首尾同刻/亚毫秒跨度）无节奏可言：报 99 哨兵=瞬间完成
                # 形态，绝不报 0.00 让现场把"快"误读成"饿"。
                _rate = (_audio_s / _span_s) if _span_s > 0.001 else 99.0
                _LOGGER.info(
                    "[TTS] 推流 %d 帧 %.2fs（流式，首块 %sms；跨度 %.2fs 速率 %.2f× 最大块隙 %dms 水位 %.3fs）",
                    frames_sent,
                    _audio_s,
                    first_chunk_ms if first_chunk_ms is not None else -1,
                    _span_s,
                    _rate,
                    _max_gap_ms,
                    buffer_target_s,
                )
                # v1.0.90：饥饿形态额外升一条 WARNING。上面那行是 INFO，HA 默认
                # 日志级（warning）下**现场看不到**，而"一句话分几次说完"的定锅
                # 全靠这两个数：速率<0.8 或 最大块隙>1s ⇒ 锅在上游数据晚到/HA 推流
                # 循环被饿（设备侧已排除：固件 v2.1.51+ 一拍可收 10 包、且停顿期无
                # `Component … took a long time`）。现场复制这一行就能定方向。
                if _rate < 0.8 or _max_gap_ms > 1000:
                    _LOGGER.warning(
                        "[TTS] 下行饥饿：音频 %.2fs 用了 %.2fs 推完（速率 %.2f×，"
                        "最长一次等待 %dms）——听感为播报断续；锅在上游出帧晚或 HA 推流"
                        "被饿，非设备丢帧",
                        _audio_s, _span_s, _rate, _max_gap_ms,
                    )
        except asyncio.CancelledError:
            return  # Don't trigger state change
        finally:
            # v1.0.88（I-2）：只有现归属者能向设备宣告"这条流结束了"。被接管
            # 的旧流若在这里补发 TTS_STREAM_END，设备会在**新流刚起**的
            # STREAMING_RESPONSE 态收到一条属于旧流的结束，直接切 STOP_PIPELINE
            # （固件 :411-425），现场=新播报只响半句甚至全哑。旧流的 END 由
            # _dl_takeover 的接管者负责（新流自己会成对发 START/END）。
            if dl_seq is None or dl_seq == self._dl_seq:
                self.cli.send_voice_assistant_event(
                    VoiceAssistantEventType.VOICE_ASSISTANT_TTS_STREAM_END, {}
                )
            else:
                _LOGGER.info(
                    "慧尖卫星: 被接管的旧流不代发现场 TTS_STREAM_END"
                    "（seq=%s 现归属=%s）——否则新流被当场收口",
                    dl_seq, self._dl_seq,
                )

        # State change
        _converge_response()

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
        """Start a UDP server on a random free port.

        v1.0.52：先无条件停旧实例（幂等）。barge-in 的 stale done-callback 会
        提前 return 跳过 handle_pipeline_finished 里的 _stop_udp_server()，
        下一轮 start 若直接覆盖 self._udp_server 就泄漏一个绑死 socket。
        """
        self._stop_udp_server()
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
