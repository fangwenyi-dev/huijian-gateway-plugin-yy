"""Support for esphome devices."""

from __future__ import annotations

import logging

from aioesphomeapi import APIClient, APIConnectionError
from homeassistant.components import zeroconf
from homeassistant.components.bluetooth import async_remove_scanner
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, Platform
from homeassistant.const import __version__ as ha_version
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.issue_registry import async_delete_issue
from homeassistant.helpers.network import get_url
from homeassistant.helpers.typing import ConfigType

from . import assist_satellite, dashboard, ffmpeg_proxy
from .api import async_setup_api
from .const import (CONF_BLUETOOTH_MAC_ADDRESS, CONF_CONFIG_TYPE,
                    CONF_LLM_ENDPOINT, CONF_MCP_ENDPOINT, CONF_NOISE_PSK,
                    CONF_STT_ENDPOINT, CONF_TTS_ENDPOINT, DOMAIN,
                    VOICE_CHANNELS, VOICE_WS_PORT)
from .domain_data import DomainData
from .encryption_key_storage import async_get_encryption_key_storage
from .entry_data import ESPHomeConfigEntry, RuntimeEntryData
from .huijian import LOGGER, Dict, get_entry_data, mcp_transport
from .huijian.http import async_setup_https
from .intent import async_setup_intents
from .intent_automation import get_automation_manager, reset_automation_globals
from .intent_voice_scene import reset_voice_scene_globals
from .manager import (DEVICE_CONFLICT_ISSUE_FORMAT, ESPHomeManager,
                      cleanup_instance)
from .websocket_api import async_setup as async_setup_websocket_api

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

CLIENT_INFO = f"Home Assistant {ha_version}"

# 自动补建判定（v1.0.16 语义修正）：仅当域内确无 config_type=assist 条目时才
# 经 SOURCE_IMPORT 补建一条（端点默认本机加载项 :8000）；有则不碰——用户自建/
# 改配的条目一律尊重。旧实现曾在 entry.data 写入一次性持久标志位（"置位后
# 永不再试"），副作用实发（2026-09-08 台架两轮）：用户手删引擎条目后设备
# setup 永不自愈，卫星静默挂到 validation-error。现每次 setup 都幂等检查——
# 幂等安全（有 assist 条目即早退；补建失败也只记日志 fail-open），该持久标志
# 字段不再读写，存量条目残留无影响（防回退钉桩见 tests）。


def _assist_default_data(hass: HomeAssistant) -> dict | None:
    """按 HA internal URL host 生成默认 assist 语音引擎条目数据。

    集成与 huijian_voice 加载项同宿主（加载项 host_network 监听宿主 :8000），
    internal URL 的 host 即宿主局域网地址。host 解析不出（无 internal/异常）
    返回 None，调用方跳过自动补建（不阻塞设备装配）。
    """
    try:
        from urllib.parse import urlparse

        url = get_url(hass, prefer_external=False)
        host = urlparse(url).hostname or ""
    except Exception:  # noqa: BLE001 —— 装饰性推导，绝不阻断设备装配
        return None
    if not host:
        return None
    data = {
        CONF_CONFIG_TYPE: "assist",
        CONF_MCP_ENDPOINT: None,
        # B0 根治：原写 CONF_DEVICE_NAME 键但本模块并未 import 该常量 →
        # 真机 host 解析成功即 NameError，自动补建从未生效（v1.0.7 实锤）。
        # async_step_import 消费的键是 speak_name（与 qrcode setup_data 同名，
        # 由 config_flow 落进条目 data 的 device_name），此处直接给 speak_name。
        "speak_name": "huijian AI 语音引擎",
    }
    for channel in VOICE_CHANNELS:
        data[f"{channel}_endpoint"] = f"ws://{host}:{VOICE_WS_PORT}/xiaozhi/v1/{channel}"
    return data


