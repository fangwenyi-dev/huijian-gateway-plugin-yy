"""v1.0.41 安全/稳健批（审查 S1-S11 第二波）回归钉。

每条钉子对应一个**已独立实证**的真实缺陷（探针复现→修复→本钉必红变绿）：
- S1  LLM 工具名白名单（agent._tool）：`x/../../services/light/turn_on` 形态的名字
      曾被直呼进执行链（实证：yarl 对拼接 URL 做 dot-segment 归一化 → 携 Supervisor
      全权 token 可打任意 HA REST 写端点）。
- S2  ha_client.handle_intent 入口意图名形态守卫（全调用方兜底层）。
- S3  settings.update 丢弃非 dict 脏节点：`{"security": null}` 曾被深合并进数据 →
      repair 重置默认 → 静默重生成 ws/pairing token → 全部已配对端握手失效且零提示。
- S9  masked() 脏叶子（ws_token 位藏 dict）不再 500。
- S4  哨兵被"丢最旧保最新"挤丢后，_wrap_audio_stream 靠 pending+排空兜底收束（僵尸
      管线/音频劈裂病灶；AST 抽取真实函数执行，非字符串钉）。
- S6  SttSession 单会话 PCM 累积硬顶（开放局域网灌帧不再无界涨内存）。
- S7  model_store._write_status 中途异常不再泄漏 fd、不再堆 .mst-*.tmp 孤儿。
- S17 24/8/32bit WAV 不再 ValueError 掉进"整文件含 44B 头当裸 PCM"回退（爆音+假识别）。
- S10 boot.sh klar 落位 mv 失败不再假成功（写 .version 令下次启动跳过重试）。
- S11 custom_llm_api 暴露判定 fail-open 从 debug 升 warning（60s 限流）——源码级钉
      （该模块依赖 homeassistant，仓内不可导入，沿用"源码级钉+CI e2e"惯例）。
- S2' 场景部分失败不再带「已执行场景」成功话术（intent_voice_scene 同惯例源码级钉，
      行为面由 sim_full 场景诚实桩 + run_local E2E 覆盖）。
"""
import ast
import asyncio
import io
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
import os
import sys
sys.path.insert(0, str(ROOT))

from core.agent import Agent, TOOLS, _TOOL_NAMES          # noqa: E402
from core.ha_client import HAClient, _INTENT_NAME_RE      # noqa: E402
from core.settings import Settings                        # noqa: E402
from core.session import SttSession                       # noqa: E402
from core import audio as core_audio                      # noqa: E402
from core.model_store import ModelStore                   # noqa: E402

SAT = ROOT / "custom_components" / "huijian_ai" / "assist_satellite.py"


# ── S1：LLM 工具名白名单 ─────────────────────────────────────────────
EVIL = "x/../../services/light/turn_on"


def test_tools_derived_whitelist_matches_schema():
    names = {t["function"]["name"] for t in TOOLS}
    assert _TOOL_NAMES == frozenset(names) and "TurnDeviceOn" in _TOOL_NAMES
    assert EVIL not in _TOOL_NAMES


def test_agent_tool_rejects_traversal_name():
    class Boom:  # 触达即炸：证明拒绝发生在执行之前
        async def run(self, plan):
            raise AssertionError("恶意工具名竟然进了执行器")

    async def go():
        return await Agent(None, None, Boom())._tool(EVIL, {"entity_id": "light.x"})

    ok, msg = asyncio.run(go())
    assert ok is False and "工具名" in msg


def test_agent_tool_rejects_nonstring_name():
    async def go():
        return await Agent(None, None, None)._tool({"weird": 1}, {})
    ok, msg = asyncio.run(go())
    assert ok is False


def test_agent_tool_passes_legit_name():
    seen = {}

    class Rec:
        async def run(self, plan):
            seen["intent"] = plan.intent
            return True, "ok"

    ok, _ = asyncio.run(Agent(None, None, Rec())._tool("TurnDeviceOn", {}))
    assert ok and seen["intent"] == "TurnDeviceOn"


# ── S2：handle_intent 入口形态守卫 ──────────────────────────────────
@pytest.mark.parametrize("bad", [EVIL, "Hass TurnOn", "a" * 65, "", "意图名", None,
                                 "HassTurnOn\n", {"k": 1}])
def test_handle_intent_rejects_illegal_shapes(bad):
    r = asyncio.run(HAClient().handle_intent(bad, {}))
    assert r["success"] is False and "意图名" in r["message"]


