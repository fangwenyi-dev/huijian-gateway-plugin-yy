# -*- coding: utf-8 -*-
"""判定旗标契约：控制流判据不得 sniff trace 诊断文案。

出处（2026-09-22 优化盘点 P2）：三处执行语义曾直接读 trace 字符串——
  fast_path T1 接管闸 `trace[-1].startswith("miss:提取质量低")`
  pipeline._apply_context `any("代词目标" in t or "回指→" in t ...)`
  pipeline._is_anaphoric 同上 + "链内回指"
trace 是给人看的诊断串。改一句日志措辞（或顺手给前缀加个字）就会**静默**改掉
"要不要给 T1 一次接管机会""这句要不要继承上一轮目标"——而这两条正落在
误执行/漏执行的分岔口上（v1.1.1 近音误执行、v1.1.2 状态疑问句误执行同族）。

收口口径：
  · pipeline.py 是纯消费方 → 一个 trace 文案字面量都不许有，判据读 Plan.flags；
  · fast_path.py 是文案唯一产地 → 允许 append，但不许在产地之外再"解读"它，
    解读只准发生在旗标派生表 _FLAG_TRACE_TOKENS；
  · miss 原因名与 tag 名生产/消费共用常量，常量只准定义一次。
"""
import ast
import pathlib

from core.nlu.fast_path import (FLAG_ANAPHORA_STRIPPED, FLAG_CHAIN_ANAPHORA,
                                FLAG_PRONOUN_TARGET, MISS_LOW_EXTRACT_QUALITY,
                                Plan, TRACE_TAG_CHAIN, TRACE_TAG_CONTEXT,
                                _FLAG_TRACE_TOKENS)

CORE = pathlib.Path(__file__).resolve().parents[1] / "core"
FAST_PATH = CORE / "nlu" / "fast_path.py"
PIPELINE = CORE / "pipeline.py"

# 曾驱动执行语义的文案 token。不含裸 "上下文"：那是常用词（pipeline 里的
# "删第N条:无清单上下文" 是诊断标签，不是判据），tag 形态由 TRACE_TAG_CONTEXT
# 常量与 f"{tag}:" 前缀承载，单列在下面等值钉里。
TRACE_TOKENS = ("代词目标", "回指→", "链内回指", "提取质量低")

# 合法持有这些 token 的模块级常量名（文案单点）
TOKEN_CONSTANTS = {"FLAG_PRONOUN_TARGET", "FLAG_ANAPHORA_STRIPPED",
                   "FLAG_CHAIN_ANAPHORA", "MISS_LOW_EXTRACT_QUALITY",
                   "TRACE_TAG_CHAIN", "TRACE_TAG_CONTEXT"}


def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _docstring_lines(tree):
    """文档串占用的行号——散文里提"链内回指"是叙述，不是控制流依赖。"""
    out, stack = set(), [tree]
    while stack:
        node = stack.pop()
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            head = body[0]
            doc = getattr(head, "value", None)
            if isinstance(doc, ast.Constant) and isinstance(doc.value, str):
                out.update(range(head.lineno, (head.end_lineno or head.lineno) + 1))
        stack.extend(ast.iter_child_nodes(node))
    return out


def _has_token(value):
    return isinstance(value, str) and any(t in value for t in TRACE_TOKENS)


def test_pipeline_carries_zero_trace_wording():
    """反向钉①：pipeline.py 任何非文档串位置出现 trace 字面量即红。

    拦的是"把文案抄第二份"的形态（`tag = "链内回指"`、`"代词目标" in t`），
    按 AST 字面量判而不是行内子串——子串判会误伤注释与文档串，也会放过
    赋值形态。"""
    tree = _parse(PIPELINE)
    docs = _docstring_lines(tree)
    bad = [n.lineno for n in ast.walk(tree)
           if isinstance(n, ast.Constant) and _has_token(n.value)
           and n.lineno not in docs]
    assert not bad, (
        "pipeline.py 出现 trace 文案字面量（判据应读 Plan.flags，tag/原因名应复用 "
        "TRACE_TAG_*/MISS_LOW_EXTRACT_QUALITY 常量）：行号 "
        f"{bad}")


