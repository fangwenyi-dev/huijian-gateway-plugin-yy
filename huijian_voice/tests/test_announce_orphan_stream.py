# -*- coding: utf-8 -*-
"""播报孤儿推流根修的结构性守卫（v1.0.100，真机台架实锤立案；AST 核验，非字符串匹配）。

背景（台架实锤，两条独立样本，设备毫秒时间戳）：
  _v59guard.log 尾标2：Announce 119815 → barge-in abort 126855 → `Announce
    finished (130048 bytes)` → Discarding 126885(streak=1)…133255(streak=201)
    = 201 帧 / 6.370 s = **1.00× 实时**，丢弃 205,824 B = **6.4 s 音频**（该文本全长
    366,720 B 的 56%）。
  _m2158_barge2.log 尾标6：streak 1@731865 → 281@739605 = 280 帧 / 7.740 s
    = **1.16× 实时**，丢弃 286,720 B = 8.9 s 音频。
  短串样本（同日志尾标1）21 帧即止 ⇒ **孤儿流寿命＝到下一条下行流被创建为止**；
  barge-in 重启那轮若没产生 TTS（用户静默/空轮），就永远没人吊销。

根因（**不是**"网络在途残留"——被量化否证：背压水位仅 0.384 s≈12 帧，而实测持续
6~9 s 且速率恰等于推流循环的 28.8 ms/帧）：**播报腿漏登记句柄**。会话腿自 v1.0.49
起就把 `_tts_streaming_task` 交给 `_dl_takeover` / `_abort_pipeline` / 新开轮三处
吊销；而 v1.0.93 复用同一条 `_stream_tts_audio` 的**播报腿从未登记**，三处 cancel
全都够不着。设备 barge-in 只回 AnnounceFinished（v2.1.50 裁决：announce 非设备发起
会话，不发 start=0），集成侧 `handle_announcement_finished` 又只做
`tts_response_finished()` 不收流 ⇒ 孤儿流按自带限速把剩余音频推到耗尽。
对用户无可闻损害（丢的全是作废音频），代价是 HA 白烧 6~9 s TTS 算力 + 与新一轮
uplink 抢同一条 API socket + 设备逐帧解码与 UART 告警。

实现注：播报推流的真身在 `_do_announce`——`async_announce`(:866) 与
`async_start_conversation`(:873) 两个入口都汇到它，故登记与吊销只需在这一处做对。

本文件全部判据走 **AST**（本仓铁规则：签名/结构类缺陷只能靠 AST 或源码签名核验，
"源码字符串钉桩 + 全绿"拦不住）；同时兼容 pytest 收集与 python 直跑。
"""
import ast
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PATH = os.path.join(HERE, "..", "custom_components", "huijian_ai",
                        "assist_satellite.py")

WANTED = ("_do_announce", "_dl_takeover", "_revoke_announce_stream",
          "_clear_announce_stream_task", "async_announce",
          "async_start_conversation")


def _load():
    if not os.path.isfile(SRC_PATH):
        raise AssertionError(f"真身源缺失：{SRC_PATH}（守卫拒绝自证）")
    src = open(SRC_PATH, encoding="utf-8").read()
    tree = ast.parse(src)
    funcs = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in WANTED:
                funcs[node.name] = (node, src)
    return src, funcs


def _segment(node, src):
    return ast.get_source_segment(src, node) or ""


def _calls_in(node):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


def _call_names(node):
    out = []
    for c in _calls_in(node):
        f = c.func
        if isinstance(f, ast.Attribute):
            out.append(f.attr)
        elif isinstance(f, ast.Name):
            out.append(f.id)
    return out


def _has_await(node):
    return any(isinstance(n, ast.Await) for n in ast.walk(node))


