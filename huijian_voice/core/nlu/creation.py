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

# ①d 列表查询（v1.0.34 本地句）：列出场景 / 我有哪些自动化 / 场景都有什么 /
# 多少个场景。裸名词（"场景"俩字）不收——guard 在 parse。
# 2026-09-10 真机补洞：原式全锚定、词首只认「帮我|请」，用户实际说法带前导时间/
# 语气词时**从第一个字就不匹配**——「现在有哪些语音场景」整句落兜底（创建/触发
# 都正常，只有"看清单"问不出来）；同时补「有哪几个/有多少个」变体。
_LIST_RE = re.compile(
    r"^(?:(?:看看|看一下|查看看|查查)\s*)?"
    r"(?:现在|目前|当前|如今|请问|麻烦你?|我想知道|想知道)?\s*"
    r"(?:(?:帮我|请)\s*)?(?:列出|查看|查一下|查下|看看|读一下|说一下|告诉我|报一下|查)?\s*"
    r"(?:我)?(?:的)?(?:都)?(?:有(?:哪(?:些|几个)|什么|几个|多少(?:个)?))?个?\s*"
    r"(?:所有|全部)?\s*"
    r"(?P<what>语音场景|场景|语音自动化|自动化)\s*"
    r"(?:列表)?(?:都?(?:有哪些|有什么|是什么|有多少|有几个))?(?:呢|吗)?[？?。]?$")

# ①e 自动化删除（v1.0.34）：删除自动化[序号|关键词]；把自动化N删了。
# target 可空=裸删（pipeline 列编号让用户点选，不猜）。
_AUTO_DEL_RE = re.compile(
    r"^(?:(?:帮我|请)\s*)?(?:删除|删掉|移除|删)\s*(?:语音)?自动化\s*"
    r"(?:第)?\s*(?P<t>[\d一二三四五六七八九十]{1,3}[号]?"
    r"|[「\"『][^」\"』]{1,14}[」\"』]?|[^「」\"』，。;；\s]{1,14}[号条个]?)?\s*$"
    r"|^把\s*(?:语音)?自动化\s*(?:第)?\s*"
    r"(?P<t2>[\d一二三四五六七八九十]{1,3}|[^，。;；\s]{1,14})\s*"
    r"(?:号|个)?\s*(?:给我)?删(?:掉|了|除)\s*[。！!？?～~]?$")

# ①f 场景修改（v1.0.34）：把场景X改成/换成/修改为 + 新动作整句。
# Y 全量替换旧动作（预检白名单通过才动旧数据）。
_SCENE_MOD_RE = re.compile(
    r"^(?:(?:帮我|请|我要)\s*)?(?:把|将)?\s*(?:修改|更改|调整)?\s*"
    r"(?:语音)?场景\s*[「\"『]?(?P<x>[^」\"』，。;；\s]{1,12})[」\"『]?\s*"
    r"[，,、]?\s*(?:的?动作|的内容)?\s*"
    r"(?:改成|改为|换成|换做|修改为|修改成|变为|变成)\s*"
    r"(?:(?:就|帮我|请|要|给我)\s*)?(?P<y>\S(?:.*\S)?)$")

# ①h 裸删场景（本地闭环补全）：只说"删除场景/把场景删掉"不带名字——不猜、
# 不交由 LLM，本地列清单+编号引导（与裸删自动化同纪律；trigger_phrase=""）。
_SCENE_DEL_BARE_RE = re.compile(
    r"^(?:(?:帮我|请|我要)\s*)?(?:删除|删掉|移除|删)\s*(?:语音)?场景\s*(?:列表)?\s*$"
    r"|^把\s*(?:语音)?场景\s*(?:给我)?\s*删(?:掉|了|除)?\s*$")

# ①i 自动化修改（本地闭环补全）：把自动化N改成<新句> / 修改自动化N的动作，改成<Y>。
# Y 是否为完整条件句由 pipeline 判定：是→连触发条件一起换；只是动作→保留原条件。
_AUTO_MOD_RE = re.compile(
    r"^(?:(?:帮我|请|我要)\s*)?(?:把|将)?\s*(?:修改|更改|调整)?\s*(?:语音)?自动化\s*"
    r"[「\"『]?(?P<t>[^「」\"』，,。;；\s]{1,14}?)[」\"』]?\s*(?:这|那)?(?:一)?(?:条|个)?\s*"
    r"(?:的?(?:动作|内容|触发条件|条件|触发))?\s*[，,、]?\s*"
    r"(?:改成|改为|换成|换做|修改为|修改成|变为|变成)\s*"
    r"(?:(?:就|帮我|请|要|给我)\s*)?(?P<y>\S(?:.*\S)?)$")

_CN_IDX = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


