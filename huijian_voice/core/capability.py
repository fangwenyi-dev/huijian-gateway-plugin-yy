"""设备能力矩阵（v1.1.3 P0-2 第一步：只读裁决，不改写计划）。

存在理由（2026-09-21 真机对账实锤）：网关把"能不能做"判在三处手抄表里
（fast_path 的 `_ATTR_DOMAIN`/`_T0_ATTR_WORD`、集成 `register_adjustment`、
HA 实体自己的 supported_features/模式列表），中间没有共享契约，于是出现两类
只在看得到真机的地方才暴露的故障：

  · 「把空调风速调大」→ 我们发 fan_speed=high，而小米空调的 fan_modes 是
    ['level1'…'level7']，集成回 `unsupported the mode`；用户听到"这句话我还不会"
    级别的失败，却不知道自己的设备其实能调、只是档位名不同。
  · 灯+position（「客厅灯开到50」修前形态）→ 同样只能等集成侧报错。

本模块**只做拒绝、绝不做改写**：拿目标实体的真实 attributes
（supported_features / supported_color_modes / fan_modes / hvac_modes /
current_position / min-max…）判这一条命令在**这个家**里到底能不能成立；
不能就当场给出带真实选项的中文答复（把用户教回来），而不是白跑一趟 HA。

判据一律取"属性在场性"而非服务名猜测，且**信息不足即放行**（宁让集成再判一次，
绝不在网关侧凭空拒掉一个本来能做的动作）。
"""
from __future__ import annotations

from typing import Any, Optional

# HA 域能力位（legacy supported_features，与 core 各域 const 同值）
_LIGHT_BRIGHTNESS = 1
_LIGHT_COLOR_TEMP = 2
_LIGHT_COLOR = 8           # 含 rgb/hs/xy（现代 HA 由 supported_color_modes 表达）
_COVER_SET_POSITION = 4
_FAN_SET_SPEED = 1

# HA 里"能设色/能显色"的颜色模式（color 槽合法集）
_COLOR_CAPABLE_MODES = {"rgb", "rgbw", "rgbww", "hs", "hsv", "xy"}
_CT_MODES = {"color_temp", "ct", "rgbw", "rgbww"}

# 只读域：这些实体没有"开关"可言（语音 Turn* 必须拒；查询族仍可读）
READ_ONLY_DOMAINS = frozenset({
    "sensor", "binary_sensor", "weather", "update", "person", "image",
    "calendar", "zha", "system_log", "provisioning", "stt", "tts", "notify",
    "conversation", "scene_config", "sun", "zone", "geo_location", "backup",
    "hassio", "config", "diagnostics", "analytics",
})

# 特殊档位禁忌表来自契约单点（core.nlu.schema），不在这里二次手抄。
from .nlu.schema import NEVER_SPECIALS as _NEVER_SPECIALS


def resolve_candidates(states: dict, entity_area: dict, target: list) -> list:
    """把计划里的 target 形（area + devices[{name,domains}]）映射到本家实体快照。

    executor 的能力预裁与 pipeline 的歧义确认共用这一份匹配近似——两处各写一遍
    就是下一个漂移源。语义刻意保守：name 用**子串**（与集成端 6 级匹配同向），
    命中不到就返回空表，由调用方按"信息不足=放行"处理。永不抛。
    """
    out: list = []
    try:
        for slot in (target or []):
            if not isinstance(slot, dict):
                continue
            area = str(slot.get("area") or "")
            for dev in (slot.get("devices") or [{}]):
                nm = str((dev or {}).get("name") or "").strip()
                doms = tuple((dev or {}).get("domains") or ())
                for eid, e in (states or {}).items():
                    dom = str(eid).split(".", 1)[0]
                    if doms and dom not in doms:
                        continue
                    if not isinstance(e, dict):
                        continue
                    fn = str(((e.get("attributes") or {}).get("friendly_name")) or "")
                    if area and (entity_area or {}).get(eid) != area and area not in fn:
                        continue
                    if nm and nm not in str(eid) and nm not in fn:
                        continue
                    e = dict(e)
                    e.setdefault("entity_id", eid)
                    if e not in out:
                        out.append(e)
    except Exception:  # noqa: BLE001 解析故障=空表（调用方按信息不足放行）
        return []
    return out