async def _async_auto_ensure_assist(hass: HomeAssistant, entry: ESPHomeConfigEntry) -> None:
    """device 型语音卫星装配后，确保存在 assist 引擎条目（fail-open）。

    触发条件：config_type=device（语音卫星入驻）且 HA 域内尚无 config_type=assist
    的条目。通过 config flow 的 SOURCE_IMPORT 分支自动建/更 assist 条目（其端点
    默认本机加载项 :8000，用户可在条目「重新配置」修改）。任何失败只记日志
    ——不能因补建问题拖垮设备装配或 HA 启动。
    """
    if entry.data.get(CONF_CONFIG_TYPE) == "assist":
        return
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id != entry.entry_id and other.data.get(
            CONF_CONFIG_TYPE
        ) == "assist":
            return  # 已有 assist 引擎条目（用户手建/改配或此前自动建）——不碰
    try:
        # 包 try 而非只包 async_init：端点推导纯函数同样必须 fail-open，
        # 任何异常（B0 这类 NameError、get_url 行为变化等）都不得逃出
        # fire-and-forget 任务留下 "Task exception was never retrieved"。
        data = _assist_default_data(hass)
    except Exception:  # noqa: BLE001
        LOGGER.exception("assist 端点推导失败（设备装配不受影响）")
        return
    if not data:
        LOGGER.debug("assist 自动补建跳过：无法解析本机加载项 host（internal URL 缺失）")
        return
    try:
        await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=data
        )
    except Exception:  # noqa: BLE001 —— fail-open：补建失败不影响设备
        LOGGER.exception("assist 语音引擎自动补建失败（设备装配不受影响）")


# 引擎实体在 stt.py/tts.py/conversation.py 中固定 entity_id（显式赋值）——
# 管道接线直接引用这些字面量，与三平台保持同步。
_HUIJIAN_STT_ENTITY = "stt.huijian_asr"
_HUIJIAN_TTS_ENTITY = "tts.huijian_speech"
_HUIJIAN_AGENT_ENTITY = "conversation.huijian_agent"
HUIJIAN_PIPELINE_NAME = "慧尖语音"


