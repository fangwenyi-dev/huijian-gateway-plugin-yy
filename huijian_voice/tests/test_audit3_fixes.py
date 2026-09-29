# -*- coding: utf-8 -*-
"""第三轮独立审计 A 段修复钉（2026-09-29 复核：12 条声称逐条自核 → 11 条真、全部收口）。

A1 TurnDeviceOn×lock 锁定方向（两臂双向） / A2 清单锚点按 origin+TTL+GC /
A4 扩链与拒猜同源 / A5 折算单点（消费链对钉在 test_v1064_honesty_batch）/
A6 自动化 ID 秒级唯一化 / A7 调色预裁位 / A8/A9 时间解析 / A10 hex 形状闸 /
A11 真执行与破坏性写端点令牌闸。A3 链歧义闸的端到端钉在 test_nlu_llm_boundary。
"""
import ast
import asyncio
import os
import sys
import tempfile
import textwrap
import types
from datetime import datetime as _real_dt
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_audit3_"))
os.environ["HUIJIAN_NLU_DATA"] = str(HERE / "nlu_data")

from conftest import FakeHAClient                      # noqa: E402
from core import capability                            # noqa: E402
from core.executor import Executor                     # noqa: E402
from core.nlu import creation                          # noqa: E402
from core.nlu import targets as T                       # noqa: E402
from core.nlu.fast_path import Plan                    # noqa: E402
from core.pipeline import Pipeline                     # noqa: E402

CC = HERE / "custom_components" / "huijian_ai"


