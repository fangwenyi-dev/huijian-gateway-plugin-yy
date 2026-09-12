"""T0/T1 快速通道级联（自 061701 树 fast_path.py v1.5（840 行，三缺陷已修）移植）。

收编改造点（对照《落地方案 v4》§5.2 与收编三修）：
  1. 执行通道：原 MCP(call_mcp_endpoint_tool) + 假想 REST(/api/huijian_ai/fast_intent，
     从未有实现) 双路径 → 统一产出 Plan{intent,args}，由 executor 走
     POST /api/intent/handle；MCP 工具发现(_TOOL_NAME_MAP/props 检查)整体移除——
     14 个 huijian_ai intent + HA 内置快捷意图名字面已知，无需动态发现。
  2. PlayMusic：模式 B 无小智播放面 → 不接管（落到兜底/LLM 档），原表保留注释。
  3. 阈值统一 0.7 缺陷 → textcnn.py 按类阈值（OOS 0.85 / SceneTrigger 0.5 / 其余 0.7）。
  4. _HA_REQUEST_TIMEOUT NameError 缺陷 → 不再适用（桥层 aiohttp timeout=10 常量化）。
  5. 动作 "A"：透传后集成 find_action_in_text 归一（实测 intent_window_const.py:30
     关键字含 "A"/"a"）；本层输出统一小写 "a"。
输出契约：match() 返回 Plan 或 None；None=本级联未接管（上层继续走查询族/LLM/兜底）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from . import corrector, targets as T
from . import creation
from .music import GENERIC_WORDS as _MUSIC_WORDS
from .query import looks_local_query

logger = logging.getLogger("huijian.fastpath")

# ── 动作词匹配规则（v1.5 L207-255 逐字移植；(pattern, intent, action_val)）──
_ACTION_PATTERNS: list[tuple[re.Pattern, str, Any]] = [
    # v1.0.40 修复（A1）：交替式必须**长词在前**——Python re 交替是"最左优先"而非
    # 最长匹配，旧写法 `^(开窗|打开窗|开窗户|打开窗户|…)` 对「打开窗户」先命中"打开窗"
    # → 残留"户" → 目标提取质量门不过 → 整句落空（真机实测 打开窗户/关闭窗户/开窗户/
    # 关窗户/打开窗帘/关窗帘/关闭窗帘 七个常见说法全部 None，而 T1 明明判对）。
    # 另加 `(?!帘)`：窗帘是 cover 设备，不能被窗户动作吃掉前缀（否则"打开窗帘"残留
    # "帘"、且语义也从"开窗帘"错成"开窗"）；命中不了即落到下方通用 `^(打开|…)`。
    (re.compile(r"^(打开窗户|开窗户|打开窗(?!帘)|开窗(?!帘)|窗户打开)"), "ControlWindow", "open"),
    (re.compile(r"^(关闭窗户|关窗户|关闭窗(?!帘)|关窗(?!帘)|窗户关闭)"), "ControlWindow", "close"),
    (re.compile(r"^(内倒|内导|内岛|内到|内道|内达|内打|内大|内藻)"), "ControlWindow", "A"),
    # 音乐带（2026-09-12）：后接音乐补语（播放/音乐/歌）时让位——"停止播放"
    # 是播控令不是窗帘暂停；裸"暂停/停"与"暂停窗帘"仍走窗户语义。
    (re.compile(r"^(暂停|停止|停)(?!(?:播放|音乐|歌|一?首))"), "ControlWindow", "pause"),
    (re.compile(r"^(开到|打开到|关到)\s*(\d+)"), "AdjustDeviceAttribute", {"attribute": "position", "delta": "$2"}),
    (re.compile(r"^关一半"), "AdjustDeviceAttribute", {"attribute": "position", "delta": "50"}),
    (re.compile(r"^(调到|调为|调成|温度调到|温度设到|温度设为)\s*(\d+)\s*度"), "AdjustDeviceAttribute", {"attribute": "temperature", "delta": "$2"}),
    (re.compile(r"^(调到|调为|调成|温度调到|温度设到|温度设为)\s*([零一二三四五六七八九十百]+)\s*度?"), "AdjustDeviceAttribute", {"attribute": "temperature", "delta": "cn:$2"}),
    (re.compile(r"^(亮度设到|亮度调到|调亮到|亮度)\s*(\d+)"), "AdjustDeviceAttribute", {"attribute": "brightness", "delta": "$2"}),
    (re.compile(r"^(亮度调到百分之)\s*([零一二三四五六七八九十百]+)"), "AdjustDeviceAttribute", {"attribute": "brightness", "delta": "cn:$2"}),
    (re.compile(r"^(亮度|调亮到)\s*百分之\s*([零一二三四五六七八九十百]+)"), "AdjustDeviceAttribute", {"attribute": "brightness", "delta": "cn:$2"}),
    (re.compile(r"^(调到百分之)\s*(\d+)"), "AdjustDeviceAttribute", {"attribute": "brightness", "delta": "$2"}),
    (re.compile(r"^(调到百分之)\s*([零一二三四五六七八九十百]+)"), "AdjustDeviceAttribute", {"attribute": "brightness", "delta": "cn:$2"}),
    (re.compile(r"^(亮一点|亮一些|调亮|亮些)"), "AdjustDeviceAttribute", {"attribute": "brightness", "delta": "+20"}),
    (re.compile(r"^(暗一点|暗一些|调暗|暗些)"), "AdjustDeviceAttribute", {"attribute": "brightness", "delta": "-20"}),
    (re.compile(r"^(brighter|brighten)"), "AdjustDeviceAttribute", {"attribute": "brightness", "delta": "+20"}),
    (re.compile(r"^(dimmer|dim)"), "AdjustDeviceAttribute", {"attribute": "brightness", "delta": "-20"}),
    # ── 色温调节 ──
    (re.compile(r"^(色温调到|色温|调到)\s*(\d+)\s*[kK]"), "AdjustDeviceAttribute", {"attribute": "color_temperature", "delta": "$2"}),
    (re.compile(r"^(暖光|暖色)"), "AdjustDeviceAttribute", {"attribute": "color_temperature", "delta": "2700"}),
    (re.compile(r"^(冷光|冷色)"), "AdjustDeviceAttribute", {"attribute": "color_temperature", "delta": "6500"}),
    (re.compile(r"^(白光|自然光)"), "AdjustDeviceAttribute", {"attribute": "color_temperature", "delta": "4000"}),
    (re.compile(r"^(风大一点|风大些|加大风速|风量大一点|风量加大)"), "AdjustDeviceAttribute", {"attribute": "fan_speed", "delta": "+1"}),
    (re.compile(r"^(风小一点|风小些|减小风速|风量小一点|风量减小|风量调小)"), "AdjustDeviceAttribute", {"attribute": "fan_speed", "delta": "-1"}),
    (re.compile(r"^(制热模式|制热|加热模式|加热)"), "SetDeviceMode", {"mode": "heat"}),
    (re.compile(r"^(制冷模式|制冷|冷却模式|冷却)"), "SetDeviceMode", {"mode": "cool"}),
    (re.compile(r"^(除湿模式|除湿|抽湿)"), "SetDeviceMode", {"mode": "dry"}),
    (re.compile(r"^(送风模式|送风|通风)"), "SetDeviceMode", {"mode": "fan_only"}),
    (re.compile(r"^(自动模式|自动)"), "SetDeviceMode", {"mode": "auto"}),
    (re.compile(r"^(调到|调为|调成|设为|改成|改|切换为|换为)\s*(制热|制热模式|加热|加热模式)"), "SetDeviceMode", {"mode": "heat"}),
    (re.compile(r"^(调到|调为|调成|设为|改成|改|切换为|换为)\s*(制冷|制冷模式|冷却|冷却模式)"), "SetDeviceMode", {"mode": "cool"}),
    (re.compile(r"^(调到|调为|调成|设为|改成|改|切换为|换为)\s*(除湿|除湿模式|抽湿)"), "SetDeviceMode", {"mode": "dry"}),
    (re.compile(r"^(调到|调为|调成|设为|改成|改|切换为|换为)\s*(送风|送风模式|通风)"), "SetDeviceMode", {"mode": "fan_only"}),
    (re.compile(r"^(调到|调为|调成|设为|改成|改|切换为|换为)\s*(自动|自动模式)"), "SetDeviceMode", {"mode": "auto"}),
    # ── 场景模式（v1.0.30 收编 060401/061701 语料：五拆之外的 HA preset 档；
    #    mode 直发英文规范名，集成端 set_preset_mode 通道按实体能力校验）──
    (re.compile(r"^(调到|调为|调成|设为|设成|改成|换成|切换为|切换到|切到|切为|换到|变为|进入)\s*(?:的)?(睡眠|睡觉|夜间)(?:模式|档位|挡位|档)?$"), "SetDeviceMode", {"mode": "sleep"}),
    (re.compile(r"^(调到|调为|调成|设为|设成|改成|换成|切换为|切换到|切到|切为|换到|变为|进入)\s*(?:的)?(节能|省能|省电)(?:模式|档位|挡位|档)?$"), "SetDeviceMode", {"mode": "eco"}),
    (re.compile(r"^(调到|调为|调成|设为|设成|改成|换成|切换为|切换到|切到|切为|换到|变为|进入)\s*(?:的)?(舒适)(?:模式|档位|挡位|档)?$"), "SetDeviceMode", {"mode": "comfort"}),
    (re.compile(r"^(调到|调为|调成|设为|设成|改成|换成|切换为|切换到|切到|切为|换到|变为|进入)\s*(?:的)?(静音|安静)(?:模式|档位|挡位|档)?$"), "SetDeviceMode", {"mode": "silent"}),
    (re.compile(r"^(调到|调为|调成|设为|设成|改成|换成|切换为|切换到|切到|切为|换到|变为|进入)\s*(?:的)?(强力|强劲|增强|速冷|速热)(?:模式|档位|挡位|档)?$"), "SetDeviceMode", {"mode": "boost"}),
    (re.compile(r"^(调到|调为|调成|设为|设成|改成|换成|切换为|切换到|切到|切为|换到|变为|进入)\s*(?:的)?(标准|常规)(?:模式|档位|挡位|档)?$"), "SetDeviceMode", {"mode": "normal"}),
    # 体验批 E2E 补洞（2026-09-12）：开解锁令曾整体走兜底——确认环与 NLU 脱节。
    # 必须排在通用「开」前，否则 "开锁" 被 TurnDeviceOn(name=锁) 吃掉（语义还反了）。
    (re.compile(r"^(解锁|开锁|解开锁|打开锁)"), "HassUnlock", None),
    (re.compile(r"^(上锁|锁上|落锁)"), "HassLock", None),
    (re.compile(r"^(打开|开启|开一下|开了|开)"), "TurnDeviceOn", None),
    (re.compile(r"^(关闭|关掉|关了|关一下|关)"), "TurnDeviceOff", None),
    (re.compile(r"^(open (?:the )?window)(?:\s+|$)", re.I), "ControlWindow", "open"),
    (re.compile(r"^(close (?:the )?window)(?:\s+|$)", re.I), "ControlWindow", "close"),
    (re.compile(r"^(open|turn on|switch on|power on)(?:\s+|$)", re.I), "TurnDeviceOn", None),
    (re.compile(r"^(close|turn off|switch off|power off)(?:\s+|$)", re.I), "TurnDeviceOff", None),
    # PlayMusic：模式 B 无小智播放面，保留词表但显式放行到上层（收编改造点 2）
    (re.compile(r"^(播放|放|来一首|唱)"), "PlayMusic", None),
]

# T1 类 → (intent, 附加 extra_args 种子)
_T1_MAP: dict[str, tuple[str, dict]] = {
    "TurnDeviceOn": ("TurnDeviceOn", {}),
    "TurnDeviceOff": ("TurnDeviceOff", {}),
    "OpenCover": ("TurnDeviceOn", {}),           # 窗帘开→TurnDeviceOn（域提示 cover）
    "ControlWindow": ("ControlWindow", {}),      # action 需从文本再扫描
    "AdjustBrightness": ("AdjustDeviceAttribute", {"attribute": "brightness"}),
    "AdjustTemperature": ("AdjustDeviceAttribute", {"attribute": "temperature"}),
    "SetColorTemp": ("AdjustDeviceAttribute", {"attribute": "color_temperature"}),
    "SetFanSpeed": ("AdjustDeviceAttribute", {"attribute": "fan_speed"}),
    "SetModeHeat": ("SetDeviceMode", {"mode": "heat"}),
    "SetModeCool": ("SetDeviceMode", {"mode": "cool"}),
    "SetModeDry": ("SetDeviceMode", {"mode": "dry"}),
    "SetModeFan": ("SetDeviceMode", {"mode": "fan_only"}),
    "SetModeAuto": ("SetDeviceMode", {"mode": "auto"}),
    "SceneTrigger": ("SceneTrigger", {}),
}

# T1 Adjust* 的 delta 扫描表（收编新增：原 v1.5 对 T1 命中不带 delta 的缺陷补全）
_DELTA_SCANNERS: dict[str, list[tuple[re.Pattern, Any]]] = {
    "brightness": [
        (re.compile(r"亮度\s*(?:调到|设到|为|成)?\s*(\d+)"), "$1"),
        (re.compile(r"百分之\s*([零一二三四五六七八九十百]+)"), "cn:$1"),
        (re.compile(r"(?:开到|打开到|调到|设到|设为|关到)\s*(\d+)\s*[%％]?"), "$1"),
        (re.compile(r"调到\s*(\d+)\s*%?"), "$1"),
        (re.compile(r"(亮一点|亮一些|调亮|亮些|大一点|大一些|高一点|高一些|调高)"), "+20"),
        (re.compile(r"(暗一点|暗一些|调暗|暗些|小一点|小一些|低一点|低一些|调低)"), "-20"),
    ],
    "temperature": [
        (re.compile(r"(?:调到|调为|调成|设为|设到|变成)\s*(\d+)\s*度"), "$1"),
        (re.compile(r"(\d+)\s*度"), "$1"),
        (re.compile(r"(?:调到|设为)\s*([零一二三四五六七八九十百]+)\s*度"), "cn:$1"),
        (re.compile(r"(高一点|高一些|暖一点)"), "+1"),
        (re.compile(r"(低一点|低一些|凉一点)"), "-1"),
    ],
    "fan_speed": [
        (re.compile(r"风[量速]?\s*(?:调到|设为|为|成)?\s*(\d+)"), "$1"),
        (re.compile(r"(大一点|大一些|大些|加大|调大)"), "+1"),
        (re.compile(r"(小一点|小一些|小些|减小|调小)"), "-1"),
    ],
    "color_temperature": [
        (re.compile(r"(\d+)\s*[kK]"), "$1"),
        (re.compile(r"(暖光|暖色|暖一点)"), "2700"),
        (re.compile(r"(冷光|冷色|冷一点)"), "6500"),
        (re.compile(r"(白光|自然光)"), "4000"),
    ],
    "position": [
        (re.compile(r"开到\s*(\d+)\s*[%％]?"), "$1"),
        (re.compile(r"关到\s*(\d+)"), "$1"),
        (re.compile(r"关一半"), "50"),
        (re.compile(r"打开到\s*(\d+)"), "$1"),
    ],
}

# 属性词 → 目标域映射（"卧室亮度调高一点"：亮度=属性不是设备名，转区域级 light 目标）
_ATTR_ONLY = re.compile(
    r"^\s*([\u4e00-\u9fff]{1,4}?(?:室|厅|房|间|区|馆|楼))?[的]?((?:亮度|色温|温度|风量|风速|位置|开合度))(?:调)?(?:一点|一些|点|些)?\s*$")
_ATTR_DOMAIN = {"亮度": "light", "色温": "light", "温度": "climate",
                "风量": "climate", "风速": "climate", "位置": "cover", "开合度": "cover"}

_WINDOW_ACTION_SCAN = [
    (re.compile(r"内倒|内导|内岛"), "a"),
    (re.compile(r"暂停|停止|停"), "pause"),
    (re.compile(r"关|close"), "close"),
    (re.compile(r"开|open"), "open"),
]

# ── 百分比开度预检（任何动作表扫描之前裁决）────────────────────────
# 病灶（开窗器百分比主诉）：设备词以动作词「开窗」起头，`^开窗(?!帘)` 在
# _ACTION_PATTERNS 里先于位置规则 `^(开到|打开到|关到)\d+` 命中——
# 「开窗器开到50%」被 ControlWindow(open) 截胡，百分比静默丢、按钮全开
# （假动作成功）；「打开展厅推拉窗50%」「展厅推拉窗打开50%」两种语序锚定
# 动作表根本够不到；且位置形态只认阿拉伯数字，「百分之五十/五十/一半」
# （温度/亮度均支持）此前是空洞。
# 本预检只接管「句尾显式开度数值 + 目标含窗类词」的句子；窗类走
# ControlWindow(position)——网关开窗器按钮体系（button/cover 同设备），
# 集成端按按钮→同设备 cover 下发 set_cover_position，与开/关/暂停/内倒
# 同源解析。窗帘/纱帘/纱窗/百叶=标准 cover 实体，维持既有
# AdjustDeviceAttribute 路径，本层一律不碰。
_POS_TAIL_VERBS = ("打开到|关闭到|关上到|设置到|开到|关到|调到|调为|调成|设为|设到|"
                   "设成|变成|改为|全开到|全开|打开|关闭|关上|调|设|开|关|到|为|成")
_POS_TAIL_RE = re.compile(
    rf"(?:(?P<verb>{_POS_TAIL_VERBS})\s*)?"
    r"(?P<num>百分之[零一二三四五六七八九十百]+|[0-9]{1,3}\s*[%％]|一半|"
    r"[零一二三四五六七八九十百]{1,4}|[0-9]{1,3})\s*$")
_POS_LEAD_RE = re.compile(rf"^(?:{_POS_TAIL_VERBS})\s*(?=[\u4e00-\u9fff0-9])")
_POS_CURTAIN_WORDS = ("帘", "纱窗", "百叶")


def _parse_position(num: str, had_verb: bool) -> Optional[int]:
    """句尾数值 token → 0-100 开度；None=不接管。
    无 %/百分之/一半 标记的裸数字必须有相邻动词引导（「窗户50」既不是
    百分比命令也可能是设备编号，宁可不接管）；越界拒接。永不抛。"""
    try:
        n = (num or "").strip()
        if n == "一半":
            return 50
        if n.startswith("百分之"):
            pos = int(T.cn2num(n[len("百分之"):].strip()))
            explicit = True
        elif re.fullmatch(r"[0-9]{1,3}\s*[%％]", n):
            pos = int(re.sub(r"\D", "", n))
            explicit = True
        elif re.fullmatch(r"[0-9]{1,3}", n):
            pos = int(n)
            explicit = False
        else:
            pos = int(T.cn2num(n))
            explicit = False
        if not 0 <= pos <= 100:
            return None
        if not explicit and not had_verb:
            return None
        return pos
    except (TypeError, ValueError):
        return None


def _is_window_position_target(target: str) -> bool:
    """窗类词闸：开窗器/12 窗型/裸「窗」尾缀（"3号窗"）；帘族排除。"""
    t = (target or "").strip()
    if not t:
        return False
    if any(w in t for w in _POS_CURTAIN_WORDS):
        return False
    return ("开窗器" in t or "开合器" in t or _window_type(t) is not None
            or t.rstrip("的地得").endswith("窗"))


@dataclass
class Plan:
    intent: str                                   # 直传 /api/intent/handle 的 name
    args: dict
    source: str = ""                              # klar|t0|t0_strip|t0_prefix|t0_pinyin|scene|t1
    utterance: str = ""
    trace: list[str] = field(default_factory=list)
    # ── klar 一级 NLU 专用（其余来源恒默认值，构造全兼容）──
    speech: str = ""                              # 引擎自带的中文播报（优先于话术层）
    extra_steps: list = field(default_factory=list)  # 多分句后续步骤 [{name,args}]
    # 显式全屋语义（"打开所有灯/全部灯/全屋的灯"）：目标不带区域不带名字，只留域过滤；
    # 空间化（卫星区域注入）与创建侧区域继承都必须让路，否则"所有灯"会被缩成一间屋。
    whole_house: bool = False


# ── 礼貌/口语归一（体验批 P2-16）───────────────────────────────
# 前缀迭代剥（"请帮我把…"剥完才见动作），尾缀剥语气词。只进 fast_path：
# 查询族/LLM 档看原文，避免语义动词（"能不能开"类疑问）被误剥成命令。
_POLITE_HEAD = re.compile(
    r"^(?:请问|请|麻烦|帮我|帮忙|我想|我要|我要把|你能|你可以|能不能|可不可以|"
    r"给我|替我|告诉我|说一下|报一下)[，,。!\s]*")
_POLITE_TAIL = re.compile(
    r"[，,\s]*(?:吧|呗|呢|啊|呀|哦|嘛|好吗|好么|可以吗|行吗|能不能|谢谢|多谢)[。！？!?\s]*$")


def normalize_polite(text: str) -> str:
    """"帮我把灯打开好吗" → "灯打开"。永不抛；每类最多剥三层防退化。"""
    for _ in range(3):
        new = _POLITE_HEAD.sub("", text, count=1).strip()
        if new == text:
            break
        text = new
    for _ in range(2):
        new = _POLITE_TAIL.sub("", text, count=1).strip()
        if new == text:
            break
        text = new
    return text


# ── 复合句切分（体验批 P2-12）───────────────────────────────────
# 只认强连接词（"再"必须跟在标点后，防"再见/再来"误切）。2~3 段封顶，任一段
# 独立匹配不中即整句回退单发级联（与 klar 多分句 all-or-nothing 同纪律）。
_COMPOUND = re.compile(
    r"\s*(?:然后(?:再|把)?|接着|之后|顺便(?:你)?|再帮我|并且|同时|[，,、]\s*再(?=[\u4e00-\u9fff]))\s*")


def split_compound(text: str) -> list[str]:
    """返回 []（非复合/超界/任一段过短）或 2~3 个分句。"""
    if len(text) < 6:
        return []
    parts = [p.strip(" 。！？!?，,、") for p in _COMPOUND.split(text)]
    parts = [p for p in parts if p]
    if len(parts) < 2 or len(parts) > 3:
        return []
    if any(len(p) < 2 for p in parts):
        return []
    if parts == [text]:
        return []
    return parts


# 指代词（体验批 P2-10）：目标位是代词 = 明示「沿用上一轮目标」，交由 pipeline
# 上下文注入；本层视作空目标产出全屋形态（保守回退：无上下文可继承时行为同旧）。
_PRONOUNS = frozenset({"它", "他们", "它们", "她们", "这个", "那个", "这块", "那块", "他", "她"})

# 句首回指副词（"再亮一点"/"还是关掉"）：剥后置标记，目标继承交给 pipeline
_ANAPHORA_HEAD = re.compile(r"^(?:还是|还要|再|又|继续)(?=[\u4e00-\u9fff])")
# SOV 语序锁令："X开锁/X解锁/X上锁/X锁上" → 动作前置（X 为 2-8 字目标词）
_LOCK_INV = re.compile(r"^(.{2,8}?)(开锁|解锁|上锁|锁上)(?:了|啦|咯)?$")


def is_pronoun(text: str) -> bool:
    return (text or "").strip().rstrip("。！!？?") in _PRONOUNS


# "它/那个"[也都又还][动作][了吧呢]（t1 rest 常带全文动作词）：仍是代词目标，
# 不是设备名。演示实锤形态："它也关了" 曾被吃成 name（2026-09-12 真件回归）。
_PRON_ACT_TAIL = re.compile(
    r"^(?:它|他|她|它们|他们|这个|那个|这块|那块)"
    r"(?:也|都|又|还|就|全部?)*"
    r"(?:打开来|打开|关掉|关闭|开了|关了|开一下|关一下|起来|停|掉|开|关|上|下)?"
    r"(?:了吧|了|吧|呢|的|呀)*$")
# 残句里的复合连接词残留（split_compound 未切/链被否时防单发错配）
_COMPOUND_RESIDUE = re.compile(r"然后|接着|之后|顺便|并且|同时|再帮我")


def _extract_text(raw: Any) -> str:
    """HA STT 文本偶带 {"content":…} 包裹（v1.5 L352-361 原语义）。"""
    if not isinstance(raw, str):
        return str(raw) if raw else ""
    try:
        if raw.strip().startswith("{"):
            d = json.loads(raw)
            if "content" in d:
                return str(d["content"])
    except Exception:
        pass
    return raw


def _is_complex_query(text: str) -> bool:
    """创建/修改/查询类走上层（v1.5 L256-275 逐字）。"多少度/几度" 放行给查询族。"""
    t = text.lower()
    if re.search(r"(创建|自动(化|场景)|修改|删除|添加|配对的?)", t):
        return True
    # v1.0.49（Q2）：本地可答的量纲问句先放行给查询族（"温度多少/电量多少/
    # 是否有人"）——下面的「多少|几 → 上层」和「是不是|有没有 → 上层」两条
    # 粗闸会把它们全部截走，无 LLM 现场即哑。未命中查询族自然回落原链。
    if looks_local_query(t):
        return False
    if re.search(r"(状态|情况|哪些|列表)", t):
        return True
    if re.search(r"所有.*(?:灯|设备|开关)", t):
        # v1.0.41（F13）：「所有…」分支原先不分位置——「把所有灯都关掉」这类
        # 全屋祈使句被当查询交上层，无 LLM 的用户只剩兜底话术（真机主诉）。
        # 收紧：句尾落控制动词（_WH_TRAIL_RE，$ 锚定）且**无疑问标记**才算祈使
        # 放行（全屋分支在守卫之前已裁决，此处同口径兜底）；疑问标记一票否决。
        # 裁决：豁免只认疑问标记（什么|多少|几|吗|呢|怎么|？），「都/啦/了」
        # 不豁免——「所有灯都关啦」是命令不是问句。放行条件与 _wholehouse_plan
        # 尾动分支同判据（**句首即全屋标记**）：「客厅所有灯都打开」是区域句，
        # 守卫照旧拦——放行会让它在真模型下被 T1 接成全屋，误开全家灯。
        if _WH_TRAIL_RE.search(t) and _WH_HEAD_RE.match(t) and not re.search(
                r"(什么|多少|几|吗|呢|怎么|[?？])", t):
            pass
        else:
            return True
    if re.search(r"(为什么|怎么|如何|是不是|有没有|能否|可以.*吗)", t):
        return True
    if re.match(r"^(why|what|how|which|who|can you|could you)\b", t):
        return True
    if re.search(r"(多少度|几度)", t):
        return False
    if re.search(r"(多少|几)", t):
        return True
    # 场景"保存/存为/命名为"类：M0 显式不接管（M1 快照型场景实现）
    if re.search(r"(保存|存为|命名为)", t):
        return True
    return False


def _parse_action_value(aval: Any, match: re.Match) -> dict:
    """$n 捕获替换 / cn: 中文数字 / dict 字面量（v1.5 L170-186 逐字语义）。"""
    extra: dict[str, Any] = {}
    if isinstance(aval, dict):
        for k, v in aval.items():
            if isinstance(v, str) and v.startswith("cn:"):
                g_str = v[3:].lstrip("$")
                cn = match.group(int(g_str)) if g_str.isdigit() and match.lastindex and int(g_str) <= match.lastindex else ""
                extra[k] = T.cn2num(cn)
            elif isinstance(v, str) and v.startswith("$"):
                idx = int(v[1:])
                extra[k] = match.group(idx) if match.lastindex and idx <= match.lastindex else v
            else:
                extra[k] = v
    elif aval is not None:
        extra["action"] = aval
    return extra


def _scan_delta(text: str, attribute: str) -> Optional[tuple[str, str]]:
    """返回 (delta 值, 命中原片段)——片段供 target 提取前剔除，防数值尾巴污染设备名。"""
    for pat, val in _DELTA_SCANNERS.get(attribute, []):
        m = pat.search(text)
        if m:
            if val.startswith("cn:"):
                g = val[3:]
                return T.cn2num(m.group(int(g[1:])) if g[1:].isdigit() else ""), m.group(0)
            if val.startswith("$"):
                return m.group(int(val[1:])), m.group(0)
            return val, m.group(0)
    return None


def _scan_window_action(text: str) -> Optional[str]:
    for pat, act in _WINDOW_ACTION_SCAN:
        if pat.search(text):
            return act
    return None


# v1.0.40 修复（D3）：T1 的标签集里只有 OpenCover、**没有 CloseCover**（见 _T1_MAP），
# 于是"拉上/合上/收起窗帘"这类关闭向说法被模型判成 OpenCover(0.86~0.97) 后原样
# 映射 TurnDeviceOn → **反向执行**（用户要关、设备去开）。按方向词就地纠正：
# 出现关闭向词且无开启向词 → 翻成 TurnDeviceOff。
# v1.0.41 修复（F12）：真模型实测「拉下窗帘」0.959/「窗帘拉下来」0.858/「闭合窗帘」
# 0.937 全部过阈值却不在词表 → 原样执行成"开帘"（方向反转，用户可感最重残留洞）。
# 补入 拉下/拉下来/闭合/拉严；顺带去重 open 表里误写的两个「升起」。
_COVER_CLOSE_WORDS = ("拉上", "合上", "收起", "收拢", "放下", "降下", "关闭", "关上",
                      "关掉", "关", "拉下", "拉下来", "闭合", "拉严")
_COVER_OPEN_WORDS = ("打开", "拉开", "开启", "开一下", "升起", "抬", "开")


def _cover_intent(text: str, intent: str) -> str:
    """T1 命中 OpenCover 后的方向纠正；非 TurnDeviceOn 原样返回。永不抛。"""
    try:
        if intent != "TurnDeviceOn" or not text:
            return intent
        if any(w in text for w in _COVER_CLOSE_WORDS) and not any(
                w in text for w in _COVER_OPEN_WORDS):
            return "TurnDeviceOff"
    except Exception:
        return intent
    return intent


# ── v1.0.41（F11）：帘/窗语序归一 ─────────────────────────────────
# SOV「(把)客厅窗帘拉上」「窗帘拉上」「卧室窗户关上」与 SVO「拉上客厅窗帘」
# 「关上窗户」——原动作表只有动词前置引导词（^打开/^关闭…），这些形态 T0 全部
# 落空（裸「拉上窗帘」只靠 T1 方向纠正侥幸救回，无资产/关闭 T1 即真落空）。
# 就地归一成「关闭客厅窗帘」标准动形，复用既有目标提取、区域扫描、窗型纠正与
# cover 域推断：窗帘/纱帘→Turn*+domains=[cover]；窗户→「关闭卧室窗户」既有
# 链路→窗型纠正 ControlWindow。**不收裸 关/开**（「关一下客厅窗帘」的裸关会与
# 后续杂字误拼）；等值判定前置，场景触发词不受改写影响。
_COVER_V_CLOSE = "拉上|合上|闭合|收起|收拢|拉下来|拉下|降下|关上|关闭|关了"
_COVER_V_OPEN = "拉开|打开|开了"
_COVER_HEAD = r"[\u4e00-\u9fff]{0,6}?(?:窗帘|纱帘|窗户)"
_COVER_SOV_RE = re.compile(
    rf"^(?P<head>{_COVER_HEAD})(?:(?P<close>{_COVER_V_CLOSE})"
    rf"|(?P<open>{_COVER_V_OPEN}))(?:了|啦)?$")
_COVER_SVO_RE = re.compile(
    rf"^(?:(?P<close>{_COVER_V_CLOSE})|(?P<open>{_COVER_V_OPEN}))(?:了|啦)?"
    rf"(?P<head>{_COVER_HEAD})$")


def _cover_wordorder(text: str) -> Optional[str]:
    """SOV/SVO 帘窗句 → 「关闭/打开＋目标」标准形；不匹配或已是标准形→None。永不抛。"""
    try:
        for m in (_COVER_SOV_RE.match(text), _COVER_SVO_RE.match(text)):
            if not m:
                continue
            cand = ("打开" if m.group("open") else "关闭") + m.group("head")
            if cand != text:
                return cand
    except Exception:
        return None
    return None


class FastPath:
    def __init__(self, scenes, textcnn, settings):
        self.scenes = scenes
        self.textcnn = textcnn
        self.settings = settings

    def _wholehouse_plan(self, text: str, trace: list[str]) -> Optional[Plan]:
        """显式全屋动作句（动词在前："打开所有灯/关掉全部窗帘"）→ Plan。
        必须在复杂查询守卫**之前**裁决：守卫的 `所有.*(?:灯|设备|开关)` 分支会把
        「打开所有灯」误判成查询句交上层（2026-09-15 实测），而它是命令。
        认不出域的口径（"所有设备"）返回 None——不冒然全屋全动（会带上门锁）。"""
        if not _WHOLEHOUSE_RE.search(text or ""):
            return None
        for pattern, intent_type, _v in _ACTION_PATTERNS:
            if intent_type not in ("TurnDeviceOn", "TurnDeviceOff"):
                continue
            m = pattern.match(text)
            if not m:
                continue
            rest = text[m.end():].strip()
            # v1.0.41 审查 S12：动词后的**残句必须以全屋标记起头**——
            # 「打开客厅所有灯」这类动词+区域+全屋混合句是**区域句**（客厅的灯），
            # 旧实现剥标记后拿 domain_hint 直产 whole_house，把「客厅」静默吞掉
            # = 扩大作用域开全家灯（真机可感，比落空更糟）。与尾动分支的
            # _WH_HEAD_RE 判据同口径：区域句交回上层（T1/LLM 能接就接，
            # 接不住也只是不执行，绝不冒然全屋）。
            if rest and not _WH_HEAD_RE.match(rest):
                continue
            word = _wholehouse_word(rest or text)
            doms = [str(d) for d in (T.domain_hint(word) if word else [])]
            if not doms:
                return None
            trace.append(f"全屋显式:{word}→domains={doms}")
            return Plan(intent=intent_type,
                        args={"target": [{"devices": [{"name": "", "domains": doms}]}]},
                        source="t0", utterance=text, trace=trace, whole_house=True)
        # v1.0.41（F13）：动词在尾的全屋命令（「所有灯打开」「家里的灯全部关掉」
        # 「帮我把所有灯都关掉」剥把字后=「所有灯都关掉」）——旧表只认动词前置，
        # 这类真机高频形态只能靠 T1/LLM。同判据同产物（认不出域不冒然全屋全动）。
        # 只接**句首即全屋标记**的形态：「客厅所有灯都打开」是区域句不是全屋句，
        # 误产 whole_house 会打开全家灯——静默做错比落空更糟，交回上层。
        m_tw = _WH_TRAIL_RE.search(text)
        if m_tw and _WH_HEAD_RE.match(text):
            intent_type = ("TurnDeviceOff" if m_tw.group(1)[0] == "关"
                           else "TurnDeviceOn")
            word = _wholehouse_word(text[:m_tw.start()].strip() or text)
            doms = [str(d) for d in (T.domain_hint(word) if word else [])]
            if not doms:
                return None
            trace.append(f"全屋尾动:{word}→domains={doms}")
            return Plan(intent=intent_type,
                        args={"target": [{"devices": [{"name": "", "domains": doms}]}]},
                        source="t0", utterance=text, trace=trace, whole_house=True)
        return None

    # ── v1.0.42 家电族：扫地机/吸尘器/拖地机 专有动作层 ─────────────────
    # 集成侧 TurnDeviceOn/Off 已有 vacuum 映射（on→vacuum.start 开扫、
    # off→vacuum.return_to_base 回充），本层只把字面表不认的形态（启动/
    # 开始清扫/暂停/回充，含设备词前置的 SOV 语序）收束到既有意图；
    # 「打开/关闭扫地机器人」等标准形不带专有词，照旧走原表零扰动。
    _VAC_DEV_RE = re.compile(r"(?:扫地机器人|扫拖机器人|扫地机|吸尘器|拖地机)")
    _VAC_START = re.compile(r"(启动|开始|清扫|打扫|扫地|吸尘|拖地|工作|出发|出动)")
    _VAC_RETURN = re.compile(r"(回充|回巢|回去|回来|回家|结束|收工|充电)")
    _VAC_PAUSE = re.compile(r"(暂停|停一下|先停|停止)")
    # pre 侧区域提取前的动词/介词残渣清除（「让客厅的扫地机器人…」pre=「让客厅的」→「客厅」）
    _VAC_PRE_CLEAN = re.compile(r"(让|把|给|帮我把|帮我|请|的|去|马上|现在|立即|一下|吧|"
                                r"启动|开始|暂停|停一下|先停|停止|回充|回巢|回去|回来|回家|"
                                r"结束|收工|充电|清扫|打扫|扫地|吸尘|拖地|工作)")
    # 无设备词的裸令（"开始扫地/去打扫"）→ 通用名「扫地机器人」+ domains=[vacuum]，
    # 集成侧单台 vacuum 按域即中。
    _VAC_BARE = re.compile(r"^(?:开始|去|马上|现在|立即)?(?:扫地|吸尘|拖地|打扫)(?:一下|吧|了)?$")
    _VAC_NOGO = re.compile(r"[吗呢？?]|什么|多少|几|怎|哪|状态|怎样|如何|是不是|有没有|"
                           r"[不别勿莫没]")

    def _vacuum_plan(self, text: str, trace: list) -> Optional[Plan]:
        if self._VAC_NOGO.search(text):        # 疑问/否定绝不冒动设备
            return None
        m = self._VAC_DEV_RE.search(text)
        if m:
            pre, post = text[:m.start()], text[m.end():]
            # 动作词在设备短语**两侧合找**，设备词本身不参与——「关闭扫地机器人」
            # 的"扫地"二字藏在设备名里，整句检测会把关闭误判成开扫（意图反转）。
            rest = pre + post
            name = m.group(0)
            area = None
            if pre:
                # 剥动词/介词残渣后走 _area_of_prefix（parse_target 对裸区域名
                # 回 area=None——「让客厅的扫地机器人开始」pre 段只剩「客厅」）。
                clean = self._VAC_PRE_CLEAN.sub("", pre).strip()
                if clean and len(clean) <= 6:
                    area = T._area_of_prefix(clean)
        elif self._VAC_BARE.match(text):
            rest, name, area = "扫地", "扫地机器人", None
        else:
            return None
        if self._VAC_PAUSE.search(rest):
            intent, tag = "PauseDevice", "家电暂停"
        elif self._VAC_RETURN.search(rest):
            intent, tag = "TurnDeviceOff", "家电回充→TurnDeviceOff"
        elif self._VAC_START.search(rest):
            intent, tag = "TurnDeviceOn", "家电清扫→TurnDeviceOn"
        else:
            return None                        # 打开/关闭等标准形交回原表
        entry: dict = {"devices": [{"name": name, "domains": ["vacuum"]}]}
        if area:
            entry["area"] = area
        trace.append(f"{tag}:{area or ''}{name}")
        return Plan(intent=intent, args={"target": [entry]}, source="t0",
                    utterance=text, trace=trace)

    def _position_plan(self, text: str, trace: list) -> Optional[Plan]:
        """百分比开度句 → ControlWindow(position)。永不抛；门不齐返回 None
        交回原动作表，无数字/帘族/非窗设备的句子行为与改动前完全一致。"""
        try:
            m = _POS_TAIL_RE.search(text)
            if not m:
                return None
            verb = m.group("verb") or ""
            pos = _parse_position(m.group("num"), bool(verb))
            if pos is None:
                return None
            head = text[:m.start()].strip().strip(" 的地得了吧啦，,")
            # 「打开展厅推拉窗50%」引导动词形：剥掉后再验窗类词（剥完不含
            # 窗词就不剥，保守回原表）
            lm = _POS_LEAD_RE.match(head)
            if lm and _is_window_position_target(head[lm.end():]):
                head = head[lm.end():].strip()
            if not _is_window_position_target(head):
                return None
            trace.append(f"百分比开度:{head or '全屋窗'}→{pos}%")
            return self._build_plan("ControlWindow", head, {"position": pos},
                                    text, "t0", trace)
        except Exception:  # noqa: BLE001
            logger.exception("[fastpath] 百分比开度预检异常（视为不接管）")
            return None

    # ── 主入口 ──────────────────────────────────────────────────
    async def match(self, raw_text: str) -> Optional[Plan]:
        trace: list[str] = []
        text = _extract_text(raw_text)
        text = corrector.apply(text, self.settings.get("nlu.corrections_extra") or {})
        if not text or len(text.strip()) < 2:
            return None
        text = text.strip()
        text = re.sub(r"^[把将]\s*", "", text)   # 处置介词核心化："把灯打开"→"灯打开"
        # 体验批 P2-16：礼貌语归一（迭代剥，可能再次暴露 把/将）
        polite = normalize_polite(text)
        polite = re.sub(r"^[把将]\s*", "", polite).strip()
        if polite != text:
            trace.append(f"礼貌→{polite}")
            text = polite
        # 体验批 P2-10：句首回指副词（"再亮一点"→"亮一点"；目标由上下文注入）
        m_an = _ANAPHORA_HEAD.match(text)
        if m_an:
            stripped = text[m_an.end():].strip()
            if len(stripped) >= 2:
                trace.append(f"回指→{stripped}")
                text = stripped
        if not text or len(text.strip()) < 2:
            return None
        trace.append(f"纠错→{text}")
        # 场景缓存：稳态后台刷新零等待；仅冷启动未加载过时同步兜一次（P0-2）
        if self.scenes.needs_blocking():
            await self.scenes.refresh()
        else:
            self.scenes.refresh_soon()

        # 场景契约恒最高优先（模块头裁决①）：等值触发词判定必须在**一切闸之前**，
        # 尤其先于 _wholehouse_plan。2026-09-10 跨版本 sweep 实锤：触发词写成「打开
        # 所有灯」这类含"所有/全部"字样的，会被全屋分支先做成"开两盏灯"并播报成功
        # ——静默做错动作，比 fallback 更糟（用户被明确告知"以后说 X 就 Y"）。
        # 只认等值（同复杂查询分支纪律）：防短触发词「开灯」吞掉「开灯亮度50」。
        phrase = self.scenes.check(text)
        if phrase and phrase == text:
            return await self._scene_plan(phrase, text, trace)

        # 显式全屋命令先于复杂查询守卫裁决（守卫会吞掉"打开所有灯"，见方法注释）
        wh = self._wholehouse_plan(text, trace)
        if wh is not None:
            return wh
        if _is_complex_query(text):
            trace.append("复杂查询守卫→交上层")
            # 等值触发词已在最前面裁决过（同 text 同 check），此处不再重复判定
            return self._miss(trace)

        # v1.0.41（F11）：帘窗 SOV/SVO 语序归一（「客厅窗帘拉上」「拉上客厅窗帘」
        # 「卧室窗户关上了」）。放在两道等值判定之间：触发词写成倒装形已在 L481
        # 等值裁决；写成标准形而用户说倒装形的，由下方第二道等值（锁具归一后那
        # 道）接住。裸 关/开 不在改写词表——「关一下客厅窗帘」「睡觉关窗帘」等
        # 既有形态一律不碰。
        cov = _cover_wordorder(text)
        if cov:
            trace.append(f"帘窗语序→{cov}")
            text = cov

        # 体验批 E2E 补洞：SOV 语序锁令「大门开锁/把门锁上(门+锁上)」动作前置归一。
        # 否定/疑问字（没不别谁哪）不参与——「还没上锁」是陈述不是命令。
        m_lv = _LOCK_INV.match(text)
        if m_lv and not re.search(r"[没不别谁哪怎]", m_lv.group(1)):
            head = m_lv.group(1)
            mapped = ("解锁" if m_lv.group(2) in ("开锁", "解锁") else "锁上") + head
            trace.append(f"语序→{mapped}")
            text = mapped

        # 场景契约第二道等值判定（缓存就绪已在最前做过）：上一步 SOV 语序归一会改写
        # text，触发词若写作锁令倒装形（「大门开锁」）只有归一后才与缓存等值。
        # 「打开空调」这类"看着像设备指令"的触发词同样由此拦下：不先判场景就会被
        # 下面 ① 的 ^打开 吃掉、再被空调区域守卫判 miss，整句落兜底（2026-09-10
        # 真机：创建成功、复述触发词却 fallback）；能构造成功的形态更糟——会去开关
        # 那台设备，场景永不触发。**只认等值**：防短触发词（"开灯"）吞掉「开灯亮度50」。
        phrase = self.scenes.check(text)
        if phrase and phrase == text:
            return await self._scene_plan(phrase, text, trace)

        # 连排句绝不在单发通路里执行（2026-09-10 真机实锤）：无连接词的动词连排
        # （"关闭办公室射灯关闭办公室平开窗"）必须由 pipeline 链发切分逐段执行；
        # 走单发会被 T0 当成一句——轻则第一子句被吃成区域残渣（"办公室射灯关闭
        # 办公室"）只动最后一个设备，重则前半句执行、后半句**静默丢掉**。链发那
        # 边任一段听不懂会拒绝，这里同样拒（交上层），宁可如实说没听懂。
        if creation.serial_clauses(text):
            return self._miss(trace, "连排句→交链发/上层")

        # v1.0.42 家电族：两道场景等值与连排闸之后、动作表扫描之前——
        # 「暂停扫地机器人」必须先于此层的暂停(窗户)表项，SOV 形「扫地机器人
        # 开始清扫」也是动作表够不到的语序。
        vp = self._vacuum_plan(text, trace)
        if vp is not None:
            return vp

        # v1.0.4x 百分比开度预检：必须在 ① 动作表之前——「开窗器」以动作词
        # 「开窗」起头，① 的 ^开窗(?!帘) 会先截胡丢数值（见 _POS_TAIL_RE 注释）。
        pp = self._position_plan(text, trace)
        if pp is not None:
            return pp

        matched_intent: Optional[str] = None
        extra_args: dict[str, Any] = {}
        rest_text = text
        source = "t0"

        # ① 正则匹配动作前缀
        for pattern, intent_type, action_val in _ACTION_PATTERNS:
            m = pattern.match(text)
            if m:
                matched_intent = intent_type
                rest_text = text[m.end():].strip()
                extra_args = _parse_action_value(action_val, m)
                break
        # ② 未命中 → 已知设备名前缀剥离重试（"空调风量大一点"→"风量大一点"）
        if not matched_intent:
            for kd in sorted(T.KNOWN_DEVICES_PREFIX, key=len, reverse=True):
                if text.startswith(kd) and len(text) > len(kd):
                    rest = text[len(kd):].strip()
                    for pattern, intent_type, action_val in _ACTION_PATTERNS:
                        m = pattern.match(rest)
                        if m:
                            matched_intent, source = intent_type, "t0_strip"
                            rest_text = kd
                            extra_args = _parse_action_value(action_val, m)
                            break
                    if matched_intent:
                        break
        # ③ 仍未命中 → 区域前缀扫描 + 动作后缀匹配
        if not matched_intent and text and "\u4e00" <= text[0] <= "\u9fff":
            prefix, suffix = T.extract_prefix(text)
            if prefix and suffix:
                suffix = suffix.lstrip("的")   # "客厅的窗帘关一半"：前缀扫描容忍属格「的」
                for pattern, intent_type, action_val in _ACTION_PATTERNS:
                    m = pattern.match(suffix)
                    if m:
                        matched_intent, source = intent_type, "t0_prefix"
                        rest_text = prefix
                        extra_args = _parse_action_value(action_val, m)
                        break
                if not matched_intent:
                    for kd in sorted(T.KNOWN_DEVICES_PREFIX, key=len, reverse=True):
                        if suffix.startswith(kd) and len(suffix) > len(kd):
                            rest = suffix[len(kd):].strip()
                            for pattern, intent_type, action_val in _ACTION_PATTERNS:
                                m = pattern.match(rest)
                                if m:
                                    matched_intent, source = intent_type, "t0_prefix_strip"
                                    rest_text = prefix + kd
                                    extra_args = _parse_action_value(action_val, m)
                                    break
                            if matched_intent:
                                break
        # ③b 前缀扫描二次分裂：suffix 以设备单字开头（"卧室|灯开到50%"）
        #     （移植期新增：原 v1.5 未覆盖「区域+裸设备字+动作」形态）
        if not matched_intent and text and "\u4e00" <= text[0] <= "\u9fff":
            prefix, suffix = T.extract_prefix(text)
            if prefix and suffix and suffix[0] in "灯窗扇机":
                dev, rest2 = suffix[0], suffix[1:]
                for pattern, intent_type, action_val in _ACTION_PATTERNS:
                    m = pattern.match(rest2)
                    if m:
                        matched_intent, source = intent_type, "t0_prefix_dev"
                        rest_text = prefix + dev
                        extra_args = _parse_action_value(action_val, m)
                        break
        # ④ 场景触发词（缓存）
        if not matched_intent:
            phrase = self.scenes.check(text)
            if phrase:
                return await self._scene_plan(phrase, text, trace)
        # ⑤ T1 TextCNN（仅一次；OOS/阈值不足返回 None）
        if not matched_intent and self.settings.get("nlu.textcnn_enabled", True) and self.textcnn:
            # F3：ort 推理与会话构建（首调用 ~百ms、预热持锁时更久）必须出事件
            # 循环——原在循环内直调，冻结三通道所有 WS 帧处理（审查 F3）。
            hit = await asyncio.get_running_loop().run_in_executor(None, self.textcnn.predict, text)
            if hit:
                label, prob = hit
                trace.append(f"T1={label}({prob:.2f})")
                intent, seed = _T1_MAP[label]
                if intent == "SceneTrigger":
                    return await self._scene_plan(text, text, trace)
                if label == "OpenCover":
                    # v1.0.40（D3）：标签集无 CloseCover → 按方向词纠正，防"拉上窗帘"被反向执行
                    fixed = _cover_intent(text, intent)
                    if fixed != intent:
                        trace.append(f"T1方向纠正:{label}→{fixed}")
                        intent = fixed
                matched_intent = intent
                extra_args = dict(seed)
                rest_text = text
                source = "t1"
                # T1 命中补参：动作/数值扫描 + 前缀拆分（v1.5 L462-479 语义扩展）
                if intent == "ControlWindow" and "action" not in extra_args:
                    act = _scan_window_action(text)
                    if act:
                        extra_args["action"] = act
                    else:
                        return self._miss(trace, "ControlWindow 无动作词")
                if intent == "AdjustDeviceAttribute":
                    scanned = _scan_delta(text, extra_args.get("attribute", ""))
                    if scanned is None:
                        return self._miss(trace, "Adjust 无可用数值")
                    delta, matched = scanned
                    extra_args["delta"] = delta
                    rest_text = text.replace(matched, "").strip()
                if intent in ("TurnDeviceOn", "TurnDeviceOff") and text and "\u4e00" <= text[0] <= "\u9fff":
                    prefix, suffix = T.extract_prefix(text)
                    if prefix and suffix:
                        for pattern, it2, av2 in _ACTION_PATTERNS:
                            if pattern.match(suffix):
                                rest_text = prefix
                                break

        if not matched_intent:
            return self._miss(trace, "无匹配动作")
        if matched_intent == "PlayMusic":
            return self._miss(trace, "PlayMusic 不接管(模式B)")

        trace.append(f"{source}:{matched_intent} rest={rest_text!r} extra={extra_args}")
        plan = self._build_plan(matched_intent, rest_text, extra_args, text, source, trace)
        # v1.0.40 修复（A1 后半）：T0 命中了动作、但**目标提取质量不足**时，旧实现直接落空
        # 进兜底话术——而 T1 往往判得对（实测「打开窗户」旧正则残留「户」时 T1 判
        # ControlWindow 0.9+）。给 T1 一次接管机会。
        # 但这道兜底**只许对"提取质量低"生效**：T0 的其它 miss 是刻意的语义拒绝
        # （音乐泛词交音乐带、PlayMusic 不接管、复合残句拒猜、ControlWindow 无动作词…），
        # 让 T1 接管会把"故意不接"变成"乱接"——CI 实测反例：`关掉音乐` 被兜底接成
        # TurnDeviceOff 且设备名是整句残渣（test_music_generic_word_not_device 红）。
        if (plan is None and source != "t1" and trace
                and trace[-1].startswith("miss:提取质量低")
                and self.settings.get("nlu.textcnn_enabled", True) and self.textcnn):
            # v1.0.40 修复（A1 后半）：T0 命中了动作、但**目标提取落空**（残留字/质量门/
            # 提取低分）时，旧实现直接落空进兜底话术——而 T1 往往判得对（实测「打开窗户」
            # 旧正则残留「户」时 T1 判 ControlWindow 0.9+、「打开窗帘」判 OpenCover）。
            # 给 T1 一次接管机会；仍失败则维持原 None 语义（继续 klar/查询族/LLM/兜底）。
            hit2 = await asyncio.get_running_loop().run_in_executor(
                None, self.textcnn.predict, text)
            if hit2:
                label2, prob2 = hit2
                t1_intent, seed2 = _T1_MAP.get(label2, (None, {}))
                if t1_intent and t1_intent != "SceneTrigger":
                    if label2 == "OpenCover":
                        t1_intent = _cover_intent(text, t1_intent)
                    retry_extra = dict(seed2)
                    if t1_intent == "ControlWindow" and "action" not in retry_extra:
                        act2 = _scan_window_action(text)
                        if act2:
                            retry_extra["action"] = act2
                    if t1_intent == "AdjustDeviceAttribute":
                        scanned2 = _scan_delta(text, retry_extra.get("attribute", ""))
                        if scanned2:
                            retry_extra["delta"] = scanned2[0]
                    trace.append(f"T1-retry={label2}({prob2:.2f})")
                    retry_plan = self._build_plan(t1_intent, text, retry_extra, text,
                                                  "t1", trace)
                    if retry_plan is not None:
                        plan = retry_plan
        if plan is None:
            # 构造失败（守卫/提取质量低/复合残余）→ 触发词兜底：前缀形态仍算用户
            # 说了触发词（"打开空调吧"被 ① 吃成 rest="空调吧" 后判 miss）。命中才
            # 接管，未命中维持原 None 语义（继续走 klar/查询族/LLM/兜底）。
            phrase = self.scenes.check(text)
            if phrase:
                return await self._scene_plan(phrase, text, trace)
        return plan

    # ── 场景与参数组装 ──────────────────────────────────────────
    async def _scene_plan(self, phrase: str, text: str, trace: list[str]) -> Optional[Plan]:
        if not await self.scenes.verify_or_refresh(phrase):
            return self._miss(trace, "场景触发未缓存")
        trace.append(f"场景触发={phrase}")
        return Plan(intent="HassTriggerVoiceScene", args={"trigger_phrase": phrase},
                    source="scene", utterance=text, trace=trace)

    def _build_plan(self, intent: str, rest_text: str, extra: dict, text: str,
                    source: str, trace: list[str]) -> Optional[Plan]:
        """args 构造（v1.5 L529-712 的 MCP-free 版）。"""
        # 收编修复五（移植期新发现的原缺陷）：delta/语气残渣（"亮度调到60%"→"%"、
        # "亮度调暗一点" 去 "暗一点" 后→"亮度调"）不构成设备目标，归零为全屋调节。
        rest_text = (rest_text or "").strip()
        # 体验批 P2-10：代词目标 → 空目标形态（pipeline._apply_context 注入上一轮
        # 目标；无上下文时保守回落全屋，行为不劣于旧版把代词当设备名查必 miss）
        # 复合代词形态："把它关掉" 归一后 rest="它关掉"——代词+动作尾巴，同判。
        if rest_text and (is_pronoun(rest_text) or _PRON_ACT_TAIL.match(rest_text)):
            trace.append(f"代词目标:{rest_text}→待上下文注入")
            rest_text = ""
        # 体验批 P2-12：链被否决后的复合残句（含"然后/接着"等）绝不猜单目标——
        # 实测会把「打开客厅的灯，然后再关闭窗帘」错配成客厅窗帘。拒了交回上层。
        if rest_text and _COMPOUND_RESIDUE.search(rest_text):
            trace.append("复合残余→拒猜目标")
            return None
        residue = re.sub(r"[调一些点把将了%％到亮暗度色温风量速为成设]", "", rest_text)
        if not residue.strip():
            rest_text = ""
        # Adjust + 「区域?+属性词」：目标=该区域属性域，属性词不吃成设备名
        if intent == "AdjustDeviceAttribute" and rest_text and (mm := _ATTR_ONLY.match(rest_text)):
            area, attr_word = mm.group(1) or "", mm.group(2)
            args = {"attribute": extra.get("attribute", ""), "delta": str(extra.get("delta", ""))}
            dom = _ATTR_DOMAIN.get(attr_word, "light")
            args["target"] = ([{"area": area}] if area else []) or [{"devices": [{"domains": [dom]}]}]
            if area:
                args["target"] = [{"area": area, "devices": [{"domains": [dom]}]}]
            trace.append(f"属性词快捷:{area or '全屋'}+{attr_word}→{dom}")
            return Plan(intent=intent, args=args, source=source, utterance=text, trace=trace)
        # 温度调节且无设备名 → HassClimateSetTemperature 改道（原 L695-701 保留）
        if (extra.get("attribute") == "temperature" and extra.get("delta")
                and not rest_text.strip()):
            return Plan(intent="HassClimateSetTemperature",
                        args={"temperature": _to_int(extra["delta"]), "area": ""},
                        source=source, utterance=text, trace=trace + ["温度改道 ClimateSetTemperature"])

        _was_on = intent == "TurnDeviceOn"
        area, name, score = T.parse_target(rest_text, action_match=_any_action_match)
        # 连排残渣守卫（2026-09-10 真机）：链发没接住的连排句会被 T0 当成**一句**，
        # parse_target 把第一个动作子句整段吃成"区域名"（"办公室射灯关闭办公室"）。
        # 区域名里出现动作动词＝这句其实是两句，绝不能拿残渣区域去执行——真机里
        # 它把平开窗关了、射灯没关还报"成功"（静默做错远糟于如实拒收）。
        if area and _AREA_VERB_RESIDUE.search(str(area)):
            return self._miss(trace, f"区域残渣(连排未切分):{area}")
        # 窗型词落进设备名 → 按钮按压语义，不是开关设备（实机 2026-09-08：
        # 「打开 办公室平开窗」被错产成 TurnDeviceOn，集成按开关找窗必 miss）。
        if intent in ("TurnDeviceOn", "TurnDeviceOff") and name:
            wt = _window_type(name)
            if not wt and _window_type(rest_text):
                # parse_target 把窗型词切碎（「推拉门」的推/拉被当残留动词
                # → name 只剩「门」）；rest 尾部找回完整窗名顶替。
                name = wt = _window_type(rest_text)
            if wt:
                trace.append(f"窗型纠正:{name}→ControlWindow")
                intent = "ControlWindow"
                extra = {**extra, "action": extra.get("action") or
                         ("open" if _was_on else "close")}
        if name is None:
            # 全局类："开灯/关灯"（rest 为空但设备词在原文里）
            if intent in ("TurnDeviceOn", "TurnDeviceOff") and rest_text.strip():
                area, name = None, T.normalize_name(T.strip_modal(rest_text))
            elif not rest_text.strip():
                area, name = None, None          # 无目标=全屋（Turn* / ControlWindow 通用窗）
            else:
                return self._miss(trace, f"提取质量低(score={score})")
        # 音乐泛词守卫（2026-09-12 零改动过渡带）：Turn* 车道若把泛音乐词
        # ("关掉音乐/关音乐")吃成设备名，会错关同名实体或空转失败。放行 None，
        # 交回级联 ⑤b 音乐带处理（真叫"音乐"的设备请说"关掉音乐开关"消歧）。
        if (intent in ("TurnDeviceOn", "TurnDeviceOff") and name and area is None
                and str(name).strip() in _MUSIC_WORDS):
            return self._miss(trace, f"音乐泛词:{name}→交音乐带")
        # 空调区域守卫（原 L662-668）；2026-09-15 补：动态词表（P2-17）会把
        # 区域吸进设备名——HA 实体常叫「客厅空调」——此时 name 已自带限定，
        # 再按"缺区域"拒执行会让这类设备的**所有**空调指令失效（无 LLM 时
        # 直接不可用）。只有裸"空调"（或纯编号）才算真的缺区域。
        if name and any(k in name.lower() for k in _AC_KEYWORDS):
            if not area and not _ac_name_qualified(name):
                return self._miss(trace, "空调缺区域信息")
        args: dict[str, Any] = {}
        # 显式全屋（余下语序："全屋的灯打开"等）：目标不带区域、不带设备名，只留域
        # 过滤（集成端 name 空 + area 无 = 不过滤，全屋同域设备一起动），并打
        # whole_house 标，让卫星空间化/创建侧区域继承一律让路。
        # v1.0.41 审查 S12：本"余下语序"全屋兜底同样不得吞区域——rest_text 必须
        # 以全屋标记起头。否则「打开卧室全部窗帘」(rest=卧室全部窗帘) 会剥掉
        # 「全部」得「卧室窗帘」→ domain cover → whole_house=True，把「卧室」静默
        # 丢弃开全家窗帘。区域句落回下方 :803 正常 area/name 处理（卧室+窗帘）。
        if (intent in ("TurnDeviceOn", "TurnDeviceOff") and _WHOLEHOUSE_RE.search(text)
                and _WH_HEAD_RE.match(rest_text or "")):
            word = _wholehouse_word(rest_text or text)
            doms = [str(d) for d in (T.domain_hint(word) if word else [])]
            if doms:
                args["target"] = [{"devices": [{"name": "", "domains": doms}]}]
                trace.append(f"全屋显式:{word}→domains={doms}")
                return Plan(intent=intent, args=args, source=source, utterance=text,
                            trace=trace, whole_house=True)
        if name or area:
            device_item = {"name": name, "domains": T.domain_hint(name or "")} if name else {"domains": []}
            entry: dict[str, Any] = {}
            if area:
                entry["area"] = area
            if name:
                entry["devices"] = [device_item]
            args["target"] = [entry] if entry else []
        if "action" in extra:
            args["action"] = str(extra["action"]).lower()
        for k in ("attribute", "delta", "mode", "position"):
            if k in extra:
                args[k] = extra[k]
        # 温度调节带目标也改道 ClimateSetTemperature（原第二处：name 有而 attribute=temperature 保留 Adjust——仅无设备名改道，已在上分支）
        trace.append(f"args={args}")
        return Plan(intent=intent, args=args, source=source, utterance=text, trace=trace)

    def _miss(self, trace: list[str], why: str = "") -> None:
        if why:
            trace.append(f"miss:{why}")
            logger.debug("[T0/T1] %s | %s", why, " ← ".join(trace))
        return None


# 12 窗型 + 泛称「窗户」（与 custom_components/huijian_ai WINDOW_ACTION_MAPPING 对齐，
# 长词在前防短词截胡）；窗帘/纱窗=标准 cover，不在此表。
_WINDOW_TYPES = ("内开内倒窗", "外装平开窗", "单内倒窗", "平推窗", "平开窗",
                 "推拉窗", "内开窗", "外开窗", "推拉门", "智能窗", "天窗",
                 "飘窗", "窗户")


_AC_KEYWORDS = ("空调", "空調", "aircondition")


# 显式全屋说法（2026-09-15 用户令）：只有用户明说"所有/全部/全屋/整个家/家里/每个"
# 才是全屋语义；没写区域的"开灯"由创建侧继承触发条件的区域（客厅温度→客厅的灯）。
_WHOLEHOUSE_RE = re.compile(r"所有|全部|全屋|整屋|整个家|家里|全家|各个|每个")
# v1.0.41（F13）：全屋句的**句尾**控制动词（$ 锚死——「家里灯都开着吗」的 吗、
# 「所有灯现在什么状态」的名词尾都不落位；裁决：「都」只作可选前缀、句尾只容
# 完成态助词 了/啦——「所有灯都关啦」是命令，疑问字仍不豁免）。长形态排前。
_WH_TRAIL_RE = re.compile(r"(?:全都|全部|都|全)?"
                          r"(关闭|关掉|关了|打开|开了|开启|开一下|开|关)(?:了|啦)?$")
# 全屋句的合法句首（守卫与尾动分支共用）：句首即全屋标记才算全屋句。
_WH_HEAD_RE = re.compile(r"^(所有|全部|全屋|整屋|整个家|家里|全家|各个|每个)")

# 连排残渣检测（2026-09-10）：区域名里出现这些动作动词＝parse_target 把第二个动作
# 子句当成了区域（见 _build_plan 守卫）。与 creation._SERIAL_VERB 同表。
_AREA_VERB_RESIDUE = re.compile(
    r"打开|关闭|关掉|关上|开启|开一下|开到|关到|调到|调为|调成|调节|调整|"
    r"设为|设成|设定|设置|播放|停止|暂停|锁上|解锁|拉上|拉下|全开|全关")


def is_whole_house(text: str) -> bool:
    """文本是否含显式全屋标记（创建侧据此不继承区域）。"""
    return bool(_WHOLEHOUSE_RE.search(text or ""))


def _wholehouse_word(text: str) -> str:
    """显式全屋说法的设备词：剥掉"所有/全部/全屋/家里"等标记与属格"的"后的实义部分
    （"所有灯"→灯、"全屋的窗帘"→窗帘），供域提示推断。永不抛。"""
    t = _WHOLEHOUSE_RE.sub("", text or "").replace("的", "").strip()
    return t


def _ac_name_qualified(name: str) -> bool:
    """设备名是否自带区域/编号限定（"客厅空调"/"办公室空调"/"2号空调"）。
    判据：剥掉"空调"字样后还剩实义汉字——只有裸"空调"才需要用户补区域。"""
    s = str(name or "").strip().lower()
    for k in _AC_KEYWORDS:
        s = s.replace(k, "")
    return any("\u4e00" <= c <= "\u9fff" for c in s)


def _window_type(name: str) -> Optional[str]:
    n = str(name or "")
    for w in _WINDOW_TYPES:
        if w in n:
            return w
    return None


def _any_action_match(rest: str) -> bool:
    return any(p.match(rest) for p, _, _ in _ACTION_PATTERNS)


def _to_int(delta: Any) -> int:
    try:
        return int(float(delta))
    except (TypeError, ValueError):
        return 0
