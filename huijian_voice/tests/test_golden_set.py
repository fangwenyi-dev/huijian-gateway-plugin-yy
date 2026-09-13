"""P1-4 Golden 语料评估表（v1.0.62）。

nlu_data/golden_utterances.jsonl = 82 句人工终审的**本地理解契约**：
每句钉 (intent, source) 精确值——fp（字面表/剥壳/前缀/同音 + T1 真资产 +
场景判等）与查询族（fake ha 固定实体集）两棵确定性引擎的当前行为快照。

这不是"通过率指标"而是**漂移检测器**：任何一次字面表增删、纠错表改动、
阈值/margin 调参、TextCNN 重训，只要让某句理解变了档，本表立即红——
人工确认"变好"后才重跑 tests/golden_gen.py 更新表（红是特性不是缺陷）。
miss 行同样钉住：负样本不许被本地档误接（假成功比拒答危险），
覆盖观察行（色温调低等 klar 承接句）钉住"fp 环境不冒接"的边界。

契约面不含 级联外壳/确认环/LLM（那三层有专项行为钉千余条）。
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

from core.nlu.fast_path import FastPath
from core.nlu.query import QueryZone
from core.nlu.textcnn import TextCNN

from golden_gen import FakeScenes, FakeHa, S      # tests/ 目录随 pytest 入 path

GOLDEN = Path(os.environ.get("HUIJIAN_NLU_DATA", "nlu_data")) / "golden_utterances.jsonl"


def _rows():
    return [json.loads(l) for l in GOLDEN.read_text(encoding="utf8").splitlines() if l.strip()]


ROWS = _rows()
_IDS = [f"{i}:{r['text']}" for i, r in enumerate(ROWS)]


@pytest.fixture(scope="module")
def engines():
    tc = TextCNN(Path(os.environ["HUIJIAN_NLU_DATA"]))
    tc._ensure()
    fp = FastPath(FakeScenes(), tc, S())
    qz = QueryZone(FakeHa(), S())
    return fp, qz


@pytest.mark.parametrize("idx", range(len(ROWS)), ids=_IDS)
def test_golden_row(engines, idx):
    fp, qz = engines
    row = ROWS[idx]
    plan = asyncio.run(fp.match(row["text"]))
    if plan is not None:
        got_intent, got_source = plan.intent, plan.source
    else:
        ans = asyncio.run(qz.answer(row["text"]))
        got_intent, got_source = ("_query" if ans else None), ("query" if ans else "miss")
    assert got_intent == row["intent"], \
        f"{row['text']!r} 意图漂移：契约 {row['intent']} ← 现测 {got_intent}"
    assert got_source == row["source"], \
        f"{row['text']!r} 档位漂移：契约 {row['source']} ← 现测 {got_source}"


def test_golden_table_shape():
    rows = _rows()
    assert len(rows) >= 80
    srcs = {r["source"] for r in rows}
    assert {"t0", "t1", "scene", "query", "miss"} <= srcs, "五档齐备（负样本必须占位）"
    assert not any("error" in r for r in rows), "生成期异常行不许入仓"
