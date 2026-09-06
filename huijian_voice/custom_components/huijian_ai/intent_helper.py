import logging
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

import voluptuous as vol
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent

_LOGGER = logging.getLogger(__name__)

DOMAIN_ALIASES: dict[str, str | list[str]] = {
    "window": ["cover", "button"],
    "windows": ["cover", "button"],
    "curtain": "cover",
    "curtains": "cover",
    "blind": "cover",
    "blinds": "cover",
    "shutter": "cover",
    "shutters": "cover",
    "plug": "switch",
    "plugs": "switch",
    "outlet": "switch",
    "outlets": "switch",
    "fan": "fan",
    "fans": "fan",
    "ac": "climate",
    "air_conditioner": "climate",
    "heater": "climate",
    "lamp": "light",
    "lamps": "light",
    "door": ["lock", "cover", "button"],
    "doors": ["lock", "cover", "button"],
    "tv": "media_player",
    "speaker": "media_player",
}


def normalize_targets_device_names(targets: list[dict]) -> list[dict]:
    """归一化 targets 中所有设备名称里的中文数字。

    Converts Chinese numerals (一二三...十) in device names to Arabic numerals.
    Improves entity matching when ASR recognizes "五号" but HA entity is "5号".

    Args:
        targets: List of target dicts with 'devices' containing 'name' fields.

    Returns:
        New list of targets with normalized device names.
    """
    from .intent_window_const import normalize_chinese_numbers

    result = []
    for target in targets:
        target = dict(target)
        devices = target.get("devices", [])
        if devices:
            new_devices = []
            for d in devices:
                d = dict(d)
                if d.get("name"):
                    d["name"] = normalize_chinese_numbers(d["name"])
                new_devices.append(d)
            target["devices"] = new_devices
        result.append(target)
    return result


def _expand_domains(domains: list[str]) -> list[str]:
    expanded = list(domains)
    for d in domains:
        alias = DOMAIN_ALIASES.get(d)
        if alias:
            aliases = alias if isinstance(alias, list) else [alias]
            for a in aliases:
                if a not in expanded:
                    expanded.append(a)
    return expanded


def target_parameter_type():
    return vol.All(
        cv.ensure_list,
        [
            vol.Schema(
                {
                    vol.Optional("devices"): vol.All(
                        cv.ensure_list,
                        [
                            vol.Schema(
                                {
                                    vol.Required("domains"): vol.All(
                                        cv.ensure_list, [cv.string]
                                    ),
                                    vol.Optional("name"): cv.string,
                                }
                            )
                        ],
                    ),
                    vol.Optional("area"): cv.string,
                }
            )
        ],
    )


def get_entity_name(entity_entry: er.RegistryEntry, state: State) -> str:
    if len(entity_entry.aliases) > 0:
        alias = list(entity_entry.aliases)[0]
        name = str(alias) if alias is not None else state.name
        if "ComputedNameType" not in name:
            return name

    if isinstance(entity_entry.name, str):
        name = entity_entry.name if entity_entry.name else state.name
        if "ComputedNameType" not in name:
            return name

    friendly = state.attributes.get("friendly_name", "")
    if friendly and "ComputedNameType" not in friendly:
        return friendly
    if "ComputedNameType" not in state.name:
        return state.name
    return state.entity_id


@dataclass
class AreaInfo:
    name: str
    id: str


def get_entity_area(
    hass: HomeAssistant, entity_entry: er.RegistryEntry
) -> AreaInfo | None:
    area_names = []
    area_registry = ar.async_get(hass)
    device_registry = dr.async_get(hass)
    if entity_entry.area_id and (
        area := area_registry.async_get_area(entity_entry.area_id)
    ):
        area_names.extend(area.aliases)
        area_names.append(area.name)
        return AreaInfo(id=entity_entry.area_id, name=area_names[0])
    elif entity_entry.device_id and (
        device := device_registry.async_get(entity_entry.device_id)
    ):
        if device.area_id and (area := area_registry.async_get_area(device.area_id)):
            area_names.extend(area.aliases)
            area_names.append(area.name)
            return AreaInfo(id=device.area_id, name=area_names[0])