def _feat(ent: dict) -> int:
    try:
        return int(((ent or {}).get("attributes") or {}).get("supported_features") or 0)
    except (TypeError, ValueError):
        return 0


def _attrs(ent: dict) -> dict[str, Any]:
    a = (ent or {}).get("attributes")
    return a if isinstance(a, dict) else {}


def _names(ents: list[dict]) -> str:
    out = []
    for e in ents[:3]:
        nm = _attrs(e).get("friendly_name") or e.get("entity_id") or ""
        if nm:
            out.append(str(nm))
    return "、".join(out)


def _opt_text(opts: list) -> str:
    vals = [str(o) for o in opts if o not in (None, "")][:8]
    return "、".join(vals) if vals else "（该设备未上报可选档位）"


def supports_attribute(ent: dict, attribute: str) -> bool:
    """单个实体是否具备该属性槽的能力。

    **只在有"否证"时判不支持**：supported_features / supported_color_modes /
    fan_modes / current_position 等元数据在场且明确不含该能力，才返回 False；
    元数据缺失（老集成、未上报、测试替身给的空 attributes）一律放行。只读裁决
    的边界就在这条上——错拒一个本来能做的动作，比漏放一个做不成的动作严重得多
    （后者只是多一次 HA 报错，前者是用户彻底失去这条口令）。
    """
    a = _attrs(ent)
    dom = str(ent.get("entity_id", "")).split(".", 1)[0]
    has_feat = a.get("supported_features") is not None
    modes = {str(m).lower() for m in (a.get("supported_color_modes") or [])}
    if attribute in ("color_temperature", "colour_temperature") or (
            attribute == "temperature" and dom == "light"):
        if not (has_feat or modes):
            return True
        return bool(_feat(ent) & _LIGHT_COLOR_TEMP) or bool(modes & _CT_MODES) \
            or a.get("color_temp") is not None or a.get("color_temp_kelvin") is not None
    if attribute == "brightness" and dom == "light":
        if not (has_feat or modes):
            return True
        return bool(_feat(ent) & _LIGHT_BRIGHTNESS) or a.get("brightness") is not None \
            or bool(modes - {"off", "onoff", "unknown"})
    if attribute == "color" and dom == "light":
        if not (has_feat or modes):
            return True
        return bool(_feat(ent) & _LIGHT_COLOR) or bool(modes & _COLOR_CAPABLE_MODES) \
            or a.get("rgb_color") is not None or a.get("hs_color") is not None
    if attribute == "position" and dom == "cover":
        if not has_feat and "current_position" not in a:
            return True
        return bool(_feat(ent) & _COVER_SET_POSITION) or a.get("current_position") is not None
    if attribute == "fan_speed":
        if dom == "fan":
            if not has_feat and "percentage" not in a and "percentage_step" not in a:
                return True
            return bool(_feat(ent) & _FAN_SET_SPEED) or a.get("percentage") is not None \
                or a.get("percentage_step") is not None
        if dom == "climate":
            if "fan_modes" not in a:
                return True         # 未上报=未知，不是"不能调"
            return bool(a.get("fan_modes"))
        return True
    if attribute == "temperature" and dom == "climate":
        if "hvac_modes" not in a and "target_temperature" not in a:
            return True
        return bool(a.get("hvac_modes")) or a.get("target_temperature") is not None
    if attribute == "humidity" and dom == "humidifier":
        if "humidity" not in a and "min_humidity" not in a and not has_feat:
            return True
        return a.get("humidity") is not None or a.get("min_humidity") is not None
    if attribute == "value" and dom == "number":
        if "value" not in a and "min" not in a:
            return True
        return a.get("value") is not None or (a.get("min") is not None
                                              and a.get("max") is not None)
    if dom == "media_player" and attribute in ("volume", "brightness"):
        # 集成侧对 media_player 的 volume/brightness 是**显式 raise unsupported**
        # （adjust.py:595-602），网关不再把注定失败的命令发上去；音量改走 HA 原生
        # media_player.volume_set 是后话（本批不开新能力）。
        return False
    return True


def _fan_mode_options(ents: list[dict]) -> list:
    for e in ents:
        modes = _attrs(e).get("fan_modes")
        if modes:
            return list(modes)
    return []


