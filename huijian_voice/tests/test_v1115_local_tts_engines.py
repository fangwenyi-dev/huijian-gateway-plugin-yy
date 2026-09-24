"""v1.1.5 钉：本地 TTS 多引擎档（local_matcha / local_melo）。

背景（2026-09-21 四引擎台架横评 bench_tts_20260921）：
- Matcha zh-en RTF 0.022/首包 32ms、MeloTTS zh_en 0.197 入选可换档；
  ZipVoice distill RTF 0.9~1.66 + 回调非增量 → **不接入**（本文件反向钉）。
- Matcha 官方包不含声码器（缺 vocos 时 sherpa C++ 构造**终止进程**，台架实锤），
  model_store 新增 extra_files 二跳下载；vocos 入 required_files 硬闸。
- 换绑纪律：在飞合成让位（旧嗓干完本轮）；缓存键/指纹含引擎维度。
"""
import json
import re
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


class _Settings:
    def __init__(self, data=None):
        self._d = data or {}

    def get(self, path, default=None):
        return self._d.get(path, default)


def _engine(data, store=None):
    import core.tts as m
    return m.TtsEngine(_Settings(data), store or types.SimpleNamespace())


# ── lock 清单 ──────────────────────────────────────────────────

def _lock():
    return json.loads((_ROOT / "models.lock.json").read_text(encoding="utf-8"))


def test_lock_matcha_entry_hard_gates_vocoder():
    e = _lock()["tts_matcha_zh_en"]
    assert e["default_provider"] is False
    assert "vocos-16khz-univ.onnx" in e["required_files"]      # 就绪硬闸（C++ 终止进程防线）
    ex = e["extra_files"][0]
    assert ex["file"] == "vocos-16khz-univ.onnx"
    assert re.fullmatch(r"[0-9a-f]{64}", ex["sha256"])
    assert ex["urls"][0].startswith("https://gh-proxy.com/")   # 国内源优先同主包纪律
    assert re.fullmatch(r"[0-9a-f]{64}", e["sha256"])
    assert e["urls"][0].startswith("https://gh-proxy.com/")


def test_lock_melo_entry_present_and_no_zipvoice():
    lk = _lock()
    e = lk["tts_melo_zh_en"]
    assert "model.onnx" in e["required_files"]
    assert re.fullmatch(r"[0-9a-f]{64}", e["sha256"])
    # 反向钉：ZipVoice 台架出局（RTF>1 非增量），清单里不得混入
    assert not any("zipvoice" in k for k in lk)


# ── provider → 模型键 / 指纹 / sid ─────────────────────────────

def test_provider_model_key_mapping_and_unknown_fallback():
    assert _engine({"tts.provider": "local_kokoro"}).model_key() == "tts_kokoro_multilang"
    assert _engine({"tts.provider": "local_matcha"}).model_key() == "tts_matcha_zh_en"
    assert _engine({"tts.provider": "local_melo"}).model_key() == "tts_melo_zh_en"
    assert _engine({"tts.provider": "local_不存在的档"}).model_key() == "tts_melo_zh_en"  # v1.1.10 未知档回落 melo


def test_fingerprint_engine_prefix_rotates_but_kokoro_compat():
    # Kokoro 前缀保持 "local:"（存量 HA 盘缓存键不轮换）；换档必换键
    assert _engine({"tts.provider": "local_kokoro", "tts.sid": 28}).voice_fingerprint() \
        .startswith("local:sid28+")
    assert _engine({"tts.provider": "local_matcha", "tts.sid": 28}).voice_fingerprint() \
        .startswith("local_matcha:")
    assert _engine({"tts.provider": "local_melo", "tts.sid": 28}).voice_fingerprint() \
        .startswith("local_melo:")
    assert _engine({"tts.provider": "local_matcha"}).voice_fingerprint() != \
        _engine({"tts.provider": "local_melo"}).voice_fingerprint()


