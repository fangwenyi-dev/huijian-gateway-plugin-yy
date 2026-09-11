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

    @property
    def is_connected(self):
        return self._is_connected and self._current_ws and not self._current_ws.closed

    def clear_endpoint_from_data(self):
        if self.entry.data.get(self.attr_endpoint, "") == self.endpoint:
            self.logger.info(
                "Clearing endpoint from config entry data: %s %s",
                self.attr_endpoint,
                self.endpoint,
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
                    self.endpoint,
                )
                self._connect_now.set()
            else:
                self.logger.info(
                    "On-demand connecting to WebSocket: %s", self.endpoint
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
                "Failed to connect to websocket at %s: %s", self.endpoint, err
            )
            raise

        return self.should_reconnect

    async def _establish_websocket_connection(self):
        """Establish WebSocket connection and run server tasks."""
        self.logger.info("Connecting to: %s", self.endpoint)
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

    async def send_message(self, message):
        """Send a message to the WebSocket server."""
        self.update_activity_time()
        if not self._send_writer:
            self.logger.warning("Cannot send message, send writer is not available")
            return
        await self._send_writer.send(message)

    async def send_hello(self):
        await self.send_message(
            {
                "type": "hello",
                "version": 1,
                "transport": "websocket",
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
                    self.logger.info("Send message: %s", message)
                    await self._current_ws.send_str(message)
                else:
                    await self._current_ws.send_bytes(message)
        except Exception as err:
            self.logger.error("Error writing to WebSocket: %s", err)
        finally:
            self.logger.info("Websocket writer stopped")
            try:
                if self._current_ws and not self._current_ws.closed:
                    await self._current_ws.close()
            except Exception as err:
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
    # ⚠ 默认关闭（None=无限等，与历史行为逐比特一致）：stt/llm/mcp 通道在
    # "连接即收到 hello/echo、消费端尚未挂上"的窗口里必须允许 reader 任务
    # 排队等待——5s 判死会引发永断永重的风暴。只在 TtsTransport 启用：其服务
    # 端契约是无请求不推帧（detect 应答才有帧/stop），且 stream() 以锁保证
    # 恰好一个消费者，交付超时唯一的可能就是消费端已消失。
    _CONSUMER_HANDOFF_TIMEOUT_S: float | None = None

    async def _deliver(self, writer, item) -> bool:
        """把一条消息交给消费端；返回 False = 消费端已消失，调用方须收口。"""
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

    async def _process_text_message(self, msg: aiohttp.WSMessage) -> bool:
        """Process a text message from WebSocket. False = 消费端已消失。"""
        try:
            if msg.data[0:2] == '"{':
                json_data = Dict(json.loads(msg.json()))
            else:
                json_data = Dict(msg.json())
            self.logger.debug("Process incoming msg: %s", json_data)
        except Exception as err:
            self.logger.error("Invalid incoming msg: %s", msg)
            return True   # 解析失败与交付无关，链接保持
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
                self.logger.debug("heartbeat ping for %s", self.endpoint)
                await self._current_ws.ping()
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
            try:
                await ws.close()
            except Exception as err:
                self.logger.debug("restart close ignored: %s", err)

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
            await self._current_ws.close()
        for stream in (
            self._recv_writer,
            self._recv_reader,
            self._send_writer,
            self._send_reader,
        ):
            if stream:
                await stream.aclose()
        self.logger.info("Stop end")
