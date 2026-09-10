"""v1.0.40 修复批回归钉（A1-A6 / D3 / D4 / D10）。

每条钉子对应一个**已实证的真实缺陷**；钉子在修前必红、修后必绿：
- A1 fast_path 窗/帘动词表"最左优先"截断 → 常见说法整句落空（实测 7 个说法全 None）
- D3 T1 无 CloseCover 类 → "拉上/合上/收起窗帘"被反向执行成"打开"
- A2 显式全屋目标被上一轮上下文静默替换（说"所有灯"只动客厅灯）
- A3 脱敏回写哨兵与 masked() 格式不匹配 → 整块回传把 ws_token 写成 9 字符残串
- A4 settings.json 节点被写成非 dict 时启动崩（AttributeError）
- D4 Supervisor options 覆盖层落盘并每次启动压过 Web UI
- D10 pairing_token 未脱敏
- A5/A6/前端：落盘原子性、暴露判定留痕、HTML 转义（源码级钉）
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.nlu.fast_path import FastPath, _cover_intent          # noqa: E402
from core.pipeline import Pipeline, _is_wholehouse_args          # noqa: E402
from core.settings import Settings                               # noqa: E402
from tests.test_fast_path import FakeScenes                      # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class FakeTC:
    """可编程的 T1 替身：帘→OpenCover，窗→ControlWindow，其余不判。"""
    available = True

    def __init__(self, mapping=None):
        self.mapping = mapping or {}

    def predict(self, text):
        for kw, hit in self.mapping.items():
            if kw in text:
                return hit
        return None


def _fp(settings, mapping=None):
    return FastPath(FakeScenes(), FakeTC(mapping or {"帘": ("OpenCover", 0.92),
                                                     "窗": ("ControlWindow", 0.95)}),
                    settings)


# ── A1：窗/帘说法必须各有正确落点 ────────────────────────────────
@pytest.mark.parametrize("text,intent,name", [
    ("打开窗户", "ControlWindow", None),
    ("关闭窗户", "ControlWindow", None),
    ("开窗户", "ControlWindow", None),
    ("关窗户", "ControlWindow", None),
    ("打开窗户吧", "ControlWindow", None),
    # 窗帘是 cover 设备：不能被窗户动作吃掉前缀，也不能残留"帘"
    ("打开窗帘", "TurnDeviceOn", "窗帘"),
    ("关窗帘", "TurnDeviceOff", "窗帘"),
    ("关闭窗帘", "TurnDeviceOff", "窗帘"),
])
def test_a1_window_curtain_phrases(settings, text, intent, name):
    plan = asyncio.run(_fp(settings).match(text))
    assert plan is not None, f"{text!r} 整句落空（A1 复发）"
    assert plan.intent == intent
    if name:
        tgt = plan.args["target"]
        assert tgt[0]["devices"][0]["name"] == name
        assert tgt[0]["devices"][0]["domains"] == ["cover"]


def test_a1_actions_are_not_swallowed_by_short_alternatives():
    """根因钉：长词必须排在交替式前面，且窗户动作不吃"帘"。"""
    from core.nlu.fast_path import _ACTION_PATTERNS
    open_pat = _ACTION_PATTERNS[0][0]
    assert open_pat.match("打开窗户").group(0) == "打开窗户"
    assert open_pat.match("打开窗帘") is None
    close_pat = _ACTION_PATTERNS[1][0]
    assert close_pat.match("关闭窗户").group(0) == "关闭窗户"
    assert close_pat.match("关闭窗帘") is None


def test_a1_t1_retry_takes_over_when_t0_target_fails(settings):
    """A1 后半：T0 命中但目标提取落空时，T1 接管（旧实现直接落空）。"""
    fp = _fp(settings, {"某": ("TurnDeviceOn", 0.9)})
    # 构造 T0 命中、rest 不可解析 → _build_plan 落空 → T1-retry 接管
    plan = asyncio.run(fp.match("打开某物"))          # 通用 Turn 模式，rest="某物"
    assert plan is not None and plan.source in ("t0", "t1")


def test_a1_retry_must_not_override_semantic_refusals(settings):
    """CI 实发反例（本钉即那次失败）：`关掉音乐` 是 T0 **刻意的语义拒绝**——音乐泛词
    交音乐带处理，不是设备名。T1 兜底只许对"目标提取质量低"生效；若像初版那样无条件
    兜底，真模型（CI 有 onnxruntime）会给 TurnDeviceOff 高置信，于是把"故意不接"
    变成"乱接"，设备名还是整句残渣。"""
    fp = _fp(settings, {"音乐": ("TurnDeviceOff", 0.99), "关": ("TurnDeviceOff", 0.99),
                        "灯": ("TurnDeviceOn", 0.99)})
    for t in ("关掉音乐", "关音乐"):
        assert asyncio.run(fp.match(t)) is None, f"{t} 被 T1 兜底乱接了（语义拒绝被覆盖）"


# ── D3：窗帘方向词纠正 ────────────────────────────────────────────
@pytest.mark.parametrize("text,want", [
    ("拉上窗帘", "TurnDeviceOff"), ("合上窗帘", "TurnDeviceOff"),
    ("收起窗帘", "TurnDeviceOff"), ("关闭窗帘", "TurnDeviceOff"),
    ("拉开窗帘", "TurnDeviceOn"), ("打开窗帘", "TurnDeviceOn"),
])
def test_d3_cover_direction(settings, text, want):
    plan = asyncio.run(_fp(settings).match(text))
    assert plan is not None and plan.intent == want, f"{text!r} 方向判错（D3 复发）"


def test_d3_cover_intent_helper_is_safe():
    assert _cover_intent("拉上窗帘", "TurnDeviceOn") == "TurnDeviceOff"
    assert _cover_intent("打开窗帘", "TurnDeviceOn") == "TurnDeviceOn"
    assert _cover_intent("拉上窗帘", "TurnDeviceOff") == "TurnDeviceOff"   # 非开向不动
    assert _cover_intent("", "TurnDeviceOn") == "TurnDeviceOn"            # 脏输入不抛
    assert _cover_intent(None, "TurnDeviceOn") == "TurnDeviceOn"


# ── A2：显式全屋不被上下文替换 ────────────────────────────────────
def test_a2_wholehouse_args_recognised():
    assert _is_wholehouse_args(
        {"target": [{"devices": [{"name": "", "domains": ["light"]}]}]}) is True
    # 有具体设备名/区域/实体 → 不是全屋
    assert _is_wholehouse_args(
        {"target": [{"devices": [{"name": "灯", "domains": ["light"]}]}]}) is False
    assert _is_wholehouse_args(
        {"target": [{"area": "客厅", "devices": [{"name": "", "domains": ["light"]}]}]}) is False
    assert _is_wholehouse_args({"entity_id": "light.x"}) is False
    assert _is_wholehouse_args({}) is False
    assert _is_wholehouse_args({"target": "脏"}) is False
    assert _is_wholehouse_args({"target": [{"devices": [{"name": ""}]}]}) is False  # 无 domains


def test_a2_context_never_replaces_wholehouse(settings):
    """实测复现路径：'再打开所有灯' 的真实计划不得被上一轮目标覆盖。"""
    from core.nlu.fast_path import Plan
    fp = _fp(settings, {})                       # T1 不介入，走 T0 全屋分支
    plan = asyncio.run(fp.match("再打开所有灯"))
    assert plan is not None
    before = json.loads(json.dumps(plan.args))
    assert _is_wholehouse_args(before) is True, "全屋形态前提不成立"

    pl = Pipeline.__new__(Pipeline)
    pl.settings = settings
    pl._last_target = {"panel": {"kind": "target",
                                 "target": [{"area": "客厅", "devices": [{"name": "灯"}]}],
                                 "ts": 9e18}}      # 永不超时，确保"新鲜"
    out = pl._apply_context(plan, "再打开所有灯", "panel")
    assert out.args == before, "全屋目标被上下文替换（A2 复发）"


# ── A3/A4/D4/D10：settings ────────────────────────────────────────
def test_a4_non_dict_node_repaired_not_crash(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"security": None, "stt": "坏值"}), encoding="utf-8")
    s = Settings(p)                                    # 修前：AttributeError 崩启动
    assert isinstance(s.data["security"], dict)
    assert isinstance(s.data["stt"], dict)
    assert len(s.get("security.ws_token") or "") >= 20  # 密钥已重建


def test_a3_masked_writeback_keeps_token(tmp_path):
    s = Settings(tmp_path / "settings.json")
    before = s.get("security.ws_token")
    s.update({"security": s.masked()["security"]})      # UI 的 GET→POST 整块回写
    assert s.get("security.ws_token") == before, "脱敏回写写坏了 token（A3 复发）"


def test_a3_scrub_tolerates_non_dict_security(tmp_path):
    s = Settings(tmp_path / "settings.json")
    s.update({"security": None})                        # 不得抛
    assert isinstance(s.data["security"], dict)
    s.update({"security": {"ws_token": "****", "pairing_token": "abcd…wxyz"}})
    assert len(s.get("security.ws_token") or "") > 10


def test_d10_pairing_token_masked(tmp_path):
    s = Settings(tmp_path / "settings.json")
    assert "…" in s.masked()["security"]["pairing_token"]


def test_d4_options_never_persisted_and_zero_means_ui_wins(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.delenv("HUIJIAN_OPT_IDLE_UNLOAD_MIN", raising=False)
    s = Settings(p)
    s.update({"power": {"unload_when_idle_min": 30}})   # 用户在 Web UI 设省电档

    monkeypatch.setenv("HUIJIAN_OPT_IDLE_UNLOAD_MIN", "42")
    s42 = Settings(p)
    assert s42.get("power.unload_when_idle_min") == 42          # options 非 0 → 运行期生效
    assert json.loads(p.read_text(encoding="utf-8"))["power"][
        "unload_when_idle_min"] == 30, "options 被落盘污染（D4 复发）"

    monkeypatch.setenv("HUIJIAN_OPT_IDLE_UNLOAD_MIN", "0")      # run.sh 默认值
    assert Settings(p).get("power.unload_when_idle_min") == 30, "UI 省电档被 0 抹回（D4 复发）"


# ── A5/A6/前端：源码级钉 ──────────────────────────────────────────
def test_a5_boot_atomic_install():
    src = (ROOT / "boot.sh").read_text(encoding="utf-8")
    assert 'rm -rf "${DST}"\n      cp -a' not in src, "旧的先删后拷（非原子）写法回来了"
    assert 'TMP="${DST}.new.$$"' in src and "mv \"${TMP}\" \"${DST}\"" in src
    assert 'jq -e . "${TMP}/manifest.json"' in src, "换名前缺少可解析校验"
    # 版本戳必须在换名成功之后写
    assert src.index('echo "${want}" > "${DST}/.huijian_voice_stamp"') > src.index(
        'mv "${TMP}" "${DST}"')


def test_a6_exposure_failure_is_logged():
    src = (ROOT / "custom_components" / "huijian_ai" / "custom_llm_api.py").read_text(
        encoding="utf-8")
    assert "(KeyError, Exception)" not in src, "冗余元组回来了"
    assert "实体暴露判定失败" in src, "暴露判定失败又变成静默"


def test_frontend_escapes_pill_and_error_text():
    src = (ROOT / "www" / "index.html").read_text(encoding="utf-8")
    assert "const esc0 = " in src
    assert "esc0(txt)" in src and "esc0(label)" in src
    assert "esc0(String(st.ha_error)" in src and "esc0(String(o.detail)" in src