def test_fast_path_does_not_interpret_its_own_wording():
    """反向钉②：产地可以写 trace，但解读只准在旗标派生表那一处。

    命中形态：比较 (`token in x` / `x in token`)、startswith/endswith/find/index。
    f-string 的字面片段不在其列（`f"miss:{CONST}"` 的字面部分是 "miss:"，不含
    token），所以共用常量的写法不会被自己拦掉。"""
    tree = _parse(FAST_PATH)
    docs = _docstring_lines(tree)
    bad = []
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", 0)
        if not lineno or lineno in docs:
            continue
        if isinstance(node, ast.Compare):
            for op in (node.left, *node.comparators):
                if isinstance(op, ast.Constant) and _has_token(op.value):
                    bad.append(lineno)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"startswith", "endswith", "find", "index", "count"}:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and _has_token(arg.value):
                        bad.append(lineno)
    assert not bad, (
        "fast_path 又就地解读 trace 文案，请改判 Plan.flags（派生只在 "
        f"_FLAG_TRACE_TOKENS）：行号 {sorted(set(bad))}")


def test_flag_vocabulary_and_derivation_table_agree():
    """元钉：旗标常量与派生表一一对应——加了旗标忘了配 token，旗标永远不亮
    （比没有旗标更坏：读代码的人以为它有）。"""
    assert set(_FLAG_TRACE_TOKENS) == {FLAG_PRONOUN_TARGET,
                                       FLAG_ANAPHORA_STRIPPED, FLAG_CHAIN_ANAPHORA}
    for flag, tokens in _FLAG_TRACE_TOKENS.items():
        assert tokens and all(tokens), f"{flag} 派生 token 为空=永不可达"


def test_trace_tokens_still_derive_flags():
    """正向钉：生产侧沿用的 trace 形态要能派生出旗标（手搓 trace 的测试替身
    与 boot 期未 mark 的路径都靠这条兜住）。负例：无关 trace 不得凭空亮旗标。"""
    assert FLAG_PRONOUN_TARGET in Plan(
        "TurnDeviceOn", {}, trace=["代词目标:它→待上下文注入"]).flags
    assert FLAG_ANAPHORA_STRIPPED in Plan("X", {}, trace=["回指→亮一点"]).flags
    assert FLAG_CHAIN_ANAPHORA in Plan("X", {}, trace=["链内回指:继承目标 a"]).flags
    assert Plan("X", {}, trace=["无匹配动作"]).flags == set()


def test_mark_is_primary_and_trace_independent():
    """旗标脱离文案单独成立：mark 后即生效，不依赖任何字面量。"""
    p = Plan("X", {}).mark(FLAG_CHAIN_ANAPHORA)
    assert p.trace == [] and FLAG_CHAIN_ANAPHORA in p.flags
    assert FLAG_PRONOUN_TARGET not in p.flags


def test_shared_constants_defined_once_and_used_by_consumer():
    """miss 原因名/tag 名生产与消费共用常量；常量就地重复赋值=判据被偷改。"""
    fp = FAST_PATH.read_text(encoding="utf-8")
    pl = PIPELINE.read_text(encoding="utf-8")
    assert 'f"{MISS_LOW_EXTRACT_QUALITY}(score=' in fp, "生产侧回到裸字面量"
    assert 'f"miss:{MISS_LOW_EXTRACT_QUALITY}"' in fp, "消费侧回到裸字面量"
    assert "TRACE_TAG_CHAIN" in pl and "TRACE_TAG_CONTEXT" in pl, "tag 常量没被 pipeline 用"
    tree = _parse(FAST_PATH)
    rebound = [n.id for node in tree.body if isinstance(node, ast.Assign)
               for n in node.targets
               if getattr(n, "id", "") in TOKEN_CONSTANTS]
    assert sorted(rebound) == sorted(set(rebound)), f"旗标/tag 常量被重复赋值：{rebound}"
    assert (MISS_LOW_EXTRACT_QUALITY, TRACE_TAG_CHAIN, TRACE_TAG_CONTEXT) == \
        ("提取质量低", "链内回指", "上下文")
