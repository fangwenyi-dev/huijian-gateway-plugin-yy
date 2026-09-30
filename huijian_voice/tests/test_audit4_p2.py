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
