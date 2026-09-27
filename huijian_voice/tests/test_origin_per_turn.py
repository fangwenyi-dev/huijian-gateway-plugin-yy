# -*- coding: utf-8 -*-
"""每轮话语自带的卫星身份 → origin 分桶改真（v1.1.7 那道闸的收口批）。

病灶（审计实证，非推测）：v1.1.7 让集成在 hello 里发 `entry.unique_id` 当"卫星
MAC"，但 stt/tts/llm transport 挂在**全局唯一**的 assist 条目上
（`custom_components/huijian_ai/config_flow.py:448` "全局唯一：unique_id=haid"），
所以那个值是 HA instance_id——全屋一颗桶，多卫星照样串台；三颗既有钉又是从客户端
**手写** device 喂进去的（`test_protocol_ws.py:269`），只验了消费侧没验生产侧。

本批口径：卫星身份改由**每轮 detect 帧**携带——一条 WS 连接服务全屋所有卫星，
hello 只在建连时发一次，靠它分不出"这句是谁在说"。加载项按轮取用、**不粘粘**
（上一轮的身份不得污染下一轮）。
"""
import json

import pytest
from aiohttp import ClientSession, WSMsgType

from test_protocol_ws import _run, _connect, server  # noqa: F401  复用真 WS 服务夹具


async def _turn(ws, text: str, device=None):
    """发一轮 detect 并等到 end；返回该轮 pipeline.handle 收到的 (text, origin)。"""
    frame = {"type": "listen", "state": "detect", "text": text}
    if device is not None:
        frame["device"] = device
    await ws.send_str(json.dumps(frame))
    while True:
        msg = await ws.receive(6)
        if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("state") == "end":
            break
    return None


def test_detect_device_overrides_hello_origin(server):
    """每轮带 device ⇒ 该轮 origin 用它，压过 hello 的会话默认值。"""
    port, ctx = server

    async def go():
        async with ClientSession() as sess:
            ws = await _connect(sess, port, "llm")
            await ws.send_str(json.dumps({"type": "hello", "device": "ha-instance-1"}))
            await _turn(ws, "打开客厅的灯", device="sat-bb28")
            await ws.close()
            assert ctx.pipeline.calls
            assert ctx.pipeline.calls[-1][1] == "sat-bb28"
    _run(go())


def test_detect_device_used_without_hello(server):
    """没有 hello 也认每轮 device（连接级默认值不是必要条件）。"""
    port, ctx = server

    async def go():
        async with ClientSession() as sess:
            ws = await _connect(sess, port, "llm")
            await _turn(ws, "打开客厅的灯", device="sat-32b8")
            await ws.close()
            assert ctx.pipeline.calls
            assert ctx.pipeline.calls[-1][1] == "sat-32b8"
    _run(go())


def test_detect_device_absent_falls_back_to_hello(server):
    """旧集成/非卫星入口不带 device ⇒ 逐值回落到连接级默认（hello 值）。"""
    port, ctx = server

    async def go():
        async with ClientSession() as sess:
            ws = await _connect(sess, port, "llm")
            await ws.send_str(json.dumps({"type": "hello", "device": "ha-instance-1"}))
            await _turn(ws, "打开客厅的灯")
            await ws.close()
            assert ctx.pipeline.calls
            assert ctx.pipeline.calls[-1][1] == "ha-instance-1"
    _run(go())


def test_detect_device_blank_ignored(server):
    """空白 device ⇒ 不污染 origin，回落连接级默认（畸形键 fail-open）。"""
    port, ctx = server

    async def go():
        async with ClientSession() as sess:
            ws = await _connect(sess, port, "llm")
            await ws.send_str(json.dumps({"type": "hello", "device": "ha-instance-1"}))
            await _turn(ws, "打开客厅的灯", device="   ")
            await ws.close()
            assert ctx.pipeline.calls
            assert ctx.pipeline.calls[-1][1] == "ha-instance-1"
    _run(go())