# ①g 编号回指删除（v1.0.34）：删第2条 / 删除第三条 / 把第2个删了。
# 语义 = 上一次清单播报（场景或自动化）里的第 N 项，上下文由 pipeline 记。
_DEL_IDX_RE = re.compile(
    r"^(?:删除|删掉|移除|删)\s*第\s*(?P<n>[\d一二三四五六七八九十]{1,3})\s*(?:条|个|号)$"
    r"|^把\s*第\s*(?P<n2>[\d一二三四五六七八九十]{1,3})\s*(?:条|个|号)\s*"
    r"(?:给我)?删(?:掉|了|除)$")


def auto_target(raw: str):
    """自动化删除目标归一：None=裸删；int=序号；str=关键词。永不抛。"""
    s = (raw or "").strip().strip("「」『』\"'").strip()
    for suf in ("号", "条", "个"):
        if s.endswith(suf) and len(s) > 1:
            s = s[:-1]
    s = s[1:] if s.startswith("第") else s
    if not s:
        return None
    if s.isdigit():
        try:
            return max(1, int(s))
        except ValueError:
            return s
    if s in _CN_IDX:
        return _CN_IDX[s]
    if len(s) == 2 and s[0] == "十" and s[1] in _CN_IDX:
        return 10 + _CN_IDX[s[1]]          # 十一~十九
    return s

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


# 动词连排切分（v1.0.34 真机句式「…就帮我同时打开办公室的空调关闭办公室的
# 平台窗」——两动作间零标点）。只在**多字动词/客套词**前下刀：单字
# 开/关/调 可能落在「办公室/空调」等名词里，绝不作切点。
_SERIAL_CUT = re.compile(
    r"(?=帮我把|帮我|麻烦你|麻烦|请你|请|顺便|把|将|打开|关闭|关掉|关上|开启|"
    r"开一下|调到|调成|调节|调整|设为|设成|设定|设置|播放|停止|暂停|锁上|解锁|"
    r"拉上|拉下|全开|全关)")
_SERIAL_LEAD = re.compile(
    r"^(?:同时|另外|并且|而且|然后|接着|之后|一并|再|就)+")
# 真动词表（段有效性判定用）：切点组里的构式/客套词（把/将/请/帮我）不算
# 动词——否则把字句 "把空调设定为制冷" 会被拦腰误切。
_SERIAL_VERB = re.compile(
    r"打开|关闭|关掉|关上|开启|开一下|调到|调成|调节|调整|设为|设成|设定|"
    r"设置|播放|停止|暂停|锁上|解锁|拉上|拉下|全开|全关")
# 段头单字动词（名词内单字不作切点，但独立段以动词开头是合法动作）
_SERIAL_HEAD_V = re.compile(r"^[开关调设拉锁停]")


def _serial_expand(parts: list[str]) -> list[str]:
    """对每段做动词连排细分：['开A关B'] → ['开A','关B']。
    回退律（宁欠勿过）：任一子段剥去连接词/客套后**不含动词**（如
    把字句拦腰的 '空调'、'播放器' 名词碎段）→ 整段保原形交级联。"""
    out: list[str] = []
    for p in parts:
        subs = [x.strip(" 。") for x in _SERIAL_CUT.split(p) if x.strip(" 。")]
        fixed: list[str] = []
        broken = False
        for x in subs:
            x2 = _SERIAL_LEAD.sub("", _Y_POLITE.sub("", x))
            if not x2:
                continue                       # 纯客套/连接词残片（'帮我'）丢弃
            if len(x2) < 2:
                broken = True
                break
            if not (_SERIAL_VERB.search(x2) or _SERIAL_HEAD_V.match(x2)):
                broken = True                  # 名词碎段（无动词）→ 段无效
                break
            fixed.append(x2)
        if not broken and len(fixed) >= 2:
            out.extend(fixed)
        elif p and (fixed or len(_Y_POLITE.sub("", _SERIAL_LEAD.sub("", p))) >= 2):
            out.append(p)            # 纯客套残段（'帮我'）直接丢弃
    return out


def split_actions(y_text: str) -> list[str]:
    """动作子句切分（2~3 段封顶；任何子句 <2 字判非复合）。
    标点分句与无标点动词连排都支持。"""
    y_text = (y_text or "").strip().strip("。！？!?")
    if len(y_text) < 6:
        return [y_text] if y_text else []
    parts = [p.strip(" 。！？!?，,、") for p in _Y_SPLIT.split(y_text)]
    parts = [p for p in parts if p]
    parts = _serial_expand(parts)
    if len(parts) < 2:
        return [y_text]
    if len(parts) > 3 or any(len(p) < 2 for p in parts):
        return [y_text]        # 切形可疑：整句单发，交级联判定
    return parts


# ── 连排句判据（直接指令通道与创建通道共用；2026-09-10 真机实锤）──────
# 真机病灶：「关闭办公室射灯关闭办公室平开窗」只关了平开窗。此前连排切分只在
# 建场景的 Y 子句里用，直接指令通路完全没有——整句被 T0 当成**一句**，
# parse_target 把第一个子句吃成"区域名"残渣（"办公室射灯关闭办公室"），
# 于是只有名字匹配上的最后那个设备真的动了，还回「已经帮你执行了」。
_SERIAL_HEAD_RE = re.compile(
    r"^(?:打开|关闭|关掉|关上|开启|开一下|开到|关到|调到|调为|调成|调节|调整|"
    r"设为|设成|设定|设置|播放|停止|暂停|锁上|解锁|拉上|拉下|全开|全关|"
    r"开|关|调|设|拉|锁|停)")