@dataclass
class EntityInfo:
    name: str
    area: AreaInfo | None
    state: State
    entity: er.RegistryEntry
    on_off: Literal["on", "off"]

    @property
    def area_name(self) -> str:
        if self.area:
            return self.area.name
        return ""

    @property
    def area_id(self) -> str:
        if self.area:
            return self.area.id
        return ""


class HaDeviceItem(TypedDict):
    domains: list[str]
    name: str | None


class HaTargetItem(TypedDict):
    area: str | None
    devices: list[HaDeviceItem]


@dataclass
class StateWithAreaConstraint:
    states: list[State]
    unset_area_constraint: bool


async def _match_with_constraints(
    hass: HomeAssistant,
    targets: list[HaTargetItem],
    assistant: str | None,
) -> tuple[list[StateWithAreaConstraint], set[str]]:
    """Match targets with given assistant constraint. Returns states + expanded domains."""
    found_states: list[StateWithAreaConstraint] = []
    all_expanded_domains: set[str] = set()

    for target in targets:
        for device in target["devices"]:
            area_name = target.get("area")
            expanded_domains = _expand_domains(device["domains"])
            all_expanded_domains.update(expanded_domains)
            match_constraints = intent.MatchTargetsConstraints(
                name=device.get("name"),
                area_name=area_name,
                domains=expanded_domains,
                assistant=assistant,
                single_target=False,
                allow_duplicate_names=True,
            )
            _LOGGER.info("Match constraints (assistant=%s): %s", assistant, match_constraints)
            match_result = intent.async_match_targets(hass, match_constraints)
            if not match_result.is_match:
                continue
            found_states.append(
                StateWithAreaConstraint(
                    states=match_result.states,
                    unset_area_constraint=(area_name == ""),
                )
            )

    return found_states, all_expanded_domains


def _build_candidate_entities(
    hass: HomeAssistant,
    found_states: list[StateWithAreaConstraint],
    entity_registry: er.Registry,
) -> list[EntityInfo]:
    """Build candidate EntityInfo list from matched states."""
    candidate_entities: list[EntityInfo] = []

    for item in found_states:
        for state in item.states:
            if state.state == "unavailable":
                continue
            entity_entry = entity_registry.async_get(state.entity_id)
            if not entity_entry:
                continue
            entity_area = get_entity_area(hass, entity_entry)
            if item.unset_area_constraint and entity_area:
                continue
            entity_name = get_entity_name(entity_entry, state)
            candidate_entities.append(
                EntityInfo(
                    name=entity_name,
                    area=entity_area,
                    state=state,
                    entity=entity_entry,
                    on_off="off" if state.state == "off" else "on",
                )
            )

    return candidate_entities


