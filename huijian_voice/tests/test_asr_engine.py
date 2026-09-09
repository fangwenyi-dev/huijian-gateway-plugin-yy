"""v4.2 ASR 双引擎选择/回落/换绑/输出路径测试（SenseVoice 默认 + Paraformer 兼容档）。

钉桩点（2026-09-13 换引擎定案的行为契约）：
  ① stt.local_model 默认 sensevoice，model_key 随配置解析（_loop_models 消费）；
  ② 主档缺失/构建失败 → 自动回落 Paraformer（fail-open），回落态 stale_kind=True；
  ③ 主档补就绪后 rebind_primary 原地换绑；推理在飞必须跳过（不断会话）；
  ④ SenseVoice 路径：剥离 <|zh|> 类标签、不补尾静音（离线模型无 600ms 整块丢尾坑）；
  ⑤ Paraformer 路径：1s 尾部补静音定案原样保留（回归 2026-09-08 丢尾事故）；
     且**无 _hj_kind 标记的替身默认走此路径**（test_concurrency_guards 兼容）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import asr as asr_mod                      # noqa: E402
from core.asr import AsrEngine, KEY_PF, KEY_SV       # noqa: E402


class S:
    def __init__(self, **kw):
        self.d = kw

    def get(self, k, default=None):
        return self.d.get(k, default)


class Store:
    def __init__(self, ready_keys=(), ensure_noop=True):
        self.ready = set(ready_keys)
        self.ensure_calls = []
        self.base = Path("/models")

    def model_dir_for(self, key):
        return self.base / key if key in self.ready else None

    def ensure(self, key):
        self.ensure_calls.append(key)
        return key in self.ready


class PfStream:
    def __init__(self):
        self.chunks = []
        self.finished = False

    def accept_waveform(self, rate, data):
        self.chunks.append((rate, len(data)))

    def input_finished(self):
        self.finished = True


class PfRec:
    kind = "paraformer"

    def __init__(self):
        self.stream = PfStream()
        self.decoded = 0

    def create_stream(self):
        self.stream = PfStream()
        return self.stream

    def is_ready(self, s):
        if self.decoded == 0:
            self.decoded += 1
            return True
        return False

    def decode_stream(self, s):
        pass

    def get_result_all(self, s):
        return type("R", (), {"text": "打开客厅▁射灯"})()

    def reset(self, s):
        pass


class SvStream:
    def __init__(self):
        self.accepted = 0

    def accept_waveform(self, rate, data):
        self.accepted += len(data)

    result = type("R", (), {"text": "<|zh|><|NEUTRAL|><|Speech|><|woitn|>打开客厅的射灯"})()


class SvRec:
    kind = "sensevoice"

    def __init__(self):
        self.stream = None

    def create_stream(self):
        self.stream = SvStream()
        return self.stream

    def decode_stream(self, s):
        pass

    def reset(self, s):
        pass


def make_engine(settings, store, fail_kinds=()):
    eng = AsrEngine(settings, store)

    def fake_build(kind, d):
        if kind in fail_kinds:
            raise RuntimeError(f"boom {kind}")
        rec = SvRec() if kind == "sensevoice" else PfRec()
        rec._hj_kind = kind
        return rec

    eng._build_recognizer = fake_build
    return eng


PCM = b"\x01\x00" * 8000   # 0.5s s16le@16k


def test_default_kind_and_model_key():
    eng = make_engine(S(), Store())
    assert eng._primary_kind() == "sensevoice"
    assert eng.model_key == KEY_SV
    eng2 = make_engine(S(**{"stt.local_model": "paraformer"}), Store())
    assert eng2.model_key == KEY_PF
    eng3 = make_engine(S(**{"stt.local_model": "瞎写的值"}), Store())
    assert eng3.model_key == KEY_SV, "未知值必须安全回默认 sensevoice"


def test_sv_loads_by_default():
    store = Store(ready_keys={KEY_SV})
    eng = make_engine(S(), store)
    assert eng.ensure_loaded() is True
    assert eng.loaded_kind() == "sensevoice"
    assert store.ensure_calls == []


def test_fallback_to_paraformer_when_sv_broken():
    store = Store(ready_keys={KEY_SV, KEY_PF})
    eng = make_engine(S(), store, fail_kinds=("sensevoice",))
    assert eng.ensure_loaded() is True, "主档坏了也必须能识别（fail-open）"
    assert eng.loaded_kind() == "paraformer"
    assert eng.stale_kind() is True


def test_fallback_load_triggers_ensure_for_missing_dir():
    store = Store(ready_keys={KEY_PF})          # sv 目录缺
    eng = make_engine(S(), store)
    assert eng.ensure_loaded() is True
    assert store.ensure_calls[0] == KEY_SV, "主档目录缺失必须先走同步 ensure（升级过渡）"
    assert eng.loaded_kind() == "paraformer"


def test_rebind_primary_after_main_arrives():
    store = Store(ready_keys={KEY_PF})
    eng = make_engine(S(), store)
    assert eng.ensure_loaded() and eng.loaded_kind() == "paraformer"
    store.ready.add(KEY_SV)                     # 后台下载完成
    eng._busy = 1
    assert eng.rebind_primary() is False, "推理在飞不得换绑"
    eng._busy = 0
    assert eng.rebind_primary() is True
    assert eng.loaded_kind() == "sensevoice"
    assert eng.stale_kind() is False


def test_explicit_paraformer_never_falls_sideways():
    store = Store(ready_keys={KEY_SV, KEY_PF})
    eng = make_engine(S(**{"stt.local_model": "paraformer"}), store)
    assert eng.ensure_loaded() is True
    assert eng.loaded_kind() == "paraformer"
    assert eng.stale_kind() is False, "显式选 paraformer 时它就是主档，无换绑诉求"


def test_sv_transcribe_strips_tags_and_no_tail_pad():
    eng = make_engine(S(), Store(ready_keys={KEY_SV}))
    eng.ensure_loaded()
    text = eng._local_transcribe(PCM)
    assert text == "打开客厅的射灯"
    assert "<|" not in text and "|" not in text
    assert eng._rec.stream.accepted == 8000, "SenseVoice 不补 1s 尾静音（离线模型无丢尾坑）"


def test_pf_transcribe_keeps_tail_padding_and_markerless_compat():
    eng = make_engine(S(**{"stt.local_model": "paraformer"}), Store(ready_keys={KEY_PF}))
    eng.ensure_loaded()
    text = eng._local_transcribe(PCM)
    assert text == "打开客厅 射灯", "▁→空格处理保留"
    last = eng._rec.stream.chunks[-1]
    assert last == (16000, 16000), "尾部必须有 1s 整静音块（2026-09-08 丢尾事故钉桩）"
    assert eng._rec.stream.finished is True
    # 无标记替身（并发守卫同款注入）默认走 pf 路径
    eng._rec = PfRec()
    assert eng._local_transcribe(PCM) != "", "无 _hj_kind 的替身必须兼容旧路径"


def test_lock_entry_pins_sensevoice_int8():
    lock = json.loads((Path(__file__).resolve().parents[1] / "models.lock.json")
                      .read_text(encoding="utf-8"))
    e = lock["asr_sensevoice_small"]
    assert e["required_files"] == ["model.int8.onnx", "tokens.txt"], "绝不钉 fp32 model.onnx"
    assert e["sha256"] == "f6b2a72ebcb1ac7a764d4cfccd886e6bcb2a95c4657c2199d0ba95ed4b9ea71a"
    assert e["urls"][0].startswith("https://gh-proxy.com/"), "第一源必须国内可达"
    assert lock["asr_paraformer_bilingual"]["default_provider"] is False
    assert lock["asr_paraformer_bilingual"]["required_files"], "回落档文件不得删"


def test_settings_defaults_local_model_present_for_merge():
    """存量 settings.json 无 local_model 键——升级后必须靠 DEFAULTS 深合并拿到默认。"""
    from core.settings import DEFAULTS
    assert DEFAULTS["stt"]["local_model"] == "sensevoice"
    assert DEFAULTS["stt"]["provider"] == "local_paraformer", "provider 值空间兼容 pin 不得改"
