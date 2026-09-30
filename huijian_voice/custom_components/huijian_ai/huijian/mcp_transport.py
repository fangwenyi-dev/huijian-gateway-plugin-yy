import asyncio
import logging

import aiohttp
import anyio
from homeassistant.components import conversation
from homeassistant.components.mcp_server.server import create_server
from homeassistant.components.mcp_server.session import Session, SessionManager
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import llm
from mcp import types

from custom_components.huijian_ai.entry_data import ESPHomeConfigEntry

from ..const import DOMAIN
from . import EntryAuthFailedError, get_entry_data
from .ws_transport import WsTransport

try:
    from mcp.shared.message import SessionMessage  # ha>=2025.10,mcp>=1.14.1
except (ImportError, ModuleNotFoundError):
    SessionMessage = None


_LOGGER = logging.getLogger(__name__)
ATTR_ENDPOINT = "mcp_endpoint"
ATTR_TRANSPORT = "mcp_transport"

# 全局 MCP 连接 ID 计数器
mcp_transport_id = 0


async def async_setup_entry(hass: HomeAssistant, entry: ESPHomeConfigEntry):
    """Set up MCP Server from a config entry."""
    # 添加设备 或 重新配置
    # - 添加设备：新建连接
    # - 重新配置：重建连接
    endpoint: str | None = entry.data.get(ATTR_ENDPOINT)
    if not endpoint:
        # HUIJIAN-PATCH D1（huijian_voice 发行版，2026-09，盘点 §6-D1）：
        # 纯 LAN 模式 B 没有云端 MCP 端点；原逻辑 raise EntryAuthFailedError
        # 会让任何 setup 路径永久失败（assist 条目离线死路）。
        # 门禁改法：端点为空 → 跳过 MCP 传输挂载；stt/tts/llm 本地链路照常。
        _LOGGER.info("mcp_endpoint 为空：跳过 MCP 传输（LAN 本地链路，D1 门禁）")
        return True

    this_data = get_entry_data(hass, entry)
    transport: McpTransport | None = this_data.pop(ATTR_TRANSPORT, None)
    _LOGGER.info("Setup mcp endpoint entry: %s", [entry.title, transport])
    if transport:
        try:
            await transport.stop("Reconfigure")
        except Exception as err:
            _LOGGER.error("Error stopping existing transport: %s", err)

    global mcp_transport_id
    mcp_transport_id += 1
    # 第四轮审计 P1：端点走脱敏助手（基类同口径）——mcp endpoint 可内嵌
    # ?token=，原文进 INFO 即凭据落 HA 日志（v1.0.48 纪律，本通道此前漏网）。
    _LOGGER.info(
        "Set mcp endpoint and ensure connected: endpoint=%s id=%s",
        McpTransport._redact_endpoint(endpoint),
        mcp_transport_id,
    )
    logger = logging.getLogger(__name__ + "." + str(mcp_transport_id))
    transport = McpTransport(hass, entry, endpoint, ATTR_ENDPOINT, logger)
    this_data[ATTR_TRANSPORT] = transport
    await transport.ensure_connected()

    return True