async def _async_ensure_huijian_pipeline(hass: HomeAssistant) -> None:
    """assist 引擎条目装配后，把引擎三件套接成 core 语音管道（v1.0.17）。

    台架实发链（2026-09-08 剥洋葱）：引擎条目齐全，但 core assist_pipeline
    的默认管道 Speech-to-text 为空——卫星按键仍 validation-error
    （"the pipeline does not support speech-to-text"）。"有引擎没管道"是
    从未接线的产品缺口：安装向导与自动补建都止步于引擎条目。

    策略（幂等、尊重用户）：
      1. 名称「慧尖语音」的管道不存在则建（stt/tts 用本集成固定实体）；
      2. 建/更 conversation_engine 指向 huijian_agent（对话实体存在时）；
      3. 仅当当前 preferred（默认）管道不存在或没有 STT 时，才把 preferred
         接管给慧尖管道——用户自己配好的默认管道绝不抢占。
    任何失败只记日志（fail-open），不阻塞条目装配。
    """
    try:
        from homeassistant.components.assist_pipeline import pipeline as ap

        pipeline_data = hass.data[ap.KEY_ASSIST_PIPELINE]
        col = pipeline_data.pipeline_store
        items = col.async_items() or []

        existing = next(
            (p for p in items if p.name == HUIJIAN_PIPELINE_NAME), None
        )
        if existing is None:
            pipeline = await ap.async_create_default_pipeline(
                hass,
                stt_engine_id=_HUIJIAN_STT_ENTITY,
                tts_engine_id=_HUIJIAN_TTS_ENTITY,
                pipeline_name=HUIJIAN_PIPELINE_NAME,
            )
            if pipeline is None:
                LOGGER.warning(
                    "慧尖语音管道未创建：STT/TTS 引擎未就绪（%s/%s）——"
                    "确认加载项在线并重启 HA 重试",
                    _HUIJIAN_STT_ENTITY,
                    _HUIJIAN_TTS_ENTITY,
                )
                return
            mine = pipeline
        else:
            mine = existing

        updates: dict = {}
        if (
            mine.conversation_engine != _HUIJIAN_AGENT_ENTITY
            and er.async_get(hass).async_get(_HUIJIAN_AGENT_ENTITY) is not None
        ):
            updates["conversation_engine"] = _HUIJIAN_AGENT_ENTITY
        if mine.stt_engine != _HUIJIAN_STT_ENTITY:
            updates["stt_engine"] = _HUIJIAN_STT_ENTITY
        if mine.tts_engine != _HUIJIAN_TTS_ENTITY:
            updates["tts_engine"] = _HUIJIAN_TTS_ENTITY
        if updates:
            await ap.async_update_pipeline(hass, mine, **updates)

        # preferred（默认管道）接管：只救空壳/缺失，不抢用户已配好的默认
        preferred_id = col.async_get_preferred_item()
        if preferred_id != mine.id:
            cur = next(
                (p for p in col.async_items() if p.id == preferred_id), None
            )
            if cur is None or not cur.stt_engine:
                col.async_set_preferred_item(mine.id)
                LOGGER.info(
                    "默认语音管道无 STT，已接管为「%s」（可随时在"
                    " 设置→语音助手→管道 改回自己的默认）",
                    HUIJIAN_PIPELINE_NAME,
                )
    except Exception:  # noqa: BLE001 —— fail-open：管道装配不影响条目 setup
        LOGGER.exception("慧尖语音管道自动装配失败（引擎条目装配不受影响）")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the esphome component."""
    ffmpeg_proxy.async_setup(hass)
    await assist_satellite.async_setup(hass)
    await dashboard.async_setup(hass)
    async_setup_websocket_api(hass)

    await async_setup_https(hass)
    await async_setup_api(hass)
    await async_setup_intents(hass)

    manager = get_automation_manager(hass)
    await manager.async_start()

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ESPHomeConfigEntry) -> bool:
    """Set up the esphome component."""
    LOGGER.info("Setup entry: %s", [entry.title, entry.entry_id, entry.data])
    config_type = entry.data.get("config_type")
    if config_type == "assist":
        PLATFORMS = set()
        entry.runtime_data = Dict(loaded_platforms=PLATFORMS)
        if entry.data.get("llm_endpoint"):
            PLATFORMS.add(Platform.CONVERSATION)
        if entry.data.get("stt_endpoint"):
            PLATFORMS.add(Platform.STT)
        if entry.data.get("tts_endpoint"):
            PLATFORMS.add(Platform.TTS)
        await mcp_transport.async_setup_entry(hass, entry)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        # v1.0.17：引擎实体就位后接线 core 语音管道（fail-open，见函数注释）。
        # forward 已 await 平台装配，固定 entity_id 此刻在注册表可见。
        await _async_ensure_huijian_pipeline(hass)
        return True

    host: str = entry.data[CONF_HOST]
    port: int = entry.data[CONF_PORT]
    password: str | None = entry.data[CONF_PASSWORD]
    noise_psk: str | None = entry.data.get(CONF_NOISE_PSK)

    zeroconf_instance = await zeroconf.async_get_instance(hass)

    cli = APIClient(
        host,
        port,
        password,
        client_info=CLIENT_INFO,
        zeroconf_instance=zeroconf_instance,
        noise_psk=noise_psk,
        timezone=hass.config.time_zone,
    )

    domain_data = DomainData.get(hass)
    entry_data = RuntimeEntryData(
        client=cli,
        entry_id=entry.entry_id,
        title=entry.title,
        store=domain_data.get_or_create_store(hass, entry),
        original_options=dict(entry.options),
    )
    entry.runtime_data = entry_data

    manager = ESPHomeManager(
        hass, entry, host, password, cli, zeroconf_instance, domain_data
    )
    await manager.async_start()

    await mcp_transport.async_setup_entry(hass, entry)
    # 语音卫星入驻后自动补建 assist 引擎条目（若 HA 尚无）：三平台实体
    # (conversation/stt/tts) 随慧尖设备安装自动注册、端点默认本机加载项 :8000。
    # 不 await——补建走独立 config flow(SOURCE_IMPORT)，失败 fail-open。
    if entry.data.get(CONF_CONFIG_TYPE) != "assist":
        hass.async_create_task(
            _async_auto_ensure_assist(hass, entry)
        )
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ESPHomeConfigEntry):
    """Handle update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ESPHomeConfigEntry) -> bool:
    """Unload an esphome config entry."""
    this_data = get_entry_data(hass, entry)
    # mcp_transport 一并在此关闭：core 在 unload 成功后即删除 assist 的
    # runtime_data，remove 回调再取已无对象（close 不可达）。unload 是唯一
    # 数据仍存活的钩子；device 条目走 hass.data，async_remove_entry 内 pop
    # 自身保证 remove 回调二次调用为 no-op，不会双关。
    for k in ["llm_transport", "stt_transport", "tts_transport", "mcp_transport"]:
        if transport := this_data.get(k):
            await transport.async_remove_entry()

    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, entry.runtime_data.loaded_platforms
    )
    if unload_ok:
        await cleanup_instance(entry)
        reset_automation_globals()
        reset_voice_scene_globals()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ESPHomeConfigEntry) -> None:
    """Remove an esphome config entry."""
    if bluetooth_mac_address := entry.data.get(CONF_BLUETOOTH_MAC_ADDRESS):
        async_remove_scanner(hass, bluetooth_mac_address.upper())
    async_delete_issue(
        hass, DOMAIN, DEVICE_CONFLICT_ISSUE_FORMAT.format(entry.entry_id)
    )
    await DomainData.get(hass).get_or_create_store(hass, entry).async_remove()

    if transport := get_entry_data(hass, entry, "mcp_transport"):
        await transport.async_remove_entry()

    await _async_clear_dynamic_encryption_key(hass, entry)


