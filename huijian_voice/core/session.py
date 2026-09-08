"""三通道会话状态机（《小智协议子集-服务器契约.md》逐条实现）。

通道语义回顾（集成侧 huijian_ai 源码实证后的服务器义务）：
  stt  : hello 可收可回；listen start 清缓冲；binary=裸 opus(16k/mono/60ms)；
         listen stop ⇒ 必须回且仅回一条 {"type":"stt","text":…}（静音也回 text:""）；
         此通道禁止 binary、禁发 tts/text 帧。
  tts  : 无 hello；收 {"type":"tts","state":"detect","text":…} ⇒ 流裸 opus binary，
         收束发 {"type":"tts","state":"stop"}；新 detect 到达丢弃旧流且不得有孤儿帧；
         任何 JSON 帧不得含 "error" 键；每 detect 必有 stop。
  llm  : 收 listen detect(mode=prompt,text) ⇒ {"type":"text","state":"start"} →
         逐句 {"type":"text","state":"sentence_end","data":…} →
         终帧 {"type":"text","state":"end"}（客户端 end 判定先于 type，且 end 必发）。
通用：ping→pong；不主动 close；预算：stt 结果 55s / tts 流 55s / llm 回合 50s；
JSON 宽容解析（未知键忽略）；token 校验在 HTTP 握手层（401 不升级）。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Optional

import aiohttp

from . import audio, const

logger = logging.getLogger("huijian.session")


def _json(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class BaseSession:
    channel = ""

    def __init__(self, ws: aiohttp.WebSocketResponse, ctx):
        self.ws = ws
        self.ctx = ctx                      # AppContext（asr/tts/pipeline/settings）
        self.device_hint = ""
        self._send_lock = asyncio.Lock()
        self.created = time.time()
        # F7b：fire-and-forget 回执帧（pong/hello）必须持强引用——asyncio 仅弱
        # 引用 task，GC 时机不巧即丢 pong → 客户端 keepalive 超时断链。
        self._pending: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._pending.add(t)
        t.add_done_callback(self._pending.discard)

    def close_work(self) -> None:
        """停机/断连钩子：取消在飞工作 task（子类覆写补专属资源）。"""
        t = getattr(self, "_task", None)
        if t is not None and not t.done():
            t.cancel()

    async def send_json(self, obj: dict) -> bool:
        async with self._send_lock:
            if self.ws.closed:
                return False
            try:
                await self.ws.send_str(_json(obj))
                return True
            except Exception:
                return False

    async def send_bytes(self, data: bytes) -> bool:
        async with self._send_lock:
            if self.ws.closed:
                return False
            try:
                await self.ws.send_bytes(data)
                return True
            except Exception:
                return False

    async def on_text(self, raw: str) -> None:
        raise NotImplementedError

    async def on_binary(self, data: bytes) -> None:
        raise NotImplementedError

    async def on_close(self) -> None:
        pass

    # 通用帧（ping/hello/tts 状态回声）——子类覆写前调用
    def _common(self, obj: dict) -> bool:
        typ = obj.get("type")
        if typ == "ping":
            self._spawn(self.send_json({"type": "pong"}))
            return True
        if typ == "hello":
            # 回执一份 hello（客户端会跳过非业务帧，无害且便于日志核对）
            self._spawn(self.send_json({
                "type": "hello", "transport": "websocket", "channel": self.channel,
                "audio_params": {"format": "opus", "sample_rate": const.SAMPLE_RATE,
                                 "channels": const.CHANNELS, "frame_duration": 60}}))
            return True
        return False


class SttSession(BaseSession):
    channel = "stt"

    def __init__(self, ws, ctx):
        super().__init__(ws, ctx)
        self._pcm = bytearray()
        self._decoder: Optional[audio.OpusPcmDecoder] = None
        self._dec_err = False
        self._task: Optional[asyncio.Task] = None

    async def on_text(self, raw: str) -> None:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return
        if self._common(obj):
            return
        if obj.get("type") == "listen":
            state = obj.get("state")
            if state == "start":
                self._pcm.clear()          # 新一轮 utterance（含 realtime restart）
            elif state == "stop":
                await self._transcribe_and_reply()
            # detect/cancel: 无操作（cancel 由下一轮 start 清缓冲天然生效）

    async def on_binary(self, data: bytes) -> None:
        if self._decoder is None and not self._dec_err:
            try:
                factory = getattr(self.ctx, "decoder_factory", None)   # 协议测试注入点
                self._decoder = factory() if factory else audio.OpusPcmDecoder()
            except audio.OpusError:
                self._dec_err = True
                logger.error("[STT] opus 解码器不可用（libopus 缺失），回传空文本")
        if self._decoder:
            try:
                self._pcm += self._decoder.decode(data)
            except Exception:
                logger.debug("[STT] 解码异常帧 len=%d", len(data), exc_info=True)

    async def _transcribe_and_reply(self) -> None:
        # 上一轮识别未回时不并发（保 stop 语义单条）
        if self._task and not self._task.done():
            self._task.cancel()
        pcm = bytes(self._pcm)
        self._pcm.clear()
        self._task = asyncio.create_task(self._run(pcm))

    async def _run(self, pcm: bytes) -> None:
        text = ""
        try:
            if pcm:
                text = await asyncio.wait_for(
                    self.ctx.asr.transcribe_pcm(pcm), timeout=const.STT_RESULT_BUDGET_S)
            elif not self._dec_err:
                text = ""       # 静音：契约要求仍回一条 text:""
        except asyncio.TimeoutError:
            logger.warning("[STT] 识别超预算 %ss", const.STT_RESULT_BUDGET_S)
        except asyncio.CancelledError:
            # P1-8：被新一轮 stop 抢占/断连取消——契约 §1.4"每 stop 必回且仅回
            # 一条"不容破例：本 stop 以空文本收束后继续上抛取消。shield 保证
            # 二次取消（关站风暴）下收束帧仍会发出。
            logger.info("[STT] 在飞识别被抢占，本 stop 以空文本收束")
            with contextlib.suppress(Exception):
                await asyncio.shield(self._reply_stt(""))
            raise
        except Exception:
            logger.exception("[STT] 识别异常")
        await self._reply_stt(text)

    async def _reply_stt(self, text: str) -> None:
        await self.send_json({"type": "stt", "text": text or ""})

    async def on_close(self) -> None:
        # F7a：断连即取消在飞转写（Wi-Fi 抖动重连风暴不占死 executor 线程）
        if self._task is not None and not self._task.done():
            self._task.cancel()


class TtsSession(BaseSession):
    channel = "tts"

    def __init__(self, ws, ctx):
        super().__init__(ws, ctx)
        self._gen = 0
        self._task: Optional[asyncio.Task] = None

    async def on_text(self, raw: str) -> None:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return
        if self._common(obj):
            return
        typ = obj.get("type")
        if typ == "tts":
            state = obj.get("state")
            text = str(obj.get("text", "")).strip()
            # 宽容：detect 为主形态；sentence_start/无状态带 text 也接单条合成（stop 回显忽略）
            if state in ("detect", "sentence_start", None) and text:
                self._start_stream(text)
        # listen stop 等在 tts 通道无义务响应（客户端不收口）

    async def on_binary(self, data: bytes) -> None:
        pass    # tts 通道无上行音频（卫星形态）

    def _start_stream(self, text: str) -> None:
        # 新 detect 到达 → 旧流作废（generation 守卫，不发孤儿帧）
        self._gen += 1
        gen = self._gen
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._stream(text, gen))

    async def _stream(self, text: str, gen: int) -> None:
        deadline = time.monotonic() + const.TTS_STREAM_BUDGET_S
        sent_any = False
        try:
            # F5：预算必须覆盖「生成器挂起」——逐包用剩余预算做 wait_for，
            # native 合成卡死也能按点收束（finally 的 stop 义务不变）。
            it = self.ctx.tts.stream_opus(text).__aiter__()
            while True:
                remain = deadline - time.monotonic()
                if gen != self._gen or remain <= 0:
                    return                       # 被顶替/超预算：静默终止（stop 由 finally 统一收束）
                try:
                    pkt = await asyncio.wait_for(it.__anext__(), timeout=remain)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    logger.warning("[TTS] 合成流停滞超预算，截断收束")
                    return
                if not await self.send_bytes(pkt):
                    return
                sent_any = True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[TTS] 合成流异常（以 stop 收束）")
        finally:
            # 仅当自己仍是当前代时才收束（防被顶替后发孤儿 stop）
            if gen == self._gen:
                await self.send_json({"type": "tts", "state": "stop"})
                if not sent_any and text:
                    logger.warning("[TTS] 空音频收束（模型未就绪？）: %r", text[:30])

    async def on_close(self) -> None:
        if self._task:
            self._task.cancel()


class LlmSession(BaseSession):
    channel = "llm"

    def __init__(self, ws, ctx):
        super().__init__(ws, ctx)
        self._task: Optional[asyncio.Task] = None

    async def on_text(self, raw: str) -> None:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return
        if self._common(obj):
            return
        if obj.get("type") == "listen" and obj.get("state") == "detect":
            text = str(obj.get("text", ""))
            if self._task and not self._task.done():
                self._task.cancel()
            self._task = asyncio.create_task(self._turn(text))

    async def on_binary(self, data: bytes) -> None:
        pass    # llm 通道禁 binary（契约 §4）

    async def _turn(self, text: str) -> None:
        await self.send_json({"type": "text", "state": "start"})
        reply_text = const.FALLBACK_TEXT
        streamed = False

        async def _on_sentence(sent: str) -> None:
            # P2-15：LLM 流式逐句下传（start 已发；end 帧永远由本协程收束）
            nonlocal streamed
            # data 字段名是客户端硬约束（llm_transport 聚合读 data），勿改
            if await self.send_json({"type": "text", "state": "sentence_end", "data": sent}):
                streamed = True

        try:
            reply = await asyncio.wait_for(
                self.ctx.pipeline.handle(text, origin=self.device_hint,
                                         on_sentence=_on_sentence),
                timeout=const.LLM_TURN_BUDGET_S)
            reply_text = reply.text or const.FALLBACK_TEXT
            if getattr(reply, "streamed", False):
                streamed = True
        except asyncio.TimeoutError:
            logger.warning("[LLM] 回合超预算 %ss", const.LLM_TURN_BUDGET_S)
        except asyncio.CancelledError:
            await self.send_json({"type": "text", "state": "end"})
            raise
        except Exception:
            logger.exception("[LLM] 处理异常")
        if not streamed:
            from .agent import _sentences
            for sent in _sentences(reply_text):
                if not await self.send_json({"type": "text", "state": "sentence_end", "data": sent}):
                    break
        await self.send_json({"type": "text", "state": "end"})

    async def on_close(self) -> None:
        if self._task:
            self._task.cancel()


SESSION_BY_CHANNEL = {"stt": SttSession, "tts": TtsSession, "llm": LlmSession}
