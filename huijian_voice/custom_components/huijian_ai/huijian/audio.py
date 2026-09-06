import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterable

import numpy as np
import opuslib_next as opuslib
from homeassistant.components import ffmpeg
from homeassistant.core import HomeAssistant

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)


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
