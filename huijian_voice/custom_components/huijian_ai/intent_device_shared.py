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
    # 「内倒窗」现场简称（成员判定无扫描序问题，与上两词并存即可）。
    "内倒窗",
    "外装平开窗",
    "智能窗",
    # 2026-09 悬窗族/提升窗（与加内 _WINDOW_TYPES/KNOWN_DEVICES_PREFIX、
    # intent_window_const.WINDOW_NAME_MAPPING 三方同步，守卫钉）。
    "下悬窗",
    "上悬窗",
    "提升窗",
    "悬窗",
    # 2026-09-30 数据集对账补口（三方同步守卫钉）。窗帘字面已在排除表——
    # 「电动窗帘」被排除表先拦，不会撞本键。
    "电动窗",
]
# 机器人：擦窗机器人/清洁机器人属家电（vacuum 族），名称带"窗"却绝不是
# 按压窗控设备——进排除表，防 is_window_device 子串误判。
# v1.1.27：帘族闸与 intent_window_const.extract_window_name 顶部短路口径对齐
# （"帘"/"纱窗"/"百叶"）——「百叶窗」「纱窗」是 cover 语义，旧表只挡"窗帘"
# ⇒「关纱窗/关百叶窗」被当窗控按本区全部窗钮（关一扇变关一排）。
WINDOW_EXCLUDE_KEYWORDS = ["帘", "纱窗", "百叶", "机器人"]
WINDOW_DOMAINS = {"window", "windows"}

# Turn* 族 → 窗控意图的**唯一**改写表（v1.1.27：非本表命中的意图一律原样透传）
_TURN_TO_WINDOW_INTENT = {
    "TurnDeviceOn": "ControlWindow",
    "TurnDeviceOff": "ControlWindow",
}


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
            # v1.1.27：**只有 Turn\* 族**做「意图 → ControlWindow + open/close」
            # 改写；其余意图（ControlWindow 自身、PauseDevice 等）name、action 与
            # 全部参数原样透传。旧版三元把任何非 TurnDeviceOn 的意图都定死
            # action="close"、params 重建为 {target, action} ⇒ 内倒(a)/暂停(pause)/
            # position/speed/strength 全丢（场景与自动化动作入库即坏，回放时关错
            # 方向、开度丢失）。
            mapped_intent = _TURN_TO_WINDOW_INTENT.get(intent_name)
            if mapped_intent:
                out_params = {
                    **params,
                    "target": window_targets,
                    "action": "open" if intent_name == "TurnDeviceOn" else "close",
                }
                out_name = mapped_intent
            else:
                out_params = {**params, "target": window_targets}
                out_name = intent_name
            split_actions.append({"name": out_name, "parameters": out_params})

    _LOGGER.info("Split actions: %s -> %s", len(actions), len(split_actions))
    return split_actions
