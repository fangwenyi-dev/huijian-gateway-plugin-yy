import asyncio
import io
import logging
import wave
from collections.abc import AsyncGenerator, AsyncIterable

import numpy as np
import opuslib_next as opuslib
from homeassistant.components import ffmpeg
from homeassistant.core import HomeAssistant

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def wrap_pcm_as_wav(pcm: bytes, rate: int, channels: int, sample_bytes: int = 2) -> bytes:
    """裸 PCM → WAV 容器（纯 Python，无 ffmpeg 依赖；卫星推流目标同构）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_bytes)
        wav_file.setframerate(rate)
        wav_file.writeframes(pcm)
    return buf.getvalue()


async def async_convert_audio(
    hass: HomeAssistant,
    audio_bytes_gen: AsyncIterable[bytes] | AsyncGenerator[bytes],
    from_extension: str,
    to_extension: str,
    to_codec: str | None = None,
    to_sample_rate: int | None = None,
    to_sample_channels: int | None = None,
    to_sample_bytes: int | None = None,
    to_frame_duration: int | None = None,
    input_params: list | None = None,
) -> AsyncGenerator[bytes, None]:
    """Convert audio to a preferred format using ffmpeg."""
    # ── s16le → wav 纯 Python 直封（v1.0.25）──────────────────────────
    # 卫星推流只认 16k/mono/16bit WAV，而加载项 WS tts 通道吐的正是裸
    # s16le 16k/mono——本可原样封容器。此前一律走 ffmpeg，而
    # ffmpeg.get_ffmpeg_manager(hass) 要求 HA 已配置 ffmpeg 集成，
    # 未配置即 RuntimeError → 播报静默且报错离病因很远（2026-09-09 排查）。
    # 直封后关键路径零外部依赖、零子进程，字节级可断言。
    if from_extension == "s16le" and to_extension == "wav":
        rate, channels = 16000, 1
        params = list(input_params or [])
        for idx, param in enumerate(params):
            if param == "-ar" and idx + 1 < len(params):
                rate = int(params[idx + 1])
            elif param == "-ac" and idx + 1 < len(params):
                channels = int(params[idx + 1])
        # v1.0.27：直封成立的前提是「输出要求 == 源 PCM 形态」。源是 16bit
        # 裸 PCM 故 to_sample_bytes 只容 None/2；to_sample_rate/-channels 被
        # 要求成别的值（如 tts.speak 要立体声或 22050）时必须回退 ffmpeg——
        # 否则封出的 WAV 头字段与调用方要求不符：卫星按头校验直接拒收
        # （「Can only stream 16Khz 16-bit mono WAV」→ 又一场静音），媒体
        # 播放器则变速播放。宁慢勿错。
        if (
            to_sample_rate in (None, rate)
            and to_sample_channels in (None, channels)
            and to_sample_bytes in (None, 2)
        ):
            pcm = b"".join([chunk async for chunk in audio_bytes_gen])
            if not pcm:
                # v1.0.34（审查 M2）：空合成必须在此截住——44 字节纯头是"非空
                # bytes"，能骗过出口 `if not audio` 闸写进 HA TTS 缓存，同一句
                # 永久静音+日志死寂（2026-09-09 病灶复发入口）。不产出 → 出口
                # fail-loud 报 (None,None)，HA 跳缓存、错误当场可见。
                _LOGGER.error("[TTS] 直封收到空 PCM（加载项未回音频），不产出（出口走 fail-loud）")
                return
            wav = wrap_pcm_as_wav(pcm, rate, channels, 2)
            _LOGGER.info(
                "[TTS] s16le→wav 直封：%d 帧 %.2fs（%dHz/%dch/16bit，%d 字节）",
                len(pcm) // (2 * channels),
                len(pcm) / (2 * channels * rate) if pcm else 0.0,
                rate,
                channels,
                len(wav),
            )
            yield wav
            return
        _LOGGER.info(
            "[TTS] 直封不适用：输出要求 %sHz/%sch/%sbyte ≠ 源 %dHz/%dch/16bit，转 ffmpeg",
            to_sample_rate,
            to_sample_channels,
            to_sample_bytes,
            rate,
            channels,
        )

    ffmpeg_manager = ffmpeg.get_ffmpeg_manager(hass)
    command = [
        ffmpeg_manager.binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        from_extension,
        *(input_params or []),
        "-i",
        "pipe:0",
    ]
    if to_sample_rate is not None:
        command.extend(["-ar", str(to_sample_rate)])
    if to_sample_channels is not None:
        command.extend(["-ac", str(to_sample_channels)])
    if to_extension == "mp3":
        command.extend(["-q:a", "0"])
    if to_codec is not None:
        command.extend(["-c:a", str(to_codec)])
    elif to_extension == "opus":
        command.extend(["-c:a", "libopus"])
    if to_sample_bytes == 2:
        command.extend(["-sample_fmt", "s16"])
    if to_frame_duration is not None:
        command.extend(["-frame_duration", str(to_frame_duration)])
    command.extend(["-f", to_extension, "pipe:1"])
    _LOGGER.debug("Convert audio using ffmpeg: %s", " ".join(command))

    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def write_input() -> None:
        assert process.stdin
        try:
            async for chunk in audio_bytes_gen:
                process.stdin.write(chunk)
                await process.stdin.drain()
        finally:
            if process.stdin:
                process.stdin.close()

    writer_task = hass.async_create_background_task(
        write_input(), f"{DOMAIN}_stt_ffmpeg"
    )
    assert process.stdout
    try:
        if to_extension == "opus":
            demuxer = AsyncOggOpusDemuxer(process.stdout)
            async for chunk in demuxer:
                yield chunk
        else:
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    break
                yield chunk
    finally:
        await writer_task
        retcode = await process.wait()
        if retcode != 0:
            assert process.stderr
            stderr_data = await process.stderr.read()
            _LOGGER.error(
                "Convert audio failed (%s): %s", retcode, stderr_data.decode()
            )
            raise RuntimeError(
                f"Unexpected error while running ffmpeg with arguments: {command}. See log for details."
            )


def _parse_wav_data_offset(data: bytes) -> int:
    """Parse RIFF/WAV header to find the offset of the data chunk.

    Standard WAV has a 44-byte header, but extension chunks (fact, list, etc.)
    can make it larger. This function traverses RIFF chunks to find 'data'.
    """
    if len(data) < 12:
        return 44
    if data[0:4] != b"RIFF":
        return 0
    # Skip RIFF header (12 bytes: RIFF + size + WAVE)
    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset:offset + 4]
        chunk_size = int.from_bytes(data[offset + 4:offset + 8], "little")
        if chunk_id == b"data":
            return offset + 8
        offset += 8 + chunk_size
        # Chunks are padded to even byte boundary
        if chunk_size % 2:
            offset += 1
    return 44


async def wav_to_opus(stream, sample_rate=16000, channels=1, frame_duration=60):
    frame_samples = int(sample_rate * (frame_duration / 1000))
    frame_bytes = frame_samples * channels * 2
    encoder = opuslib.Encoder(sample_rate, channels, opuslib.APPLICATION_AUDIO)
    buffer = bytearray()
    wav_header_skip = None
    async for chunk in stream:
        if wav_header_skip is None and chunk.startswith(b"RIFF"):
            wav_header_skip = _parse_wav_data_offset(chunk)
            _LOGGER.debug("WAV data offset: %s", wav_header_skip)
        elif wav_header_skip is None:
            wav_header_skip = 0
        if wav_header_skip > 0:
            skip_len = min(len(chunk), wav_header_skip)
            chunk = chunk[skip_len:]
            wav_header_skip -= skip_len
            if not chunk:
                continue
        buffer.extend(chunk)
        while len(buffer) >= frame_bytes:
            pcm_frame = buffer[:frame_bytes]
            del buffer[:frame_bytes]
            # yield bytes(pcm_frame)
            np_frame = np.frombuffer(pcm_frame, dtype=np.int16)
            yield encoder.encode(np_frame.tobytes(), frame_samples)
    if buffer:
        buffer = buffer.ljust(frame_bytes, b"\x00")
        yield encoder.encode(bytes(buffer), frame_samples)


class AsyncOggOpusDemuxer:
    def __init__(self, reader: asyncio.StreamReader):
        self._reader = reader
        self._buffer = bytearray()
        self._packet_count = 0

    async def _read_exact(self, n: int) -> bytes | None:
        while len(self._buffer) < n:
            chunk = await self._reader.read(4096)
            if not chunk:
                return None
            self._buffer.extend(chunk)

        data = self._buffer[:n]
        del self._buffer[:n]
        return bytes(data)

    async def __aiter__(self) -> AsyncGenerator[bytes, None]:
        while True:
            page_header = await self._read_exact(4)
            if not page_header:
                break
            if page_header != b"OggS":
                raise ValueError("Invalid Ogg header received from ffmpeg")

            common_header = await self._read_exact(23)
            if not common_header:
                break

            n_segments = common_header[-1]

            segment_table_bytes = await self._read_exact(n_segments)
            if not segment_table_bytes:
                break

            segment_table = list(segment_table_bytes)
            page_data_len = sum(segment_table)
            page_data = await self._read_exact(page_data_len)
            if not page_data:
                break

            packet_buffer = bytearray()
            data_ptr = 0
            for segment_len in segment_table:
                packet_buffer.extend(page_data[data_ptr : data_ptr + segment_len])
                data_ptr += segment_len

                if segment_len < 255:
                    self._packet_count += 1
                    if self._packet_count > 2:
                        yield bytes(packet_buffer)
                    packet_buffer = bytearray()