async def _async_clear_dynamic_encryption_key(
    hass: HomeAssistant, entry: ESPHomeConfigEntry
) -> None:
    """Clear the dynamic encryption key on the device and from storage."""
    if entry.unique_id is None or entry.data.get(CONF_NOISE_PSK) is None:
        return

    # Only clear the key if it's stored in our storage, meaning it was
    # dynamically generated by us and not user-provided
    storage = await async_get_encryption_key_storage(hass)
    if await storage.async_get_key(entry.unique_id) is None:
        return

    host: str = entry.data[CONF_HOST]
    port: int = entry.data[CONF_PORT]
    password: str | None = entry.data[CONF_PASSWORD]
    noise_psk: str | None = entry.data.get(CONF_NOISE_PSK)

    zeroconf_instance = await zeroconf.async_get_instance(hass)

    cli = APIClient(
        host,
        port,
        password,
        client_info=CLIENT_INFO,
        zeroconf_instance=zeroconf_instance,
        noise_psk=noise_psk,
        timezone=hass.config.time_zone,
    )

    try:
        await cli.connect()
        # Clear the encryption key on the device by passing an empty key
        if not await cli.noise_encryption_set_key(b""):
            _LOGGER.debug(
                "Could not clear dynamic encryption key for ESPHome device %s: Device rejected key removal",
                entry.unique_id,
            )
            return
    except APIConnectionError as exc:
        _LOGGER.debug(
            "Could not connect to ESPHome device %s to clear dynamic encryption key: %s",
            entry.unique_id,
            exc,
        )
        return
    finally:
        await cli.disconnect()

    await storage.async_remove_key(entry.unique_id)
