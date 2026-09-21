"""目标/参数提取公共件（自 fast_path v1.5 逐段移植，去掉 MCP 依赖）。

包含：_cn2num、_normalize_name、_extract_prefix、_levenshtein、拼音模糊、
候选六法（的/里分割、英文词、中文设备词、前缀剥离、区域前缀、拼音）+ 质量评分。
"""
from __future__ import annotations

import logging
import re

# 只读域清单与能力裁决共用一份定义（core.capability 不反向依赖本模块，无环）。
from ..capability import READ_ONLY_DOMAINS

logger = logging.getLogger("huijian.targets")

# v1.0.40：「两」补入表——creation.py 的时间/阈值正则（_AUTO_TIME_RE 的 h、
# _AUTO_NUM_RE 的 n、_CN_DIGITS）本就收录「两」，唯独本表漏了，导致
# 「下午两点」被算成 12:00（"两"分支取 CN_MAP.get("两",1)*100/10 的兜底）、
# 「超过两百度」算成 100。加一行即自愈：两百→200、两百五→205、两点→2。
CN_MAP = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100}
DEVICE_SUFFIX = ["室", "厅", "房", "间", "楼", "区", "馆", "灯", "扇", "机", "窗", "调", "备"]
AREA_SUFFIX = ["室", "厅", "房", "间", "楼", "区", "馆"]
# ── 2026-09-30 静态基准区表（用户令：数据集「我经常用到的窗型和区域」优化进加载项）──
# 数据集 payload area 实测频次里，不带 AREA_SUFFIX 尾字的通用区名：主卧x38/次卧x27/
# 阳台x20/玄关x8/车库x4/露台x4/走廊x2——冷启动（HA 区域注册表未同步）与注册表缺位时
# 这些区段整段丢失（「主卧灯打开」= 全屋灯；「次卧窗开到30」= 无区域泛窗扇出）。
# 它们是中文住宅通用空间词而非客户专属专名，固化零风险；客户自定义区名（如「影音室」
# 本来就带尾字/或走注册表）仍由 sync_areas 动态通道叠加承接，两层判据在 _area_like 汇合。
# 其余高频区（客厅/卧室/书房/厨房/办公室/老人房/儿童房/车间/浴室/卫生间/餐厅/展厅/
# 储物间/地下室/健身房/保姆房）全部以 AREA_SUFFIX 尾字收尾，静态后缀已覆盖，不重复入表。
BASE_AREAS = frozenset({"主卧", "次卧", "阳台", "玄关", "车库", "露台", "走廊"})

_KNOWN_DEVICES_TAIL = ["提升窗", "平开窗", "推拉窗", "平推窗", "天窗", "飘窗", "百叶窗", "筒灯", "灯泡",
                       # 2026-09-16 内倒语序洞：窗型词补全（fast_path 窗型纠正/集成侧
                       # valid window names 认这套，设备词表此前缺一半——「内开内倒窗」
                       # 这类长词落不进子串扫描，尾置动作句整体失配）
                       "内开窗", "外开窗", "推拉门", "智能窗", "内开内倒窗", "单内倒窗", "外装平开窗",
                       "内倒窗",
                       # 2026-09 开窗器名称洞（用户令优化「开窗器名称识别」）：
                       # 「关闭开窗器」此前被剥成 name="窗" 残渣、意图错判 Turn*——
                       # 开窗器/开合器/推窗器 是设备词，必须整词保留（fast_path 窗族纠正联动）。
                       "开窗器", "开合器", "推窗器",
                       # 2026-09 悬窗族（用户点名开窗器机型按「区域+窗名」命名：
                       # 客厅的上悬窗/下悬窗/悬窗/提升窗）。提升窗 原只在本表，
                       # 缺窗型纠正→「客厅提升窗关闭」错走 Turn*——一并入三处同步。
                       "下悬窗", "上悬窗", "悬窗",
                       # 2026-09-30 数据集对账补口（意图数据集窗型实测有词、全仓
                       # 六处窗表却从未收）：电动窗 x10。**电动窗帘必须同批入表**
                       # ——KNOWN_DEVICES 长词优先扫描，缺它则「电动窗帘」被 3 字
                       # 「电动窗」截胡成窗族（开帘变按窗钮=假动作）。
                       "电动窗", "电动窗帘",
                       # 2026-10-01 数据集对账二期（两份慧尖数据集 payload name 实测
                       # 有词、仓内从未收；折成泛称=过宽扇出，缺域提示=全实体面找名）：
                       # 具名灯具族 主灯x4/吊灯x3/阅读灯x2/镜前灯x2/感应灯x2/工业灯x2/
                       # 大灯x1——旧逻辑一律折成泛称「灯」，无区域时=整屋灯扇出；
                       # 帘族 卷帘x2/百叶帘x2——旧逻辑 parse score=0（硬失败）；
                       # 扇族 落地扇x1；空调机型族 中央/挂机/柜机 各x2——折成裸「空调」
                       # 后被「空调缺区域」守卫整句拦死（实测 打开中央空调=MISS）；
                       # 电风扇/投影仪/空气净化器 整词（子串折走连带同族其它实体，如
                       # 风扇 同时命中 排风扇+循环扇）。帘族/机型**不进 _WINDOW_TYPES
                       # 与集成窗表**（cover/climate 域，隔离有钉）。
                       "主灯", "大灯", "吊灯", "阅读灯", "镜前灯", "感应灯", "工业灯",
                       "卷帘", "百叶帘", "落地扇",
                       "中央空调", "挂机空调", "柜机空调",
                       "电风扇", "投影仪", "空气净化器",
                       "空调", "风扇", "窗户", "窗帘", "纱窗", "加湿器", "热水器", "净化器", "灯", "窗", "幕布",
                       "门", "电视", "投影", "音箱",
                       # v1.0.42 家电族：冷启动（registry 动态词表未同步时）也能
                       # 直呼这些常用设备名。
                       "扫地机器人", "扫拖机器人", "吸尘器", "拖地机", "雷达",
                       # 2026-09 通用智能家居产品词（用户令按市面常见品类扩充；
                       # 动态词表 sync_vocab 只覆盖已接入实体，这些是冷启动兜底）。
                       # 清洁：擦窗机器人**含窗字但属家电**——device_shared 排除表联动
                       "洗地机", "除螨仪", "擦窗机器人",
                       # 环境：新风/换气多挂 switch/fan，域提示宁缺毋滥
                       "新风机", "新风", "除湿机", "换气扇", "排风扇", "循环扇",
                       "取暖器", "电暖器", "暖风机", "浴霸", "香薰机",
                       # 厨卫大电
                       "洗衣机", "烘干机", "干衣机", "洗碗机", "油烟机",
                       "燃气灶", "灶具", "微波炉", "烤箱", "电饭煲", "空气炸锅",
                       "电磁炉", "破壁机", "榨汁机", "咖啡机", "饮水机", "净水器",
                       "电水壶", "养生壶",
                       # 生活/安防/控制：门锁走 lock 域话术道（打开门锁=上锁 D7 已钉）
                       "晾衣架", "晾衣机", "按摩椅", "音响",
                       "门锁", "智能门锁", "智能锁", "猫眼", "智能猫眼",
                       "摄像头", "监控", "门铃", "智能门铃",
                       "插座", "智能插座"]
