# -*- coding: utf-8 -*-
"""v1.1.36 ⑦：TTS 句缓存的**写侧**同竞态未护。

今天读侧补了 `except KeyError`（core/tts.py:1402-1405、:1444-1447），理由写得很清楚：
换绑/换代在 worker 线程 `clear()` 与本读竞态，"丢一次缓存命中只是多合成一句，
绝不能 KeyError 穿出打断整轮"。但**同一条竞态在写侧还有一个更狠的落点**：

    _cache_put(:1494-1507)
        self._cache[key] = (packets, size)
        self._cache_bytes += size
        while len(self._cache) > _CACHE_MAX_ITEMS or self._cache_bytes > _CACHE_MAX_BYTES:
            _k, (_pkt, b) = self._cache.popitem(last=False)   # ← clear() 插队在这

`len()` 判过之后、`popitem()` 取之前被清空 ⇒ OrderedDict.popitem 抛 KeyError ⇒
它在**发声协程**的写缓存路径上 ⇒ 穿出去就是整轮播报中断（正是读侧要避免的形态），
另附 `_cache_bytes` 记账被留在虚高值（后续每轮都会多挤一次缓存）。

触发输入（现场就有）：合成进行中在 web 面板切 TTS 引擎/换嗓。
测试用**确定性的插入点**复现该交错，不起线程（避免 flaky）。
"""
from collections import OrderedDict

import pytest

from core import tts as tts_mod


class _ClearOnEvict(OrderedDict):
    """第一次 popitem 前模拟 worker 线程把表清空（就是那道交错）。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.cleared = False

    def popitem(self, last=True):
        if not self.cleared:
            self.cleared = True
            super().clear()          # 并发 clear() 插在 len() 判与 popitem() 之间
        return super().popitem(last=last)


def _engine():
    """只装缓存需要的最小件（`__new__` 避开真引擎加载）。"""
    eng = tts_mod.TtsEngine.__new__(tts_mod.TtsEngine)
    eng._cache = OrderedDict()
    eng._cache_bytes = 0
    eng.settings = {"tts.cache_enabled": True}
    return eng


def test_cache_put_survives_concurrent_clear():
    eng = _engine()
    eng._cache = _ClearOnEvict()
    big = [b"\x00" * 4096] * 8
    for i in range(tts_mod._CACHE_MAX_ITEMS + 3):
        eng._cache_put(("p", f"s{i}", 0, 1.0), [b"x" * 2048])   # 不得抛
    assert eng._cache_bytes >= 0, f"记账为负（并发后没跟着归零）：{eng._cache_bytes}"


def test_cache_put_accounting_still_correct_without_race():
    """**反向不变量**：没有交错时驱逐必须照旧记账——
    把 `except KeyError: self._cache_bytes = 0` 写成无条件归零也过上一条，
    这条钉住"只在真被插队时才重算"。"""
    eng = _engine()
    packets = [b"y" * 100]
    for i in range(tts_mod._CACHE_MAX_ITEMS + 5):
        eng._cache_put(("p", f"s{i}", 0, 1.0), packets)
    assert len(eng._cache) <= tts_mod._CACHE_MAX_ITEMS
    expect = sum(size for _pkts, size in eng._cache.values())
    assert eng._cache_bytes == expect, (eng._cache_bytes, expect)


# ── ④ 出帧单元上限的**真**不变量（旧注释自相矛盾，代码为准）───────
@pytest.mark.parametrize("sent", [
    "一二三四五六七八九十一二三四五六七八九十一二",            # 22 字无标点
    "一二三四五六七八九十一二三四五六七八九十" + "一二三四",      # 24 字
    "一二三四五六七八九十一二三四五六七八九十" + "一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十",
    "灯开了",
    "一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十",
])
def test_chunk_units_bounded_at_22_and_no_stub_tail(sent):
    """并块把上限抬到 `_CHUNK_CHARS+2`，所以不变量是"**≤22 且不出现 ≤2 字的孤段**"。

    旧注释写"保证任何单元 ≤_CHUNK_CHARS(20)"与代码冲突（tts.py:129 vs :138），
    v1.1.36 已按代码改正注释；这条钉把改正后的真不变量钉住：
    上限若被继续放宽（比如并成 40 字）或残段又开始独占一段，本钉转红。
    """
    out = tts_mod.split_sentences(sent)
    assert out, sent
    assert all(len(u) <= tts_mod._CHUNK_CHARS + 2 for u in out), out
    assert all(len(u) > 2 for u in out), f"又出现 1~2 字孤段（多带一段空洞）：{out}"


class _ClearOnSetItem(OrderedDict):
    """更狠的交错：`clear()` 插在 `pop(旧值) → 减法 → 赋值` 中间。

    对抗复核抓到、我复现：三段本身非原子，减法落在已被归零的计数上 ⇒
    `_cache_bytes` 变**负**（虚高只是多挤一次，负值则让"超上限"判据方向失真）。
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.armed = False

    def __setitem__(self, k, v):
        if self.armed:
            self.armed = False
            saved = dict(self)
            super().clear()
            super().__setitem__(k, v)
            return
        super().__setitem__(k, v)


def test_cache_bytes_never_goes_negative_under_interleaved_clear():
    """记账的**真源是这张表**：被插队后必须从表重算，不许留负值/虚高。"""
    eng = _engine()
    eng._cache = _ClearOnSetItem()
    eng._cache_put(("p", "warm", 0, 1.0), [b"z" * 5000])   # 先放一条，制造 old-pop
    eng._cache.armed = True
    eng._cache_put(("p", "s2", 0, 1.0), [b"y" * 100])
    assert eng._cache_bytes >= 0, f"记账被打成负值：{eng._cache_bytes}"
    assert eng._cache_bytes == sum(sz for _p, sz in eng._cache.values()), \
        (eng._cache_bytes, dict(eng._cache))