def is_bare_verb_clause(clause: str) -> bool:
    """从句剥掉段首动词后没剩任何目标 → 纯动词残段（"关闭"/"打开"）。

    这种段**绝不能**进链发：单发会把它当"无目标=全屋"，于是
    「打开客厅的灯关闭」会变成"开客厅灯 + 全屋关灯"（比切分失败危险得多）。"""
    return not _SERIAL_HEAD_RE.sub("", clause or "", count=1) \
        .strip(" 了吧呢啊哦呀的").strip()


def serial_clauses(text: str) -> list[str]:
    """无连接词的动词连排句切分（"关闭办公室射灯关闭办公室平开窗"→两段）。

    复用 split_actions 的切分纪律（_SERIAL_CUT 动词边界切、真动词表判段有效性、
    拿不准整段回退），再补"是否值得链发"的把关：<2 段、或任一段是纯动词残段，
    一律返回 [] —— 宁欠勿过，切形可疑就让上层按原样处理（单发通路另有一道
    "连排句不执行"的闸，见 fast_path.match）。
    """
    try:
        parts = [p for p in split_actions(text) if p and p.strip()]
    except Exception:          # noqa: BLE001 —— 切分永不冒泡（fail-open 回单发）
        return []
    if len(parts) < 2 or any(is_bare_verb_clause(p) for p in parts):
        return []
    return parts


def parse(text: str) -> Optional[dict[str, Any]]:
    """解析场景/自动化生命周期句式。返回 kind：
      {kind:"scene", trigger_phrase, y}                     创建语音场景
      {kind:"automation", trigger:{...}, desc, y}           创建语音自动化
      {kind:"delete_scene", trigger_phrase}                 删除语音场景
      {kind:"modify_scene", trigger_phrase, y}              改语音场景动作
      {kind:"modify_automation", target, y}                 改自动化（y=新条件句或仅新动作）
      {kind:"list_scenes"|"list_automations"}               列出
      {kind:"delete_automation", target:raw}                删自动化(序号/关键词/裸)
      {kind:"delete_index", n:int}                          「删第N条」回指上次清单
    trigger 形态：{entity_id:str descriptor, above|below:float}
                / {entity_id, to:"on"|"off"} / {at:"HH:MM"}
    不匹配任何句式 → None（交回级联，行为零变化）。永不抛。
    """
    t = (text or "").strip()
    m = _SCENE_DEL_RE.match(t)                    # 删除句先过闸（不含"当"系字）
    if m:
        x = (m.group("x") or m.group("x2") or "").strip()
        if x in ("给我", "我", "它", "吧"):        # "把场景给我删了" 实为裸删
            return {"kind": "delete_scene", "trigger_phrase": ""}
        if 1 <= len(x) <= 12:
            return {"kind": "delete_scene", "trigger_phrase": x}
        return None
    if _SCENE_DEL_BARE_RE.match(t):               # 裸删：本地列清单+编号引导
        return {"kind": "delete_scene", "trigger_phrase": ""}
    m = _SCENE_MOD_RE.match(t)                    # 改场景（v1.0.34）
    if m:
        x, y = m.group("x").strip(), _clean_y(m.group("y"))
        if 1 <= len(x) <= 12 and len(y) >= 2:
            return {"kind": "modify_scene", "trigger_phrase": x, "y": y}
        return None
    m = _AUTO_MOD_RE.match(t)                     # 改自动化（本地闭环补全）
    if m:
        raw = (m.group("t") or "").strip().strip("这那")
        y = _clean_y(m.group("y"))
        if raw and raw not in ("的", "动作", "内容", "条件", "触发条件") and len(y) >= 2:
            return {"kind": "modify_automation", "target": raw, "y": y}
        return None
    m = _AUTO_DEL_RE.match(t)                     # 删自动化（v1.0.34）
    if m:
        raw = (m.group("t") or m.group("t2") or "").strip()
        if auto_target(raw) is None and not raw:
            return {"kind": "delete_automation", "target": ""}   # 裸删→列编号
        return {"kind": "delete_automation", "target": raw}
    m = _LIST_RE.match(t)                         # 列出（v1.0.34）
    if m:
        what = m.group("what")
        core = t.strip().strip("。？?！!").lstrip("我").lstrip("的")
        if core in (what, what + "列表"):         # 裸名词不接（防"场景"两字触发）
            return None
        return {"kind": "list_automations" if "自动化" in what else "list_scenes"}
    m = _DEL_IDX_RE.match(t)                      # 删第N条（回指上次清单）
    if m:
        n = auto_target("第" + (m.group("n") or m.group("n2")))
        if isinstance(n, int):
            return {"kind": "delete_index", "n": n}
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