KNOWN_DEVICES_PREFIX = ["空调", "风扇", "加湿器", "净化器", "热水器", "电视", "投影", "音箱", "幕布",
                        "窗帘", "窗户", "筒灯", "射灯", "灯带", "吸顶灯", "台灯", "落地灯", "床头灯", "夜灯",
                        # 2026-09-16 内倒语序洞（现场实锤「展厅平开窗内倒」→None）：
                        # ②③ 前缀剥离只认本表——「区域+窗型词+尾置动作」（X内倒/把X内倒/
                        # X关闭）此前全部够不到，落给 klar 产歧义目标再被过宽闸 clarify。
                        # 词表与 fast_path._WINDOW_TYPES/集成 valid names 同集合，守卫测试钉。
                        "平开窗", "平推窗", "内开内倒窗", "单内倒窗", "外装平开窗", "内开窗",
                        "内倒窗",
                        "外开窗", "推拉窗", "推拉门", "智能窗", "天窗", "飘窗",
                        # 开窗器/开合器/推窗器 同进前缀表：「开窗器关闭/开窗器打开」
                        # SOV 形靠 ② 前缀剥离回捞（fast_path 通用 开 字冠对器字已设护栏）。
                        "开窗器", "开合器", "推窗器",
                        # 悬窗族/提升窗（与 _WINDOW_TYPES/集成映射三方同步，守卫钉）。
                        "下悬窗", "上悬窗", "提升窗", "悬窗",
                        # 电动窗（与 _WINDOW_TYPES/集成两表三方同步，守卫钉）；
                        # 电动窗帘=cover 设备词同入前缀表（SOV「电动窗帘拉上」②
                        # 剥离回捞），**不进 _WINDOW_TYPES**（帘族，见 fast_path
                        # _window_type 短路闸）。
                        "电动窗", "电动窗帘",
                        # 2026-10-01 二期具名设备词同批入前缀表（SOV「主灯打开」
                        # 「卷帘拉上」「中央空调关闭」④ 前缀剥离要认；与表一同源，
                        # 守卫钉 test 表间一致）。
                        "主灯", "大灯", "吊灯", "阅读灯", "镜前灯", "感应灯", "工业灯",
                        "卷帘", "百叶帘", "落地扇",
                        "中央空调", "挂机空调", "柜机空调",
                        "电风扇", "投影仪", "空气净化器",
                        # 同批表间一致补口：百叶窗 原只在表一（同 2026-09「提升窗 原
                        # 只在本表」同型）——「客厅百叶窗关闭」④ 前缀剥不出尾动词，
                        # 整句 MISS；且它是 cover（_window_type 帘族闸返 None），只走
                        # Turn*，故仅入前缀表、**不进 _WINDOW_TYPES/集成窗表**。
                        "百叶窗", "纱窗",
                        "扫地机器人", "扫拖机器人", "吸尘器", "拖地机",
                        # 2026-09 通用智能家居产品词（与表一同步，SOV「X关闭」回捞）
                        "洗地机", "除螨仪", "擦窗机器人", "新风机", "新风", "除湿机",
                        "换气扇", "排风扇", "循环扇", "取暖器", "电暖器", "暖风机",
                        "浴霸", "香薰机", "洗衣机", "烘干机", "干衣机", "洗碗机",
                        "油烟机", "燃气灶", "灶具", "微波炉", "烤箱", "电饭煲",
                        "空气炸锅", "电磁炉", "破壁机", "榨汁机", "咖啡机", "饮水机",
                        "净水器", "电水壶", "养生壶", "晾衣架", "晾衣机", "按摩椅",
                        "音响", "门锁", "智能门锁", "智能锁", "猫眼", "智能猫眼",
                        "摄像头", "监控", "门铃", "智能门铃", "插座", "智能插座"]
# 修A（2026-09）：射灯/灯带/吸顶灯/台灯/落地灯/床头灯/夜灯 原只在表二，而候选⑥设备词子串扫描
# 与设备词加分只认表一——「办公室射灯」找不到 len≥2 设备词退单字「灯」(4+1=5)，
# ⑤区域候选("办公室","射灯",3+1=4)反而落败，区域整个丢失。
# 设备词全集并为一张表（③⑥⑦与加分共用）；表二保持原样（fast_path 前缀剥离/候选④依赖）。
def _dedup_keep_order(words):
    """按**书写序**去重（dict.fromkeys 保序）。设备表里等长词的先后必须是作者写下
    的顺序——本仓多处依赖这条纪律（「电动窗 排在泛称 窗 前」、悬窗族长词顶前、
    「本表 order=正确性」）。"""
    return list(dict.fromkeys(words))


# 静态表书写序（表一 先于 表二），KNOWN_DEVICES 与动态合并的唯一顺序来源。
_STATIC_ORDER: list[str] = _dedup_keep_order(_KNOWN_DEVICES_TAIL + KNOWN_DEVICES_PREFIX)
# ⚠ 2026-10-01 根修「同一句话时灵时不灵」：原实现 sorted(set(表一) | set(表二),
# key=len, reverse=True)——sorted 虽稳定，但 set 的迭代序随 PYTHONHASHSEED 变化，
# **等长词**的先后因此逐进程随机。实锤：「投影幕布」在多数进程命中 投影
# (media_player)、个别进程才命中 幕布(cover)（seed=42 才正确），现场即"有时能开
# 有时开错设备"。喂确定输入序后，等长平局恒落在书写序上。
KNOWN_DEVICES = sorted(_STATIC_ORDER, key=len, reverse=True)
EN_DEVICES = ["light", "lamp", "fan", "ac", "airconditioner", "switch", "outlet", "window",
              "curtain", "blind", "tv", "speaker", "heater", "humidifier", "downlight"]

# ── v1.0.99+ 英文目标桥（2026-09-30 用户现场主诉）──────────────────────
# 病灶：SenseVoice 听得出英文（'turn on the office light' 整句转写正确），t0 也
# 拆得出 area=office/name=light，但客户 HA 的区域与实体全是中文命名 →
# async_match_targets 必 miss →「没找到符合条件的设备」。LLM 复议档对无 LLM
# 配置的用户不存在，**离线确定性**是唯一通路。
# 路线：英文设备/区域词 → 中文规范词表；_build_plan 末端 bilingual_targets
# **追加**等价中文目标（英文原形保留：英文命名 HA 今日通路零回归；中文命名
# HA 接住新通路；Turn lane 集成逐目标并集匹配，互不干扰）。
EN_DEVICE_ZH: dict[str, str] = {
    "light": "灯", "lamp": "灯", "downlight": "筒灯", "ceiling light": "吸顶灯",
    "desk lamp": "台灯", "bedside lamp": "床头灯", "strip light": "灯带",
    "spotlight": "射灯", "bulb": "灯泡",
    "window": "窗", "door": "门", "curtain": "窗帘", "blind": "百叶",
    "fan": "风扇", "ac": "空调", "air conditioner": "空调",
    "air conditioning": "空调", "airconditioner": "空调",
    "tv": "电视", "television": "电视", "speaker": "音响",
    "switch": "开关", "outlet": "插座", "plug": "插座",
    "humidifier": "加湿器", "dehumidifier": "除湿机",
    "purifier": "净化器", "air purifier": "净化器",
    "heater": "取暖器", "camera": "摄像头",
    "vacuum": "扫地机器人", "robot vacuum": "扫地机器人",
    "lock": "门锁", "door lock": "门锁", "smart lock": "智能门锁",
    "projector": "投影", "screen": "幕布",
}
EN_AREA_ZH: dict[str, str] = {
    "office": "办公室", "living room": "客厅", "lounge": "客厅",
    "bedroom": "卧室", "master bedroom": "主卧", "second bedroom": "次卧",
    "guest room": "客房", "kitchen": "厨房", "bathroom": "卫生间",
    "toilet": "卫生间", "washroom": "卫生间", "restroom": "卫生间",
    "dining room": "餐厅", "study": "书房", "study room": "书房",
    "balcony": "阳台", "hallway": "走廊", "corridor": "走廊",
    "garage": "车库", "garden": "花园", "showroom": "展厅",
    "kids room": "儿童房", "children room": "儿童房",
    # 2026-09-30 数据集对账补齐（area 词频实测用户高频区）：
    "entrance": "玄关", "foyer": "玄关", "workshop": "车间",
    "basement": "地下室", "cellar": "地下室",
    "storage room": "储物间", "storage": "储物间",
    "gym": "健身房", "terrace": "露台",
}


