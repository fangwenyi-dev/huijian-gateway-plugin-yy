import logging

from homeassistant.components.button.const import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.input_button import DOMAIN as INPUT_BUTTON_DOMAIN

_LOGGER = logging.getLogger(__name__)

WINDOW_NAME_MAPPING = {
    "平推窗": "平推窗",
    "pingtui": "平推窗",
    "平开窗": "平开窗",
    "推拉窗": "推拉窗",
    "内开窗": "内开窗",
    "外开窗": "外开窗",
    "天窗": "天窗",
    "飘窗": "飘窗",
    "推拉门": "推拉门",
    "内开内倒窗": "内开内倒窗",
    "单内倒窗": "单内倒窗",
    "外装平开窗": "外装平开窗",
    "智能窗": "智能窗",
    "窗户": "窗户",
    "窗": "窗户",
}

WINDOW_ACTION_MAPPING = {
    "open": ["开启", "开", "open"],
    "close": ["关闭", "关", "close"],
    "pause": ["暂停", "停止", "pause", "stop"],
    "a": ["A", "a", "内倒", "内岛"],
}

REMOVE_KEYWORDS = ["删除", "remove", "shan_chu", "shanchu", "delete"]

# Pre-computed set of all window names (keys + values) for fast reuse
WINDOW_ALL_NAMES: set[str] = set(WINDOW_NAME_MAPPING.keys()) | set(WINDOW_NAME_MAPPING.values())
# Pre-sorted by length descending for keyword matching
WINDOW_ALL_NAMES_SORTED: list[str] = sorted(WINDOW_ALL_NAMES, key=len, reverse=True)


def normalize_text(text: str) -> str:
    return text.lower().strip() if text else ""


_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_NUM_PATTERN = None


def _parse_chinese_number(s: str) -> str:
    """Parse a Chinese number string to Arabic numeral string.

    Handles: 五→5, 二十三→23, 一百二十→120, 二百三十→230, 十一→11, 十→10
    """
    if not s:
        return ""
    result = 0
    current = 0
    for ch in s:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if current == 0:
                current = 1
            result += current * unit
            current = 0
    result += current
    return str(result)


def normalize_chinese_numbers(text: str) -> str:
    """Convert Chinese numerals in text to Arabic numerals.

    Examples:
        '五号测试窗' -> '5号测试窗'
        '二号窗户' -> '2号窗户'
        '十号' -> '10号'
        '二十三号' -> '23号'
        '一百二十号' -> '120号'
        '二百三十号' -> '230号'
    """
    if not text:
        return text
    global _CN_NUM_PATTERN
    if _CN_NUM_PATTERN is None:
        import re
        _CN_NUM_PATTERN = re.compile(r"[零一二三四五六七八九十百千]+")
    return _CN_NUM_PATTERN.sub(lambda m: _parse_chinese_number(m.group(0)), text)


def extract_window_name(name: str) -> str | None:
    if not name:
        return None
    name_lower = name.lower()
    # 通用名称（"所有窗户"、"全部窗"等）不匹配具体窗户类型，返回None触发全窗查找
    generic_names = ["所有窗户", "所有窗", "全部窗户", "全部窗", "每个窗户", "每扇窗户"]
    if any(gn in name_lower for gn in generic_names):
        _LOGGER.info("Detected generic window name '%s', will use fallback mode", name)
        return None
    # Check both keys AND values of WINDOW_NAME_MAPPING.
    # e.g. "2号测试窗" → value "窗户" not found, but key "窗" is found → returns "窗户"
    for key, value in WINDOW_NAME_MAPPING.items():
        if key.lower() in name_lower or value.lower() in name_lower:
            return value
    return None


def _find_standalone_keyword(name_lower: str, keyword_lower: str) -> int | None:
    """Find a keyword as a standalone word (not part of another word) in a string.

    Searches ALL occurrences of keyword and returns the first one that passes
    the boundary check (surrounded by spaces or string boundaries).
    This is needed because window names like '内开内倒窗' contain substrings
    like '内倒' and '开' that are also action keywords.
    """
    pos = 0
    while True:
        idx = name_lower.find(keyword_lower, pos)
        if idx == -1:
            return None
        after_idx = idx + len(keyword_lower)
        after_char = name_lower[after_idx] if after_idx < len(name_lower) else " "
        before_char = name_lower[idx - 1] if idx > 0 else " "
        if after_char.strip() == "" and before_char.strip() == "":
            return idx
        pos = idx + 1


def _strip_window_names(text_lower: str) -> str:
    """Remove known window names from text to avoid action keyword conflicts.

    e.g. '内开内倒窗' contains '开' (open keyword) and '内倒' (tilt keyword),
    which would interfere with action detection.
    Strips both keys and values from WINDOW_NAME_MAPPING so that shorthand
    variants like '窗' are also removed.
    """
    remaining = text_lower
    all_names = WINDOW_ALL_NAMES
    for wname in sorted(all_names, key=len, reverse=True):
        remaining = remaining.replace(wname.lower(), "")
    return remaining


