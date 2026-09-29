import json
import logging
import time

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
from . import end_dialogue
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
        """基类 await_message 的整轮收口（真 anyio；v1.1.27 根因①同款改法）。

        v1.1.27（与 huijian/tts_transport.py:213-224 已定案的根因①同病同治）：
        旧实现 `with anyio.fail_after(timeout): async for msg in
        transport.await_message(): yield msg` —— anyio cancel scope 横跨
        `yield`，scope 的任务仿射绑定在"驱动到首块"的任务上；HA 的对话 delta
        消费面若换任务续跑/收口（TTS 侧已实证：`async_create_background_task`
        续跑 → `__exit__` 与 `__enter__` 异任务 → RuntimeError "Attempted to
        exit cancel scope in a different task"），整轮对话当场作废。
        现改为单调 deadline + 逐条 receive 独立短 scope：enter/exit 恒在同一
        次 `__anext__` 步内（中间无 yield），yield 点零存活 scope；内层生成器
        在 finally 里确定性收链（不赌 GC 时机）。
        另注：`anyio.fail_after` 是同步上下文管理器（与 llm/stt/tts_transport
        同款用法）——写成 `async with` 在首次对话即抛 TypeError（2026-09-08
        台架实发 "Unexpected error during intent recognition"），此处同样只用
        同步形式。
        """
        deadline = time.monotonic() + timeout
        agen = transport.await_message().__aiter__()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _LOGGER.error("LLM response timeout after %ss", timeout)
                    yield {"error": "抱歉，AI 响应超时，请重试"}
                    return
                with anyio.move_on_after(remaining) as scope:
                    try:
                        msg = await agen.__anext__()
                    except StopAsyncIteration:
                        return
                if scope.cancelled_caught:
                    _LOGGER.error("LLM response timeout after %ss", timeout)
                    yield {"error": "抱歉，AI 响应超时，请重试"}
                    return
                yield msg
        finally:
            try:
                await agen.aclose()
            except Exception:  # noqa: BLE001 收链尽力而为，不掩盖主因
                _LOGGER.debug("LLM await_message 收链异常", exc_info=True)

    async def _async_handle_chat_log(
        self,
        transport: llm_transport.LlmTransport,
        user_input: ConversationInput,
        chat_log: conversation.ChatLog,
    ):
        frame = {
            "type": "listen",
            "state": "detect",
            "text": user_input.text,
        }
        # 每轮携带卫星身份（v1.1.7 分桶改真）：stt/tts/llm transport 挂在**全局唯一**
        # 的 assist 条目上（config_flow.py "unique_id=haid"），一条 WS 连接服务全屋
        # 所有卫星，hello 只在建连时发一次 ⇒ 只靠 hello 分不出"这句是谁在说"，
        # 多卫星的确认环/跨轮上下文仍按一颗桶串台（bb28 的确认被 32b8 应答）。
        # 真源是 HA 设备注册 id：`assist_pipeline.py:1214` 把发起本轮管道的卫星设备
        # id 放进 `ConversationInput.device_id`，稳定、跨重连不变。取不到/空白 →
        # **不加键**，帧形与旧版逐字节相同（加载项回落连接级默认），旧加载项零暴露。
        device_id = getattr(user_input, "device_id", None)
        if isinstance(device_id, str) and device_id.strip():
            frame["device"] = device_id.strip()
        await transport.send_message(json.dumps(frame))

        # v1.0.93 退下旗（信号线第一段）：应答流里出现 end_dialogue → 按
        # chat_log.conversation_id 记账。键必须**剥掉再交给 chat_log**：
        # core 的 delta 消费面按已知字段 .get()，多键今天无害，但那是 core 的
        # 自由裁量面——不赌它，原样只传它认识的 role/content。
        async def _capturing():
            async for msg in self._await_message_with_timeout(transport):
                if isinstance(msg, dict) and msg.get("end_dialogue"):
                    end_dialogue.mark(chat_log.conversation_id)
                    msg = {k: v for k, v in msg.items() if k != "end_dialogue"}
                yield msg

        async for content in chat_log.async_add_delta_content_stream(
            self.entity_id, _capturing()
        ):
            # v1.1.27（隐私文本纪律）：应答全文属家居隐私，INFO 会随 HA 日志落盘
            # ——只留长度，全文降 DEBUG（对齐 ws_transport:411 同款纪律）。
            _LOGGER.debug("LLM response: %s", content)
