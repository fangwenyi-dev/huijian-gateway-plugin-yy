"""STT 通道传输（v1.0.64 深审批5：全量迁移 TTS 已验证的锁+超时+清算三件套）。

M9/H7/M10 三案同源（报告 2026-09-23）：全卫星共享一条 SttTransport，旧实现
无事务锁、发送零超时、空/超时也回 SUCCESS——并发听写互相顶替帧序、悬挂
writer 永久阻塞、假成功喂管线"空话"。TTS 侧 v1.0.45 已把同病灶三件套收口
（_request_lock + wait_for + restart 清算），STT 从未迁移，本文件补齐。
await_message 保留为兼容壳（新链路一律走 recognize）。
"""
import asyncio
import logging
import random
import time

import anyio
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import Dict, EntryAuthFailedError, get_entry_data
from .ws_transport import WsTransport

_LOGGER = logging.getLogger(__name__)
ATTR_ENDPOINT = "stt_endpoint"
ATTR_TRANSPORT = "stt_transport"

_SEND_TIMEOUT_S = 10.0
_STALE_DRAIN_BUDGET_S = 0.5
# v1.0.89（F5-a2）：开轮后"首帧音频"等待窗。设备侧 abort/自触发后会留下**一帧都
# 不推**的空轮（现场 18:09~18:14 反复四条 `STT 事务被外部取消（发送相 在途4.1s
# 已发0帧）`；4.0/4.1s 既不是本仓常量也不是 core 常量，而是"下一个拆轮者到来"的
# 时刻——即这轮一直自己挂着）。挂着的代价不止延迟：事务不收口 ⇒ 它的回包在
# buffer-0 内存流上堵死 reader ⇒ 30s 交付判死换连（18:14:14.84 → 18:14:44.841
# 恰好 30.0s）⇒ 与条目 reload 撞车，就是现场"播报中再唤醒就熔断"的放大器之一。
_FIRST_CHUNK_TIMEOUT_S = 3.0
# v1.0.92：服务端欢迎帧 stt_proto >= 本值才启用轮次身份（客户端 mint，回执配对）。
# 与 TTS 的 _TTS_PROTO_STREAM_ID 对偶；未协商=0 全程旧语义逐字节不变（fail-open）。
_STT_PROTO_RID = 2


def get_entry_transport(hass: HomeAssistant, entry: ConfigEntry) -> "SttTransport":
    """Set up from a config entry."""
    endpoint: str | None = entry.data.get(ATTR_ENDPOINT)
    if not endpoint:
        raise EntryAuthFailedError(hass, entry)

    this_data: dict = get_entry_data(hass, entry)
    transport: SttTransport | None = this_data.get(ATTR_TRANSPORT)
    if transport and transport.endpoint == endpoint and transport.available:
        return transport

    _LOGGER.info(
        "Creating new SttTransport for entry: %s %s", entry.entry_id, entry.title
    )
    transport = SttTransport(hass, entry, endpoint, ATTR_ENDPOINT, _LOGGER)
    this_data[ATTR_TRANSPORT] = transport
    return transport


