import logging

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
        """Wait response message"""
        content = ""
        # v1.0.93 退下旗：加载项 end 帧只在 True 时带 end_dialogue 键；
        # 这里透传到聚合 Delta 上（conversation 实体消费时记账）。False/缺席
        # =逐字节旧帧形，旧加载项零暴露。
        end_dialogue = False
        try:
            with anyio.fail_after(timeout):
                async for data in self._recv_reader:
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
