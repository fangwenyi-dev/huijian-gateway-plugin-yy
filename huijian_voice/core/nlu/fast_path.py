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

# ── v1.1.1 属性车道批次常量（#3 色名表 / #4 绝对值哨兵）───────────────
# 「动词+绝对值」的中间哨兵：字面表阶段还不知道设备族，属性名一律挂哨兵，
# 等 _build_plan 解析出 target.domains 后再由 resolve_absolute_lane 落名。
# 哨兵绝不出 _build_plan（test_v1111 test_sentinel_attribute_never_escapes 钉）。
_ABSOLUTE = "@absolute"

# 色名 → 色值。**hex 全部取自慧尖数据集 assistant 真值**（两份数据集 16 条
# color 样本逐字抄录，不自创色卡）：暖色 #FFAA80 / 暖白色 #FFEFD5 / 冷色
# #80FFFF / 紫 #8000FF / 红 #FF0000 / 黄 #FFFF00 / 绿 #00FF00 / 蓝 #0000FF /
# 白 #FFFFFF。集成端 light.color 处理器（parse_delta 认 # 前缀 → RGBColor）。
_COLOR_HEX = {"暖白色": "#FFEFD5", "暖色": "#FFAA80", "冷色": "#80FFFF",
              "白色": "#FFFFFF", "红色": "#FF0000", "橙色": "#FFA500",
              "黄色": "#FFFF00", "绿色": "#00FF00", "青色": "#00FFFF",
              "蓝色": "#0000FF", "紫色": "#8000FF", "粉色": "#FFC0CB"}
# 橙/青/粉=同族话术补全（数据集未穷举，形态与已列色词同构）；长词排前，
# Python re 交替是最左优先，「暖白色」必须在「暖色」之前。
_COLOR_ALT = "|".join(sorted(_COLOR_HEX, key=len, reverse=True))

# ②③ 设备前缀剥离的补充候选：具名表 KNOWN_DEVICES_PREFIX 全表 ≥2 字，单字
# 泛设备词「灯」进那张表会污染 parse_target 评分与窗族守卫（targets 侧表另有
# 用途，不改），而「灯光」既不在表内也非 KNOWN_DEVICE——数据集原形「把灯调成
# 红色」「把灯光调成白色」两条路都够不到，只在本层动作表重扫时补这两个头。
_BARE_DEV_HEADS = ("灯光", "灯")


def _strip_heads() -> tuple:
    """②③ 剥离候选（具名表 ∪ 裸设备词，长词在前）。每次现读：动态词表会扩表。"""
    return tuple(sorted(set(T.KNOWN_DEVICES_PREFIX) | set(_BARE_DEV_HEADS),
                        key=len, reverse=True))


def resolve_color_delta(word: str) -> Optional[str]:
    """色名词 → #RRGGBB；表外词 None。

    绝不把中文原词当 delta 发上 wire——集成 parse_delta 只认 `#` 前缀，
    其余形态落 invalid value，用户听到的是"没听懂"却已占用一次执行。
    """
    return _COLOR_HEX.get((word or "").strip())


def color_word(hex_value: str) -> Optional[str]:
    """#RRGGBB → 中文色名（播报侧回显）；表外 None。hex 念进 TTS 是噪音。"""
    h = (hex_value or "").upper()
    return next((w for w, v in _COLOR_HEX.items() if v.upper() == h), None)


def resolve_absolute_lane(domains: list, raw_delta: str) -> Optional[tuple]:
    """「开到/调到+绝对值」按设备族落属性名 → (attribute, delta)；族落不了 None。

    域×属性名一律取集成 register_adjustment 的合法组合（intent_adjust_attribute
    逐条核）：light=brightness/color/temperature、cover=position（**只支持
    number**）、fan/humidifier/number 各一档、climate 由**单位**裁决（%/档→
    fan_speed；度形温度句根本不进本车道，见 _ACTION_PATTERNS 绝对值表注释）。
    climate 裸数属"26 度还是 26% 风速"二义，与 v1.0.69 红线同判据：宁如实
    MISS，绝不猜（猜错=动了另一台设备的另一个属性）。

    单位口径（parse_delta/calc_target 实测）：只有 档/挡 会改变集成侧分支
    （level），% 对 number 分支等价 → 剥掉，免得把 "50%" 发成非法形态。
    """
    m = re.match(r"^\s*(一半|百分之([零一二三四五六七八九十百]+)|(\d+(?:\.\d+)?))\s*"
                 r"([%％]|档|挡)?\s*$", (raw_delta or "").strip())
    if not m:
        return None
    unit = m.group(4) or ""
    if m.group(1) == "一半":
        num = "50"                        # 「调一半」=绝对 50（v1.0.63 同口径）
    elif m.group(3):
        num = m.group(3)
    else:
        try:
            n = int(T.cn2num(m.group(2)))
        except (TypeError, ValueError):
            return None                   # 数词表外形态（"百分之最大"）一律不接管
        num = str(n)
    if not re.fullmatch(r"\d+(\.\d+)?", num):
        return None
    if not 0 <= float(num) <= 100 and unit not in ("档", "挡"):
        return None                       # 0-100 之外不属百分比/亮度/开合度语义
    doms = [d for d in (domains or []) if d]
    if "climate" in doms:
        if unit in ("%", "％", "档", "挡"):
            return ("fan_speed", num + unit if unit in ("档", "挡") else num)
        return None
    for dom, attr in (("light", "brightness"), ("cover", "position"),
                      ("fan", "fan_speed"), ("humidifier", "humidity"),
                      ("number", "value")):
        if dom in doms:
            return (attr, num + unit if unit in ("档", "挡") else num)
    return None                           # 认不出族（switch/media_player/纯区域…）


