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

logger = logging.getLogger("huijian.fastpath")

# ── 动作词匹配规则（v1.5 L207-255 逐字移植；(pattern, intent, action_val)）──
_ACTION_PATTERNS: list[tuple[re.Pattern, str, Any]] = [
    (re.compile(r"^(开窗|打开窗|开窗户|打开窗户|窗户打开)"), "ControlWindow", "open"),
    (re.compile(r"^(关窗|关闭窗|关窗户|关闭窗户|窗户关闭)"), "ControlWindow", "close"),
    (re.compile(r"^(内倒|内导|内岛|内到|内道|内达|内打|内大|内藻)"), "ControlWindow", "A"),
    (re.compile(r"^(暂停|停止|停)"), "ControlWindow", "pause"),
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
    if re.search(r"(状态|情况|哪些|所有.*(?:灯|设备|开关)|列表)", t):
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


class FastPath:
    def __init__(self, scenes, textcnn, settings):
        self.scenes = scenes
        self.textcnn = textcnn
        self.settings = settings

    # ── 主入口 ──────────────────────────────────────────────────
    async def match(self, raw_text: str) -> Optional[Plan]:
        trace: list[str] = []
        text = _extract_text(raw_text)
        text = corrector.apply(text, self.settings.get("nlu.corrections_extra") or {})
        if not text or len(text.strip()) < 2:
            return None
        text = text.strip()
        text = re.sub(r"^[把将]\s*", "", text)   # 处置介词核心化："把灯打开"→"灯打开"
        trace.append(f"纠错→{text}")
        if _is_complex_query(text):
            trace.append("复杂查询守卫→交上层")
            plan_src = None
            # 场景触发词是「当我说X」创建后的短语，仍允许短等值命中
            phrase = self.scenes.check(text)
            if phrase and phrase == text:
                plan_src = phrase
            if not plan_src:
                return self._miss(trace)
            return await self._scene_plan(plan_src, text, trace)

        await self.scenes.refresh()

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
        return self._build_plan(matched_intent, rest_text, extra_args, text, source, trace)

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
        # 空调区域守卫（原 L662-668）
        if name and any(k in name.lower() for k in ("空调", "空調", "aircondition")):
            if not area:
                return self._miss(trace, "空调缺区域信息")
        args: dict[str, Any] = {}
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
        for k in ("attribute", "delta", "mode"):
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
