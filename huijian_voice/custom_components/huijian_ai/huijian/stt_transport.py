"""STT 通道传输（v1.0.64 深审批5：全量迁移 TTS 已验证的锁+超时+清算三件套）。

M9/H7/M10 三案同源（报告 2026-09-23）：全卫星共享一条 SttTransport，旧实现
无事务锁、发送零超时、空/超时也回 SUCCESS——并发听写互相顶替帧序、悬挂
writer 永久阻塞、假成功喂管线"空话"。TTS 侧 v1.0.45 已把同病灶三件套收口
（_request_lock + wait_for + restart 清算），STT 从未迁移，本文件补齐。
await_message 保留为兼容壳（新链路一律走 recognize）。
"""
import asyncio
import logging
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

    def _on_incoming(self, item):
        """交付前过滤（基类钩子）：无在途事务 ⇒ 就地丢弃，返回 True=丢。"""
        if self._request_lock.locked():
            return False
        self._unclaimed_total += 1
        if self._unclaimed_total == 1 or self._unclaimed_total % 20 == 0:
            self.logger.warning(
                "STT 通道无在途事务，就地丢弃消息（上一轮已消失：被取消/超时未收口）"
                "，累计 %d 条——残包不再堵 reader，避免 30s 交付判死把整条通道换连",
                self._unclaimed_total)
        return True

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
        """
        async with self._request_lock:
            if not await self.ensure_connected():
                return None, "WebSocket connection unavailable"
            self._drain_stale()
            frames = 0
            # v1.0.73 归因链·④钉年龄基准（与卫星端③钉成对）。用 loop.time 而非
            # time.monotonic：recognize 会被测试以最小命名空间提取执行（v1043/v1045
            # 纪律），asyncio 是本法已有硬依赖，不为此引入新全局符号。
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            try:
                await asyncio.wait_for(self.send_hello(), _SEND_TIMEOUT_S)
                await asyncio.wait_for(
                    self.send_message({"type": "listen", "state": "start"}),
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
                            self.send_message({"type": "listen", "state": "stop"}),
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
                    self.send_message({"type": "listen", "state": "stop"}),
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
            try:
                with anyio.fail_after(timeout):
                    async for data in self._recv_reader:
                        if data.type in ["stt", "tts"]:
                            text = data.text
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
