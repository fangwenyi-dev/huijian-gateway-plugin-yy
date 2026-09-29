# -*- coding: utf-8 -*-
"""金标复测第 2 轮（对抗复核抓出的"本批引入"与"未真修好"项）——先红后绿。

覆盖（钉面=行为，全部 import 真实现直调）：
  A 否定守卫补语定语小句误杀：把没关紧的窗关上 / 没关好的灯关掉 …（基线有计划，本批被吞）
  B 纠错词界漏区域前缀：主卧舍灯 / 走廊舍灯 …（基线能纠，本批不纠）
  C klar timeout_s=0/"0" 直传 ⇒ aiohttp total=0 永不超时（本批旁效）
  D LlmTransport 的 fail_after 仍横跨 yield（跨任务收口必炸，与 tts 面 v1.0.69 定案同型）
"""
import asyncio
import copy
import os
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_v1127r2_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

from core.nlu import corrector                        # noqa: E402
from core.nlu.fast_path import FastPath               # noqa: E402
from core.nlu.klar_client import KlarClient, DEFAULT_TIMEOUT_S   # noqa: E402
from core.nlu.textcnn import TextCNN                  # noqa: E402
from core.settings import DEFAULTS                    # noqa: E402


class FakeScenes:
    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def refresh(self, force=False):
        pass

    def check(self, text):
        return None

    async def verify_or_refresh(self, phrase):
        return False


class _S:
    def __init__(self, over=None):
        self._over = over or {}

    def get(self, dotted, default=None):
        if dotted in self._over:
            return self._over[dotted]
        cur = copy.deepcopy(DEFAULTS)
        for k in dotted.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur


@pytest.fixture(scope="module")
def fp():
    tc = TextCNN(os.environ["HUIJIAN_NLU_DATA"])
    tc._ensure()
    return FastPath(FakeScenes(), tc, _S())


def _m(fp, t):
    return asyncio.run(fp.match(t))


# ── A 补语定语小句不得被否定守卫误杀 ──────────────────────────────
ATTRIB_CLAUSES = [
    "把没关紧的窗关上",
    "没关好的灯关掉",
    "把没关完的灯关了",
    "把没关紧的客厅窗帘拉上",
    "把没开完的窗打开",
    "没关紧的门关上",
]


@pytest.mark.parametrize("sentence", ATTRIB_CLAUSES)
def test_attributive_negation_clause_still_executes(fp, sentence):
    """「没关紧/好/完的 + 目标」是定语小句不是否定祈使——不得落 MISS。"""
    plan = _m(fp, sentence)
    assert plan is not None, f"定语小句被否定守卫误杀：{sentence!r}"


@pytest.mark.parametrize("sentence", ["别开灯", "不要关灯", "没开灯", "别锁门"])
def test_real_negation_imperative_still_refused(fp, sentence):
    """反向钉：真否定祈使必须仍被拒（防为修误杀而放宽成不设防）。"""
    assert _m(fp, sentence) is None, f"真否定祈使漏放：{sentence!r}"


# ── B 纠错词界：区域前缀形必须仍能纠 ──────────────────────────────
AREA_PREFIX_FIXES = [
    ("关闭主卧舍灯", "关闭主卧射灯"),
    ("关闭走廊舍灯", "关闭走廊射灯"),
    ("车库舍灯", "车库射灯"),
    ("后厨舍灯", "后厨射灯"),
]


@pytest.mark.parametrize("src,want", AREA_PREFIX_FIXES)
def test_area_prefixed_typo_still_corrected(src, want):
    assert corrector.apply(src) == want, f"{src!r} → {corrector.apply(src)!r}"


def test_word_spanning_typo_still_not_corrected():
    """反向钉：跨词命中（宿舍=宿舍、不是舍+灯）不得被改（动错设备比听不懂危险）。"""
    assert corrector.apply("关掉宿舍灯") == "关掉宿舍灯"


# ── C klar timeout_s 非正数按缺省（0 不能变成"永不超时"）───────────
@pytest.mark.parametrize("raw", [0, 0.0, "0", -1])
def test_klar_nonpositive_timeout_falls_back(raw):
    c = KlarClient(_S({"klar.timeout_s": raw}))
    snap = c._opt()                      # (enabled, url, lang, timeout, min_conf, token, bad)
    assert snap[3] == DEFAULT_TIMEOUT_S, f"timeout_s={raw!r} ⇒ {snap[3]}"
    assert "klar.timeout_s" in (snap[6] or ""), f"非正超时须记非法并可见：{snap[6]!r}"