# ── 动作词匹配规则（v1.5 L207-255 逐字移植；(pattern, intent, action_val)）──
_ACTION_PATTERNS: list[tuple[re.Pattern, str, Any]] = [
    # v1.0.40 修复（A1）：交替式必须**长词在前**——Python re 交替是"最左优先"而非
    # 最长匹配，旧写法 `^(开窗|打开窗|开窗户|打开窗户|…)` 对「打开窗户」先命中"打开窗"
    # → 残留"户" → 目标提取质量门不过 → 整句落空（真机实测 打开窗户/关闭窗户/开窗户/
    # 关窗户/打开窗帘/关窗帘/关闭窗帘 七个常见说法全部 None，而 T1 明明判对）。
    # 另加 `(?!帘)`：窗帘是 cover 设备，不能被窗户动作吃掉前缀（否则"打开窗帘"残留
    # "帘"、且语义也从"开窗帘"错成"开窗"）；命中不了即落到下方通用 `^(打开|…)`。
    # 2026-09 开窗器护栏：`开窗(?!帘)` 会把「开窗器关闭」的 开窗 咬成动词、
    # 残出「器关闭」，(?!帘|器) 让整词进 ② 前缀剥离/窗族纠正车道。
    (re.compile(r"^(打开窗户|开窗户|打开窗(?!帘)|开窗(?!帘|器)|窗户打开)"), "ControlWindow", "open"),
    (re.compile(r"^(关闭窗户|关窗户|关闭窗(?!帘)|关窗(?!帘|器)|窗户关闭)"), "ControlWindow", "close"),
    (re.compile(r"^(内倒|内导|内岛|内到|内道|内达|内打|内大|内藻)"), "ControlWindow", "A"),
    # 音乐带（2026-09-12）：后接音乐补语（播放/音乐/歌）时让位——"停止播放"
    # 是播控令不是窗帘暂停；裸"暂停/停"与"暂停窗帘"仍走窗户语义。
    (re.compile(r"^(暂停|停止|停)(?!(?:播放|音乐|歌|一?首))"), "ControlWindow", "pause"),
    # v1.1.1 #4：「动词+绝对值」不再硬编码 position——属性名由**设备族**落
    # （resolve_absolute_lane），认不出族如实 MISS。旧形态实测：「客厅灯开到50」
    # 发 position 给 light=unsupported；「把窗帘调到50%」动词表够不到掉进 T1
    # Turn，50% 整个丢光（假动作）。
    # 尾随 度/℃ 一律**不接管**（负向预查）：温度绝对值有自己的成熟车道
    # （上方 度 形专表 + T1 AdjustTemperature → HassClimateSetTemperature，
    # golden「设为26度」钉在案），抢过来只会把无设备的上下文句判成 MISS。
    (re.compile(r"^(?:开到|打开到|关到|调到|调为|调成|设为|设到|设成|改成|换成)\s*"
                r"(\d+(?:\.\d+)?\s*(?:[%％]|档|挡)?|百分之[零一二三四五六七八九十百]+)"
                r"(?![\d.零一二三四五六七八九十百%％档挡度℃Ff])"),
     "AdjustDeviceAttribute", {"attribute": _ABSOLUTE, "delta": "$1"}),
    # v1.1.1 对账：「(窗帘)调一半」形（数据集 position 50）。动词表**不收裸
    # 调/设**——实测「调高亮度到80%」被 ^调+一半 之外的裸调截走后 rest 只剩
    # 「高亮度到80%」，属性词快捷够不到 → 80% 丢光（test_context_chain_absolute
    # _brightness 钉的就是这条链路），故 一半 单独成行。
    (re.compile(r"^(?:调|整|拉)(?:到)?一半"), "AdjustDeviceAttribute",
     {"attribute": _ABSOLUTE, "delta": "50"}),
    # v1.0.63 开向位置缺陷修复（golden 建表实锤）：旧表只有 ^关一半——
    # 「窗帘开一半」被 L91 ^(开|打开) 吃成 TurnDeviceOn、"一半"当残渣剥掉，
    # 用户要半开得到**全开**：错误结果比拒答危险（cover 开向/关向的"一半"
    # 目标位都是绝对 50，同 executor 既有落地，仅入口漏配）。
    # v1.1.1 #4：同批改走族落表（帘→position、灯→brightness、空调无歧义属性→MISS）。
    (re.compile(r"^(?:打开|开|关)(?:到)?一半"), "AdjustDeviceAttribute",
     {"attribute": _ABSOLUTE, "delta": "50"}),
    (re.compile(r"^(调到|调为|调成|温度调到|温度设到|温度设为)\s*(\d+)\s*度"), "AdjustDeviceAttribute", {"attribute": "temperature", "delta": "$2"}),
    (re.compile(r"^(调到|调为|调成|温度调到|温度设到|温度设为)\s*([零一二三四五六七八九十百]+)\s*度?"), "AdjustDeviceAttribute", {"attribute": "temperature", "delta": "cn:$2"}),
    (re.compile(r"^(亮度设到|亮度调到|调亮到|调暗到|亮度)\s*(\d+)"), "AdjustDeviceAttribute", {"attribute": "brightness", "delta": "$2"}),
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
    # v1.1.1 #2/#3：色温相对档锚点（零宽，**不吃字**——属性词得留在 rest 里给
    # _ATTR_ONLY 认族，见 _build_plan 属性词快捷）。过去「把色温调高一点」字面表
    # 全够不到 ⇒ 掉 T1 判成 AdjustTemperature ⇒ 去动**空调**（跨域误执行，与
    # 「内倒→雷达」同级）。锚上 color_temperature 后由扫描器出 ±500K（=集成
    # light.temperature 的 supported_adjust_step），域恒 light，不再串到 climate。
    (re.compile(r"^(?=色温)"), "AdjustDeviceAttribute", {"attribute": "color_temperature"}),
    (re.compile(r"^(暖光)"), "AdjustDeviceAttribute", {"attribute": "color_temperature", "delta": "2700"}),
    (re.compile(r"^(冷光)"), "AdjustDeviceAttribute", {"attribute": "color_temperature", "delta": "6500"}),
    (re.compile(r"^(白光|自然光)"), "AdjustDeviceAttribute", {"attribute": "color_temperature", "delta": "4000"}),
    # v1.1.1 #3：色名词句（数据集 16 条 color 原形全 MISS 的收口）。色词在句尾，
    # 引导动词 调成/设为 由 ②③ 前缀剥离后的 suffix 带进来，故动词段可选；
    # delta 先落**色词原词**，_build_plan 末端 resolve_color_delta 折成 #hex
    # （表外色词当场 MISS，不把中文发上 wire）。
    (re.compile(r"^(?:调成|调到|设为|设成|改成|换成|变成|换到|切到|切换为)?("
                + _COLOR_ALT + r")调?"), "AdjustDeviceAttribute",
     {"attribute": "color", "delta": "$1"}),
    (re.compile(r"^(风大一点|风大些|加大风速|风量大一点|风量加大)"), "AdjustDeviceAttribute", {"attribute": "fan_speed", "delta": "+1"}),
    (re.compile(r"^(风小一点|风小些|减小风速|风量小一点|风量减小|风量调小)"), "AdjustDeviceAttribute", {"attribute": "fan_speed", "delta": "-1"}),
    # v1.1.1 对账：风速/湿度**零宽锚点**（与 ^色温 同构，只定属性不吃字）——
    # 数值与档位交 _ATTR_ONLY/_scan_delta 落，目标域由属性词快捷给（裸 风量/
    # 风速 句无设备名，旧实现产 target 缺失的 Adjust，集成 slot_schema
    # Required('target') 当场 Invalid）。
    (re.compile(r"^(?=风速|风量)"), "AdjustDeviceAttribute", {"attribute": "fan_speed"}),
    (re.compile(r"^(?=湿度)"), "AdjustDeviceAttribute", {"attribute": "humidity"}),
    # v1.1.1 对账：开合度相对档锚点（数据集原形「把窗帘位置调低」=position -20，
    # 原表只有 开到N/关到N 绝对形，"位置调低"整句 MISS）。
    (re.compile(r"^(?=位置|开合度)"), "AdjustDeviceAttribute", {"attribute": "position"}),
    (re.compile(r"^(制热模式|制热|加热模式|加热)"), "SetDeviceMode", {"mode": "heat"}),
    (re.compile(r"^(制冷模式|制冷|冷却模式|冷却)"), "SetDeviceMode", {"mode": "cool"}),
    (re.compile(r"^(除湿模式|除湿|抽湿)"), "SetDeviceMode", {"mode": "dry"}),
    (re.compile(r"^(送风模式|送风|通风)"), "SetDeviceMode", {"mode": "fan_only"}),
    (re.compile(r"^(自动模式|自动)"), "SetDeviceMode", {"mode": "auto"}),
    # 2026-10-01 数据集对账二期：模式族动词条与下方 preset 族（睡眠/节能…）拉齐。
    # 原五条只认「调到/调为/调成/设为/改成/改/切换为/换为」，实测「空调设成送风」
    # 「客厅空调换成制冷」落 MISS，而同一句换成「设为睡眠模式」却能出档——同源话术
    # 两套动词表=口径漂移，按 preset 族的长表统一（保留裸「改」在最后，交替序优先
    # 长动词，防「改成」被「改」截成残段）。
    (re.compile(r"^(调到|调为|调成|调至|调整为|调整到|设置为|设定为|设定成|设置成|设为|设成|设置|设定|改成|换成|切换为|切换到|切到|切为|换到|变为|进入|改)\s*(制热模式|制热|加热模式|加热)"), "SetDeviceMode", {"mode": "heat"}),
    (re.compile(r"^(调到|调为|调成|调至|调整为|调整到|设置为|设定为|设定成|设置成|设为|设成|设置|设定|改成|换成|切换为|切换到|切到|切为|换到|变为|进入|改)\s*(制冷模式|制冷|冷却模式|冷却)"), "SetDeviceMode", {"mode": "cool"}),
    (re.compile(r"^(调到|调为|调成|调至|调整为|调整到|设置为|设定为|设定成|设置成|设为|设成|设置|设定|改成|换成|切换为|切换到|切到|切为|换到|变为|进入|改)\s*(除湿模式|除湿|抽湿)"), "SetDeviceMode", {"mode": "dry"}),
    (re.compile(r"^(调到|调为|调成|调至|调整为|调整到|设置为|设定为|设定成|设置成|设为|设成|设置|设定|改成|换成|切换为|切换到|切到|切为|换到|变为|进入|改)\s*(送风模式|送风|通风)"), "SetDeviceMode", {"mode": "fan_only"}),
    (re.compile(r"^(调到|调为|调成|调至|调整为|调整到|设置为|设定为|设定成|设置成|设为|设成|设置|设定|改成|换成|切换为|切换到|切到|切为|换到|变为|进入|改)\s*(自动模式|自动)"), "SetDeviceMode", {"mode": "auto"}),
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
    # 裸 开 加器字护栏：「开窗器/开合器」句首时不得从中间下刀（残「窗器…」），
    # 让位给 ② 设备前缀剥离（开窗器∈KNOWN_DEVICES_PREFIX 后整词回捞）。
    (re.compile(r"^(打开|开启|开一下|开了|开(?!窗器|合器))"), "TurnDeviceOn", None),
    (re.compile(r"^(关闭|关掉|关了|关一下|关)"), "TurnDeviceOff", None),
    (re.compile(r"^(open (?:the )?window)(?:\s+|$)", re.I), "ControlWindow", "open"),
    (re.compile(r"^(close (?:the )?window)(?:\s+|$)", re.I), "ControlWindow", "close"),
    (re.compile(r"^(open|turn on|switch on|power on)(?:\s+|$)", re.I), "TurnDeviceOn", None),
    (re.compile(r"^(close|turn off|switch off|power off)(?:\s+|$)", re.I), "TurnDeviceOff", None),
    # PlayMusic：模式 B 无小智播放面，保留词表但显式放行到上层（收编改造点 2）
    (re.compile(r"^(播放|放|来一首|唱)"), "PlayMusic", None),
]

