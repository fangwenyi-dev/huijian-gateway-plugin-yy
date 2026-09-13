"""v1.0.64 批2 风险闸钉（H2+M6，报告 2026-09-23）。

H2：v1.0.62 P0-3 免确认解锁闸只认字面 domains:["lock"]，而执行面
    intent_helper.DOMAIN_ALIASES{door:[lock,cover,button]}+_expand_domains
    会把 door 形扩进锁域 → `HassTurnDeviceOff{name:大门,domains:["door"]}`
    旁路=免确认解锁。
M6：intent_turn D7 映射 alarm×TurnOff→alarm_disarm，三层闸（pipeline
    _plan_has_risky_step / agent._tool / custom_llm_api._call_intent）
    只认锁——一句话直撤家庭安防。
修法双端：判据升级为「域别名闭包 + 锁/安防中文词表 + alarm 域/entity_id」，
A 通道=core/nlu/targets.py args_target_lock（三消费点经 T.args_target_lock
自动接入，pipeline.py 零改动），B 通道=custom_llm_api._args_targets_lock。
双端受控重复，本文件跑双端真函数同矩阵 + 三表（A/B/intent_helper 真源）
door 形一致钉。
"""
import ast
from pathlib import Path

from core.nlu.targets import args_target_lock as a_lock

ROOT = Path(__file__).resolve().parents[1]
CC = ROOT / "custom_components" / "huijian_ai"


def _claw_fn(name):
    src = (CC / "custom_llm_api.py").read_text(encoding="utf8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {}
    for node in tree.body:
        if node is fn:
            continue
        seg = ast.get_source_segment(src, node)
        if seg is None:
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            try:
                exec(compile(seg, "<const>", "exec"), ns)  # noqa: S102
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(node, ast.FunctionDef) and node.name == "_risky_domain_closure":
            exec(compile(seg, f"<{node.name}>", "exec"), ns)  # noqa: S102
    exec(compile(ast.get_source_segment(src, fn), f"<{name}>", "exec"), ns)  # noqa: S102
    return ns[name]


b_lock = _claw_fn("_args_targets_lock")

# ── 双端同矩阵（H2 door 形 + M6 alarm 形 + 不扩伤面）──────────────
MATRIX_HIT = [
    {"target": [{"devices": [{"name": "大门", "domains": ["lock"]}]}]},
    {"target": [{"devices": [{"name": "门锁"}]}]},
    {"target": [{"devices": [{"name": "大门", "domains": ["door"]}]}]},     # H2 主案
    {"target": [{"devices": [{"name": "", "domains": ["doors"]}]}]},        # 全屋形
    {"target": [{"devices": [{"name": "卷帘门", "domains": ["cover"]}]}]},  # 中文词表
    {"target": [{"devices": [{"name": "家庭安防系统", "domains": ["alarm_control_panel"]}]}]},  # M6
    {"target": [{"devices": [{"name": "", "domains": ["alarm_control_panel"]}]}]},              # M6 全屋形
    {"target": [{"devices": [{"name": "撤防面板"}]}]},
    {"name": "大门"},
    {"entity_id": "lock.front_door"},
    {"entity_id": ["alarm_control_panel.home", "light.x"]},
]
MATRIX_MISS = [
    {"target": [{"devices": [{"name": "筒灯", "domains": ["light"]}]}]},
    {"target": [{"devices": [{"name": "空调", "domains": ["climate"]}]}]},
    {"target": [{"devices": [{"name": "插座", "domains": ["switch", "plug"]}]}]},
    {"target": [{"area": "客厅"}]},
    {"entity_id": "media_player.客厅音箱"},   # 音乐批常用面，绝不扩伤
    {"entity_id": "cover.窗帘"},
    {},
    None,
    {"target": ["坏元素", 3, None]},
]


def test_h2m6_dual_channel_matrix():
    for args in MATRIX_HIT:
        assert a_lock(args), f"A通道漏判: {args}"
        assert b_lock(args), f"B通道漏判: {args}"
    for args in MATRIX_MISS:
        assert not a_lock(args), f"A通道误伤: {args}"
        assert not b_lock(args), f"B通道误伤: {args}"


def test_h2_alias_table_sync():
    """受控重复三表一致钉：执行面 intent_helper.DOMAIN_ALIASES 是唯一真源，
    A/B 两闸表若与 door 形脱节=别名旁路复发（本测试读源码字面不 import HA）。"""
    src = (CC / "intent_helper.py").read_text(encoding="utf8")
    tree = ast.parse(src)
    real = None
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "DOMAIN_ALIASES":
            real = ast.literal_eval(node.value)
        elif isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "DOMAIN_ALIASES" for t in node.targets):
            real = ast.literal_eval(node.value)
    assert isinstance(real, dict) and "door" in real
    door = real["door"] if isinstance(real["door"], list) else [real["door"]]
    assert "lock" in door, "intent_helper door→lock 别名被删——执行面语义变了，双闸表须同步改"
    import core.nlu.targets as A
    b_aliases = _claw_alias_table()
    for mod_table in (A._RISKY_DOMAIN_ALIASES, b_aliases):
        assert set(mod_table["door"]) >= set(door) - {"door"}, \
            "闸表与执行面真源脱节（H2 旁路复发前兆）"


def _claw_alias_table():
    src = (CC / "custom_llm_api.py").read_text(encoding="utf8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            names = [getattr(t, "id", "") for t in getattr(node, "targets", [])] \
                if isinstance(node, ast.Assign) else [getattr(node.target, "id", "")]
            if "_RISKY_DOMAIN_ALIASES" in names:
                return ast.literal_eval(node.value)
    raise AssertionError("custom_llm_api 缺 _RISKY_DOMAIN_ALIASES")


def test_m6_pipeline_agent_wiring_untouched():
    """三层闸消费点完整性：pipeline/agent 均经 T.args_target_lock 接入，
    本批零改动它们也自动罩 alarm（防未来有人复制粘贴出第四份判据）。"""
    pipe = (ROOT / "core" / "pipeline.py").read_text(encoding="utf8")
    agent = (ROOT / "core" / "agent.py").read_text(encoding="utf8")
    assert "T.args_target_lock" in pipe and pipe.count("args_target_lock") >= 3, \
        "pipeline 风险闸消费点缺失/被绕过"
    assert "args_target_lock" in agent, "agent._tool 闸消费点缺失"
    # 判据实现唯一在 targets.py（受控重复仅集成侧一处）
    ts = (ROOT / "core" / "nlu" / "targets.py").read_text(encoding="utf8")
    assert "def _risky_domain_closure" in ts