# ── D LlmTransport：cancel scope 不得横跨 yield（跨任务收口）────────
def test_llm_transport_cross_task_close_no_scope_error():
    """跨任务收口复现在**子进程**里跑：全量套件里别的测试会污染 anyio/asyncio
    环境（同一场景单跑绿、全量红），子进程隔离后判据只关于被测代码本身。"""
    import subprocess

    # CI（Lint）环境不带 anyio/aiohttp：本钉按"缺依赖即跳过"处置（与 tts 面
    # test_v1127_transport_creds_ui 的 _real_anyio 同口径），不得把 job 打红。
    probe = subprocess.run([sys.executable, "-c", "import anyio, aiohttp"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
    if probe.returncode != 0:
        pytest.skip(f"环境缺少 anyio/aiohttp（{(probe.stderr or '').strip()[:80]}）")

    CC = HERE / "custom_components" / "huijian_ai"
    script = r'''
import asyncio, importlib.util, sys, types
sys.path.insert(0, r"{here}")
sys.path.insert(0, r"{tests}")
import test_window_speed_behavior as bench
bench._install_ha_stubs()
import anyio
CC = r"{cc}"
ha = sys.modules.get("homeassistant")
if ha is not None and not getattr(ha, "__path__", None):
    ha.__path__ = []
ce = sys.modules.get("homeassistant.config_entries") or types.ModuleType("homeassistant.config_entries")
if not hasattr(ce, "ConfigEntry"):
    ce.ConfigEntry = type("ConfigEntry", (), {{}})
sys.modules["homeassistant.config_entries"] = ce
core = sys.modules.get("homeassistant.core") or types.ModuleType("homeassistant.core")
if not hasattr(core, "HomeAssistant"):
    core.HomeAssistant = type("HomeAssistant", (), {{}})
sys.modules["homeassistant.core"] = core
exc = sys.modules.get("homeassistant.exceptions") or types.ModuleType("homeassistant.exceptions")
for n in ("HomeAssistantError", "ConfigEntryAuthFailed", "ServiceValidationError"):
    if not hasattr(exc, n):
        setattr(exc, n, type(n, (Exception,), {{}}))
sys.modules["homeassistant.exceptions"] = exc

pkg = types.ModuleType("r2cc"); pkg.__path__ = [CC]; sys.modules["r2cc"] = pkg
sub = types.ModuleType("r2cc.huijian"); sub.__path__ = [CC + r"\huijian"]
sub.Dict = dict; sub.EntryAuthFailedError = type("E", (Exception,), {{}})
sub.get_entry_data = lambda hass, entry: {{}}
sys.modules["r2cc.huijian"] = sub
for nm in ("ws_transport", "llm_transport"):
    s = importlib.util.spec_from_file_location("r2cc.huijian." + nm, CC + r"\huijian\\" + nm + ".py")
    m = importlib.util.module_from_spec(s); sys.modules[s.name] = m; s.loader.exec_module(m)
mod = sys.modules["r2cc.huijian.llm_transport"]

async def scenario():
    send, recv = anyio.create_memory_object_stream(10)
    tr = object.__new__(mod.LlmTransport)
    tr._recv_reader = recv
    tr.logger = types.SimpleNamespace(info=lambda *a, **k: None,
        warning=lambda *a, **k: None, error=lambda *a, **k: None,
        debug=lambda *a, **k: None, log=lambda *a, **k: None)
    gen = tr.await_message(timeout=5)
    await send.send(types.SimpleNamespace(state="start", type="text", data=None))
    await send.send(types.SimpleNamespace(state="end", type="text", data=None))
    first = await gen.__anext__()
    async def closer():
        try:
            await gen.aclose(); return None
        except BaseException as e:
            return e
    err = await asyncio.create_task(closer())
    await send.aclose(); await recv.aclose()
    return first, err

first, err = asyncio.run(scenario())
assert err is None, "cross-task close raised: %r" % (err,)
assert first is not None
print("OK")
'''.replace("{here}", str(HERE)).replace("{tests}", str(Path(__file__).resolve().parent)) \
        .replace("{cc}", str(CC)).replace("{{", "{").replace("}}", "}")

    proc = subprocess.run([sys.executable, "-c", script], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)
    assert proc.returncode == 0 and "OK" in (proc.stdout or ""), \
        f"子进程复现失败：rc={proc.returncode}\n{proc.stdout}\n{proc.stderr[-800:]}"


# ── E config_flow：setup_data/mcp_endpoint 进 INFO 前必须脱敏 ───────
def _extract_fn(path, name, ns=None):
    import ast as _ast
    tree = _ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, _ast.FunctionDef) and node.name == name:
            mod = _ast.Module(body=[node], type_ignores=[])
            _ast.fix_missing_locations(mod)
            g: dict = dict(ns or {})
            exec(compile(mod, str(path), "exec"), g)
            return g[name]
    raise AssertionError(f"{name} not found in {path}")


def test_config_flow_setup_log_redacted():
    """「入驻数据」含四端点（可内嵌 ?token=）与 noise_psk——INFO 原文落盘即凭据外流
    （本仓既改过 ws_transport 的同类点，config_flow 是漏网）。"""
    import ast as _ast
    cf = HERE / "custom_components" / "huijian_ai" / "config_flow.py"
    tree = _ast.parse(cf.read_text(encoding="utf-8"))
    consts: dict = {}
    for node in tree.body:
        if isinstance(node, _ast.Assign) and isinstance(node.targets[0], _ast.Name) \
                and node.targets[0].id.startswith("_SETUP_LOG_"):
            consts[node.targets[0].id] = _ast.literal_eval(node.value)
    assert consts, "setup 日志脱敏常量表未找到"
    url_fn = _extract_fn(cf, "_redact_url_for_log")
    fn = _extract_fn(cf, "_redact_setup_for_log",
                     {**consts, "_redact_url_for_log": url_fn})
    out = fn({"llm_endpoint": "ws://192.168.1.9:8000/xiaozhi/v1/llm?token=SEKRET",
              "mcp_endpoint": "wss://gw.example/mcp?token=SEKRET",
              "noise_psk": "abcd1234efgh", "speak_name": "客厅"})
    blob = repr(out)
    assert "SEKRET" not in blob and "token=" not in blob, blob
    assert out["speak_name"] == "客厅" and "192.168.1.9:8000" in out["llm_endpoint"], out
    assert "abcd1234efgh" not in blob and "len=12" in out["noise_psk"], out

    src = cf.read_text(encoding="utf-8")
    assert "_redact_setup_for_log(self.setup_data)" in src, "setup_data 日志未走脱敏"
    assert "_redact_url_for_log(mcp_endpoint)" in src, "mcp_endpoint 日志未走脱敏"