# ── v1.0.93 「退下」字面表（2026-09-18 用户批准收词表）───────────────────
# 连续对话语音退出：**整句精确匹配**，绝不做子串——「安静一点」（调亮度）、
# 「再见面」「不用了谢谢」类误杀案由负例钉守。容忍句首"好的，"承接与句尾
# 单个语气字（吧/啦/呀/啊/哦/嘛）；STT 尾标点归一由 is_end_dialogue 兜。
# 裁决位：场景契约等值之后、一切动作表/闸之前（见 FastPath.match 注）。
END_DIALOGUE_INTENT = "HuijianEndConversation"
_END_DIALOGUE_RE = re.compile(
    r"^(?:好的[，,]?\s*)?(?:退下|结束对话|不聊了|不说(?:了)?|再见|拜拜|停止聆听|"
    r"退出对话|安静|别念了|不用了)[吧啦呀啊哦嘛]?$")


def is_end_dialogue(text: str) -> bool:
    """收词表谓词（级联与 nlu-off 支共用）。永不抛；先去尾标点再等值。"""
    try:
        t = re.sub(r"[\s。，,！!？?~～.]+$", "", (text or "").strip())
        return bool(_END_DIALOGUE_RE.match(t))
    except Exception:  # noqa: BLE001 谓词故障=不退出（fail-open 到旧行为）
        return False


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
        # 裸「到/至」形 2026-09-21 补：「调高亮度到80%」曾被可选组漏掉"到"，
        # 整句掉进 (调高)→+20 相对档——**绝对值被静默丢**，用户要 80% 得到 +20。
        # 「调高/调低…到」双动词形：亮度与数值间允许 ≤2 字间隙、"到"前缀可选。
        (re.compile(r"(?:调到|设到|调高到|调低到|提高|降低)?\s*亮度[^\d]{0,2}(\d+)"), "$1"),
        (re.compile(r"亮度\s*(?:到|至)?\s*(\d+)"), "$1"),
        (re.compile(r"百分之\s*([零一二三四五六七八九十百]+)"), "cn:$1"),
        (re.compile(r"(?:开到|打开到|调到|设到|设为|关到|调高到|调低到)\s*(\d+)\s*[%％]?"), "$1"),
        (re.compile(r"调到\s*(\d+)\s*%?"), "$1"),
        (re.compile(r"(?:一半|半数)"), "50"),
        (re.compile(r"(亮一点|亮一些|调亮|亮些|大一点|大一些|高一点|高一些|调高)"), "+20"),
        (re.compile(r"(暗一点|暗一些|调暗|暗些|小一点|小一些|低一点|低一些|调低)"), "-20"),
        # v1.1.1 对账：极值档（与 fan_speed 同族缺口；集成 calc_target 的
        # special 分支认 max/min，medium/auto 反而 unsupported，故只补这两个）
        (re.compile(r"(最大|最亮|最高|满亮度)"), "max"),
        (re.compile(r"(最小|最暗|最低)"), "min"),
    ],
    "temperature": [
        (re.compile(r"(?:调到|调为|调成|设为|设到|变成)\s*(\d+)\s*度"), "$1"),
        # v1.1.1 对账：「调高N度」是相对档，必须先于裸 `(\d+)度` 收——否则
        # 「把空调温度调高2度」被裸形吃成绝对 2℃（数据集期望 +2）。动词与数值
        # 允许 ≤6 字间隙（「调高空调温度5度」数据集原形，中间隔着设备词）。
        (re.compile(r"(?:调高|升高|提高|加大|调大)[^0-9]{0,6}?(\d+)\s*度"), "+$1"),
        (re.compile(r"(?:调低|降低|减小|减少|调小)[^0-9]{0,6}?(\d+)\s*度"), "-$1"),
        (re.compile(r"(\d+)\s*度"), "$1"),
        (re.compile(r"(?:调到|设为)\s*([零一二三四五六七八九十百]+)\s*度"), "cn:$1"),
        (re.compile(r"(高一点|高一些|暖一点)"), "+1"),
        (re.compile(r"(低一点|低一些|凉一点)"), "-1"),
    ],
    "humidity": [
        (re.compile(r"湿度\s*(?:调到|设为|设到|为|成|到|至)?\s*(\d+)"), "$1"),
        (re.compile(r"(大一点|大一些|高一点|调高)"), "+10"),
        (re.compile(r"(小一点|小一些|低一点|调低)"), "-10"),
    ],
    "fan_speed": [
        (re.compile(r"风[量速]?\s*(?:调到|设为|为|成|到|至)?\s*(\d+)"), "$1"),
        # v1.1.1 对账：极值档与"调大/调小"补进（数据集 fan_speed 三档实测
        # max/high/low，本表此前只有 ±1 数值档 → 「风速调到最大」整句 MISS）。
        # 顺序即优先级：带"一点"的渐进档（既有承诺，golden「风量大一点」=+1）
        # 必须排在裸 调大/加大 之前，否则相对档被极值档截胡。
        (re.compile(r"(最大|最高|最强|满档|满风)"), "max"),
        (re.compile(r"(最小|最低|最弱)"), "min"),
        (re.compile(r"(大一点|大一些|大些|高一点|高一些|高些)"), "+1"),
        (re.compile(r"(小一点|小一些|小些|低一点|低一些|低些)"), "-1"),
        (re.compile(r"(调大|加大|增大|调高|提高)"), "high"),
        (re.compile(r"(调小|减小|减少|调低|降低)"), "low"),
    ],
    "color_temperature": [
        (re.compile(r"(\d+)\s*[kK]"), "$1"),
        # v1.1.1 #3：相对档排在绝对色温档之前——「色温调暖一点」要的是"再暖
        # 一档"（±500K=集成 light.temperature 的 supported_adjust_step），不是
        # 一步跳到 2700K 最暖档；「暖光/冷光」这类无"调"字的档位词仍走绝对值。
        # 只认**显式**形态（调X / X一点），裸单字不收：「色温高的灯」不是指令。
        (re.compile(r"(调高|升高|提高|变高|高一点|高一些|大一点|冷一点|冷一些"
                    r"|凉一点|凉一些|变冷|调冷)"), "+500"),
        (re.compile(r"(调低|降低|变小|变低|低一点|低一些|小一点|暖一点|暖一些"
                    r"|变温|调温|调暖)"), "-500"),
        (re.compile(r"(暖光|暖色)"), "2700"),
        (re.compile(r"(冷光|冷色)"), "6500"),
        (re.compile(r"(白光|自然光)"), "4000"),
    ],
    "position": [
        (re.compile(r"开到\s*(\d+)\s*[%％]?"), "$1"),
        (re.compile(r"关到\s*(\d+)"), "$1"),
        # v1.0.63：开向"一半"同判（把X开一半/开一半 残扫车道），绝对位 50。
        (re.compile(r"(?:打开|开|关)一半"), "50"),
        (re.compile(r"打开到\s*(\d+)"), "$1"),
        # v1.1.1 对账：相对档（「把窗帘位置调低」数据集 = -20；步进 10% 的
        # 集成侧 supported_adjust_step 取一档 = 20，与亮度 ±20 同口径）
        (re.compile(r"(调高|开大|高一点|大一点|大一些|开一些)"), "+20"),
        (re.compile(r"(调低|关小|低一点|小一点|小一些|关一些)"), "-20"),
    ],
}

