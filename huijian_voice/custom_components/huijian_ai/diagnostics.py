"""Diagnostics support for ESPHome."""

from __future__ import annotations

from typing import Any

from homeassistant.components.bluetooth import async_scanner_by_source
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import CONF_NOISE_PSK
from .const import CONF_DEVICE_NAME
from .dashboard import async_get_dashboard
from .entry_data import ESPHomeConfigEntry

REDACT_KEYS = {CONF_NOISE_PSK, CONF_PASSWORD, "mac_address", "bluetooth_mac_address"}
CONFIGURED_DEVICE_KEYS = (
    "configuration",
    "current_version",
    "deployed_version",
    "loaded_integrations",
    "target_platform",
)
# v1.1.27（凭据面收口）：端点允许内嵌 `?token=<真 token>`（加载项 translations
# 指引的强制模式），而 REDACT_KEYS 只按**精确键名**掩码——`as_dict()` 原样导出
# 即凭据外泄。端点类键改值级脱敏，纪律：只留长度 + 首字节。
ENDPOINT_KEYS = ("llm_endpoint", "stt_endpoint", "tts_endpoint", "mcp_endpoint")


def _mask_endpoint_value(value: Any) -> Any:
    """端点值脱敏：只留长度与首字节（host/path/query 一律不落）。"""
    if not isinstance(value, str):
        return value
    if not value:
        return value
    return f"<redacted len={len(value)} head={value[0]}>"


def _redact_endpoints(data: Any) -> Any:
    """递归对端点类键做值级脱敏（嵌套 data/options 一并覆盖，不改写入参）。"""
    if isinstance(data, dict):
        return {
            key: (_mask_endpoint_value(value)
                  if key in ENDPOINT_KEYS else _redact_endpoints(value))
            for key, value in data.items()
        }
    if isinstance(data, (list, tuple)):
        return [_redact_endpoints(item) for item in data]
    return data


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ESPHomeConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    diag: dict[str, Any] = {}

    diag["config"] = config_entry.as_dict()

    # 未加载/setup 失败的条目没有 runtime_data（core 在 unload 成功后会删除
    # 该属性；类级 annotation 不保证存在）——下载诊断信息不该炸日志。
    entry_data = getattr(config_entry, "runtime_data", None)
    if entry_data is None:
        diag["note"] = "entry not loaded; runtime data unavailable"
        # v1.1.27：早退路径同样过值级端点脱敏（config 已在前填好）
        return async_redact_data(_redact_endpoints(diag), REDACT_KEYS)
    device_info = entry_data.device_info
    device_name: str | None = (
        device_info.name if device_info else config_entry.data.get(CONF_DEVICE_NAME)
    )

    if (storage_data := await entry_data.store.async_load()) is not None:
        diag["storage_data"] = storage_data

    if (
        device_info
        and (
            scanner_mac := device_info.bluetooth_mac_address or device_info.mac_address
        )
        and (scanner := async_scanner_by_source(hass, scanner_mac.upper()))
        and (bluetooth_device := entry_data.bluetooth_device)
    ):
        diag["bluetooth"] = {
            "connections_free": bluetooth_device.ble_connections_free,
            "connections_limit": bluetooth_device.ble_connections_limit,
            "available": bluetooth_device.available,
            "scanner": await scanner.async_diagnostics(),
        }

    diag_dashboard: dict[str, Any] = {"configured": False}
    diag["dashboard"] = diag_dashboard
    if dashboard := async_get_dashboard(hass):
        diag_dashboard["configured"] = True
        diag_dashboard["supports_update"] = dashboard.supports_update
        diag_dashboard["last_update_success"] = dashboard.last_update_success
        diag_dashboard["last_exception"] = dashboard.last_exception
        diag_dashboard["addon"] = dashboard.addon_slug
        if device_name and dashboard.data:
            diag_dashboard["has_matching_name"] = device_name in dashboard.data
            if data := dashboard.data.get(device_name):
                diag_dashboard["device"] = {
                    key: data.get(key) for key in CONFIGURED_DEVICE_KEYS
                }

    # v1.1.27：键名掩码之前先做端点值级脱敏（内嵌 ?token= 不在键名管辖内）
    return async_redact_data(_redact_endpoints(diag), REDACT_KEYS)
