import io
import json
import logging

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import instance_id

from ..const import DOMAIN

LOGGER = logging.getLogger(__name__)


class Dict(dict):
    def __getattr__(self, item):
        value = self.get(item)
        return Dict(value) if isinstance(value, dict) else value

    def __setattr__(self, key, value):
        self[key] = Dict(value) if isinstance(value, dict) else value

    def to_json(self, **kwargs):
        return json.dumps(self, **kwargs)


async def get_haid(hass):
    return await instance_id.async_get(hass)


def get_entry_data(hass, entry, field=None, set_default=None, pop=False):
    config_type = entry.data.get("config_type")
    if config_type == "assist":
        # HA core 在 unload 成功后执行 object.__delattr__(entry, "runtime_data")，
        # 且 setup 失败的条目从未 set 过该属性（类级 annotation 不提供缺省值）；
        # 而 remove 回调在 unload 之后运行——裸访问在删除 assist 条目时必炸
        # AttributeError（2026-09-08 台架实发 "Error calling entry remove
        # callback"）。数据缺席即视为空：读路径返回 None；写路径（仅 setup 期）
        # 永远发生在 runtime_data 构建之后，不受此守卫影响。
        data = getattr(entry, "runtime_data", None)
        if data is None:
            # 缺席语义与在场空 dict 对齐：field 读→None、set_default→默认值、
            # 无 field→空视图（写不落盘——数据容器已不存在，本就无法持久）。
            tmp: dict = {}
            if field and set_default is not None:
                return tmp.setdefault(field, set_default)
            return None if field else {}
    else:
        domain_data = hass.data.setdefault(DOMAIN, {})
        data = domain_data.setdefault(entry.entry_id, {})

    if field and pop:
        return data.pop(field, None)
    if field and set_default is not None:
        return data.setdefault(field, set_default)
    if field:
        return data.get(field)
    return data


def get_config_entry(hass, speak_id=None, mac=None):
    for entry in hass.config_entries.async_entries(DOMAIN):
        data = Dict(entry.data)
        if speak_id and speak_id == data.speak_id:
            return entry
        if mac and mac == data.mac:
            return entry
    return None


def get_entities(hass, speak_id=None, mac=None):
    entry = get_config_entry(hass, speak_id, mac)
    if not entry:
        return []
    return er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)


def get_entities_ids(hass, speak_id=None, mac=None):
    return [entity.entity_id for entity in get_entities(hass, speak_id, mac)]


def EntryAuthFailedError(hass, entry):
    entry.async_start_reauth(hass)
    return ConfigEntryAuthFailed(
        translation_domain=DOMAIN,
        translation_key="huijian_auth_error",
        translation_placeholders={"name": entry.title},
    )


def generate_qr_code(data: str):
    """Generate a base64 PNG string represent QR Code image of data."""
    import pyqrcode  # noqa: PLC0415

    qr_code = pyqrcode.create(data)
    with io.BytesIO() as buffer:
        qr_code.svg(file=buffer, scale=4, module_color="#FFFFFF", background="#000000")
        return str(
            buffer.getvalue()
            .decode("ascii")
            .replace("\n", "")
            .replace(
                (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<svg xmlns="http://www.w3.org/2000/svg"'
                ),
                "<svg",
            )
        )