# 属性词 → 目标域映射（"卧室亮度调高一点"：亮度=属性不是设备名，转区域级 light 目标）
# 2026-09-21 扩数值尾巴：「调高亮度到80%」rest=亮度到80% 旧形不认"到80%"，
# 掉进 parse_target 质量门→miss→fallback（浴霸幻觉被 ⑦ 收紧堵掉后显形）。
# 属性句的正确形态=属性域目标+delta 走 _apply_context 上下文继承回上一设备。
_ATTR_ONLY = re.compile(
    # v1.1.1 数据集对账：连接动词段原为 `(?:调|整)*(?:到|至|为|成)?`，「湿度设为
    # 50%」「风速调到最大」这类 设为/调到 复合形整段不认（掉 parse_target 撞
    # 质量门=整句 MISS）；极值档（最大/最小）与"一半"根本没位置。改具名分段，
    # area/attr 仍占 1/2 号组（既有 group(1)/group(2) 引用不破）。
    r"^\s*([\u4e00-\u9fff]{1,4}?(?:室|厅|房|间|区|馆|楼))?[的]?"
    r"(亮度|色温|温度|风量|风速|湿度|位置|开合度)"
    r"(调到|调为|调成|调至|设为|设到|设成|设置为|设置到|设置成|调整到|升高到|降低到"
    r"|调|设|整|到|至|为|成)*"
    r"(?P<dir>高|低|亮|暗|大|小|暖|凉|冷|热)?"
    r"(到|至|为|成)?"
    r"\s*(?P<val>\d{1,3})?\s*(?:[%％度℃])?\s*"
    r"(?P<sp>百分之[零一二三四五六七八九十百]+|一半|最大|最小|最高|最低|最强|最弱|满档)?\s*"
    r"(?:的)?(?:一点|一些|点|些)?\s*$")
_ATTR_DOMAIN = {"亮度": "light", "色温": "light", "温度": "climate",
                "风量": "climate", "风速": "climate", "湿度": "humidifier",
                "位置": "cover", "开合度": "cover"}
# 属性名→域（v1.1.1 对账）：裸属性句（「风速调到最大」「设置为制冷」）剥完
# 数值后既无设备名也无区域，必须由**级联层**补一个同域过滤目标——
# 集成 slot_schema Required('target') 缺槽即 Invalid（整句白丢）。
# 不在 _build_plan 内补：那形态正是 pipeline._is_wholehouse_args 认的"显式全屋"
# （空 name + domains 过滤），会被提前豁免上下文继承，实测把
# 「打开办公室射灯」→「调高亮度到80%」的继承目标 射灯 吃成全屋灯。
_ADJUST_DOMAIN = {"brightness": "light", "color": "light",
                  "color_temperature": "light", "temperature": "climate",
                  "fan_speed": "climate", "position": "cover",
                  "humidity": "humidifier"}
_MODE_DOMAIN = {"heat": "climate", "cool": "climate", "dry": "climate",
                "fan_only": "climate"}


def attribute_domain_target(intent: str, args: dict) -> list:
    """Adjust/SetMode 无目标句 → 属性/模式所属域的过滤目标；判不出域返回 []。

    只回 `domains` 过滤、**不回 name**：全屋同域扇出是这类句子的既有语义
    （「亮度调高」=所有灯），具名目标一律交 pipeline 上下文继承先占。
    """
    try:
        if intent == "AdjustDeviceAttribute":
            fam = _ADJUST_DOMAIN.get(str((args or {}).get("attribute") or ""))
        elif intent == "SetDeviceMode":
            fam = _MODE_DOMAIN.get(str((args or {}).get("mode") or ""))
        else:
            fam = None
        return [{"devices": [{"domains": [fam]}]}] if fam else []
    except Exception:  # noqa: BLE001 兜底目标构造故障=不补（宁 Invalid 不猜设备）
        return []
# M3（2026-09-23 深审）：T0 动词头吞掉目标后，「短前缀+属性词+数值/相对档」尾巴
# 不得静默丢——「开灯亮度50」曾落 TurnDeviceOn(灯)谎报成功，「亮度50」蒸发，
# 且违约 CHANGELOG v1.0.37 承诺「触发词是开灯时，说开灯亮度50依然是调亮度」。
_T0_ATTR_WORD = {"亮度": "brightness", "色温": "color_temperature",
                 "温度": "temperature", "风量": "fan_speed", "风速": "fan_speed",
                 "湿度": "humidity", "开合度": "position", "位置": "position"}
_T0_ATTR_TAIL = re.compile(
    r"^(?P<dev>[\u4e00-\u9fffA-Za-z0-9]{0,6}?)"
    r"(?P<attr>亮度|色温|温度|风量|风速|开合度|位置)"
    r"\s*(?:调到|设为|设到|改成|改为|调高到|调低到|到|至|为|成)?\s*"
    r"(?P<val>\d{1,5}\s*[%％Kk]?|(?:百分之)?[零一二三四五六七八九十百]{1,6}\s*[%％度Kk]?"
    r"|一半|半数"
    r"|(?:调高|调低|调亮|调暗|加大|减小|增大|高|低|大|小|亮|暗|暖|凉)\s*(?:一点|一些|些|点)?)"
    r"$")