class SttTransport(WsTransport):
    _transport_type = "stt"

    def __init__(self, hass, entry, endpoint, attr_endpoint, logger=None):
        super().__init__(hass, entry, endpoint, attr_endpoint, logger)
        # M9：整段对话上锁串行是唯一解（v1.0.45 TTS 定案原文同病同理）。
        self._request_lock = asyncio.Lock()
        # v1.0.89（F5-a1）认领判定＝"本通道的对话锁是否被持有"。recognize 全程持
        # 锁（含被取消时的上下文管理器释放），所以 `locked()` 为真 ⇔ 此刻有一个
        # 消费者在收；为假时到达的消息必属**已消失的上一轮**，在交付点就地丢弃
        # （连 reader 都不进）。与 v1.0.88 TTS 的 `_round_active` 同效，但零新增
        # 生命周期状态——不再有"认领未释放"这类二次泄漏面。
        self._unclaimed_total = 0
        # ── v1.0.92 STT 轮次身份（stt_proto+rid，TTS Stage-1 的对偶）────────
        # 真机定案（2026-09-17 现场 + 台架复现）：锁判只回答"有没有消费者"，
        # 不回答"回执属于哪一轮"——旧轮（发送相被拆、事务已消失）的转写在
        # 服务端 ≤52s 预算后迟到，恰好落进**新轮持锁**的队列，被新轮消费循环
        # 认成本轮文本（张冠李戴），新轮自己的回执随后无人认领再堵 buffer-0
        # reader → 30s 交付判死换连。现场"stt 通道 30s 交付判死换连仍在"即此。
        # 现：协商连接上 mint rid（随机基+单调，0=legacy 哨兵），listen
        # start/stop 携带、服务端回显；交付点(_on_incoming)与消费点双闸配对，
        # 明确别轮回执**就地丢弃、不拆连接**——30s 判死降级回真僵尸末位兜底。
        self._proto = 0
        self._rid = random.getrandbits(31) | 1
        self._round_rid = 0            # 本轮 rid（0=未协商/未开轮）
        self._crossed_total = 0        # 别轮回执丢弃计数（限频 WARN）

    def _next_rid(self) -> int:
        rid = self._rid
        self._rid = rid + 1 if rid < 0xFFFFFFFF else 1
        return rid

    async def _create_streams(self):
        await super()._create_streams()
        # 协商结果属"这一条连接"：换连即归零，等新连接欢迎帧重新申报
        # （镜像 TTS v1.0.88；本轮 _round_rid 不动——轮若还活着由消费端配对）。
        self._proto = 0

    def _on_server_settings(self, data) -> None:
        """欢迎帧旁路（基类不进业务流）：捕获 stt_proto 协商。"""
        try:
            proto = int(data.get("stt_proto") or 0)
        except (TypeError, ValueError):
            proto = 0
        if proto != self._proto:
            _LOGGER.info("huijian STT 协议代次: %s → %s（轮次身份%s）",
                         self._proto, proto,
                         "启用" if proto >= _STT_PROTO_RID else "不启用")
            self._proto = proto

    def _rid_of(self, item) -> int:
        """消息携带的轮次身份（宽容：畸形/缺失=0=legacy 形态）。"""
        try:
            got = item.get("rid") if isinstance(item, dict) else \
                getattr(item, "rid", None)
            got = int(got or 0)
        except (TypeError, ValueError):
            return 0
        return got if 0 < got < (1 << 32) else 0

    def _on_incoming(self, item):
        """交付前过滤（基类钩子）：无在途事务 ⇒ 就地丢弃，返回 True=丢。"""
        if not self._request_lock.locked():
            self._unclaimed_total += 1
            if self._unclaimed_total == 1 or self._unclaimed_total % 20 == 0:
                self.logger.warning(
                    "STT 通道无在途事务，就地丢弃消息（上一轮已消失：被取消/超时未收口）"
                    "，累计 %d 条——残包不再堵 reader，避免 30s 交付判死把整条通道换连",
                    self._unclaimed_total)
            return True
        # v1.0.92：锁被持有只证明"有消费者"，不证明"这条回执是他的"。
        # 协商轮上 rid 明确配不上 ⇒ 属旧轮债，交付点就地丢（若放行，它会
        # 挂进本轮 reader 队列被本轮认走——就是 30s 判死链的第一环）。
        # rid=0（旧服务端/未协商/旧形态）fail-open 放行，保护弱一档不哑管线。
        round_rid = getattr(self, "_round_rid", 0)
        if round_rid and getattr(item, "type", None) in ("stt", "tts"):
            try:
                raw = item.get("rid") if isinstance(item, dict) else \
                    getattr(item, "rid", None)
                got = int(raw or 0)
            except (TypeError, ValueError):
                got = 0
            if got and got != round_rid:
                self._crossed_total = getattr(self, "_crossed_total", 0) + 1
                if self._crossed_total == 1 or self._crossed_total % 20 == 0:
                    self.logger.warning(
                        "STT 旧轮回执配错门（rid=%s 本轮=%s），就地丢弃不拆连，"
                        "累计 %d 条", got, round_rid, self._crossed_total)
                return True
        return False

    def _drain_stale(self) -> int:
        """事务前排净 reader 上已排队的残帧/残转录（上一轮超时/取消遗留）。"""
        drained = 0
        deadline = time.monotonic() + _STALE_DRAIN_BUDGET_S
        while time.monotonic() < deadline:
            try:
                self._recv_reader.receive_nowait()
            except (anyio.WouldBlock, anyio.EndOfStream, anyio.ClosedResourceError):
                break
            drained += 1
        if drained:
            self.logger.warning(
                "STT 连接上清掉上一轮残留 %d 条（此前有识别被取消/超时），已丢弃",
                drained)
        return drained

    async def recognize(self, chunks, timeout: int = 60):
        """一整轮听写事务（hello→start→opus 帧→stop→收转录）。

        返回 (text, error)：
          - (str|None, None)：正常收口，"" 为合法空识别；
          - (_, error 非空)：连接/发送/超时故障——调用方必须报
            SpeechResultState.ERROR。旧形态超时/None 也 SUCCESS（H7），
            管线播"空话"假成功；替身恒 wait_for(3) 的 e2e 测不出真机悬挂，
            本方法栈就是判据本体。
        发送段一律 wait_for 可超时（悬挂 writer 永堵=持锁永堵，TTS :118
        原律）；任何非正常收口都 restart_connection 断连清算，残帧不跨轮。
        v1.0.92：协商轮上全程携带 rid（start/stop 出、回执配对入），旧轮迟到
        回执在交付点/消费点双双不认——**不拆连接**地解毒（30s 判死退回真僵尸
        兜底位）。未协商（旧服务端）逐字节旧形态。
        """
        async with self._request_lock:
            if not await self.ensure_connected():
                return None, "WebSocket connection unavailable"
            self._drain_stale()
            # rid mint（fail-open：任何异常=0=旧协议，绝不打死识别链）。
            rid = 0
            try:
                if self._proto >= _STT_PROTO_RID:
                    rid = self._next_rid()
                    self._round_rid = rid
            except Exception:  # noqa: BLE001（含桩缺符号）
                rid = 0
            frames = 0
            # v1.0.73 归因链·④钉年龄基准（与卫星端③钉成对）。用 loop.time 而非
            # time.monotonic：recognize 会被测试以最小命名空间提取执行（v1043/v1045
            # 纪律），asyncio 是本法已有硬依赖，不为此引入新全局符号。
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            try:
                await asyncio.wait_for(self.send_hello(), _SEND_TIMEOUT_S)
                await asyncio.wait_for(
                    self.send_message({"type": "listen", "state": "start",
                                       **({"rid": rid} if rid else {})}),
                    _SEND_TIMEOUT_S)
                # v1.0.89（F5-a2）：**首帧单独设 3s 窗**。开轮后设备一帧不推
                # （barge-in/abort 后静默、自触发空轮）时，旧形态抱着这条事务等
                # "下一个拆轮者"（现场 4.1s 悬挂 ×N），悬挂期间它的回包还会堵死
                # buffer-0 reader 直到 30s 交付判死换连。现当场以 stop 收口、按
                # **契约内的合法空识别**返回（见本函数 docstring）。后续帧不设窗：
                # 稳态推流节拍由设备决定，误砍=丢音频（宁慢勿砍）。
                it = chunks.__aiter__()
                try:
                    chunk = await asyncio.wait_for(
                        it.__anext__(), _FIRST_CHUNK_TIMEOUT_S)
                except StopAsyncIteration:
                    chunk = None
                except asyncio.TimeoutError:
                    self.logger.warning(
                        "STT 开轮后 %.1fs 无上行音频（设备未推流/轮已被打断）"
                        "→ 就地 stop 收口为空识别，不再悬挂等拆轮者",
                        _FIRST_CHUNK_TIMEOUT_S)
                    try:
                        await it.aclose()
                    except Exception:  # noqa: BLE001 收口尽力而为，不掩盖主因
                        pass
                    try:
                        await asyncio.wait_for(
                            self.send_message({"type": "listen", "state": "stop",
                                               **({"rid": rid} if rid else {})}),
                            _SEND_TIMEOUT_S)
                    except Exception as err:  # noqa: BLE001
                        self.logger.debug("STT 空轮收口 stop 未送达: %s", err)
                    return "", None
                while chunk is not None:
                    await asyncio.wait_for(self.send_message(chunk),
                                           _SEND_TIMEOUT_S)
                    frames += 1
                    try:
                        chunk = await it.__anext__()
                    except StopAsyncIteration:
                        chunk = None
                await asyncio.wait_for(
                    self.send_message({"type": "listen", "state": "stop",
                                       **({"rid": rid} if rid else {})}),
                    _SEND_TIMEOUT_S)
            except asyncio.CancelledError:
                # v1.0.73 归因链·④钉（发送相）：外部取消=HA 管线把这轮拆了
                # （新轮接管/barge-in/实体移除）——下一轮的"清掉残留"从此有据
                # 可查；取消必须原样上抛（吞了=任务取消纪律被破）。
                self.logger.warning(
                    "STT 事务被外部取消（发送相 在途%.1fs 已发%d帧）——本轮被 HA 侧拆掉",
                    asyncio.get_running_loop().time() - t0, frames)
                raise
            except Exception as err:  # noqa: BLE001（含 TimeoutError）
                self.logger.warning("STT 发送段失败（已发 %d 帧）: %s", frames, err)
                await self.restart_connection(f"STT 发送段失败: {err}")
                return None, f"Send failed: {err}"
            _LOGGER.debug("STT 发送完成：%d 帧，等待转录", frames)
            text = None
            claimed_gen = getattr(self, "_conn_gen", None)   # 桩缺不判（旧语义）
            try:
                with anyio.fail_after(timeout):
                    async for data in self._recv_reader:
                        if data.type in ["stt", "tts"]:
                            # v1.0.92 轮配对（消费点兜底；交付点已丢过一道）：
                            # 明确别轮回执→不认、继续等本轮；rid=0 legacy fail-open
                            # 认（宁弱一档保护不哑管线，镜像 TTS paired 语义）。
                            if rid:
                                try:
                                    got = int(getattr(data, "rid", 0) or 0)
                                except (TypeError, ValueError):
                                    got = 0
                                if got and got != rid:
                                    self._crossed_total += 1
                                    if (self._crossed_total == 1
                                            or self._crossed_total % 20 == 0):
                                        self.logger.warning(
                                            "STT 消费点跳过别轮回执（rid=%s 本轮=%s，"
                                            "累计 %d）", got, rid, self._crossed_total)
                                    continue
                            text = data.text
                            break
                        if claimed_gen is not None and \
                                getattr(self, "_conn_gen", claimed_gen) != claimed_gen:
                            # v1.0.92（镜像 TTS :271-277）：轮中换连——本轮 start/stop
                            # 随旧连接作废，服务端从没见过这轮，回执永不到；当场
                            # 错误收口，不再白等 timeout 窗（僵尸事务=堵 reader 元凶）。
                            self.logger.warning(
                                "STT 轮中连接被更换（gen %s→%s），本轮按错误收口",
                                claimed_gen, getattr(self, "_conn_gen", None))
                            break
            except asyncio.CancelledError:
                # v1.0.73 归因链·④钉（等转录相）：转录还没到、这轮先被拆——
                # 稍后网关回话时消费端已消失，30s 交付判死随之出现，闭环成链。
                self.logger.warning(
                    "STT 事务被外部取消（等转录相 在途%.1fs 已发%d帧）——本轮被 HA 侧拆掉",
                    asyncio.get_running_loop().time() - t0, frames)
                raise
            except TimeoutError:
                self.logger.warning("STT 等待转录超时（%ds，已发 %d 帧）",
                                    timeout, frames)
                return None, "Response timeout"
            except Exception as err:  # noqa: BLE001 reader 被关闭等
                self.logger.warning("STT 收取异常: %s", err)
                return None, f"Receive failed: {err}"
            finally:
                if text is None:
                    await self.restart_connection(
                        "STT 未以转录消息收口（超时/异常），断连清算残留")
            if text is None and claimed_gen is not None and \
                    getattr(self, "_conn_gen", claimed_gen) != claimed_gen:
                # 换连 break：显式 error（不得 (None,None) 让调用方猜成功）。
                return None, "Connection replaced mid-round"
            return text, None

    async def await_message(self, timeout: int = 60):
        """兼容壳（新链路一律走 recognize；保留仅防第三方直接调用）。"""
        try:
            with anyio.fail_after(timeout):
                async for data in self._recv_reader:
                    if data.type in ["stt", "tts"]:
                        yield data
                        break
        except RuntimeError as exc:
            self.logger.info(str(exc), exc_info=True)
        except TimeoutError:
            yield Dict(error="Response timeout")

    async def async_remove_entry(self):
        entry = self.entry
        this_data: dict = get_entry_data(self.hass, entry)
        transport: SttTransport | None = this_data.pop(ATTR_TRANSPORT, None)
        self.logger.info(
            "Remove entry from STT transport: title=%s id=%s",
            entry.title,
            entry.entry_id,
        )
        if transport:
            await transport.stop("Remove entry")
