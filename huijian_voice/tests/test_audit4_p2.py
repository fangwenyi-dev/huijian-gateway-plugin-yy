# -*- coding: utf-8 -*-
"""第四轮审计 P2 批（用户可见/低危类）行为钉——先钉后修，逐条执行真实行为。

覆盖：①面板 Kokoro 档回显（node 真跑页面里那行三目）；②云 STT 错误体截读；
③`?nocache` 空值语义；④连续对话回显判据排 unavailable；⑤OTA 桥断不白签令牌
（钉在 test_ota_firmware 原地加严）；⑥TTS 硬切尾残片并段；⑦模型下载字节上限。
"""
import asyncio
import json
import re
import shutil
import logging
import types
import subprocess
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
CC = HERE / "custom_components" / "huijian_ai"
NODE = shutil.which("node")


# ── ① 面板 Kokoro 档回显（v1.1.27 引入的静默改写回归）───────────────
@pytest.mark.skipif(NODE is None, reason="无 node")
def test_p2_kokoro_provider_echo_kept():
    """存量 provider=local_kokoro 的用户：设置页回显必须是 Kokoro（旧识别数组
    漏了它 ⇒ 回显 Melo、下一次保存静默把引擎翻成 melo）。钉=真跑页面里那行三目。"""
    html = (HERE / "www" / "index.html").read_text(encoding="utf-8")
    m = re.search(r'\$\("#tts_provider"\)\.value = String\(S\.tts\.provider.*?;', html, re.S)
    assert m, "找不到 tts_provider 回显行"
    line = m.group(0)
    js = ("const stub = {value: null}; const $ = () => stub; let S;\n"
          "for (const p of ['local_kokoro','local_matcha','local_melo',"
          "'cloud','cloud_openai_compat','']) {\n"
          "  S = {tts: {provider: p}};\n"
          "  let v;\n" + line + "\n"
          "  console.log(p + '=>' + stub.value);\n"
          "}\n")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(js)
        tmp = f.name
    r = subprocess.run([NODE, tmp], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr[:400]
    got = dict(l.split("=>") for l in r.stdout.strip().splitlines())
    assert got["local_kokoro"] == "local_kokoro", f"Kokoro 档被吞: {got}"
    assert got["local_matcha"] == "local_matcha"
    assert got["local_melo"] == "local_melo"
    assert got["cloud"] == "cloud" and got["cloud_openai_compat"] == "cloud"
    assert got[""] == "local_melo"          # 空值回落默认档


# ── ② 云 STT 非 200：错误体截读 ────────────────────────────────
def test_p2_asr_cloud_error_body_capped():
    src = (HERE / "core" / "asr.py").read_text(encoding="utf-8")
    i = src.index("if r.status != 200:")
    blk = src[i:i + 420]
    assert "r.content.read(8192)" in blk, "云 STT 错误体未截读（整读响应进内存）"
    assert "await r.text()" not in blk, "回潮整读错误体"


# ── ③ `?nocache` 空值语义 ─────────────────────────────────────
def test_p2_nocache_empty_value_disables_cache():
    src = (CC / "huijian" / "http.py").read_text(encoding="utf-8")
    assert 'use_file_cache=request.query.get("nocache") in (None, "")' in src, \
        "`?nocache`（空值）应禁缓存；旧写法 falsy 判等让开关失效"


# ── ④ 连续对话回显判据排 unavailable ──────────────────────────
def test_p2_echo_rejects_unavailable_state():
    src = (CC / "huijian" / "http.py").read_text(encoding="utf-8")
    i = src.index("for _ in range(8):")
    blk = src[i:i + 400]
    assert 'st.state in ("on", "off")' in blk, \
        "关开关时 unavailable 会被判成已回显（面板谎报，旧形 False==False）"


# ── ⑥ TTS 硬切尾残片并段 ─────────────────────────────────────
def test_p2_tts_tail_fragment_merged():
    from core.tts import split_sentences
    text = "一二三四五六七八九十一二三四五六七八九十一"      # 21 字、无标点
    segs = split_sentences(text)
    assert "".join(segs) == text, "切段必须逐字还原"
    assert all(len(s) >= 3 for s in segs), f"1~2 字残段独占成段: {[len(s) for s in segs]}"
    assert all(len(s) <= 22 for s in segs), f"并段后超长: {[len(s) for s in segs]}"


# ── ⑦ 模型下载字节上限（历史遗留⑥）────────────────────────────
def test_p2_model_download_has_byte_cap(tmp_path, monkeypatch):
    from core import model_store as ms

    class _S:
        def get(self, k, dv=None):
            return {"power.auto_download": True}.get(k, dv)

    entry = {"tarball": "m.tar.bz2", "sha256": "", "size_mb": 1,
             "urls": ["https://x.invalid/m"]}
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"mk": entry}), encoding="utf-8")
    store = ms.ModelStore(_S(), lock_path=lock, models_dir=tmp_path / "models",
                          status_file=tmp_path / "st.json")

    class _Resp:
        def __init__(self):
            self.n = 0

        def read(self, n):
            self.n += 1
            return b"x" * (1 << 20) if self.n <= 4 else b""   # 谎报 4MB（声明 1MB）

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    dest = tmp_path / "m.tar.bz2"
    ok = store._download_any({**entry, "_key": "mk"}, dest)
    assert ok is False, "超上限必须拒绝"
    assert not dest.exists(), "超上限不得落盘成品"
    assert not list(tmp_path.glob("m.tar.bz2.part.*")), "超上限须清 .part 残块"


