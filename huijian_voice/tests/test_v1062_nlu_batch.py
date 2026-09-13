"""v1.0.62 NLU 优化批钉桩（P0-1/P0-2/P0-3/P1-5/P1-6/P1-7/P2-8/P2-9/P2-10）。

纪律照旧：行为钉（跑真函数）+ 病灶复现钉（改动前的错误必须不再发生）+
回退防线钉（源码结构，防整段被删）。集成侧文件无 HA 库不能 import，
按仓内 AST+exec 抽取范式取真函数执行（test_integration_config_flow 同法）。
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import time
import types
from pathlib import Path

import pytest

import core.nlu.telemetry as tele
from core.nlu.canonical import canonical
from core.nlu.query import QueryZone
from core.nlu.textcnn import TextCNN

ROOT = Path(__file__).resolve().parents[1]
NLU_DATA = ROOT / "nlu_data"
CLAW = ROOT / "custom_components" / "huijian_ai" / "custom_llm_api.py"


# ── P0-3 集成侧同构闸（AST 抽真函数 + 源码防线） ──────────────────
def _claw_src():
    return CLAW.read_text(encoding="utf8")


def _claw_fn(name):
    src = _claw_src()
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {}
    exec(compile(ast.get_source_segment(src, fn), f"<{name}>", "exec"), ns)  # noqa: S102
    return ns[name]


def test_gate_lock_forms():
    f = _claw_fn("_args_targets_lock")
    assert f({"target": [{"devices": [{"name": "大门", "domains": ["lock"]}]}]})
    assert f({"target": [{"devices": [{"name": "门锁"}]}]})       # 名字含锁（LLM 常不写 domains）
    assert f({"target": [{"devices": [{"name": "大门", "domains": ["Lock"]}]}]})  # 大小写免疫
    assert not f({"target": [{"devices": [{"name": "筒灯", "domains": ["light"]}]}]})
    assert not f({"target": [{"area": "客厅"}]})                   # 全屋/仅区域不误伤
    assert not f({})
    assert not f(None)
    assert not f({"target": ["坏元素", 3, None]})                  # 形状错误永不抛


def test_gate_wired_in_call_intent():
    src = _claw_src()
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HuijianControlAPI")
    meth = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_call_intent")
    body = ast.get_source_segment(src, meth)
    assert "_RISKY_LOCK_OFF_INTENTS" in body and "_args_targets_lock(" in body, \
        "P0-3：_call_intent 里的同构闸被删/旁路"
    assert body.index("_enrich_target_domains") < body.index("_args_targets_lock("), \
        "闸必须置于 enrich 之后（LLM 不写 domains 时靠真实状态回填才能拦）"
    # TurnDeviceOn×lock=上锁（安全）绝不进风险集
    assert '"TurnDeviceOn"' not in src.split("_RISKY_LOCK_OFF_INTENTS = ")[1].split("\n")[0]


def test_gate_tool_description_safety_layer():
    # P1-5 集成侧：schema 即治理——Off 工具描述必须带锁域铁律
    src = _claw_src()
    seg = src[src.index('"HassTurnDeviceOff"'):]
    seg = seg[:seg.index("self._handle_turn_off")]
    assert "lock" in seg and "锁" in seg, "HassTurnDeviceOff 描述缺锁域安全层（P1-5 回退）"


# ── P1-5 加载项 Agent：prompt 规则 + 工具描述 ────────────────────
def test_agent_prompt_lock_rule():
    from core.agent import SYSTEM_PROMPT, TOOLS
    assert "9)" in SYSTEM_PROMPT and "锁" in SYSTEM_PROMPT and "TurnDeviceOff" in SYSTEM_PROMPT
    off = next(t for t in TOOLS if t["function"]["name"] == "TurnDeviceOff")
    assert "锁" in off["function"]["description"]
    on = next(t for t in TOOLS if t["function"]["name"] == "TurnDeviceOn")
    assert "TurnDeviceOn" not in SYSTEM_PROMPT.split("9)")[1].split("打开/上锁")[0].replace(
        "对锁域设备**禁止**发 TurnDeviceOff/HassTurnOff/HassToggle", "") or True  # 上锁不禁，见下钉
    # 上锁语义保通：规则 9 只禁 Off/Toggle，明示 TurnDeviceOn 不受此限
    rule9 = "9)" + SYSTEM_PROMPT.split("9)")[1]
    assert "TurnDeviceOn" in rule9 and "不受此限" in rule9


# ── P0-1 漏斗 ────────────────────────────────────────────────────
def test_funnel_counts_and_window():
    f = tele.Funnel()
    f.record("t0", True, 0.10)
    f.record("t0", False, 0.20)
    f.record("llm", True, 1.50)
    snap = f.snapshot()
    assert snap["total"] == 3
    assert snap["by_source"]["t0"]["n"] == 2 and snap["by_source"]["t0"]["ok_pct"] == 50.0
    assert snap["by_source"]["t0"]["avg_ms"] == 150.0
    assert snap["by_source"]["t0"]["max_ms"] == 200.0
    assert snap["by_source"]["llm"]["win1h"] == 1


def test_funnel_window_expiry():
    f = tele.Funnel()
    f.record("t0", True, 0.1)
    # 注入一条 2h 前的滑窗样本到**队首**（record 清扫按时间序自头部弹，
    # 乱序注入破坏 deque 时序不变式——生产端 monotonic 永不发生）
    f._win.appendleft((time.monotonic() - 7200, "t0", True))
    f.record("t0", True, 0.1)          # 触发 cutoff 清扫
    snap = f.snapshot()
    assert snap["by_source"]["t0"]["n"] == 2       # 总计数只认 record 进来的
    assert snap["by_source"]["t0"]["win1h"] == 2   # 滑窗把 2h 前的陈旧样本裁掉了


# ── P0-2 回流 ────────────────────────────────────────────────────
class _S:
    def __init__(self, d): self._d = d
    def get(self, k, default=None): return self._d.get(k, default)


def test_mining_pool_selection(tmp_path):
    m = tele.Mining(_S({}), tmp_path / "mining.jsonl")
    m.maybe("开灯", "t0", True)              # 本地干净命中：不入池
    m.maybe("乱七八糟", "fallback", False)   # 固定兜底：入
    m.maybe("讲个故事", "llm", True)         # LLM 兜底句（NLU 未覆盖）：入
    m.maybe("执行失败句", "t0", False)       # 本地执行失败：入
    lines = [json.loads(x) for x in
             (tmp_path / "mining.jsonl").read_text(encoding="utf8").splitlines()]
    assert [r["source"] for r in lines] == ["fallback", "llm", "t0"]
    assert lines[2]["ok"] is False


def test_mining_switch_and_rotation(tmp_path):
    m = tele.Mining(_S({"nlu.mining_enabled": False}), tmp_path / "m.jsonl")
    m.maybe("句", "fallback", False)
    assert not (tmp_path / "m.jsonl").exists()          # 关=真停
    m2 = tele.Mining(_S({"nlu.mining_max_lines": 60}), tmp_path / "m.jsonl")
    for i in range(100):
        m2.maybe(f"句{i}", "fallback", False)
    n = len((tmp_path / "m.jsonl").read_text(encoding="utf8").splitlines())
    assert n <= 90, n                                     # 触发轮转（cap//2 保新）
    tail = json.loads((tmp_path / "m.jsonl").read_text(encoding="utf8").splitlines()[-1])
    assert tail["text"] == "句99"                          # 保的是最新
    assert m2.status()["lines"] == n


def test_mining_never_raises(tmp_path):
    bad = tmp_path / "occupied"
    bad.write_text("x")                                   # 路径被目录文件占用
    m = tele.Mining(_S({}), bad / "sub" / "m.jsonl")      # 父目录造不出来 → OSError
    m.maybe("句", "fallback", False)                       # 必须静默，不炸主链


# ── pipeline 观测钩子（handle 收口一次记全） ─────────────────────
def _mk_pipe(tmp_path):
    from core.pipeline import Pipeline, Reply
    p = Pipeline.__new__(Pipeline)
    p.settings = _S({"dialog.dedup_window_s": 0.0, "nlu.enabled": True})
    p.telemetry = tele.Telemetry(p.settings, tmp_path)
    p._last = types.SimpleNamespace(items=lambda: [])
    async def _fake_cascade(text, origin="", on_sentence=None):
        return Reply("好的", "t0", True, ["pin"])
    async def _fake_query(text):
        return None
    p._cascade = _fake_cascade
    p.query = types.SimpleNamespace(answer=_fake_query)
    p._dedup_gate = lambda text: _ret(None)
    p._dedup_settle = lambda text, reply: None
    p._sync_vocab = lambda: None
    p._spawn = lambda coro: coro.close()
    p.ha = types.SimpleNamespace(fire_event=lambda *a, **k: _noop())
    p._observe = None
    return p, Reply


async def _ret(v):
    return v


async def _noop():
    return None


def test_pipeline_handle_records_funnel(tmp_path):
    p, Reply = _mk_pipe(tmp_path)
    asyncio.run(p.handle("开灯"))
    asyncio.run(p.handle("乱七八糟呀"))
    snap = p.telemetry.snapshot()
    assert snap["funnel"]["total"] == 2
    assert snap["funnel"]["by_source"]["t0"]["n"] == 2


# ── P1-6 量纲闸 ──────────────────────────────────────────────────
def test_unit_gate():
    assert QueryZone._unit_ok("湿度", {"unit_of_measurement": "%"})
    assert QueryZone._unit_ok("设定温度", {"unit_of_measurement": "°C"})
    assert QueryZone._unit_ok("湿度", {})                          # 无单位=信任键表
    assert not QueryZone._unit_ok("湿度", {"unit_of_measurement": "ppm"})   # AQI 型错维度
    assert not QueryZone._unit_ok("温度", {"unit_of_measurement": "%"})     # 反向也不认
    assert QueryZone._unit_ok("亮度", {"unit_of_measurement": "lx"})        # 白名单外词不拦


class _HaEnts:
    def __init__(self, ents): self._e = ents
    async def find_entities(self, area="", domains=()): return self._e


def test_aqi_entity_never_called_humidity():
    """病灶复现钉（P3-b 同族根治）：unit=AQI 的实体问湿度必须 None，不得播
    「湿度是 35」；单位正常的湿度属性才放行。"""
    z = QueryZone(_HaEnts([{"attributes": {"friendly_name": "净化器",
                                           "humidity": 55, "unit_of_measurement": "%"}}]), None)
    assert asyncio.run(z._attr_answer("", "净化器", "湿度")) == "净化器湿度是 55。"
    z2 = QueryZone(_HaEnts([{"attributes": {"friendly_name": "净化器",
                                            "humidity": 35, "unit_of_measurement": "ppm"}}]), None)
    assert asyncio.run(z2._attr_answer("", "净化器", "湿度")) is None


def test_color_temp_mireds_and_gear():
    z = QueryZone(_HaEnts([{"attributes": {"friendly_name": "氛围灯", "color_temp": 370}}]), None)
    assert "约 2700K" in asyncio.run(z._attr_answer("", "灯", "色温"))
    z2 = QueryZone(_HaEnts([{"attributes": {"friendly_name": "风扇", "percentage": 3}}]), None)
    assert "3 档" in asyncio.run(z2._attr_answer("", "风扇", "档位"))
    z3 = QueryZone(_HaEnts([{"attributes": {"friendly_name": "风扇", "percentage": 80,
                                            "unit_of_measurement": "%"}}]), None)
    assert "80%" in asyncio.run(z3._attr_answer("", "风扇", "档位"))


# ── P1-7 canonical ───────────────────────────────────────────────
def test_canonical_idempotent_and_safe():
    once = canonical("帮我开一下空条", _S({"nlu.corrections_extra": {}}))
    assert "空调" in once and not once.startswith("帮我")
    assert canonical(once) == once, "canonical 必须幂等（fp 内部纵深调用二次无害）"
    assert canonical("   ") == ""
    assert canonical("正常句", _S({"nlu.corrections_extra": None})) == "正常句"
    class Bad:
        def get(self, k, d=None): raise RuntimeError("settings 炸")
    assert canonical("开空调", Bad()) == "开空调"                  # 永不抛


def test_cascade_entry_normalizes_for_query():
    """跨档统一起点实锤：ASR 变体「开床器电量多少」旧状 fp 认得 query 不认得；
    canonical 接线后查询族必须接得住。"""
    seen = {}
    async def _answer(text):          # 级联调用：query.answer(text)
        seen["t"] = text
        return None
    from core.pipeline import Pipeline, Reply
    p = Pipeline.__new__(Pipeline)
    p.settings = _S({"nlu.enabled": True, "dialog.fallback_text": "不太理解"})
    p._confirm = {}
    p._last = types.SimpleNamespace(items=lambda: [])
    p.query = types.SimpleNamespace(answer=_answer)
    p._origin_ts = {}
    async def _miss(*a, **k):                    # 各钩子签名 (text[, origin])
        return None
    p._voice_creation = _miss
    p._try_compound = _miss
    p._confirm_answer = _miss
    p.agent = None
    p.telemetry = None
    from core.nlu.fast_path import FastPath
    p.fast_path = FastPath.__new__(FastPath)
    p.fast_path.settings = p.settings
    p.fast_path.scenes = None
    p.fast_path.textcnn = None
    p.ha = types.SimpleNamespace(fire_event=lambda *a, **k: _noop())
    async def _pair(*a, **k):
        return None, None                        # fp∥klar 双空 → 级联下探
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Pipeline, "_match_pair", _pair)
        mp.setattr(FastPath, "match", lambda self, t: (None, []))
        asyncio.run(p._cascade("开床器电池电量剩余多少"))
    assert "开窗器" in seen["t"], f"查询族收到的是未归一文本：{seen['t']!r}"


# ── P2-8/9 margin 质量闸 ─────────────────────────────────────────
def test_margin_gate_real_asset():
    t = TextCNN(NLU_DATA)
    hit = t.predict("打开灯")
    assert hit and hit[0] == "TurnDeviceOn"                  # 正常命中不误伤
    assert t.predict("现在几点了") is None                    # margin 0.06 边界让位（P2-9）
    t_off = TextCNN(NLU_DATA, min_margin=0.0)                # 开关真生效
    assert t_off.predict("现在几点了") is not None
    t2 = TextCNN(NLU_DATA); t2._ensure()
    t2.thresholds["__min_margin__"] = "0.9"                  # 现场调参键
    assert t2.predict("打开窗帘") is None                     # margin 0.457<0.9 让位


def test_margin_garbage_inputs_safe():
    t = TextCNN(NLU_DATA, min_margin="坏值")
    assert t.min_margin == 0.15
    t._ensure()
    assert t.predict("打开灯") is not None                     # 坏配置回落后推理照常


# ── P2-10 历史窗独立治理 ─────────────────────────────────────────
def test_history_ttl_independent_window():
    from core.pipeline import Pipeline
    p = Pipeline.__new__(Pipeline)
    from collections import deque
    now = time.time()
    p._turns = {"sat": deque([(now - 200, "上一句", "好的"),
                              (now - 700, "很久前", "嗯")])}
    p.settings = _S({"dialog.history_ttl_s": 360.0})          # 独立键：只裁历史窗
    hist = p._history_snapshot("sat")
    assert len([m for m in hist if m["content"] == "上一句"]) == 1
    assert all(m["content"] != "很久前" for m in hist)
    p.settings = _S({"dialog.history_ttl_s": 99999.0, "llm.history_rounds": 10})
    assert len(p._history_snapshot("sat")) == 4               # 放宽=全保（旧 *4 魔法数行为兼容）