# T1 Adjust 剥数值段后的**句首**调节动词（只收双字形——单字会误剥
# "空调/拉窗"类设备名头部）。
_ADJ_HEAD = re.compile(r"^(?:调高|调低|调亮|调暗|调大|调小|调到|调至|调成|调为|"
                       r"提高|降低|提升|加大|减小|增加|减少)+")

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


def _is_param_single(text: str) -> bool:
    """句尾显式数值 + 窗类参数词（速度/力度）＝单发参数令，不是连排句。

    「开窗器速度设为80」的 开窗 是设备名的一部分，却被连排切分误判成动词
    段（split→['开窗器速度','设为80']），链发第二段裸数值永远听不懂，整句
    落兜底。豁免只认**假分裂**：目标剥掉窗类名词后不含独立动作动词——
    「关闭办公室平开窗速度设为30」里 关闭 是真子句动词，仍按连排拒收
    （单发误执行=只关窗丢数值，比如实听不懂更糟）。"""
    try:
        m = _POS_TAIL_RE.search(text or "")
        if not m:
            return False
        head0 = text[:m.start()].strip()
        pm = _WIN_PARAM_TAIL_RE.search(head0)
        if not pm:
            return False
        target = head0[:pm.start()].strip(" 的地得了吧啦，,")
        if not _is_window_position_target(target):
            return False
        resid = target
        for w in ("开窗器", "开合器") + _WINDOW_TYPES:
            resid = resid.replace(w, "")
        return not re.search(r"打开|关闭|关掉|关上|开启|开一下|开到|关到|"
                             r"调节|调整|设定|设置|停止|暂停|全开|全关|开|关",
                             resid)
    except Exception:  # noqa: BLE001 —— 豁免判定永不冒泡（保守=不豁免）
        return False

# ── 开窗器速度/力度参数（网关 v1.4.3+ 的 number 滑动条）─────────────
# 「办公室平开窗速度设为百分之三十」旧版被百分比预检当**开度**吃掉（播报
# "开到30%"、窗位被改，速度设定纹丝不动）。速度/力度词出现在数值段之前、
# 且目标仍是窗类时，语义让位给参数通道：ControlWindow(speed|strength=N)。
# 目标非窗类（"风扇速度调到30%"）一律不接管，落回原有车道——帘族同排除。
_WIN_PARAM_TAIL_RE = re.compile(r"(?P<kw>速度|力度)\s*(?:的)?\s*$")
_WIN_PARAM_KEYS = {"速度": "speed", "力度": "strength"}


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
            # 2026-09-30 谎报闸（v1.0.69「宁如实失败」纪律）：cn2num 对非纯数词
            # 是**静默返 0**（'最大'/'全部'/'顶' → position 0 = 完全关窗还播
            # 「已开到 X%」）。裸中文 token 必须逐字全是数词才可信；未来谁给
            # _POS_TAIL_RE 加新档位，这里都拦得住。
            if not n or not re.fullmatch(r"[零一二三四五六七八九十百两]+", n):
                return None
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
# 2026-09 多设备护栏（与 creation.split_actions 段数上限配套）：目标段里仍带
# 「、/，+动作动词」= 多段连排句没被链发接住（超上限退化成整段）。旧实测：
# 六段场景句退回单发被错配成「打开办公室平开窗」单动作、静默建出半截场景。
# 只认分隔符后的强动词形态（不带裸 开/关），防"打开空调，26度"这类正常补语误伤。
_SERIAL_RESIDUE = re.compile(
    r"[、，,]\s*[^、，,]{0,14}?(?:打开|开启|关闭|关掉|关上|关了|开了|调|设|拉|锁)")


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
            if "$" in val:
                # 模板替换（v1.1.1 对账扩）：原只认整串 "$n"，"+$2" 这类**带符号
                # 模板**（相对档"调高N度"→ +N）会被 int("$2") 炸掉。逐处替换，
                # 越界组号按空串处理（宁缺不崩）。
                def _sub(g):
                    idx = int(g.group(1))
                    return (m.group(idx) or "") if m.lastindex and idx <= m.lastindex else ""
                return re.sub(r"\$(\d)", _sub, val), m.group(0)
            return val, m.group(0)
    return None


def _scan_window_action(text: str) -> Optional[str]:
    for pat, act in _WINDOW_ACTION_SCAN:
        if pat.search(text):
            return act
    return None