def test_resolve_sid_single_speaker_clamps_silently():
    eng = _engine({"tts.provider": "local_matcha", "tts.sid": 28})
    eng._tts = types.SimpleNamespace(num_speakers=1)     # 单女声引擎
    assert eng.resolve_sid() == 0                        # Kokoro 默认 28 残留 → 钳 0
    eng2 = _engine({"tts.provider": "local_matcha", "tts.sid": 99})
    eng2._tts = types.SimpleNamespace(num_speakers=1)
    assert eng2.resolve_sid() == 0                       # 真越界同样钳 0


# ── 换绑纪律 ───────────────────────────────────────────────────

def test_rebind_defers_while_round_inflight():
    eng = _engine({"tts.provider": "local_matcha"})
    old = object()
    eng._tts = old
    eng._loaded_prov = "local_kokoro"
    eng._round_busy = 1                                  # 有播报轮在飞
    assert eng.ensure_loaded() is True                   # 让位=保旧嗓收 True，不炸不断
    assert eng._tts is old
    assert not eng.ready_for_current_provider()          # 但当前档未就绪，models 循环会再试


def test_ready_for_current_provider():
    eng = _engine({"tts.provider": "local_matcha"})
    assert not eng.ready_for_current_provider()          # 无引擎
    eng._tts = object()
    eng._loaded_prov = "local_kokoro"
    assert not eng.ready_for_current_provider()          # 在载≠当前档（换绑触发条件）
    eng._loaded_prov = "local_matcha"
    assert eng.ready_for_current_provider()


# ── model_store extra_files ────────────────────────────────────

def test_extra_files_downloaded_after_extract(tmp_path, monkeypatch):
    from core import model_store as ms
    entry = {"tarball": "m.tar.bz2", "sha256": "x", "top_dir": "mtop",
             "required_files": ["model.onnx", "vocos-16khz-univ.onnx"],
             "extra_files": [{"file": "vocos-16khz-univ.onnx", "sha256": "",
                              "urls": ["https://example.invalid/v.onnx"]}]}
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"mk": entry}), encoding="utf-8")
    store = ms.ModelStore(_Settings({"power.auto_download": True}),
                          lock_path=lock,
                          models_dir=tmp_path / "models",
                          status_file=tmp_path / "status.json")
    keydir = tmp_path / "models" / "mk" / "mtop"
    keydir.mkdir(parents=True)
    (keydir / "model.onnx").write_bytes(b"1")
    (tmp_path / "models" / "mk" / ".extracted_ok").write_text("m.tar.bz2|x")
    calls = []

    def fake_dl(e, dest):
        calls.append((e["tarball"], Path(dest)))
        Path(dest).write_bytes(b"voc")
        return True
    monkeypatch.setattr(store, "_download_any", fake_dl)
    assert store._ensure_extra_files("mk", entry) is True
    assert calls and calls[0][0] == "vocos-16khz-univ.onnx"
    assert (keydir / "vocos-16khz-univ.onnx").exists()
    assert store.model_dir_for("mk") is not None         # vocos 落位后才算就绪
    # 幂等：文件已在 → 不再触发下载
    calls.clear()
    assert store._ensure_extra_files("mk", entry) is True
    assert not calls


def test_extra_files_failure_marks_incomplete(tmp_path, monkeypatch):
    from core import model_store as ms
    entry = {"tarball": "m.tar.bz2", "top_dir": "", "required_files": ["v.onnx"],
             "extra_files": [{"file": "v.onnx", "sha256": "", "urls": ["u"]}]}
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"mk2": entry}), encoding="utf-8")
    store = ms.ModelStore(_Settings({}), lock_path=lock,
                          models_dir=tmp_path / "models",
                          status_file=tmp_path / "status.json")
    (tmp_path / "models" / "mk2").mkdir(parents=True)
    monkeypatch.setattr(store, "_download_any", lambda e, d: False)
    assert store._ensure_extra_files("mk2", entry) is False
    assert store.snapshot()["mk2"]["state"] == "incomplete"


# ── Web 结构钉（引擎下拉与白名单）──────────────────────────────

def test_web_provider_options_and_whitelist():
    html = (_ROOT / "www" / "index.html").read_text(encoding="utf-8")
    assert '<option value="local_matcha">' in html
    assert '<option value="local_melo">' in html
    assert '"local_kokoro","local_matcha","local_melo","cloud","cloud_openai_compat"' in html
    assert "tts_single_hint" in html and "tv_row" in html