class McpTransport(WsTransport):
    """Handles WebSocket transport for MCP server."""

    _transport_type = "mcp"

    async def _create_server(self, context: llm.LLMContext):
        """Create MCP server instance."""
        llm_api_id = llm.LLM_API_ASSIST
        mcp_api = await llm.async_get_api(self.hass, llm_api_id, context)
        tools = [tool.name for tool in mcp_api.tools]
        self.logger.info("MCP server tools: %s, llm_api_id=%s", tools, llm_api_id)
        return await create_server(self.hass, llm_api_id, context)

    async def connect_to_client(self) -> bool:
        """Connect to WebSocket endpoint."""
        if not self.endpoint:
            self.logger.error("No endpoint configured in config entry")
            return False

        if not self.should_reconnect:
            # 提前终止
            self.logger.info("Interrupted before connect to client")
            return False

        self.logger.info("Websocket connect to client")
        try:
            context = llm.LLMContext(
                platform=DOMAIN,
                context=None,
                language="*",
                assistant=conversation.DOMAIN,
                device_id=None,
            )
            self._mcp_server = await self._create_server(context)
            self._mcp_server.version = "2.2.0"
            options = await self.hass.async_add_executor_job(
                self._mcp_server.create_initialization_options
            )

            session_manager = SessionManager()
            async with session_manager.create(Session(self._recv_writer)) as session_id:
                await self._establish_websocket_connection_with_options(options)

        except Exception as err:
            # 第四轮审计 P1：端点脱敏后再落日志（含 token 的 URL 不得进 HA 日志）
            self.logger.exception(
                "Failed to connect to websocket at %s: %s",
                self._redact_endpoint(self.endpoint), err,
            )
            raise

        return self.should_reconnect

    async def _establish_websocket_connection_with_options(self, options):
        """Establish WebSocket connection and run server tasks."""
        self.logger.info("Connecting to: %s",
                         self._redact_endpoint(self.endpoint))
        assert self.endpoint, "No endpoint configured"
        assert self._mcp_server, "MCP server not created"
        if not self.should_reconnect:
            # 提前终止
            self.logger.info("Interrupted before session created")
            return
        await self._create_streams()
        timeout = aiohttp.ClientTimeout(total=None, connect=60)
        async with aiohttp.ClientSession(timeout=timeout) as client_session:
            try:
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
                            try:
                                await self._mcp_server.run(
                                    self._recv_reader, self._send_writer, options
                                )
                            except Exception as err:
                                self.logger.error("Error in server run: %s", err)
                        except Exception as err:
                            self.logger.error("Error in server tasks: %s", err)
                            tg.cancel_scope.cancel()
                            raise
            except aiohttp.WSServerHandshakeError as err:
                self.logger.warning("WebSocket handshake failed: %s", err)
                if err.status == 401:
                    self.should_reconnect = False
                    self.logger.warning("WebSocket unauthorized, disable reconnect")
                    self.clear_endpoint_from_data()

                    raise EntryAuthFailedError(self.hass, self.entry)
            except Exception as err:
                self.logger.exception("WebSocket connection failed: %s", err)
                raise
            finally:
                self._is_connected = False

    async def _handle_outgoing_messages(self):
        """Handle outgoing messages to WebSocket."""
        try:
            async for session_message in self._send_reader:
                if SessionMessage is not None and isinstance(
                    session_message, SessionMessage
                ):
                    message = session_message.message
                else:
                    message = session_message
                payload = message.model_dump_json(by_alias=True, exclude_none=True)
                # 第四轮审计 P1：出帧全文不进 INFO（含工具参数/家居文本），
                # 与基类 ws_transport 同口径：只留长度，全文降 DEBUG。
                self.logger.info(
                    "Send message: %d chars%s",
                    len(payload),
                    " (full text at DEBUG)" if self.logger.isEnabledFor(logging.DEBUG) else "",
                )
                self.logger.debug("Send message full: %s", payload)
                await self._current_ws.send_str(payload)
        except Exception as err:
            self.logger.error("Error writing to WebSocket: %s", str(err), exc_info=True)
        finally:
            self.logger.info("Websocket writer stopped")
            try:
                if self._current_ws and not self._current_ws.closed:
                    # 第四轮审计 P1：基类 T3 同款带闸收口——半开 TCP 上裸 close
                    # 无限挂会让任务组拆不净（_loop_task 恒 not done → 之后每次
                    # ensure_connected 白等 15s = v1.0.40 僵尸签名）。超时即 abort。
                    await asyncio.wait_for(self._current_ws.close(), 5)
            except Exception as err:  # noqa: BLE001（含 TimeoutError）
                if isinstance(err, asyncio.TimeoutError):
                    self.logger.warning("writer close timeout, abort: %s",
                                        self._redact_endpoint(self.endpoint))
                    try:
                        t = self._current_ws and self._current_ws.transport
                        t and t.abort()
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    self.logger.error("Error closing WebSocket: %s", err)

    async def _process_text_message(self, msg: aiohttp.WSMessage) -> bool:
        """Process a text message from WebSocket. False = 消费端已消失（须收口重连）。

        v1.1.27（MCP 通道"首帧即死"根修）：旧形态本方法**全路径无 return**
        （恒 None），而基类 reader 判 `if not await self._process_text_message(msg):
        break`（ws_transport 文本帧分支）——收到第一条文本帧即被判"消费端已消失"，
        break → 任务组拆 → 重连 → 再拆：MCP 通道永久不可用 + 重连风暴。
        返回契约与基类同判：解析失败=True（链接保持，仅留痕），交付结果由
        `_deliver` 收口（交付超时/消费端消失才 False，绝不裸 send 无限挂）。
        """
        try:
            json_data = msg.json()
            message = types.JSONRPCMessage.model_validate(json_data)
        except Exception as err:
            # 隐私/日志纪律同基类：原始帧只留 120 字符定位形制，全文降 DEBUG
            self.logger.error("Invalid incoming msg: %r (%s)", str(msg.data)[:120], err)
            self.logger.debug("Invalid incoming msg 全文: %s", msg)
            return True   # 解析失败与交付无关，链接保持（旧形态恒 None=当场拆连）
        self.logger.debug("Process incoming msg: %s", message)
        if SessionMessage:
            message = SessionMessage(message)
        return await self._deliver(self._recv_writer, message)

    async def async_remove_entry(self):
        entry = self.entry
        this_data: dict = get_entry_data(self.hass, entry)
        transport: McpTransport | None = this_data.pop(ATTR_TRANSPORT, None)
        self.logger.info(
            "Remove entry from MCP transport: title=%s id=%s",
            entry.title,
            entry.entry_id,
        )
        if transport:
            await transport.stop("Remove entry")