# ── ⑤ OTA 桥断不白签（原地加严见 test_ota_firmware）──────────────
def test_p2_ota_bridge_check_precedes_issue():
    src = (HERE / "core" / "ota_api.py").read_text(encoding="utf-8")
    i_bridge = src.index("_bridge_ok(ctx.ha)")
    i_issue = src.index("store.issue, version, mac")
    assert i_bridge < i_issue, "桥判必须在签发之前（白签 10min 令牌）"


# ══ 剩余批（最早一批收尾）钉 ═════════════════════════════════════════


# ── ① executor 早退路径保留已攒判据 ────────────────────────────
def test_accum_notes_helper_and_wiring():
    import ast as _ast
    src = (HERE / "core" / "executor.py").read_text(encoding="utf-8")
    tree = _ast.parse(src)
    fn = next(n for n in _ast.walk(tree)
              if isinstance(n, _ast.FunctionDef) and n.name == "_accum_notes")
    ns: dict = {}
    exec(compile(_ast.get_source_segment(src, fn), "<accum>", "exec"), ns)  # noqa: S102
    acc = ns["_accum_notes"]
    assert acc(["射灯"], [], [], [], []) == "（「射灯」我没找到）"
    out = acc(["射灯"], ["台灯"], ["电视"], ["门锁"], ["「大门锁」没确认到已上锁"], 2)
    for seg in ("我没找到", "状态上", "回执", "离线", "没确认到", "另有 2 条"):
        assert seg in out, (seg, out)
    assert acc([], [], [], [], []) == ""
    # 三处早退（能力闸/预裁/可用态）都必须带尾注
    assert src.count("+ self._accum_notes(missing, noops, no_receipt,") == 3, \
        "早退路径未接已攒判据尾注（部分执行被说成整句没做）"


# ── ⑪ 播报单位：湿度 %、极值档中文 ─────────────────────────────
def test_executor_speech_units_humidity_and_special():
    from conftest import FakeHAClient
    from core.executor import Executor
    from core.nlu.fast_path import Plan

    ex = Executor(FakeHAClient(), None)

    def say(attr, delta):
        plan = Plan("AdjustDeviceAttribute",
                    {"attribute": attr, "delta": delta,
                     "target": [{"devices": [{"name": "加湿器", "domains": ["humidifier"]}]}]},
                    "t0", utterance="")
        return ex.speech(plan, {"success": True, "control_targets": [{"name": "加湿器"}]})

    s1 = say("humidity", "60")
    assert "60%" in s1 and "档" not in s1, f"湿度播报单位不对: {s1!r}"
    s2 = say("brightness", "max")
    assert "最大" in s2 and "max" not in s2, f"极值档未中文化: {s2!r}"