def _en_singular(w: str) -> str:
    """英文词形归一：小写、剥句读、去冠词、规则复数（仅当剥 s 后是表内词才剥，
    防 status→statu 胡剥；windows→window、lights→light 皆中）。永不抛。"""
    s = (w or "").lower().strip().strip(".,;:!?")
    s = re.sub(r"^(?:the|a|an)\s+", "", s).strip()
    if s.endswith("s") and not s.endswith("ss"):
        base = s[:-1]
        if base in EN_DEVICE_ZH or base in EN_AREA_ZH or base in EN_DEVICES:
            return base
    return s


def en_device_zh(word: str):
    """英文设备词 → 中文规范词；非英文/无表项 → None（中文句零扰动）。"""
    return _en_lookup(EN_DEVICE_ZH, word)


def en_area_zh(word: str):
    """英文区域词 → 中文规范区域词；非英文/无表项 → None。"""
    return _en_lookup(EN_AREA_ZH, word)


def _en_lookup(table: dict, w: str):
    s = _en_singular(w)
    if not s or any("\u4e00" <= c <= "\u9fff" for c in s):
        return None
    return table.get(s)


def bilingual_targets(entries: list) -> list:
    """英文 area/name → 目标表末端**追加**等价中文目标（原条目不动、原序不动：
    ① 集成 Turn lane 逐目标并集匹配，中文命名现场自此可执行；② 英文命名现场
    命中面与改前逐字一致；③ ControlWindow 只读 targets[0]，英文窗类在
    _build_plan 窗前换形顶前，不经本函数兜尾——两通道各走各的。
    查无译名/含汉字 → 原样返回入参（非英文句零扰动）。永不抛。"""
    try:
        out = list(entries or [])
        added = False
        for t in (entries or []):
            if not isinstance(t, dict):
                continue
            area = str(t.get("area") or "").strip()
            zh_area = _en_lookup(EN_AREA_ZH, area) if area else None
            devs = [dv for dv in (t.get("devices") or []) if isinstance(dv, dict)]
            zh_devs, name_hit = [], False
            for dv in devs:
                nm = str(dv.get("name") or "").strip()
                zh = _en_lookup(EN_DEVICE_ZH, nm) if nm else None
                if zh:
                    name_hit = True
                    zh_devs.append({"name": zh,
                                    "domains": domain_hint(zh)
                                    or list(dv.get("domains") or [])})
                else:
                    item = {"domains": list(dv.get("domains") or [])}
                    if nm:
                        item["name"] = nm
                    zh_devs.append(item)
            if not (zh_area or name_hit) or not devs:
                continue
            # 空 area 是集成端 unset_area_constraint 特殊语义，**绝不**写入克隆
            nt = {}
            if zh_area or area:
                nt["area"] = zh_area or area
            nt["devices"] = zh_devs
            if nt not in out:
                out.append(nt)
                added = True
        return out if added else (entries or [])
    except Exception:
        logger.exception("[targets] 双语目标追加异常（原样放行）")
        return entries

# 属性/参数词：句中残留含这些词时禁入拼音模糊档（⑦）——它们是调节参数名，
# 近音撞进设备表就是"目标幻觉"（亮度→浴霸 事故形）。
_ATTR_NO_PINYIN = ("亮度", "色温", "温度", "湿度", "风量", "风速",
                   "开合度", "模式", "档位", "音量")

# 2026-10-01（v1.1.0 发版后对账审计）目标幻觉禁区：
# **动作/模式语素不是设备名**，一律禁入 ⑦ 拼音模糊档（与上方 _ATTR_NO_PINYIN 同族
# 同修法——「亮度→浴霸」那次事故就是这个形态）。实锤两例：
#   「内倒」neidao --⑦--> 「雷达」leida（滑窗 "neida" 距 1 ≤ tol；「雷达」是
#   v1.0.42 家电族收的毫米波存在传感器）⇒「打开内倒」产 TurnDeviceOn name=雷达：
#   用户要的是窗内倒动作，设备却去开传感器并回「办好了」=误执行+谎报；
#   「通风」tongfeng --⑦--> 「筒灯」tongdeng（距 1 ≤ tol=2）⇒ 同型误执行。
# 禁区只关**近音档**：「内倒窗/内开内倒窗/上悬窗」等字面词仍由 ⑥ 正常命中（实测
# 零回归）。宁 MISS 如实失败，绝不猜设备（v1.0.69 红线）。
_ACTION_NO_PINYIN = ("内倒", "内开", "外开", "上悬", "下悬", "悬窗", "开合",
                     "推窗", "通风", "换气", "排气", "制冷", "制热", "除湿",
                     "送风", "睡眠", "节能", "省电", "自动")

# ── 动态设备词表（体验批 P2-17：别名自学习）────────────────────
# 静态 KNOWN_DEVICES 是通用词表；真实部署里设备叫「氛围灯带/玄关射灯/新风机」等
# 千奇百怪。从 HA 实体 friendly_name 派生每装专属词表，parse_target/拼音模糊
# 共用——设备改名即自动跟上（每 30s 随状态缓存节流重派生），零持久化零学习风险。
_VOCAB_STOP = {"开关", "状态", "电量", "信号", "电池", "亮度", "色温", "温度", "湿度",
               "待机", "在线", "离线", "主开关", "设置", "传感器", "实体", "慧尖",
               "左", "右", "上", "下", "中", "全部", "全屋"}
_dyn_vocab: tuple[str, ...] = ()      # 已排序（长在前），整体替换赋值（GIL 原子）


def _name_tokens(friendly: str) -> list[str]:
    toks = re.split(r"[\s_\-/·，,、()（）\[\]【】]+", friendly or "")
    out = []
    for t in toks:
        t = t.strip()
        if not (2 <= len(t) <= 8) or t in _VOCAB_STOP:
            continue
        if not all("\u4e00" <= c <= "\u9fff" for c in t):
            continue
        out.append(t)
    return out


def sync_vocab(states: dict) -> None:
    """从 ha 状态缓存派生动态词表（O(实体数) 小任务，pipeline 节流调用）。"""
    names: set[str] = set()
    # v1.1.3 P0-1：域白名单 → **排除表**。原白名单只放 9 个域，其余（valve/
    # number/select/alarm_control_panel/water_heater/scene/script/dishwasher/
    # lawn_mower/siren…）连词表都进不去，用户怎么叫都"没找到设备"——这与
    # "语音控 HA 全设备"的目标正面冲突。反转为排除只读/系统域后可控域自动全覆盖。
    per_word: dict[str, set[str]] = {}
    for eid, ent in (states or {}).items():
        dom = str(eid).split(".", 1)[0]
        if dom in _VOCAB_EXCLUDED_DOMAINS:
            continue
        fn = str(((ent or {}).get("attributes") or {}).get("friendly_name") or "")
        toks = _name_tokens(fn)
        names.update(toks)
        for t in toks:
            per_word.setdefault(t, set()).add(dom)
    global _dyn_vocab, ALL_DEVICES, ALL_SET, _ALL_MIN2, _dyn_domains, _dyn_lookup
    # 动态词之间等长平局给**码点序**次键（注册表派生顺序不代表作者意图，但必须可
    # 复现）；静态表与动态词合并时**静态在前**，与 KNOWN_DEVICES 同一确定性判据。
    _dyn_vocab = tuple(sorted(names, key=lambda w: (-len(w), w)))
    merged = _dedup_keep_order(_STATIC_ORDER + list(_dyn_vocab))
    ALL_DEVICES = tuple(sorted(merged, key=len, reverse=True))
    ALL_SET = frozenset(merged)
    _ALL_MIN2 = tuple(d for d in ALL_DEVICES if len(d) >= 2)   # 已长→短
    # 词→域映射：域名按字母序固定（同一份注册表在任何进程/任何时刻得到同一张表），
    # 查找表按长度倒序（长词优先，与 ALL_DEVICES 同判据）。
    _dyn_domains = {w: tuple(sorted(ds)) for w, ds in per_word.items()}
    _dyn_lookup = tuple(sorted(_dyn_domains, key=lambda w: (-len(w), w)))


