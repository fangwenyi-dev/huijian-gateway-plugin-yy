import asyncio
import json
import logging
import time

import aiohttp
import anyio
from anyio.streams.memory import (MemoryObjectReceiveStream,
                                  MemoryObjectSendStream)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from . import Dict

_LOGGER = logging.getLogger(__name__)


class WsTransport:
    """Handles WebSocket transport."""

    _transport_type = ""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        endpoint: str,
        attr_endpoint: str,
        logger=None,
    ):
        self.stop_event = asyncio.Event()
        self.endpoint = endpoint
        self.attr_endpoint = attr_endpoint
        self.reconnect_times = 0
        self.should_reconnect = True
        self._current_ws = None
        self._idle_timeout = 180
        self._last_activity_time = 0
        self._is_connected = False

        self.hass = hass
        self.entry = entry
        self.logger = logger or _LOGGER
        self._connection_lock = asyncio.Lock()
        # v1.0.40：连接循环句柄（防重复 spawn，见 ensure_connected 说明）
        self._loop_task = None
        # v1.0.40：叫醒信号——要求正在退避的循环立即重连（见 _wait_backoff）
        self._connect_now = asyncio.Event()

        self._recv_writer: MemoryObjectSendStream = None  # type: ignore
        self._recv_reader: MemoryObjectReceiveStream = None  # type: ignore
        self._send_writer: MemoryObjectSendStream = None  # type: ignore
        self._send_reader: MemoryObjectReceiveStream = None  # type: ignore
        # v1.0.88（下行流标识）：连接代次。每次 _create_streams 前进一号——
        # 任何"绑定某条连接"的账（协议协商结果、本轮认领凭据）都必须与之配对
        # 使用，否则半开旧连接的凭据会与新连接撞号。
        self._conn_gen = 0

    def _on_incoming(self, item) -> bool:
        """recv 通道交付前的钩子；返回 True = 就地丢弃（连接保持）。

        基类不过滤（stt/llm/mcp 语义零变化）；TtsTransport 用它实现"无人认领
        的旧轮残料不进 reader 队列"。放在交付点而不是消费点，是因为残料一旦
        进了队列，就再也分不清它属于哪一轮——那正是 v1.0.45 猜了一年的形态。
        """
        return False

    @property
    def available(self):
        return not self.stop_event.is_set() and self.should_reconnect

    def init(self):
        pass

    def update_activity_time(self):
        self._last_activity_time = time.monotonic()

    def ws_log(self, msg, *args, **kwargs):
        lvl = logging.ERROR if self.reconnect_times >= 3 else logging.INFO
        self.logger.log(lvl, msg, *args, **kwargs)

    @staticmethod
    def _redact_endpoint(url: str) -> str:
        """v1.0.48（凭据面收口）：translations 指引 token 强制模式把 ?token=<真
        token> 粘进 endpoint（translations :94），而本文件把 endpoint 全量写进
        INFO 日志（每次连接一行）→ 凭据随 HA 日志落盘外流。日志只保留 '?' 前
        host+path，query 以标记留痕；connect 实参仍用原文。"""
        return str(url).split("?", 1)[0] + (" ?<masked>" if "?" in str(url) else "")

    @property
    def is_connected(self):
        return self._is_connected and self._current_ws and not self._current_ws.closed

    def clear_endpoint_from_data(self):
        if self.entry.data.get(self.attr_endpoint, "") == self.endpoint:
            self.logger.info(
                "Clearing endpoint from config entry data: %s %s",
                self.attr_endpoint,
                self._redact_endpoint(self.endpoint),
            )
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={
                    **self.entry.data,
                    self.attr_endpoint: "",
                },
            )

    async def ensure_connected(self):
        """Ensure WebSocket is connected. Connect if not already connected.

        v1.0.40（重连竞态修复）：连接循环**只允许一条**。`run_connection_loop`
        在掉线后会退避重连（`sleep(3~60s)`，期间 `is_connected` 为假但循环仍活着），
        而本方法此前只要 `is_connected` 为假就再 spawn 一条循环——循环句柄从未被
        记住（原代码 `task = …` 赋值后即丢弃）。两条循环并发 `_create_streams()`
        会互相覆盖同一组 stream/`_current_ws`：先起那条的 `_handle_outgoing_messages`
        消费的是被替换掉的"孤儿 reader"（该属性在协程入口一次性求值）→ 僵尸连接
        （收得到、永远发不出），且两连接同时压同一通道。现场形态：加载项/HA 重启
        或网络抖动后的那一轮 `Response timeout`/没声。
        修法：留住句柄，已有活循环就只等待、不再 spawn。
        """
        if not self.should_reconnect:
            self.logger.info("Interrupted before ensure connected")
            return False

        if self.is_connected:
            self.update_activity_time()
            return True

        async with self._connection_lock:
            if self.is_connected:
                self.update_activity_time()
                return True

            if not self.endpoint:
                self.logger.error("No endpoint configured in config entry")
                return False

            if self._loop_task is not None and not self._loop_task.done():
                # 已有连接循环在跑（含退避重连窗口）→ 只等它连上，绝不重复 spawn；
                # 若它正在退避睡觉，用 _connect_now 叫醒它立刻重试（否则白等 15s）。
                self.logger.info(
                    "Connection loop already running, waiting for it to connect: %s",
                    self._redact_endpoint(self.endpoint),
                )
                self._connect_now.set()
            else:
                self.logger.info(
                    "On-demand connecting to WebSocket: %s",
                    self._redact_endpoint(self.endpoint),
                )
                self._loop_task = self.entry.async_create_background_task(
                    self.hass,
                    self.run_connection_loop(),
                    f"transport_loop:{self._transport_type}",
                )
            self.update_activity_time()

            # Wait for connection to be established
            for _ in range(150):
                if not self.should_reconnect:
                    self.logger.info("Interrupted wait connected")
                    return False
                if self.is_connected:
                    return True
                await asyncio.sleep(0.1)

            self.logger.error("Timed out waiting for WebSocket connection")
            return False

    async def _create_streams(self):
        """Create memory object streams for communication."""
        # v1.0.88：新 stream 对 = 新连接凭据（换连后一切"属这条连接"的账重算）
        self._conn_gen += 1
        self._recv_writer, self._recv_reader = anyio.create_memory_object_stream(0)
        self._send_writer, self._send_reader = anyio.create_memory_object_stream(0)

    async def run_connection_loop(self) -> None:
        """Run the connection loop with automatic reconnection."""
        while self.should_reconnect:
            try:
                if not await self.connect_to_client():
                    break
            except ConfigEntryAuthFailed:
                raise
            except Exception as err:
                self.logger.warning("Websocket disconnected or failed: %s", err)
            finally:
                self._is_connected = False
            if self.should_reconnect:
                seconds = max(min(60, self.reconnect_times * 5), 3)
                self.logger.info(
                    "Websocket retry after %s seconds, times: %s",
                    seconds,
                    self.reconnect_times,
                )
                self.reconnect_times += 1
                if seconds > 0:
                    await self._wait_backoff(seconds)

    async def _wait_backoff(self, seconds: float) -> None:
        """退避等待，但被 `ensure_connected` 要求立即重连时提前醒来。

        v1.0.40：修复"重复 spawn"后不能再靠"另起一条循环"来抢时间——否则一条
        循环正在 60s 退避里睡觉时，新请求会白等满 15s 等待窗才失败（比旧行为更慢）。
        故改为**叫醒同一条循环**：等 event 或被超时打断，二者先到为准。
        """
        if self._connect_now.is_set():
            self._connect_now.clear()      # 进睡前已被叫醒 → 直接重试，不再睡
            return
        try:
            await asyncio.wait_for(self._connect_now.wait(), timeout=seconds)
            self._connect_now.clear()      # 睡中被叫醒 → 立即重试
            self.logger.info("Reconnect requested while backing off, retrying now")
        except asyncio.TimeoutError:
            pass

    async def connect_to_client(self) -> bool:
        """Connect to WebSocket endpoint."""
        if not self.endpoint:
            self.logger.error("No endpoint configured in config entry")
            return False

        if not self.should_reconnect:
            # 提前终止
            self.logger.info("Interrupted before connect")
            return False

        try:
            await self._create_streams()
            await self._establish_websocket_connection()
        except Exception as err:
            self.logger.exception(
                "Failed to connect to websocket at %s: %s",
                self._redact_endpoint(self.endpoint),
                err,
            )
            raise

        return self.should_reconnect

    async def _establish_websocket_connection(self):
        """Establish WebSocket connection and run server tasks."""
        self.logger.info("Connecting to: %s", self._redact_endpoint(self.endpoint))
        assert self.endpoint
        timeout = aiohttp.ClientTimeout(total=None, connect=60)
        async with aiohttp.ClientSession(timeout=timeout) as client_session:
            try:
                if not self.should_reconnect:
                    # 提前终止
                    self.logger.info("Interrupted after session created")
                    return

                assert self.endpoint
                async with client_session.ws_connect(self.endpoint) as ws:
                    if not self.should_reconnect:
                        # 提前终止
                        self.logger.info("Interrupted after websocket connected")
                        return
                    self._current_ws = ws
                    self._is_connected = True
                    self.update_activity_time()
                    self.reconnect_times = 0
                    async with anyio.create_task_group() as tg:
                        try:
                            tg.start_soon(
                                self._handle_incoming_messages, tg.cancel_scope
                            )
                            tg.start_soon(self._handle_outgoing_messages)
                            tg.start_soon(self._heartbeat_task)
                            tg.start_soon(self._idle_monitor_task, tg.cancel_scope)
                        except Exception as err:
                            self.logger.error("Error in server tasks: %s", err)
                            tg.cancel_scope.cancel()
                            raise
                    self.logger.info("WebSocket connection tasks completed.")
            except aiohttp.WSServerHandshakeError as err:
                self.logger.warning("WebSocket handshake failed: %s", err)
                if err.status == 401:
                    self.should_reconnect = False
                    self.clear_endpoint_from_data()
                    self.logger.warning("WebSocket unauthorized, disable reconnect")
            except Exception as err:
                self.logger.exception("WebSocket connection failed: %s", err)
                raise
            finally:
                self.logger.info("WebSocket connection stop over.")
                self._is_connected = False

    async def _idle_monitor_task(self, cancel_scope: anyio.CancelScope):
        """Monitor idle time and close connection if idle too long."""
        try:
            while (
                self.should_reconnect
                and self._current_ws
                and not self._current_ws.closed
            ):
                await asyncio.sleep(30)  # Check every 30 seconds
                idle_seconds = time.monotonic() - self._last_activity_time
                if idle_seconds >= self._idle_timeout:
                    self.logger.info(
                        "WebSocket idle for %.0f seconds (>%ds), closing to save resources",
                        idle_seconds,
                        self._idle_timeout,
                    )
                    self.should_reconnect = False
                    cancel_scope.cancel()
                    return
        except Exception as err:
            self.logger.error("Idle monitor error: %s", err)

    async def _handle_incoming_messages(self, cancel_scope: anyio.CancelScope):
        """Handle incoming WebSocket messages."""
        assert self._current_ws, "WebSocket connection not established"
        try:
            async for msg in self._current_ws:
                self.update_activity_time()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    if not await self._process_text_message(msg):
                        break
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    if not await self._deliver(self._recv_writer, msg.data):
                        break
                elif msg.type == aiohttp.WSMsgType.CLOSE:
                    self.logger.error("WebSocket closed: %s", msg.extra)
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    self.logger.error("WebSocket error: %s", msg.data)
                    break
        except Exception as err:
            self.logger.error("Error reading WebSocket messages: %s", err)
            raise
        finally:
            self.ws_log(
                "WebSocket connection stopped. Final close code: %s",
                self._current_ws.close_code,
            )
            if self._current_ws.close_code == 1008:
                # 被顶号后，禁止重连
                self.should_reconnect = False
            cancel_scope.cancel()

    # v1.0.70（深审⑨）：交付队列闸。writer 消费者（_handle_outgoing_messages）
    # 卡在半开 TCP 的 ws.send_* 上时，_send_reader 缓冲灌满 → 这里无限阻塞，
    # conversation.py 的裸 await 就是"LLM 通道一卡、对话兜底永久失效（不是
    # 超时，是无限）"的现场形态。15s：正常交付进程内微秒级，触发即连接坏死。
    _SEND_HANDOFF_TIMEOUT_S = 15.0

    async def send_message(self, message):
        """Send a message to the WebSocket server."""
        self.update_activity_time()
        if not self._send_writer:
            self.logger.warning("Cannot send message, send writer is not available")
            return
        try:
            await asyncio.wait_for(
                self._send_writer.send(message), self._SEND_HANDOFF_TIMEOUT_S)
        except asyncio.TimeoutError:
            # 不 raise（保持调用方契约）：判连接坏死，后台换连——"下一次请求
            # 在全新连接上开始"是本传输层的既有承诺（restart_connection）。
            # 本条消息按丢弃处理：上层各自的 await_message/整流预算会把它
            # 收敛成一次可见的失败，而不是无限挂起。
            self.logger.warning(
                "send_message 交付超 %.0fs——writer 卡死（半开 TCP/消费者消失），"
                "主动换连自愈", self._SEND_HANDOFF_TIMEOUT_S)
            self._schedule_restart("send stalled")

    def _schedule_restart(self, reason: str) -> None:
        """restart_connection 的火后即忘包装（send_message 内不可自等待：
        它会拆掉本 writer 队列，而调用栈还挂在这条 send 上）。"""
        try:
            if self.hass is not None:
                self.hass.async_create_background_task(
                    self.restart_connection(reason), "huijian_ai_ws_restart")
            else:  # 测试/无 hass 环境
                asyncio.get_running_loop().create_task(
                    self.restart_connection(reason))
        except Exception:  # noqa: BLE001 自愈动作本身绝不能再炸调用栈
            self.logger.exception("schedule restart failed: %s", reason)

    async def send_hello(self):
        await self.send_message(
            {
                "type": "hello",
                "version": 1,
                "transport": "websocket",
                # v1.1.7：卫星身份（entry.unique_id = 设备 MAC）随 hello 上报，供
                # 加载项按卫星分桶 origin（确认环/跨轮上下文不再多机串台）。MAC 稳定、
                # 跨重连不变；加载项缺此字段回落 request.remote，向后兼容旧加载项。
                "device": getattr(self.entry, "unique_id", "") or "",
                "audio_params": {
                    "format": "opus",
                    "sample_rate": 16000,
                    "channels": 1,
                    "frame_duration": 60,
                },
            }
        )

    async def _handle_outgoing_messages(self):
        """Handle outgoing messages to WebSocket."""
        assert self._current_ws, "WebSocket connection not established"
        try:
            async for message in self._send_reader:
                if isinstance(message, dict):
                    message = json.dumps(message, ensure_ascii=False)
                if isinstance(message, str):
                    # v1.0.48（隐私/日志噪音）：本行曾把全量出帧 JSON（含播报
                    # 文本明文）逐条 INFO 落日志。播报内容属家居隐私，只留长度；
                    # 排障需要全文时用 DEBUG 级（现场默认 INFO 不落盘）。
                    self.logger.info(
                        "Send message: %d chars%s",
                        len(message),
                        " (full text at DEBUG)" if self.logger.isEnabledFor(logging.DEBUG) else "",
                    )
                    self.logger.debug("Send message full: %s", message)
                    await self._current_ws.send_str(message)
                else:
                    await self._current_ws.send_bytes(message)
        except Exception as err:
            self.logger.error("Error writing to WebSocket: %s", err)
        finally:
            self.logger.info("Websocket writer stopped")
            try:
                if self._current_ws and not self._current_ws.closed:
                    # v1.0.65（TTS 深审 T3）：H8 同款带闸收口补第三处漏网——半开
                    # TCP 上裸 close 无限挂会让任务组永拆不干净（_loop_task 恒
                    # not done → 后续 ensure_connected 全走 15s 失败支 = v1.0.40
                    # 僵尸签名）。close 超时即 abort，拆链路径不留无限 await。
                    await asyncio.wait_for(self._current_ws.close(), 5)
            except Exception as err:  # noqa: BLE001（含 TimeoutError）
                if isinstance(err, asyncio.TimeoutError):
                    self.logger.warning(
                        "writer close timeout, abort: %s",
                        self._redact_endpoint(self.endpoint))
                    try:
                        t = self._current_ws and self._current_ws.transport
                        t and t.abort()
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    self.logger.error("Error closing WebSocket: %s", err)

    # v1.0.45（reader 僵尸断根）：buffer-0 内存流的 send 在**没有消费者**时会
    # 无限阻塞。TTS 消费端中途被取消（管线打断/超时/页面切换）后，加载项仍在
    # 吐帧，reader 任务就永久卡在 `sw.send`——永远读不到对端 CLOSE，任务组拆不
    # 干净，连接循环停在 teardown，之后每条播报都 "Timed out waiting for
    # WebSocket connection"，直到 HA 重启（台架实锤：一次中途取消毒死整条连接
    # 循环；同 v1.0.40 僵尸连接的同族，那条修的是 send 侧，这是 recv 侧）。
    # 交付超时 = 判据：正常消费在进程内微秒级完成，永不触发；一旦触发消费者
    # 必已消失 → 丢帧并主动 break 走收口重连，让"下一次对话在全新连接上开始"
    # 的承诺真正成立。
    # ⚠ 窗口约束：stt/llm/mcp 通道在"连接即收到 hello/echo、消费端尚未挂上"
    # 的窗口里必须允许 reader 任务排队等待——5s 判死会引发永断永重的风暴。
    # v1.0.70（深审⑨）：None（无限等）→ 30.0。挂账形态本身仍被窗口合法解释
    # （hello 窗亚秒级、消费端 await_message 秒级挂上），但"消费端已消失"
    # 不再等于 reader 永挂、is_connected 恒真、任务组永拆不干净——30s 判死
    # 换连自愈。5s 会误杀冷启动、30s 只杀真僵尸。
    # TtsTransport 覆写 5.0：其服务端契约是无请求不推帧（detect 应答才有
    # 帧/stop），且 stream() 以锁保证恰好一个消费者，交付超时唯一可能就是
    # 消费端已消失，判得更快。
    _CONSUMER_HANDOFF_TIMEOUT_S: float | None = 30.0

    async def _deliver(self, writer, item) -> bool:
        """把一条消息交给消费端；返回 False = 消费端已消失，调用方须收口。"""
        # v1.0.88：recv 通道交付前先过子类过滤器（TTS 用它丢弃无人认领的旧轮
        # 残料）。被丢弃的消息**不进出队**，故不会与下一轮的头对不上；返回
        # True 表示链路健康、只是这条不算数（连接保持，绝不因丢弃而断连）。
        if writer is self._recv_writer and self._on_incoming(item):
            return True
        try:
            if self._CONSUMER_HANDOFF_TIMEOUT_S is None:
                await writer.send(item)
                return True
            await asyncio.wait_for(
                writer.send(item), self._CONSUMER_HANDOFF_TIMEOUT_S)
            return True
        except asyncio.TimeoutError:
            self.logger.warning(
                "%s 通道消息交付超时 %.0fs——上一轮请求已消失，主动断开本连接自愈"
                "（残帧不得毒化下一轮）",
                self._transport_type or self.__class__.__name__,
                self._CONSUMER_HANDOFF_TIMEOUT_S,
            )
            return False
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            return False

    def _on_server_settings(self, data) -> None:
        """服务端 type=="settings" 控制消息（v1.0.48 P5：音色指纹推送）。
        基类忽略；关心配置的子通道覆写。此类消息不进出帧流。"""
        return None

    async def _process_text_message(self, msg: aiohttp.WSMessage) -> bool:
        """Process a text message from WebSocket. False = 消费端已消失。"""
        try:
            if msg.data[0:2] == '"{':
                json_data = Dict(json.loads(msg.json()))
            else:
                json_data = Dict(msg.json())
            self.logger.debug("Process incoming msg: %s", json_data)
        except Exception as err:
            # v1.0.65（TTS 深审 T5）：本基类为 tts/stt/llm/mcp 四通道共享——
            # STT 通道的损坏/截断 JSON 帧可含用户转写文本，旧版全量原始帧进
            # ERROR（必落盘）且无长度上限，违 v1.0.48 日志纪律（INFO 截断、
            # 全文降 DEBUG）。留 120 字符定位形制，全文降 DEBUG 供排障。
            self.logger.error("Invalid incoming msg: %r (%s)",
                              str(msg.data)[:120], err)
            self.logger.debug("Invalid incoming msg 全文: %s", msg)
            return True   # 解析失败与交付无关，链接保持
        if json_data.get("type") == "settings":
            self._on_server_settings(json_data)
            return True
        return await self._deliver(self._recv_writer, json_data)

    async def await_message(self, timeout: int = 120):
        """Wait response message"""
        try:
            with anyio.fail_after(timeout):
                async for data in self._recv_reader:
                    yield data
        except TimeoutError:
            yield Dict(error="Response timeout")

    async def _heartbeat_task(self):
        """Send periodic heartbeat pings."""
        try:
            while (
                self.should_reconnect
                and self._current_ws
                and not self._current_ws.closed
            ):
                await asyncio.sleep(55)
                self.logger.debug("heartbeat ping for %s",
                                  self._redact_endpoint(self.endpoint))
                # v1.0.65（T3 顺带）：ping 也在半开 TCP 上裸 await 的点位——
                # 挂住=heartbeat 任务僵死、ws.closed 永不翻转，短路不了任何东西。
                await asyncio.wait_for(self._current_ws.ping(), 10)
                # v1.0.70（深审⑩）：ping 成功=链路层活动，计入活动时间。
                # 旧形态 _last_activity_time 只认用户收发帧，aiohttp 又不把
                # PING/PONG 递进 `async for`——健康的闲置链路照样被 180s 空闲
                # 监控判死自杀，首唤付冷握手+15s 连接闸。计入后：TCP 活着
                # 且心跳在走=连接保温；空闲监控退化为真僵尸兜底（心跳僵死
                # 且无用户流量才落刀）。
                self.update_activity_time()
        except Exception as err:
            self.ws_log("heartbeat ping failed: %s", err)

    async def restart_connection(self, reason: str = "") -> None:
        """拆掉当前连接、保留自动重连并立即续连（v1.0.45）。

        介于"不动"与 stop() 之间：调用方判定本连接的对话状态已被污染
        （典型：TTS 消费被取消，残帧/残 stop 可能挂在 reader 上毒化下一轮
        请求），与其在旧流上猜，不如换连接——重连后 `_create_streams` 给出
        全新 stream 对，残留物理归零。`_connect_now` 同时叫醒正处于退避的
        循环，下一请求不必空等 3~60s。
        """
        self.logger.warning("Restarting websocket connection: %s", reason)
        ws = self._current_ws
        self._is_connected = False
        self._connect_now.set()
        if ws is not None and not ws.closed:
            # H8（2026-09-23 深审）：aiohttp ws.close() 要 writer drain+等 CLOSE
            # 分手——半开 TCP 上无限挂，而本方法正被 stream() finally 在
            # _request_lock 持有期内必经（v1.0.55 定案②），锁挂=全通道播报永堵
            # （同文件 send 侧 :118 注释早立过此律，close 漏收=修一漏一）。
            try:
                await asyncio.wait_for(ws.close(), 5)
            except Exception as err:  # noqa: BLE001（含 TimeoutError）
                self.logger.warning("restart close timeout, abort: %s", err)
                try:
                    ws.transport and ws.transport.abort()
                except Exception:  # noqa: BLE001
                    pass

    async def stop(self, reason: str = ""):
        if self.stop_event.is_set():
            return
        self.stop_event.set()

        self.logger.info("Stop begin, reason: '%s'", reason)
        self.should_reconnect = False
        self._is_connected = False
        # v1.0.40：唤醒正在退避的循环，令其立刻看到 should_reconnect=False 退出，
        # 不必再空睡最长 60s（卸载集成/删除条目的收尾更快）
        self._connect_now.set()
        self.reconnect_times = 0

        if self._current_ws and not self._current_ws.closed:
            self.logger.info("Closing websocket")
            # H8 同口收口：stop 也走带闸 close——卸载路径挂死=条目删不掉。
            try:
                await asyncio.wait_for(self._current_ws.close(), 5)
            except Exception as err:  # noqa: BLE001（含 TimeoutError）
                self.logger.warning("stop close timeout, abort: %s", err)
                try:
                    self._current_ws.transport and self._current_ws.transport.abort()
                except Exception:  # noqa: BLE001
                    pass
        for stream in (
            self._recv_writer,
            self._recv_reader,
            self._send_writer,
            self._send_reader,
        ):
            if stream:
                await stream.aclose()
        self.logger.info("Stop end")
