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
# v1.1.29 复核 A7：8 是 LightEntityFeature.FLASH（本仓 light.py:373 无条件置它），
# legacy「可调色」位是 16（SUPPORT_COLOR，含 rgb/hs/xy）——取 8 ⇒ 本仓自有灯族全部
# 绕过调色预裁（播「颜色已设为绿」而灯只是变冷白），且误拒 legacy 彩色灯。
_LIGHT_COLOR = 16
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

# 不可开关域（语音开关族预检的唯一来源，executor._turn_gate 引用）。
# v1.1.27 收口（逐行审计实锤）：executor 侧同名表与 READ_ONLY_DOMAINS **不一致**
# （executor 多 number/select/text/button/datetime…，capability 多 weather/person/
# calendar…）⇒ `_turn_gate` 的域预检形同只做了半张表：另半张域的实体照样被喂
# turn_on/off（HA core 语义：这些域没有 turn_on 动作＝ServiceNotSupported 风暴 +
# 集成回顶层 success 时还会谎报"开了"）。语义取**并集**（更严＝多拦，宁如实失败）。
UNTOGGLEABLE_DOMAINS = frozenset(READ_ONLY_DOMAINS | {
    "number", "select", "text", "button", "datetime", "date", "time", "event",
})

# 特殊档位禁忌表来自契约单点（core.nlu.schema），不在这里二次手抄。
from .nlu.schema import NEVER_SPECIALS as _NEVER_SPECIALS


def _domain_slots(raw) -> tuple:
    """domains 槽归一（v1.1.27）。LLM/上游常把**单域**塞成字符串："light"——
    旧写法 `tuple(raw)` 会炸成 ('l','i','g','h','t')，于是 `dom not in doms` 恒真、
    候选恒空、能力预裁静默失效（比拦错更糟：整条能力面无声关闭）。
    str → (str,)（空串＝未指定＝不过滤，语义保持）；列表/元组/集合 → 逐项取字符串；
    其余脏值 → 空元组（＝不过滤）。永不抛。"""
    if isinstance(raw, str):
        return (raw.strip(),) if raw.strip() else ()
    if isinstance(raw, (list, tuple, set, frozenset)):
        return tuple(str(x).strip() for x in raw if str(x).strip())
    return ()


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
                doms = _domain_slots((dev or {}).get("domains"))
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
    # 可用性优先（2026-09-27 办公实锤 D4）：同名实体里离线孪生不得排在可用台之前。
    # 稳定排序，只把 unavailable 沉底——'unknown'（未首 poll 的瞬态）不算离线，与
    # executor._availability_refuse 同口径，否则会把刚重启的实体误判成离线。
    out.sort(key=lambda e: str((e or {}).get("state")) == "unavailable")
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


# ── 区域可解析性预检（v1.1.24）──────────────────────────────────
# 办公实锤：连写句「打开办公室的射灯办公室的空调」被解析成 area='办公室的射灯办公室'
# 的畸形目标——创建入库后触发必半失败；即时执行则白跑一趟集成。
# 判据只认"注册表里确实没有"这一种确定态：注册表未同步（_areas 空）⇒ 一律放行，
# 与模块头同一纪律（宁漏放不误拒）。
def _slot_resolvable(slot: dict, states: dict, entity_area: dict) -> bool:
    """该槽能否在本家**自己**落到实体（畸形区域闸的逐槽放行证据，v1.1.28）。

    只有"该槽自身能落到具体实体"才允许放行整句：`resolve_candidates`
    先按 area 过滤、再按 name/domains 过滤 ⇒ 区域名不在注册表里时，除非该字符串
    恰好是某台设备 friendly_name 的一部分，否则恒空 ⇒ **未知区域不得跨区抓设备**
    这条原始语义（本闸的存在理由）逐槽依然成立。
    快照空/fail ⇒ False（无证据＝不许放行，退回"只认注册表"的旧口径）。"""
    try:
        if not states or not isinstance(slot, dict):
            return False
        return bool(resolve_candidates(states, entity_area or {}, [slot]))
    except Exception:      # noqa: BLE001 判定故障=当作解析不出（不许放行）
        return False


def bad_area_slots(targets, reg_areas, states=None, entity_area=None) -> list:
    """逐槽筛出**不合格**的目标槽：区域不在注册表、且本槽解析不出实体。

    reg_areas 空（注册表未同步）⇒ 恒 []：判不了即放行，与模块头同一纪律。
    无区域槽（area 为空）不在本闸管辖内（那是"整区/全屋"或纯按名的槽）。
    返回的是**原槽 dict**（可被调用方就地剪除），永不抛。"""
    out: list = []
    try:
        if not reg_areas:
            return out
        for t in targets or []:
            if not isinstance(t, dict):
                continue
            a = str(t.get("area") or "").strip()
            if not a:
                continue                              # 无区域槽：不在本闸管辖
            if a in reg_areas:
                continue                              # 区域在册：合格
            if _slot_resolvable(t, states or {}, entity_area or {}):
                continue                              # 该槽自身能落地：逐槽放行
            out.append(t)
    except Exception:      # noqa: BLE001 判不了=不合格槽为空（放行）
        return []
    return out


def bad_target_area(targets, reg_areas, states=None, entity_area=None) -> Optional[str]:
    """target 槽列表里**所有**带区域的槽都不合格时，返回第一个问题区域名。

    v1.1.24 旧口径是"任一槽区域不在注册表 ⇒ 整句拦下"；v1.1.28 改为**逐槽判定**：
    双语桥会把一个英文句拆成两个 Target 槽（"turn on the office light" ⇒
    [{area:'office'…}, {area:'办公室'…}]），第二个槽已正确解析到实体，整句却被
    第一个槽的英文区域名连坐拒掉（v1.1.25 的英文能力在真机上等于未兑现）。新口径
    只在**所有带区域的槽**都不合格时才拦（"只要还有能解析到设备的槽" ⇒ 不拦整句），
    其余不合格槽由调用方就地剪除
    （`bad_area_slots`），**绝不带着未知区域下发**——本闸"防跨区误抓"的原意不丢。

    reg_areas 空 ⇒ None（判不了，放行）；states 空 ⇒ 无实体证据 ⇒ 只认注册表。"""
    try:
        if not reg_areas:
            return None
        area_slots = [t for t in (targets or [])
                      if isinstance(t, dict) and str(t.get("area") or "").strip()]
        if not area_slots:
            return None
        bads = bad_area_slots(targets, reg_areas, states, entity_area)
        if len(bads) != len(area_slots):
            return None            # 还有合格的槽 ⇒ 不拦整句（不合格槽另行剪除）
        return str((bads[0] or {}).get("area") or "").strip() or None
    except Exception:  # noqa: BLE001 判不了=放行
        return None


async def registry_areas(ha) -> set:
    """HA 区域注册表名集合（未同步=空集）。区域预检的共用取数口：
    先 await 一次 states（真客户端会顺带按 TTL 拉注册表），再读 `_areas`。永不抛。"""
    try:
        if ha is None:
            return set()
        await ha.states()
        areas = getattr(ha, "_areas", {}) or {}
        return {str(v).strip() for v in areas.values() if str(v).strip()}
    except Exception:  # noqa: BLE001 判不了=空集（调用方按 fail-open 处理）
        return set()