def test_handle_intent_accepts_legal_and_reaches_channel_check():
    # 合法名走原路径（无 session ⇒ 「通道未就绪」，而非形态拒绝）——守卫不误伤。
    r = asyncio.run(HAClient().handle_intent("HassTurnOn", {}))
    assert r["message"] != "意图名不合法"
    assert _INTENT_NAME_RE.fullmatch("huijianGetLiveContext")
    assert _INTENT_NAME_RE.fullmatch("HassListScenes")


# ── S3/S9：settings 脏节点与脏叶子 ──────────────────────────────────
def test_update_drops_dirty_nodes_and_keeps_tokens(tmp_path):
    s = Settings(tmp_path / "s.json")
    t0, p0 = s.get("security.ws_token"), s.get("security.pairing_token")
    assert t0 and p0
    s.update({"security": None})
    s.update({"security": "garbage"})
    assert s.get("security.ws_token") == t0, "脏节点曾触发 token 静默重生成"
    assert s.get("security.pairing_token") == p0
    s.update({"llm": {"model": "ok"}})      # 合法 patch 仍正常合并
    assert s.get("llm.model") == "ok"


def test_masked_survives_dirty_token_leaf(tmp_path):
    s = Settings(tmp_path / "m.json")
    s._data["security"]["ws_token"] = {"evil": 1}          # 直写脏叶子（绕过 update）
    d = s.masked()                                           # 旧实现在这里 TypeError
    assert d["security"]["ws_token"] == "****"
    s._data["security"]["pairing_token"] = 12345
    assert s.masked()["security"]["pairing_token"] == "****"


def test_masked_still_masks_str_tokens(tmp_path):
    s = Settings(tmp_path / "k.json")
    t = s.get("security.ws_token")
    d = s.masked()
    assert d["security"]["ws_token"] == t[:4] + "…" + t[-4:]
    assert t not in json_dumps(d)


def json_dumps(x):
    import json
    return json.dumps(x, ensure_ascii=False)


# ── S4：哨兵被挤丢后管线仍能收束（AST 抽真函数）────────────────────
def _extract_func(path: Path, name: str, extra_ns: dict | None = None):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            target = node
            break
    assert target is not None, f"{name} not found"
    mod = ast.Module(body=[target], type_ignores=[])
    ast.fix_missing_locations(mod)
    import typing
    ns = {"asyncio": asyncio, "types": types,
          "AsyncIterable": typing.AsyncIterable}   # py313 在 def 时求值返回注解（3.14 PEP649 惰性）
    if extra_ns:
        ns.update(extra_ns)
    exec(compile(mod, str(path), "exec"), ns)
    return ns[name]


def _sat_ns():
    src = SAT.read_text(encoding="utf-8")
    tree = ast.parse(src)
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in ("_MAX_AUDIO_QUEUE_CHUNKS", "_STREAM_END_POLL_S"):
            consts[node.targets[0].id] = ast.literal_eval(node.value)
    consts["_LOGGER"] = types.SimpleNamespace(
        debug=lambda *a, **k: None, warning=lambda *a, **k: None,
        info=lambda *a, **k: None, exception=lambda *a, **k: None)
    return consts


def test_sentinel_evicted_by_flood_still_closes_stream():
    ns = _sat_ns()
    qf = _extract_func(SAT, "_queue_audio_chunk", ns)
    ns["_queue_audio_chunk"] = qf          # _stop_pipeline 的模块级依赖共享同一 ns
    wrap = _extract_func(SAT, "_wrap_audio_stream", ns)
    stop = _extract_func(SAT, "_stop_pipeline", ns)

    async def go():
        q: asyncio.Queue = asyncio.Queue(maxsize=6)
        me = types.SimpleNamespace(_audio_queue=q, _stream_end_pending=False)
        for i in range(6):
            qf(q, b"D%d" % i)
        stop(me)                       # 哨兵入队（挤掉 D0）
        for i in range(6):
            qf(q, b"E%d" % i)          # 管线停摆期灌帧——哨兵作为最旧被挤丢（实证）
        assert q.full() and all(c is not None for c in list(q._queue)), "构造失效"
        got, t0 = [], time.monotonic()
        async for c in wrap(me):
            got.append(c)
        assert time.monotonic() - t0 < 3.0, "没走兜底就是永久挂起"
        assert got and all(c is not None for c in got)
        assert me._stream_end_pending is False
        return len(got)

    n = asyncio.run(go())
    assert n >= 1