def _tail_window_action(rest_text: str, window_word: str) -> Optional[str]:
    """剥掉窗型词根后再扫动作（v1.1.1 #5：内倒语序可达性）。

    「打开客厅窗户内倒」的 内倒 是**动作**，而「打开客厅的内开内倒窗」的
    内倒 是**窗型词根**——同一子串两种身份，不先抹掉 _window_type 命中的整词
    就无法区分，且后者被 _WINDOW_ACTION_SCAN 排在最前会截胡（数据集实锤该句
    action=open，扫成 a=反向误执行）。返回 None 时交回动词头原判据。
    """
    residue = (rest_text or "").replace(window_word or "", "")
    return _scan_window_action(residue) or None


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
_COVER_V_CLOSE = "拉上|合上|闭合|收起|收拢|拉下来|拉下|降下|放下|关上|关闭|关了"
_COVER_V_OPEN = "拉开|打开|开了"
# 2026-10-01 二期两处收口（同「模式动词条三缺一」型漂移）：
# ① 放下——_COVER_CLOSE_WORDS(:524) 早认它为关闭向，本道动词表却漏，致「放下窗帘」
#    「百叶窗放下」整句落空（数据集原句：帮我关灯并放下投影幕布）；
# ② 幕布——已在 KNOWN_DEVICES 两表且投影幕布生态=cover，但本道名词表不列 → SOV/SVO
#    语序归一够不到它。名词仍**具名逐词列举不撒网**（防吞杂串）。
_COVER_HEAD = r"[\u4e00-\u9fff]{0,6}?(?:窗帘|纱帘|卷帘|百叶帘|帘子|幕布|窗户|窗子|百叶窗)"
# 2026-10-01 二期：帘族具名词随表补入（卷帘/百叶帘 已入 KNOWN_DEVICES 且 domain_hint
# 判 cover，但本道只列 窗帘|纱帘|窗户 → 「卷帘拉上」「客厅百叶窗关闭」整句落空）。
# 与 _CURTAIN_ROOT_WORDS/domain_hint 同口径；**具名逐词列举不撒「帘」网**（防吞
# 「帘子布」类杂串），窗户/窗子/百叶窗 保持原语义走向（窗族→窗型纠正、帘族→cover）。
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
        """百分比开度句 → ControlWindow(position)；速度/力度参数句 →
        ControlWindow(speed|strength)。永不抛；门不齐返回 None 交回原动作表，
        无数字/帘族/非窗设备的句子行为与改动前完全一致。"""
        try:
            m = _POS_TAIL_RE.search(text)
            if not m:
                return None
            verb = m.group("verb") or ""
            head0 = text[:m.start()].strip()
            # 速度/力度参数句：数值段之前的 head 以参数词收尾（引导动词已被
            # verb 组吃掉；「平开窗的速度调到30%」的属格「的」也在这里一并
            # 认）。窗类目标走参数通道；非窗类（风扇等）返回 None 落回原车道，
            # 行为与改动前逐字一致。裁决先于开度解析：参数词即强意图标记，
            # 裸数字（"速度设80"）也放行。
            pm = _WIN_PARAM_TAIL_RE.search(head0)
            if pm:
                param = _WIN_PARAM_KEYS[pm.group("kw")]
                target = head0[:pm.start()].strip(" 的地得了吧啦，,")
                if not _is_window_position_target(target):
                    return None
                val = _parse_position(m.group("num"), True)
                if val is None:
                    return None
                trace.append(f"开窗{pm.group('kw')}:{target or '全屋窗'}→{val}%")
                return self._build_plan("ControlWindow", target, {param: val},
                                        text, "t0", trace)
            head = head0.strip(" 的地得了吧啦，,")
            pos = _parse_position(m.group("num"), bool(verb))
            if pos is None:
                return None
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

        # v1.0.93 「退下」字面表：仅次于场景契约的最高优先——在并列宾语闸/
        # 全屋分支/动作表扫描之前。此前"退下"走 T0 落兜底「我还不会」且照常
        # 续轮（2026-09-18 用户点名）；等值命中直接产出会话控制 Plan，不进
        # executor、不发设备指令（裁决在 pipeline._cascade 收口）。
        if _END_DIALOGUE_RE.match(text):
            trace.append("退下字面表")
            return Plan(intent=END_DIALOGUE_INTENT, args={}, source="t0_end",
                        utterance=text, trace=trace)

        # 并列宾语「打开A和B」单发禁执行闸（2026-09-21 用户令第③点）：该形态
        # 由 pipeline._try_compound 链发处理；链拒（某分句不认）回落到这里时，
        # 绝不允许 T0 把吃到的那一个执行掉并谎报成功——半执行比不执行危险。
        if T.coord_refuse(text):
            trace.append("并列宾语:链已拒或含不识分片,单发拒猜")
            return self._miss(trace)

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
        # 唯一豁免：窗类参数单发令（「开窗器速度设为80」的 开窗 是设备名的一部分，
        # 不是子句动词——见 _is_param_single）。
        if creation.serial_clauses(text) and not _is_param_single(text):
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
            # 剥离候选额外并 _BARE_DEV_HEADS（见 _strip_heads 注释）。
            for kd in _strip_heads():
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
            # v1.1.1 数据集对账：两种切法依次试——extract_prefix 的判据是"前缀尾字
            # ∈室厅房间楼区馆灯窗扇机…"，「阳台窗帘调成50%」会在 阳台窗|帘 处错切
            # （窗帘被劈成 窗+帘 → ②③ 与内层设备剥离全失配 → 掉 T1 把 50% 丢光）；
            # 区名直给切分（targets.split_area_head）补位。尾字形先试，「客厅…」
            # 既有语义与 source 标注零扰动。
            for prefix, suffix in (T.extract_prefix(text), T.split_area_head(text)):
                if not (prefix and suffix):
                    continue
                suffix = suffix.lstrip("的")   # "客厅的窗帘关一半"：前缀扫描容忍属格「的」
                for pattern, intent_type, action_val in _ACTION_PATTERNS:
                    m = pattern.match(suffix)
                    if m:
                        matched_intent, source = intent_type, "t0_prefix"
                        rest_text = prefix
                        extra_args = _parse_action_value(action_val, m)
                        break
                if not matched_intent:
                    for kd in _strip_heads():
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
                    # 剥数值段后再剥**调节动词头**：「调高亮度到80%」scan 吃掉
                    # "亮度到80" 剩 "调高%"——残渣守卫剥 %/调 后剩"高"非空，
                    # 旧实现以整块残渣去 parse_target 撞质量门→整句 miss。
                    rest_text = _ADJ_HEAD.sub("", text.replace(matched, "").strip()).strip()
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
        # 连排残段（超限/链被否退回单发）：目标段仍带「、/，+动词」→ 绝不单执行
        if rest_text and _SERIAL_RESIDUE.search(rest_text):
            trace.append("分句残余→拒猜目标(交链发/上层)")
            return None
        residue = re.sub(r"[调一些点把将了%％到亮暗度色温风量速为成设]", "", rest_text)
        if not residue.strip():
            rest_text = ""
        # M3（2026-09-23 深审）：Turn* 句残段仍带「属性词+数值/相对档」→ 绝不
        # 按开关谎报（尾巴被吞=半执行）；能折算落属性通道，不能如实整句拒。
        if intent in ("TurnDeviceOn", "TurnDeviceOff") and rest_text \
                and (am := _T0_ATTR_TAIL.match(rest_text)):
            attr_word = am.group("attr")
            attribute = _T0_ATTR_WORD[attr_word]
            _dl = None
            vtail = rest_text[am.start("attr"):]
            if (am.group("val") or "").strip() in ("一半", "半数"):
                _dl = "50"          # 属性句的「一半」=绝对 50（v1.0.63 同口径）
            scanned = _scan_delta(vtail, attribute)
            if _dl is None and scanned:
                _dl = scanned[0]
            elif _dl is not None and scanned is None:
                pass                # 一半已由上面定值
            if _dl is None:
                vm = re.match(r"^(\d{1,5})\s*[%％Kk]?$", (am.group("val") or "").strip())
                if vm:
                    _dl = vm.group(1)
            if _dl is None:
                return self._miss(trace, f"T0属性尾巴不可解:{rest_text}")
            vraw = (am.group("val") or "").strip()
            if (attr_word in ("位置", "开合度") and re.fullmatch(r"\d{1,2}", vraw)
                    and "%" not in vraw and "％" not in vraw):
                # 裸短数字对帘是「50%」还是「第1档按压」歧义——如实拒，不猜。
                # 「一半/半数」无歧义（=50，v1.0.63 同口径），不在拒列。
                return self._miss(trace, f"位置裸数字歧义:{rest_text}")
            dev = T.clean_name(T.normalize_name(T.strip_modal(am.group("dev")))).strip("的地")
            dom = _ATTR_DOMAIN.get(attr_word, "light")
            if not dev:
                tgt = [{"devices": [{"name": "", "domains": [dom]}]}]
            elif dev.endswith(tuple(T.AREA_SUFFIX)):
                tgt = [{"area": dev, "devices": [{"domains": [dom]}]}]
            else:
                tgt = [{"devices": [{"name": dev,
                                      "domains": T.domain_hint(dev) or [dom]}]}]
            trace.append(f"T0属性尾巴收编:{dev or dom}+{attr_word}→{_dl}")
            return Plan(intent="AdjustDeviceAttribute",
                        args={"attribute": attribute, "delta": str(_dl), "target": tgt},
                        source=source, utterance=text, trace=trace)
        # Adjust + 「区域?+属性词」：目标=该区域属性域，属性词不吃成设备名
        if intent == "AdjustDeviceAttribute" and rest_text and (mm := _ATTR_ONLY.match(rest_text)):
            area, attr_word = mm.group(1) or "", mm.group(2)
            _attribute = extra.get("attribute") or _T0_ATTR_WORD.get(attr_word, "")
            _dl = str(extra.get("delta") or "").strip()
            _val, _dir, _sp = mm.group("val"), mm.group("dir"), mm.group("sp")
            if _val:
                # 方向字在前 = **相对档**（数据集实锤：「把空调温度调高2度」=+2，
                # 旧实现取绝对值 2 → 空调被设到 2℃，H1 同族谎报）；无方向字才是
                # 绝对值（「调到26度」）。
                _dl = (f"{'-' if _dir in ('低', '小', '暗') else '+'}{_val}"
                       if _dir in ("高", "低", "大", "小", "亮", "暗") else _val)
            elif _sp == "一半":
                _dl = "50"                          # 属性句"一半"=绝对 50（v1.0.63 同口径）
            elif _sp and _sp.startswith("百分之"):
                _dl = str(T.cn2num(_sp[len("百分之"):]))
            elif _sp in ("最大", "最高", "最强", "满档"):
                _dl = "max"                         # 集成 calc_target 的 special 支
            elif _sp in ("最小", "最低", "最弱"):
                _dl = "min"
            elif _dl in ("", "None"):
                # v1.1.1 #3：^色温/^风速|^湿度 零宽锚点只定属性不定值——数值/
                # 相对档一律交扫描器，扫不到即如实 MISS，绝不发空 delta
                # （集成 parse_delta 空值=invalid value，等于白占一次执行）。
                scanned = _scan_delta(rest_text, _attribute)
                _dl = scanned[0] if scanned else ""
            if str(_dl).strip() in ("", "None"):
                return self._miss(trace, f"属性句无数值:{rest_text}")
            args = {"attribute": _attribute, "delta": str(_dl)}
            dom = _ATTR_DOMAIN.get(attr_word, "light")
            args["target"] = ([{"area": area}] if area else []) or [{"devices": [{"domains": [dom]}]}]
            if area:
                args["target"] = [{"area": area, "devices": [{"domains": [dom]}]}]
            trace.append(f"属性词快捷:{area or '全屋'}+{attr_word}→{dom}")
            return Plan(intent=intent, args=args, source=source, utterance=text, trace=trace)
        # 温度调节且无设备名 → HassClimateSetTemperature 改道（原 L695-701 保留）
        # H1（2026-09-23 深审）：改道分支 `_to_int("+1")=1` 把**相对档静默变
        # 绝对值**——「温度调高一点」全屋空调设到 1°C 且谎报「已调到1度」。
        # 改道只对无符号绝对值合法；带符号相对档落 Adjust+climate 域通道
        # （对照面：带设备名/带区域的同句一直保相对语义，非对称即缺陷）。
        if (extra.get("attribute") == "temperature" and extra.get("delta")
                and not rest_text.strip()):
            _d = str(extra["delta"]).strip()
            if not _d.startswith(("+", "-")):
                return Plan(intent="HassClimateSetTemperature",
                            args={"temperature": _to_int(_d), "area": ""},
                            source=source, utterance=text,
                            trace=trace + ["温度改道 ClimateSetTemperature"])
            return Plan(intent="AdjustDeviceAttribute",
                        args={"attribute": "temperature", "delta": _d,
                              "target": [{"devices": [{"name": "", "domains": ["climate"]}]}]},
                        source=source, utterance=text,
                        trace=trace + ["温度相对档→Adjust+climate(H1)"])

        _was_on = intent == "TurnDeviceOn"
        area, name, score = T.parse_target(rest_text, action_match=_any_action_match)
        # 连排残渣守卫（2026-09-10 真机）：链发没接住的连排句会被 T0 当成**一句**，
        # parse_target 把第一个动作子句整段吃成"区域名"（"办公室射灯关闭办公室"）。
        # 区域名里出现动作动词＝这句其实是两句，绝不能拿残渣区域去执行——真机里
        # 它把平开窗关了、射灯没关还报"成功"（静默做错远糟于如实拒收）。
        if area and _AREA_VERB_RESIDUE.search(str(area)):
            return self._miss(trace, f"区域残渣(连排未切分):{area}")
        # 2026-09-30：全局类目标兜底**上移**至窗前（原在窗型纠正之后）——
        # 英文尾词单字形（'close window'/'turn on the ac' 被 parse_target
        # 质量门拒出 name=None 后，rest 整段顶名）过去够不到窗纠正与空调
        # 守卫，裸 "window" 会漏成 TurnDeviceOff(name=窗族) 扇出。中文句
        # 该分支的产物恒含已知设备词（⑥ 已接），实际行为不变。
        if name is None:
            # 全局类："开灯/关灯"（rest 为空但设备词在原文里）
            if intent in ("TurnDeviceOn", "TurnDeviceOff") and rest_text.strip():
                area, name = None, T.normalize_name(T.strip_modal(rest_text))
            elif not rest_text.strip():
                area, name = None, None          # 无目标=全屋（Turn* / ControlWindow 通用窗）
            else:
                return self._miss(trace, f"提取质量低(score={score})")
        # 窗型词落进设备名 → 按钮按压语义，不是开关设备（实机 2026-09-08：
        # 「打开 办公室平开窗」被错产成 TurnDeviceOn，集成按开关找窗必 miss）。
        # 2026-09-30 英文桥（用户现场：'turn on the office light'/'close the
        # bedroom window' 转写正确却「没找到设备」——SenseVoice 出英文词，客户
        # HA 全中文命名）。判窗先试中文等价词：window 类英文窗词换中文形顶前
        # （ControlWindow 集成端只读 targets[0]，英文形放首位必死）；非窗类
        # 英文词不动原形，由末端 bilingual_targets 并集追加中文目标（Turn lane
        # 逐目标 union——英文命名 HA 的既有通路零回归）。
        zh_name = T.en_device_zh(name) if isinstance(name, str) and name else None
        if intent in ("TurnDeviceOn", "TurnDeviceOff") and zh_name \
                and (_window_type(zh_name) or zh_name in ("窗", "窗户")
                     or _opener_word(zh_name)):
            name = zh_name
            zh_area = T.en_area_zh(area) if area else None
            if zh_area:
                area = zh_area
        if intent in ("TurnDeviceOn", "TurnDeviceOff") and name:
            wt = _window_type(name)
            if not wt and _window_type(rest_text):
                # parse_target 把窗型词切碎（「推拉门」的推/拉被当残留动词
                # → name 只剩「门」）；rest 尾部找回完整窗名顶替。
                name = wt = _window_type(rest_text)
            if wt:
                trace.append(f"窗型纠正:{name}→ControlWindow")
                intent = "ControlWindow"
                extra = {**extra, "action": extra.get("action")
                         or _tail_window_action(rest_text, wt)
                         or ("open" if _was_on else "close")}
            else:
                # 2026-09 开窗器名称纠正（用户令优化第①项）：「关闭开窗器」
                # 曾被 parse_target 剥成 name="窗" 残渣 + TurnDeviceOff 错意图
                # ——集成按开关域找"窗"必败或错设备。开窗器/开合器=窗控设备词
                # → ControlWindow 按压语义；整名保留编号限定（「3号开窗器」），
                # 区域前缀已析出则从整名中剔除。集成端 extract_window_name 认
                # 窗族词 + find_window_buttons 设备注册表按 original_name 寻径。
                # 「窗帘开合器」= 开合帘设备（cover），上下文带帘族词一律不进
                # ControlWindow——帘字必须看整段（rest+name），只看剥出的
                # name 会把「开合器」单独摘出来误判成窗。
                op = (None if any(w in f"{rest_text}{name}"
                                  for w in ("帘", "纱窗", "百叶"))
                      else _opener_word(name) or _opener_word(rest_text))
                if op:
                    full = T.clean_name(T.normalize_name(
                        T.strip_modal(str(rest_text or ""))))
                    if area and str(area) in full:
                        full = full.replace(str(area), "", 1).strip(" 的地里得")
                    if not _opener_word(full) or len(full) > 12:
                        full = op
                    name = full
                    trace.append(f"开窗器纠正:{name}→ControlWindow")
                    intent = "ControlWindow"
                    extra = {**extra, "action": extra.get("action")
                             or _tail_window_action(rest_text, op)
                             or ("open" if _was_on else "close")}
                elif name in ("窗", "窗户"):
                    # 2026-09-14 现场日志（2026-09-27 修复批）：「打开办公室平
                    # 盖窗」谎报「窗户打开了」、窗没动——未知窗词「X窗」被
                    # parse_target 尾剥折叠
                    # 成泛称「窗」+ Turn* 车道 = 谎报温床——集成 TurnDeviceOn
                    # 遇裸「窗」走全窗兜底并**伪造**「好的，X的窗户打开了」
                    # （用户没这扇窗，办公室所有开合器按钮反倒被按）。双层修：
                    # ① corrector 音似表（平盖窗→平开窗，主修，真机走通）；
                    # ② 本闸（深度防御，对所有未知「X窗」族成立）：rest 里还
                    #    剩更长的「X窗」残段=用户其实在点**具名**窗，保留整词
                    #    转 ControlWindow——集成端对未识别窗名有「不敢按全窗
                    #    执行」如实拒收终（intent_window_control），宁如实失
                    #    败不谎报。rest 只剩泛称（「打开办公室窗」）也纠车道：
                    #    开合器=按钮按压语义（与 窗户 既有待遇对齐），全窗执
                    #    行走 ControlWindow 如实收口（空结果=失败，不再伪造）。
                    full = T.clean_name(T.normalize_name(
                        T.strip_modal(str(rest_text or ""))))
                    if area and str(area) in full:
                        full = full.replace(str(area), "", 1).strip(" 的地里得")
                    # 首部量词/属格残渣（「开个窗」→「个窗」）先洗，防误入抗折叠
                    full = re.sub(r"^[一各这个那扇家的里]+", "", full).strip()
                    if (full and full not in ("窗", "窗户")
                            and full.endswith(("窗", "窗户"))
                            and len(full) <= 12
                            and not any(w in full for w in ("帘", "纱窗", "百叶"))):
                        name = full
                        trace.append(f"窗名抗折叠:{name}")
                    trace.append(f"泛窗纠正:{name}→ControlWindow")
                    intent = "ControlWindow"
                    extra = {**extra, "action": extra.get("action")
                             or _tail_window_action(full or rest_text,
                                                   _window_type(full or "") or "")
                             or ("open" if _was_on else "close")}
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
        # 2026-09-30 英文桥：'turn on the ac' 的裸 "ac" 与裸"空调"同权——守卫
        # 看中文等价词（name 现值现翻：桥前置换形/兜底顶名两条路都盖到），
        # 否则英文绕闸全屋扇出空调。
        _ac_probe = (T.en_device_zh(name) or str(name or "")).lower()
        if (name and intent in ("TurnDeviceOn", "TurnDeviceOff")
                and any(k in _ac_probe for k in _AC_KEYWORDS)):
            # v1.1.1 数据集对账：守卫**收窄到开关族**。原判据无差别拦所有意图，
            # 实测把数据集 20+ 句打死（「空调调成制冷模式」② 已正确产
            # SetDeviceMode cool、「把空调风速调大」T1 SetFanSpeed 1.00 均整句
            # MISS）。分域判据：开关=改变别人家设备电源态（跨房间实害，2026-09
            # 事故形态，保留）；模式/参数设定=全屋同向语义一致（数据集期望
            # target 恒 {domains:[climate]} 无区域，单空调户是唯一 sane 解）。
            if not area and not _ac_name_qualified(name):
                return self._miss(trace, "空调缺区域信息")
        args: dict[str, Any] = {}
        # v1.1.1 #3/#4：@绝对值哨兵与色词原词在**目标域已定**之后才落最终形态。
        # 只看设备名域提示、不看区域：区域句（"书房开到50"）属性二义=猜，按
        # v1.0.69 红线如实 MISS。
        if extra.get("attribute") == _ABSOLUTE:
            _doms = list(T.domain_hint(name or "") or []) if name else []
            _res = resolve_absolute_lane(_doms, str(extra.get("delta", "")))
            if _res is None:
                return self._miss(trace,
                                  f"绝对值落不了设备族:{name or area or rest_text}")
            extra = {**extra, "attribute": _res[0], "delta": _res[1]}
            trace.append(f"绝对值族落:{_res[0]}={_res[1]}@{'/'.join(_doms)}")
        if (extra.get("attribute") == "color"
                and not str(extra.get("delta", "")).startswith("#")):
            _hex = resolve_color_delta(str(extra.get("delta", "")))
            if _hex is None:
                return self._miss(trace, f"色名不可解:{extra.get('delta')}")
            extra = {**extra, "delta": _hex}
        if intent == "AdjustDeviceAttribute" and str(extra.get("delta") or "").strip() in ("", "None"):
            # v1.1.1 对账：属性句缺 delta 一律如实 MISS（原形态产 args 无 delta
            # 槽 → 集成 slot_schema Required('delta') Invalid，等于白执行一次）。
            # 「把空调风速调大」这类 ② 剥离句 rest 只剩设备名，档位词在原句里，
            # 扫描器必须拿**整句**扫。
            _sc = _scan_delta(text, str(extra.get("attribute") or ""))
            if _sc is None:
                return self._miss(trace, f"Adjust 无可用数值:{rest_text or text}")
            extra = {**extra, "delta": _sc[0]}
            trace.append(f"数值回扫:{_sc[0]}")
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
            # 英文目标桥（见 targets.bilingual_targets）：英文 area/name 追加
            # 中文等价目标并集；纯中文句原样返回（零扰动）。
            args["target"] = T.bilingual_targets([entry] if entry else [])
        if "action" in extra:
            args["action"] = str(extra["action"]).lower()
        for k in ("attribute", "delta", "mode", "position", "speed", "strength"):
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
_WINDOW_TYPES = ("内开内倒窗", "外装平开窗", "单内倒窗", "内倒窗", "平推窗", "平开窗",
                 "推拉窗", "内开窗", "外开窗", "推拉门", "智能窗", "天窗",
                 "飘窗", "窗户",
                 # 2026-09 悬窗族 + 提升窗（用户点名「区域+窗户」机型）。_window_type
                 # 是**先命中先返回**的子串扫描，下悬窗/上悬窗/提升窗（含悬窗/升窗
                 # 形态）必须排在裸 悬窗 前，否则 "关闭下悬窗" 会被短词 悬窗 截胡。
                 "下悬窗", "上悬窗", "提升窗", "悬窗",
                  # 2026-09-30 数据集对账补口：电动窗（意图数据集窗型实测 x10，
                  # 六表从未收；与 targets 两表/集成两表三方同步，守卫钉）。
                  "电动窗")