def gate(intent: str, args: dict, candidates: list[dict]) -> Optional[str]:
    """只读预裁。返回 None=放行；返回中文串=当场如实失败（不外发）。

    candidates = 该计划目标在本家解析到的实体快照（可空——空=信息不足，一律放行，
    设备存在性由集成端判，这里不越权）。
    """
    ents = [e for e in (candidates or []) if isinstance(e, dict)]
    if not ents:
        return None

    if intent in ("TurnDeviceOn", "TurnDeviceOff"):
        doms = {str(e.get("entity_id", "")).split(".", 1)[0] for e in ents}
        if doms and doms <= READ_ONLY_DOMAINS:
            return (f"{_names(ents)}是传感器/状态类实体，没有开关可以按。"
                    f"想问它现在的读数可以说「{_ask_hint(ents)}」。")
        return None

    if intent == "PauseDevice":
        ok = any(str(e.get("entity_id", "")).split(".", 1)[0] in
                 ("vacuum", "media_player", "cover", "fan", "timer") for e in ents)
        if not ok:
            return f"{_names(ents)}不支持暂停，只支持开和关。"
        return None

    if intent != "AdjustDeviceAttribute":
        return None

    attribute = str((args or {}).get("attribute") or "")
    delta = str((args or {}).get("delta") or "").strip().lower()
    supported = [e for e in ents if supports_attribute(e, attribute)]
    if not supported:
        why = _attr_fail_text(attribute, ents)
        return why or None

    # 档位特殊值必须落在该实体真实可选列表里（真机实锤：high/low 对
    # level1~7 的空调=直接 unsupported the mode）
    if attribute == "fan_speed" and delta in ("high", "low", "max", "min",
                                              "medium", "auto"):
        opts = _fan_mode_options(supported)
        doms = {str(e.get("entity_id", "")).split(".", 1)[0] for e in supported}
        if opts and doms == {"climate"}:
            low = {str(o).lower() for o in opts}
            if delta in _NEVER_SPECIALS or delta not in low:
                return (f"这台空调的风速档是 {_opt_text(opts)}，没有「{delta}」这一档。"
                        f"可以说具体档位，比如「风速调成{opts[0]}」。")
    elif delta in _NEVER_SPECIALS and attribute != "fan_speed":
        return f"这台设备没有「{delta}」档，说个具体数值更稳（比如 50%）。"
    return None


def _attr_fail_text(attribute: str, ents: list[dict]) -> Optional[str]:
    doms = {str(e.get("entity_id", "")).split(".", 1)[0] for e in ents}
    who = _names(ents)
    cn = {"brightness": "亮度", "color": "颜色", "color_temperature": "色温",
          "colour_temperature": "色温", "position": "开合度", "fan_speed": "风速",
          "temperature": "温度", "humidity": "湿度", "value": "数值",
          "volume": "音量"}.get(attribute)
    if cn is None:
        return None
    if attribute == "volume":
        return (f"{who}的音量现在还不能靠语音调（这条通道在设备端没开）。"
                f"可以在 Home Assistant 里给它做一个音量实体或用自动化控制。")
    if attribute == "color" and "light" in doms:
        modes = set()
        for e in ents:
            modes |= {str(m).lower() for m in (_attrs(e).get("supported_color_modes") or [])}
        return (f"{who}不支持调色，它支持的灯光是 {_opt_text(sorted(modes)) or '只有开关/色温'}。"
                f"想换冷暖可以说色温。")
    if attribute == "position" and "cover" in doms:
        return f"{who}这台设备没有开合度读数，只能整开整关。"
    return f"{who}不支持{cn}调节。"


def _ask_hint(ents: list[dict]) -> str:
    for e in ents:
        a = _attrs(e)
        dc = str(a.get("device_class") or "")
        if dc in ("temperature", "humidity", "illuminance", "power", "battery"):
            nm = a.get("friendly_name") or e.get("entity_id", "")
            return f"{nm}多少"
        if str(e.get("entity_id", "")).startswith("binary_sensor."):
            return f"{a.get('friendly_name') or e.get('entity_id')}是不是开着"
    return "它现在是什么状态"