def find_action_in_text(text: str) -> str | None:
    text_lower = text.lower()
    # Strip window names first to avoid conflicts:
    # e.g. "内开内倒窗" contains "开" (would match "open") and "内倒" (would match "a")
    cleaned = _strip_window_names(text_lower)
    remaining = cleaned.strip()
    if not remaining:
        # Text is entirely window names with no action keywords present
        return None
    for action, keywords in WINDOW_ACTION_MAPPING.items():
        for keyword in keywords:
            if keyword.lower() in remaining:
                return action
    return None


def is_remove_button(state) -> bool:
    entity_id = state.entity_id.lower()
    unique_id = getattr(state, "unique_id", "") or ""
    name = getattr(state, "name", "") or ""
    object_id = state.entity_id.split(".")[-1] if state.entity_id else ""
    for kw in REMOVE_KEYWORDS:
        if (
            kw.lower() in entity_id
            or kw.lower() in unique_id.lower()
            or kw.lower() in name.lower()
            or kw.lower() in object_id.lower()
        ):
            return True
    return False


def _build_alt_names(window_name_lower: str) -> set[str]:
    """Build alternative name set from window name and WINDOW_NAME_MAPPING."""
    alt_names = {window_name_lower}
    for key, value in WINDOW_NAME_MAPPING.items():
        if value.lower() == window_name_lower and key.lower() != window_name_lower:
            alt_names.add(key.lower())
    return alt_names


def _build_conflict_names(window_name_lower: str, alt_names: set[str]) -> set[str]:
    """Build set of longer window names that might cause substring conflicts."""
    conflicting: set[str] = set()
    for wname in set(WINDOW_NAME_MAPPING.values()):
        wname_lower = wname.lower()
        if len(wname) > len(window_name_lower):
            if any(alt in wname_lower for alt in alt_names):
                conflicting.add(wname_lower)
    for wkey in set(WINDOW_NAME_MAPPING.keys()):
        wkey_lower = wkey.lower()
        if wkey_lower != window_name_lower and len(wkey) > len(window_name_lower):
            if any(alt in wkey_lower for alt in alt_names):
                conflicting.add(wkey_lower)
    return conflicting


def _match_by_name_or_device(
    entity_id: str, name_lower: str, alt_names: set[str],
    entity_registry, device_registry,
) -> bool:
    """Check if button name or device name matches any alt name."""
    if any(alt in name_lower for alt in alt_names):
        return True
    entry_check = entity_registry.async_get(entity_id)
    if entry_check and entry_check.device_id:
        device_check = device_registry.async_get(entry_check.device_id)
        if device_check:
            device_display_lower = (
                device_check.name_by_user or device_check.name or ""
            ).lower()
            if any(alt in device_display_lower for alt in alt_names):
                return True
    return False


def _passes_exact_filter(
    entity_id: str, name_lower: str, original_name_lower: str | None,
    entity_registry, device_registry,
) -> bool:
    """Check if button passes the original name exact filter."""
    if original_name_lower is None:
        return True
    if original_name_lower in name_lower:
        return True
    entry = entity_registry.async_get(entity_id)
    if entry and entry.device_id:
        device = device_registry.async_get(entry.device_id)
        if device:
            device_display = (device.name_by_user or device.name or "").lower()
            if (
                original_name_lower in device_display
                or device_display in original_name_lower
            ):
                return True
    return False


