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
# 查询族设备类别表（v1.1.2 统一：状态/属性/计数三支共用）。
# 原表 12 词且三支各写一份正则（属性支漏 射灯/平开窗、计数支漏 窗/幕布…），
# v1.1.0 那批具名设备词进了**设备**表却没进**查询**表，「射灯亮度多少」
# 「哪些窗开着」类整族落空或答成全域。长词优先，最左命中即定域（「电动窗帘」
# 不得被 电动窗/窗 截胡成窗族）。
_DEVICE_WORDS = {
    "灯": ("light",), "筒灯": ("light",), "射灯": ("light",), "灯带": ("light",),
    "吸顶灯": ("light",), "台灯": ("light",), "吊灯": ("light",), "主灯": ("light",),
    "阅读灯": ("light",), "镜前灯": ("light",), "感应灯": ("light",), "夜灯": ("light",),
    "落地灯": ("light",), "床头灯": ("light",),
    "空调": ("climate",), "中央空调": ("climate",), "挂机空调": ("climate",),
    "柜机空调": ("climate",),
    "风扇": ("fan",), "落地扇": ("fan",), "电风扇": ("fan",), "循环扇": ("fan",),
    "排气扇": ("fan",), "换气扇": ("fan",),
    "窗": ("cover",), "窗帘": ("cover",), "电动窗帘": ("cover",), "卷帘": ("cover",),
    "百叶帘": ("cover",), "百叶窗": ("cover",), "纱窗": ("cover",), "开窗器": ("cover",),
    "平开窗": ("cover",), "推拉窗": ("cover",), "内开窗": ("cover",), "悬窗": ("cover",),
    "电动窗": ("cover",), "幕布": ("cover",), "投影幕布": ("cover",),
    "加湿器": ("humidifier",), "净化器": ("fan",), "空气净化器": ("fan",),
    "除湿机": ("humidifier",), "热水器": ("water_heater",), "新风机": ("fan",),
    "插座": ("switch",), "开关": ("switch",), "门": ("lock", "cover"),
    "锁": ("lock",), "门锁": ("lock",), "扫地机器人": ("vacuum",),
}
# 最长优先扫描表（等长按表内声明序，与 targets 侧同纪律）
_CLASS_WORDS = tuple(sorted(_DEVICE_WORDS, key=len, reverse=True))

# 播报量词（计数/状态句里"3 __"的空）。表外类别回退"个设备"。
_CLASS_NOUN = {"灯": "盏灯", "筒灯": "盏灯", "射灯": "盏灯", "灯带": "条灯带",
               "吸顶灯": "盏灯", "台灯": "盏灯", "吊灯": "盏灯", "主灯": "盏灯",
               "阅读灯": "盏灯", "镜前灯": "盏灯", "感应灯": "盏灯", "夜灯": "盏灯",
               "落地灯": "盏灯", "床头灯": "盏灯",
               "空调": "台空调", "中央空调": "台空调", "挂机空调": "台空调",
               "柜机空调": "台空调", "风扇": "台风扇", "落地扇": "台风扇",
               "电风扇": "台风扇", "循环扇": "台风扇", "排气扇": "台风扇",
               "换气扇": "台风扇", "新风机": "台新风机",
               "窗": "扇窗", "平开窗": "扇窗", "推拉窗": "扇窗", "内开窗": "扇窗",
               "悬窗": "扇窗", "电动窗": "扇窗", "纱窗": "扇纱窗", "百叶窗": "扇百叶",
               "窗帘": "幅窗帘", "电动窗帘": "幅窗帘", "卷帘": "幅卷帘",
               "百叶帘": "幅百叶帘", "开窗器": "扇窗", "幕布": "幅幕布",
               "投影幕布": "幅幕布", "加湿器": "台加湿器", "除湿机": "台除湿机",
               "净化器": "台净化器", "空气净化器": "台净化器", "热水器": "台热水器",
               "插座": "个插座", "开关": "个开关", "门": "扇门", "锁": "把锁",
               "门锁": "把锁", "扫地机器人": "台扫地机器人"}

_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# 属性取值链的域代表词回落：_ATTR_KEYS 按"人最常问的那一型"写死（灯/空调/
# 风扇…），而类别表现在 30+ 词——「射灯亮度多少」查 (射灯,亮度) 落空并不
# 意味着该域没有亮度属性，必须回落到同域代表词的取值链。
_DOMAIN_CANON = {}
for _w, _d in _DEVICE_WORDS.items():
    _DOMAIN_CANON.setdefault(_d, _w)

