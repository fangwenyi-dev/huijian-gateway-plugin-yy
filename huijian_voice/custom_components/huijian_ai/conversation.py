import json
import logging

import anyio
from homeassistant.components import conversation
from homeassistant.components.conversation import DOMAIN as ENTITY_DOMAIN
from homeassistant.components.conversation import ChatLog
from homeassistant.components.conversation import \
    ConversationEntity as BaseEntity
from homeassistant.components.conversation import (ConversationInput,
                                                   ConversationResult)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .huijian import get_entry_data, llm_transport

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities
):
    """Set up conversation entities."""
    async_add_entities([HuijianConversationEntity(hass, config_entry)])


class HuijianConversationEntity(BaseEntity):
    domain = ENTITY_DOMAIN

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.entity_id = f"{self.domain}.huijian_agent"
        self._attr_name = "huijian AI 对话代理"
        self._attr_unique_id = f"{self.entry.entry_id}-{ENTITY_DOMAIN}"
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name="huijian AI",
            manufacturer="huijian",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @property
    def supported_languages(self):
        """Return a list of supported languages."""
        return MATCH_ALL

    async def _async_handle_message(
        self,
        user_input: ConversationInput,
        chat_log: ChatLog,
    ) -> ConversationResult:
        """Call the API."""
        transport = llm_transport.get_entry_transport(self.hass, self.entry)
        if not await transport.ensure_connected():
            raise HomeAssistantError("Failed to establish WebSocket connection for LLM")

        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                user_extra_system_prompt=user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        await self._async_handle_chat_log(transport, user_input, chat_log)
        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    async def _await_message_with_timeout(self, transport, timeout=60):
        try:
            async with anyio.fail_after(timeout):
                async for msg in transport.await_message():
                    yield msg
        except TimeoutError:
            _LOGGER.error("LLM response timeout after %ss", timeout)
            yield {"error": "抱歉，AI 响应超时，请重试"}

    async def _async_handle_chat_log(
        self,
        transport: llm_transport.LlmTransport,
        user_input: ConversationInput,
        chat_log: conversation.ChatLog,
    ):
        await transport.send_message(
            json.dumps(
                {
                    "type": "listen",
                    "state": "detect",
                    "text": user_input.text,
                }
            )
        )
        async for content in chat_log.async_add_delta_content_stream(
            self.entity_id, self._await_message_with_timeout(transport)
        ):
            _LOGGER.info("LLM response: %s", content)
