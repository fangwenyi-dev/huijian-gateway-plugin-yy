import contextlib
import logging
import time
from collections.abc import AsyncGenerator

import opuslib_next as opuslib
from homeassistant.components.tts import TextToSpeechEntity as BaseEntity
from homeassistant.components.tts import TtsAudioType
from homeassistant.components.tts.const import DOMAIN as ENTITY_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .huijian import tts_transport
from .huijian.audio import async_convert_audio

# v1.0.52：HA 的流式 TTS 契约（TTSAudioRequest/TTSAudioResponse）。老 HA 没有这
# 组 API：那时子类的 async_stream_tts_audio 不会被任何东西调用，整段路径照旧，
# 故此处做成软依赖（缺了也不影响集成加载）。
try:  # pragma: no cover - 取决于运行环境 HA 版本
    from homeassistant.components.tts import (TTSAudioRequest,
                                              TTSAudioResponse)
    _HA_STREAMING_TTS = True
except ImportError:  # pragma: no cover
    TTSAudioRequest = None  # type: ignore[assignment,misc]
    TTSAudioResponse = None  # type: ignore[assignment,misc]
    _HA_STREAMING_TTS = False

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
        # v1.0.26：必须声明首选格式键，否则 HA core 会把它们从 options 里
        # **弹出**（tts/__init__.py _async_generate_tts_audio：不在
        # supported_options 里的 preferred_* 一律 pop，引擎根本收不到），
        # 于是本实体恒走 mp3、再由 HA 用 ffmpeg 二次转码——v1.0.25 的
        # s16le→wav 纯 Python 直封成了死代码。声明后引擎直接按卫星要求
        # 产 16k/mono/16bit WAV，少一次有损转码。
        self._attr_supported_options = [
            "preferred_format",
            "preferred_sample_rate",
            "preferred_sample_channels",
            "preferred_sample_bytes",
            "huijian_voice_fp",
        ]
        self._attr_extra_state_attributes = {}

    @property
    def default_options(self) -> dict:
        """v1.0.48（P5，"多嗓音"第六路径收口）：core 把实体 default_options
        无条件合进 options 并参与缓存键 sha1(文本)_语言_options_引擎——加载项
        内部音色对键**不感知**，web 换嗓后同一句永远命中旧嗓缓存。把服务端
        经 WS settings 推送的音色指纹放进键里：换嗓 → 键轮换 → 必重合成，
        旧条目 TTL 自然作废。指纹缺席（旧版加载项）→ 空 dict，行为与今一致。"""
        try:
            tr = self.hass.data[DOMAIN][self.entry.entry_id].get(
                tts_transport.ATTR_TRANSPORT)
            fp = getattr(tr, "voice_fp", None) if tr else None
        except Exception:
            fp = None
        return {"huijian_voice_fp": str(fp)} if fp else {}

    async def async_added_to_hass(self):
        _LOGGER.info("huijianTtsEntity.async_added_to_hass")
        await super().async_added_to_hass()

    # ── v1.0.52：拆出的公共件（流式/整段两条路复用）─────────────────────

    @staticmethod
    def _resolve_fmt(options: dict) -> str:
        """v1.0.19：消费 core 下发的首选格式。

        assist_satellite 对可推流设备传 preferred_format="wav"/16k/mono/16bit，
        卫星流式通道（AS:_stream_tts_audio）只认 wav——此前只读 "audio_format"
        键（core 从不传）恒回 mp3，播报在卫星端必「Only WAV」早退（台架×固件
        协议审计实锤）。无 preferred 时保持旧行为 mp3。
        """
        return options.get("preferred_format") or options.get("audio_format") or "mp3"

    def _convert(self, pcm_stream, fmt: str, options: dict):
        """s16le@16k PCM 流 → 目标容器（wav 走 audio.py 的纯 Python 直封）。"""
        return async_convert_audio(
            self.hass,
            pcm_stream,
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

    async def _async_pcm_stream(self, message: str):
        """连加载项 → 逐块产出 s16le@16k PCM（opus 解码后）。

        v1.0.52：从原 data_gen 原样搬出，供"流式"与"整段"两条路复用。
        调用方负责 aclose（流式路径由外层生成器 finally 收口）。
        v1.0.45（缺字/静音错位毒化根治）：detect 发送、整流消费、收口判定整体
        下沉到 TtsTransport.stream()——同连接并发请求在传输层串行，且本轮若未以
        stop 收口（本函数被取消/出错）即断连清算，残帧绝不拖进下一次播报。
        """
        transport = tts_transport.get_entry_transport(self.hass, self.entry)
        if not await transport.ensure_connected():
            _LOGGER.error("Failed to establish WebSocket connection for TTS")
            raise HomeAssistantError("huijian TTS WebSocket 未连接")
        decoder = opuslib.Decoder(self.opus_sample_rate, self.opus_channels)
        stream = transport.stream(message)
        try:
            async for resp in stream:
                if isinstance(resp, bytes):
                    try:
                        resp = decoder.decode(resp, self.opus_frame_samples)
                    except Exception as e:
                        # v1.0.34（审查 L9）：解码失败绝不 yield 原始 opus 包——
                        # 此前混流让卫星播噪声；丢帧保静音，日志留痕。
                        _LOGGER.error("Decode opus failed, frame dropped: %s", e)
                        continue
                    _LOGGER.info(
                        "Received bytes: %s %s", len(resp), resp.hex()[0:64]
                    )
                    yield resp
                else:
                    if getattr(resp, "error", None):
                        raise RuntimeError(resp.error)
                    _LOGGER.info("Received response: %s", resp)
        finally:
            with contextlib.suppress(BaseException):
                await stream.aclose()

    # ── v1.0.52：真流式出口（HA 原生流式契约）───────────────────────────

    async def async_stream_tts_audio(
        self, request: "TTSAudioRequest"
    ) -> "TTSAudioResponse":
        """边合成边下发，首音延迟 = 首块延迟（而不是整段合成时间）。

        为什么必须走这条 API（而不是在 async_get_tts_audio 里返回生成器）：
        HA 的 `TtsAudioType = tuple[str|None, bytes|None]` **只收 bytes**
        （components/tts/const.py），实体侧唯一的流式出口就是
        `async_stream_tts_audio → TTSAudioResponse(extension, data_gen)`；父类
        `async_supports_streaming_input()` 以"子类是否重写本方法"自动判定，重写
        即生效（HA 的 pipeline 随之走 streaming 分支并用 tts_result.data_gen）。
        HA 的 TTSCache 会边读 data_gen 边把每块 `put_nowait` 给消费者
        （components/tts/__init__.py `TTSCache.async_stream_data`），于是这条流
        一路流到卫星、再流到设备——现场"STREAM_START 后静音 31 秒"的病灶即此处
        以前把整段攒成 bytes。
        """
        if not _HA_STREAMING_TTS:  # pragma: no cover - 老 HA 不会走到本方法
            raise HomeAssistantError("当前 HA 版本不支持流式 TTS（缺 TTSAudioResponse 契约）")

        t_start = time.monotonic()
        message = "".join([chunk async for chunk in request.message_gen])
        options = request.options or {}
        _LOGGER.info(
            "huijianTtsEntity.async_stream_tts_audio: message=%.40s…(%d chars), language=%s",
            message,
            len(message),
            request.language,
        )
        fmt = self._resolve_fmt(options)
        pcm = self._async_pcm_stream(message)
        converted = self._convert(pcm, fmt, options)

        # peek 首块：流式下无法"事后返回 (None, None)"，改为抛错——HA 的
        # _load_data_into_cache 捕获异常后会 pop 掉内存缓存条目，等价保留
        # v1.0.25「空音频绝不进缓存」纪律（空结果一旦进缓存 = 同一句永远静音）。
        try:
            first = await converted.__anext__()
        except StopAsyncIteration:
            first = b""
        except BaseException:
            with contextlib.suppress(BaseException):
                await converted.aclose()
            raise
        if not first:
            _LOGGER.error(
                "[TTS] 合成结果为空（加载项未回音频/转换失败）: %r", message[:40]
            )
            with contextlib.suppress(BaseException):
                await converted.aclose()
            raise HomeAssistantError(f"No TTS from {self.entity_id} for '{message[:40]}'")
        _LOGGER.info(
            "[TTS] 首块就绪 %dms：%s %d 字节（原文 %r）——流式下发",
            int((time.monotonic() - t_start) * 1000),
            fmt,
            len(first),
            message[:40],
        )

        async def _stream() -> AsyncGenerator[bytes]:
            total = len(first)
            try:
                yield first
                async for chunk in converted:
                    if chunk:
                        total += len(chunk)
                        yield chunk
            finally:
                with contextlib.suppress(BaseException):
                    await converted.aclose()
                _LOGGER.info(
                    "[TTS] 流式下发收束：%s 共 %d 字节（原文 %r）", fmt, total, message[:40]
                )

        return TTSAudioResponse(fmt, _stream())

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict
    ) -> TtsAudioType:
        """整段路径（保留）：非管线消费者（tts.speak 服务 / media_source / 老 HA）
        仍走这里；HA 的 assist 管线现在走上面的流式出口。"""
        # v1.0.48（隐私）：播报文本属家居隐私，INFO 只留前 40 字+长度，
        # 全文降 DEBUG（现场默认 INFO 级不落全量）。
        _LOGGER.info(
            "huijianTtsEntity.async_get_tts_audio: message=%.40s…(%d chars), language=%s, options=%s",
            message,
            len(message),
            language,
            options,
        )
        fmt = self._resolve_fmt(options)
        pcm = self._async_pcm_stream(message)
        converting = self._convert(pcm, fmt, options)
        audio = b""
        try:
            async for chunk in converting:
                audio += chunk
        except HomeAssistantError:
            # 连接失败 == 旧实现的 (None, None) 语义
            return None, None
        finally:
            with contextlib.suppress(BaseException):
                await converting.aclose()
            with contextlib.suppress(BaseException):
                await pcm.aclose()
        # v1.0.25 fail-loud：空音频是「灯开了不播报」的直接病灶，此前静默返回
        # 空 WAV 一路无声；现在两端日志各留一行，链路可逐跳对账。
        if not audio:
            _LOGGER.error("[TTS] 合成结果为空（加载项未回音频/转换失败）: %r", message[:40])
            # 空结果绝不能返回 (fmt, b"")——HA 会把它写进 TTS 缓存，之后同一句
            # 永远命中空缓存（连引擎都不再调用），现场形态就是"灯开了、永远没
            # 声音、日志一片安静"（2026-09-09 实锤）。返回 (None, None) 让 HA
            # 报错并跳过缓存，问题当场可见。
            return None, None
        _LOGGER.info("[TTS] 音频就绪：%s %d 字节（原文 %r）", fmt, len(audio), message[:40])
        return fmt, audio