# 语音设备词表的排除域（v1.1.3 P0-1）：只读/系统域**不进设备词表**——
#   · sensor/binary_sensor/weather/person… 不是"可开关的设备"，收进来只会让
#     「打开温度计」这类话产出一个注定失败的计划；它们要能被问到，靠的是查询族
#     直接按实体名读 states（`test_targets_sync_vocab_and_clear` 钉的正是这条，
#     域白名单时代也在排除，本批不放宽）。
#   · stt/tts/notify/conversation/config/hassio… 是引擎或系统实体，出现在设备
#     名里只会造成误命中。
# 与旧白名单的差别在**反方向**：valve/number/select/alarm_control_panel/
# water_heater/dishwasher/siren/scene/script… 过去连词表都进不去（语音怎么叫都
# "没找到设备"），现在按注册表自动全覆盖——这才是"控 HA 全设备"的那一步。
_VOCAB_EXCLUDED_DOMAINS = frozenset(READ_ONLY_DOMAINS) | {
    "camera", "scene_config", "config", "hassio", "system_log", "diagnostics",
    "provisioning", "backup", "analytics", "timer", "wake_word",
    "homeassistant", "default", "trace",
}
_dyn_domains: dict[str, tuple[str, ...]] = {}
_dyn_lookup: tuple[str, ...] = ()


def clear_vocab() -> None:      # 测试隔离
    global _dyn_vocab, ALL_DEVICES, ALL_SET, _ALL_MIN2, _dyn_areas
    global _dyn_domains, _dyn_lookup
    _dyn_vocab = ()
    _dyn_areas = ()
    _dyn_domains = {}
    _dyn_lookup = ()
    ALL_DEVICES = tuple(sorted(_STATIC_ORDER, key=len, reverse=True))
    ALL_SET = frozenset(_STATIC_ORDER)
    _ALL_MIN2 = tuple(d for d in ALL_DEVICES if len(d) >= 2)


# ── 2026-09-30 动态区域表（意图数据集对账引进）────────────────────
# 病灶：区域判据此前只认「尾字∈室厅房间楼区馆」的静态后缀——数据集高频区域
# 主卧x38/次卧x27/阳台x20/玄关x8/露台/走廊/车库 全部落不进（「主卧灯打开」
# 区域整个丢失=全屋灯；「阳台窗开到30」折成无区域泛窗）。两层修复：通用空间词
# 固化为 BASE_AREAS 静态基准（上方）；客户自定义区名由本动态表承接——HA 区域
# 注册表是单一事实源（pipeline._sync_vocab 同一节流点喂入）。与 P2-17 设备词表同构。
_dyn_areas: tuple[str, ...] = ()     # 已排序（长名在前），整体替换赋值（GIL 原子）


def sync_areas(names) -> None:
    """从 HA 区域注册表派生（dict_values/list/set 皆可）。空=回落纯静态后缀。"""
    global _dyn_areas
    try:
        s = {str(n).strip() for n in (names or []) if n and str(n).strip()}
    except Exception:  # noqa: BLE001 同步器永不冒泡
        s = set()
    _dyn_areas = tuple(sorted(s, key=len, reverse=True))


def _area_like(s: str) -> bool:
    """区域判据：静态基准区表（BASE_AREAS，数据集实锤通用空间词）∪ 静态后缀
    （室厅房间楼区馆）命中，或真实区域表**全等/尾缀**命中（长名在前；'公室'
    这类残缺前缀不会误配——endswith 要求区域名完整落在尾部）。"""
    s = (s or "").strip()
    if not s:
        return False
    if any(w in s for w in ("帘", "纱窗", "百叶")):
        return False                      # 区域尾缀永不吞帘族词根（阳台≠…护栏）
    if s in BASE_AREAS:
        return True
    if any(s.endswith(w) for w in AREA_SUFFIX):
        return True
    for a in _dyn_areas:
        if s == a or s.endswith(a):
            return True
    return False


_STATIC_DEVICES = tuple(KNOWN_DEVICES)          # 已按长度倒序
_STATIC_SET = frozenset(KNOWN_DEVICES)
ALL_DEVICES = _STATIC_DEVICES                   # 静态+动态合并视图（parse_target 用）
ALL_SET = _STATIC_SET
_ALL_MIN2 = tuple(d for d in ALL_DEVICES if len(d) >= 2)


def cn2num(s: str) -> str:
    """中文数字→阿拉伯数字字符串（"二十三"→"23"，递归处理"一百二十三"）。原样移植。"""
    if not s:
        return "0"
    if s.isdigit():
        return s
    if "零" in s:
        s = re.sub(r"零+", "", s)
        if not s:
            return "0"
    if "百" in s:
        parts = s.split("百", 1)
        front = CN_MAP.get(parts[0], 1 if parts[0] else 1) * 100
        if len(parts) > 1 and parts[1]:
            return str(front + int(cn2num(parts[1])))
        return str(front)
    if "十" in s:
        if s == "十":
            return "10"
        if s.startswith("十"):
            return str(10 + CN_MAP.get(s[1], 0))
        if s.endswith("十") and len(s) == 2:
            return str(CN_MAP.get(s[0], 1) * 10)
        parts = s.split("十", 1)
        front = CN_MAP.get(parts[0], 1) * 10
        if len(parts) > 1 and parts[1]:
            return str(front + CN_MAP.get(parts[1], 0))
        return str(front)
    return str(CN_MAP.get(s, "0"))


_NUM_PAT = re.compile(r"[零一二三四五六七八九十百]+")
# 2026-10-01 数据集对账二期（慧尖数据集 payload name 实锤）：裸数词替换会咬掉
# **词汇化数字**——表内既有词「百叶窗」被改成「100叶窗」（实测 客厅百叶窗关闭/
# 卧室百叶窗关掉 整句失能=MISS，集成按名找实体也必 miss），「百叶帘」→「100叶帘」
# 连 domains 一起丢。归一的用途从来只有「编号口语化」（一号→1号），故加语境闸：
# 数词只有在后接索引量词时才转；纯数字名（"二十三"）整体仍转。
_INDEX_COUNTERS = "号栋楼层单元室区座组排档挡级路期井盏扇"
_NUM_CTX_PAT = re.compile(
    r"[零一二三四五六七八九十百千两]+(?=[" + _INDEX_COUNTERS + r"])")
_PURE_NUM_NAME = re.compile(r"^[零一二三四五六七八九十百千两]+$")


def normalize_name(name: str) -> str:
    """"一号测试窗"→"1号测试窗"（HA 实体名多为阿拉伯数字）。
    只在该数词是**编号**时转（后接 号/栋/楼…）；"百叶窗/百叶帘/千叶灯" 这类
    词汇化首字一律原样保留（见 _NUM_CTX_PAT 头注）。"""
    if not name:
        return name
    if name in ALL_SET:
        return name                      # 表内词恒等（第二道闸，防长词表内藏数字）
    out = _NUM_CTX_PAT.sub(lambda m: cn2num(m.group(0)), name)
    return cn2num(out) if _PURE_NUM_NAME.fullmatch(out or "") else out


def levenshtein(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        s1, s2 = s2, s1
    if not s2:
        return len(s1)
    prev = range(len(s2) + 1)
    for c1 in s1:
        curr = [prev[0] + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + (0 if c1 == c2 else 1)))
        prev = curr
    return prev[-1]