def _assign_targets(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                out.add(ast.dump(t))
    return out


def _attrs_assigned(node):
    """返回被赋值过的属性名，如 self._announce_stream_task -> '_announce_stream_task'"""
    res = set()
    for n in ast.walk(node):
        if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in targets:
                if isinstance(t, ast.Attribute):
                    res.add(t.attr)
    return res


def _funcs():
    src, funcs = _load()
    got = {}
    for name, (node, _s) in funcs.items():
        got[name] = (node, _segment(node, src))
    return src, got


# ── 判据 ────────────────────────────────────────────────────────────────
def test_do_announce_registers_and_revokes_handle():
    """根修本体：_do_announce 登记播报句柄 + done-callback，且在设备收口后吊销。"""
    _src, f = _funcs()
    for need in ("_do_announce", "_dl_takeover", "_revoke_announce_stream",
                 "_clear_announce_stream_task"):
        assert need in f, f"缺函数 {need}（改名或结构漂移，守卫拒绝自证）"
    node, seg = f["_do_announce"]

    # ① 句柄登记（属性赋值）+ done-callback 挂上
    assert "_announce_stream_task" in _attrs_assigned(node), \
        "播报腿未把推流任务登记进 _announce_stream_task —— 三处 cancel 够不着＝孤儿流推到耗尽（台架实锤单轮 6~9 秒）"
    assert "add_done_callback" in _call_names(node), \
        "播报流没挂 done-callback：自然耗尽后句柄悬空，RUN_END 类判据会被历史轮顶住"

    # ② 吊销必须排在"等播报结束"之后（这一刀才掐掉尾巴）
    lines_await = [c.lineno for c in _calls_in(node)
                   if isinstance(c.func, ast.Attribute)
                   and c.func.attr.endswith("announcement_await_response")]
    lines_revoke = [c.lineno for c in _calls_in(node)
                    if isinstance(c.func, ast.Attribute)
                    and c.func.attr == "_revoke_announce_stream"]
    assert lines_await and lines_revoke, \
        "找不到 announcement_await_response 或 _revoke_announce_stream 调用"
    assert min(lines_revoke) > max(lines_await), \
        "吊销落在 await 之前＝收口后才起的流没人管（静默轮永远没人吊销）"


def test_takeover_and_revoke_keep_invariants():
    """I-1（takeover 同步零 await）与 I-2/I-3（吊销器不代发 END、不替新流落状态）。"""
    _src, f = _funcs()
    tk_node, tk_seg = f["_dl_takeover"]
    assert not _has_await(tk_node), \
        "_dl_takeover 里出现了 await —— 旧流会多一次 enqueue 机会（v1.0.88 混音窗口复发）"
    assert "_revoke_announce_stream" in _call_names(tk_node), \
        "_dl_takeover 不再吊销播报流（会话腿被吊销、播报腿漏掉＝同类缺陷复发）"

    rv_node, rv_seg = f["_revoke_announce_stream"]
    names = _call_names(rv_node)
    assert not _has_await(rv_node), "吊销器含 await＝不可在同步路径调用，I-1 会被破坏"
    for forbidden in ("_converge_response", "tts_response_finished",
                      "send_voice_assistant_audio", "send_voice_assistant_announcement_await_response"):
        assert forbidden not in names, \
            f"吊销器调了 {forbidden} —— 被吊销的流不得代发 END/不得替任何轮落状态（I-2/I-3）"


def test_clear_callback_is_identity_guarded():
    """陈旧 done-callback 不得顶掉当前句柄（否则本轮又变孤儿）。"""
    _src, f = _funcs()
    cl_node, seg = f["_clear_announce_stream_task"]
    tests = [n for n in ast.walk(cl_node) if isinstance(n, ast.If)]
    assert tests, "清句柄没有身份判断（无条件清空＝上一轮的 done 会抹掉本轮句柄）"
    dumped = " ".join(ast.dump(t.test) for t in tests)
    assert "_announce_stream_task" in dumped and "Compare" in dumped, \
        "清句柄的身份判断不是 `is task` 形态"


def test_entry_points_share_the_single_choke():
    """两个播报入口必须都汇到 _do_announce（新入口绕开＝句柄纪律再次漏掉）。"""
    _src, f = _funcs()
    for entry in ("async_announce", "async_start_conversation"):
        assert entry in f, f"入口 {entry} 消失，本钉需同步更新"
        assert "_do_announce" in _call_names(f[entry][0]), \
            f"{entry} 不再经 _do_announce —— 新入口自起流会绕过登记与吊销"


def test_session_leg_cancels_not_weakened():
    """改播报腿不得把会话腿原有三处吊销点弄丢。"""
    src, f = _funcs()
    tree = ast.parse(src)
    cancels = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "cancel":
            if isinstance(node.func.value, ast.Attribute) \
                    and node.func.value.attr == "_tts_streaming_task":
                cancels += 1
    assert cancels >= 2, \
        f"会话腿 _tts_streaming_task.cancel() 只剩 {cancels} 处（应≥2：新开轮 / _abort_pipeline）"
    assert "_clear_tts_streaming_task" in src, "会话腿 done-callback 丢了"


# ── 直跑入口（python tests/test_announce_orphan_stream.py）────────────────
if __name__ == "__main__":
    import traceback
    fns = sorted((k, v) for k, v in globals().items()
                 if k.startswith("test_") and callable(v))
    bad = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  [✓][ANNORPH] {name}")
        except Exception:  # noqa: BLE001
            bad += 1
            print(f"  [✗][ANNORPH] {name}")
            print("    " + traceback.format_exc().replace("\n", "\n    ").rstrip())
    print(f"\n  播报孤儿流守卫：{len(fns) - bad}/{len(fns)} 通过"
          + (f"，{bad} 失败" if bad else "，全绿"))
    raise SystemExit(1 if bad else 0)