def test_web_voice_table_rebuilt_per_engine():
    """2026-09-22：音色表随引擎档重建。旧形态=一张 Kokoro 103 项表走天下，
    切到 Matcha/Melo/云档只盖一句"该引擎单女声（音色表与自定义音色仅 Kokoro
    可用）"——用户看到的下拉内容与真正发声的引擎对不上。

    两侧事实都钉：
    - 正向：Kokoro 主表仍 103 项；Matcha/Melo 各列本档唯一内置女声
      （实测 num_speakers=1，包内无 voices.bin，无追加载体）；
      云档只列固定回落嗓（tts.sid 在云档不生效，core/tts.py:1323）。
    - 反向：非 Kokoro 表不得复用 VOICES、不得混入 zf_*/zm_* 音色名；
      旧一刀切文案必须消失；tts.sid 不得再被无条件提交——否则切到单音色档
      保存一次就会把 Kokoro 档的嗓静默改写成 0。
    """
    html = (_ROOT / "www" / "index.html").read_text(encoding="utf-8")

    # ① Kokoro 主表仍是 103 项（计数钉：防被单音色表顶掉或截断）
    kok = re.search(r"const VOICES = \[(.*?)\n\];", html, re.S)
    assert kok, "VOICES 主表缺失"
    assert len(re.findall(r"\[\d+,", kok.group(1))) == 103

    # ② 四档各有一张表，非 Kokoro 档均 1 项且不复用主表
    tbl = re.search(r"const VOICE_TABLES = \{(.*?)\n\};", html, re.S)
    assert tbl, "VOICE_TABLES 缺失"
    rows = {}
    for eng in ("local_kokoro", "local_matcha", "local_melo", "cloud"):
        m = re.search(rf"^\s*{eng}:\s*(.+)$", tbl.group(1), re.M)
        assert m, f"{eng} 缺引擎原生音色表（不得复用 Kokoro 表）"
        rows[eng] = m.group(1)
    assert rows["local_kokoro"].startswith("VOICES"), "Kokoro 档未指向主表"
    for eng, sid in (("local_matcha", "0"), ("local_melo", "0")):
        row = rows[eng]
        assert row.count("[") == 2, f"{eng} 表应恰 1 项（该档唯一内置女声）"
        assert f"[{sid}," in row, f"{eng} 表 sid 应为 {sid}"
        assert "VOICES" not in row, f"{eng} 表不得复用 Kokoro 全表"
        assert "zf_" not in row and "zm_" not in row, f"{eng} 表混入 Kokoro 音色名"
    assert rows["cloud"].count("[") == 2, "云档表应恰 1 项（固定回落嗓）"
    assert "KOKORO_DEFAULT_SID," in rows["cloud"], "云档未列固定回落嗓"
    assert "VOICES" not in rows["cloud"], "云档表不得复用 Kokoro 全表"
    assert "KOKORO_DEFAULT_SID = 28" in html, "回落/默认嗓常量与后端 _DEFAULT_SID 脱钩"

    # ③ 渲染按引擎取表与提示；旧一刀切文案必须消失
    assert "VOICE_TABLES[eng]" in html and "VOICE_HINTS[eng]" in html, \
        "渲染未按引擎取表/取提示"
    assert "音色表与自定义音色仅 Kokoro 可用" not in html, "旧一刀切提示词仍在"

    # ④ tts.sid 写权只属 Kokoro 档（切档不提交 → Kokoro 的选择不被静默改写）
    assert 'TTS_SID_OWNER = "local_kokoro"' in html
    fn = re.search(r"function ttsSidPatch\(\)\{(.*?)\n\}", html, re.S)
    assert fn, "ttsSidPatch 缺失（sid 提交未条件化）"
    assert "TTS_SID_OWNER" in fn.group(1), "sid 提交未按引擎档设闸"
    seg = re.search(r"tts:\{ provider:.*?speed:", html, re.S)
    assert seg and "ttsSidPatch()" in seg.group(0), "保存体未走 ttsSidPatch"
    assert "sid:" not in seg.group(0), "保存体仍有无条件 sid 写入"