def extract_prefix(text: str, start: int = 2, end: int = 10) -> tuple:
    """头部前缀扫描：返回 (prefix, suffix)|(None, text)。
    ⚠ 原 v1.5 语义用 _DEVICE_SUFFIX（含 灯/窗/扇…，「窗户内倒」「卧室灯关」靠它拆），
    移植初版误收窄为 AREA_SUFFIX 导致 "窗户内倒一下" 整段失配，此处回改忠实原码。"""
    _SUF = ["室", "厅", "房", "间", "楼", "区", "馆", "灯", "扇", "机", "窗", "调", "备"]
    for i in range(start, min(end, len(text))):
        prefix, suffix = text[:i], text[i:].lstrip()
        if suffix and any(prefix.endswith(w) for w in _SUF):
            return prefix, suffix
    return None, text


def split_area_head(text: str) -> tuple:
    """句首**区名**切分 → (area, rest)；无区名 (None, text)。

    extract_prefix 的判据是"前缀尾字∈室厅房间楼区馆灯窗扇机…"，覆盖不到
    BASE_AREAS 里不带尾字的通用空间词：「阳台窗帘调成50%」在 i=3 处把
    「窗帘」从中间劈开（阳台窗 | 帘调成50%），②③ 与内层设备剥离全失配，
    整句退到 T1 → 50% 数值直接丢光（数据集对账实锤：阳台/主卧/次卧/玄关系列
    「区域+设备+绝对值」句全灭，同构的「客厅窗帘调成80%」却因为"厅"是尾字而通过）。
    区域表（静态 BASE_AREAS ∪ HA 动态区表）本身认得这些词，这里按长名优先
    直给切分，只回区域不吃设备名。帘族护栏同 _area_like（"阳台" 前缀不吞帘）。
    """
    t = (text or "").strip()
    if not t:
        return None, t
    try:
        names = sorted(set(BASE_AREAS) | set(_dyn_areas), key=len, reverse=True)
        for a in names:
            if a and t.startswith(a) and len(t) > len(a):
                return a, t[len(a):].lstrip("的地里得")
    except Exception:  # noqa: BLE001 切分器永不冒泡（保守=不切）
        return None, t
    return None, t


def _area_of_prefix(pre: str) -> str | None:
    """目标词前缀→区域名（修A）：尾字区域词命中（"办公室"）→ 剥属格「的/里/得」重试
    （"办公室的"）→ 二次前缀扫描回捞（未知复合词 "办公室吊灯" 的单字残段→"办公室"）。
    只回区域不拼设备名，避免拿未知残字重构出臆造名词。
    2026-09-30：判据并 HA 真实区域表（sync_areas）——主卧/次卧/阳台/玄关 等
    不带区域尾字的高频区名自此可析出（数据集实锤丢失面）。"""
    pre = re.sub(r"[的里得]+$", "", (pre or "").strip())
    if not pre:
        return None
    if _area_like(pre):
        return pre
    return extract_prefix(pre)[0]


def strip_modal(raw: str) -> str:
    """剥离语气词/英文冠词残留 + 处置介词「把/将」头部（"把灯打开"核心化）。"""
    raw = re.sub(r"^[把将]\s*", "", (raw or "").strip())
    raw = re.sub(r"(这个|那个|一下|吧|嘛|啊|啦|哦|哟)$", "", raw).strip()
    for prefix in ("the ", "in the ", "in ", "at "):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix):].strip()
            break
    return raw


_NAME_TAIL_VERBS = re.compile(r"(打开|关闭|关掉|关了|开一下|关上|开|关|动作|一些|全部|都)$")


def clean_name(name: str) -> str:
    """目标名清洗：反复剥离尾部动词残留与首部助词（"灯打开"→"灯"，"的筒灯"→"筒灯"）。"""
    prev = None
    while name != prev:
        prev = name
        name = _NAME_TAIL_VERBS.sub("", name).strip()
        name = re.sub(r"^[的地得了了]", "", name).strip()
    return name


def _dyn_lookup_domain(n: str) -> list[str]:
    """注册表派生域：整词全等优先，其次长词包含（"办公室射灯"⊃"射灯"）。

    只认 ≥2 字词（单字"灯/窗"参与包含匹配会把"香薰灯"判成灯域以外的东西，
    静态链对这类泛称判断更稳），且**不做跨词根拼接**——与 parse_target 的
    「阳台灯≠台灯」护栏同一纪律。
    """
    if not n:
        return []
    hit = _dyn_domains.get(n)
    if hit:
        return list(hit)
    for w in _dyn_lookup:
        if len(w) >= 2 and w in n:
            return list(_dyn_domains[w])
    return []


def domain_hint(name: str) -> list[str]:
    """设备名词表→HA 域提示（原 fast_path hint_domains 逻辑逐字移植）。

    v1.1.3 P0-1：先查**注册表派生**的词→域映射（域信息本来就直接写在 entity_id
    前缀里，由 sync_vocab 建表），命中即真值；静态判据链退为冷启动/无注册表
    兜底。这一层把「116 静态词里 54 词无域提示」的整类缺陷归零：新设备接进来
    不需要再往词表里补词，域也不会猜错（例：客户把加湿器命名成"香薰机"，
    旧逻辑 domains=[]，集成端只能全实体面按名找）。
    """
    raw = (name or "").strip()
    n = raw.lower()
    dyn = _dyn_lookup_domain(n)
    if dyn:
        return dyn
    if "空调" in n or "空調" in n:
        return ["climate"]
    if "灯" in n or "照明" in n:
        return ["light"]
    if "窗帘" in n or "百叶" in n or n.endswith("帘") or "幕布" in n or "纱窗" in n:
        # 2026-10-01 数据集对账二期：卷帘x2/百叶帘x2 此前 domains=[]（集成端只能全
        # 实体面找名，同名歧义面大）——帘族按词尾收口（纱帘/罗马帘/梦境帘同源）。
        # 幕布 必须在 投影 规则**之前**判：HA 生态里「投影幕布」是 cover，而
        # 「投影仪」才是 media_player——顺序错则幕布被划进媒体设备域（实锤错域）。
        # 纱窗 同批补口（v1.1.1 绝对值车道实需）：HA 生态里纱窗=cover 实体，
        # 与 fast_path._POS_CURTAIN_WORDS/_CURTAIN_ROOT_WORDS 的帘族判定同源；
        # 缺它则「纱窗开到50%」族落不到属性名（认不出族=如实 MISS 是新车道
        # 红线，但设备族本身在表里就得认得出）。
        return ["cover"]
    if "风扇" in n or n.endswith("扇"):
        # 落地扇x1 同型缺陷；扇族词尾一律 fan（吊扇/壁扇/台扇同源）。灯规则在上方，
        # 「吊扇灯」这类灯扇一体实体仍先判 light（现状不变）。
        return ["fan"]
    # 2026-09 通用智能家居品类扩充（擦窗机器人含窗字但属 vacuum 族——
    # 域提示在窗/帘判定之后、与按压窗控无涉；换气/排气/循环扇生态恒 fan）。
    if any(w in n for w in ("扫地", "吸尘", "机器人", "洗地机", "拖地机", "除螨仪")):
        return ["vacuum"]
    if any(w in n for w in ("换气扇", "排风扇", "循环扇")):
        return ["fan"]
    if any(w in n for w in ("门锁", "智能锁")):
        return ["lock"]
    if any(w in n for w in ("插座",)):
        return ["switch"]
    if any(w in n for w in ("摄像头", "监控", "猫眼")):
        return ["camera"]
    if "电视" in n or "tv" in n or "音响" in n:
        return ["media_player"]
    # v1.0.42 家电域提示补齐（此前加湿器/净化器等 domains=[] ，集成端只能
    # 全实体面找名——同名歧义面大）。净化器 HA 生态多挂 fan 域，两域并集提示。
    if "加湿" in n or "除湿" in n:
        return ["humidifier"]
    if "净化" in n:
        return ["fan", "humidifier"]
    if "投影" in n:
        return ["media_player"]
    if "热水器" in n:
        return ["water_heater"]
    if "洗碗" in n:
        return ["dishwasher"]
    if "洗衣机" in n or "烘干" in n:
        return ["laundry_washer", "laundry_dryer"]
    return []


