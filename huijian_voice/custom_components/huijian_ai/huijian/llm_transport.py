import logging
import time

import anyio
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import Dict, EntryAuthFailedError, get_entry_data
from .ws_transport import WsTransport

_LOGGER = logging.getLogger(__name__)
ATTR_ENDPOINT = "llm_endpoint"
ATTR_TRANSPORT = "llm_transport"


def get_entry_transport(hass: HomeAssistant, entry: ConfigEntry) -> "LlmTransport":
    """Set up from a config entry."""
    endpoint: str | None = entry.data.get(ATTR_ENDPOINT)
    if not endpoint:
        raise EntryAuthFailedError(hass, entry)

    this_data: dict = get_entry_data(hass, entry)
    transport: LlmTransport | None = this_data.get(ATTR_TRANSPORT)
    if transport and transport.endpoint == endpoint and transport.available:
        return transport

    _LOGGER.info(
        "Creating new LlmTransport for entry: %s %s", entry.entry_id, entry.title
    )
    transport = LlmTransport(hass, entry, endpoint, ATTR_ENDPOINT, _LOGGER)
    this_data[ATTR_TRANSPORT] = transport
    return transport


class LlmTransport(WsTransport):
    _transport_type = "llm"

    async def await_message(self, timeout: int = 180):
        """Wait response message。

        v1.1.27-r2（金标复测）：旧实现 `with anyio.fail_after(timeout): async for …
        yield` 把 cancel scope **横跨 yield**——驱动到首块的任务与收口任务不同即
        anyio 抛 "cancel scope in a different task"（与 huijian/tts_transport
        v1.0.69 根因①同型；那边已改"单调 deadline + 逐条 receive 独立短 scope"，
        LLM 通道是漏网的第四处）。现同口径：scope 只包单条 receive，yield 点零存活
        scope；整轮仍是墙钟 deadline（语义不变：超过 timeout 未收口即 error 帧）。
        """
        content = ""
        # 第四轮审计 P2：本轮开局连接代次（与 tts/stt 同款闸）——轮中被换连时
        # 本轮的 reader 已失效，继续读会把旧流残帧算进新连接的回合。
        _round_gen = getattr(self, "_conn_gen", None)
        # v1.0.93 退下旗：加载项 end 帧只在 True 时带 end_dialogue 键；
        # 这里透传到聚合 Delta 上（conversation 实体消费时记账）。False/缺席
        # =逐字节旧帧形，旧加载项零暴露。
        end_dialogue = False
        deadline = time.monotonic() + max(1, int(timeout))
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                if _round_gen is not None and self._conn_gen != _round_gen:
                    _LOGGER.warning("connection replaced mid-round (gen %s→%s)"
                                    "，本轮按连接关闭收口", _round_gen, self._conn_gen)
                    raise StopAsyncIteration
                with anyio.fail_after(remaining):
                    data = await self._recv_reader.__anext__()
                if data.state == "end":
                    try:
                        end_dialogue = bool(data.get("end_dialogue"))
                    except Exception:
                        end_dialogue = False
                    break
                if data.type != "text":
                    continue
                if data.state == "start":
                    content = ""
                if data.state == "sentence_end" and isinstance(data.data, str):
                    content += data.data
            if end_dialogue:
                yield Dict(role="assistant", content=content,
                           end_dialogue=1)
            else:
                yield Dict(role="assistant", content=content)
        except TimeoutError:
            _LOGGER.error("response timeout")
            yield Dict(error="Response timeout")
        except StopAsyncIteration:
            _LOGGER.error("response stream closed before end frame")
            yield Dict(error="WebSocket connection closed")
        except anyio.ClosedResourceError:
            # stop()/换连关掉内存流时 receive 抛它——旧形未捕，直穿 conversation
            _LOGGER.error("response stream closed (resource closed)")
            yield Dict(error="WebSocket connection closed")

    async def async_remove_entry(self):
        entry = self.entry
        this_data: dict = get_entry_data(self.hass, entry)
        transport: LlmTransport | None = this_data.pop(ATTR_TRANSPORT, None)
        self.logger.info(
            "Remove entry from LLM transport: title=%s id=%s",
            entry.title,
            entry.entry_id,
        )
        if transport:
            await transport.stop("Remove entry")
