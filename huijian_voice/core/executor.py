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
from typing import Optional

from .nlu.fast_path import Plan

logger = logging.getLogger("huijian.executor")

ACT_CN = {"open": "打开", "close": "关闭", "pause": "暂停", "a": "内倒", "tilt": "内倒"}
ATTR_CN = {"brightness": "亮度", "colour_temperature": "色温", "color_temperature": "色温",
           "temperature": "温度", "fan_speed": "风量", "position": "开合度"}
MODE_CN = {"heat": "制热", "cool": "制冷", "dry": "除湿", "fan_only": "送风", "auto": "自动",
           "eco": "节能", "sleep": "睡眠", "offline": "关闭"}
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


class Executor:
    def __init__(self, ha, settings=None):
        self.ha = ha
        self.settings = settings

    async def run(self, plan: Plan) -> tuple[bool, str]:
        """执行 Plan（klar 多分句逐步顺序执行）。返回 (success, 中文播报)。永不抛。"""
        steps = [(plan.intent, plan.args)] + [
            (st.get("name"), st.get("args") or {})
            for st in (getattr(plan, "extra_steps", None) or [])]
        results = []
        for name, args in steps:
            direct = self._klar_direct(name, args) if plan.source == "klar" else None
            if direct is not None:
                domain, service, data = direct
                result = await self.ha.call_service(domain, service, data)
            else:
                result = await self.ha.handle_intent(name, args)
            if not result.get("success"):
                reply = zh_error(str(result.get("error") or result.get("message") or ""),
                               klar=plan.source == "klar")
                logger.info("[执行] %s %s → 失败 | %s", name, args, reply)
                return False, reply
            results.append(result)
        klar_speech = (getattr(plan, "speech", "") or "").strip()
        if klar_speech:
            # klar 引擎自带的中文播报（zh_cn pack 产出）优于话术层泛化模板
            reply = klar_speech
        elif len(results) == 1:
            reply = self.speech(plan, results[0])
        else:
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
            return "好的，场景已创建"
        if intent == "HassDeleteVoiceScene":
            return "好的，场景已删除"
        if intent == "HassListVoiceScenes":
            scenes = result.get("scenes") or []
            if not scenes:
                return "你还没有创建过语音场景"
            names = "、".join((s.get("name") or s.get("trigger_phrase", "")) for s in scenes[:6])
            return f"目前有这些场景：{names}"
        # 空调温度直改（HA 内置意图改道）
        if intent == "HassClimateSetTemperature":
            t = args.get("temperature")
            area = args.get("area") or ""
            return f"好的，{area}空调已调到{t}度"
        if intent == "HassGetCurrentTime":
            return result.get("speech", {}).get("plain", {}).get("output", "") or "好的"
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
