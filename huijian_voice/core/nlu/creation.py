"""「当我说X，就Y」/「当[事件]，就Y」语音创建句式承接（零 LLM，v1.0.30）。

以 060401 慧尖集成为基础（VoiceSceneStore/AutomationStore 执行引擎原样复用），
本层补的是旧架构空白——旧实现这两句式的**解析**归 LLM function calling
（custom_llm_api 时代），离线默认态无入口。收编级联后本地模板直接产出
HassCreateVoiceScene / HassCreateAutomation 的 Plan，入库即生效。

对旧集的三处优化（用户令"在060401基础上优化"）：
  1. 事件触发不再折进语音场景（旧 SFT 集把「温度超28度」错注册成
     trigger_phrase=高温开空调 的死场景）——真走 HassCreateAutomation；
  2. 旧自动化引擎只认数值穿越（人体"有人/没人"float 化失败恒不触发），
     本层产出 state(to) 触发，集成侧 trigger_eval 配套支持；
  3. 时间触发（"每天早上7点开窗帘"）旧集成完全不支持，新增 at="HH:MM"。

设计纪律：
  · 纯解析、永不抛、不执行——动作子句 Y 由 pipeline 喂回 fast_path 级联
    解析（"能执行的句子才能进场景"），任一子句听不懂即**整单拒绝**并播报
    示范话术，绝不建半成品（用户"真栈实证不猜"铁律）；
  · ASR 无标点：不依赖逗号，靠「就/帮我/请/要」语义锚点切分；
  · 动作白名单与集成侧 _execute_intent 六 intent 完全对齐，闭环。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from . import targets as T

# 可注册进场景/自动化的动作意图——与集成侧 intent_voice_scene._execute_intent
# 及 intent_automation._execute_actions 的六类白名单逐字对齐，多余意图拒收。
ACTIONABLE_INTENTS = frozenset({
    "TurnDeviceOn", "TurnDeviceOff", "ControlWindow",
    "AdjustDeviceAttribute", "SetDeviceMode",
})

# 创建语句元前缀（"帮我创建一个语音场景，当我说晚安就关灯"形态）
_META_PREFIX = re.compile(
    r"^(?:帮我|请|我要|我想)?\s*(?:创建|设置|添加|新建|建立)(?:一个|个)?"
    r"(?:语音)?(?:场景|自动化|智能场景|智能)?\s*[，,、:：的]?\s*")

# ① 语音场景：当我说X（的时候）就/帮我Y
_SCENE_RE = re.compile(
    r"^当我说(?P<x>[^，,。;；]{1,12}?)(?:的)?(?:时候|时|后)?"
    r"[，,、\s]*(?:就|帮我|请|要|给|把)(?P<y>.+)$")

# ①b 无连接词变体：ASR 常吞"就"（"当我说晚安关卧室灯"）——Y 必须以动作字
# 开头才认，防"当我说晚安的时候"这类半句被吞成场景。
_SCENE_RE2 = re.compile(
    r"^当我说(?P<x>[^，,。;；]{1,12}?)(?:的)?(?:时候|时|后)?"
    r"[，,、\s]*(?P<y>打开|关闭|关掉|开了|关了|开|关|调|拉|锁|解|启动|停止|播放)(?P<y2>.+)$")

# ①c 场景删除（v1.0.33 本地句）：删除/删掉/移除 + 场景X；把场景X删掉。
# 只认带名字的精准删除——裸"删除场景"不接（回原流程，LLM 通道列清单）。
_SCENE_DEL_RE = re.compile(
    r"^(?:(?:帮我|请|我要)\s*)?(?:删除|删掉|移除|删)\s*(?:语音)?场景\s*"
    r"[「\"『]?(?P<x>[^「」\"』，。;；\s]{1,12})[」\"』]?$"
    r"|^把\s*(?:语音)?场景\s*[「\"『]?(?P<x2>[^「」\"』，。;；\s]{1,12})"
    r"[」\"』]?\s*(?:给我)?删(?:掉|了|除)$")

# ② 自动化·数值阈值：当<设备/区域+属性> 超过/低于 <数值>[度|%]（时）就Y
_AUTO_NUM_RE = re.compile(
    r"^(?:当|如果|要是|假如)(?P<d>[^，,。;；就]{2,14}?)"
    r"(?P<op>超过|大于等于|大于|高于|达到|低过|低于等于|低于|小于等于|小于|低到)"
    r"\s*(?P<n>[0-9]+(?:\.[0-9]+|[点][0-9]+)?|[零一二三四五六七八九十百点两]+)\s*"
    r"(?:度|摄氏度|℃|%|％|勒克斯|lux|LUX|个)?(?:的)?(?:数值|读数)?"
    r"(?:的)?(?:时候|时|后)?[，,、\s]*(?:就|帮我|请|要|给)(?P<y>.+)$")

# ③ 自动化·状态（人体感应等）：当<区域>检测到有人/没人（时）就Y
_AUTO_STATE_RE = re.compile(
    r"^(?:当|如果|要是|假如)(?P<d>[^，,。;；就]{1,14}?)"
    r"(?:传感器|感应器)?(?:检测到|感应到|探测到|发现)?(?:有)?(?P<s>人|无人|没人)"
    r"(?:活动|经过|移动|走动)?(?:的)?(?:时候|时|后)?[，,、\s]*"
    r"(?:就|帮我|请|要|给)(?P<y>.+)$")

# ④ 自动化·时间：每天（早上）7点（半/15分）（都）就/帮我Y
_AUTO_TIME_RE = re.compile(
    r"^每天(?P<am>早上|上午|中午|凌晨|下午|晚上)?\s*"
    r"(?P<h>[0-9]{1,2}|[零一二两三四五六七八九十]+)\s*点\s*"
    r"(?P<m>半|[0-9一二三四五六七八九十]{1,3}分|一刻|三刻)?(?:的)?(?:时候|时)?"
    r"[，,、\s]*(?:就|帮我|请|要|给|把)?(?P<y>.+)$")

# 动作子句强连接词（并/并且/然后/再/同时/、）——比 _COMPOUND 宽，创建专用；
# "再"不要求前置逗号（X 场景里"关窗帘再开灯"无歧义）。
_Y_SPLIT = re.compile(
    r"\s*(?:并且|并|然后(?:再|把)?|接着|之后|同时|再|[，,、])\s*")

_CN_UNIT_STRIP = re.compile(r"(度|摄氏度|℃|%|％)$")
_Y_POLITE = re.compile(r"^(?:帮我|请|要|来|给我|麻烦)+")


def _clean_y(y: str) -> str:
    """连接词后残留敬语剥离（"就帮我打开空调"→"打开空调"）。"""
    return _Y_POLITE.sub("", (y or "").strip().strip("。！？!?")).strip()


_CN_DIGITS = "零〇一二两三四五六七八九"


def _num(raw: str) -> Optional[float]:
    """阿拉伯/中文数字 → float。实测 cn2num 不支持小数（"二十六点五"会被
    静默截成 20——比失败更危险），这里按"点"自拆：整数部分走 cn2num，
    小数位逐字翻译。永不抛。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    if "点" in raw:
        head, _, tail = raw.partition("点")
        ip = _num(head) if head else 0.0
        if ip is None or ip != int(ip):
            return None
        frac = ""
        for ch in tail:
            if ch.isdigit():
                frac += ch
            elif ch in _CN_DIGITS:
                got = T.cn2num(ch) or ""
                if not got.isdigit():
                    return None
                frac += got
            else:
                return None
        return int(ip) + float("0." + frac) if frac else None
    try:
        return float(T.cn2num(raw))
    except Exception:
        return None