# 状态疑问判据（v1.1.2 安全闸，fast_path 同调此表——一处判据两档共守）。
# 「状态词 + 语气尾」收尾＝问句；礼貌请求尾（好吗/可以吗/行吗）已在
# normalize_polite/_ECHO_TONE 剥除，剥后仍留 吗/没 的就是真问句。
STATE_QUESTION_TAIL = re.compile(
    r"(?:开着|开了|开着了|关着|关了|关上|关闭|关好|灭着|灭了|亮着|拉着|拉上|拉下|拉开"
    r"|停着|停了|停止|运行|在运行|插着|锁着|上锁|反锁|开着门)"
    r"(?:的)?(?:呢|了)?\s*(?:吗|么|没有|没|[?？])\s*$")
STATE_QUESTION_ALT = re.compile(
    r"(?:是开着还是关着|是关着还是开着|开着还是关着|关了没有|是不是开着|是不是关着"
    r"|是不是(?:还)?开|是不是(?:还)?关|是否开着|是否关着|有没有开|有没有关"
    r"|(?:现在|目前)?什么状态|状态怎么样|状态如何|现在怎样|怎么样了吗"
    r"|查询.{0,10}状态|查一下.{0,10}状态|查.{0,10}状态)")


def is_state_question(text: str) -> bool:
    """状态疑问句判据：命令档必须让路（实测「射灯关了吗」曾被 ^关了 接成
    TurnDeviceOff，问一句关一次设备），交查询族作答。永不抛。"""
    try:
        t = (text or "").strip().rstrip("。！!，,、")
        return bool(STATE_QUESTION_TAIL.search(t) or STATE_QUESTION_ALT.search(t))
    except Exception:  # noqa: BLE001 判据故障=不误拦命令（保守放行原链）
        return False


