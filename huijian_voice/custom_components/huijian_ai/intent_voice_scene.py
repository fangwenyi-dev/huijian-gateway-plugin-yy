import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import intent
from homeassistant.helpers.storage import Store
from homeassistant.util.json import JsonObjectType

from .intent_device_shared import split_actions_by_device
from .intent_result import fold_action_ok
from .intent_helper import validate_slots_safely

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "huijian_voice_scenes"
STORAGE_VERSION = 1


class VoiceSceneStore:
    """Manage voice scene storage using HA's storage mechanism."""

    def __init__(self, hass: HomeAssistant):
        self._hass = hass
        self._store: Store | None = None
        self._data: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    async def _get_store(self) -> Store:
        """Get or create the store instance."""
        if self._store is None:
            self._store = Store(self._hass, STORAGE_VERSION, STORAGE_KEY)
        return self._store

    async def _load_data(self) -> dict[str, Any]:
        """Load data from storage."""
        if self._data is None:
            store = await self._get_store()
            self._data = await store.async_load() or {
                "version": 1,
                "scenes": {},
                "trigger_index": {},
            }
        return self._data

    async def _save_data(self, data: dict[str, Any]) -> None:
        """Save data to storage."""
        self._data = data
        store = await self._get_store()
        await store.async_save(data)

    async def get_scene_by_trigger(self, trigger_phrase: str) -> dict[str, Any] | None:
        """Get scene by trigger phrase."""
        data = await self._load_data()
        scene_id = data.get("trigger_index", {}).get(trigger_phrase)
        if scene_id:
            return data.get("scenes", {}).get(scene_id)
        return None

    async def get_scene_by_id(self, scene_id: str) -> dict[str, Any] | None:
        """Get scene by ID."""
        data = await self._load_data()
        return data.get("scenes", {}).get(scene_id)

    async def get_all_scenes(self) -> list[dict[str, Any]]:
        """Get all scenes."""
        data = await self._load_data()
        return list(data.get("scenes", {}).values())

    async def create_scene(
        self, trigger_phrase: str, actions: list[dict[str, Any]]
    ) -> tuple[bool, str]:
        """Create a new scene.

        Returns:
            tuple: (success, scene_id or error_message)
        """
        async with self._lock:
            data = await self._load_data()

            if trigger_phrase in data.get("trigger_index", {}):
                return False, f"触发词'{trigger_phrase}'已存在，请使用其他词"

            scene_id = f"voice_scene_{datetime.now().strftime('%Y%m%d%H%M%S')}"
            # v1.1.27（批7）：秒级时间戳同秒两次创建会算出同一 ID——后写顶掉
            # 前写（scene 被覆盖），而 trigger_index 两条触发词反指同一 scene
            # （旧触发词指到新动作上）。冲突即加后缀唯一化（不换形态，管理页
            # 与 _bad_id 闸只认无点/无空格形）。
            if scene_id in data.get("scenes", {}):
                n = 1
                while f"{scene_id}_{n}" in data.get("scenes", {}):
                    n += 1
                scene_id = f"{scene_id}_{n}"
            scene = {
                "scene_id": scene_id,
                "trigger_phrase": trigger_phrase,
                "actions": actions,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            data["scenes"][scene_id] = scene
            data.setdefault("trigger_index", {})[trigger_phrase] = scene_id

            await self._save_data(data)
            _LOGGER.info("Created voice scene: %s, trigger: %s", scene_id, trigger_phrase)
            return True, scene_id

    async def delete_scene(
        self, trigger_phrase: str | None = None, scene_id: str | None = None
    ) -> tuple[bool, str]:
        """Delete a scene by trigger phrase or scene ID.

        Returns:
            tuple: (success, message)
        """
        async with self._lock:
            data = await self._load_data()

            if trigger_phrase:
                actual_scene_id = data.get("trigger_index", {}).get(trigger_phrase)
                if not actual_scene_id:
                    return False, f"未找到触发词'{trigger_phrase}'对应的场景"
                scene_id = actual_scene_id

            if scene_id:
                scene = data.get("scenes", {}).get(scene_id)
                if not scene:
                    return False, f"未找到场景ID'{scene_id}'对应的场景"

                trigger = scene.get("trigger_phrase")
                if trigger and trigger in data.get("trigger_index", {}):
                    del data["trigger_index"][trigger]

                del data["scenes"][scene_id]
                await self._save_data(data)
                _LOGGER.info("Deleted voice scene: %s", scene_id)
                return True, f"已删除语音场景：{trigger or scene_id}"
            else:
                return False, "请提供trigger_phrase或scene_id"

    async def update_scene(
        self,
        scene_id: str,
        trigger_phrase: str | None = None,
        actions: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, str]:
        """Update a scene's trigger phrase and/or actions.

        Returns:
            tuple: (success, message)
        """
        async with self._lock:
            data = await self._load_data()
            scenes = data.get("scenes", {})
            if scene_id not in scenes:
                return False, f"未找到场景ID'{scene_id}'"

            scene = scenes[scene_id]
            old_trigger = scene.get("trigger_phrase", "")

            if trigger_phrase is not None and trigger_phrase != old_trigger:
                if trigger_phrase in data.get("trigger_index", {}):
                    return False, f"触发词'{trigger_phrase}'已存在"
                if old_trigger and old_trigger in data.get("trigger_index", {}):
                    del data["trigger_index"][old_trigger]
                data.setdefault("trigger_index", {})[trigger_phrase] = scene_id
                scene["trigger_phrase"] = trigger_phrase

            if actions is not None:
                # 修⑤：只有动作**真的变了**才重盖 created_at。对抗复核实证过宽形：
                # 客户端读-改-写回（actions 一字未动）也会重盖 ⇒ 旧启发式补的匿名
                # 区级窗动作被放回执行面（按区压所有窗钮）。"变了才重新计时"才是
                # 用户对这份形状做了当下确认；原样重存不是。
                changed = actions != scene.get("actions")
                scene["actions"] = actions
                if changed:
                    # 与 `create_scene` 同一枚时钟、同一格式（:101），否则时间闸失义。
                    scene["created_at"] = datetime.now(timezone.utc).isoformat()

            await self._save_data(data)
            _LOGGER.info("Updated voice scene: %s", scene_id)
            return True, f"已更新语音场景：{scene.get('trigger_phrase', scene_id)}"


_store_instance: VoiceSceneStore | None = None


def get_voice_scene_store(hass: HomeAssistant) -> VoiceSceneStore:
    """Get the singleton store instance."""
    global _store_instance
    if _store_instance is None:
        _store_instance = VoiceSceneStore(hass)
    return _store_instance


def reset_voice_scene_globals():
    """Reset global singleton reference for clean reload."""
    global _store_instance
    _store_instance = None


class HassCreateVoiceSceneIntent(intent.IntentHandler):
    intent_type = "HassCreateVoiceScene"
    description = (
        "Creates a voice-triggered scene that stores trigger phrase and actions. "
        "Use ONLY when user says something like '当我说xxx的时候，帮我执行yyy', "
        "'你听到我说xxx就yyy', '如果我说xxx就开机'. "
        "DO NOT use for sensor/condition-based triggers (temperature, humidity, etc.) - "
        "use HassCreateAutomation for those. "
        "IMPORTANT WINDOW RULE: If user says '打开窗户'/'打开展厅的平推窗' etc., "
        "use TurnDeviceOn (NOT ControlWindow). The system will auto-convert it to ControlWindow(open). "
        "If user says '关闭窗户'/'关窗' etc., "
        "use TurnDeviceOff. The system will auto-convert it to ControlWindow(close). "
        "Parameters: trigger_phrase (a spoken phrase that will trigger the scene), "
        "actions (array of intent+params objects)."
    )

    @property
    def slot_schema(self) -> dict | None:
        return {
            vol.Required("trigger_phrase"): cv.string,
            vol.Required("actions"): vol.All(cv.ensure_list, [dict]),
        }

    async def async_handle(self, intent_obj: intent.Intent) -> JsonObjectType:
        slots, fail = validate_slots_safely(
            self, intent_obj, "HassCreateVoiceScene")
        if fail is not None:
            return fail
        _LOGGER.info("HassCreateVoiceScene slots=%s", slots)

        trigger_phrase = slots.get("trigger_phrase", {}).get("value", "")
        actions = slots.get("actions", {}).get("value", [])

        if not trigger_phrase or not trigger_phrase.strip():
            return {"success": False, "error": "触发词不能为空"}

        if not actions:
            return {"success": False, "error": "动作列表不能为空"}

        # 只做「明说的窗设备 → ControlWindow」拆分（split_actions_by_device），
        # 不做任何「同区多域动作自动补窗」推断：用户/模型没点名的窗，任何方向都
        # 不许动——误开/误关窗的代价远大于漏做（产品口径同「所有设备不冒然全动」）。
        split_actions = split_actions_by_device(actions)
        _LOGGER.info("HassCreateVoiceScene split_actions=%s", split_actions)

        store = get_voice_scene_store(intent_obj.hass)
        success, result = await store.create_scene(trigger_phrase, split_actions)

        if success:
            return {
                "success": True,
                "scene_id": result,
                "message": f"已创建语音场景：{trigger_phrase}",
            }
        else:
            return {"success": False, "error": result}


# 「自动补窗」启发式退场时刻：v1.1.33（2026-09-30）起创建侧不再产出按区补窗动作。
# 只有**创建时刻早于它**的存量记录才可能是旧启发式的产物；此后创建的匿名区级窗动作
# 只能来自模型/面板的正当写入（`intent_automation.py` 的 LLM 示例逐字就是
# target:[{area,devices:[{domains:[button]}]}]，且现网实证模型仍会写出"单动作多域"），
# 一律不跳。
_AUTO_WINDOW_RETIRED_AT = datetime(2026, 9, 30, tzinfo=timezone.utc)


def _scene_predates_auto_window_retirement(created_at: Any) -> bool:
    """场景创建时刻是否早于启发式退场——只有"是"才有资格被当作存量补窗。

    无 created_at / 解析失败 ⇒ False（放行）：启发式唯一的写入口是
    `VoiceSceneStore.create_scene`，而它自 v1.0.0 首发就逐条盖 created_at
    ⇒ **没戳的记录不可能出自启发式那条路**（PUT/面板写的同理），不凭"猜旧"动手。
    """
    if not created_at:
        return False
    try:
        raw = str(created_at).strip().replace("Z", "+00:00")
        ts = datetime.fromisoformat(raw)
    except Exception:  # noqa: BLE001 时间戳形态异常按"非存量"处置，绝不因它跳动作
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts < _AUTO_WINDOW_RETIRED_AT


def legacy_auto_window_area(action: dict, siblings: list,
                            created_at: Any = None) -> str:
    """识别**已删除的**「自动补窗」启发式留在存量库里的窗动作，回其区域名；否则空串。

    启发式（v1.0.0~v1.1.32）已在创建侧删除，但它当年追加的动作仍躺在用户
    `.storage` 里（STORAGE_VERSION 未升版、无迁移），回放会按区域压该区所有窗钮
    ——正是它被删掉的理由本身，所以删创建不等于止损。四件判据缺一不认（认不准
    就按用户意图照常执行，宁漏不误删）：
      ⓪该场景的创建时刻早于启发式退场（见上）；
      ①窗动作目标只带 area、devices 里没有设备名（匿名）；
      ②同场景有 TurnDeviceOn/Off 指向同一区域；
      ③那条开关覆盖 ≥2 个非 button 域，且窗动作方向与它同向
        （旧码 window_action = "open" if TurnDeviceOn else "close"）。
    """
    if not _scene_predates_auto_window_retirement(created_at):
        return ""
    # 修⑤：键形必须是启发式那一路（`name`+`parameters`）。启发式唯一的写入口
    # `VoiceSceneStore._auto_supplement_windows`（v1.1.32 及以前）逐字写的是
    # `{"name": "ControlWindow", "parameters": {...}}`；而工具通道/自动化存的是
    # 模型自己写的 `{"intent": ..., "params": ...}`（intent_automation.py 的
    # schema 示例逐字即此形）。旧判据 `name or intent` 把两路混在一起 ⇒ **模型正当
    # 写入的匿名区级窗动作被当存量启发式永久跳过**（2026-10-01 探针实得：
    # 用户明说「开客厅灯+空调+开客厅的窗」→ legacy_auto_window_area 回「客厅」）。
    # 认不准就不动，这是本闸自订的"宁漏不误删"纪律的应有之义。
    if "name" not in action and "parameters" not in action:
        return ""
    # 位置判据（同族第四件）：旧启发式只会 `new_actions.append(...)`——补的那条**恒在
    # 末尾**（v1.1.32 及以前源码实证）。因此"不在末尾的匿名区级窗"绝不可能是它的产物，
    # 只可能是用户/模型自己写的位置。这一格只能**减少**误跳，不可能放过真启发式产物。
    if not siblings or siblings[-1] is not action:
        return ""
    if (action.get("name") or action.get("intent")) not in (
            "ControlWindow", "WindowControl"):
        return ""
    params = action.get("params") or action.get("parameters") or {}
    targets = params.get("target") or []
    if isinstance(targets, dict):
        targets = [targets]
    if len(targets) != 1 or not isinstance(targets[0], dict):
        return ""
    t = targets[0]
    area = str(t.get("area") or "").strip()
    devices = t.get("devices") or []
    if isinstance(devices, dict):
        devices = [devices]
    if not area or any(isinstance(d, dict) and str(d.get("name") or "").strip()
                       for d in devices):
        return ""
    want = str(params.get("action") or "").strip().lower()
    if want not in ("open", "close"):
        return ""
    for sib in siblings:
        if sib is action or not isinstance(sib, dict):
            continue
        s_name = sib.get("name") or sib.get("intent")
        if s_name not in ("TurnDeviceOn", "TurnDeviceOff"):
            continue
        if want != ("open" if s_name == "TurnDeviceOn" else "close"):
            continue
        s_params = sib.get("params") or sib.get("parameters") or {}
        s_targets = s_params.get("target") or []
        if isinstance(s_targets, dict):
            s_targets = [s_targets]
        domains: set = set()
        same_area = False
        for st in s_targets:
            if not isinstance(st, dict):
                continue
            if str(st.get("area") or "").strip() == area:
                same_area = True
            for d in (st.get("devices") or []):
                if isinstance(d, dict):
                    domains.update(str(x) for x in (d.get("domains") or []))
        if same_area and len([d for d in domains if d != "button"]) >= 2:
            return area
    return ""


class HassTriggerVoiceSceneIntent(intent.IntentHandler):
    intent_type = "HassTriggerVoiceScene"
    description = (
        "Triggers an existing voice scene by its trigger phrase. "
        "Use when user says the trigger phrase to execute a previously created scene. "
        "Parameters: trigger_phrase (string)."
    )
    service_timeout = 30

    @property
    def slot_schema(self) -> dict | None:
        return {
            vol.Required("trigger_phrase"): cv.string,
        }

    async def async_handle(self, intent_obj: intent.Intent) -> JsonObjectType:
        """Handle voice scene trigger - execute stored actions."""
        slots, fail = validate_slots_safely(
            self, intent_obj, "HassTriggerVoiceScene")
        if fail is not None:
            return fail
        _LOGGER.info("HassTriggerVoiceScene slots=%s", slots)

        trigger_phrase = slots.get("trigger_phrase", {}).get("value", "")

        if not trigger_phrase:
            return {"success": False, "error": "触发词不能为空"}

        store = get_voice_scene_store(intent_obj.hass)
        scene = await store.get_scene_by_trigger(trigger_phrase)

        if not scene:
            return {
                "success": False,
                "error": f"未找到触发词'{trigger_phrase}'对应的场景",
            }

        executed_actions = []
        # v1.1.27（批7 M7 同口）存量脏形态韧性：PUT 早期版本/手工改 .storage 可
        # 让 actions 混入非 dict（api.py 已加形态闸，存量数据仍可能带）——旧实现
        # 在此裸 .get 直接 AttributeError → REST 500。非 dict 跳过，不炸。
        raw_actions = scene.get("actions")
        if not isinstance(raw_actions, list):
            raw_actions = []
        replay_actions = [a for a in raw_actions if isinstance(a, dict)]
        if not replay_actions:
            # PUT 允许写 actions: []（形态闸只判 list[dict]），而回放空列表
            # all([])==True 会假报「已执行场景」——空动作如实拒回放。
            _LOGGER.warning("场景「%s」无可执行动作（actions=%r）",
                            trigger_phrase, raw_actions)
            msg = f"场景「{trigger_phrase}」没有可执行的动作（动作列表为空）"
            return {
                "success": False,
                "scene_id": scene.get("scene_id"),
                "executed_actions": [],
                "error": msg,
                "message": msg,
            }
        legacy_skips: list = []
        partial_notes: list = []          # 修③：整步可用但有个别台没动 ⇒ 必须点名
        for action in replay_actions:
            intent_name = action.get("intent") or action.get("name")
            params = action.get("params") or action.get("parameters", {})
            _lw_area = legacy_auto_window_area(action, replay_actions,
                                               scene.get("created_at"))
            if _lw_area:
                # 存量旧启发式补的窗动作：**不执行，也不静默**——回执里点名说
                # 跳过了哪个区域（用户没点名的窗任何方向都不动；要恢复就在场景
                # 里明说开窗，或去面板删掉这条动作）。
                _lw_dir = {"open": "开窗", "close": "关窗"}.get(
                    str(params.get("action") or "").strip().lower(), "窗动作")
                _LOGGER.warning("场景「%s」存量自动补窗动作（区域=%s %s）已跳过",
                                trigger_phrase, _lw_area, _lw_dir)
                executed_actions.append(
                    {"intent": intent_name, "result": "skipped_legacy",
                     "reason": f"旧版自动补的「{_lw_area}」{_lw_dir}动作已跳过"}
                )
                legacy_skips.append((_lw_area, _lw_dir))
                continue
            _LOGGER.info(
                f"Executing scene action: intent={intent_name}, params={params}"
            )

            try:
                result = await self._execute_action_with_timeout(
                    intent_obj, intent_name, params
                )
                # H3（2026-09-23 深审）：成败判定此前只认**异常**，而
                # _execute_intent 把不支持类型/IntentHandleError 全折叠成
                # {"success": False} 返回值——零动作生效也回「已执行场景」。
                # 本文件 S1 折算的存在即意图反证。折叠值与 IntentResponse
                # 对象双形态判据。
                # v1.1.29 复核 A5：折算收口到 intent_result.fold_action_ok（同族第 4 处
                # 漏网：SetDeviceMode 族只回 {"results":[…]}，无 success 键时旧判据恒真）。
                ok, _err = fold_action_ok(result)
                if ok:
                    executed_actions.append(
                        {"intent": intent_name, "result": "success", "detail": result}
                    )
                    if _err:
                        # 修③：`{"results":[{成},{败}]}` 折算=该步可用（确实动了东西），
                        # 但"有一台没动"绝不能被「已执行场景」吞掉（真机红线：播报与
                        # 事实一致；turn 族 v1.1.31 已按逐台真值同规点名，此处补同族）。
                        partial_notes.append(f"{intent_name}：{_err[:40]}")
                else:
                    err = ""
                    if isinstance(result, dict):
                        err = str(result.get("error") or "")
                    else:
                        err = str(getattr(result, "error", "") or "执行未成功")
                    _LOGGER.error("Scene action folded failure: %s: %s",
                                  intent_name, err)
                    executed_actions.append(
                        {"intent": intent_name, "result": "error",
                         "error": err or "执行未成功"}
                    )
            except asyncio.TimeoutError:
                _LOGGER.error("Action timeout: intent=%s", intent_name)
                executed_actions.append(
                    {"intent": intent_name, "result": "error", "error": "执行超时"}
                )
            except Exception as e:
                _LOGGER.error("Failed to execute action: %s", e)
                executed_actions.append(
                    {"intent": intent_name, "result": "error", "error": str(e)}
                )

        # v1.1.27：空回放已在上方拒掉，此处 bool 兜底防「零动作=全成功」复活
        # （PUT 可写 actions: []，all([])==True 会假报「已执行场景」）。
        all_success = all(
            a.get("result") != "error" for a in executed_actions)
        all_success = all_success and bool(executed_actions)
        out = {
            "success": all_success,
            "scene_id": scene.get("scene_id"),
            "executed_actions": executed_actions,
        }
        if all_success:
            out["message"] = f"已执行场景：{trigger_phrase}"
            if legacy_skips:
                out["message"] += (
                    "，旧版自动补的 "
                    + "、".join(f"「{a}」{d}" for a, d in dict.fromkeys(legacy_skips))
                    + "动作已跳过")
            if partial_notes:
                out["message"] += ("，有设备没动：" +
                                   "；".join(dict.fromkeys(partial_notes)))
        else:
            # v1.0.41 审查 S1：部分失败时 message 不能再带「已执行场景」成功话术——
            # core/executor 失败分支取 error or message 折叠播报，设备离线等失败会被
            # 播成"已执行"（与 v1.0.39 逐实体折算同病灶的场景路径漏网）。error 优先，
            # message 同步置失败文案，防直呼 message 的旧消费方二次踩雷。
            # 分母只算**真尝试过**的动作：被跳过的存量补窗不是失败，混进分母会把
            # 「1/1 没成功」播成「1/2 没成功」（用户听成两个动作坏了一个）。
            attempted = [a for a in executed_actions
                         if a.get("result") != "skipped_legacy"]
            nfail = sum(1 for a in attempted if a.get("result") == "error")
            fail_msg = f"场景「{trigger_phrase}」{nfail}/{len(attempted)} 个动作没执行成功"
            out["error"] = fail_msg
            out["message"] = fail_msg
        return out

    async def _execute_action_with_timeout(
        self, intent_obj: intent.Intent, intent_name: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute an intent action with timeout."""
        try:
            result = await asyncio.wait_for(
                self._execute_intent(intent_obj, intent_name, params),
                timeout=self.service_timeout,
            )
            return result
        except asyncio.TimeoutError:
            raise

    async def _execute_intent(
        self, intent_obj: intent.Intent, intent_name: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute an intent action by delegating to the registered IntentHandler.

        Calls the original IntentHandler via intent.async_handle, eliminating
        code duplication with intent_turn.py, intent_window_control.py, etc.
        """
        from homeassistant.helpers import intent as ha_intent

        from .const import DOMAIN

        if intent_name not in [
            "TurnDeviceOn",
            "TurnDeviceOff",
            "ControlWindow",
            "WindowControl",
            "AdjustDeviceAttribute",
            "SetDeviceMode",
        ]:
            return {"success": False, "error": f"不支持的intent类型: {intent_name}"}

        normalized_name = intent_name
        if intent_name == "WindowControl":
            normalized_name = "ControlWindow"

        try:
            ha_slots = {k: {"value": v} for k, v in params.items()}
            response = await ha_intent.async_handle(
                hass=intent_obj.hass,
                platform=DOMAIN,
                intent_type=normalized_name,
                slots=ha_slots,
                assistant=intent_obj.assistant,
                device_id=intent_obj.device_id,
            )
            return response
        except Exception as e:
            _LOGGER.error("Intent execution failed: %s: %s", intent_name, e)
            return {"success": False, "error": str(e)}


class HassDeleteVoiceSceneIntent(intent.IntentHandler):
    intent_type = "HassDeleteVoiceScene"
    description = (
        "Deletes an existing voice scene. "
        "Use when user wants to delete a created scene. "
        "Parameters: trigger_phrase (string) OR scene_id (string)."
    )

    @property
    def slot_schema(self) -> dict | None:
        return {
            vol.Optional("trigger_phrase"): cv.string,
            vol.Optional("scene_id"): cv.string,
        }

    async def async_handle(self, intent_obj: intent.Intent) -> JsonObjectType:
        slots, fail = validate_slots_safely(
            self, intent_obj, "HassDeleteVoiceScene")
        if fail is not None:
            return fail
        _LOGGER.info("HassDeleteVoiceScene slots=%s", slots)

        trigger_phrase = slots.get("trigger_phrase", {}).get("value")
        scene_id = slots.get("scene_id", {}).get("value")

        if not trigger_phrase and not scene_id:
            return {"success": False, "error": "请提供trigger_phrase或scene_id"}

        store = get_voice_scene_store(intent_obj.hass)
        success, message = await store.delete_scene(
            trigger_phrase=trigger_phrase, scene_id=scene_id
        )

        return {
            "success": success,
            "message": message if success else None,
            "error": message if not success else None,
        }


class HassListVoiceScenesIntent(intent.IntentHandler):
    intent_type = "HassListVoiceScenes"
    description = (
        "Lists all stored voice scenes. "
        "Use when user wants to see all created scenes. "
        "No parameters required."
    )

    @property
    def slot_schema(self) -> dict | None:
        return None

    async def async_handle(self, intent_obj: intent.Intent) -> JsonObjectType:
        _LOGGER.info("HassListVoiceScenes called")

        store = get_voice_scene_store(intent_obj.hass)
        scenes = await store.get_all_scenes()

        return {"success": True, "scenes": scenes}