# ── ⑫ indeterminate 标记被 executor 采纳 ───────────────────────
def test_executor_honours_indeterminate_flag():
    from conftest import FakeHAClient
    from core.executor import Executor
    from core.nlu.fast_path import Plan
    ha = FakeHAClient(results={"TurnDeviceOn": {
        "success": False, "message": "HA 通道异常", "indeterminate": True}})
    ex = Executor(ha, None)
    asyncio.run(ex.run_raw(Plan("TurnDeviceOn", {"entity_id": "light.x"}, "t0")))
    assert ex.last_run["indeterminate"] is True, \
        "连接类失败必须带不确定性（否则降级重放闸放行、相对量做第二遍）"
    src = (HERE / "core" / "ha_client.py").read_text(encoding="utf-8")
    assert src.count('"indeterminate": _indeterminate_exc(e)') == 3, \
        "ha_client 三处异常折叠未打标记"
    assert 'def _indeterminate_exc(' in src


# ── ②⑤ 状态新鲜度可判 + 查询加注 ───────────────────────────────
def test_states_stale_reasons_and_query_caveat():
    from core.ha_client import HAClient
    from core.nlu.query import QueryZone

    hc = HAClient.__new__(HAClient)
    hc._states_ts, hc._states_ok = 0.0, False
    assert "尚未取到" in hc.states_stale()
    import time as _t
    hc._states_ts, hc._states_ok = _t.time(), False
    assert "读取失败" in hc.states_stale()
    hc._states_ts, hc._states_ok = _t.time() - 10_000, True
    assert "未更新" in hc.states_stale()
    hc._states_ts, hc._states_ok = _t.time(), True
    assert hc.states_stale() == ""

    q = QueryZone.__new__(QueryZone)

    class _Ha:
        @staticmethod
        def states_stale():
            return "上次状态读取失败（快照约 30 秒前）"

    async def _inner(text):
        return "客厅灯是开着的"

    q.ha, q._answer_inner = _Ha(), _inner
    out = asyncio.run(q.answer("客厅的灯开着吗"))
    assert "客厅灯是开着的" in out and "数字可能不是最新" in out, out


# ── ⑥⑦⑧ pipeline 三项 ────────────────────────────────────────
def test_pipeline_dedup_gc_and_klar_write_gate():
    from collections import OrderedDict

    import core.pipeline as pl
    from core.nlu.klar_client import KLAR_CONTROL_INTENTS
    from core.pipeline import Pipeline
    p = Pipeline.__new__(Pipeline)
    p._origin_ts = {}
    assert p._dkey("请调亮一点", "devA") == p._dkey("调亮一点", "devA"), \
        "去重键必须用 canonical 归一（否则同句三写各执行一遍）"
    p._last_list = OrderedDict()
    p._set_last_list("devA", "scene")
    assert "devA" in p._origin_ts, "_last_list 键必须并入 GC 依据（否则 64 上限裁剪落空）"
    for fam in ("HassFanSetSpeed", "HassClimateSetHumidity"):
        assert fam in pl._KLAR_WRITE_INTENTS, f"{fam} 未进 grounded 写值证据闸"
        assert fam in KLAR_CONTROL_INTENTS, f"{fam} 已不在接管白名单（表漂移）"


# ── ⑩ 开合器：用户原话即证据 ──────────────────────────────────
def test_opener_without_window_char_now_matches():
    import test_audit4_fixes as a4
    import test_window_speed_behavior as bench
    bench._install_ha_stubs()
    hass = a4._build("dev_kh", "办公室开合器", "area_office",
                     ["办公室开合器 开启", "办公室开合器 关闭"])
    res = a4._handle(hass, "开合器", "办公室", action="close")
    assert res["success"] is True, res
    assert a4._eids(hass.services.calls) == ["button.dev_kh_1"], \
        f"整名不含'窗'的开合器仍失配: {a4._eids(hass.services.calls)}"