# ── 2026-09-30 泛称近音折叠救援（用户日志：「展厅催拉窗开到百分之三十」）──
# 病灶：STT 把「推拉窗」听成「催拉窗」——⑥ 单字泛称档把整词折成「窗」，区域+
# 泛称 = 整区窗扇出（现场被过宽闸 clarify 拦成「说具体点」，用户其实说具体了，
# 是识别歪了字）。逐词加纠错表治不了本（近音变体持续新冒），这里做**音节级**
# 拼音救援：泛称字前的修饰段+泛称字 与表内同长同尾词比拼音节，**全局唯一且
# 只差一个音节**才收（催cui/推tui 一音节之差；「宁开窗」对 内开窗/平开窗 各差
# 一音节成平局 → 不救）。两字词不入救援窗（短窗噪音大：拉窗→天窗 形似实错）。
# 护栏：修饰段纯汉字、不含属性词/帘族字；pypinyin 缺失/异常 → 一律 None
# （行为回落到改动前的泛称折叠，fail-open）。
_RESIVE_NO_GO = ("帘", "纱", "叶")


def _generic_rescue(pre: str, generic: str):
    """泛称字前的残段 pre → 应顶替「pre+泛称」整段的表内标准词；None=不救。"""
    try:
        from pypinyin import lazy_pinyin
        pre = (pre or "").rstrip("的地得")
        if not (2 <= len(pre) <= 4):
            return None
        if not all("\u4e00" <= c <= "\u9fff" for c in pre):
            return None
        if (any(w in pre for w in _ATTR_NO_PINYIN)
                or any(w in pre for w in _ACTION_NO_PINYIN)
                or any(c in pre for c in _RESIVE_NO_GO)):
            return None
        # 剥掉可识别的区域前缀（「展厅催拉」→ 修饰段只剩「催拉」；区域本身
        # 由调用方 _area_of_prefix 走既有车道回捞）
        area = _area_of_prefix(pre)
        junk = pre[len(area):] if area and pre.startswith(area) else pre
        if not (2 <= len(junk) <= 3):
            return None
        word = junk + generic
        if len(word) < 3 or word in ALL_SET:
            return None
        syl = lazy_pinyin(word)
        scored = []
        for cand in ALL_DEVICES:
            if len(cand) != len(word) or not cand.endswith(generic) or cand == word:
                continue
            if not all("\u4e00" <= c <= "\u9fff" for c in cand):
                continue
            if any(c in cand for c in _RESIVE_NO_GO):
                continue
            cs = lazy_pinyin(cand)
            scored.append((sum(1 for a, b in zip(syl, cs) if a != b), cand))
        if not scored:
            return None
        best_d = min(d for d, _ in scored)
        if best_d > 1:
            return None
        winners = {c for d, c in scored if d == best_d}
        if len(winners) == 1:
            return winners.pop()      # 全局唯一最近（同音 d=0 或差一个音节 d=1）
    except ImportError:
        return None
    except Exception:
        logger.exception("[targets] 泛称近音救援异常（回落原车道）")
    return None


def _area_split_wins(stripped: str, idx: int, dev_len: int) -> bool:
    """设备词命中起点是否落在**更长区域名内部**（跨词根拼词判据）。

    idx=设备词在句中的起点，dev_len=其长度。若存在前缀长度 L ∈ (idx, idx+dev_len)
    使 stripped[:L] 命中区域判据（_area_like），说明设备词的头部其实是区域名的
    尾字——'阳台灯' 区名「阳台」L=2 > idx=1，「台灯」的「台」是区名尾字而非设备
    词根。零重叠（L==idx，如 '阳台台灯'）不触发，具名设备原样保留。"""
    for L in range(idx + 1, min(idx + dev_len, len(stripped))):
        if _area_like(stripped[:L]):
            return True
    return False


