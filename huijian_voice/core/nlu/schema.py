"""意图与属性契约单点（v1.1.3 融合第一步）。

背景（同一病灶打了三次）：`AdjustDeviceAttribute` 的合法属性名在**三处**各写
一份——① 集成端 `register_adjustment` 装饰器（真正的裁决者）、② 加载项 LLM 工具
枚举 `agent.TOOLS`、③ 出站改名表 `executor._ATTR_WIRE`。三处手抄必然漂移：
v1.1.1 就抓到 ② 里躺着注册表根本不存在的 `colour_temperature`（LLM 照表发即
unsupported），而 ③ 的存在本身就是因为 ①② 名字不统一。

本模块把契约收在**一处**，并与 HA 原生意图目录对齐命名/槽位形状（`HassTurnOn`
那套 name/area/floor/domain/device_class 的槽位形状），自研的中文解析层不变：
借契约、不换引擎。`tests/test_v1113_capability_vocab.py` 里的跨仓守卫会在集成
注册表增删属性而本表未跟时当场红。
"""
from __future__ import annotations

# 集成端 register_adjustment 实际注册的 (domain, attribute) 合法属性名全集。
# 顺序即播报/枚举展示序（与集成错误信息里的期望序一致）。
REGISTRY_ATTRIBUTES: tuple[str, ...] = (
    "position", "temperature", "fan_speed", "color", "humidity", "value",
    "brightness", "volume",
)

# **可下达**子集=注册过且真能成功的。volume 虽然在集成里注册了（所以 slot
# 校验会放过它），但处理器是显式 `raise unsupported`（adjust.py:595-602），
# 让 LLM 发它就是白跑一趟；能力门也会拒（core/capability.gate）。工具枚举因此
# 用本表，不用 REGISTRY_ATTRIBUTES。
ADDRESSABLE_ATTRIBUTES: tuple[str, ...] = tuple(
    a for a in REGISTRY_ATTRIBUTES if a != "volume")

# 网关内部属性名 → 注册表出站名。内部名不许改（`temperature` 在网关侧是空调℃
# 口径：_T0_ATTR_WORD / H1 改道 / 播报三分支全按 color_temperature 挑「色温」，
# 就地改名=串台），所以只在**上 wire 前**做这一层映射。
ATTR_TO_WIRE: dict[str, str] = {
    "color_temperature": "temperature",
    "colour_temperature": "temperature",
}

# 属性 → 播报中文（executor 话术层单点用，过去也是散在两个 dict 里）。
ATTR_CN = {
    "brightness": "亮度", "color": "颜色", "color_temperature": "色温",
    "colour_temperature": "色温", "temperature": "温度", "fan_speed": "风量",
    "position": "开合度", "humidity": "湿度", "value": "数值", "volume": "音量",
}

# 集成 calc_target 明说 unsupported 的特殊值（真机核过：min/max/low/high 可用，
# medium/auto 一律 raise）——网关侧永不产出，能力门也照此拒。
NEVER_SPECIALS: frozenset[str] = frozenset({"medium", "auto"})


def wire_attribute(attribute: str) -> str:
    """内部属性名 → 注册表出站名（未登记的名称原样透传）。"""
    return ATTR_TO_WIRE.get(attribute, attribute)