# 帘族字（与 _POS_CURTAIN_WORDS 同集合）：窗型判定短路专用——「电动窗帘」
# 含窗型词根 电动窗，字面扫描必截胡；帘=cover 设备，误判成窗型即按窗钮
# （假动作+帘不动）。凡含帘族字的名词一律不进窗型纠正。
_CURTAIN_ROOT_WORDS = ("帘", "纱窗", "百叶")


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
    if any(w in n for w in _CURTAIN_ROOT_WORDS):
        return None                # 「电动窗帘」≠「电动窗」——帘族短路（09-30 对账批）
    for w in _WINDOW_TYPES:
        if w in n:
            return w
    return None


# 开窗器/开合器/推窗器：窗控机型的设备词（与 12 窗型同族，button 按压体系），
# 不是窗型细分——单独成表，免得污染 _WINDOW_TYPES↔KNOWN_DEVICES_PREFIX↔集成
# valid names 的三方一致性守卫钉。帘族一律排除。
_WINDOW_OPENER_WORDS = ("开窗器", "开合器", "推窗器")


def _opener_word(text: str) -> Optional[str]:
    t = str(text or "")
    if any(w in t for w in ("帘", "纱窗", "百叶")):
        return None
    for w in _WINDOW_OPENER_WORDS:
        if w in t:
            return w
    return None


def _any_action_match(rest: str) -> bool:
    return any(p.match(rest) for p, _, _ in _ACTION_PATTERNS)


def _to_int(delta: Any) -> int:
    try:
        return int(float(delta))
    except (TypeError, ValueError):
        return 0