def parse_target(raw: str, action_match=None) -> tuple[str | None, str | None, int]:
    """候选六法 + 质量评分（v1.5 并行提取段移植）。
    action_match: 可选谓词 callable(rest)->bool，命中给 ④ 前缀候选加分（原 4 分档）。
    返回 (area, name, score)；解析失败时 area=None、name=清洗后的 raw。"""
    raw = strip_modal(raw or "")
    # 2026-09-30 英文 in-后置方位形：'light in the office'/'lights in the living
    # room' → 设备词挪尾、区域词挪头，喂给 ② 的「区域+设备尾词」结构。
    # 判据锁死空格分写的 ASCII 词形——中文目标永远没有这种空格，零扰动。
    m_in = re.match(r"^([A-Za-z][A-Za-z0-9 ]*?)\s+in\s+(?:the\s+)?([A-Za-z][A-Za-z0-9 ]*)$",
                    raw.strip())
    if m_in:
        raw = f"{m_in.group(2).strip()} {m_in.group(1).strip()}"
    if not raw:
        return None, None, 0
    candidates: list[tuple[str, str, int]] = []
    # ① 的/里 分割
    for sep in ("的", "里"):
        if sep in raw:
            p = raw.split(sep, 1)
            if len(p[1]) > 0 and len(p[0]) <= 6:
                candidates.append((p[0].strip(), p[1].strip(), 3))
    # ② 英文尾词（2026-09-30：复数归一 + 多词设备尾短语「air conditioner」。
    #    name 存归一后的英文单数原形——英文命名 HA 的既有匹配面零回归；
    #    中文命名 HA 由 _build_plan 末端 bilingual_targets 并集追加中文目标）
    words = raw.strip().split()
    for _k in (3, 2, 1):
        if len(words) <= _k:
            continue
        _tail = _en_singular(" ".join(words[-_k:]))
        if _tail in EN_DEVICE_ZH:
            candidates.append((" ".join(words[:-_k]), _tail, 3))
            break
    # ③ 中文设备尾词
    stripped = raw.strip()
    for d in KNOWN_DEVICES:
        if stripped.endswith(d) and len(stripped) > len(d):
            candidates.append((stripped[: -len(d)].strip(), d, 2))
            break
    # ④ 已知设备前缀剥离（"空调风量大一点"→设备=空调；剩余匹配动作→4 分）
    for d in sorted(KNOWN_DEVICES_PREFIX, key=len, reverse=True):
        if stripped.startswith(d) and len(stripped) > len(d):
            rest = stripped[len(d):].strip()
            if action_match and action_match(rest):
                candidates.append((d, rest, 4))
            else:
                candidates.append((d, rest, 3))
            break
    # ⑤ 区域前缀扫描（2026-09-30：判据升级为「静态后缀 ∪ HA 真实区域表」，
    #   主卧/次卧/阳台/玄关 等不带区域后缀的字面自此可析出）
    for i in range(2, min(5, len(stripped))):
        pre, suf = stripped[:i], stripped[i:]
        if suf and _area_like(pre):
            candidates.append((pre, suf, 3))
    # ⑥ 设备词子串优先（"暂停窗户动作"→窗户；移植期新增，堵原表中段词漏提）。
    #    先做 len≥2；无果退单字通用词（灯/窗/门），再往后才轮到拼音档，
    #    防 "灯打开" 被近音 "灯泡"(dist=2) 截胡（tie 先到优先）。
    #    修A：区域提取统一走 _area_of_prefix（剥「的」+二次回捞），不再因残字丢区域。
    _hit_dev = False
    for d in _ALL_MIN2:
        idx = stripped.find(d)
        if idx >= 0:
            # 2026-09-30 帘字防吞闸（收 电动窗 同批揪出的存量缺陷）：窗型词根
            # 后紧跟「帘」是帘设备名——「智能窗帘」曾被 "智能窗" 截胡→窗型纠正
            # →按窗钮+帘不动（假动作）。跳过该词让长帘词/「窗帘」承接，再不行
            # 落泛称→⑦ 拼音（'催拉窗帘'→窗帘）。
            if d.endswith("窗") and stripped[idx + len(d):idx + len(d) + 1] == "帘":
                continue
            # 2026-10-01 复核补口（09-30 数据集对账批遗留红）：区域名尾字与设备词
            # 首字跨词根拼词——「阳台灯/露台灯」内含 台灯（起点落在区名「阳台」
            # 内部），⑥ 贪心先命中即把「台」吞进设备名、区域整段丢失（现场=阳台灯
            # 喊成台灯，假动作；同型「南阳台灯」在动态区表下同样塌）。判据与写法
            # 沿用上闸：命中点落在更长区域名内部 → 拼词假象，continue 让单字泛称
            # 车道以「整区名+设备字」承接。零重叠不受影响（'阳台台灯'仍出台灯）。
            # idx==0 一律不裁：设备词整段居首=HA 实体自带区域的注册表全名（「客厅
            # 空调」由 sync_vocab 入表后长词优先命中），拆成 area+裸空调会让「实体
            # 名带房间、但没挂 area」的现场 miss（test_nlu_llm_boundary 钉）。
            if idx and _area_split_wins(stripped, idx, len(d)):
                continue
            candidates.append((_area_of_prefix(stripped[:idx]), d, 5))
            _hit_dev = True
            break
    if not _hit_dev:
        for d in _SINGLE_GENERIC:
            idx = stripped.find(d)
            if idx >= 0:
                if d == "窗" and stripped[idx + 1:idx + 2] == "帘":
                    continue              # 同上帘字防吞（泛称档）
                # 泛称折叠救援（见 _generic_rescue 头注）：单字泛称前带 2~3 字
                # 纯汉字修饰段时先试音节级近音整词；不中才退泛称原车道。
                wd = _generic_rescue(stripped[:idx], d)
                if wd is not None:
                    candidates.append((_area_of_prefix(stripped[:idx]), wd, 5))
                    _hit_dev = True
                    break
                candidates.append((_area_of_prefix(stripped[:idx]), d, 4))
                _hit_dev = True
                break
    # ⑦ 拼音模糊（v1.5 两缺陷修正：a) 首个 ≤5 即 break 会让「空调 kongtiao」被
    #    「筒灯 tongdeng」(dist=5) 截胡；b) 阈值过松噪音大。改为全表择优 + 收紧 ≤2
    #    + 仅在无子串命中时启用；短文本才跑）
    # 2026-09-21 目标幻觉实锤再收紧（「调高亮度到80%」曾把 liangdudao80% 窗口
    # 撞 dist=2 配成"浴霸"）：①两字设备（拼音 ≤5 字母）容差降到 1——4 字母
    # 窗口错 2 个=半句皆可错，短词误配是必然；②残段含属性词禁入⑦——亮度/色温
    # 这类**参数名**永远不该升格成设备目标（属性句走 no-target+上下文继承）。
    if (not _hit_dev and len(stripped) <= 8
            and not any(w in stripped for w in _ATTR_NO_PINYIN)
            and not any(w in stripped for w in _ACTION_NO_PINYIN)):
        try:
            from pypinyin import lazy_pinyin
            py_raw = "".join(lazy_pinyin(stripped))
            best = None  # (dist, -len(d), d)
            for d in ALL_DEVICES:
                if len(d) < 2:
                    continue
                py_dev = "".join(lazy_pinyin(d))
                tol = 1 if len(py_dev) <= 5 else 2
                dist = 99
                for i in range(max(0, len(py_raw) - len(py_dev) + 1)):
                    dist = min(dist, levenshtein(py_raw[i: i + len(py_dev)], py_dev))
                if dist <= tol and (best is None or dist < best[0] or (dist == best[0] and len(d) > -best[1])):
                    best = (dist, -len(d), d)
            if best is not None:
                candidates.append(("", best[2], 4 if best[0] <= 1 else 3))
        except ImportError:
            pass
        except Exception:
            pass

    best_score, area, name = 0, None, stripped
    for a, n, base in candidates:
        score = base
        if n in ALL_SET or any(k in n.lower() for k in EN_DEVICES):
            score += 1
        if a and _area_like(a):
            score += 1
        if score > best_score:
            best_score, area, name = score, a, n
    # 质量门：低分且无显式分隔符 → 交上层回退（原 score<3 规则）
    if best_score < 3 and "的" not in stripped and "里" not in stripped and " " not in stripped:
        return None, None, best_score
    cleaned = clean_name(normalize_name(name))
    return area, cleaned or name, best_score


# ── 并列宾语展开（2026-09-21 用户令第③点：「打开展厅内倒窗和推拉窗」必须两扇
#     都开）────────────────────────────────────────────────────────
# 病灶：共享动词的"打开A和B"从不进分句通路——T0 单发把 parse_target 吃到的
# 那一个执行掉并回「办好了」，另一半**静默丢弃**（半执行+谎报，链发纪律里
# 最危险形态）。本函数把它改写成"打开A、打开B"动词连排形态，交既有
# serial_clauses/split_actions 链发（全有全无+区域上下文注入全部复用）。
# 判据宁严勿松：句首显式动词、连接词两侧**全部**以已知设备词结尾、任一分片
# 含动词或长度越界 → 返回 None 原样交既有通道。"开合器"等词无「和」字零冲突。
_COORD_HEAD = re.compile(
    r"^(打开|开启|关闭|关掉|关上|拉开|拉上|拉下|播放|停止|暂停)"
    r"(?:一下)?[把将]?[\s]*(?=\S)")
# SOV 尾动形（2026-09-21 二批）：「内倒窗和推拉窗打开」「把A和B关闭」——
# 实测 T0 单发吃一扇谎报成功，且「内倒」位置动作 a 污染整句到推拉窗。
# 只收双字尾动词（裸"开/关"单字歧义大，不入）。
_COORD_TAIL = re.compile(r"(?:把|将)?\s*(打开|开启|关闭|关掉|关上|拉开|拉上|拉下)$")
# 顿号=汉语并列常规形（「打开平开窗、推拉窗」），一并收；设备词表无标点零冲突
_COORD_CONJ = re.compile(r"[和与、]")
# 动词片只认**双字动词形**——单字表会误杀设备词（"推_拉_窗""空_调_"），
# 首版实测即栽在此（coord_clauses 恒 []）。宁可漏判（漏→原通道，不误伤）。
_COORD_VERBISH = ("打开", "开启", "关闭", "关掉", "关上", "调高", "调低",
                  "调亮", "调暗", "调到", "设为", "设到", "设置", "设定",
                  "播放", "停止", "暂停", "锁上", "解锁", "拉上", "拉下",
                  "拉开", "摇上", "摇下", "换成", "变到")


def _coord_ends_device(seg: str) -> bool:
    return any(seg.endswith(d) for d in _ALL_MIN2)


# M2（2026-09-23 深审）：候选⑤特批的单字通用设备词（parse_target 认它们做
# 目标），coord_refuse 的 ≥2 字判据却看不见——「关灯和窗」右片「窗」是设备
# 词但整段长 1，不被拒 → 单发灯丢窗谎报成功。判据盲区=同病灶外延。
_SINGLE_GENERIC = ("灯", "窗", "门")