def _extract_func_src(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return textwrap.dedent(ast.get_source_segment(src, node))
    raise AssertionError(f"{path.name} 找不到函数 {name}")


class _S:
    def __init__(self, d=None):
        self.d = dict(d or {})

    def get(self, k, default=None):
        return self.d.get(k, default)


def _pipe(settings=None):
    p = Pipeline.__new__(Pipeline)
    p.settings = _S(settings)
    p._confirm = {}
    p._origin_ts = {}
    p._last_list = {}
    return p


# ── A1：TurnDeviceOn×lock 的锁定方向 ────────────────────────────────
def _lock_reply(state, results=None):
    states = {"lock.da_men_suo": {"entity_id": "lock.da_men_suo", "state": state,
                                  "attributes": {"friendly_name": "大门锁"}}}
    ex = Executor(FakeHAClient(states=states,
                               results=results or {"TurnDeviceOn": {"success": True}}), None)
    plan = Plan(intent="TurnDeviceOn",
                args={"target": [{"devices": [{"name": "门锁", "domains": ["lock"]}]}]},
                source="t0", utterance="打开门锁")
    return asyncio.run(ex.run(plan))


def test_a1_lock_success_never_says_unlocked():
    """上锁成功 ⇒ 绝不许出现「已解锁」方向的话术（旧码 want 取反）。"""
    ok, say = _lock_reply("locked")
    assert ok is True and "已解锁" not in say, say


def test_a1_lock_failure_is_named_not_silent():
    """真没锁上（快照仍 unlocked）⇒ 必须点名「还没确认到已上锁」，不许只回「好的」。"""
    ok, say = _lock_reply("unlocked")
    assert ok is True and "还没确认到已上锁" in say, say


def test_a1_lock_success_keeps_locked_wording():
    """带真回执时成功话术是 D7 的「已上锁」（旧码 bits 分支会把它整句吞掉）。"""
    ok, say = _lock_reply("locked", results={"TurnDeviceOn": {
        "success": True, "control_targets": [{"name": "大门锁", "area": ""}]}})
    assert "已上锁" in say and "已解锁" not in say, say


def test_a1_turn_off_lock_still_unlock_direction():
    """反向守卫：TurnDeviceOff×lock=解锁（集成 else 支 unlock），不得被改错方向。"""
    states = {"lock.da_men_suo": {"entity_id": "lock.da_men_suo", "state": "unlocked",
                                  "attributes": {"friendly_name": "大门锁"}}}
    ex = Executor(FakeHAClient(states=states,
                               results={"TurnDeviceOff": {"success": True}}), None)
    plan = Plan(intent="TurnDeviceOff",
                args={"target": [{"devices": [{"name": "门锁", "domains": ["lock"]}]}]},
                source="t0", utterance="关上门锁")
    ok, say = asyncio.run(ex.run(plan))
    assert "已上锁" not in say, say


# ── A2：清单锚点按 origin 分桶 + TTL + GC ───────────────────────────
def test_a2_anchor_is_per_origin_and_one_shot():
    p = _pipe()
    p._set_last_list("sat-A", "scene")
    assert p._peek_last_list("sat-A") == "scene"
    assert p._peek_last_list("sat-B") is None       # 别的卫星读不到（旧码是全局单值）
    assert p._take_last_list("sat-B") is None
    assert p._take_last_list("sat-A") == "scene"
    assert p._take_last_list("sat-A") is None       # 一次性


def test_a2_anchor_expires_by_ttl():
    import time as _t
    p = _pipe({"dialog.context_ttl_s": 5})
    p._set_last_list("o", "automation")
    p._last_list["o"] = ("automation", _t.time() - 6)
    assert p._take_last_list("o") is None           # 过期锚点不认（旧码永不过期）


def test_a2_anchor_is_gc_bounded():
    p = _pipe()
    for i in range(70):
        p._set_last_list(f"o{i}", "scene")
        p._origin_ts[f"o{i}"] = float(i)
    p._gc_origins()
    assert len(p._last_list) <= 64


# ── A4：扩链与拒猜同源 ─────────────────────────────────────────────
def test_a4_single_generic_tail_splits_or_refuses():
    """「客厅的灯」尾片是单字通用设备词 ⇒ 必须裂成两腿（旧码两判据都够不着）。"""
    assert T.coord_clauses("打开客厅的灯和空调") == ["打开客厅的灯", "打开客厅的空调"]
    # 反向守卫：裸 1 字片不扩链，但必须被拒猜拦住（不得单发半执行）
    assert T.coord_clauses("关灯和窗") == [] and T.coord_refuse("关灯和窗") is True


# ── A6：自动化 ID 秒级唯一化（冻结时钟） ────────────────────────────
def test_a6_automation_id_unique_within_same_second():
    class _Frozen:
        @staticmethod
        def now(tz=None):
            return _real_dt(2026, 9, 29, 12, 0, 0, tzinfo=tz)

    class _Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    store: dict = {}

    class Self:
        _lock = _Lock()

        async def _load_data(self):
            return store

        async def _save_data(self, data):
            store.update(data)

    ns = {"datetime": _Frozen, "timezone": __import__("datetime").timezone,
          "_LOGGER": types.SimpleNamespace(info=lambda *a, **k: None)}
    exec(compile(_extract_func_src(CC / "intent_automation.py",  # noqa: S102
                                   "create_automation"),
                 "<create_automation>", "exec"), ns)

    async def _twice():
        r1 = await ns["create_automation"](Self(), {"at": "07:00"}, [{"intent": "TurnDeviceOn"}])
        r2 = await ns["create_automation"](Self(), {"at": "08:00"}, [{"intent": "TurnDeviceOff"}])
        return r1, r2

    (ok1, id1), (ok2, id2) = asyncio.run(_twice())
    assert ok1 and ok2 and id1 != id2, (id1, id2)
    assert len(store.get("automations", {})) == 2, store    # 同秒两条都在（旧码后写顶前写）


# ── A7：调色预裁位（8=FLASH / 16=legacy 彩色） ─────────────────────
def _light(feats, modes=None):
    a = {"supported_features": feats}
    if modes is not None:
        a["supported_color_modes"] = modes
    return {"entity_id": "light.x", "state": "on", "attributes": a}


def test_a7_flash_bit_is_not_color_capability():
    assert capability.supports_attribute(_light(2), "color") is False        # 仅色温
    assert capability.supports_attribute(_light(8), "color") is False        # FLASH≠彩色
    assert capability.supports_attribute(_light(16), "color") is True        # legacy 彩色位
    assert capability.supports_attribute(_light(9, ["rgb"]), "color") is True  # 现代口径


# ── A8/A9：时间解析 ────────────────────────────────────────────────
def test_a8_minute_zero_and_tail_residue():
    assert creation.parse("每天早上七点零五分打开书房灯")["trigger"] == {"at": "07:05"}
    # 句尾分钟短语（无动作）＝没动作，如实拒建（旧码建成 07:00 + 动作="十分"）
    assert creation.parse("每天早上七点十分") is None
    assert creation.parse("每天七点十五分") is None


def test_a9_evening_twelve_is_midnight():
    assert creation.parse("每天晚上12点关闭所有灯")["trigger"] == {"at": "00:00"}
    assert creation.parse("每天晚上十二点关闭所有灯")["trigger"] == {"at": "00:00"}
    assert creation.parse("每天下午12点关闭所有灯")["trigger"] == {"at": "12:00"}  # 正午不变
    assert creation.parse("每天晚上十一点半关闭所有灯")["trigger"] == {"at": "23:30"}


# ── A10：hex 形状闸收成 3|6 ────────────────────────────────────────
def test_a10_hex_shape_gate_only_3_or_6():
    class _AT:
        SET = "set"

    ns = {"re": __import__("re"), "DELTA_SPECIAL_VALUES": (),
          "AdjustType": _AT}

    class _Delta:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    ns["Delta"] = _Delta
    exec(compile(_extract_func_src(CC / "intent_adjust_attribute.py",  # noqa: S102
                                   "parse_delta"), "<parse_delta>", "exec"), ns)
    pd = ns["parse_delta"]
    assert pd("#ABC") is not None and pd("#AABBCC") is not None
    assert pd("#ABCD") is None, "4 位必须拒（旧码会在 int('') 抛 ValueError 逃出 handler）"
    assert pd("#FFFFF") is None, "5 位必须拒（旧码静默算出 RGB(255,255,15)）"


# ── A11：真执行/破坏性写端点令牌闸（只读面保持匿名） ────────────────
def _class_block(name: str) -> str:
    src = (CC / "api.py").read_text(encoding="utf-8")
    i = src.index(f"class {name}(")
    return src[i:i + 400]


def test_a11_write_and_execute_views_require_auth():
    for cls in ("TestSceneView", "TestAutomationView",
                "VoiceSceneDeleteView", "AutomationDeleteView"):
        assert "requires_auth = True" in _class_block(cls), f"{cls} 必须 HA 令牌闸"


def test_a11_readonly_views_stay_anonymous():
    for cls in ("VoiceScenesListView", "AutomationsListView", "AutomationLogView"):
        assert "requires_auth = False" in _class_block(cls), f"{cls} 只读面保持匿名"


# ── A5：折算单点（helper 级；消费链对钉在 test_v1064_honesty_batch） ─
def _fold():
    ns: dict = {}
    exec(compile((CC / "intent_result.py").read_text(encoding="utf-8"),  # noqa: S102
                 "<intent_result.py>", "exec"), ns)
    return ns["fold_action_ok"]


def test_a5_fold_results_shape():
    fold = _fold()
    assert fold({"results": [{"success": False, "error": "不支持"}]})[0] is False
    assert fold({"results": []})[0] is False
    assert fold({"results": [{"success": False}, {"success": True}]})[0] is True
    assert fold({"success": False, "error": "x"})[0] is False
    assert fold({})[0] is True                      # 判不了不误伤
    assert fold({"error": "boom"})[0] is False      # 有失败证据必采信
    assert fold(types.SimpleNamespace(success=False, error="e"))[0] is False