def find_window_buttons(
    hass, window_name: str, area_name: str | None, original_name: str | None = None
) -> dict[str, str]:
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    target_area_id = None
    if area_name:
        from homeassistant.helpers import area_registry as ar

        area_registry = ar.async_get(hass)
        area = area_registry.async_get_area_by_name(area_name)
        if area:
            target_area_id = area.id

    result = {}
    _LOGGER.info(
        "Searching buttons: window_name='%s', area_name='%s', target_area_id='%s', original_name='%s'",
        window_name, area_name, target_area_id, original_name,
    )

    button_count = 0
    match_count = 0
    skip_area_count = 0
    skip_remove_count = 0

    window_name_lower = window_name.lower()
    alt_names = _build_alt_names(window_name_lower)
    conflicting_longer_names = _build_conflict_names(window_name_lower, alt_names)

    use_exact_filter = (
        original_name and original_name.strip().lower() != window_name_lower
    )
    original_name_lower = original_name.strip().lower() if use_exact_filter else None

    for state in hass.states.async_all():
        if state.domain not in (BUTTON_DOMAIN, INPUT_BUTTON_DOMAIN):
            continue

        button_count += 1
        name = getattr(state, "name", "") or ""
        entity_id = state.entity_id
        name_lower = name.lower()

        if not _match_by_name_or_device(
            entity_id, name_lower, alt_names, entity_registry, device_registry
        ):
            continue
        if any(ln.lower() in name_lower for ln in conflicting_longer_names):
            continue
        if not _passes_exact_filter(
            entity_id, name_lower, original_name_lower, entity_registry, device_registry
        ):
            continue

        match_count += 1

        if is_remove_button(state):
            skip_remove_count += 1
            continue

        entry = entity_registry.async_get(entity_id)

        if target_area_id and entry.area_id and entry.area_id != target_area_id:
            skip_area_count += 1
            continue
        if target_area_id and not entry.area_id:
            _LOGGER.debug("Including button without area_id: %s (%s)", entity_id, name)

        for action, keywords in WINDOW_ACTION_MAPPING.items():
            for keyword in keywords:
                keyword_lower = keyword.lower()
                if _find_standalone_keyword(name_lower, keyword_lower) is not None:
                    if action not in result:
                        result[action] = entity_id
                        _LOGGER.info(
                            "Found %s button: %s (name: %s)", action, entity_id, name
                        )
                    break

    _LOGGER.info(
        "Search summary: total_buttons=%s, name_matches=%s, skipped_remove=%s, skipped_area=%s, result=%s",
        button_count, match_count, skip_remove_count, skip_area_count, result,
    )
    return result


def find_window_buttons_by_area_id(hass, area_id: str | None) -> dict[str, str]:
    """Find all window buttons in a given area by area_id, keyed by action type."""
    from homeassistant.helpers import entity_registry as er

    entity_registry = er.async_get(hass)

    buttons = {}
    for state in hass.states.async_all():
        if state.domain not in (BUTTON_DOMAIN, INPUT_BUTTON_DOMAIN):
            continue
        entry = entity_registry.async_get(state.entity_id)
        if not entry:
            continue
        if area_id and entry.area_id and entry.area_id != area_id:
            continue
        if area_id and not entry.area_id:
            _LOGGER.debug(
                "find_window_buttons_by_area_id: including button without area_id: %s", state.entity_id
            )
        name = getattr(state, "name", "") or ""
        name_lower = name.lower()
        if is_remove_button(state):
            continue
        for action_key_kw, keywords in WINDOW_ACTION_MAPPING.items():
            for keyword in keywords:
                keyword_lower = keyword.lower()
                if _find_standalone_keyword(name_lower, keyword_lower) is not None:
                    if action_key_kw not in buttons:
                        buttons[action_key_kw] = state.entity_id
                    break
    return buttons


def find_all_window_buttons_by_action(
    hass, area_name: str | None, action: str
) -> list[str]:
    """Find ALL window buttons matching an action in the given area.

    Used when user says 'open all windows' without specifying a window type.
    Returns a list of entity_ids for all matching buttons.
    """
    from homeassistant.helpers import entity_registry as er

    entity_registry = er.async_get(hass)

    target_area_id = None
    if area_name:
        from homeassistant.helpers import area_registry as ar

        area_registry = ar.async_get(hass)
        area = area_registry.async_get_area_by_name(area_name)
        if area:
            target_area_id = area.id

    action_keywords = WINDOW_ACTION_MAPPING.get(action, [])
    if not action_keywords:
        return []

    result = []
    seen_window_types = set()

    for state in hass.states.async_all():
        if state.domain not in (BUTTON_DOMAIN, INPUT_BUTTON_DOMAIN):
            continue
        name = getattr(state, "name", "") or ""
        name_lower = name.lower()
        if is_remove_button(state):
            continue
        entry = entity_registry.async_get(state.entity_id)
        if not entry:
            continue
        if target_area_id and entry.area_id and entry.area_id != target_area_id:
            continue
        if target_area_id and not entry.area_id:
            _LOGGER.debug(
                "Including button without area_id: %s (%s)", state.entity_id, name
            )

        # Auto-derive window keywords from WINDOW_NAME_MAPPING
        # so they stay in sync when new window types are added
        window_keywords = WINDOW_ALL_NAMES_SORTED
        has_window_keyword = any(kw.lower() in name_lower for kw in window_keywords)
        if not has_window_keyword:
            continue

        for keyword in action_keywords:
            keyword_lower = keyword.lower()
            match_idx = _find_standalone_keyword(name_lower, keyword_lower)
            if match_idx is not None:
                window_type = name_lower[:match_idx].strip()
                if window_type not in seen_window_types:
                    seen_window_types.add(window_type)
                    result.append(state.entity_id)
                    _LOGGER.info(
                        "Found all-window button: %s (name: %s)", state.entity_id, name
                    )
                break

    return result