# ── P3 四条 ──────────────────────────────────────────────────
def test_p3_translations_token_and_lock_doc():
    for f in ("en.yaml", "zh-CN.yaml", "zh-Hans.yaml"):
        s = (HERE / "translations" / f).read_text(encoding="utf-8")
        assert "无需重启" not in s and "Applies live" not in s, f"{f} 仍称即时生效"
        assert "restart" in s or "重启" in s
    html = (HERE / "www" / "index.html").read_text(encoding="utf-8")
    assert "var(--bad)" not in html and "var(--red)" in html
    lock = (HERE / "firmware.lock.json").read_text(encoding="utf-8")
    assert "gh-proxy 代理→GitHub 直连→Gitee 兜底" in lock, "lock _doc 顺序未更正"
    acr = (HERE.parent / "scripts" / "acr_transcode.py").read_text(encoding="utf-8")
    assert "def head(self" in acr and "dst.head(" in acr, "秒传探测未改真 HEAD"


# ══ P2 未修 8 条收口批（工作树）钉 ═══════════════════════════════════

# ── P2-14：链歧义退单发清挂起必须用归一键 ─────────────────────────
def test_p2_confirm_pop_uses_normalized_key():
    """_chain_decide 里的清挂起必须用归一键（_confirm_answer 等处的
    pop(origin) 都在 `origin = origin or "panel"` 之后，属合法——按 AST 只看
    _chain_decide）。"""
    import ast as _ast
    src = (HERE / "core" / "pipeline.py").read_text(encoding="utf-8")
    tree = _ast.parse(src)
    bad = []
    for fn in _ast.walk(tree):
        if not (isinstance(fn, _ast.AsyncFunctionDef) and fn.name == "_chain_decide"):
            continue
        for call in _ast.walk(fn):
            if (isinstance(call, _ast.Call) and isinstance(call.func, _ast.Attribute)
                    and call.func.attr == "pop" and call.args
                    and isinstance(call.args[0], _ast.Name)
                    and call.args[0].id == "origin"):
                bad.append(call.lineno)
    assert not bad, f"_chain_decide 回潮未归一 pop（行 {bad}）：origin 为空时挂起清不掉"
    assert 'self._confirm.pop(origin or "panel", None)' in src

# ── P2-17/18：半度步进可达 + 越档吸附（不再抛英文） ─────────────────
def _calc_target():
    import ast as _ast
    import textwrap
    from enum import Enum as _Enum
    from typing import Literal
    src = (CC / "intent_adjust_attribute.py").read_text(encoding="utf-8")
    tree = _ast.parse(src)
    ns: dict = {"intent": types.SimpleNamespace(
        IntentHandleError=type("IHE", (Exception,), {})),
        "_LOGGER": logging.getLogger("t"), "Literal": Literal,
        "Enum": _Enum, "DeltaSupport": object}   # 注解占位（Literal 别名）
    ns["UnsupportAdjustmentError"] = ns["intent"].IntentHandleError("unsupported")
    for node in tree.body:
        if isinstance(node, _ast.ClassDef) and node.name == "AdjustType":
            exec(compile(_ast.get_source_segment(src, node), "<e>", "exec"), ns)  # noqa: S102
    fn = next(n for n in _ast.walk(tree)
              if isinstance(n, _ast.FunctionDef) and n.name == "calc_target")
    code = "class _Ctx:\n" + textwrap.indent(
        _ast.get_source_segment(src, fn), "    ") + "\n"
    exec(compile(code, "<calc>", "exec"), ns)  # noqa: S102

    def call(adjust, value, min_change, *, cur=None, unit="", special="",
             lo=0, hi=100):
        c = ns["_Ctx"]()
        c.adjust, c.value, c.unit, c.special = adjust, value, unit, special
        return c.calc_target(cur, min_change, min_change, lo, hi, {"number"})
    return ns["AdjustType"], call


