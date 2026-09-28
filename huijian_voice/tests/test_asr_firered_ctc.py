"""本地 STT 新增 FireRedASR2-CTC 对比档（用户 2026-09-28 点名实测）。

钉桩点（这一档和现役两档不同形，全是会现场炸的点）：
  ① stt.local_model=firered_ctc 必须解析到**独立 lock 键**——main._loop_models 靠
     asr.model_key 决定后台下哪个包、预热哪个档，写错=下错模型或永不就绪；
  ② 该档是 **OfflineRecognizer**（CTC 非自回归），必须走离线单次解码路径；
     现役流式分支（补 1s 尾静音 + is_ready/get_result_all）是 Paraformer 形态，
     CTC recognizer 按那套调用就是空结果；
  ③ 显式选它做主档而构建失败时**不得静默回落 Paraformer**——否则回落档的识别
     结果会被当成 FireRed 的成绩，用户的对比实测直接失效（与"识别错、动作对、
     话术报成功"同一类陷阱）；
  ④ 构造参数必须落在 sherpa-onnx v1.13.4 的真实签名内（该工厂函数**没有
     sample_rate 形参**，沿用 SenseVoice 那套传参＝TypeError→现场"模型加载失败"）；
  ⑤ Web 下拉第三项 + loadSettings 如实回填（未知值仍按默认档显示，与引擎读侧
     _primary_kind 的回落判据同形）。
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import asr as asr_mod                       # noqa: E402
from core.asr import AsrEngine, KEY_PF, KEY_SV        # noqa: E402

PCM = b"\x01\x00" * 8000      # 0.5s s16le@16k → 8000 采样


class S:
    def __init__(self, **kw):
        self.d = kw

    def get(self, k, default=None):
        return self.d.get(k, default)


class Store:
    def __init__(self, ready_keys=()):
        self.ready = set(ready_keys)
        self.base = Path("/models")
        self.async_calls = []

    def model_dir_for(self, key):
        return self.base / key if key in self.ready else None

    def ensure_async(self, key, force=False):
        self.async_calls.append(key)


class FrStream:
    """只提供离线 API（无 input_finished），文本不带 SenseVoice 那套标签。"""

    def __init__(self):
        self.n = 0

    def accept_waveform(self, rate, data):
        self.n += len(data)

    result = type("R", (), {"text": "打开客厅的射灯"})()


class FrRec:
    def __init__(self):
        self.stream = None

    def create_stream(self):
        self.stream = FrStream()
        return self.stream

    def decode_stream(self, s):
        pass

    def reset(self, s):
        pass


def _engine(settings, store, kind_returned="firered_ctc", fail_kinds=()):
    """_build_recognizer 打桩：返回**离线形态**的 CTC recognizer 替身。"""
    eng = AsrEngine(settings, store)

    def fake_build(kind, d):
        if kind in fail_kinds:
            raise RuntimeError(f"boom {kind}")
        rec = FrRec()
        rec._hj_kind = kind_returned
        return rec

    eng._build_recognizer = fake_build
    return eng


def _fr_key():
    key = asr_mod._KIND_KEY.get("firered_ctc")
    assert key, "stt.local_model 未登记 firered_ctc 档（_KIND_KEY 缺项）"
    return key


def test_firered_kind_resolves_to_its_own_lock_key():
    """model_key 必须解析到独立键，且与现役两档互不相同（下载/预热靠它）。"""
    key = _fr_key()
    assert key == "asr_firered_ctc", f"lock 键名不符：{key}"
    assert len({key, KEY_SV, KEY_PF}) == 3, "三档键位不得重叠"
    eng = AsrEngine(S(**{"stt.local_model": "firered_ctc"}), Store())
    assert eng._primary_kind() == "firered_ctc"
    assert eng.model_key == key
    # 默认档不得被这次接线翻掉（存量 settings 无该键时仍 SenseVoice）
    assert AsrEngine(S(), Store()).model_key == KEY_SV


def test_firered_decodes_offline_single_pass_no_tail_pad():
    """在载 firered 时必须整句一次喂入、不补 1s 尾静音、不走流式收流调用。"""
    key = _fr_key()
    eng = _engine(S(**{"stt.local_model": "firered_ctc"}), Store(ready_keys={key}))
    assert eng.ensure_loaded() is True
    assert eng.loaded_kind() == "firered_ctc"
    text = eng._local_transcribe(PCM)
    assert text == "打开客厅的射灯", f"离线分派未生效，拿到：{text!r}"
    assert eng._rec.stream.n == 8000, "CTC 不得补尾静音（8000 采样即整句）"


def test_firered_build_failure_does_not_silently_fall_back():
    """对比档失败不得用 Paraformer 顶替：那会把回落成绩记成 FireRed 的。"""
    key = _fr_key()
    store = Store(ready_keys={key, KEY_SV, KEY_PF})
    eng = _engine(S(**{"stt.local_model": "firered_ctc"}), store,
                  kind_returned="firered_ctc", fail_kinds=("firered_ctc",))
    assert eng.ensure_loaded() is False, "所选档起不来必须如实失败"
    assert eng.loaded_kind() != "paraformer", "静默回落＝对比实测失效"
    # 分因要说清是**所选那档**失败（现场口径与其他两档同形：报 kind，非 lock 键名）
    assert "firered_ctc" in (eng.last_reason or "") and "加载失败" in eng.last_reason, \
        f"失败要带得出具名分因：{eng.last_reason!r}"


def test_firered_build_passes_only_supported_kwargs():
    """构造调用必须落在 v1.13.4 真实签名内（无 sample_rate 形参）。"""
    calls = []

    def from_fire_red_asr_ctc(model, tokens, num_threads=1,
                              decoding_method="greedy_search", debug=False,
                              provider="cpu"):
        calls.append(dict(model=model, tokens=tokens, num_threads=num_threads,
                          provider=provider))
        return FrRec()

    fake = types.SimpleNamespace(
        OfflineRecognizer=types.SimpleNamespace(
            from_fire_red_asr_ctc=from_fire_red_asr_ctc,
            from_sense_voice=lambda **kw: FrRec(),
            from_paraformer=lambda **kw: FrRec(),
        ))
    old = sys.modules.get("sherpa_onnx")
    sys.modules["sherpa_onnx"] = fake
    try:
        eng = AsrEngine(S(), Store())
        eng._build_recognizer("firered_ctc", Path("/models/asr_firered_ctc"))
    finally:
        if old is not None:
            sys.modules["sherpa_onnx"] = old
        else:
            sys.modules.pop("sherpa_onnx", None)

    assert calls, "firered_ctc 分支未调用 from_fire_red_asr_ctc"
    assert calls[0]["num_threads"] == 2, "线程数须与现役两档/台架同参"
    assert calls[0]["provider"] == "cpu", "靶机 4 核 8G 无 GPU，不得默认 cuda"


def test_www_firered_option_wired_and_roundtrip():
    html = (Path(__file__).resolve().parents[1] / "www" / "index.html").read_text(
        encoding="utf-8")
    assert '<option value="firered_ctc">' in html, "下拉缺第三档"
    assert "_STT_KINDS.includes(S.stt.local_model)" in html, \
        "loadSettings 仍是二选一三元式，回填会把 firered 显示成默认档"


def test_lock_entry_firered_ctc_is_measured_and_non_default():
    """lock 条目必须落在**实测**字节上：sha256/实名来自 2026-09-28 gh-proxy 直下。"""
    import json
    lock = json.loads((Path(__file__).resolve().parents[1] / "models.lock.json")
                      .read_text(encoding="utf-8"))
    e = lock["asr_firered_ctc"]
    assert e["required_files"] == ["model.int8.onnx", "tokens.txt"], "须与 tar 内实名一致"
    assert e["sha256"] == "1da8b737ecc5e29f36759a4460c754863e7c919a4ba325aea187331fbfc83274"
    assert e["top_dir"] == "sherpa-onnx-fire-red-asr2-ctc-zh_en-int8-2026-02-25"
    assert e["urls"][0].startswith("https://gh-proxy.com/"), "第一源必须国内可达"
    assert len(e["urls"]) == 2, "hf-mirror 该包实测 404，不得挂死源（逃生门是 import/）"
    assert e["default_provider"] is False, "对比档不得进默认集（否则开机就下 776MB）"
