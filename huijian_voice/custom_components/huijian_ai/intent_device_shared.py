import logging

_LOGGER = logging.getLogger(__name__)

WINDOW_KEYWORDS = [
    "窗户",
    "窗",
    "平推窗",
    "平开窗",
    "推拉窗",
    "内开窗",
    "外开窗",
    "天窗",
    "飘窗",
    "推拉门",
    "内开内倒窗",
    "单内倒窗",
    "外装平开窗",
    "智能窗",
]
WINDOW_EXCLUDE_KEYWORDS = ["窗帘"]
WINDOW_DOMAINS = {"window", "windows"}


def is_window_device(device: dict) -> bool:
    name = device.get("name", "") or ""
    domains = device.get("domains", [])
    if any(kw in name for kw in WINDOW_EXCLUDE_KEYWORDS):
        return False
    if any(kw in name for kw in WINDOW_KEYWORDS):
        return True
    if isinstance(domains, list) and any(d in WINDOW_DOMAINS for d in domains):
        return True
    return False


def split_actions_by_device(actions: list[dict]) -> list[dict]:
    if not actions:
        return actions

    split_actions = []
    for action in actions:
        intent_name = action.get("name") or action.get("intent", "")
        params = action.get("parameters") or action.get("params", {})
        targets = params.get("target", [])

        if not isinstance(targets, list):
            targets = [targets] if isinstance(targets, dict) else []
            params["target"] = targets

        normal_targets = []
        window_targets = []

        for target in targets:
            if not isinstance(target, dict):
                continue
            devices = target.get("devices", [])
            if not isinstance(devices, list):
                devices = [devices] if isinstance(devices, dict) else []
                target["devices"] = devices

            normal_devices = []
            window_devices = []

            for device in devices:
                if not isinstance(device, dict):
                    continue
                if is_window_device(device):
                    window_devices.append(device)
                else:
                    normal_devices.append(device)

            if normal_devices:
                normal_targets.append({**target, "devices": normal_devices})
            if window_devices:
                window_targets.append({**target, "devices": window_devices})

        if normal_targets:
            split_actions.append(
                {
                    "name": intent_name,
                    "parameters": {**params, "target": normal_targets},
                }
            )

        if window_targets:
            action_mapping = {
                "TurnDeviceOn": "ControlWindow",
                "TurnDeviceOff": "ControlWindow",
            }
            window_intent = action_mapping.get(intent_name, intent_name)
            window_action = "open" if intent_name == "TurnDeviceOn" else "close"
            split_actions.append(
                {
                    "name": window_intent,
                    "parameters": {"target": window_targets, "action": window_action},
                }
            )

    _LOGGER.info("Split actions: %s -> %s", len(actions), len(split_actions))
    return split_actions