def test_p2_half_degree_and_step_snap():
    AdjustType, call = _calc_target()
    # 0.5 度步进：目标值不得被 int() 截断
    assert call(AdjustType.SET, 26.5, 0.5, cur=24, lo=16, hi=30) == 26.5
    assert call(AdjustType.INCREASE, 0.5, 0.5, cur=26, lo=16, hi=30) == 26.5
    # step=25 的百分比设备：「调到60%」吸附到最近档（旧形抛英文错误）
    assert call(AdjustType.SET, 60, 25) == 50
    assert call(AdjustType.DECREASE, -30, 25, cur=60) == 25
    # step=1 的原行为不变
    assert call(AdjustType.SET, 60, 1) == 60
    # 对抗复核（2026-09-30）：吸附档不得逆行——INCREASE 必须高于当前值、
    # DECREASE 必须低于（旧形 51→50、99→100 反向走）
    assert call(AdjustType.INCREASE, 10, 25, cur=51) == 75
    assert call(AdjustType.DECREASE, -1, 25, cur=99) == 75
    assert call(AdjustType.INCREASE, 10, 25, cur=50) == 75    # 原位不算调高
    assert call(AdjustType.DECREASE, -10, 25, cur=100) == 75  # 原位不算调低


# ── P2-21：本地 STT 异常必须落本轮分因 ─────────────────────────────
def test_p2_asr_local_exc_sets_round_reason():
    import threading as _th
    from core.asr import AsrEngine

    class _Rec:
        def create_stream(self):
            raise RuntimeError("boom")

    class _S:
        def get(self, k, dv=None):
            return "sensevoice"

    eng = AsrEngine.__new__(AsrEngine)
    eng._rec, eng._lock, eng._busy, eng.settings = _Rec(), _th.Lock(), 0, _S()
    sink: dict = {}
    out = eng._local_transcribe(b"\x00" * 100, reason_out=sink)
    assert out == "" and "异常" in str(sink.get("reason") or ""), \
        f"本地识别异常必须写本轮分因（否则与真静音同形）: {sink}"


# ── P2-28/29：代次闸与自愈代次（行为钉：真调传输层，不断言源码文本）──
def _restart_recorder(tr):
    """把 restart_connection 换成"建协程即记账"替身——实参在协程构造时就绑定，
    所以 `_schedule_restart` 到底带没带代次，这里看到的就是它传出去的。"""
    calls = []

    def _rec(reason="", generation=None):
        calls.append((reason, generation))

        async def _noop():
            return None
        return _noop()

    tr.restart_connection = _rec
    return calls


def _bare_transport(mod, cls_name):
    import test_v1127_transport_creds_ui as creds
    tr = getattr(mod, cls_name).__new__(getattr(mod, cls_name))
    tr.logger = logging.getLogger("hj_p2_gen_gate")
    tr._conn_gen = 3
    tasks = []

    class _Hass:
        def async_create_background_task(self, coro, name=None):
            tasks.append(coro)
            coro.close()
            return None

    tr.hass = _Hass()
    return tr, tasks


def test_p2_ws_restart_carries_generation():
    import test_v1127_transport_creds_ui as creds
    mods = creds._load_hj()
    ws, _tasks = _bare_transport(mods["ws_transport"], "WsTransport")
    calls = _restart_recorder(ws)

    ws._schedule_restart("x", generation=3)
    assert calls == [("x", 3)], \
        f"_schedule_restart 未把代次透传 restart_connection: {calls}"

    class _Stall:
        async def send(self, msg):
            await asyncio.sleep(30)

    ws.update_activity_time = lambda: None
    ws._send_writer = _Stall()
    ws._SEND_HANDOFF_TIMEOUT_S = 0.05
    asyncio.run(ws.send_message({"a": 1}))
    assert ("send stalled", 3) in calls, \
        f"send 卡死自愈未带发送时刻代次（=不设闸，晚到会拆新连接）: {calls}"


