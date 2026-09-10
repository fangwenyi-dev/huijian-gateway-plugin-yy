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

from .nlu.fast_path import Plan, is_pronoun, normalize_polite

logger = logging.getLogger("huijian.executor")

ACT_CN = {"open": "打开", "close": "关闭", "pause": "暂停", "a": "内倒", "tilt": "内倒"}
ATTR_CN = {"brightness": "亮度", "colour_temperature": "色温", "color_temperature": "色温",
           "temperature": "温度", "fan_speed": "风量", "position": "开合度"}
MODE_CN = {"heat": "制热", "cool": "制冷", "dry": "除湿", "fan_only": "送风", "auto": "自动",
           "eco": "节能", "sleep": "睡眠", "offline": "关闭",
           "comfort": "舒适", "silent": "静音", "boost": "强力", "normal": "标准"}
_EN_ERR_MAP = [
    ("could not extract window name", "没找到要控制的窗户，试试说「客厅的窗户内倒」"),
    ("window control failed", "窗户控制没成功，可能窗户没在 HA 里配好"),
    ("no available", "没找到符合条件的设备，试试带上房间名或换个叫法"),
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
    return f"抱歉，这一步没有执行成功（{raw[:30]}），可以换个说法再试"


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
    r"^(?:打开来|打开|关闭|关掉|开了|关了|开一下|关一下|开启|关上|启动|停止|切换(?!器)|开(?!关)|关|换)")
_ECHO_TAIL = re.compile(
    r"(?:打开来|打开|关闭|关掉|开一下|关一下|开启|关上|起来|启动|停止|(?<!开)关|开)+$")
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


class Executor:
    def __init__(self, ha, settings=None):
        self.ha = ha
        self.settings = settings

    async def run(self, plan: Plan) -> tuple[bool, str]:
        """执行 Plan（klar 多分句/复合链逐步顺序执行）。返回 (success, 中文播报)。永不抛。"""
        steps = [(plan.intent, plan.args)] + [
            (st.get("name"), st.get("args") or {})
            for st in (getattr(plan, "extra_steps", None) or [])]
        results = []
        for idx, (name, args) in enumerate(steps):
            direct = self._klar_direct(name, args) if plan.source == "klar" else None
            if direct is not None:
                domain, service, data = direct
                result = await self.ha.call_service(domain, service, data)
            else:
                result = await self.ha.handle_intent(name, args)
            if not result.get("success"):
                reply = zh_error(str(result.get("error") or result.get("message") or ""),
                               klar=plan.source == "klar")
                # P2-12 链失败定位：部分执行已成事实，如实说清第几步、还剩几步
                # （保留"抱歉"字头——话术层诚实失败纪律被测试钉死）
                detail = reply[3:] if reply.startswith("抱歉，") else reply
                if len(steps) > 1 and idx > 0:
                    reply = f"抱歉，前面 {idx} 步已完成，但第 {idx + 1} 步没成功——{detail}"
                elif len(steps) > 1:
                    reply = f"抱歉，第 1 步没成功，后面的步骤先不执行了（{detail}）"
                logger.info("[执行] %s %s → 失败 | %s", name, args, reply)
                return False, reply
            results.append(result)
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
                for i, ((n, a), r) in enumerate(zip(steps, results)):
                    s = self.speech(Plan(intent=n, args=a, source=plan.source,
                                         utterance=plan.utterance), r)
                    if i > 0:
                        s = s.removeprefix("好的，")
                    segs.append(s)
                reply = "，".join(x for x in segs if x) or "好的，都办妥了"
                if not reply.startswith("好的"):
                    reply = "好的，" + reply
            except Exception:
                reply = "好的，都办妥了"
        tag = f"(+%d步)" % (len(steps) - 1) if len(steps) > 1 else ""
        logger.info("[执行] %s %s%s → 成功 | %s", plan.intent, plan.args, tag, reply)
        return True, reply

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
        "HassSetPosition": ("cover", "set_position"),
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
                data = {"entity_id": raw}
                for k in self._KLAR_KEYS["HassLightSet"]:
                    v = args.get(k if k != "color_name" else "color")
                    if v is None and k == "color_name":
                        v = args.get("color")
                    if v is not None:
                        data[k] = v
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
            return f"好的，{'、'.join(names) if names else '设备'}已处理"
        if "success_count" in result:
            return "好的，已执行"
        return "好的"

    def _targets_speech(self, plan: Plan, targets: list) -> str:
        names = "、".join([t.get("name", "") for t in targets if t.get("name")]) or "设备"
        areas = [t.get("area", "") for t in targets if t.get("area")]
        area = areas[0] if areas else ""
        head = f"{area}的" if area else ""
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
        if intent == "ControlWindow":
            act = ACT_CN.get(str(args.get("action", "")).lower(), "调节")
            return f"好的，{head}{names}已{act}"
        if intent == "AdjustDeviceAttribute":
            attr = ATTR_CN.get(args.get("attribute", ""), args.get("attribute", ""))
            delta = str(args.get("delta", ""))
            if delta.startswith("+") or delta.startswith("-"):
                up = delta.startswith("+")
                verb = {"brightness": ("调亮", "调暗"), "fan_speed": ("调大", "调小"),
                        "temperature": ("调高", "调低"), "position": ("开大", "关小"),
                        "color_temperature": ("调冷", "调暖")}.get(args.get("attribute"), ("调高", "调低"))
                return f"好的，{head}{names}的{attr}{verb[0] if up else verb[1]}了" if attr else f"好的，已调节{names}"
            if args.get("attribute") == "temperature":
                return f"好的，{head}{names}温度调到{delta}度了"
            unit = "%" if args.get("attribute") in ("brightness", "position") else ("K" if args.get("attribute") == "color_temperature" else "档")
            return f"好的，{head}{names}的{attr}已设为{delta}{unit if attr != '色温' else ''}".replace("档档", "档")
        if intent == "SetDeviceMode":
            mode = MODE_CN.get(args.get("mode", ""), args.get("mode", ""))
            return f"好的，{head}{names}已切到{mode}模式"
        # 未知成功
        return f"好的，{head}{names}已处理"
