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

    # v1.0.41 审查 S16：单会话 PCM 累积顶（16k×2B/s≈32KB/s，取 5 分钟 ≈9.4MB）。
    # 正常语句远小于此；触顶即异常流（无 stop 狂发），丢最旧保顶不再无界涨。
    _MAX_PCM_BYTES = 32000 * 300

    def __init__(self, ws, ctx):
        super().__init__(ws, ctx)
        self._pcm = bytearray()
        self._decoder: Optional[audio.OpusPcmDecoder] = None
        self._dec_err = False
        self._pcm_overflow_warned = False
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
                # v1.0.41 审查 S16：单会话硬上限——16k s16 ≈32KB/s，开放局域网
                # （require_token=false）下设备狂发二进制帧且不发 stop 时，旧实现
                # 内存无界涨（WS 帧大小上限对分帧累加无效）。溢出丢最旧保最新
                # （与 assist_satellite 音频队列同语义），每会话留痕一次。
                if len(self._pcm) > self._MAX_PCM_BYTES:
                    del self._pcm[:len(self._pcm) - self._MAX_PCM_BYTES]
                    if not self._pcm_overflow_warned:
                        self._pcm_overflow_warned = True
                        logger.warning("[STT] 单会话语音超上限 %dKB，丢最旧保顶（异常长流？）",
                                       self._MAX_PCM_BYTES // 1024)
            except Exception:
                logger.debug("[STT] 解码异常帧 len=%d", len(data), exc_info=True)

    async def _transcribe_and_reply(self) -> None:
        # 上一轮识别未回时不并发（保 stop 语义单条）
        if self._task and not self._task.done():
            # P1-8 契约「每 stop 必回且仅回一条」：被抢占的 stop 必须收束。
            # 2026-09-12 模拟台实锤竞态——若 cancel() 落在任务首次调度前，协程体
            # 根本不会执行，_run 里的 CancelledError 收束分支也就永远不跑，
            # 该 stop 静默丢帧（快速连发可复现）。故改由**抢占方**当场收束，
            # 与任务是否起跑无关。
            self._task.cancel()
            self._task = None
            logger.info("[STT] 在飞识别被抢占，本 stop 以空文本收束")
            with contextlib.suppress(Exception):
                await self._reply_stt("")
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
            # 收束已由抢占方（_transcribe_and_reply）或断连（on_close）负责，
            # 这里不再补发，避免双帧（2026-09-12 竞态修复配套）。
            logger.info("[STT] 在飞识别被取消")
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
        n_frames = n_bytes = 0
        engine: dict = {}
        it = None
        # v1.0.55（深审定案②）：本轮音频是否"缺尾巴"。一切非自然早退
        # （超预算/停滞/断连/合成异常/引擎自报半途收束）都必须置位——
        # stop 帧带 truncated 声明，集成侧据此以 error 收口，HA 才不会被
        # 截断音频毒化消息哈希缓存（内存+落盘、跨重启命中）。顶替/取消路
        # 径本来就不发 stop（gen 守卫），不在此列。
        truncated = False
        try:
            # F5：预算必须覆盖「生成器挂起」——逐包用剩余预算做 wait_for，
            # native 合成卡死也能按点收束（finally 的 stop 义务不变）。
            it = self.ctx.tts.stream_opus(text, engine_out=engine).__aiter__()
            while True:
                remain = deadline - time.monotonic()
                if gen != self._gen:
                    # v1.0.45：顶替截断必须留痕——现场"播报下发 N 帧"里 N 小于
                    # 整句应有帧数、又无别的告警行时，唯一解释就是这条。
                    logger.warning("[TTS] 旧流被新播报顶替截断：已发 %d 帧 / %r",
                                   n_frames, text[:30])
                    return
                if remain <= 0:
                    truncated = True
                    logger.warning("[TTS] 整流超预算截断：已发 %d 帧 / %r",
                                   n_frames, text[:30])
                    return
                try:
                    pkt = await asyncio.wait_for(it.__anext__(), timeout=remain)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    truncated = True
                    logger.warning("[TTS] 合成流停滞超预算，截断收束（已发 %d 帧）: %r",
                                   n_frames, text[:30])
                    return
                if not await self.send_bytes(pkt):
                    # v1.0.45：连接中断同样点名——半截音频在此对账（对端多半
                    # 正在重连，本端 stop 也会失败，只有这行 WARN 说明真相）。
                    truncated = True
                    logger.warning("[TTS] 播报连接中断，停止下发：已发 %d 帧 / %r",
                                   n_frames, text[:30])
                    return
                sent_any = True
                n_frames += 1
                n_bytes += len(pkt)
        except asyncio.CancelledError:
            # 顶替的常态路径：_start_stream 直接 cancel，旧 task 死在任意
            # await 点上、根本走不到循环顶的 gen 检查——不留痕就永远查无此人。
            if gen != self._gen:
                logger.warning("[TTS] 旧流被新播报顶替截断：已发 %d 帧 / %r",
                               n_frames, text[:30])
            raise
        except Exception:
            truncated = True
            logger.exception("[TTS] 合成流异常（以 stop 收束）")
        finally:
            # v1.0.48：四条早退（顶替/超预算/停滞/断连）与 cancel 路径统一在此
            # 确定性关停合成生成器——stream_opus 靠自身 finally 收束本地合成/
            # 云端资源，此前只赌 CPython GC 终值回调，违反自家纪律
            # （tts.py「确定性关停，不赌 GC 时机」）。
            if it is not None:
                with contextlib.suppress(Exception):
                    await it.aclose()
            # 仅当自己仍是当前代时才收束（防被顶替后发孤儿 stop）
            if gen == self._gen:
                if engine.get("truncated"):
                    # 引擎自报半途收束（v1.0.55 云端半途断流等）——
                    # 生成器是"正常耗尽"，本函数各早退位点看不见，必须在此并档。
                    truncated = True
                stop_frame = {"type": "tts", "state": "stop"}
                if truncated:
                    stop_frame["truncated"] = True
                await self.send_json(stop_frame)
                # v1.0.25：成功也留一行——「灯开了不播报」必须能逐跳对账
                # （加载项下发 → 集成收帧 → 卫星推流 → 设备出声），此前成功全静默。
                if sent_any:
                    # 引擎名必上日志：云⇄本地回落=换嗓（云端可配男声、本地
                    # sid18 女声），"第一句男声第二句女声"要一眼可辨。
                    logger.info("[TTS] 播报下发：%s / %d 帧 / %d 字节 / %r",
                                engine.get("engine", "?"),
                                n_frames, n_bytes, text[:30])
                elif text:
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
