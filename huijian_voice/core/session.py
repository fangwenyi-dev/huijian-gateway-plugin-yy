"""三通道会话状态机（《小智协议子集-服务器契约.md》逐条实现）。

通道语义回顾（集成侧 huijian_ai 源码实证后的服务器义务）：
  stt  : hello 可收可回；listen start 清缓冲；binary=裸 opus(16k/mono/60ms)；
         listen stop ⇒ 必须回且仅回一条 {"type":"stt","text":…}（静音也回 text:""）；
         此通道禁止 binary、禁发 tts/text 帧。
  tts  : 无 hello；收 {"type":"tts","state":"detect","text":…} ⇒ 流裸 opus binary，
         收束发 {"type":"tts","state":"stop"}；新 detect 到达丢弃旧流且不得有孤儿帧；
         任何 JSON 帧不得含 "error" 键；每 detect 必有 stop。
         v1.0.88 边带身份（proto=2，建连欢迎帧申报）：detect 可带 "rid":<u32>
         （集成 mint 的下行流标识，谁可能被污染谁 mint）。带 rid ⇒ 本流任何帧
         之前先回声 {"state":"stream_start","rid":N}，收束 {"state":"stop","rid":N}；
         消费端据此"未同步到自己 rid 前全丢"。rid 缺省/畸形=0=旧协议逐字节不变。
         不变量（锁内归属守卫保证）：stream_start(N) 之后不可能再有旧流帧上栈。
  llm  : 收 listen detect(mode=prompt,text) ⇒ {"type":"text","state":"start"} →
         逐句 {"type":"text","state":"sentence_end","data":…} →
         终帧 {"type":"text","state":"end"}（客户端 end 判定先于 type，且 end 必发）。
通用：ping→pong；不主动 close；预算：stt 结果 52s / tts 逐帧间隙 52s+整轮总闸
660s（v1.0.83，见 const ⑧块）/ llm 回合 50s；
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

# v1.0.65（TTS 深审 F1）：tts detect 文本硬上限（字）。正常播报远小于此
# （场景话术 ≤ 数十余字）；4000 字 ≈ 十几分钟音频，已是预算外极限形态。
_TTS_TEXT_CAP = 4000


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

    # ── 发送三态与归属守卫（v1.0.88 下行流标识 Stage 1 的地基）──────────────
    # True=已入栈；False=发送失败（对端停读/断连）；None=**归属守卫判定本帧
    # 所属流已作废**（新 detect 已顶替，一帧都不许多发）。三态必须可分：
    # False 走"按断连处理"的既有路径，None 走"顶替截断"路径（不发孤儿 stop）。
    #
    # 为什么守卫必须在锁内、紧邻真实 enqueue（这是边带身份 1b 成立的前提）：
    # 集成侧的判定是"见到本流 stream_start 之前的帧全丢"，它**辨不出**
    # stream_start 之后才冒出来的旧流残帧（帧形制不变、无逐帧身份）。若在
    # 「锁外查代际 → 进锁 send」，中间那一次 await 就是竞态窗：新 detect 在
    # 此刻顶替、新流 stream_start 先进锁发出，旧流随后这一帧就成了"ack 之后
    # 的残段"，现场形态=播报头部混半句旧音频。守卫下沉后，旧流任何一帧要么
    # 在 stream_start 之前上栈（被集成同步门前丢弃），要么被本守卫当场拦死
    # ——二者必居其一，无需 drain/屏障等待（零首帧延迟）。
    def _send_guard(self, guard) -> bool:
        return True if guard is None else bool(guard())

    async def send_json(self, obj: dict, guard=None):
        return await self._send(lambda: self.ws.send_str(_json(obj)), guard)

    async def send_bytes(self, data: bytes, guard=None):
        return await self._send(lambda: self.ws.send_bytes(data), guard)

    # v1.0.65（TTS 深审 F2）：v1.0.45 的 wait_for 只包住了生成侧，发送侧此前
    # 无闸——aiohttp 对端零窗口（连着但不读）时 ws.send_* 在内部 drain 无限期
    # 挂起，且挂在 _send_lock 内：同会话 pong/hello 全部堵死并堆积；ws 又是
    # heartbeat=None，服务端零自保，恢复全靠对端重连。与生成侧同构收口：
    # 发送有界，超时=按断连处理（truncated 语义由各调用点承接）。正常帧发送
    # 微秒级，触发即确凿异常，WARN 不致刷屏。
    # v1.0.70（深审⑧）：5.0→3.0。预算对账=整流 52 + 在飞帧 ≤3 + 收口 stop
    # ≤3 = 58 ≤ 客户端 60-2s 网络余量；此值是求和项，改动须同看 const ⑧注释。
    _SEND_TIMEOUT_S = 3.0

    async def _send(self, coro_fn, guard=None):
        async def _locked():
            async with self._send_lock:
                if self.ws.closed:
                    return False
                # 归属判定与真实 enqueue 同持锁、之间零 await（见上注释）
                if not self._send_guard(guard):
                    return None
                await coro_fn()
                return True
        try:
            return await asyncio.wait_for(_locked(), self._SEND_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("[%s] 发送超 %ss（对端停读/积压），按断连处理",
                           self.channel or "?", self._SEND_TIMEOUT_S)
            return False
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
        # v1.0.88（Stage 1 边带身份）：本会话当前有效流的线上标识。由**集成
        # mint、服务端回显**（谁可能被污染谁 mint：混入的受害者是集成）。
        # 0 = 客户端未带 rid（旧集成）→ 整条路径与本变更前逐字节一致。
        self._rid = 0
        self._task: Optional[asyncio.Task] = None

    @staticmethod
    def _parse_rid(obj: dict) -> int:
        """宽容解析 detect 携带的 rid（契约：未知键忽略、值形态不可信）。

        u32 且 0 作 legacy 哨兵，故越界/非数字/负数一律折成 0（按旧协议跑），
        绝不因一个畸形键把播报打死（fail-open 方向恒为"有声音"）。
        """
        try:
            rid = int(obj.get("rid") or 0)
        except (TypeError, ValueError):
            return 0
        return rid if 0 < rid < (1 << 32) else 0

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
            # 深审 R2 #10：`get(k, "")` 只在**键缺失**时兜底——{"text": null}
            # 返回 None，str(None)="None" 会被合成**念出字面 "None"**。`or ""`
            # 把 null/False/0 全折叠为空（与"空文本 detect 零帧收束"契约对齐）。
            text = str(obj.get("text") or "").strip()
            # v1.0.65（TTS 深审 F1）：detect 文本硬上限。max_msg_size=64KB 单帧
            # 可载 ~2 万字无标点文本：generate 非流式（整段合成完才返回）且
            # executor 线程不可取消——一段巨型合成持 _gen_lock 分钟级，期间全部
            # 播报/试听堵锁各自 55s 预算耗尽（全线 truncated），8 线程默认池堵满
            # 连带 asr 同池排队（STT 停摆），数百 MB 样本驻留（OOM）。默认
            # require_token=false，LAN 未认证单帧即可触发。截断优于拒绝：播报
            # 保序出声，WARN 留痕对账。
            cap_truncated = False
            if len(text) > _TTS_TEXT_CAP:
                logger.warning("[TTS] detect 文本 %d 字超上限 %d，截断合成"
                               "（防单帧全栈 DoS）: %r",
                               len(text), _TTS_TEXT_CAP, text[:30])
                text = text[:_TTS_TEXT_CAP]
                # v1.0.70（深审②根治）：cap 截断也是"缺尾巴"——此前只 WARN，
                # 半截音频按"正常收束"交回 → HA 以**原文哈希**把缺尾音频写进
                # 消息缓存（内存+落盘、跨重启），同句永久只念前段且不自愈。
                # 带旗收口 → 集成以 error 收口 → core 异常路径 pop 缓存，
                # 播报照常出声（前段），只是不再投毒。
                cap_truncated = True
            # 宽容：detect 为主形态；sentence_start/无状态带 text 也接单条合成（stop 回显忽略）
            # 审查修复（2026-09-21）：不再以 `and text` 静默忽略空文本 detect——自家
            # 契约「每 detect 必有 stop」，旧形态空 detect（tts.speak message=""，
            # core schema 不拦空串）不出帧不发 stop，客户端 fail_after(60) 白等
            # 一整分钟且全程持有播报通道 _request_lock。空文本统一走整流：
            # split 出 0 句 → 零帧 → 干净 stop，顶替语义也一并保住。
            if state in ("detect", "sentence_start", None):
                # v1.0.88：detect 可带 rid（集成 mint 的下行流标识）→ 本流所有
                # 控制帧回显同一 rid；不带/畸形=0=legacy 路径，行为与旧版一致。
                self._start_stream(text, cap_truncated, self._parse_rid(obj))
        # listen stop 等在 tts 通道无义务响应（客户端不收口）

    async def on_binary(self, data: bytes) -> None:
        pass    # tts 通道无上行音频（卫星形态）

    def _start_stream(self, text: str, cap_truncated: bool = False,
                      rid: int = 0) -> None:
        # 新 detect 到达 → 旧流作废（generation 守卫，不发孤儿帧）
        self._gen += 1
        gen = self._gen
        self._rid = rid
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._stream(text, gen, cap_truncated, rid))

    async def _stream(self, text: str, gen: int,
                      cap_truncated: bool = False, rid: int = 0) -> None:
        # 归属守卫：本流仍是当前代际才许上栈。判定发生在 _send_lock 临界区内
        # （见 BaseSession._send 注释）——这是"stream_start 之后绝无旧流残帧"
        # 的全部依据，边带身份方案（1b）成立与否就看这一条。
        mine = lambda: gen == self._gen    # noqa: E731（同块内联，勿提函数）
        # v1.0.83（修#1）：旧形态是单一整轮墙钟（52s 到点即截）——长播报
        # （句句正常产出、只是总合成超 52s）被必然腰斩，现场=播报缺尾。
        # 现拆两道窗（对账见 const ⑧ v1.0.83 块）：
        #   • gap_deadline：最大帧间隙窗（TTS_STREAM_BUDGET_S），每成功发出
        #     一帧即重置——只杀"停滞"，不杀"慢而持续"（与云档 _CLOUD_READ/
        #     TOTAL 的拆段哲学同构，v2.1.42 固件心跳语义的源头侧对齐）；
        #   • total_deadline：整轮总闸（TTS_STREAM_TOTAL_BUDGET_S）——帧帧
        #     合法但永不停产的僵尸流由它有界收口。
        now0 = time.monotonic()
        gap_deadline = now0 + const.TTS_STREAM_BUDGET_S
        total_deadline = now0 + const.TTS_STREAM_TOTAL_BUDGET_S
        sent_any = False
        n_frames = n_bytes = 0
        engine: dict = {}
        it = None
        # v1.0.55（深审定案②）：本轮音频是否"缺尾巴"。一切非自然早退
        # （超预算/停滞/断连/合成异常/引擎自报半途收束）都必须置位——
        # stop 帧带 truncated 声明，集成侧据此以 error 收口，HA 才不会被
        # 截断音频毒化消息哈希缓存（内存+落盘、跨重启命中）。顶替/取消路
        # 径本来就不发 stop（gen 守卫），不在此列。
        # v1.0.70（深审②）：cap 截断（on_text 判定点）从出生就带旗。
        truncated = cap_truncated
        try:
            # ── v1.0.88（Stage 1 边带身份）：本流的一切帧之前先回声 stream_start
            # 集成侧的门是"未同步到自己 rid 前，binary 与 stop 一律丢弃"，故本
            # ack 必须先于本流任何一帧上栈：它与帧发送共用 _send_lock + 同一
            # 归属守卫，先后次序在锁内锁死（见 BaseSession._send 注释）。
            # rc=None=本流在 ack 前已被顶替（静默退场，不发孤儿 stop）；
            # rc=False=对端停读/断连 → 与帧失败同径早退（后续帧也发不出去）。
            if rid:
                rc = await self.send_json(
                    {"type": "tts", "state": "stream_start", "rid": rid}, mine)
                if rc is None:
                    logger.warning("[TTS] 旧流在身份声明前即被顶替（rid=%s）/ %r",
                                   rid, text[:30])
                    return
                if not rc:
                    truncated = True
                    logger.warning("[TTS] stream_start(rid=%s) 发送失败——本流按"
                                   "断连收束（集成侧同步超时后 fail-open）: %r",
                                   rid, text[:30])
                    return
            # F5：预算必须覆盖「生成器挂起」——逐包用剩余预算做 wait_for，
            # native 合成卡死也能按点收束（finally 的 stop 义务不变）。
            it = self.ctx.tts.stream_opus(text, engine_out=engine).__aiter__()
            while True:
                now = time.monotonic()
                if gen != self._gen:
                    # v1.0.45：顶替截断必须留痕——现场"播报下发 N 帧"里 N 小于
                    # 整句应有帧数、又无别的告警行时，唯一解释就是这条。
                    logger.warning("[TTS] 旧流被新播报顶替截断：已发 %d 帧 / %r",
                                   n_frames, text[:30])
                    return
                if now >= total_deadline:
                    truncated = True
                    logger.warning("[TTS] 整流超预算截断（整轮总闸 %ss）：已发 %d 帧 / %r",
                                   const.TTS_STREAM_TOTAL_BUDGET_S, n_frames, text[:30])
                    return
                remain = min(gap_deadline, total_deadline) - now
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
                rc = await self.send_bytes(pkt, mine)
                if rc is None:
                    # 锁内守卫拦下这一帧：新 detect 已顶替，且新流的 stream_start
                    # 只可能在本帧之后上栈（同一把锁）→ 集成侧同步门前必丢它。
                    # 这是"ack 之后绝无旧流残帧"的落地点，勿改成锁外判定。
                    logger.warning("[TTS] 旧流被新播报顶替截断（锁内守卫 rid=%s）："
                                   "已发 %d 帧 / %r", rid, n_frames, text[:30])
                    return
                if not rc:
                    # v1.0.45：连接中断同样点名——半截音频在此对账（对端多半
                    # 正在重连，本端 stop 也会失败，只有这行 WARN 说明真相）。
                    truncated = True
                    logger.warning("[TTS] 播报连接中断，停止下发：已发 %d 帧 / %r",
                                   n_frames, text[:30])
                    return
                sent_any = True
                n_frames += 1
                n_bytes += len(pkt)
                # 帧已产出并发出 = 合成活着：间隙窗重置（心跳语义）。
                gap_deadline = time.monotonic() + const.TTS_STREAM_BUDGET_S
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
                if rid:
                    # v1.0.88：stop 也回显 rid——顶替后晚到的旧流 stop 若不带身份，
                    # 集成会把"上一轮的收口"当成本轮的干净结束（半截音频以
                    # 正常收口进 HA 无 TTL 盘缓存 = v1.0.55 定案②的毒化形态回潮）。
                    stop_frame["rid"] = rid
                if truncated:
                    stop_frame["truncated"] = True
                # v1.0.84（O3）：「每 detect 必有 stop」是尽力而为——漏发时
                # 集成侧靠逐帧间隙窗超时判死自愈（v1.0.83 语义），但归因必须
                # 在此点名，否则现场只见客户端 timeout 不见服务端缺 stop。
                if not await self.send_json(stop_frame):
                    logger.warning("[TTS] stop 帧发送失败（对端已断/积压）——"
                                   "本 detect 缺 stop 收口，集成侧将按间隙窗"
                                   "超时判死自愈: %r", text[:30])
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
                else:
                    # 审查修复：空文本 detect 的零帧收束也要留痕（旧版静默）
                    logger.info("[TTS] 空文本 detect：零帧直接收束 stop")

    async def on_close(self) -> None:
        if self._task:
            self._task.cancel()


class LlmSession(BaseSession):
    channel = "llm"

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
        if obj.get("type") == "listen" and obj.get("state") == "detect":
            text = str(obj.get("text") or "")   # 深审 R2 #10 同闸（LLM detect 形）
            # C1（2026-09-22 审查批）：抢占代次守卫——与 TtsSession._gen、
            # SttSession「抢占方收束」纪律对称。此前被 cancel 的旧 _turn 在
            # except CancelledError 里**无条件**补发 end：客户端 await_message
            # 以 state=="end" 断流，新回合 start 刚出就被这条孤儿 end 掐死
            # （拿回空/半截答案），随后新回合的句子帧再被下一请求错位消费。
            # on_close 不 bump gen——断连路径保持「cancel 也收 end」旧行为。
            self._gen += 1
            gen = self._gen
            if self._task and not self._task.done():
                self._task.cancel()
            self._task = asyncio.create_task(self._turn(text, gen))

    async def on_binary(self, data: bytes) -> None:
        pass    # llm 通道禁 binary（契约 §4）

    async def _turn(self, text: str, gen: int) -> None:
        await self.send_json({"type": "text", "state": "start"})
        reply_text = const.FALLBACK_TEXT
        streamed = False

        async def _on_sentence(sent: str) -> None:
            # P2-15：LLM 流式逐句下传（start 已发；end 帧永远由本协程收束）
            nonlocal streamed
            if gen != self._gen:
                return    # 已抢占：本句属旧回合，不发（孤儿句会把新回合掐流）
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
            # C1：仅「非抢占」取消（on_close 清理，gen 未 bump）才收 end；
            # 被新 detect 顶替时 end 归新回合发，旧回合静默退场并留痕
            # （TtsSession v1.0.45「顶替必须点名」同款纪律）。
            if gen == self._gen:
                await self.send_json({"type": "text", "state": "end"})
            else:
                logger.warning("[LLM] 旧回合被新 detect 顶替，孤儿 end 已抑制 / %r",
                               text[:30])
            raise
        except Exception:
            logger.exception("[LLM] 处理异常")
        if gen != self._gen:
            # 完成竞态窗口：handle 正常返回后、收 end 前被顶替——句子与 end
            # 都不再补发，新回合的 start/sentence/end 自成闭环。
            logger.warning("[LLM] 旧回合收尾前被顶替，剩余帧已抑制 / %r", text[:30])
            return
        if not streamed:
            from .agent import _sentences
            for sent in _sentences(reply_text):
                if gen != self._gen:
                    break
                if not await self.send_json({"type": "text", "state": "sentence_end", "data": sent}):
                    break
        await self.send_json({"type": "text", "state": "end"})

    async def on_close(self) -> None:
        if self._task:
            self._task.cancel()


SESSION_BY_CHANNEL = {"stt": SttSession, "tts": TtsSession, "llm": LlmSession}
