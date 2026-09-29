"""Encryption key storage for ESPHome devices."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import JSONEncoder
from homeassistant.helpers.singleton import singleton
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

_LOGGER = logging.getLogger(__name__)

ENCRYPTION_KEY_STORAGE_VERSION = 1
ENCRYPTION_KEY_STORAGE_KEY = "esphome.encryption_keys"


def _store_file_exists(store: Store) -> bool:
    """存储文件是否真实存在——区分"首次运行"与"读取失败"的唯一判据（v1.1.27）。

    判不出路径时按"不存在"（=旧语义：首次运行建空表）处理，绝不因判据自身
    出错而阻断既有流程；OSError（权限/坏路径）按"存在"处理——宁可拒写留痕，
    也不拿空表覆盖。
    """
    try:
        path = getattr(store, "path", None)
        if path is None:
            return False
        return Path(path).exists()
    except OSError:
        return True


class EncryptionKeyData(TypedDict):
    """Encryption key storage data."""

    keys: dict[str, str]  # MAC address -> base64 encoded key


KEY_ENCRYPTION_STORAGE: HassKey[ESPHomeEncryptionKeyStorage] = HassKey(
    "esphome_encryption_key_storage"
)


class ESPHomeEncryptionKeyStorage:
    """Storage for ESPHome encryption keys."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the encryption key storage."""
        self.hass = hass
        self._store = Store[EncryptionKeyData](
            hass,
            ENCRYPTION_KEY_STORAGE_VERSION,
            ENCRYPTION_KEY_STORAGE_KEY,
            encoder=JSONEncoder,
        )
        self._data: EncryptionKeyData | None = None
        # v1.1.27：读取失败（损坏/不可读）标志——此后一律拒写，绝不用空表覆盖
        self._load_failed = False

    async def async_load(self) -> None:
        """Load encryption keys from storage.

        v1.1.27（"文件损坏=全部历史 PSK 被抹"的入口根修）：`Store.async_load()`
        返 None 有两种语义——**文件不存在**（首次运行）与**读取失败/损坏**
        （旧版 Store 记完日志返 None；新版直接 raise）。旧实现一律
        `data or {"keys": {}}` 当空表固化，随后任一 `async_store_key` 就把它
        当全量写回 ⇒ 一次损坏/半写即抹掉全部历史 PSK（设备侧密钥还在，HA 侧
        记录没了 = 再也连不上加密设备，且不可自愈）。
        现先问"文件在不在"：不存在=首次（空表）；存在却读不到=损坏 → 置
        `_load_failed` 拒写并留痕（读侧如实回 None，让调用方生成新密钥，
        但绝不落盘覆盖旧账）。
        """
        if self._data is not None or self._load_failed:
            return
        existed = await self.hass.async_add_executor_job(_store_file_exists, self._store)
        try:
            data = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 新版 Store 对损坏 JSON 直接 raise
            self._load_failed = True
            _LOGGER.error(
                "加密密钥存储读取失败（%s）：已拒绝后续写入——空表写回会抹掉全部"
                "历史 PSK；请修复/移除 %s 后重启 HA",
                err,
                getattr(self._store, "path", "?"),
            )
            return
        if data is None:
            if existed:
                self._load_failed = True
                _LOGGER.error(
                    "加密密钥存储文件存在但读不出内容（%s）：按损坏处理并拒绝写入"
                    "（旧形态会固化成空表，下一次写盘即抹掉全部历史 PSK）",
                    getattr(self._store, "path", "?"),
                )
                return
            data = {"keys": {}}
        self._data = data

    async def async_save(self) -> None:
        """Save encryption keys to storage."""
        if self._data is not None:
            await self._store.async_save(self._data)

    async def async_get_key(self, mac_address: str) -> str | None:
        """Get encryption key for a MAC address."""
        await self.async_load()
        if self._data is None:      # 读取失败：如实回 None（不猜、不写）
            _LOGGER.error(
                "加密密钥存储不可用（读取失败），无法取 MAC %s 的密钥",
                mac_address,
            )
            return None
        return self._data["keys"].get(mac_address.lower())

    async def async_store_key(self, mac_address: str, key: str) -> None:
        """Store encryption key for a MAC address."""
        await self.async_load()
        if self._data is None:
            # v1.1.27：拒写（空表写回=抹掉全部历史 PSK）。只留痕不抛：设备侧
            # 密钥本轮已设好，entry.data 仍会更新，炸掉设备流程只会更糟。
            _LOGGER.error(
                "加密密钥存储不可用（读取失败/文件损坏）：拒绝写入 MAC %s 的密钥"
                "（不写盘，避免整表被覆盖）",
                mac_address,
            )
            return
        self._data["keys"][mac_address.lower()] = key
        await self.async_save()
        _LOGGER.debug(
            "Stored encryption key for device with MAC %s",
            mac_address,
        )

    async def async_remove_key(self, mac_address: str) -> None:
        """Remove encryption key for a MAC address."""
        await self.async_load()
        if self._data is None:
            _LOGGER.error(
                "加密密钥存储不可用（读取失败），拒绝删除 MAC %s 的密钥记录",
                mac_address,
            )
            return
        lower_mac_address = mac_address.lower()
        if lower_mac_address in self._data["keys"]:
            del self._data["keys"][lower_mac_address]
            await self.async_save()
            _LOGGER.debug(
                "Removed encryption key for device with MAC %s",
                mac_address,
            )


@singleton(KEY_ENCRYPTION_STORAGE, async_=True)
async def async_get_encryption_key_storage(
    hass: HomeAssistant,
) -> ESPHomeEncryptionKeyStorage:
    """Get the encryption key storage instance."""
    storage = ESPHomeEncryptionKeyStorage(hass)
    await storage.async_load()
    return storage