async def match_intent_entities(
    intent_obj: intent.Intent, targets: list[HaTargetItem]
) -> tuple[dict | None, list[EntityInfo] | None]:
    """Match entities by request parameters."""
    hass = intent_obj.hass
    entity_registry = er.async_get(hass)

    # 第一轮：严格匹配（带 assistant）
    found_states, domains1 = await _match_with_constraints(
        hass, targets, intent_obj.assistant
    )
    all_expanded_domains = domains1.copy()
    candidate_entities = _build_candidate_entities(hass, found_states, entity_registry)

    # 第二轮：无 assistant 回退
    if len(candidate_entities) == 0:
        _LOGGER.warning(
            "Strict match failed (assistant=%s), trying fallback without assistant filter",
            intent_obj.assistant,
        )
        found_states2, domains2 = await _match_with_constraints(
            hass, targets, None
        )
        all_expanded_domains.update(domains2)
        candidate_entities = _build_candidate_entities(hass, found_states2, entity_registry)

    # ── 精确名称优先过滤 ──
    # 如果 LLM 指定了 name，优先匹配完全相同的实体名
    # 这样可以避免 HA 子串匹配导致的过匹配问题
    requested_name = None
    for target in targets:
        for device in target["devices"]:
            name = device.get("name")
            if name:
                requested_name = name
                break
        if requested_name:
            break

    if requested_name:
        name_lower = requested_name.lower().strip()
        exact_matches = [e for e in candidate_entities if e.name.lower() == name_lower]
        if exact_matches:
            _LOGGER.info(
                "Exact name match found: %d entities (filtered from %d)",
                len(exact_matches),
                len(candidate_entities),
            )
            candidate_entities = exact_matches
        else:
            # 无精确匹配时，尝试前缀匹配（如 name='窗户' 匹配 '2号测试窗户' 等）
            prefix_matches = [
                e for e in candidate_entities if e.name.lower().startswith(name_lower)
            ]
            if prefix_matches:
                _LOGGER.info(
                    "Prefix name match found: %d entities (filtered from %d)",
                    len(prefix_matches),
                    len(candidate_entities),
                )
                candidate_entities = prefix_matches
            else:
                # ── 设备名匹配 ──
                # 当实体名不匹配请求名时（如 has_entity_name=True 的网关按钮，
                # 实体名 "开启" vs 设备名 "开窗器 01"），通过设备注册表查找
                dev_reg = dr.async_get(hass)
                device_matches = []
                for e in candidate_entities:
                    entity_entry = e.entity
                    if not entity_entry.device_id:
                        continue
                    device = dev_reg.async_get(entity_entry.device_id)
                    if not device:
                        continue
                    device_name = device.name_by_user or device.name or ""
                    if (
                        name_lower in device_name.lower()
                        or device_name.lower() in name_lower
                    ):
                        device_matches.append(e)
                if device_matches:
                    _LOGGER.info(
                        "Device name match found: %d entities (filtered from %d via device name)",
                        len(device_matches),
                        len(candidate_entities),
                    )
                    candidate_entities = device_matches
                else:
                    # ── Entity ID 子串匹配 ──（最终兜底）
                    # 当设备名也匹配不上时（如实体无 device_id），
                    # 尝试用请求名匹配 entity_id（如 entity_id 含设备标识）
                    entity_id_matches = [
                        e
                        for e in candidate_entities
                        if name_lower in e.entity.entity_id.lower()
                    ]
                    if entity_id_matches:
                        _LOGGER.info(
                            "Entity ID match found: %d entities (filtered from %d via entity_id)",
                            len(entity_id_matches),
                            len(candidate_entities),
                        )
                        candidate_entities = entity_id_matches

    # ── 设备注册表级兜底（第5级） ──
    # 前4级匹配全部失败（如按钮实体名"开窗器 开启"完全不包含
    # 请求的设备名"2号测试窗"），HA 的 async_match_targets 按
    # 实体名匹配完全找不到结果。此时通过设备注册表按设备名查找。
    if len(candidate_entities) == 0 and requested_name:
        _LOGGER.info(
            "Device registry fallback: name='%s', expanded_domains=%s",
            requested_name,
            all_expanded_domains,
        )
        dev_reg = dr.async_get(hass)
        ent_reg = er.async_get(hass)

        name_lower = requested_name.lower().strip()
        matched_device_ids: set[str] = set()
        for device_entry in dev_reg.devices.values():
            device_display = (
                device_entry.name_by_user or device_entry.name or ""
            ).lower()
            if device_display and (
                name_lower in device_display or device_display in name_lower
            ):
                matched_device_ids.add(device_entry.id)

        if matched_device_ids:
            for entity_entry in list(ent_reg.entities.values()):
                if entity_entry.device_id not in matched_device_ids:
                    continue
                if (
                    all_expanded_domains
                    and entity_entry.domain not in all_expanded_domains
                ):
                    continue
                state = hass.states.get(entity_entry.entity_id)
                if not state or state.state == "unavailable":
                    continue
                entity_area = get_entity_area(hass, entity_entry)
                entity_name = get_entity_name(entity_entry, state)
                on_off = "off" if state.state == "off" else "on"
                entity_info = EntityInfo(
                    name=entity_name,
                    area=entity_area,
                    state=state,
                    entity=entity_entry,
                    on_off=on_off,
                )
                _LOGGER.info("Device registry fallback entity: %s", entity_info)
                candidate_entities.append(entity_info)

    if len(candidate_entities) == 0:
        return {"success": False, "error": "No available devices found"}, None

    return None, candidate_entities