def _coord_refuse_seg_device(seg: str) -> bool:
    return seg in _SINGLE_GENERIC or _coord_ends_device(seg)


def _coord_split(text: str):
    """并列骨架分解：SVO「打开A和B」与 SOV「把A和B打开」同收。

    返回 (verb, segs)（segs 已剥语气词/把字头/属格，≥2 片）；不是并列形态
    → None。分片正确性判据（已知设备尾/无内藏动词）留在调用方——
    coord_refuse 只需要"连词挂在设备尾之后"这一半。"""
    text = (text or "").strip().strip("。！？!?")
    if not (4 <= len(text) <= 30):
        return None
    m = _COORD_HEAD.match(text)
    if m:
        verb, body = m.group(1), text[m.end():]
    else:
        stripped = re.sub(r"^[把将]\s*", "", text)   # SOV 把字头
        mt = _COORD_TAIL.search(stripped)
        if not mt or mt.start() < 2:
            return None
        verb, body = mt.group(1), stripped[:mt.start()]
    segs = [strip_modal(s).strip(" 的") for s in _COORD_CONJ.split(body)]
    segs = [s for s in segs if s]
    if not (2 <= len(segs) <= 8):
        return None
    return verb, segs


def coord_clauses(text: str) -> list[str]:
    """「打开A和B」→ ["打开A", "打开B补区域"]；SOV「A和B打开」同收；否则 []。"""
    sp = _coord_split(text)
    if not sp:
        return []
    verb, segs = sp
    fixed: list[str] = []
    for s in segs:
        s = re.sub(r"^(?:帮我把|帮我|请|麻烦|把)", "", s).strip(" 的")
        if not (2 <= len(s) <= 12):
            return []
        if any(v in s for v in _COORD_VERBISH):
            return []                       # 片内藏动词=正常连排/复合句，交原通道
        if not _coord_ends_device(s):
            return []                       # 任何一片不是已知设备尾词 → 不扩
        fixed.append(s)
    area = _area_of_prefix(fixed[0]) or ""
    out: list[str] = []
    for s in fixed:
        if area and not _area_of_prefix(s):
            s = f"{area}的{s}"              # 共享区域回填（同句同房间语义）
        out.append(verb + s)
    return out


# H2/M6（2026-09-23 深审批2）风险目标判据扩容：
# ① 域别名闭包——执行面 custom_components/huijian_ai/intent_helper.py
#    DOMAIN_ALIASES{"door":["lock","cover","button"]} 会把 door 扩进锁域，
#    闸只认字面 "lock" 即被 `domains:["door"]` 旁路（免确认解锁）。本表为
#    **受控重复**（core 不 import 集成包，v1.0.62 双端同构惯例），
#    由 test_v1064_risk_batch 拿集成侧 intent_helper 源码钉两表 door 形一致。
# ② 撤防族——intent_turn D7 反转映射 alarm×TurnOff→alarm_disarm，三层闸
#    原只认锁；一句话直撤家庭安防比拔锁更重。alarm 域 / 名含安防·报警·
#    布防·撤防 并入同一判据（TurnOn=布防安全向，维持不入闸）。
_RISKY_DOMAIN_ALIASES: dict[str, tuple[str, ...]] = {
    "door": ("lock", "cover", "button"),
    "doors": ("lock", "cover", "button"),
}
_LOCK_NAME_WORDS = ("锁", "大门", "房门", "卷帘门")
_ALARM_NAME_WORDS = ("安防", "报警", "布防", "撤防")
_ALARM_DOMAINS = ("alarm_control_panel",)


def _risky_domain_closure(domains) -> set[str]:
    """domains（可混 entity_id 形）→ 小写裸域 + 别名闭包。永不抛。"""
    out: set[str] = set()
    try:
        stack = [str(d).lower().split(".", 1)[0].strip()
                 for d in (domains or [])]
        while stack:
            d = stack.pop()
            if not d or d in out:
                continue
            out.add(d)
            for a in _RISKY_DOMAIN_ALIASES.get(d, ()):
                if a not in out:
                    stack.append(a)
    except Exception:  # noqa: BLE001
        return out
    return out


def args_target_lock(args: dict) -> bool:
    """args 是否指向**风险实体**（锁：D7 反转=TurnOff 即解锁；alarm：off 即
    撤防）。函数名沿用 args_target_lock（三层闸三消费点不动名接入，
    语义面已扩；pipeline/agent 的闸内文案仍按解锁族问句）。

    2026-09-22 审查批 C2：确认环旧判据只查设备名含「锁」——三种 args 形态里
    只罩住第一种，另两种旁路：
      ① 慧尖形态设备名含「锁」（t0/T1/LLM 车道，原有判据）；
      ② klar grounded 平铺 entity_id=lock.*——引擎命中锁实体时 args 里只有
         拼音 entity_id、**无任何中文**（与窗户闸门「查不到窗」同病灶同方向），
         「解锁大门」被 grounded 成 HassTurnOff lock.x 后原闸失罩、直接拔锁；
      ③ devices[].domains 显式含 lock——「关闭所有门锁」全屋形 name 为空、
         只有域过滤，同样失罩。
    永不抛：形制异常按 False（漏判面由级联话术兜底，绝不误拦正常句）。"""
    try:
        for ent in args.get("target") or []:
            for dev in (ent or {}).get("devices") or []:
                d = dev or {}
                name = str(d.get("name") or "")
                if any(w in name for w in _LOCK_NAME_WORDS + _ALARM_NAME_WORDS):
                    return True
                doms = d.get("domains")
                if isinstance(doms, (list, tuple)):
                    closed = _risky_domain_closure(doms)
                    if "lock" in closed or closed & set(_ALARM_DOMAINS):
                        return True
        name_all = str(args.get("name") or "")
        if any(w in name_all for w in _LOCK_NAME_WORDS + _ALARM_NAME_WORDS):
            return True
        eids = args.get("entity_id")
        if isinstance(eids, str):
            eids = [eids]
        if isinstance(eids, (list, tuple)):
            return any(isinstance(e, str)
                       and e.split(".", 1)[0] in ("lock",) + _ALARM_DOMAINS
                       for e in eids)
    except Exception:  # noqa: BLE001
        return False
    return False


def coord_refuse(text: str) -> bool:
    """并列句（连词挂在已知设备尾之后）→ 单发通路必须拒猜。

    coord_clauses 只在全部分片都是已知设备时扩链；「打开内倒窗和不存在的X」
    这类右片听不懂的并列句若放给 T0 单发，parse_target 会吃掉左片执行并
    谎报「办好了」——右片被静默丢弃=半执行。本判据说的是：**只要并列连词
    挂在已识别设备尾之后**，单发怎么裁都错，如实交 fallback/LLM 兜底。
    SOV 同判（「内倒窗和X关闭」单发同样半执行）。"""
    sp = _coord_split(text)
    if not sp:
        # M2 补充（2026-09-23 深审）：裸 开/关 头按 v1.0.60 定案不入扩链头表
        # （歧义大），但「关灯和窗」单发吃灯丢窗=同族半执行谎报。只在本拒绝
        # 通道加窄判据：单字动词+单字通用设备词+连词+短尾 → 整句拒猜。
        # （扩链 coord_clauses 不碰——右片是否真设备不在此判断，宁拒勿猜。）
        if re.fullmatch(r"(?:开|关)(?:一下|掉|闭)?[灯窗门][和与、][\u4e00-\u9fffA-Za-z0-9]{1,10}", text):
            return True
        return False
    _, segs = sp
    # M2：len≥2 判据对单字通用设备词（灯/窗/门）是盲区——"关灯和窗"右片
    # 听不懂时单发怎么裁都错（v1.0.59 原话），单字形态同样适用。
    if not (1 <= len(segs[0]) <= 12):
        return False
    return _coord_refuse_seg_device(segs[0])