def test_p2_tts_stale_claim_restart_carries_generation():
    """P2-29 的同类第二处：TTS 无人认领接管也走 _schedule_restart。
    restart_connection 的闸是 `generation is not None and …`——**不传等于不设闸**，
    后台任务真正跑起来时若已换连，它会把新连接拆掉并把 _proto 归零。"""
    import test_v1127_transport_creds_ui as creds
    mods = creds._load_hj()
    tts, _tasks = _bare_transport(mods["tts_transport"], "TtsTransport")
    tts._round_active = True
    tts._is_connected = True
    tts._current_ws = types.SimpleNamespace(closed=False)   # is_connected 的判据
    calls = _restart_recorder(tts)

    assert tts._claim_round() == 3, "接管后应回本轮认领的连接代次"
    assert ("TTS stale claim taken over", 3) in calls, \
        f"接管清算没带代次（旧形=无条件拆连接）: {calls}"


def test_p2_llm_gen_gate_and_closed_resource():
    """轮中换连 ⇒ 本轮收口成 error 帧；stop()/换连关流 ⇒ ClosedResourceError
    也被收口（旧形直穿 conversation）。两条都真驱动 async generator。"""
    import test_v1127_transport_creds_ui as creds
    import importlib.util
    import sys as _sys
    mods = creds._load_hj()
    name = "huijian.llm_transport"
    if name not in _sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, CC / "huijian" / "llm_transport.py")
        mod = importlib.util.module_from_spec(spec)
        _sys.modules[name] = mod
        spec.loader.exec_module(mod)
    llm_mod = _sys.modules[name]

    def _fresh():
        import anyio
        send, recv = anyio.create_memory_object_stream(10)
        tr = object.__new__(llm_mod.LlmTransport)
        tr._recv_reader = recv
        tr._conn_gen = 3
        tr.logger = logging.getLogger("hj_p2_llm_gate")
        return tr, send, recv

    async def _swapped_mid_round():
        tr, send, recv = _fresh()
        gen = tr.await_message(timeout=5)
        task = asyncio.create_task(gen.__anext__())
        await asyncio.sleep(0)              # 生成器体起跑：采样本轮代次=3
        tr._conn_gen = 9                    # 轮中被换连
        await send.send(types.SimpleNamespace(
            state="start", type="text", data=None))
        try:
            return await task
        finally:
            await send.aclose()
            await recv.aclose()

    item = asyncio.run(_swapped_mid_round())
    assert isinstance(item, dict) and item.get("error"), \
        f"轮中换连必须收口成 error 帧，实得 {item!r}"

    async def _closed_stream():
        tr, send, recv = _fresh()
        gen = tr.await_message(timeout=5)
        task = asyncio.create_task(gen.__anext__())
        await asyncio.sleep(0)
        await recv.aclose()                 # 读端被关＝stop()/换连的形态
        try:
            return await task
        finally:
            await send.aclose()

    item2 = asyncio.run(_closed_stream())
    assert isinstance(item2, dict) and item2.get("error"), \
        f"关流异常必须收口成 error 帧（旧形直穿 conversation），实得 {item2!r}"


# ── P2-30/31：config_flow 两处 ───────────────────────────────────
def test_p2_config_flow_host_submit_and_delete_aggregation():
    src = (CC / "config_flow.py").read_text(encoding="utf-8")
    i = src.index("async def async_step_user(")
    blk = src[i:i + 900]
    assert "CONF_HOST in user_input" in blk and "_async_step_user_base(user_input)" in blk, \
        "手工表单提交必须回表单步（旧形被 qrcode 静默丢弃）"
    j = src.index('if user_input is not None:\n            to_delete = user_input.get')
    dblk = src[j:j + 2600]
    assert "if to_delete or to_delete_auto:" in dblk, "两族删除必须一次收口"
    assert "场景删除失败：" in dblk and "自动化删除失败：" in dblk, "失败必须展示"
    assert "if deleted:\n                    return self.async_show_form" not in dblk, \
        "回潮早退（删了场景就丢自动化）"


