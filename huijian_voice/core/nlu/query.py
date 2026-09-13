"""查询族（v4.1 定案 M1：本地读回，不走 LLM）。

fast_path 的复杂查询守卫把 "客厅多少度" 放行（原代码即为温度查询预留），
此处接管：解析区域+量纲 → ha_client 状态缓存 → 中文短句。
未命中返回 None 继续向 LLM/兜底层流动。支持：温度/湿度/照度读数、开关状态、
时间问答（GetDateTime 本地时钟 + HA 时区）。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger("huijian.query")

_AREA_SUFFIX = ("室", "厅", "房", "间", "区", "馆", "楼", "卫", "厨")

# v1.0.49（Q2）：本地可答量纲信号词——fast_path 的复杂守卫（多少|几 → 上层）
# 之前先过这盏灯，否则「办公室温度多少」「平开窗电池电量多少」这类最普通的
# 读数问句全部在入口被"交上层"截走，无 LLM 用户只剩兜底话术（现场主诉）。
# 只放"读数据"专有维度词，不放泛疑问词——宁可少放行不可劫持控制/创作句。
_LOCAL_DIM_RE = re.compile(
    r"(温度|湿度|照度|亮度|电量|电池|有人|没人|有没有人|是否有人|人在不在|空不空|多少度|几度)")


def looks_local_query(text: str) -> bool:
    """命中本地量纲词 → True（守卫放行给查询族，未命中自然回原链）。"""
    return bool(_LOCAL_DIM_RE.search(text or ""))
_DEVICE_WORDS = {
    "灯": ("light",), "筒灯": ("light",), "射灯": ("light",), "灯带": ("light",),
    "吸顶灯": ("light",), "台灯": ("light",),
    "空调": ("climate",), "风扇": ("fan",), "窗": ("cover",), "窗帘": ("cover",),
    "加湿器": ("humidifier",), "净化器": ("fan",),
}


class QueryZone:
    def __init__(self, ha, settings):
        self.ha = ha
        self.settings = settings
        self._tz = None

    async def timezone(self):
        if self._tz is None:
            try:
                cfg = await self.ha.get_config()
                self._tz = cfg.get("time_zone") or None
            except Exception:
                self._tz = None
        return self._tz

    # ── 主入口：命中返回中文答案，否则 None ─────────────────────
    async def answer(self, text: str) -> Optional[str]:
        if self.settings is not None and not self.settings.get("nlu.query_local", True):
            return None
        text = text.strip().rstrip("。？！?!，,")
        # 时间
        if re.search(r"(现在)?(几点了?|什么时间|几点钟|时间)", text) and not re.search(r"定时|预约", text):
            return await self._time_answer()
        area = self._find_area(text)
        # 体验批 P2-14①：设备属性读数（"空调设定温度多少/灯现在多亮"）——
        # 先于传感器规则：设备词+属性词是明确指向设备本身，不是房间传感器。
        m = re.search(r"(空调|灯|风扇|加湿器|净化器|除湿机|热水器|冰箱)[的]?.*?"
                      r"(设定温度|目标温度|当前温度|温度|亮度|色温|湿度|风量|风速|档位)", text)
        if m and re.search(r"(多少|几|怎样|怎么样|如何|现在|是)", text):
            ans = await self._attr_answer(area, m.group(1), m.group(2))
            if ans:
                return ans
        # 体验批 P2-14②：状态聚合计数（"有多少灯开着/几个设备没关"）
        if re.search(r"(多少|几个|几盏|几台|几只|哪些)[^吗]{0,6}(开着|亮着|没关|运行|工作|开着没)", text) \
                or re.search(r"(开着|亮着|没关)的[^？?]{0,4}(有哪些|几个|多少)", text):
            ans = await self._count_answer(area, text)
            if ans:
                return ans
        # 人感/有无人在场（v1.0.49 Q3：现场「办公室现在是否有人」——人感传感器
        # 用 occupancy/presence/motion 判定，多颗任一在位即"有人"）
        if re.search(r"(有人|没人|人在|空不空|有没有人|是否有人)", text):
            ans = await self._presence_answer(area)
            if ans:
                return ans
        # 设备电池电量（v1.0.49 Q4：「办公室平开窗电池电量多少」——device_class
        # =battery 传感器，设备提示词按实体名匹配，读数直报）
        if re.search(r"(电池|电量)", text) and re.search(
                r"(多少|剩|还有|低|高|满|怎样|如何|状态|不足|正常|查询|查|看看)", text):
            ans = await self._battery_answer(area, text)
            if ans:
                return ans
        # 温度/湿度/照度
        m = re.search(r"(温度|湿度|照度|亮度)", text)
        if m and (re.search(r"(多少|几|怎样|怎么样|如何)", text)
                  or re.search(r"(查询|查一查|查一下|查下|查查|看看|看下|报一下|告诉我)", text)):
            kind = {"温度": "temperature", "湿度": "humidity", "照度": "illuminance", "亮度": "illuminance"}[m.group(1)]
            return await self._sensor_answer(area, kind, m.group(1))
        # 「多少度/几度」裸形（fast_path 守卫专门放行给本层，必须接住）
        if re.search(r"(多少度|几度)", text):
            return await self._sensor_answer(area, "temperature", "温度")
        # 设备开关状态："客厅灯开着吗/窗帘关到位没"
        m = re.search(r"(.{0,6}?(?:灯|空调|风扇|窗帘|窗|加湿器|净化器))\s*(开着吗|开着没|是开的吗|关了没|关着吗|是关的吗|什么状态|现在怎样)", text)
        if m:
            word = re.sub(r"^(.*?(?=灯|空调|风扇|窗帘|窗|加湿器|净化器))", "", m.group(1)) or m.group(1)[-2:]
            return await self._state_answer(area, word.strip() or None)
        return None

    # v1.0.42（Q1）：区域名前面的时间/礼貌/查询引导词。旧版正则 {2,4}?+后缀 从
    # 句首起窗，「现在办公室的温度多少」提出「现在办公室」——查无此区域，整条
    # 温度查询落兜底（真机日志 2026-09-11）。先剥前缀再抽区域。
    _AREA_PREFIX_NOISE = re.compile(
        r"^(?:现在|此刻|目前|眼下|今天|今晚|昨天|昨晚|刚才|刚刚|此时|请问|麻烦|"
        r"帮我看看|帮我查(?:一下|下)?|查一下|查看一下|查下|查看|看一下|看下|查询|查一查|报一下|"
        r"告诉我|我想知道|想问下|问一下)+[的]?")

    def _find_area(self, text: str) -> Optional[str]:
        """区域词提取：剥时间/引导前缀 → 优先注册区域名（ha._areas）→ 后缀启发。"""
        t = self._AREA_PREFIX_NOISE.sub("", text) or text
        names = sorted(set(self.ha._areas.values()), key=len, reverse=True) if self.ha._areas else []
        for n in names:
            if n and (n in t or n in text):
                return n
        # {1,4}?：「客厅/书房」这类两字区域（1字+后缀）也要能抽出（旧 {2,4}? 最少三字，
        # 两字区域只在注册表命中时可用——registry 拿不到时静默丢区域）
        m = re.search(r"([\u4e00-\u9fff]{1,4}?(?:" + "|".join(_AREA_SUFFIX) + r"))", t)
        return m.group(1) if m else None

    async def _time_answer(self) -> Optional[str]:
        """GetDateTime → 本地时钟（HA 时区经 /api/config 缓存解析）。"""
        try:
            from zoneinfo import ZoneInfo
            tz = await self.timezone()
            now = datetime.now(ZoneInfo(tz)) if tz else datetime.now()
        except Exception:
            now = datetime.now()
        return f"现在是 {now.hour} 点 {now.minute} 分。"

    _ATTR_KEYS = {   # 体验批 P2-14①：设备词 × 属性词 → attribute 取值链（多键尝试，HA 域差异）
        ("空调", "设定温度"): ("temperature",), ("空调", "目标温度"): ("temperature",),
        ("空调", "当前温度"): ("current_temperature",), ("空调", "温度"): ("temperature", "current_temperature"),
        ("灯", "亮度"): ("brightness",), ("灯", "色温"): ("color_temp", "color_temperature"),
        ("风扇", "风量"): ("percentage",), ("风扇", "风速"): ("percentage",), ("风扇", "档位"): ("percentage",),
        # P3-b（2026-09-22 审查批）：原 ("净化器","湿度")→("aqi",) 会把 AQI 数值
        # 冠以「湿度是 35」报给用户——量纲错标签即假成功。只认设备真实发布的
        # humidity 属性；没有就返回 None 让位下方通用湿度传感器分支（宁缺勿错）。
        ("加湿器", "湿度"): ("humidity",), ("加湿器", "档位"): ("fan_speed",),
        ("净化器", "湿度"): ("humidity",), ("净化器", "档位"): ("fan_speed",),
        ("除湿机", "湿度"): ("humidity",), ("热水器", "温度"): ("temperature", "current_operation"),
        ("冰箱", "温度"): ("temperature",),
    }

    async def _attr_answer(self, area, dev_word: str, attr_word: str) -> Optional[str]:
        domains = _DEVICE_WORDS.get(dev_word, ())
        if not domains:
            return None        # 词表外设备词不猜域（find_entities 空 domains=全量，必错）
        ents = await self.ha.find_entities(area=area or "", domains=domains)
        if not ents:
            return None
        keys = self._ATTR_KEYS.get((dev_word, attr_word)) or ()
        ent = next((e for e in ents
                    if any((e.get("attributes") or {}).get(k) is not None for k in keys)),
                   None)
        if ent is None:
            return None
        attrs = ent.get("attributes") or {}
        val = next((attrs[k] for k in keys if attrs.get(k) is not None), None)
        try:
            v = float(val)
        except (TypeError, ValueError):
            return None
        nm = attrs.get("friendly_name") or ""
        prefix = f"{area}的" if area else ""
        if attr_word in ("亮度",):
            pct = int(round(v * 100 / 255)) if 0 <= v <= 255 else int(round(v))
            return f"{prefix}{nm or dev_word}亮度约 {pct}%。"
        if attr_word == "色温":
            return f"{prefix}{nm or dev_word}色温 {int(v)}K。"
        if attr_word in ("风量", "风速", "档位"):
            pct = int(round(v)) if v <= 100 else int(round(v * 100 / 255))
            return f"{prefix}{nm or dev_word}风量约 {pct}%。"
        return f"{prefix}{nm or dev_word}{'设定' if '定' in attr_word else ''}{attr_word}是 {v:g}。"

    async def _count_answer(self, area, text: str) -> Optional[str]:
        """体验批 P2-14②：开着/没关 设备计数与点名（≤3 具名，多则只报数）。"""
        m = re.search(r"(灯|空调|风扇|加湿器|净化器|窗帘|所有)?[^吗]{0,4}"
                      r"(?:开着|亮着|没关|运行|工作)", text)
        word = (m.group(1) if m else "") or ""
        domains = _DEVICE_WORDS.get(word, ())
        if word in ("所有", "") and not domains:
            domains = ("light", "climate", "fan", "cover", "humidifier", "switch")
        ents = await self.ha.find_entities(area=area or "", domains=domains or ())
        on_words = {"on", "open", "opening", "heating", "cooling", "auto", "fan_only",
                    "dry", "heat_cool", "eco", "playing", "paused", "heat", "preheat"}
        on_ents = [e for e in ents if str(e.get("state", "")) in on_words]
        prefix = f"{area}的" if area else ""
        noun = {"灯": "盏灯", "空调": "台空调", "风扇": "台风扇", "窗帘": "幅窗帘",
                "加湿器": "台加湿器", "净化器": "台净化器"}.get(word, "个设备")
        if not domains or word == "所有" or word == "":
            noun = "个设备"
        if not on_ents:
            return f"{prefix}{noun}都关着呢。"
        names = [((e.get("attributes") or {}).get("friendly_name") or e.get("entity_id", ""))
                 for e in on_ents]
        if len(on_ents) <= 3:
            return f"{prefix}开着{len(on_ents)}{noun}：" + "、".join(names) + "。"
        return f"{prefix}开着{len(on_ents)}{noun}，比如{'、'.join(names[:2])}。"

    _PRESENCE_ON = {"on", "detected", "true", "home", "occupied"}
    _PRESENCE_OFF = {"off", "not_detected", "false", "clear", "cleared",
                     "not_home", "idle", "unoccupied"}
    _PRESENCE_DCLASSES = ("occupancy", "presence", "motion")

    async def _presence_answer(self, area: Optional[str]) -> Optional[str]:
        """有人吗：区域 occupancy/presence/motion 传感器聚合。命中链：区域绑定
        → 名称含区域词（注册表缺失降级，同 _sensor_answer Q2 纪律）→ 全屋唯一。
        任一在位=有人；拿不到可判定的传感器返回 None 让位上层。"""
        states = await self.ha.states()
        have_area_data = bool(getattr(self.ha, "_areas", None)
                              or getattr(self.ha, "_entity_area", None))
        cands = []
        for eid, ent in states.items():
            domain = eid.split(".")[0]
            if domain not in ("sensor", "binary_sensor"):
                continue
            attrs = ent.get("attributes") or {}
            if attrs.get("device_class") not in self._PRESENCE_DCLASSES:
                continue
            name = attrs.get("friendly_name") or ""
            ent_area = self.ha._entity_area.get(eid, "") if hasattr(self.ha, "_entity_area") else ""
            if area and have_area_data and ent_area != area and area not in name:
                continue
            if area and not have_area_data and area not in name:
                continue
            cands.append(str(ent.get("state", "")).lower())
        if not cands:
            return None
        occ = any(st in self._PRESENCE_ON for st in cands)
        unknown = all(st not in self._PRESENCE_ON and st not in self._PRESENCE_OFF
                      for st in cands)
        if unknown:
            return None
        prefix = f"{area}现在" if area else "家里现在"
        return f"{prefix}{'有人' if occ else '没人'}。"

    async def _battery_answer(self, area: Optional[str], text: str) -> Optional[str]:
        """电池电量：device_class=battery 传感器。从问句剥骨架词得设备提示词
        （「办公室平开窗电池电量多少」→「平开窗」），按实体名/区域匹配。"""
        dev = text or ""
        for w in ("电池电量", "剩余电量", "电池", "电量", "还剩多少", "还剩", "剩下",
                  "还有多少", "还有", "是多少", "多少", "现在", "目前", "请问", "帮我",
                  "我想知道", "查询", "查一查", "查一下", "查下", "查查", "看看", "告诉",
                  "报一下", "状态", "有没有", "不足", "正常", "吗", "呢", "的", "了",
                  "？", "?", "。", "现在", "如何", "怎样", "是"):
            dev = dev.replace(w, "")
        if area:
            dev = dev.replace(area, "")
        dev = dev.strip()[:8]
        states = await self.ha.states()
        cands = []
        for eid, ent in states.items():
            if not eid.startswith("sensor."):
                continue
            attrs = ent.get("attributes") or {}
            if attrs.get("device_class") != "battery":
                continue
            name = attrs.get("friendly_name") or ""
            ent_area = self.ha._entity_area.get(eid, "") if hasattr(self.ha, "_entity_area") else ""
            try:
                val = float(ent.get("state"))
            except (TypeError, ValueError):
                continue
            if dev:
                if dev in name:
                    cands.append((name, val, 0))       # 名称含设备词：最强命中
                elif not name and area and ent_area == area:
                    cands.append((name, val, 2))
            elif area and (ent_area == area or area in name):
                cands.append((name, val, 0))
        if not cands:
            return None
        cands.sort(key=lambda c: c[2])
        name, val, _ = cands[0]
        prefix = f"{area}的" if area else ""
        label = name or (dev or "设备")
        if len(cands) > 1 and cands[1][2] == cands[0][2]:
            return f"{prefix}{label}电量还剩 {val:g}%。同区还有 {len(cands) - 1} 台设备有电池读数，想查哪台请说设备名。"
        return f"{prefix}{label}电量还剩 {val:g}%。"

    async def _sensor_answer(self, area: Optional[str], device_class: str, cn: str) -> Optional[str]:
        states = await self.ha.states()   # 此调用顺带触发 registry 懒同步（refresh_states 内）
        # v1.0.42（Q2）：区域注册表整体拿不到时（老HA端点404/权限缺失/token无
        # config读），不再"按区域过滤→全被滤光→返回None"，降级为全量找同量纲
        # 传感器唯一命中；多颗则宁缺勿滥不猜。
        have_area_data = bool(getattr(self.ha, "_areas", None)
                              or getattr(self.ha, "_entity_area", None))
        candidates: list[tuple[str, float]] = []
        for eid, ent in states.items():
            attrs = ent.get("attributes") or {}
            if attrs.get("device_class") != device_class:
                continue
            name = (attrs.get("friendly_name") or "")
            # v1.0.52：与 :223/:261 同规防护——真 HAClient 恒有该属性，但注入面
            # 缺失时裸取会 AttributeError 被级联折叠成"查询族异常"整条静默降级。
            ent_area = (self.ha._entity_area.get(eid, "")
                        if hasattr(self.ha, "_entity_area") else "")
            if area and have_area_data and ent_area != area and area not in name:
                continue
            try:
                val = float(ent.get("state"))
            except (TypeError, ValueError):
                continue
            candidates.append((name, val))
            if area and have_area_data and ent_area == area:
                break
        if not candidates:
            return None
        if len(candidates) > 1 and not have_area_data:
            # v1.0.44 名称兜底：区域注册表拿不到时，实体名含所说区域词且唯一
            # 命中 → 照答（现场「办公室温度传感器多少」，传感器名往往就叫
            # 「办公室温度」——命名规范的现场不该被"未同步房间"拒掉）。
            if area:
                named = [c for c in candidates if area in (c[0] or "")]
                if len(named) == 1:
                    candidates = named
                else:
                    # 无区域信息且多颗同类传感器：答哪颗都是猜。诚实引导。
                    return f"家里有多个{cn}传感器，但还没同步到房间信息，请给传感器所在区域绑定设备后重试。"
            else:
                return f"家里有多个{cn}传感器，但还没同步到房间信息，请给传感器所在区域绑定设备后重试。"
        name, val = candidates[0]
        unit = {"temperature": "度", "humidity": "%", "illuminance": "勒克斯"}[device_class]
        val_s = f"{val:.1f}".rstrip("0").rstrip(".")   # 26.5→「26.5」，26.0→「26」
        prefix = f"{area}的" if area else ""
        return f"{prefix}{cn}是 {val_s} {unit.replace('勒克斯','lx')}。"

    async def _state_answer(self, area: Optional[str], device_word: Optional[str]) -> Optional[str]:
        domains = _DEVICE_WORDS.get(device_word or "", ())
        ents = await self.ha.find_entities(area=area or "", domains=domains or ())
        ents = [e for e in ents if e["entity_id"].split(".")[0] in (domains or ("light", "climate", "fan", "cover", "humidifier", "switch"))]
        if not ents:
            return None
        on_words = ("on", "open", "heating", "cooling", "auto", "fan_only", "dry", "heat_cool", "eco")
        states_cn = {"on": "开着", "off": "关着", "open": "开着", "closed": "关着", "opening": "正在开", "closing": "正在关",
                     "heat": "制热中", "cool": "制冷中", "dry": "除湿中", "fan_only": "送风中", "auto": "自动模式", "idle": "待机"}
        lines = []
        for e in ents[:3]:
            st = str(e.get("state", ""))
            cn = states_cn.get(st, "开着" if st in on_words else f"处于 {st}")
            nm = (e.get("attributes") or {}).get("friendly_name") or e["entity_id"]
            lines.append(f"{nm}{cn}")
        return "，".join(lines) + "。" if lines else None
