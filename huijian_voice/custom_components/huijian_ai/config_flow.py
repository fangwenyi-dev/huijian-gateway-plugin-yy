"""Config flow to configure esphome component."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import urlencode

import aiohttp
import voluptuous as vol
from aioesphomeapi import (APIClient, APIConnectionError, DeviceInfo,
                           InvalidAuthAPIError, InvalidEncryptionKeyAPIError,
                           RequiresEncryptionAPIError, ResolveAPIError,
                           wifi_mac_to_bluetooth_mac)
from homeassistant.components import zeroconf
from homeassistant.config_entries import (SOURCE_ESPHOME, SOURCE_IGNORE,
                                          SOURCE_REAUTH, SOURCE_RECONFIGURE,
                                          ConfigEntry, ConfigEntryBaseFlow,
                                          ConfigFlow, ConfigFlowResult,
                                          FlowType, OptionsFlowWithReload)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow, FlowResultType
from homeassistant.helpers import discovery_flow, selector
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.importlib import async_import_module
from homeassistant.helpers.network import get_url
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.helpers.service_info.esphome import ESPHomeServiceInfo
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.helpers.service_info.mqtt import MqttServiceInfo
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from homeassistant.util import ulid
from homeassistant.util.json import json_loads_object

from .const import (CONF_ALLOW_SERVICE_CALLS, CONF_DEBOUNCE_MINUTES,
                    CONF_DEVICE_NAME, CONF_NOISE_PSK, CONF_STT_ENTITY_ID,
                    CONF_SUBSCRIBE_LOGS, CONF_TTS_ENTITY_ID,
                    DEFAULT_ALLOW_SERVICE_CALLS, DEFAULT_DEBOUNCE_MINUTES,
                    DEFAULT_NEW_CONFIG_ALLOW_ALLOW_SERVICE_CALLS, DEFAULT_PORT,
                    DOMAIN)
from .dashboard import (async_get_or_create_dashboard_manager,
                        async_set_dashboard_info)
from .encryption_key_storage import async_get_encryption_key_storage
from .entry_data import ESPHomeConfigEntry
from .huijian import Dict, generate_qr_code, get_haid
from .huijian.http import async_setup_https
from .manager import async_replace_device

ERROR_REQUIRES_ENCRYPTION_KEY = "requires_encryption_key"
ERROR_INVALID_ENCRYPTION_KEY = "invalid_psk"
ERROR_INVALID_PASSWORD_AUTH = "invalid_auth"
_LOGGER = logging.getLogger(__name__)

ZERO_NOISE_PSK = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
DEFAULT_NAME = "huijian"


class BaseFlow(ConfigEntryBaseFlow):
    def init(self):
        self._extra = Dict()
        self._extra.setdefault("config_data", {})

    @property
    def this_data(self):
        return self.hass.data.setdefault(DOMAIN, {})

    @property
    def setup_data(self):
        return self.this_data.setdefault(self.setup_uuid, None)

    @property
    def setup_uuid(self):
        return self._extra.setup_uuid

    @setup_uuid.setter
    def setup_uuid(self, uuid):
        if uuid:
            self._extra.setup_uuid = uuid
            self.this_data[uuid] = None
            _LOGGER.info("Waiting for setup data: %s", uuid)

    def clean_setup(self):
        self.this_data.pop(self.setup_uuid, None)
        self._extra.pop("setup_uuid", None)


class ConfigFlowHandler(ConfigFlow, BaseFlow, domain=DOMAIN):
    """Handle a esphome config flow."""

    VERSION = 1

    _reauth_entry: ConfigEntry
    _reconfig_entry: ConfigEntry
    _wait_task: asyncio.Task | None = None

    def __init__(self) -> None:
        """Initialize flow."""
        self._host: str | None = None
        self._connected_address: str | None = None
        self.__name: str | None = None
        self._port: int | None = None
        self._password: str | None = None
        self._noise_required: bool | None = None
        self._noise_psk: str | None = None
        self._device_info: DeviceInfo | None = None
        # The ESPHome name as per its config
        self._device_name: str | None = None
        self._device_mac: str | None = None
        self._entry_with_name_conflict: ConfigEntry | None = None
        self.init()

    def _cancel_wait_task(self):
        if self._wait_task and not self._wait_task.done():
            self._wait_task.cancel()
            self._wait_task = None

    async def async_abort(self) -> None:
        self._cancel_wait_task()
        self.clean_setup()
        return await super().async_abort()

    async def _async_step_user_base(
        self, user_input: dict[str, Any] | None = None, error: str | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._host = user_input[CONF_HOST]
            self._port = user_input[CONF_PORT]
            return await self._async_try_fetch_device_info()

        fields: dict[Any, type] = OrderedDict()
        fields[vol.Required(CONF_HOST, default=self._host or vol.UNDEFINED)] = str
        fields[vol.Optional(CONF_PORT, default=self._port or DEFAULT_PORT)] = int

        errors = {}
        if error is not None:
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(fields),
            errors=errors,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        return await self.async_step_qrcode(user_input=user_input)

    async def async_step_qrcode(self, user_input=None):
        await async_setup_https(self.hass)
        if not self._wait_task:
            self._wait_task = self.hass.async_create_task(self._wait_for_setup_data())
        if self._wait_task.done():
            return self.async_show_progress_done(next_step_id="qrcode_done")
        if not self.setup_uuid:
            self.setup_uuid = ulid.ulid_hex()
        params = {
            "haid": await get_haid(self.hass),
            "uuid": self.setup_uuid,
            "home_name": self.hass.config.location_name,
        }
        reconfig_entry = self._get_reconfig_entry()
        if reconfig_entry and reconfig_entry.data.get("mac"):
            params.update(
                {
                    "mac": reconfig_entry.data.get("mac"),
                    "speak_id": reconfig_entry.data.get("speak_id"),
                }
            )
        internal = get_url(self.hass, prefer_external=False)
        external = get_url(self.hass, prefer_external=True) or internal
        haip = internal.split("//")[1].split(":")[0]
        image = generate_qr_code(
            f"{external}/api/huijian-ai/setup/qrcode?{urlencode(params)}"
        )
        self._extra.tip = "\n".join(
            [
                f"您的 HomeAssistant 局域网IP地址是 **{haip}**",
                f"\n{image}",
            ]
        )
        return self.async_show_progress(
            step_id="qrcode",
            progress_action="qrcode",
            description_placeholders={
                "tip": self._extra.pop("tip", ""),
            },
            progress_task=self._wait_task,
        )

    async def async_step_qrcode_done(self, user_input=None):
        errors = {}
        schema = {}
        haid = await get_haid(self.hass)
        if user_input is None:
            user_input = {}

        _LOGGER.info("setup_data: %s", self.setup_data)
        config_type = (
            self.setup_data.get("config_type", "device") if self.setup_data else None
        )
        mcp_endpoint = self.setup_data.get("mcp_endpoint") if self.setup_data else None
        _LOGGER.info("mcp_endpoint: %s", mcp_endpoint)

        if config_type == "device":
            self._name = self.setup_data.get("speak_name") or self._name
            self._host = self.setup_data[CONF_HOST]
            port = self.setup_data.get(CONF_PORT, 6053)
            try:
                self._port = int(port)
            except (TypeError, ValueError):
                self._port = 6053
                _LOGGER.exception("Invalid port value '%s', using default 6053", port)
            self._noise_psk = self.setup_data.get(CONF_NOISE_PSK)
            error = await self.fetch_device_info()
            if error:
                errors["base"] = error
                schema = {
                    vol.Required(
                        "submit_confirm", default=True
                    ): selector.BooleanSelector(),
                }
            elif not user_input.get("submit_confirm"):
                self._extra.tip = "\n".join(
                    [
                        "设备信息如下:",
                        f"**名称**: {self._name}",
                        f"**IP**: {self._host}" f"**MAC**: {self._device_mac}",
                    ]
                )
                schema = {
                    vol.Required(
                        "submit_confirm", default=True
                    ): selector.BooleanSelector(),
                }
            else:
                self._extra.config_data = {
                    "config_type": config_type,
                    "uuid": self.setup_uuid,
                    "mac": self._device_mac,
                    "speak_id": self.setup_data.get("speak_id"),
                    "mcp_endpoint": mcp_endpoint,
                }
                self.clean_setup()
                return await self._async_authenticate_or_add()

        if config_type == "assist":
            config_data = {
                "config_type": config_type,
                "uuid": self.setup_uuid,
                "speak_id": self.setup_data.get("speak_id"),
                CONF_DEVICE_NAME: self.setup_data.get("speak_name", ""),
                "mcp_endpoint": mcp_endpoint,
                "llm_endpoint": self.setup_data.get("llm_endpoint"),
                "stt_endpoint": self.setup_data.get("stt_endpoint"),
                "tts_endpoint": self.setup_data.get("tts_endpoint"),
            }
            reconfig_entry = self._get_reconfig_entry()
            if entry := self.hass.config_entries.async_entry_for_domain_unique_id(
                DOMAIN, haid
            ):
                reconfig_entry = entry
                _LOGGER.info("Found existing entry for %s", entry.title)
            if reconfig_entry:
                _LOGGER.debug("Update existing entry: %s", config_data)
                return self.async_update_reload_and_abort(
                    reconfig_entry, data=config_data
                )

            await self.async_set_unique_id(haid)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="huijian AI",
                data=config_data,
            )

        if schema:
            return self.async_show_form(
                step_id="qrcode_done",
                errors=errors,
                data_schema=vol.Schema(schema),
                description_placeholders={
                    "tip": self._extra.pop("tip", ""),
                },
            )
        return self.async_show_form(
            step_id="qrcode_done",
            errors={"base": "unknown_config_type"},
            data_schema=vol.Schema({}),
            description_placeholders={
                "tip": "配置类型未知，请重新尝试",
            },
        )

    async def _wait_for_setup_data(self):
        for _ in range(200):
            if self.setup_data:
                return
            await asyncio.sleep(0.3)
        _LOGGER.warning("Timeout waiting for setup data for %s", self.setup_uuid)

    def _get_reconfig_entry(self):
        if getattr(self, "_reauth_entry", None):
            return self._reauth_entry
        if getattr(self, "_reconfig_entry", None):
            return self._reconfig_entry
        return None

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle a flow initialized by a reauth event."""
        self._reauth_entry = self._get_reauth_entry()
        self._host = entry_data.get(CONF_HOST)
        self._port = entry_data.get(CONF_PORT)
        self._password = entry_data.get(CONF_PASSWORD)
        self._device_name = entry_data.get(CONF_DEVICE_NAME)
        self._name = self._reauth_entry.title
        return await self.async_step_qrcode()

    async def async_step_reauth_encryption_removed_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauthorization flow when encryption was removed."""
        if user_input is not None:
            self._noise_psk = None
            return await self._async_validated_connection()

        return self.async_show_form(
            step_id="reauth_encryption_removed_confirm",
            description_placeholders={"name": self._async_get_human_readable_name()},
        )

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauthorization flow."""
        errors = {}

        if (
            await self._retrieve_encryption_key_from_storage()
            or await self._retrieve_encryption_key_from_dashboard()
        ):
            error = await self.fetch_device_info()
            if error is None:
                return await self._async_authenticate_or_add()

        if user_input is not None:
            self._noise_psk = user_input[CONF_NOISE_PSK]
            error = await self.fetch_device_info()
            if error is None:
                return await self._async_authenticate_or_add()
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_NOISE_PSK): str}),
            errors=errors,
            description_placeholders={"name": self._async_get_human_readable_name()},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by a reconfig request."""
        self._reconfig_entry = self._get_reconfigure_entry()
        data = self._reconfig_entry.data
        self._host = data.get(CONF_HOST)
        self._port = data.get(CONF_PORT, DEFAULT_PORT)
        self._noise_psk = data.get(CONF_NOISE_PSK)
        self._device_name = data.get(CONF_DEVICE_NAME)
        return await self.async_step_qrcode()

    @property
    def _name(self) -> str:
        return self.__name or DEFAULT_NAME

    @_name.setter
    def _name(self, value: str) -> None:
        self.__name = value
        self.context["title_placeholders"] = {
            "name": self._async_get_human_readable_name()
        }

    async def _async_try_fetch_device_info(self) -> ConfigFlowResult:
        """Try to fetch device info and return any errors."""
        response: str | None
        if self._noise_required:
            # If we already know we need encryption, don't try to fetch device info
            # without encryption.
            response = ERROR_REQUIRES_ENCRYPTION_KEY
        else:
            # After 2024.08, stop trying to fetch device info without encryption
            # so we can avoid probe requests to check for password. At this point
            # most devices should announce encryption support and password is
            # deprecated and can be discovered by trying to connect only after they
            # interact with the flow since it is expected to be a rare case.
            response = await self.fetch_device_info()

        if response == ERROR_REQUIRES_ENCRYPTION_KEY:
            if not self._device_name and not self._noise_psk:
                # If device name is not set we can send a zero noise psk
                # to get the device name which will allow us to populate
                # the device name and hopefully get the encryption key
                # from the dashboard.
                self._noise_psk = ZERO_NOISE_PSK
                response = await self.fetch_device_info()
                self._noise_psk = None

            # Try to retrieve an existing key from dashboard or storage.
            if (
                self._device_name
                and await self._retrieve_encryption_key_from_dashboard()
            ) or (
                self._device_mac and await self._retrieve_encryption_key_from_storage()
            ):
                response = await self.fetch_device_info()

            # If the fetched key is invalid, unset it again.
            if response == ERROR_INVALID_ENCRYPTION_KEY:
                self._noise_psk = None
                response = ERROR_REQUIRES_ENCRYPTION_KEY

        if response == ERROR_REQUIRES_ENCRYPTION_KEY:
            return await self.async_step_encryption_key()
        if response is not None:
            return await self._async_step_user_base(error=response)
        return await self._async_authenticate_or_add()

    async def _async_authenticate_or_add(self) -> ConfigFlowResult:
        # Only show authentication step if device uses password
        assert self._device_info is not None
        if self._device_info.uses_password:
            return await self.async_step_authenticate()

        self._password = ""
        return await self._async_validated_connection()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user-confirmation of discovered node."""
        if user_input is not None:
            return await self._async_try_fetch_device_info()
        return self.async_show_form(
            step_id="discovery_confirm",
            description_placeholders={"name": self._async_get_human_readable_name()},
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle zeroconf discovery."""
        mac_address: str | None = discovery_info.properties.get("mac")

        # Mac address was added in Sept 20, 2021.
        # https://github.com/esphome/esphome/pull/2303
        if mac_address is None:
            return self.async_abort(reason="mdns_missing_mac")

        # mac address is lowercase and without :, normalize it
        mac_address = format_mac(mac_address)

        # Hostname is format: livingroom.local.
        device_name = discovery_info.hostname.removesuffix(".local.")

        self._device_name = device_name
        self._name = discovery_info.properties.get("friendly_name", device_name)
        self._host = discovery_info.host
        self._port = discovery_info.port
        self._device_mac = mac_address
        self._noise_required = bool(discovery_info.properties.get("api_encryption"))

        # Check if already configured
        await self.async_set_unique_id(mac_address)

        # Convert WiFi MAC to Bluetooth MAC and notify Improv BLE if waiting
        # ESPHome devices use WiFi MAC + 1 for Bluetooth MAC
        # Late import to avoid circular dependency
        # NOTE: Do not change to hass.config.components check - improv_ble is
        # config_flow only and may not be in the components registry
        if improv_ble := await async_import_module(
            self.hass, "homeassistant.components.improv_ble"
        ):
            ble_mac = wifi_mac_to_bluetooth_mac(mac_address)
            improv_ble.async_register_next_flow(self.hass, ble_mac, self.flow_id)
            _LOGGER.debug(
                "Notified Improv BLE of flow %s for BLE MAC %s (derived from WiFi MAC %s)",
                self.flow_id,
                ble_mac,
                mac_address,
            )

        await self._async_validate_mac_abort_configured(
            mac_address, self._host, self._port
        )
        return await self.async_step_discovery_confirm()

    async def _async_validate_mac_abort_configured(
        self, formatted_mac: str, host: str, port: int | None
    ) -> None:
        """Validate if the MAC address is already configured."""
        assert self.unique_id is not None
        if not (
            entry := self.hass.config_entries.async_entry_for_domain_unique_id(
                self.handler, formatted_mac
            )
        ):
            return
        if entry.source == SOURCE_IGNORE:
            # Don't call _fetch_device_info() for ignored entries
            raise AbortFlow("already_configured")
        configured_host: str | None = entry.data.get(CONF_HOST)
        configured_port: int = entry.data.get(CONF_PORT, DEFAULT_PORT)
        # When port is None (from DHCP discovery), only compare hosts
        if configured_host == host and (port is None or configured_port == port):
            # Don't probe to verify the mac is correct since
            # the host matches (and port matches if provided).
            raise AbortFlow("already_configured")
        configured_psk: str | None = entry.data.get(CONF_NOISE_PSK)
        await self._fetch_device_info(host, port or configured_port, configured_psk)
        updates: dict[str, Any] = {}
        if self._device_mac == formatted_mac:
            updates[CONF_HOST] = host
            if port is not None:
                updates[CONF_PORT] = port
        self._abort_unique_id_configured_with_details(updates=updates)

    @callback
    def _abort_unique_id_configured_with_details(self, updates: dict[str, Any]) -> None:
        """Abort if unique_id is already configured with details."""
        assert self.unique_id is not None
        if not (
            conflict_entry := self.hass.config_entries.async_entry_for_domain_unique_id(
                self.handler, self.unique_id
            )
        ):
            return
        assert conflict_entry.unique_id is not None
        if self.source == SOURCE_RECONFIGURE:
            error = "reconfigure_already_configured"
        elif updates:
            error = "already_configured_updates"
        else:
            error = "already_configured_detailed"
        self._abort_if_unique_id_configured(
            updates=updates,
            error=error,
            description_placeholders={
                "title": conflict_entry.title,
                "name": conflict_entry.data.get(CONF_DEVICE_NAME, "unknown"),
                "mac": format_mac(conflict_entry.unique_id),
            },
        )

    async def async_step_mqtt(
        self, discovery_info: MqttServiceInfo
    ) -> ConfigFlowResult:
        """Handle MQTT discovery."""
        if not discovery_info.payload:
            return self.async_abort(reason="mqtt_missing_payload")

        device_info = json_loads_object(discovery_info.payload)
        if "mac" not in device_info:
            return self.async_abort(reason="mqtt_missing_mac")

        # there will be no port if the API is not enabled
        if "port" not in device_info:
            return self.async_abort(reason="mqtt_missing_api")

        if "ip" not in device_info:
            return self.async_abort(reason="mqtt_missing_ip")

        # mac address is lowercase and without :, normalize it
        unformatted_mac = cast(str, device_info["mac"])
        mac_address = format_mac(unformatted_mac)

        device_name = cast(str, device_info["name"])

        self._device_name = device_name
        self._name = cast(str, device_info.get("friendly_name", device_name))
        self._host = cast(str, device_info["ip"])
        self._port = cast(int, device_info["port"])

        self._noise_required = "api_encryption" in device_info

        # Check if already configured
        await self.async_set_unique_id(mac_address)
        self._abort_unique_id_configured_with_details(
            updates={CONF_HOST: self._host, CONF_PORT: self._port}
        )

        return await self.async_step_discovery_confirm()

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Handle DHCP discovery."""
        mac_address = format_mac(discovery_info.macaddress)
        await self.async_set_unique_id(format_mac(mac_address))
        await self._async_validate_mac_abort_configured(
            mac_address, discovery_info.ip, None
        )
        # This should never happen since we only listen to DHCP requests
        # for configured devices.
        return self.async_abort(reason="already_configured")

    async def async_step_hassio(
        self, discovery_info: HassioServiceInfo
    ) -> ConfigFlowResult:
        """Handle Supervisor service discovery."""
        await async_set_dashboard_info(
            self.hass,
            discovery_info.slug,
            discovery_info.config["host"],
            discovery_info.config["port"],
        )
        return self.async_abort(reason="service_received")

    async def async_step_name_conflict(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle name conflict resolution."""
        assert self._entry_with_name_conflict is not None
        assert self._entry_with_name_conflict.unique_id is not None
        assert self.unique_id is not None
        assert self._device_name is not None
        return self.async_show_menu(
            step_id="name_conflict",
            menu_options=["name_conflict_migrate", "name_conflict_overwrite"],
            description_placeholders={
                "existing_mac": format_mac(self._entry_with_name_conflict.unique_id),
                "existing_title": self._entry_with_name_conflict.title,
                "mac": format_mac(self.unique_id),
                "name": self._device_name,
            },
        )

    async def async_step_name_conflict_migrate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle migration of existing entry."""
        assert self._entry_with_name_conflict is not None
        assert self._entry_with_name_conflict.unique_id is not None
        assert self.unique_id is not None
        assert self._device_name is not None
        assert self._host is not None
        old_mac = format_mac(self._entry_with_name_conflict.unique_id)
        new_mac = format_mac(self.unique_id)
        entry_id = self._entry_with_name_conflict.entry_id
        self.hass.config_entries.async_update_entry(
            self._entry_with_name_conflict,
            data={
                **self._entry_with_name_conflict.data,
                CONF_HOST: self._host,
                CONF_PORT: self._port or DEFAULT_PORT,
                CONF_PASSWORD: self._password or "",
                CONF_NOISE_PSK: self._noise_psk or "",
            },
        )
        await async_replace_device(self.hass, entry_id, old_mac, new_mac)
        self.hass.config_entries.async_schedule_reload(entry_id)
        return self.async_abort(
            reason="name_conflict_migrated",
            description_placeholders={
                "existing_mac": old_mac,
                "mac": new_mac,
                "name": self._device_name,
            },
        )

    async def async_step_name_conflict_overwrite(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle creating a new entry by removing the old one and creating new."""
        assert self._entry_with_name_conflict is not None
        if self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
            return self.async_update_reload_and_abort(
                self._entry_with_name_conflict,
                title=self._name,
                unique_id=self.unique_id,
                data=self._async_make_config_data(),
                options={
                    CONF_ALLOW_SERVICE_CALLS: DEFAULT_NEW_CONFIG_ALLOW_ALLOW_SERVICE_CALLS,
                },
            )
        await self.hass.config_entries.async_remove(
            self._entry_with_name_conflict.entry_id
        )
        return await self._async_create_entry()

    async def _async_create_entry(self) -> ConfigFlowResult:
        """Create the config entry."""
        assert self._name is not None
        assert self._device_info is not None

        # Check if Z-Wave capabilities are present and start discovery flow
        next_flow_id: str | None = None
        # If the zwave_home_id is not set, we don't know if it's a fresh
        # adapter, or the cable is just unplugged. So only start
        # the zwave_js config flow automatically if there is a
        # zwave_home_id present. If it's a fresh adapter, the manager
        # will handle starting the flow once it gets the home id changed
        # request from the ESPHome device.
        if (
            self._device_info.zwave_proxy_feature_flags
            and self._device_info.zwave_home_id
        ):
            assert self._connected_address is not None
            assert self._port is not None

            # Start Z-Wave discovery flow and get the flow ID
            zwave_result = await self.hass.config_entries.flow.async_init(
                "zwave_js",
                context={
                    "source": SOURCE_ESPHOME,
                    "discovery_key": discovery_flow.DiscoveryKey(
                        domain=DOMAIN,
                        key=self._device_info.mac_address,
                        version=1,
                    ),
                },
                data=ESPHomeServiceInfo(
                    name=self._device_info.name,
                    zwave_home_id=self._device_info.zwave_home_id,
                    ip_address=self._connected_address,
                    port=self._port,
                    noise_psk=self._noise_psk,
                ),
            )
            if zwave_result["type"] in (
                FlowResultType.ABORT,
                FlowResultType.CREATE_ENTRY,
            ):
                _LOGGER.debug(
                    "Unable to continue created Z-Wave JS config flow: %s", zwave_result
                )
            else:
                next_flow_id = zwave_result["flow_id"]

        return self.async_create_entry(
            title=self._name,
            data=self._async_make_config_data(),
            options={
                CONF_ALLOW_SERVICE_CALLS: DEFAULT_NEW_CONFIG_ALLOW_ALLOW_SERVICE_CALLS,
            },
            next_flow=(FlowType.CONFIG_FLOW, next_flow_id) if next_flow_id else None,
        )

    @callback
    def _async_make_config_data(self) -> dict[str, Any]:
        """Return config data for the entry."""
        return {
            CONF_HOST: self._host,
            CONF_PORT: self._port,
            # The API uses protobuf, so empty string denotes absence
            CONF_PASSWORD: self._password or "",
            CONF_NOISE_PSK: self._noise_psk or "",
            CONF_DEVICE_NAME: self._device_name,
            **(self._extra.config_data or {}),
        }

    @callback
    def _async_abort_wrong_device(
        self, entry: ConfigEntry, expected_mac: str, actual_mac: str
    ) -> ConfigFlowResult:
        """Abort flow because a different device was found at the IP address."""
        assert self._host is not None
        assert self._device_name is not None
        if self.source == SOURCE_RECONFIGURE:
            reason = "reconfigure_unique_id_changed"
        else:
            reason = "reauth_unique_id_changed"
        return self.async_abort(
            reason=reason,
            description_placeholders={
                "name": entry.data.get(CONF_DEVICE_NAME, entry.title),
                "host": self._host,
                "expected_mac": expected_mac,
                "unexpected_mac": actual_mac,
                "unexpected_device_name": self._device_name,
            },
        )

    async def _async_validated_connection(self) -> ConfigFlowResult:
        """Handle validated connection."""
        if self.source == SOURCE_RECONFIGURE:
            return await self._async_reconfig_validated_connection()
        if self.source == SOURCE_REAUTH:
            return await self._async_reauth_validated_connection()
        for entry in self._async_current_entries(include_ignore=False):
            if entry.data.get(CONF_DEVICE_NAME) == self._device_name:
                self._entry_with_name_conflict = entry
                return await self.async_step_name_conflict()
        return await self._async_create_entry()

    async def _async_reauth_validated_connection(self) -> ConfigFlowResult:
        """Handle reauth validated connection."""
        assert self._reauth_entry.unique_id is not None
        if self.unique_id == self._reauth_entry.unique_id:
            return self.async_update_reload_and_abort(
                self._reauth_entry,
                data=self._reauth_entry.data | self._async_make_config_data(),
            )
        assert self._host is not None
        self._abort_unique_id_configured_with_details(
            updates={
                CONF_HOST: self._host,
                CONF_PORT: self._port,
                CONF_NOISE_PSK: self._noise_psk,
            }
        )
        # Reauth was triggered a while ago, and since than
        # a new device resides at the same IP address.
        assert self._device_name is not None
        return self._async_abort_wrong_device(
            self._reauth_entry,
            format_mac(self._reauth_entry.unique_id),
            format_mac(self.unique_id),
        )

    async def _async_reconfig_validated_connection(self) -> ConfigFlowResult:
        """Handle reconfigure validated connection."""
        assert self._reconfig_entry.unique_id is not None
        assert self._host is not None
        assert self._device_name is not None
        if not (
            unique_id_matches := (self.unique_id == self._reconfig_entry.unique_id)
        ):
            self._abort_unique_id_configured_with_details(
                updates={
                    CONF_HOST: self._host,
                    CONF_PORT: self._port,
                    CONF_NOISE_PSK: self._noise_psk,
                }
            )
        for entry in self._async_current_entries(include_ignore=False):
            if (
                entry.entry_id != self._reconfig_entry.entry_id
                and entry.data.get(CONF_DEVICE_NAME) == self._device_name
            ):
                return self.async_abort(
                    reason="reconfigure_name_conflict",
                    description_placeholders={
                        "name": self._reconfig_entry.data[CONF_DEVICE_NAME],
                        "host": self._host,
                        "expected_mac": format_mac(self._reconfig_entry.unique_id),
                        "existing_title": entry.title,
                    },
                )
        if unique_id_matches:
            return self.async_update_reload_and_abort(
                self._reconfig_entry,
                data=self._reconfig_entry.data | self._async_make_config_data(),
            )
        if self._reconfig_entry.data.get(CONF_DEVICE_NAME) == self._device_name:
            self._entry_with_name_conflict = self._reconfig_entry
            return await self.async_step_name_conflict()
        return self._async_abort_wrong_device(
            self._reconfig_entry,
            format_mac(self._reconfig_entry.unique_id),
            format_mac(self.unique_id),
        )

    async def async_step_encryption_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle getting psk for transport encryption."""
        errors = {}
        if user_input is not None:
            self._noise_psk = user_input[CONF_NOISE_PSK]
            error = await self.fetch_device_info()
            if error is None:
                return await self._async_authenticate_or_add()
            errors["base"] = error

        return self.async_show_form(
            step_id="encryption_key",
            data_schema=vol.Schema({vol.Required(CONF_NOISE_PSK): str}),
            errors=errors,
            description_placeholders={"name": self._async_get_human_readable_name()},
        )

    @callback
    def _async_get_human_readable_name(self) -> str:
        """Return a human readable name for the entry."""
        entry: ConfigEntry | None = None
        if self.source == SOURCE_REAUTH:
            entry = self._reauth_entry
        elif self.source == SOURCE_RECONFIGURE:
            entry = self._reconfig_entry
        friendly_name = self._name
        device_name = self._device_name
        if (
            device_name
            and friendly_name in (DEFAULT_NAME, device_name)
            and entry
            and entry.title != friendly_name
        ):
            friendly_name = entry.title
        if not device_name or friendly_name == device_name:
            return friendly_name
        return f"{friendly_name} ({device_name})"

    async def async_step_authenticate(
        self, user_input: dict[str, Any] | None = None, error: str | None = None
    ) -> ConfigFlowResult:
        """Handle getting password for authentication."""
        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            error = await self.try_login()
            if error:
                return await self.async_step_authenticate(error=error)
            return await self._async_validated_connection()

        errors = {}
        if error is not None:
            errors["base"] = error

        return self.async_show_form(
            step_id="authenticate",
            data_schema=vol.Schema({vol.Required("password"): str}),
            description_placeholders={"name": self._async_get_human_readable_name()},
            errors=errors,
        )

    async def _fetch_device_info(
        self, host: str, port: int | None, noise_psk: str | None
    ) -> str | None:
        """Fetch device info from API and return any errors."""
        zeroconf_instance = await zeroconf.async_get_instance(self.hass)
        cli = APIClient(
            host,
            port or DEFAULT_PORT,
            self._password or "",
            zeroconf_instance=zeroconf_instance,
            noise_psk=noise_psk,
        )
        try:
            await cli.connect()
            self._device_info = await cli.device_info()
            self._connected_address = cli.connected_address
        except InvalidAuthAPIError:
            return ERROR_INVALID_PASSWORD_AUTH
        except RequiresEncryptionAPIError:
            return ERROR_REQUIRES_ENCRYPTION_KEY
        except InvalidEncryptionKeyAPIError as ex:
            if ex.received_name:
                device_name_changed = self._device_name != ex.received_name
                self._device_name = ex.received_name
                if ex.received_mac:
                    self._device_mac = format_mac(ex.received_mac)
                if not self._name or device_name_changed:
                    self._name = ex.received_name
            return ERROR_INVALID_ENCRYPTION_KEY
        except ResolveAPIError:
            return "resolve_error"
        except APIConnectionError:
            return "connection_error"
        finally:
            await cli.disconnect(force=True)
        self._device_mac = format_mac(self._device_info.mac_address)
        self._device_name = self._device_info.name
        self._name = self._device_info.friendly_name or self._device_info.name
        return None

    async def fetch_device_info(self) -> str | None:
        """Fetch device info from API and return any errors."""
        assert self._host is not None
        assert self._port is not None
        if error := await self._fetch_device_info(
            self._host, self._port, self._noise_psk
        ):
            return error
        assert self._device_info is not None
        mac_address = format_mac(self._device_info.mac_address)
        await self.async_set_unique_id(mac_address, raise_on_progress=False)
        if self.source not in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
            self._abort_unique_id_configured_with_details(
                updates={
                    CONF_HOST: self._host,
                    CONF_PORT: self._port,
                    CONF_NOISE_PSK: self._noise_psk,
                }
            )

        return None

    async def try_login(self) -> str | None:
        """Try logging in to device and return any errors."""
        zeroconf_instance = await zeroconf.async_get_instance(self.hass)
        assert self._host is not None
        assert self._port is not None
        cli = APIClient(
            self._host,
            self._port,
            self._password,
            zeroconf_instance=zeroconf_instance,
            noise_psk=self._noise_psk,
        )

        try:
            await cli.connect(login=True)
        except InvalidAuthAPIError:
            return "invalid_auth"
        except APIConnectionError:
            return "connection_error"
        finally:
            await cli.disconnect(force=True)

        return None

    async def _retrieve_encryption_key_from_dashboard(self) -> bool:
        """Try to retrieve the encryption key from the dashboard.

        Return boolean if a key was retrieved.
        """
        if (
            self._device_name is None
            or (manager := await async_get_or_create_dashboard_manager(self.hass))
            is None
            or (dashboard := manager.async_get()) is None
        ):
            return False

        await dashboard.async_request_refresh()
        if not dashboard.last_update_success:
            return False

        device = dashboard.data.get(self._device_name)

        if device is None:
            return False

        try:
            noise_psk = await dashboard.api.get_encryption_key(device["configuration"])
        except aiohttp.ClientError as err:
            _LOGGER.error("Error talking to the dashboard: %s", err)
            return False
        except json.JSONDecodeError:
            _LOGGER.exception("Error parsing response from dashboard")
            return False

        self._noise_psk = noise_psk
        return True

    async def _retrieve_encryption_key_from_storage(self) -> bool:
        """Try to retrieve the encryption key from storage.

        Return boolean if a key was retrieved.
        """
        # Try to get MAC address from current flow state or reauth entry
        mac_address = self._device_mac
        if mac_address is None and self._reauth_entry is not None:
            # In reauth flow, get MAC from the existing entry's unique_id
            mac_address = self._reauth_entry.unique_id

        assert mac_address is not None

        storage = await async_get_encryption_key_storage(self.hass)
        if stored_key := await storage.async_get_key(mac_address):
            self._noise_psk = stored_key
            return True

        return False

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ESPHomeConfigEntry,
    ) -> OptionsFlowHandler:
        """Get the options flow for this handler."""
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlowWithReload):
    """Handle a option flow for esphome."""

    DOMAIN_NAMES = {
        "light": "灯",
        "switch": "开关",
        "climate": "空调",
        "cover": "窗帘",
        "fan": "风扇",
        "media_player": "媒体",
        "button": "窗户",
        "lock": "锁",
        "valve": "阀门",
    }
    INTENT_NAMES = {
        "TurnDeviceOn": "打开",
        "TurnDeviceOff": "关闭",
        "ControlWindow": "窗户",
        "AdjustDeviceAttribute": "调节",
        "SetDeviceMode": "设模式",
    }

    @staticmethod
    def _format_action_summary(action: dict) -> str:
        """Format a single action into a readable summary string."""
        intent = action.get("intent") or action.get("name", "")
        params = action.get("params") or action.get("parameters", {})

        if intent in ("ControlWindow", "WindowControl"):
            action_type = params.get("action", "")
            action_label = {
                "open": "开窗",
                "close": "关窗",
                "pause": "暂停窗户",
                "a": "窗户A",
            }.get(action_type, f"窗户({action_type})")
            targets = params.get("target", [])
            areas = []
            for t in targets if isinstance(targets, list) else [targets]:
                if isinstance(t, dict) and t.get("area"):
                    areas.append(t["area"])
            prefix = f"{areas[0]}" if areas else ""
            return f"{prefix}{action_label}"

        if intent in ("TurnDeviceOn", "TurnDeviceOff"):
            action_label = "打开" if intent == "TurnDeviceOn" else "关闭"
            targets = params.get("target", [])
            parts = []
            for t in targets if isinstance(targets, list) else [targets]:
                if not isinstance(t, dict):
                    continue
                area = t.get("area", "")
                devices = t.get("devices", [])
                for d in devices if isinstance(devices, list) else [devices]:
                    if not isinstance(d, dict):
                        continue
                    domains = d.get("domains", [])
                    for domain in domains if isinstance(domains, list) else [domains]:
                        name = OptionsFlowHandler.DOMAIN_NAMES.get(domain, domain)
                        prefix = f"{area}" if area else ""
                        parts.append(f"{prefix}{name}")
            if parts:
                return action_label + "+".join(parts)
            return action_label + "设备"

        if intent in ("HassCreateVoiceScene",):
            return "创建场景"

        if intent in ("HassDeleteVoiceScene",):
            return "删除场景"

        if intent == "HassBroadcast":
            return "广播"

        return intent

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage voice scenes and automations inline - support batch delete."""
        from .intent_automation import get_automation_store
        from .intent_voice_scene import get_voice_scene_store

        voice_store = get_voice_scene_store(self.hass)
        auto_store = get_automation_store(self.hass)
        scenes = await voice_store.get_all_scenes()
        automations = await auto_store.get_all_automations()

        if user_input is not None:
            to_delete = user_input.get("to_delete", [])
            if isinstance(to_delete, str):
                to_delete = [to_delete]
            if to_delete:
                deleted = []
                failed = []
                for scene_id in to_delete:
                    success, msg = await voice_store.delete_scene(scene_id=scene_id)
                    if success:
                        deleted.append(scene_id)
                    else:
                        failed.append(f"{scene_id}: {msg}")
                if deleted:
                    return self.async_show_form(
                        step_id="voice_scene_delete_result",
                        description_placeholders={
                            "result_msg": f"已删除 {len(deleted)} 个场景"
                        },
                        last_step=False,
                    )

            to_delete_auto = user_input.get("to_delete_auto", [])
            if isinstance(to_delete_auto, str):
                to_delete_auto = [to_delete_auto]
            if to_delete_auto:
                deleted_auto = []
                failed_auto = []
                for auto_id in to_delete_auto:
                    success, msg = await auto_store.delete_automation(auto_id)
                    if success:
                        deleted_auto.append(auto_id)
                    else:
                        failed_auto.append(f"{auto_id}: {msg}")
                if deleted_auto:
                    return self.async_show_form(
                        step_id="voice_scene_delete_result",
                        description_placeholders={
                            "result_msg": f"已删除 {len(deleted_auto)} 个自动化"
                        },
                        last_step=False,
                    )

            to_options = user_input.get("to_options", False)
            if to_options:
                return await self.async_step_options()

            return await self.async_step_init()

        scene_lines = []
        for i, scene in enumerate(scenes, 1):
            trigger = scene.get("trigger_phrase", "未命名")
            actions = scene.get("actions", [])
            created = scene.get("created_at", "")
            created_short = created[:19] if created else ""
            action_summaries = [self._format_action_summary(a) for a in actions]
            action_text = "、".join(action_summaries)
            scene_lines.append(f"{i}. 「{trigger}」 - {action_text} ({created_short})")
        scene_desc = "\n".join(scene_lines) if scene_lines else "暂无语音场景"

        auto_lines = []
        for i, auto in enumerate(automations, 1):
            trigger = auto.get("trigger", {})
            entity_id = trigger.get("entity_id", "未知传感器")
            above = trigger.get("above")
            below = trigger.get("below")
            condition = ""
            if above is not None:
                condition += f"> {above}"
            if below is not None:
                condition += f" < {below}" if condition else f"< {below}"
            actions = auto.get("actions", [])
            action_summaries = []
            for a in actions:
                intent_name = a.get("name") or a.get("intent", "Unknown")
                action_summaries.append(intent_name)
            auto_lines.append(
                f"{i}. {entity_id} ({condition}) -> {', '.join(action_summaries)}"
            )
        auto_desc = "\n".join(auto_lines) if auto_lines else "暂无传感器自动化"

        scene_options = {}
        for scene in scenes:
            sid = scene.get("scene_id", "")
            trigger = scene.get("trigger_phrase", "未知")
            scene_options[sid] = f"删除语音场景「{trigger}」"

        auto_options = {}
        for auto in automations:
            aid = auto.get("automation_id", "")
            trigger = auto.get("trigger", {}).get("entity_id", "未知")
            auto_options[aid] = f"删除自动化「{trigger}」"

        data_schema = vol.Schema({})
        if scene_options:
            data_schema = data_schema.extend(
                {
                    vol.Optional("to_delete", default=[]): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"value": k, "label": v}
                                for k, v in scene_options.items()
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            multiple=True,
                        ),
                    ),
                }
            )
        if auto_options:
            data_schema = data_schema.extend(
                {
                    vol.Optional("to_delete_auto", default=[]): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                {"value": k, "label": v}
                                for k, v in auto_options.items()
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            multiple=True,
                        ),
                    ),
                }
            )

        internal_url = get_url(self.hass, prefer_external=False)
        manage_url_text = f"管理界面：{internal_url}/api/huijian-ai/manage-page"

        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
            description_placeholders={
                "scene_list": scene_desc,
                "auto_list": auto_desc,
                "manage_url": manage_url_text,
            },
            last_step=False,
        )

    async def async_step_voice_scene_delete_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show delete result and allow further management."""
        return await self.async_step_init()

    async def async_step_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show options form."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_ALLOW_SERVICE_CALLS,
                    default=self.config_entry.options.get(
                        CONF_ALLOW_SERVICE_CALLS, DEFAULT_ALLOW_SERVICE_CALLS
                    ),
                ): bool,
                vol.Required(
                    CONF_SUBSCRIBE_LOGS,
                    default=self.config_entry.options.get(CONF_SUBSCRIBE_LOGS, False),
                ): bool,
                vol.Optional(
                    CONF_DEBOUNCE_MINUTES,
                    default=self.config_entry.options.get(
                        CONF_DEBOUNCE_MINUTES, DEFAULT_DEBOUNCE_MINUTES
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=60)),
                vol.Optional(
                    CONF_TTS_ENTITY_ID,
                    default=self.config_entry.options.get(
                        CONF_TTS_ENTITY_ID, "tts.huijian_speech"
                    ),
                ): str,
                vol.Optional(
                    CONF_STT_ENTITY_ID,
                    default=self.config_entry.options.get(
                        CONF_STT_ENTITY_ID, "stt.huijian_asr"
                    ),
                ): str,
            }
        )
        defaults = dict(self.config_entry.options)
        data_schema = self.add_suggested_values_to_schema(data_schema, defaults)
        return self.async_show_form(step_id="options", data_schema=data_schema)