def class_of(text: str):
    """句中最长设备类别词 → (word, domains)；无类别词 (None, ())。"""
    t = text or ""
    for w in _CLASS_WORDS:
        if w in t:
            return w, _DEVICE_WORDS[w]
    return None, ()



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
        # 时间（v1.1.2 扩：星期几/几号/什么时候——原表只认「几点/时间」，
        # 「今天星期几」在 golden 表里长期钉着 miss=已知未做，本批补齐）
        if re.search(r"(现在)?(几点了?|什么时间|几点钟|什么时候|时间)", text) \
                and not re.search(r"定时|预约", text):
            return await self._time_answer()
        if re.search(r"(星期几|礼拜几|周几)", text):
            return await self._weekday_answer()
        if re.search(r"(几号|几月几号|日期是|号是)", text) and not re.search(r"定时|预约|几天", text):
            return await self._date_answer()
        area = self._find_area(text)
        # 体验批 P2-14①：设备属性读数（"空调设定温度多少/灯现在多亮"）——
        # 先于传感器规则：设备词+属性词是明确指向设备本身，不是房间传感器。
        # v1.0.62 golden 实锤：「多亮」是"亮度"的口语变体，P2-14 注释承诺了
        # 该句式但正则只认「亮度」二字——实现与承诺不符，此处补齐。
        # v1.1.2：设备词改走统一类别表（原硬列 8 词，射灯/平开窗/卷帘/幕布
        # 一律不认——v1.1.0 具名设备词只进了设备表没进查询表）；问法标记补
        # 「多大|多高|多亮|几档」（「空调风量多大」原形整句落空）。
        dev_word, _doms = class_of(text)
        m = re.search(r"(设定温度|目标温度|当前温度|温度|亮度|多亮|色温|湿度|风量|风速|档位)",
                      text)
        if dev_word and m and re.search(
                r"(多少|几|怎样|怎么样|如何|现在|是|多大|多高)", text):
            attr_word = "亮度" if m.group(1) == "多亮" else m.group(1)
            ans = await self._attr_answer(area, dev_word, attr_word)
            if ans:
                return ans
        # 体验批 P2-14②：状态聚合计数（"有多少灯开着/几个设备没关"）
        if re.search(r"(多少|几个|几盏|几台|几只|哪些)[^吗]{0,6}(开着|亮着|没关|运行|工作|开着没)", text) \
                or re.search(r"(开着|亮着|没关)的[^？?]{0,4}(有哪些|几个|多少)", text) \
                or re.search(r"(哪些|哪个|哪有)[^？?]{0,4}(开着|亮着|没关)", text):
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
        # 设备开关状态（v1.1.2 重写：原表只有 8 个固定尾巴，实测 15 种口语问法
        # 里 9 种落空——「现在是开着的吗/是开着还是关着/还亮着吗/关了没有/查询X
        # 状态」全不在表内，落空后就被命令档字面表接走变成"问一句动一次设备"。
        # 判据与命令档共用 is_state_question 同一张表（一处定义两档共守）。）
        if is_state_question(text):
            ans = await self._state_answer(area, dev_word)
            if ans:
                return ans
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

    async def _weekday_answer(self) -> Optional[str]:
        return f"今天是{_WEEKDAYS[self._now(await self.timezone()).weekday()]}。"

    async def _date_answer(self) -> Optional[str]:
        now = self._now(await self.timezone())
        return f"今天是 {now.year} 年 {now.month} 月 {now.day} 号。"

    @staticmethod
    def _now(tz=None):
        """HA 时区优先的本地时钟（时区拿不到就退本机钟——报数比不报强）。"""
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz)) if tz else datetime.now()
        except Exception:  # noqa: BLE001 坏时区名不配让整条日期查询哑掉
            return datetime.now()

    _ATTR_KEYS = {   # 体验批 P2-14①：设备词 × 属性词 → attribute 取值链（多键尝试，HA 域差异）
        ("空调", "设定温度"): ("temperature",), ("空调", "目标温度"): ("temperature",),
        ("空调", "当前温度"): ("current_temperature",), ("空调", "温度"): ("temperature", "current_temperature"),
        ("灯", "亮度"): ("brightness",),
        ("灯", "色温"): ("color_temp_kelvin", "color_temp", "color_temperature"),
        ("风扇", "风量"): ("percentage",), ("风扇", "风速"): ("percentage",), ("风扇", "档位"): ("percentage",),
        # P3-b（2026-09-22 审查批）：原 ("净化器","湿度")→("aqi",) 会把 AQI 数值
        # 冠以「湿度是 35」报给用户——量纲错标签即假成功。只认设备真实发布的
        # humidity 属性；没有就返回 None 让位下方通用湿度传感器分支（宁缺勿错）。
        ("加湿器", "湿度"): ("humidity",), ("加湿器", "档位"): ("fan_speed",),
        ("净化器", "湿度"): ("humidity",), ("净化器", "档位"): ("fan_speed",),
        ("除湿机", "湿度"): ("humidity",), ("热水器", "温度"): ("temperature", "current_operation"),
        ("冰箱", "温度"): ("temperature",),
    }

    # v1.0.62 P1-6：手抄键表之上的**量纲闸**——实体带 unit_of_measurement 且与
    # 属性词预期量纲冲突时整键不认（把 AQI/光照 lux 当"湿度"播报这类错标签的
    # 根治：aqi 单位 "°AQI"/"AQI" ∉ % 白名单 → 拒报让位通用传感器分支，宁缺勿错）。
    # 单位缺省=信任键表（大量集成不发布 unit，一票否决会砍掉可用读数）。
    _UNIT_WHITELIST = {
        "湿度": ("%", "rh", "%r.h.", "percent"),
        "温度": ("°c", "°f", "celsius", "fahrenheit"),
        "风量": ("%", "percent"), "风速": ("%", "percent"), "档位": ("%", "percent"),
    }

    @classmethod
    def _unit_ok(cls, attr_word: str, attrs: dict) -> bool:
        allow = next((v for k, v in cls._UNIT_WHITELIST.items() if k in attr_word), None)
        if allow is None:
            return True
        u = str(attrs.get("unit_of_measurement") or "").strip().lower()
        return (not u) or u in allow

    async def _attr_answer(self, area, dev_word: str, attr_word: str) -> Optional[str]:
        domains = _DEVICE_WORDS.get(dev_word, ())
        if not domains:
            return None        # 词表外设备词不猜域（find_entities 空 domains=全量，必错）
        ents = await self.ha.find_entities(area=area or "", domains=domains)
        if not ents:
            return None
        keys = self._ATTR_KEYS.get((dev_word, attr_word)) or ()
        if not keys:
            canon = _DOMAIN_CANON.get(domains)
            keys = self._ATTR_KEYS.get((canon, attr_word)) if canon else ()
        if not keys:
            return None
        ent = next((e for e in ents
                    if self._unit_ok(attr_word, e.get("attributes") or {})
                    and any((e.get("attributes") or {}).get(k) is not None for k in keys)),
                   None)
        if ent is None:
            # v1.1.2：设备在、属性读不到——绝大多数就是"它没开着"（HA 的
            # brightness/color_temp 只在 on 时上报）。旧实现在这里返回 None，
            # 用户问「射灯亮度多少」听到的是"这句话我还不会"，等于把一句
            # 完全可以如实回答的话推给了兜底。
            silent = [e for e in ents
                      if str(e.get("state", "")) in ("off", "closed", "idle", "standby")]
            if silent:
                nm = (silent[0].get("attributes") or {}).get("friendly_name") or \
                    silent[0]["entity_id"]
                return f"{nm}现在是关着的，没有{attr_word}读数。"
            # unavailable = 实体离线，同样如实说明而不是不回话
            dead = [e for e in ents if str(e.get("state", "")) == "unavailable"]
            if dead:
                nm = (dead[0].get("attributes") or {}).get("friendly_name") or \
                    dead[0]["entity_id"]
                return f"{nm}现在不在线，读不到{attr_word}。"
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
            # v1.0.62 P1-6：light 域 color_temp 惯例是 **mireds**（370 mired≈2700K），
            # 旧代码裸报「色温 370K」=量纲错标签（与 aqi 事件同族）。≤1999 判为
            # mireds 换算（mired 可视域 140-500 与 Kelvin 1700-6500 无交叠，判据稳）。
            kelvin = int(round(1_000_000 / v / 50) * 50) if 0 < v < 2000 else int(v)
            return f"{prefix}{nm or dev_word}色温约 {kelvin}K。"
        if attr_word in ("风量", "风速", "档位"):
            # v1.0.62 P1-6：无单位的小整数是「档位」语义（1..12），percentage
            # 才报百分比——把空调 2 档播成「风量 2%」也是错标签。
            u = str(attrs.get("unit_of_measurement") or "").strip().lower()
            if not u and v <= 12 and float(v).is_integer():
                return f"{prefix}{nm or dev_word}现在是 {int(v)} 档。"
            pct = int(round(v)) if v <= 100 else int(round(v * 100 / 255))
            return f"{prefix}{nm or dev_word}风量约 {pct}%。"
        return f"{prefix}{nm or dev_word}{'设定' if '定' in attr_word else ''}{attr_word}是 {v:g}。"

    async def _count_answer(self, area, text: str) -> Optional[str]:
        """体验批 P2-14②：开着/没关 设备计数与点名（≤3 具名，多则只报数）。"""
        # v1.1.2 根修：设备类别改走统一表 class_of()。旧实现把类别词取自
        # 「可选组 + [^吗]{0,4}」的 search，最左匹配下那一组经常是空——
        # 「哪些窗开着」「还有几盏灯亮着」因此退化成**全域**计数，答案里混进
        # 麦克风开关/插座（答错比不答坏：用户听着像正确答案）。
        word, domains = class_of(text)
        if not domains:
            domains = ("light", "climate", "fan", "cover", "humidifier", "switch")
        ents = await self.ha.find_entities(area=area or "", domains=domains)
        ents = [e for e in ents if e["entity_id"].split(".")[0] in domains]
        on_words = {"on", "open", "opening", "heating", "cooling", "auto", "fan_only",
                    "dry", "heat_cool", "eco", "playing", "paused", "heat", "preheat"}
        on_ents = [e for e in ents if str(e.get("state", "")) in on_words]
        prefix = f"{area}的" if area else ""
        noun = _CLASS_NOUN.get(word, "个设备")
        if not on_ents:
            if not ents:
                return None               # 该区域/类别压根没设备：不猜，让位上层
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
            # v1.1.2：全屋压根没有可判在位/不在位的传感器时，如实说明——旧实现
            # 一律返回 None，用户问「有没有人」听到的是"这句话我还不会"，
            # 而"这家没装人感"是一个我们**确实知道**的事实。拿不准（有传感器
            # 只是绑不到这个区域）仍返回 None 让位上层，绝不猜"没人"。
            has_any = any(
                ((e.get("attributes") or {}).get("device_class") in self._PRESENCE_DCLASSES)
                for e in states.values() if isinstance(e, dict))
            if not has_any:
                return "家里还没接人感传感器，判断不了有没有人。"
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