# ══ C 段收口批（工作树）钉 ═══════════════════════════════════════════

def test_c2_speech_never_raises_on_bad_rows():
    """`{"control_targets":[None]}`/`{"states":[None]}` 旧形会 AttributeError，
    违 speech() 的"永不炸"约定。"""
    from conftest import FakeHAClient
    from core.executor import Executor
    from core.nlu.fast_path import Plan
    ex = Executor(FakeHAClient(), None)
    plan = Plan("TurnDeviceOn", {"target": [{"devices": [{"name": "灯"}]}]}, "t0")
    for bad in ({"control_targets": [None]},
                {"control_targets": [{"name": "灯"}, None]},
                {"states": [None], "success": True}):
        out = ex.speech(plan, bad)          # 不抛即通过
        assert isinstance(out, str) and out


def test_c3_deleted_area_not_replaced_by_uuid():
    from core.ha_client import HAClient
    rows = [{"entity_id": "light.x", "area_id": "gone-uuid", "device_id": None,
             "disabled_by": None, "hidden_by": None}]
    ent_map, _alias, _cls = HAClient._parse_registry(rows, areas={})
    assert "light.x" not in ent_map, \
        "已删区域被 uuid 顶替（与 _device_area_map『已删区域一律丢弃』相反）"
    rows2 = [{"entity_id": "light.y", "area_id": "a1", "device_id": None,
              "disabled_by": None, "hidden_by": None}]
    ent_map2, _a, _c = HAClient._parse_registry(rows2, areas={"a1": "客厅"})
    assert ent_map2.get("light.y") == "客厅", "正常区域路径不得回归"


def test_c4_missing_success_row_counts_as_failure():
    from core.ha_client import HAClient
    out = HAClient._normalize_result(200, '{"states": [{"name": "x"}]}', "n")
    assert out["success"] is False, \
        "缺 success 的行必须按失败计（与集成侧 fold_action_ok 同向，旧形默认 True）"
    ok = HAClient._normalize_result(200, '{"states": [{"name": "x", "success": true}]}', "n")
    assert ok["success"] is True


def test_c6_stt_rid_overwritten_each_round():
    src = (HERE / "core" / "session.py").read_text(encoding="utf-8")
    assert "self._rid = self._parse_rid(obj) or 0" in src, \
        "STT rid 必须每轮覆盖（旧形 if r: 会跨轮回显上一轮身份）"
    assert "if r:\n                    self._rid = r" not in src


def test_c7_public_task_api_only():
    src = (HERE / "custom_components" / "huijian_ai" / "intent_turn.py").read_text(encoding="utf-8")
    assert "hass.async_create_task_internal(" not in src, "非公开 API 回潮"
    assert "hass.async_create_task(" in src


def test_c8_pipeline_creation_isolates_dynamic_vocab():
    src = (HERE / "tests" / "test_pipeline_creation.py").read_text(encoding="utf-8")
    assert "_isolate_dynamic_vocab" in src and "clear_vocab()" in src, \
        "缺 targets 动态词表隔离 fixture（用例顺序敏感）"


@pytest.mark.skipif(NODE is None, reason="无 node")
def test_c_escape_attr_in_automations_page():
    """删除按钮改 dataset 取参：文本不再拼进内联 onclick 的 JS 字符串。"""
    html = (CC / "templates" / "automations.html").read_text(encoding="utf-8")
    assert "function escapeAttr(" in html, "缺属性级转义"
    assert "onclick=\"deleteAutomation(this)\"" in html, "按钮未改 dataset 形态"
    assert "deleteAutomation(\''" not in html, "回潮：文本拼进内联 handler"
    assert "data-trigger=\"' + escapeAttr(triggerText) + '\"" in html
    # 内联脚本仍须语法可过（本仓前端守卫同源）
    m = re.search(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(m.group(1))
        tmp = f.name
    r = subprocess.run([NODE, "--check", tmp], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr[:400]