def test_normal_sentinel_path_closes_immediately():
    ns = _sat_ns()
    qf = _extract_func(SAT, "_queue_audio_chunk", ns)
    wrap = _extract_func(SAT, "_wrap_audio_stream", ns)

    async def go():
        q: asyncio.Queue = asyncio.Queue(maxsize=6)
        me = types.SimpleNamespace(_audio_queue=q, _stream_end_pending=False)
        qf(q, b"x")
        qf(q, None)
        got = [c async for c in wrap(me)]
        return got

    got = asyncio.run(go())
    assert got == [b"x"]


# ── S6：SttSession 单会话 PCM 硬顶 ──────────────────────────────────
def test_stt_session_pcm_capped():
    class Dec:
        def decode(self, data):
            return b"\x00\x01" * 1024          # 2KB/帧

    ctx = types.SimpleNamespace(decoder_factory=lambda: Dec())

    async def go():
        s = SttSession(ws=None, ctx=ctx)
        for _ in range(8000):                   # 16MB 无界旧病灶
            await s.on_binary(b"\xfc")
        return s

    s = asyncio.run(go())
    assert len(s._pcm) <= s._MAX_PCM_BYTES
    assert s._pcm_overflow_warned is True


# ── S7：_write_status 无 fd/tmp 孤儿 ────────────────────────────────
def test_write_status_no_orphan_on_failure(tmp_path, monkeypatch):
    import os as _os
    ms = ModelStore(settings=types.SimpleNamespace(get=lambda k, d=None: d),
                    lock_path=tmp_path / "l", models_dir=tmp_path / "m",
                    status_file=tmp_path / "m" / "st.json")
    (tmp_path / "m").mkdir(parents=True, exist_ok=True)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("core.model_store.os.replace", boom)
    fd_before = len(_os.listdir("/proc/self/fd"))
    ms._write_status({"k": {"state": "ready"}})          # 不许抛
    assert list((tmp_path / "m").glob(".mst-*")) == []   # 不留孤儿 tmp
    assert len(_os.listdir("/proc/self/fd")) <= fd_before + 1  # 不泄漏 fd
    monkeypatch.undo()
    ms._write_status({"k": {"state": "ready"}})          # 恢复正常路径
    assert (tmp_path / "m" / "st.json").exists()


# ── S17：WAV 位深精确换算（旧代码非 16bit 一律 int32 重排→崩/爆音）──
def _mk_wav(sampwidth: int, nframes: int = 64, channels: int = 1, rate: int = 22050) -> bytes:
    buf = io.BytesIO()
    with __import__("wave").open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        w.writeframes(bytes(nframes * sampwidth * channels))
    return buf.getvalue()


@pytest.mark.parametrize("sw", [1, 2, 3, 4])
def test_wav_bitdepths_convert_to_int16_body(sw):
    raw, rate = core_audio.read_wav_pcm16(_mk_wav(sw))
    assert rate == 22050
    assert not raw.startswith(b"RIFF"), "样本体混着文件头回吐=爆音病灶"
    assert len(raw) == 64 * 2           # 输出恒 int16 单声道


# ── S10 / S11 / S2'：HA 依赖与 shell 文件——源码级钉（仓内惯例）──────
def test_boot_sh_mv_guarded():
    src = (ROOT / "boot.sh").read_text(encoding="utf-8")
    assert 'if mv -f "$bin" /data/klar/klar' in src
    assert "不写 .version" in src


def test_llm_api_expose_fail_is_warn_throttled():
    src = (ROOT / "custom_components" / "huijian_ai" / "custom_llm_api.py").read_text(encoding="utf-8")
    assert "实体暴露判定失败" in src
    i = src.find("实体暴露判定失败")
    assert "_LOGGER.warning" in src[i - 260:i], "fail-open 留痕必须 ≥warning 档"
    assert "60.0" in src[i - 400:i]


def test_scene_partial_failure_not_spoken_as_success():
    src = (ROOT / "custom_components" / "huijian_ai" / "intent_voice_scene.py").read_text(encoding="utf-8")
    i = src.find("all_success = all(")
    tail = src[i:i + 900]
    assert 'out["error"]' in tail and "个动作没执行成功" in tail
    assert 'if all_success:' in tail, "成功话术必须只在全成功路径"
