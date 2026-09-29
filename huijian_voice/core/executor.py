"""执行层：Plan → POST /api/intent/handle → 中文话术。

话术映射依据《语音集成源码盘点》§3.1/§4 与收编 _friendly_text：
- huijian_ai handler 返回裸 dict：{success, control_targets:[{name,area}]} /
  {success, message} / {success:False, error:"英文"}（窗口抽取失败等）。
- 锁语义反转修复（D7 加载项侧兜底）：控制目标名词含「锁」时，TurnDeviceOn 播报
  「已上锁」、TurnDeviceOff 播报「已解锁」（handler 无 domain 字段，取设备名启发）。
- 英文 error 中文化（D5）：常见失败短语映射，未知失败给可复述的通用短句。
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from . import capability
from .nlu.schema import ATTR_CN, ATTR_TO_WIRE
from .nlu.fast_path import Plan, color_word, is_pronoun, normalize_polite

logger = logging.getLogger("huijian.executor")

ACT_CN = {"open": "打开", "close": "关闭", "pause": "暂停", "a": "内倒", "tilt": "内倒"}
# ATTR_CN 已上收到契约单点（core/nlu/schema.py），本模块直接引用。

# 出站属性名映射（v1.1.1 #2）：网关内部属性名 ≠ 集成注册表名。
# color_temperature / colour_temperature 在集成 register_adjustment 里**从未
# 注册**（light 色温的注册名是 temperature）⇒ 「色温调到4000K」必 unsupported，
# 三日实锤死字段。内部名不能就地改：temperature 在网关侧是**空调℃口径**
# （_T0_ATTR_WORD / H1 改道 / 播报"调到X度"三分支全按 color_temperature 挑
# 「色温/调冷调暖」），改名即串台。故只在**上 wire 前**单点映射，
# Plan.args 与话术层一律不动。
_ATTR_WIRE = ATTR_TO_WIRE        # 契约单点见 core/nlu/schema.py（v1.1.3 收口）


def wire_args(name: str, args: dict) -> dict:
    """执行出站参数归一（当前仅 AdjustDeviceAttribute 属性名对注册表）。永不抛。"""
    try:
        if name != "AdjustDeviceAttribute" or not isinstance(args, dict):
            return args
        mapped = _ATTR_WIRE.get(args.get("attribute"))
        if not mapped:
            return args
        out = dict(args)
        out["attribute"] = mapped
        return out
    except Exception:  # noqa: BLE001 —— 归一故障退回原参数（宁 unsupported 不猜）
        logger.exception("[执行] 出站属性名归一异常")
        return args
MODE_CN = {"heat": "制热", "cool": "制冷", "dry": "除湿", "fan_only": "送风", "auto": "自动",
           "eco": "节能", "sleep": "睡眠", "offline": "关闭",
           "comfort": "舒适", "silent": "静音", "boost": "强力", "normal": "标准"}
_EN_ERR_MAP = [
    ("could not extract window name", "没找到要控制的窗户，试试说「客厅的窗户内倒」"),
    ("window control failed", "窗户控制没成功，可能窗户没在 HA 里配好"),
    # v1.0.71（开错房间事故）：集成如实失败句「Could not find open button for X
    # in Y」旧表不认，播报被截成英文残句「（Could not find op」——现场实锤。
    ("could not find", "没找到要操作的窗户——请确认房间名和窗型叫法（如「办公室平开窗」）"),
    ("no available", "没找到符合条件的设备，试试带上房间名或换个叫法"),
    # v1.1.17：逐台回执里的能力类真原因（"does not support set_cover_position"）此前
    # 落进兜底模板，与"没听清"同形——用户重说十遍也没用。这类错是**设备做不到**。
    ("does not support", "这台设备不支持这个操作，换个说法或换台设备试试"),
    ("ha 内部错误", "慧尖 AI 集成还没生效——若是首次使用，请先安装集成（设备与服务→添加集成）并完成一次设备配对；若是刚升级，请在 Supervisor 重启（或重载）HA Core 再试"),
    ("unknown intent", "还没安装或加载慧尖 AI 集成——设备执行能力由集成提供，请先安装集成并配对一台设备"),
    ("no match", "没找到符合条件的设备"),
    ("not found", "没找到这个设备"),
    ("entity", "设备清单里没匹配到，请换个叫法试试"),
    ("timeout", "执行超时了，请再试一次"),
    ("unauthorized", "HA 令牌权限不足，请在加载项检查集成安装"),
]


def zh_error(raw: str, klar: bool = False) -> str:
    """英文错误 → 中文播报。klar=True（引擎直调/内置意图通道）时，禁止把
    失败归因到「慧尖集成没生效」——该通道与集成无关，2026-09-08 实机误报：
    Supervisor 代理 5xx 被播成了集成话术，把用户引去装集成。"""
    low = (raw or "").lower()
    for key, zh in _EN_ERR_MAP:
        if key in low:
            if klar and "集成" in zh:
                return ("抱歉，和 Home Assistant 的连接没有走通，这次没有执行。"
                        "请检查 HA 核心是否正常运行、加载项 API 地址配置是否正确")
            return f"抱歉，{zh}"
    # v1.0.87（现场 13:06:37 案）：结果**不确定**（超时/连接/5xx）时断言"没有
    # 执行成功"是假确定——同一盏 ZHA 灯在 13:06:42.349 迟到的 500 证明命令其实
    # 落了地。假确定的代价很实际：用户听"没成功"就再说一遍 = 手动二次动作，
    # 而系统侧刚被闸成"不自动重放"（见 pipeline 级联闸）。话术必须与判据同源：
    # 说"没拿到回执、可能已动作"，并把是否重试的决定权明明白白交回用户。
    if is_indeterminate(raw):
        return ("这一步没拿到执行回执，设备可能已经动作了——"
                "为防重复执行，我不自动再试；确认要再来一遍请再说一次")
    detail = (raw or "").strip()[:30]
    if not detail:
        # v1.0.34（审查 L5）：无原因可给时别播空括号「（）」
        return "抱歉，这一步没有执行成功，可以换个说法再试"
    return f"抱歉，这一步没有执行成功（{detail}），可以换个说法再试"


# ── klar 直调路径：目标词回显（2026-09-14 用户拍板）────────────────
# 引擎 zh_cn 语料把「灯」写成拼音占位（speech.rs:45 area_light="deng {loc}"），
# 出口清洗只能删引导词、补不出主语（klar_client.fix_zh_pinyin）→ 播报成
# 「办公室开了」这种缺主语病句。标准开关族从此不采信引擎泛化话术，改为回显
# **用户自己说的那个词** + 方向动词：说「打开办公室射灯」→ 播「射灯开了」。
# 词来自原话，一定听得懂；不依赖引擎 speech、不查 entity_registry、不带拼音。
# 带数值的意图（亮度/温度/开合度/风量）仍用引擎话术——数值只在那里。
_KLAR_ECHO_VERB = {
    "HassTurnOn": "开了", "HassTurnOff": "关了", "HassToggle": "切换了",
    "HassLock": "上锁了", "HassUnlock": "解锁了",
}
# D7 锁语义反转（与 _klar_direct 同向）：目标是 lock 域时"打开"=上锁
_KLAR_ECHO_LOCK = {"HassTurnOn": "上锁了", "HassTurnOff": "解锁了"}
# 动作/语气词**只剥首尾（锚定）**，不吃句中——防「关灯助手」这类设备名被咬掉；
# 裸「开/关」再加邻字护栏，防「开关面板 → 关面板」「灯开关 → 灯」把名词啃残
# （长形态 打开/关闭/关掉/开了 排在交替式前，天然优先）。
_ECHO_PREP = re.compile(r"^(?:把|将|给|帮我把|帮我将)")
_ECHO_HEAD = re.compile(
    r"^(?:打开来|打开|关闭|关掉|开了|关了|开一下|关一下|开启|关上|启动|停止|解锁|开锁|锁上|落锁|上锁|切换(?!器)|开(?!关)|关|换|锁)")
_ECHO_TAIL = re.compile(
    r"(?:打开来|打开|关闭|关掉|开一下|关一下|开启|关上|起来|启动|停止|解锁|开锁|锁上|落锁|上锁|(?<!开)关|开)+$")
_ECHO_TONE = re.compile(r"(?:了吧|啦|咯|了|吧|呢|呀|啊|哦|嘛|都|全部|全)+$")
# 复合/连接残留：多目标回显会错指，交回原路径（多分句另有链话术）
_ECHO_MULTI = re.compile(r"[和与跟]|还有|然后|接着|顺便|并且|同时")


def echo_target(utterance: str, area: str = "") -> str:
    """用户原话 → 目标词。「打开办公室射灯」→「射灯」；拿不准返回 ""。永不抛。"""
    try:
        t = re.sub(r"[\s。，,！!？?~～]+", "", normalize_polite((utterance or "").strip()))
        for _ in range(3):
            prev = t
            t = _ECHO_TONE.sub("", _ECHO_TAIL.sub("", _ECHO_HEAD.sub(
                "", _ECHO_PREP.sub("", t, count=1), count=1)))
            if t == prev:
                break
        area = (area or "").strip()
        if area and area in t:
            t = t.replace(area, "", 1)
        t = re.sub(r"^(?:里面的?|里|内的?|的)+", "", t)
        t = _ECHO_TONE.sub("", t)
        if not (1 <= len(t) <= 8) or is_pronoun(t) or _ECHO_MULTI.search(t):
            return ""
        # 必须是实义名词（含汉字/字母/数字），且没被剥成动词残片
        return t if re.search(r"[\u4e00-\u9fffA-Za-z0-9]", t) else ""
    except Exception:  # noqa: BLE001 —— 话术层任何意外都不该伤语音链
        logger.exception("[执行] 目标词回显解析异常 → 沿用原话术")
        return ""


_INDETERMINATE_HINTS = (
    "timeout", "timed out", "超时", "connect", "connection", "连接",
    "network", "网络", "502", "503", "504", "500",
)


def is_indeterminate(err: str) -> bool:
    """失败原因是否"结果不确定"：超时/连接断开/5xx——HA 侧可能已经执行，只是
    回执在路上丢了。这类失败**绝不能**让 LLM 拿原句复议重做（相对量动作会叠加
    第二遍），是"LLM 只做兜底、不与本地执行冲突"的关键判据。"""
    low = (err or "").lower()
    return any(h in low for h in _INDETERMINATE_HINTS)


# 链式分句真伪判定用（见 Executor._leg_truth）：只对"状态名就是 on/off/locked"的域判空操作。
# v1.1.17 复审补漏：慧尖自有意图名是 `TurnDeviceOn/Off`（fast_path.py:240 / agent.py），
# `HassTurnOn/Off` 是 klar/HA 内置那套——旧表只收后者，于是**字面表主形状（target 形）
# 一上来就被判据以"意图不认识"放行**，target 支在生产里等于死码。两族都收，并把锁族
# 纳入（锁的逐台 rows 是硬编码 success=True，intent_lock.py:85，只有快照能证伪）。
_LEG_DESIRED_STATE = {"HassTurnOn": ("on",), "HassTurnOff": ("off",),
                      "TurnDeviceOn": ("on",), "TurnDeviceOff": ("off",),
                      "HassLock": ("locked",), "HassUnlock": ("unlocked",)}
_LEG_NOOP_DOMAINS = {"light", "fan", "switch", "humidifier", "input_boolean", "lock"}
# 开合类意图的目标态按 action 取（v1.1.15 收口批）：ControlWindow 不进上表是因为
# 它的"要求状态"藏在参数里。只用于**查无此名/已在要求态**的证伪，空白 action（stop 等）
# 一律不判。
_LEG_WINDOW_DESIRED = {"open": ("open",), "close": ("closed",), "closed": ("closed",)}


# 「动作=上锁」的意图集（v1.1.29 复核 A1）：TurnDeviceOn×lock 与 HassLock/
# HassTurnOn 同义（集成 intent_turn.py:288 on=lock）——漏了它 ⇒ 上锁成功反报
# 「还没确认到已解锁」、真没锁上反被静默放行。TurnDeviceOff/HassTurnOff/
# HassToggle 在 lock 域都落 unlock（同文件 else 支）⇒ 不在此列。
_LOCK_WANT_LOCKED = ("HassLock", "HassTurnOn", "TurnDeviceOn")


def _lock_domain_target(args: dict) -> bool:
    """目标是否为 lock 域（v1.1.19：锁确证按域判，不看意图名）。永不抛。"""
    try:
        raw = (args or {}).get("entity_id")
        eids = [raw] if isinstance(raw, str) else [
            e for e in (raw or []) if isinstance(e, str)]
        if any(str(e).split(".", 1)[0] == "lock" for e in eids):
            return True
        for slot in ((args or {}).get("target") or []):
            for d in ((slot or {}).get("devices") or []):
                if "lock" in ((d or {}).get("domains") or []):
                    return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _leg_want(name: str, args: dict):
    """本步"要求的状态"集合；None＝该意图不判（口径见 _leg_truth 的 docstring）。"""
    if name == "ControlWindow":
        act = str((args or {}).get("action") or "").strip().lower()
        return _LEG_WINDOW_DESIRED.get(act)
    return _LEG_DESIRED_STATE.get(name)


class Executor:
    def __init__(self, ha, settings=None):
        self.ha = ha
        self.settings = settings
        # 最近一次 run 的执行状态（pipeline 复议安全闸读它；永不作为业务返回值，
        # 免得动 run 的 (ok, speech) 契约把既有调用点/测试全推翻）。
        self.last_run: dict = {"steps": 0, "applied": 0, "indeterminate": False}
        # 能力预检"整体放行"的分因闩锁（见 _capability_refuse）
        self._gate_blind_warned = False
        # 本轮"代打"留痕（收口批）：目标离线→同名改指过的 (设备名, 改指目标区域)，
        # 按轮复位，由 _named() 落到播报——绝不静默换设备（见 _repoint_offline_twin）。
        self._repointed: list[tuple[str, str]] = []

    async def run_raw(self, plan: Plan) -> tuple[bool, dict]:
        """单步意图执行，返回 (success, 原始 result dict)——供列表类意图
        （HassListAutomations 等）读结构化数据。永不抛，失败也带回 error dict。"""
        try:
            result = await self.ha.handle_intent(plan.intent,
                                                 wire_args(plan.intent, plan.args))
        except Exception as e:
            logger.info("[执行raw] %s 异常: %s", plan.intent, e)
            self.last_run = {"steps": 1, "applied": 0,
                             "indeterminate": is_indeterminate(str(e))}
            return False, {"success": False, "error": str(e)[:120]}
        if not isinstance(result, dict):
            self.last_run = {"steps": 1, "applied": 0, "indeterminate": False}
            return False, {"success": False, "error": "bad response"}
        ok = bool(result.get("success"))
        self.last_run = {"steps": 1, "applied": 1 if ok else 0,
                         "indeterminate": False if ok else is_indeterminate(
                             str(result.get("error") or result.get("message") or ""))}
        return ok, result

    async def _capability_refuse(self, name: str, args: dict) -> Optional[str]:
        """v1.1.3 P0-2 只读预裁：拿本家实体真实能力判这条命令能不能成立。

        只拒不改写；拿不到候选实体（注册表未同步/无匹配）一律放行，让集成端
        按它自己的口径判——网关不越权凭空拒掉本来能做的动作。永不抛。

        v1.1.17 收口：**entity_id 形也进矩阵**。旧形态第一行 `tgt = args.get("target")`，
        不是 list 即 return None ⇒ 引擎 grounded 的腿整条从能力预裁旁边走过去
        （开关类由 _turn_gate 兜住，**属性能力**无人兜：纯 on/off 灯"调到 50%"照样下发）。
        与 _availability_refuse / _leg_truth_by_entity 同口径：候选按 eid 直取快照，
        取不到即放行。
        """
        try:
            tgt = (args or {}).get("target")
            has_tgt = isinstance(tgt, list) and bool(tgt)
            raw = (args or {}).get("entity_id")
            eids = [raw] if isinstance(raw, str) and "." in raw else [
                e for e in (raw or []) if isinstance(e, str) and "." in e]
            if not has_tgt and not eids:
                return None
            states = await self.ha.states()
            if not states:
                # 放行是对的（桥不通时拒=凭空少做），但放行意味着这条动作
                # **没过任何能力检查**。不留痕的话，v1.0.69 那类"假成功 +
                # ServiceNotSupported 风暴"会重新变成无迹可查。
                if not self._gate_blind_warned:
                    self._gate_blind_warned = True
                    logger.warning("[执行] HA 状态读空，本条及后续能力预检整体放行"
                                   "（未做能力检查）；reachable=%s last_error=%s",
                                   getattr(self.ha, "reachable", None),
                                   getattr(self.ha, "last_error", ""))
                return None
            self._gate_blind_warned = False
            if has_tgt:
                cands = capability.resolve_candidates(
                    states, getattr(self.ha, "_entity_area", {}) or {}, tgt)
            else:
                cands = [states[e] for e in eids if e in states]
            return capability.gate(name, args, cands)
        except Exception:  # noqa: BLE001 裁决故障=放行（宁多发一次，绝不少做）
            logger.exception("[执行] 能力预裁异常（放行）")
            return None

    async def _availability_refuse(self, name: str, args: dict) -> Optional[str]:
        """v1.1.7（办公室实锤 light.she_deng unavailable 谎报「客厅的灯关了」）：
        klar grounded entity_id 计划执行前查目标实体可用态。

        病灶：HA 对 unavailable 实体的 service call **照样回 success**（空操作），
        而 klar 直调走 call_service、结果无 per-entity `states`，_receipt 回落顶层
        success ⇒ 谎报成功。能力预裁（_capability_refuse）只看 target 形，对
        entity_id 形直接放行，罩不住这条。

        闸规则（保守，宁漏放不误拒，同 capability.py:116/201 纪律）：
          · 目标 entity_id **全部**在 states 快照里且**全部** state=='unavailable'
            → 如实失败；
          · 任一可用 / 目标不在快照（未知，快照可能不全）/ 快照空（桥不通）→ 放行；
          · 只认 'unavailable'（确证离线），不碰 'unknown'（推送实体未首 poll 瞬态）。
        永不抛。"""
        try:
            raw = (args or {}).get("entity_id")
            eids = [raw] if isinstance(raw, str) and "." in raw else [
                e for e in (raw or []) if isinstance(e, str) and "." in e]
            if not eids:
                return None
            states = await self.ha.states()
            if not states:
                return None                       # 桥不通/快照空：拒=凭空少做，放行
            present = [states[e] for e in eids if e in states]
            if not present:
                return None                       # 目标都不在快照=未知，不凭空拒
            if not all(str((e or {}).get("state")) == "unavailable" for e in present):
                return None                       # 有可用/未知台 → 放行（交 _receipt）
            nm = ""
            for e in present:
                nm = str(((e.get("attributes") or {}).get("friendly_name")) or "")
                if nm:
                    break
            what = f"「{nm}」" if nm else "目标设备"
            return f"{what}现在离线（不可用），这条没执行成，等它恢复再试"
        except Exception:  # noqa: BLE001 裁决故障=放行（宁多发一次，绝不少做）
            logger.exception("[执行] 可用态预裁异常（放行）")
            return None

    async def _offline_names(self, name: str, args: dict) -> list[str]:
        """本条**确证没动成**的那几台的点名（与 _availability_refuse 同口径：只认
        'unavailable'，'unknown'/不在快照一律不算）。

        病灶：可用态闸只在"**全部**离线"时拒答；「点名 3 台、2 台离线」是部分离线 ⇒
        闸放行 → 而 HA 对离线实体的 service call 照样回 success ⇒ 播报笼统成功，
        用户永远不知道那两台没动。这里把"哪几台没执行"在播报里点出来（只改播报，
        不改成败口径）。target 形与 entity_id 形都覆盖。

        v1.1.28 修「离线孪生污染播报」（192.168.1.91 实锤）：`light.she_deng`（客厅
        射灯，unavailable）与 `light.ban_gong_shi_she_deng`（办公室射灯）**同名**。
        说「打开射灯」时真机真正被 turn_on 的是办公室那台（事后复核 state=on），但旧
        口径把整个 target 按 name 重做一遍实体匹配，同名孪生因此被捞进来点名 ⇒
        播报成了「好的，「射灯」现在离线、这条没执行」——与事实相反。
        新口径（target 形）＝**逐个设备槽全票通过才算**：只有当用户点到的这一个名字
        在本家解析到的**所有**实体都确证 unavailable 时才点名（此时无论集成挑了哪一台，
        都确实没动成）；同名里只要还有一台可用 ⇒ 不许点名（那一台很可能才是真正被执行
        的一台；宁可不点，绝不把做成了说成没做）。**未点名的槽**（整区/纯域）不适用
        这条孪生规则——那里没有"按名重解析"这一步，区域+域本身就是真下发集合，其中
        确证离线的每一台照旧逐台点名（v1.1.17 原意）。entity_id 形仍按实际下发的
        entity_id 精确取（那本来就是"真下发集合"）。
        """
        try:
            raw = (args or {}).get("entity_id")
            eids = [raw] if isinstance(raw, str) and "." in raw else [
                e for e in (raw or []) if isinstance(e, str) and "." in e]
            states = await self.ha.states()
            if not states:
                return []
            out: list[str] = []
            if eids:
                for e in eids:
                    ent = states.get(e)
                    if isinstance(ent, dict) and str(ent.get("state")) == "unavailable":
                        nm = str(((ent.get("attributes") or {})
                                  .get("friendly_name")) or "").strip()
                        out.append(nm or str(e))
            else:
                tgt = (args or {}).get("target")
                if not isinstance(tgt, list) or not tgt:
                    return []
                entity_area = getattr(self.ha, "_entity_area", {}) or {}
                for slot in tgt:
                    if not isinstance(slot, dict):
                        continue
                    for dev in (slot.get("devices") or []):
                        if not isinstance(dev, dict):
                            continue
                        named = bool(str(dev.get("name") or "").strip())
                        # 逐个设备槽单独解析——同槽里其它可用设备不得替这台背书，
                        # 更不许被这台拖累（v1.1.28 孪生口径）
                        same = capability.resolve_candidates(
                            states, entity_area, [{**slot, "devices": [dev]}])
                        if not same:
                            continue                 # 这台查不到 ⇒ 无从点名
                        if named and not all(str((e or {}).get("state")) == "unavailable"
                                             for e in same):
                            continue                 # 点名了、同名还有可用的 ⇒ 不许点名（孪生）
                        # 未点名槽（整区/纯域）不适用孪生规则：区域+域本身就是真下发
                        # 集合，其中确证离线的每一台都没动成 ⇒ 照旧逐台点名（v1.1.17）。
                        for ent in same:
                            if not named and str((ent or {}).get("state")) != "unavailable":
                                continue
                            nm = str(((ent.get("attributes") or {})
                                      .get("friendly_name")) or "").strip()
                            if nm:
                                out.append(nm)
            return list(dict.fromkeys(out))
        except Exception:  # noqa: BLE001 判不了就不判（绝不凭空点名）
            logger.exception("[执行] 离线目标点名异常（不判）")
            return []

    async def _lock_unconfirmed(self, name: str, args: dict) -> list[str]:
        """锁命令发完后的**后置确证**（v1.1.17 复审补漏）：返回"还没到要求锁态"的点名串。

        病灶：锁族的逐台回执是硬编码 `success: True`（intent_lock.py:85）⇒ 只靠回执
        永远看不出"命令发了、锁没动"（本条之前只能靠快照判"本来就已经锁着"，那是
        **执行前**的前置判定）。这里在执行后强制刷一次状态做确证。

        纪律（宁可不点，绝不把做了说成没做）：
          · 只报**未确证**、不断言失败——锁状态可能滞后/正在动作（locking/unlocking），
            措辞是「还没确认到已上锁」，用户自己去面板核对即可；
          · 非 lock 域 / 目标不在快照 / 快照空 / 'unknown' 与中间态 / 任何异常
            → 一律当已确认（不打扰）；
          · 强制刷新失败 → 不判。
        """
        # v1.1.22：按 D7 语义判方向，不按意图名——klar 的 HassTurnOn×lock 落的是
        # `lock.lock`（:1002-1003，_KLAR_ECHO_LOCK 同为"上锁"），旧式非 HassLock
        # 一律取 "unlocked" ⇒ 上锁成功反报「还没确认到已解锁」。
        want = "locked" if name in _LOCK_WANT_LOCKED else "unlocked"
        try:
            rf = getattr(self.ha, "refresh_states", None)
            if rf is not None:
                await rf(force=True)
            states = await self.ha.states()
            if not states:
                return []
            raw = (args or {}).get("entity_id")
            eids = [raw] if isinstance(raw, str) and "." in raw else [
                e for e in (raw or []) if isinstance(e, str) and "." in e]
            if eids:
                ents = [states[e] for e in eids if e in states]
            else:
                tgt = (args or {}).get("target")
                if not isinstance(tgt, list) or not tgt:
                    return []
                ents = capability.resolve_candidates(
                    states, getattr(self.ha, "_entity_area", {}) or {}, tgt)
            if not ents:
                return []
            out: list[str] = []
            for ent in ents:
                eid = str((ent or {}).get("entity_id") or "")
                if eid.split(".", 1)[0] != "lock":
                    return []                       # 非锁域：本闸不管
                st = str((ent or {}).get("state"))
                if st not in ("locked", "unlocked"):
                    return []                       # 'unknown'/动作中/不可用：无从判
                if st != want:
                    nm = str(((ent.get("attributes") or {}).get("friendly_name"))
                             or eid).strip()
                    out.append(f"「{nm}」还没确认到{'已上锁' if want == 'locked' else '已解锁'}")
            return out
        except Exception:  # noqa: BLE001 确证故障=不打扰
            logger.exception("[执行] 锁后置确证异常（不判）")
            return []

    async def _repoint_offline_twin(self, args: dict) -> None:
        """v1.1.15（办公 .91 实锤 D4）：目标确证离线时，先改指**同名同域且唯一可用**
        的那一台，而不是直接回「"射灯"现在离线（不可用）」。

        现场：「打开客厅的灯」/「打开床头灯」/「把客厅的灯关掉」三条都被 klar 落到
        `light.she_deng`——一条已离线、friendly_name 恰好也叫"射灯"的孪生实体；而真正
        可用的 `light.ban_gong_shi_she_deng` 同名就在旁边。v1.1.7 的闸如实报了离线，
        话是**真话**，但用户听到的是"这机器坏了/我的灯不受控"，实际是**选错了台**：
        klar 的家快照里根本没有 state 字段（`snapshot.rs:59-83`），它无从避开离线实体。

        判据保守到不发虚：只认 `friendly_name` **全等** + 同域 + **恰好一台**可用；
        两台以上同名、名字缺失、快照里没有可用台 → 一律不动，交回闸如实说。
        永不抛、永不阻断（异常=原样继续）。
        """
        try:
            raw = (args or {}).get("entity_id")
            eids = [raw] if isinstance(raw, str) and "." in raw else [
                e for e in (raw or []) if isinstance(e, str) and "." in e]
            if not eids:
                return
            states = await self.ha.states()
            if not states:
                return
            fixed = {}
            for eid in eids:
                ent = states.get(eid)
                if not isinstance(ent, dict) or str(ent.get("state")) != "unavailable":
                    continue
                nm = str(((ent.get("attributes") or {}).get("friendly_name")) or "").strip()
                dom = str(eid).split(".", 1)[0]
                if not nm:
                    continue
                # 改指目标必须**确证可用**：'unavailable' 与 'unknown' 都排除
                # （'unknown'=未首 poll 的瞬态，与 _availability_refuse 不碰它的口径一致），
                # 且 friendly_name 全等、同域。
                cands = [o for o, oe in states.items()
                         if isinstance(oe, dict) and o != eid and o.startswith(dom + ".")
                         and str(oe.get("state")) not in ("unavailable", "unknown")
                         and str(((oe.get("attributes") or {}).get("friendly_name")) or "").strip() == nm]
                if len(cands) == 1:
                    fixed[eid] = (cands[0], nm)
            if not fixed:
                return
            area_map = getattr(self.ha, "_entity_area", {}) or {}
            for old, (new, nm) in fixed.items():
                if isinstance(raw, str):
                    args["entity_id"] = new
                else:
                    args["entity_id"] = [new if e == old else e for e in (args.get("entity_id") or [])]
                rec = (nm, str(area_map.get(new) or "").strip())
                if rec not in self._repointed:
                    self._repointed.append(rec)
                logger.info("[执行] 目标离线·同名改指 %s → %s（%s%s）", old, new, nm,
                            f"·{rec[1]}" if rec[1] else "")
        except Exception:  # noqa: BLE001 改指故障=不改（闸仍在后面兜底）
            logger.exception("[执行] 离线同名改指异常（不动目标）")

    async def _leg_truth_confirmed(self, name: str, args: dict) -> tuple[str, str]:
        """判"空操作"前要**确证读**一次状态（v1.1.17 复审补漏，实测复现）。

        病灶：`states()` 是 TTL 缓存（ha_client.refresh_states 默认 5s 且不回灌
        执行后果），而 `_leg_truth` 是**执行前**判的 ⇒ 同一 Executor 连发两条时，
        第二条读到的还是第一条之前的旧值：「打开台灯」(off→on 真落地) 后立刻
        「关闭台灯」，判据看到 off 就宣告"台灯本来就在要求的状态上"——把**真做了的
        那条**说成没做，正是本仓"绝不把做了说成没做"的红线反面（我 1.1.16/17 把判据
        从链放宽到单步、又补了 TurnDevice 族之后才够得到）。

        代价与边界：只在**要指控空操作**这条可疑路径上强制刷一次（正常口令零新增
        网络开销，由开销钉把住）；刷完仍成立才点名，刷不动/异常则不判（保守）。
        """
        kind, label = await self._leg_truth(name, args)
        if kind != "noop":
            return kind, label
        try:
            rf = getattr(self.ha, "refresh_states", None)
            if rf is None:
                return "", ""
            await rf(force=True)
        except Exception:  # noqa: BLE001 确证读失败=不指控（宁可不点名）
            logger.exception("[执行] 空操作确证读失败（不点名）")
            return "", ""
        return await self._leg_truth(name, args)

    async def _leg_truth(self, name: str, args: dict) -> tuple[str, str]:
        """v1.1.15（办公 .91 实锤 D2）：链式分句"这一条到底成不成立"。

        现场：「打开办公室射灯和打开床头灯」回了"好的，都办妥了"，而 HA 侧**零状态
        变化**——第一条是对已开着的灯做开（HA 照样回 success 的空操作），第二台的
        设备名在这屋里根本不存在。顶层 success 罩不住这两种（`_receipt` 只在 result
        带 per-entity `states` 时才计数，见 :366-367 自己的注释）。

        返回 ("", "")＝无从证伪（快照空/认不出域/非开关族，一律不猜）；
        ("missing", 名)＝本家快照里查不到这个设备名；
        ("noop", 名)＝目标当前已全部处在指令要求的状态上。
        只读、永不抛。
        """
        try:
            want = _leg_want(name, args)
            tgt = (args or {}).get("target")
            has_tgt = isinstance(tgt, list) and bool(tgt)
            if want is None or (not has_tgt and not (args or {}).get("entity_id")):
                return "", ""
            states = await self.ha.states()
            if not states:
                return "", ""                       # 快照空＝无从判，绝不凭空改名话术
            if not has_tgt:
                # v1.1.15 E1：只有 entity_id 的分句（klar grounded 腿）过去在这里
                # 恒返回"无从证伪"——14:04 那条混形链的第二腿正是这一形制，
                # 于是 D2 的点名对新加的直调路完全不设防。走专用判据。
                return self._leg_truth_by_entity(want, args, states)
            area_map = getattr(self.ha, "_entity_area", {}) or {}
            # 区域表护栏（收口批）：区域表来自注册表刷新（ha_client._entity_area），
            # 桥不通/未首刷时为空；而 resolve_candidates 对**句带区域**的槽在区域表
            # 为空时恒返 []（实测）⇒ 会把屋里真有的设备说成"没找到"。区域都认不出
            # 的时候，"查无此名"无从判——宁可不点名。
            want_areas = {str((s or {}).get("area") or "").strip()
                          for s in tgt if isinstance(s, dict)}
            want_areas.discard("")
            if want_areas and not want_areas.issubset(set(area_map.values())):
                return "", ""
            cands = capability.resolve_candidates(states, area_map, tgt)
            names = [str((d or {}).get("name") or "").strip()
                     for slot in tgt for d in (slot.get("devices") or [{}])]
            names = [n for n in names if n]
            if not cands:
                if not names:
                    return "", ""
                # HA 别名（{eid: [改名/原名/别名]}，ha_client._entity_alias）：本仓快照
                # 里只有 friendly_name，按名字查候选对别名句恒空——那不是"查无此名"。
                # 按 eid 补一次真判（用户明明说对了名字，绝不能报"没找到"）。
                cands = self._alias_candidates(states, names)
            if not cands:
                if not names:
                    return "", ""
                # "此区没有"≠"屋里没有"（收口批反向判据）：区域表是注册表快照
                # （ha_client._entity_area，可能陈旧/不全），resolve_candidates 按
                # (名字, 区域, 域提示) 三重过滤 ⇒ 过滤空不代表查无此名。名字在快照里
                # 别处存在（换区域或换域）时一律不点名——把"有"说成"没找到"是红线；
                # 真实的"此区没有"口径由能力闸/可用态闸承担，不靠这句话术。
                anywhere = [{"devices": [{"name": (d or {}).get("name"), "domains": []}
                                         for d in ((s or {}).get("devices") or [])]}
                            for s in tgt if isinstance(s, dict)]
                if capability.resolve_candidates(states, {}, anywhere):
                    return "", ""
                return "missing", "、".join(names)
            # 空操作判定只对"状态名就是 on/off"的域成立；cover/climate/media_player
            # 等状态词不同（open/closed、hvac_action…），一律不判，宁可不点名。
            if any(str(eid).split(".", 1)[0] not in _LEG_NOOP_DOMAINS
                   for eid in (c[0] if isinstance(c, tuple) else c.get("entity_id") or "" for c in cands)):
                return "", ""
            cur = {str((e or {}).get("state")) for e in cands}
            if cur and cur.issubset(want):
                label = "、".join(names) or str(cands[0].get("entity_id") or "")
                return "noop", label
            return "", ""
        except Exception:  # noqa: BLE001 判不了就不判（话术退回既有口径）
            logger.exception("[执行] 分句真伪判定异常（不判）")
            return "", ""

    @staticmethod
    def _leg_truth_by_entity(want: tuple, args: dict, states: dict) -> tuple[str, str]:
        """entity_id 形分句的真伪判据（v1.1.15 E1，只读、永不抛）。

        只判两形，与 target 形同一口径：
          · 快照非空却查无此台 → ("missing", "")（**不给名字**：entity_id 念进播报
            只是噪音，由调用方按数量如实说"找不到对应的设备"）；
          · 目标全部已在指令要求的状态上 → ("noop", friendly_name)；
        一律不判的：非 on/off 域（cover/climate… 状态词不同）、'unknown'
        （未首 poll 的瞬态，同 _availability_refuse 的口径）、没有一台能定名。
        """
        raw = (args or {}).get("entity_id")
        eids = [raw] if isinstance(raw, str) and "." in raw else [
            e for e in (raw or []) if isinstance(e, str) and "." in e]
        if not eids:
            return "", ""
        # v1.1.17 收口：缺台判定**先于**域闸——与 target 形同序（那边 missing 也在域闸
        # 之前）。旧序下 cover/climate 腿根本走不到这段，同一条"快照里没这台"的事实，
        # entity_id 形静默成功、target 形点名，两条路两种口径。
        # 域闸仍只管"空操作"：非 on/off 域状态词不同（open/closed、hvac_action…），
        # 一律不判 noop，防把"开到头"说成没动。
        if any(e not in states for e in eids):
            return "missing", ""
        if any(str(e).split(".", 1)[0] not in _LEG_NOOP_DOMAINS for e in eids):
            return "", ""
        rows, nm = [], ""
        for e in eids:
            ent = states[e] or {}
            st = str(ent.get("state"))
            if st == "unknown":
                return "", ""                   # 瞬态＝无从判，宁可不点名
            rows.append(st)
            nm = nm or str(((ent.get("attributes") or {}).get("friendly_name")) or "").strip()
        if rows and all(r in want for r in rows):
            return "noop", (nm or "")
        return "", ""

    def _named(self, ok: bool, reply: str) -> tuple[bool, str]:
        """把本轮的"代打"留痕落进播报（收口批，run 的唯一出口）。

        措辞只陈述**目标替换**这件事（"离线 / 已改指同名的另一台"），不陈述成败——
        失败支的"抱歉"照旧由本句给出，注不得替它宣称办妥。按 run 复位，
        上一句的留痕不得粘到下一句。
        v1.1.17：改指目标有区域时一并念出（同名两台靠名字分不出房间，不报区域
        等于让用户以为动的是他说的那间）；区域未知则不加，绝不编。
        """
        if not self._repointed:
            return ok, reply
        segs = []
        for nm, area in dict.fromkeys(self._repointed):
            segs.append(f"「{nm}」离线，已改指同名的另一台" + (f"，在{area}" if area else ""))
        note = "（注：" + "；".join(segs) + "）"
        return ok, (reply.rstrip() + note if reply else note)

    def _alias_candidates(self, states: dict, names: list[str]) -> list:
        """按 HA 别名/改名把用户说的名字还原成实体（v1.1.15 收口批，只读、永不抛）。

        `states` 只有 friendly_name；别名在注册表（`ha_client._entity_alias`，
        {eid: [改名后的 name, original_name, …aliases]}）里。用户说的是别名时，
        按名字查候选恒空，若不还原就会把"说对了名字"报成"没找到"。
        返回与 `capability.resolve_candidates` 同形的实体行（entity_id/state/attributes）。
        """
        try:
            amap = getattr(self.ha, "_entity_alias", {}) or {}
            if not amap:
                return []
            want = {str(n).strip() for n in names if str(n).strip()}
            out = []
            for eid, als in amap.items():
                ent = states.get(eid)
                if not isinstance(ent, dict):
                    continue
                if any(str(a).strip() in want for a in (als or [])):
                    out.append(ent)
            return out
        except Exception:  # noqa: BLE001 还原故障＝按"无候选"走原判据（不阻断）
            logger.exception("[执行] 别名候选还原异常（不判）")
            return []

    async def run(self, plan: Plan) -> tuple[bool, str]:
        """执行 Plan（klar 多分句/复合链逐步顺序执行）。返回 (success, 中文播报)。永不抛。"""
        # 每一步带**自己的**来源（src）：一条链由 select_primary_plan 逐分句裁决，
        # 首腿是慧尖意图（窗户恒让字面表胜）、次腿就让给了 klar，两腿的参数形制
        # 天然不同。整链只认 plan.source（＝首分句来源）会把次腿的 Klar 直调权
        # 一并没收（2026-09-27 办公 .91 实锤 D6，见 _klar_direct 的取值处）。
        steps = [(plan.intent, plan.args, plan.source)] + [
            (st.get("name"), st.get("args") or {}, st.get("source") or plan.source)
            for st in (getattr(plan, "extra_steps", None) or [])]
        self.last_run = {"steps": len(steps), "applied": 0, "indeterminate": False}
        self._repointed = []                 # 本轮的"代打"留痕按轮复位（见 _named）
        results = []
        missing: list[str] = []              # 链中"这屋里查无此名"的分句（D2）
        noops: list[str] = []                # 链中"目标已在要求状态"的空操作分句（D2）
        no_receipt: list[str] = []           # 链中点了名、回执里却没有那台的分句（E3）
        offline: list[str] = []              # 标的确证离线（部分离线时点名，见 _offline_names）
        lock_notes: list[str] = []           # 锁后置确证未过（见 _lock_unconfirmed）
        anon_missing = 0                     # entity_id 形分句查无此台（E1，无名可点）
        for idx, (name, args, src) in enumerate(steps):
            # v1.1.21：本步用**副本**——改指（_repoint_offline_twin）会就地写 args，
            # 旧实现把调用方 plan.args/extra_steps 里的同一份引用改掉（同轮二次 run
            # 会静默对改指后的目标执行、且丢"已改指"留痕）
            args = dict(args) if isinstance(args, dict) else args
            # v1.1.21：本步原话优先（链路逐腿带 utterance）；缺省回落整句原话
            _utt = plan.utterance or ""
            if idx > 0:
                _es = getattr(plan, "extra_steps", None) or []
                if idx - 1 < len(_es):
                    _utt = str((_es[idx - 1] or {}).get("utterance") or _utt)
            gate = self._turn_gate(name, args, _utt)
            if gate is not None:
                # v1.0.69 根因②：宁可当场如实失败，绝不 area 扇出+谎报成功
                self.last_run = {"steps": len(steps), "applied": len(results),
                                 "indeterminate": False}
                logger.info("[执行] %s %s → 开关族能力闸拦下（防area扇出/假成功）",
                            name, args)
                return self._named(False, self._step_say(idx, steps, gate))
            cap = await self._capability_refuse(name, args)
            if cap is not None:
                # v1.1.3：网关侧按本家实体真实能力当场如实回话（带可选档位），
                # 不再白跑一趟 HA 换一个 unsupported 错误码。
                self.last_run = {"steps": len(steps), "applied": len(results),
                                 "indeterminate": False}
                logger.info("[执行] %s %s → 能力预裁拦下 | %s", name, args, cap)
                return self._named(False, self._step_say(idx, steps, cap))
            await self._repoint_offline_twin(args)
            avail = await self._availability_refuse(name, args)
            if avail is not None:
                # v1.1.7：目标实体确证 offline → 如实失败，绝不发空操作再谎报成功
                self.last_run = {"steps": len(steps), "applied": len(results),
                                 "indeterminate": False}
                logger.info("[执行] %s %s → 目标不可用闸拦下（防谎报成功）| %s",
                            name, args, avail)
                return self._named(False, self._step_say(idx, steps, avail))
            # v1.1.15(收口批)：单步计划同样逐台证伪——旧的 `len(steps) > 1` 栅栏
            # 让"电视声被听成单步 HassTurnOff 打在已关的灯上"永远回"关了"（审计实证：
            # 17:02/17:03 两轮）。判据本身早就对单步成立（resolve_candidates 与
            # _leg_truth_by_entity 都不依赖步数），栅栏只是话术侧的旧口径。
            kind, label = await self._leg_truth_confirmed(name, args)
            if kind == "missing":
                if label:
                    missing.append(label)
                else:
                    anon_missing += 1        # 只数不猜名：entity_id 念进播报是噪音
            elif kind == "noop" and label:
                noops.append(label)
            # 直调与否按**本步来源**判（不是整链首腿来源）：klar 引擎 full 模式已把
            # 「办公室射灯」grounded 成 entity_id，这类步骤的归宿是 /api/services/*；
            # 按 plan.source 判会让"慧尖首腿 + klar 次腿"的混形链把次腿原样丢进
            # /api/intent/handle——HassTurnOn/Off 不在慧尖集成注册面内（intent.py:27-44
            # 只登记 TurnDevice*/HassLock/HassUnlock/…），HA 内置 handler 又不认
            # 慧尖口径 ⇒ 第二腿发出去没人按，顶层照样回 success（2026-09-27 实锤）。
            direct = self._klar_direct(name, args) if src == "klar" else None
            if direct is not None:
                domain, service, data = direct
                result = await self.ha.call_service(domain, service, data)
            else:
                result = await self.ha.handle_intent(name, wire_args(name, args))
            for _nm in await self._offline_names(name, args):
                if _nm not in offline:
                    offline.append(_nm)
            # v1.1.19 复审：按**目标域**判，不按意图名——klar 的 HassTurnOn/Off 落在
            # lock 域时走 _klar_direct → lock.lock/unlock，此前完全没被确证（D7 那条
            # 「打开门锁=上锁」正是这个形态）。
            _is_lock = (name in ("HassLock", "HassUnlock")
                        or _lock_domain_target(args))
            if _is_lock:
                lock_notes.extend(await self._lock_unconfirmed(name, args))
            if not result.get("success"):
                raw_err = str(result.get("error") or result.get("message") or "")
                # v1.1.17：逐台回执带原因且**全部失败**时，用逐台真原因——集成的顶层
                # `error` 常为空（AdjustDeviceAttribute 全失败只回 success=false），
                # 于是 v1.1.4 的动机案（"does not support set_cover_position"）被泛化
                # 成"换个说法再试"，用户永远不知道是设备能力不足。
                _ok, _bad, _errs = self._receipt([result])
                # v1.1.19 复审：逐台原因只用来**细化话术**，不能抹掉"结果不确定"
                # （超时/连接/5xx）——那会让 last_run.indeterminate 变 False，
                # 播报却说"我不自动再试"，下游复议/重试闸失去护栏。
                _top_err = raw_err
                if _bad and not _ok and _errs and not is_indeterminate(_top_err):
                    raw_err = _errs[0]
                self.last_run = {"steps": len(steps), "applied": len(results),
                                 "indeterminate": is_indeterminate(_top_err)}
                reply = zh_error(raw_err, klar=(src == "klar"))
                # P2-12 链失败定位：部分执行已成事实，如实说清第几步、还剩几步
                # （保留"抱歉"字头——话术层诚实失败纪律被测试钉死）
                detail = reply[3:] if reply.startswith("抱歉，") else reply
                reply = self._step_say(idx, steps, detail)
                logger.info("[执行] %s %s → 失败 | %s", name, args, reply)
                return self._named(False, reply)
            results.append(result)
            # 同栅栏收口：单步也要点名"点了名却没回执"的那台（此前只对链生效）。
            for nm in await self._unanswered(args, result):
                if nm not in no_receipt:
                    no_receipt.append(nm)
            if len(steps) > 1:
                # 逐腿留痕（D2 旧病：整链只印首步的 intent/args，第二腿在账上不存在，
                # 现场无法对账"到底动了几台"）；通道名一并印——D6 的病灶正是"走了
                # intent 通道却没人解析 entity_id"，光看参数分不出两条外发路。
                logger.info("[执行] 第 %d/%d 步 %s %s → 成功（%s）",
                            idx + 1, len(steps), name, args,
                            "直调服务" if direct is not None else "intent")
        # v1.1.4 状态回执：顶层 success / success_count 是**集成自己的口径**，
        # 真机实锤会骗人——A 组里 `success:true, success_count:1` 却是我们点名的
        # 那台实体 per-entity 报 `does not support set_cover_position`，用户听到
        # "开到60%"而窗没动。因此成功与否改按**逐实体回执**判：全失败=如实失败，
        # 部分失败=播报里点名（不做静默"都办妥了"）。
        ok_n, bad_n, errs = self._receipt(results)
        if ok_n == 0 and bad_n > 0:
            why = errs[0] if errs else "设备没接受这条指令"
            # v1.1.27：applied 语义＝本次"已生效步数"（旧写法恒写 0）。多腿链前腿
            # 已真执行（它的回执没有 per-entity 行，`_receipt` 在 :944-945 跳过）
            # 而末腿逐台全败时，写 0 会让 pipeline._exec_risk 的 `applied>0` 判据
            # 失明 ⇒ 放行降级重放(:760)/LLM 复议(:807)，已执行的腿被再做一遍。
            self.last_run = {"steps": len(steps),
                             "applied": self._applied_steps(results),
                             "indeterminate": False}
            # v1.1.17：zh_error 自带"抱歉，"字头，旧写法再拼一次 ⇒ 播报成「抱歉，抱歉，…」。
            reply = zh_error(why, klar=plan.source == "klar")
            # v1.1.19 复审：链里前面几步可能已真执行（本支在循环之后）——按 _step_say
            # 的同款口径带上步序，别把部分执行播成整句没做（用户会整句重说=重复动作）
            if len(steps) > 1 and len(results) > 1:
                reply = self._step_say(len(results) - 1, steps, reply)
            elif not reply.startswith("抱歉"):
                reply = "抱歉，" + reply
            logger.info("[执行] %s %s → 逐实体全失败 | %s", plan.intent, plan.args, reply)
            return self._named(False, reply)
        partial = f"（另有 {bad_n} 台没成功）" if bad_n > 0 else ""
        klar_speech = (getattr(plan, "speech", "") or "").strip()
        if plan.source == "klar" and len(results) == 1:
            # 标准开关族：引擎那句缺主语的话术让位给「原话目标词 + 方向动词」
            klar_speech = self._klar_echo(plan) or klar_speech
        if klar_speech:
            # klar 引擎自带的中文播报（zh_cn pack 产出）优于话术层泛化模板
            reply = klar_speech
        elif len(results) == 1:
            reply = self.speech(plan, results[0])
        elif plan.source == "klar":
            reply = "好的，都办妥了"
        else:
            # P2-12 复合链：逐步真话术串播（"好的，灯打开了，窗帘关了"），
            # 拿不准的一步退"都办妥了"，不硬拼英文意图名
            try:
                segs = []
                for i, ((n, a, step_src), r) in enumerate(zip(steps, results)):
                    s = self.speech(Plan(intent=n, args=a, source=step_src,
                                         utterance=plan.utterance), r)
                    if i > 0:
                        s = s.removeprefix("好的，")
                    segs.append(s)
                reply = "，".join(x for x in segs if x) or "好的，都办妥了"
                if not reply.startswith("好的"):
                    reply = "好的，" + reply
            except Exception:
                # 话术拼装自身出问题不改成败（各腿都已如实返回 success），但必须留痕：
                # 这一支会把播报退成笼统的"都办妥了"，2026-09-27 14:04 那条现场签名
                # 就是从这儿出来的——不留痕则下一次仍然无从归因（同 D2 的教训）。
                logger.exception("[执行] 复合链逐腿话术拼装异常 → 退笼统回执")
                reply = "好的，都办妥了"
        bits = [f"「{n}」我没找到" for n in missing] + \
               [f"「{n}」本来就在要求的状态上" for n in noops] + \
               [f"「{n}」没拿到执行回执" for n in no_receipt] + \
               [f"「{n}」现在离线、这条没执行" for n in offline] + lock_notes
        if anon_missing:
            # entity_id 形分句查无此台（E1）：没有中文名可点，按数量如实说，
            # 绝不把 entity_id 念进播报（「light.bedside_lamp」只会成噪音）。
            bits.append(f"另有 {anon_missing} 条找不到对应的设备")
        if bits:
            # 实测假形（「打开办公室射灯和打开床头灯」→"好的，都办妥了"而 HA 零变化）：
            # 只要有一条分句被证伪，就不允许再用笼统的"都办妥了"收口。
            if partial:
                # v1.1.22：分句判据（查无此名/空操作/离线点名/锁确证）与逐台失败计数
                # （逐实体行）是**两种事实**，不许互吞——旧式这里把 partial 置空，实测
                # 「另有 1 台没成功」静默消失（链里一腿查无此名 + 一腿部分失败）。
                bits.append(partial.strip("（）"))
            reply = "好的，" + "；".join(bits)
            partial = ""
        tag = f"(+%d步)" % (len(steps) - 1) if len(steps) > 1 else ""
        if partial and not reply.endswith(partial):
            reply = reply.rstrip("。") + partial      # 部分失败点名，不静默全绿
        # v1.1.27-r2（金标复测）：集成侧把窗侧失败折进 `partial_error` 返回；core
        # 旧无消费者 ⇒ 用户只听「好的，…关了」（金标复测 V1-项3 实锤）。按"部分
        # 失败点名"同口径并入话术：去重、不改成功口径（顶层仍如实 True）。
        _extra_partials: list = []
        for _r in (results or []):
            if isinstance(_r, dict):
                _pe = str(_r.get("partial_error") or "").strip()
                if _pe and _pe not in _extra_partials:
                    _extra_partials.append(_pe)
        for _pe in _extra_partials:
            if _pe and _pe not in reply:
                reply = reply.rstrip("。") + "（" + _pe.strip("（）") + "）"
        self.last_run = {"steps": len(steps), "applied": len(steps),
                         "indeterminate": False}
        logger.info("[执行] %s %s%s → 成功 | %s", plan.intent, plan.args, tag, reply)
        return self._named(True, reply)

    @staticmethod
    def _step_say(idx: int, steps: list, reason: str) -> str:
        """整链中断时的定句模板：已经动了几步必须说清（v1.1.15 E2）。

        P2-12 的步序定位原先只挂在"执行失败"这一支上，三道**前置闸**
        （开关族能力闸 / 能力预裁 / 可用态闸）命中时首腿往往已经真落地了，播报却
        只剩"没有把握找到要开关的设备"——用户听不出窗已经开过，等于把部分执行
        说成整句没做（下一次他再补一句，就变成重复动作）。四支共用同一文案。"""
        if len(steps) <= 1:
            return "抱歉，" + reason
        if idx == 0:
            return f"抱歉，第 1 步没成功，后面的步骤先不执行了（{reason}）"
        return f"抱歉，前面 {idx} 步已完成，但第 {idx + 1} 步没成功——{reason}"

    async def _unanswered(self, args: dict, result: dict) -> list[str]:
        """本腿点名的设备里，回执没提到哪几台（v1.1.15 E3，只读、永不抛）。

        为什么不走 `_receipt`：慧尖意图的返回形制里根本没有逐实体 `states`
        （`custom_components/huijian_ai/intent_turn.py:184-187` 只回
        `{success, control_targets}`），而集成侧的成败口径是"control_targets 非空即
        成功"（同文件 :179-183）⇒ 点名三台、只落地两台，从任何通道都看不出来
        （`_receipt` 对这两族恒 0/0）。改内置集成＝全客户群的爆炸半径，故在加载项侧
        把**我们点过的名**与**回执里的名**对一遍。

        判据保守：只比带名字的 target 槽（泛称/区域-only 没有可比对象，跳过）；命中
        按互相包含判（集成回的是解析后的实体名，可能比请求名更长如「平开窗 开窗器」
        或更短），全都对不上才算没回执——宁漏报一次，绝不在真执行了的时候喊没执行。
        **HA 别名**：用户可能说别名（v1.1.4 支持），集成回的是实体本名 ⇒ 比对前先把
        别名翻成本名（注册表 `_entity_alias` + states 的 friendly_name）；翻不出来的
        不判（名称比对法在别名在场时不可信）。
        """
        try:
            tgt = (args or {}).get("target")
            if not isinstance(result, dict) or "control_targets" not in result \
                    or not isinstance(tgt, list):
                return []                       # 不吃这套方言的返回（直调/内置意图）无判据
            got = [str((t or {}).get("name") or "").strip()
                   for t in (result.get("control_targets") or [])]
            got = [g for g in got if g]
            spoken = [str((d or {}).get("name") or "").strip()
                      for slot in tgt for d in ((slot or {}).get("devices") or [])]
            spoken = [n for n in spoken if n]
            alias_map = {eid: {str(x).strip() for x in (als or []) if str(x).strip()}
                         for eid, als
                         in (getattr(self.ha, "_entity_alias", {}) or {}).items()}
            states: dict = {}
            if alias_map and any(nm in als for nm in spoken
                                 for als in alias_map.values()):
                # 仅当真的说了别名才读快照（常见无别名路径零新增开销）
                states = await self.ha.states()
            out = []
            for nm in spoken:
                cand = {nm}
                for eid, als in alias_map.items():
                    if nm in als:
                        ent = states.get(eid) or {}
                        fn = str(((ent.get("attributes") or {}).get("friendly_name"))
                                 or "").strip()
                        if fn:
                            cand.add(fn)         # 别名 → 本名，纳入同一比对
                if not any(n == g or n in g or g in n
                           for n in cand for g in got):
                    out.append(nm)
            return out
        except Exception:  # noqa: BLE001 比对故障=不判（退回既有回执口径）
            logger.exception("[执行] 逐台点名回执比对异常（不判）")
            return []

    @staticmethod
    def _receipt(results: list) -> tuple:
        """逐实体回执：(成功行数, **失败台数**, 去重失败原因)。

        只在 result 真带了 per-entity `states`/`results` 时才计数——没有该键（HA 内置
        意图通道、老返回形态）就退回原有顶层 success 判定，不凭空判失败。永不抛。

        失败按**台**去重（v1.1.17）：行是实体级、一台设备可能占多行，旧口径把行数直接
        念成"另有 N 台没成功"。键取 (area,name)（SetDeviceMode 形带 area，最精确）或
        name（AdjustDeviceAttribute 形只有 name，同名跨区会并成一台——已知近似，
        比把行当台更贴近事实）；两键皆缺的行各自独立计数。
        """
        ok_n = bad_n = 0
        errs: list[str] = []
        bad_keys: list[str] = []
        anon = 0
        for r in results or []:
            if not isinstance(r, dict):
                continue
            # 三族逐台结果键名（v1.1.15 收口批）：`states`＝AdjustDeviceAttribute/
            # Lock 族（intent_adjust_attribute.py 返回），`results`＝SetDeviceMode 族
            # （intent_set_mode.py 只回这个键）。此前漏读 `results` ⇒ mode 族部分失败
            # 永远不被点名，播报只剩裸「好的」。两形行内都用 `success` 键，同口径计数。
            rows = r.get("states")
            if not isinstance(rows, list) or not rows:
                rows = r.get("results")
            if not isinstance(rows, list) or not rows:
                continue
            for st in rows:
                if not isinstance(st, dict):
                    continue
                if st.get("success"):
                    ok_n += 1
                else:
                    nm = str(st.get("name") or "").strip()
                    ar = str(st.get("area") or "").strip()
                    if nm:
                        bad_keys.append(f"{ar}|{nm}" if ar else nm)
                    else:
                        anon += 1
                        bad_keys.append(f"#{anon}")
                    e = str(st.get("error") or "").strip()
                    if e and e not in errs:
                        errs.append(e)
        bad_n = len(dict.fromkeys(bad_keys))
        return ok_n, bad_n, errs

    @classmethod
    def _applied_steps(cls, results: list) -> int:
        """本次**已生效步数**（v1.1.27 收口，`last_run["applied"]` 的唯一真源）。

        逐腿过 `_receipt`：有逐实体行且**全失败**的腿不算（它确实没生效）；
        其余腿都算——能走到 `results.append` 说明顶层 success 为真，而
        `_receipt`（:944-945）对无逐实体行的腿（HA 内置意图通道/klar 直调服务、
        老返回形态）本就跳过不该凭空判失败。下游 `pipeline._exec_risk` 按
        `applied>0` 判"这一轮可能已经生效"，据此禁降级重放/LLM 复议重做——
        宁可多拦一次重做，绝不把已执行的动作再做一遍。永不抛（判不了＝计入，
        与"结果不确定"同向保守）。"""
        n = 0
        for r in results or []:
            try:
                ok, bad, _ = cls._receipt([r])
            except Exception:  # noqa: BLE001 判不了=按已生效（保守，见 docstring）
                n += 1
                continue
            if ok > 0 or bad == 0:
                n += 1
        return n

    # ── klar grounded 步骤 → 直调服务映射 ────────────────────────
    # 引擎 full 模式已把"办公室射灯"解析成 entity_id；这类步骤绕开 intent
    # handler 直调服务（klar 自家集成同款路线），每个 intent 只带该服务
    # 合法的数据键（多余键会被 HA 服务 schema 拒）。纯 area/domain 未解析
    # 步骤返回 None → 走 /api/intent/handle 由 HA 内置解析。
    _KLAR_SERVICE = {
        "HassTurnOn": ("homeassistant", "turn_on"),
        "HassTurnOff": ("homeassistant", "turn_off"),
        "HassToggle": ("homeassistant", "toggle"),
        "HassLock": ("lock", "lock"),
        "HassUnlock": ("lock", "unlock"),
        "HassClimateSetTemperature": ("climate", "set_temperature"),
        "HassClimateSetHumidity": ("humidifier", "set_humidity"),
        # 服务名按 HA core 实源核验：cover 域只有 set_cover_position
        # （homeassistant.const.SERVICE_SET_COVER_POSITION），**没有**
        # set_position——旧值会让 klar grounded 的开窗器/窗帘定位步骤
        # 必败 "Service cover.set_position not found"（tests/
        # test_window_position.py 钉桩，防漂移）。
        "HassSetPosition": ("cover", "set_cover_position"),
        "HassFanSetSpeed": ("fan", "set_percentage"),
        "HassFanSetPresetMode": ("fan", "set_preset_mode"),
        "HassVacuumStart": ("vacuum", "start"),
        "HassVacuumPause": ("vacuum", "pause"),
        "HassVacuumReturnToBase": ("vacuum", "return_to_base"),
    }
    # intent → 允许携带的数据键（entity_id 恒带，不单列）
    _KLAR_KEYS = {
        "HassLightSet": ("brightness", "color_name", "color_temp"),
        "HassClimateSetTemperature": ("temperature",),
        "HassClimateSetHumidity": ("humidity",),
        "HassSetPosition": ("position",),
        "HassFanSetSpeed": ("percentage",),
        "HassFanSetPresetMode": ("preset_mode",),
    }

    def _klar_direct(self, name: str, args: dict):
        """返回 (domain, service, data)；None = 交给 intent 通道。永不抛。"""
        try:
            raw = args.get("entity_id")
            eids = [raw] if isinstance(raw, str) and "." in raw else [
                e for e in (raw or []) if isinstance(e, str) and "." in e]
            if not eids:
                return None
            edomain = eids[0].split(".", 1)[0]
            # D7 语义一致性：锁的"打开"=上锁、"关闭"=解锁（与 _targets_speech 同向）
            if edomain == "lock" and name in ("HassTurnOn", "HassTurnOff"):
                svc = "lock" if name == "HassTurnOn" else "unlock"
                return "lock", svc, {"entity_id": raw}
            if name == "HassLightSet":
                # v1.0.55 双闸（2026-09-12 窗案次生缺陷）：
                # ① 意图说灯、grounded 目标却不是 light 域 → 不直调，交回
                #   intent 通道由 HA 自己解析（防"亮度"打到别的域实体）。
                # ② 引擎亮度槽是 0–100 百分数（常为字符串），而 light.turn_on
                #   的 brightness 是 0–255 刻度——"30" 透传实亮 ≈12%，差 3 倍
                #   量纲；改用官方百分比键 brightness_pct，语义与话术一致。
                if edomain != "light":
                    return None
                data = {"entity_id": raw}
                for k in self._KLAR_KEYS["HassLightSet"]:
                    v = args.get(k if k != "color_name" else "color")
                    if v is None and k == "color_name":
                        v = args.get("color")
                    if v is not None:
                        data[k] = v
                b = data.get("brightness")
                try:
                    pct = float(str(b).strip().rstrip("%"))
                    if 0.0 <= pct <= 100.0:
                        data["brightness_pct"] = pct
                        del data["brightness"]
                except (TypeError, ValueError):
                    pass                       # 非数值/越界（如已是 0-255）维持原样透传
                return "light", "turn_on", data
            if name == "HassSetPosition" and edomain == "fan":
                return "fan", "set_percentage", {
                    "entity_id": raw, "percentage": args.get("position")}
            if name == "HassFanSetSpeed" and args.get("percentage") is None \
                    and args.get("speed") is not None:
                return "fan", "set_percentage", {
                    "entity_id": raw, "percentage": args.get("speed")}
            svc = self._KLAR_SERVICE.get(name)
            if svc is None:
                return None
            data = {"entity_id": raw}
            for k in self._KLAR_KEYS.get(name, ()):
                if args.get(k) is not None:
                    data[k] = args[k]
            return svc[0], svc[1], data
        except Exception:
            logger.exception("[执行] klar 直调映射异常 → 回落 intent 通道")
            return None

    # ── v1.0.69 根因②：开关族能力闸（2026-09-14 现场 11:27:12 十二条
    # "Service call failed / does not support entity" 错误风暴 + 谎报
    # 「展厅推拉开了」的根治）。窗户句漏进通用开关意图的两种灾难形态：
    #   ① args 无 entity_id → /api/intent/handle HassTurnOn{area} 由 core
    #     把全区域 exposed 实体展开逐个 turn_on——开窗器的 sensor(电压/状态)/
    #     number(力度/速度)/button、media_player、remote 全被硬喂（错误风暴），
    #     窗一律没动，播报却按 success 谎称「开了」；
    #   ② grounded 给到设备内部混合实体（button/sensor/number）→ 直调
    #     homeassistant.turn_on 同样逐实体 ServiceNotSupported。
    # 窗户执行器真实驱动是集成 ControlWindow 的 button.press，喂 turn_on 恒
    # 假动作。本闸只做保守拦截、不改道猜测（触发句形态不坐实的用户定案）：
    # 命中即如实失败并给出正确句式引导——宁如实失败，绝不谎报。永不抛。
    _TURN_FAMILY = frozenset({
        "HassTurnOn", "HassTurnOff", "HassToggle",
        # v1.0.90（现场 18:09:11 / 10:26 两案根修）：慧尖**自有**开关意图族。
        # 旧集合只认 Hass* 三名 ⇒ klar 主计划被本闸拦下后，**降级/备用通道**改投
        # TurnDeviceOn/TurnDeviceOff（args 形如 target:[{devices:[{name:'展厅'}]}]）
        # 就不再复检，同一句话先"能力闸拦下"再"成功 | 好的，展厅的展厅关了"——
        # 现场那条假成功 + media_player ServiceNotSupported 风暴正是从这里漏的。
        # 判据文字完全复用（宁如实失败，绝不 area 扇出谎报），只补覆盖面。
        "TurnDeviceOn", "TurnDeviceOff", "ToggleDevice",
    })
    # 非"可开关设备"域（HA core 语义：这些域的实体没有 turn_on 动作）
    # v1.1.27 收口：本表**必须**与 capability.UNTOGGLEABLE_DOMAINS（单一来源，
    # 读只域 ∪ 无开关域，并集更严）逐位一致——两处手抄的结果是 executor 少抄了
    # weather/person/calendar…、capability 少抄了 number/select/button…，
    # `_turn_gate` 的域预检实际只覆盖半张表（另半张域的实体照样被喂 turn_on）。
    # 一致性由 tests/test_v1127_core_ops.py 钉死（字面镜像的原因见该文件：既有
    # tests/test_v1090_executor_gate_coverage 用 AST literal_eval 直读本属性）。
    _UNTOGGLEABLE_DOMAINS = frozenset({
        "sensor", "binary_sensor", "weather", "update", "person", "image",
        "calendar", "zha", "system_log", "provisioning", "stt", "tts", "notify",
        "conversation", "scene_config", "sun", "zone", "geo_location", "backup",
        "hassio", "config", "diagnostics", "analytics",
        "number", "select", "text", "button", "datetime", "date", "time", "event",
    })
    # 窗族设备词：先剔除 窗帘/纱窗（合法 cover，开关路正常）再查
    _WINDOW_HINT_WORDS = ("窗", "开合器", "内倒", "推拉", "平开")

    def _turn_gate(self, name: str, args: dict, utterance: str):
        """None=放行；str=必须如实失败的播报正文（不含「抱歉」字头）。"""
        try:
            if name not in self._TURN_FAMILY:
                return None
            raw = (args or {}).get("entity_id")
            eids = ([raw] if isinstance(raw, str) else
                    [e for e in (raw or []) if isinstance(e, str)])
            eids = [e for e in eids if "." in e]
            if eids:
                if any(e.split(".", 1)[0] in self._UNTOGGLEABLE_DOMAINS
                       for e in eids):
                    return ("这个设备不支持直接开关；是窗户的话，"
                            "请说打开或关闭完整的窗型名称")
                return None
            t = (utterance or "").replace("窗帘", "").replace("纱窗", "")
            if any(w in t for w in self._WINDOW_HINT_WORDS):
                return ("没有把握找到要开关的设备，不敢把整屋设备冒按；"
                        "是窗户的话请带上完整窗型名称")
            # 已知残留缺口（v1.0.90 有意**不**在本批补，理由见 CHANGELOG 未收口）：
            # ASR 把「推拉窗」听成「推纱窗」时，上面的"先剔纱窗再找窗型词"会把
            # 窗型句洗成无窗句，于是 HassTurnOn{area} 仍会整区冒按并谎称"开了"
            # （现场 10:26 复现）。纯词法判据修不了它——「纱窗」本身是合法 cover
            # 词，一律按"含窗字"拦会误杀「打开客厅的窗帘」这类正常句。
            # 正解＝扇出前用状态缓存**预演**该 area 实体可开关性（存在 sensor/
            # number/button/media_player 等不支持 turn_on 的实体，或可开关实体
            # 不唯一 ⇒ 如实失败并要求指名），属下一批（需 ha_client 暴露按区
            # 域枚举 + 新行为钉），不在这里夹带半修。
            return None
        except Exception:
            return None

    # ── 话术生成 ────────────────────────────────────────────────
    def _klar_echo(self, plan: Plan) -> str:
        """klar 单步标准开关族 → 「原话目标词 + 方向动词」。不适用返回 ""（沿用
        引擎话术）：①意图不在开关族（亮度/温度/开合度等带数值的留引擎那句）；
        ②目标词拿不准（代词、复合残留、空）。永不抛。"""
        try:
            verb = _KLAR_ECHO_VERB.get(plan.intent)
            if not verb:
                return ""
            args = plan.args or {}
            raw = args.get("entity_id")
            eids = ([raw] if isinstance(raw, str) else
                    [e for e in (raw or []) if isinstance(e, str)])
            if eids and eids[0].split(".", 1)[0] == "lock":
                verb = _KLAR_ECHO_LOCK.get(plan.intent, verb)   # D7 锁语义反转
            word = echo_target(plan.utterance or "", str(args.get("area") or ""))
            return f"{word}{verb}" if word else ""
        except Exception:  # noqa: BLE001
            logger.exception("[执行] 目标词回显异常 → 沿用引擎话术")
            return ""

    def speech(self, plan: Plan, result: dict) -> str:
        args = plan.args
        intent = plan.intent
        # 场景
        if intent == "HassTriggerVoiceScene":
            msg = result.get("message")
            if msg and any("\u4e00" <= c <= "\u9fff" for c in msg):
                return msg if msg.endswith(("。", "！", "!", "了")) else msg + "了"
            name = getattr(plan, "scene_name", None) or args.get("trigger_phrase", "场景")
            return f"好的，{name}场景已执行"
        if intent == "HassCreateVoiceScene":
            x = str(args.get("trigger_phrase") or "").strip()
            return f"好的，场景已创建，说「{x}」就能触发" if x else "好的，场景已创建"
        if intent == "HassDeleteVoiceScene":
            return "好的，场景已删除"
        if intent == "HassListVoiceScenes":
            scenes = result.get("scenes") or []
            if not scenes:
                return "你还没有创建过语音场景"
            names = "、".join((s.get("name") or s.get("trigger_phrase", "")) for s in scenes[:6])
            return f"目前有这些场景：{names}"
        # 语音自动化（060401 集成引擎，v1.0.30 补 addon 话术；本地创建路径由
        # pipeline 出富回显，这里兜 LLM 工具通道）
        if intent == "HassCreateAutomation":
            return "好的，自动化已创建，条件满足就会执行"
        if intent == "HassDeleteAutomation":
            return "好的，自动化已删除"
        if intent == "HassUpdateAutomation":
            return "好的，自动化已更新"
        if intent == "HassListAutomations":
            autos = result.get("automations") or []
            if not autos:
                return "你还没有创建过语音自动化"
            return f"目前有 {len(autos)} 条语音自动化"
        # 空调温度直改（HA 内置意图改道）
        if intent == "HassClimateSetTemperature":
            t = args.get("temperature")
            area = args.get("area") or ""
            return f"好的，{area}空调已调到{t}度"
        if intent == "HassGetCurrentTime":
            return result.get("speech", {}).get("plain", {}).get("output", "") or "好的"
        # 解锁/上锁（v1.0.20 车道；真机名实相符话术，集成 intent_lock 返回 states 带 name）
        if intent in ("HassUnlock", "HassLock"):
            names = [s.get("name", "") for s in (result.get("states") or [])
                     if s.get("success") and s.get("name")]
            who = "、".join(names[:3])
            verb = "已解锁" if intent == "HassUnlock" else "已上锁"
            return f"好的，{who}{verb}" if who else \
                ("好的，锁已打开" if intent == "HassUnlock" else "好的，已上锁")
        # control_targets 族（TurnDeviceOn/Off、ControlWindow、AdjustDeviceAttribute、SetDeviceMode）
        targets = result.get("control_targets") or []
        if targets:
            return self._targets_speech(plan, targets)
        if (result.get("message") or "").strip():
            msg = str(result["message"]).strip()
            return msg if any("\u4e00" <= c <= "\u9fff" for c in msg) else zh_error(msg)
        if result.get("states"):
            names = [s.get("name", "") for s in result["states"] if s.get("success")]
            if plan.intent == "AdjustDeviceAttribute" and names:
                # v1.0.34：属性调节族不回 control_targets，旧话术只剩"已处理"
                # 丢数值（甚至因 raw 折叠只剩裸「好的」）——借 control_targets
                # 族同一模板按 state 名+slots 拼整句（如"射灯的亮度已设为10%"）。
                return self._targets_speech(plan, [{"name": n} for n in names])
            return f"好的，{'、'.join(names) if names else '设备'}已处理"
        if "success_count" in result:
            return "好的，已执行"
        return "好的"

    def _targets_speech(self, plan: Plan, targets: list) -> str:
        names = "、".join([t.get("name", "") for t in targets if t.get("name")]) or "设备"
        areas = [t.get("area", "") for t in targets if t.get("area")]
        area = areas[0] if areas else ""
        # v1.1.28：区域继承补好后，friendly_name 自带房间名的实体（「办公室空调 Air
        # Conditioner」）会被拼成「办公室的办公室空调 …」——念出来是重复房间名。名字
        # 已经以该区域名开头时不再补前缀（只改播报，不改成败、不改目标）。
        head = f"{area}的" if area and not str(names).startswith(area) else ""
        args = plan.args
        intent = plan.intent
        # 锁语义反转（D7 兜底）：目标名含「锁」
        if any("锁" in (t.get("name") or "") for t in targets):
            if intent == "TurnDeviceOn":
                return f"好的，{head}{names}已上锁"
            if intent == "TurnDeviceOff":
                return f"好的，{head}{names}已解锁"
        if intent == "TurnDeviceOn":
            return f"好的，{head}{names}打开了"
        if intent == "TurnDeviceOff":
            return f"好的，{head}{names}关了"
        if intent == "PauseDevice":            # v1.0.42 家电族（扫地机器人/电视/窗帘）
            return f"好的，{head}{names}暂停了"
        if intent == "ControlWindow":
            if args.get("speed") is not None:
                # 开窗器速度/力度参数（网关 v1.4.3+ number 滑动条）：集成端
                # 正常回中文 message，此分支兜 message 缺失
                return f"好的，{head}{names}速度已设为{args['speed']}%"
            if args.get("strength") is not None:
                return f"好的，{head}{names}力度已设为{args['strength']}%"
            if args.get("position") is not None:
                # 百分比开度（集成端正常会带中文 message 直播；此分支兜底）
                return f"好的，{head}{names}开到{args['position']}%"
            act = ACT_CN.get(str(args.get("action", "")).lower(), "调节")
            return f"好的，{head}{names}已{act}"
        if intent == "AdjustDeviceAttribute":
            attr = ATTR_CN.get(args.get("attribute", ""), args.get("attribute", ""))
            delta = str(args.get("delta", ""))
            if args.get("attribute") == "color":
                delta = color_word(delta) or delta   # #RRGGBB 念进 TTS 是噪音
            if delta.startswith("+") or delta.startswith("-"):
                up = delta.startswith("+")
                verb = {"brightness": ("调亮", "调暗"), "fan_speed": ("调大", "调小"),
                        "temperature": ("调高", "调低"), "position": ("开大", "关小"),
                        "color_temperature": ("调冷", "调暖")}.get(args.get("attribute"), ("调高", "调低"))
                return f"好的，{head}{names}的{attr}{verb[0] if up else verb[1]}了" if attr else f"好的，已调节{names}"
            if args.get("attribute") == "temperature":
                return f"好的，{head}{names}温度调到{delta}度了"
            unit = ("%" if args.get("attribute") in ("brightness", "position")
                    # 色温/颜色无量纲可播：K 与 # 念出来只是噪音（「色温已设为
                    # 4000」「颜色已设为暖白」），v1.1.1 把原 `attr != '色温'`
                    # 单点判断扩成集合，颜色族加入后不再漏。
                    else "" if args.get("attribute") in ("color_temperature", "color")
                    else "档")
            return f"好的，{head}{names}的{attr}已设为{delta}{unit}".replace("档档", "档")
        if intent == "SetDeviceMode":
            mode = MODE_CN.get(args.get("mode", ""), args.get("mode", ""))
            return f"好的，{head}{names}已切到{mode}模式"
        # 未知成功
        return f"好的，{head}{names}已处理"