def test_detect_device_does_not_stick(server):
    """不粘粘：上一轮的身份不得成为下一轮的默认（多卫星共用一条连接的纪律）。"""
    port, ctx = server

    async def go():
        async with ClientSession() as sess:
            ws = await _connect(sess, port, "llm")
            await ws.send_str(json.dumps({"type": "hello", "device": "ha-instance-1"}))
            await _turn(ws, "打开客厅的灯", device="sat-bb28")
            await _turn(ws, "关掉客厅的灯")
            await ws.close()
            assert len(ctx.pipeline.calls) >= 2
            assert ctx.pipeline.calls[-2][1] == "sat-bb28"
            assert ctx.pipeline.calls[-1][1] == "ha-instance-1", \
                "上一轮 device 粘到了下一轮（按轮取用，不写回会话默认）"
    _run(go())


# ── 生产侧（集成）契约钉：身份真源必须是每轮 device_id ──────────
def _detect_fn_wiring():
    """在 conversation.py 里定位"发 detect 帧"的那个函数，返回接线三元组。

    钉语法不钉裸词：按 AST 取赋值语句与调用参数，注释/日志里的同名文字凑不出来。
    → (帧变量名, device 键赋值右值源码, send_message 实参源码)
    """
    import ast
    from pathlib import Path

    cc = Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai"
    src = (cc / "conversation.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    hits = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        seg = ast.get_source_segment(src, fn) or ""
        if '"type": "listen"' not in seg:
            continue
        frame_var, dev_rhs, sent_arg = None, None, None
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                kv = {}
                for k, v in zip(node.value.keys, node.value.values):
                    if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                        kv[k.value] = v.value
                if kv.get("type") == "listen" and kv.get("state") == "detect" \
                        and isinstance(node.targets[0], ast.Name):
                    frame_var = node.targets[0].id
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Subscript):
                sub = node.targets[0].slice
                if isinstance(sub, ast.Constant) and sub.value == "device":
                    dev_rhs = ast.get_source_segment(src, node.value) or ""
            if isinstance(node, ast.Call):
                nm = getattr(node.func, "attr", "")
                if nm == "send_message" and node.args:
                    sent_arg = ast.get_source_segment(src, node.args[0]) or ""
        hits.append((frame_var, dev_rhs, sent_arg))
    assert len(hits) == 1, f"detect 帧发送点应恰好一处，实得 {len(hits)}"
    return hits[0]


def test_integration_detect_frame_carries_satellite_device():
    """detect 帧必须带 device，且其值取自 user_input.device_id（卫星设备注册 id）。"""
    frame_var, dev_rhs, sent_arg = _detect_fn_wiring()
    assert frame_var, "detect 帧仍是就地 json.dumps 字面量：device 无法按轮注入"
    assert dev_rhs is not None, "帧上没写 device 键（多卫星仍共用一颗桶）"
    assert "device_id" in dev_rhs, f"device 值不是从 device_id 取的：{dev_rhs!r}"
    assert frame_var in sent_arg and "json.dumps" in sent_arg, \
        f"送出去的不是注入后的帧变量：{sent_arg!r}"


def test_integration_detect_device_omitted_when_absent():
    """device 只在真取到时加键——判据必须对 None/空白 fail-open 到旧帧形。"""
    _, dev_rhs, _ = _detect_fn_wiring()
    assert dev_rhs is not None
    assert ".strip()" in dev_rhs, f"空白 device 未过滤：{dev_rhs!r}"

    import ast
    from pathlib import Path

    cc = Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai"
    src = (cc / "conversation.py").read_text(encoding="utf-8")
    guarded = False
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.If):
            seg = ast.get_source_segment(src, node) or ""
            if '["device"]' in seg and "device_id" in seg:
                guarded = True
    assert guarded, "device 键未挂在 if 判据下：缺/空白时会写进旧客户端帧形"