def _hour_minute(h_raw: str, am: str, m_raw: str) -> Optional[str]:
    """(7, 晚上, 半) → "19:30"。越界/歧义返回 None（拒建不猜）。"""
    h = _num(h_raw)
    if h is None or not 0 <= h <= 23:
        return None
    h = int(h)
    if m_raw == "半":
        m = 30
    elif m_raw in ("一刻", None, ""):
        m = 15 if m_raw == "一刻" else 0
    elif m_raw == "三刻":
        m = 45
    else:
        mv = _num(str(m_raw).rstrip("分"))
        if mv is None or not 0 <= mv <= 59:
            return None
        m = int(mv)
    if am in ("下午", "晚上") and h <= 11:
        h += 12
    if am == "中午" and h < 11:
        h = 12
    if am == "凌晨" and h == 12:
        h = 0
    if h > 23:
        return None
    return f"{h:02d}:{m:02d}"


def split_actions(y_text: str) -> list[str]:
    """动作子句切分（2~3 段封顶；任何子句 <2 字判非复合）。"""
    y_text = (y_text or "").strip().strip("。！？!?")
    if len(y_text) < 6:
        return [y_text] if y_text else []
    parts = [p.strip(" 。！？!?，,、") for p in _Y_SPLIT.split(y_text)]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return [y_text]
    if len(parts) > 3 or any(len(p) < 2 for p in parts):
        return [y_text]        # 切形可疑：整句单发，交级联判定
    return parts


def parse(text: str) -> Optional[dict[str, Any]]:
    """解析创建/删除句式。返回：
      {kind:"scene", trigger_phrase, y}                     语音场景
      {kind:"automation", trigger:{...}, desc, y}           语音自动化
      {kind:"delete_scene", trigger_phrase}                 删除语音场景
    trigger 形态：{entity_id:str descriptor, above|below:float}
                / {entity_id, to:"on"|"off"} / {at:"HH:MM"}
    不匹配任何句式 → None（交回级联，行为零变化）。永不抛。
    """
    t = (text or "").strip()
    m = _SCENE_DEL_RE.match(t)                    # 删除句先过闸（不含"当"系字）
    if m:
        x = (m.group("x") or m.group("x2") or "").strip()
        if 1 <= len(x) <= 12:
            return {"kind": "delete_scene", "trigger_phrase": x}
        return None
    if len(t) < 5 or not any(k in t for k in ("当", "每天", "如果", "要是", "假如")):
        return None
    t = _META_PREFIX.sub("", t, count=1).strip()
    if not t:
        return None

    m = _SCENE_RE.match(t)
    if m:
        x = m.group("x").strip().rstrip("的")
        y = _clean_y(m.group("y"))
        if len(x) >= 1 and len(y) >= 2:
            return {"kind": "scene", "trigger_phrase": x, "y": y}
        return None
    m = _SCENE_RE2.match(t)
    if m:
        x = m.group("x").strip().rstrip("的")
        y = _clean_y(m.group("y") + m.group("y2"))
        if len(x) >= 1 and len(y) >= 2:
            return {"kind": "scene", "trigger_phrase": x, "y": y}
        return None

    m = _AUTO_NUM_RE.match(t)
    if m:
        n = _num(m.group("n").strip())        # "点5"=0.5 由 _num 自拆，勿再剥
        d = m.group("d").strip().lstrip("的")
        y = _clean_y(m.group("y"))
        if n is None or not d or len(y) < 2:
            return None
        op = m.group("op")
        key = "above" if op in ("超过", "大于", "大于等于", "高于", "达到", "低到") and "低" not in op \
            else "below" if "低" in op or "小" in op else "above"
        # 达到 → above（越限即触发语义同超）；低到=低于 方言形
        if op == "低到":
            key = "below"
        return {"kind": "automation", "desc": d, "trigger": {"entity_id": d, key: n}, "y": y}

    m = _AUTO_STATE_RE.match(t)
    if m:
        d = m.group("d").strip().lstrip("的")
        s = m.group("s")
        y = _clean_y(m.group("y"))
        if len(y) < 2:
            return None
        if not d:
            d = "人体"
        # 区域级描述补"人体"关键词，喂集成侧 device_class=motion 猜测（060401
        # _resolve_entity_id 的 hints 表按 entity_id 字样猜类别）
        if not any(k in d for k in ("人体", "运动", "移动", "传感", "感应", "雷达")):
            d = d + "人体"
        to = "off" if s in ("无人", "没人") else "on"
        return {"kind": "automation", "desc": d, "trigger": {"entity_id": d, "to": to}, "y": y}

    m = _AUTO_TIME_RE.match(t)
    if m:
        hm = _hour_minute(m.group("h"), m.group("am") or "", m.group("m") or "")
        y = _clean_y(m.group("y"))
        if hm is None or len(y) < 2:
            return None
        desc = f"每天{m.group('am') or ''}{m.group('h')}点"
        return {"kind": "automation", "desc": desc, "trigger": {"at": hm}, "y": y}

    return None
