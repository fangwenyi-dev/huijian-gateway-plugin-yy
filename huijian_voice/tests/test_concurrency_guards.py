"""并发/生命周期审查修复（F1-F8）守卫钉桩。

对应 2026-09-08 dsh-review-loop 发现的缺陷类：
  F1 卸载/推理互斥（快照+busy）  F2 ModelStore single-flight/.part/幽灵键/symlink
  F3 TextCNN 出循环              F4 abort 位与停机
  F6 状态文件 tmp 唯一化         F7 强引用回执 + SttSession.on_close
"""
import asyncio
import gzip
import io
import json
import tarfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import urllib.request

from core import main as core_main
from core.asr import AsrEngine
from core.model_store import ModelStore
from core.nlu.fast_path import FastPath
from core.session import SttSession
from core.tts import TtsEngine


class DSettings:
    def __init__(self, d=None):
        self.d = d or {}

    def get(self, k, default=None):
        return self.d.get(k, default)


# ── F1: asr ────────────────────────────────────────────────────
class SlowRec:
    """is_ready 第一真时 sleep——构造卸载窗口。"""

    def __init__(self):
        self.first = True

    def create_stream(self):
        class Stream:
            def accept_waveform(self, rate, data):
                pass
            def input_finished(self):
                pass
        return Stream()

    def is_ready(self, s):
        if self.first:
            self.first = False
            return True
        return False

    def decode_stream(self, s):
        time.sleep(0.35)

    def get_result_all(self, s):
        return SimpleNamespace(text="开 灯")

    def reset(self, s):
        pass


def test_asr_unload_defers_to_inflight():
    eng = AsrEngine(DSettings(), SimpleNamespace())
    eng._rec = SlowRec()
    out = {}
    t = threading.Thread(target=lambda: out.update(r=eng._local_transcribe(b"\x00" * 6400)))
    t.start()
    time.sleep(0.08)
    assert eng._busy == 1
    assert eng.unload() is False, "推理在飞不得卸载"
    assert eng._rec is not None
    # 快照语义：即使外部强拆引用，在飞调用以当代对象完成且无异常
    eng._rec = None
    t.join(5)
    assert out["r"] == "开 灯"
    assert eng._busy == 0
    assert eng.unload() is True


# ── F1: tts ────────────────────────────────────────────────────
class SlowTts:
    def generate(self, text, sid=45, speed=1.0):
        time.sleep(0.35)
        return SimpleNamespace(samples=np.zeros(24000, dtype=np.float32), sample_rate=24000)

    num_speakers = 53


def test_tts_unload_defers_to_inflight():
    eng = TtsEngine(DSettings({"tts.sid": 45, "tts.speed": 1.0}), SimpleNamespace())
    eng._tts = SlowTts()
    t = threading.Thread(target=lambda: eng._synth("测试。", 45, 1.0))
    t.start()
    time.sleep(0.08)
    assert eng.unload() is False
    t.join(5)
    assert eng._busy == 0
    assert eng.unload() is True


# ── F2/F4: ModelStore single-flight ────────────────────────────
def _make_tar_bytes(top="pkg"):
    files = {f"{top}/tokens.txt": b"tok", f"{top}/encoder.int8.onnx": b"enc",
             f"{top}/decoder.int8.onnx": b"dec"}
    import hashlib
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tf:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    b = raw.getvalue()
    return b, hashlib.sha256(b).hexdigest()


@pytest.fixture()
def store_env(tmp_path):
    payload, sha = _make_tar_bytes()
    lock = {
        "k1": {"tarball": "pkg.tar.gz", "sha256": sha, "top_dir": "pkg",
               "required_files": ["tokens.txt", "encoder.int8.onnx", "decoder.int8.onnx"],
               "size_mb": 1, "urls": ["http://fake.test/pkg.tar.gz"]},
    }
    lockfile = tmp_path / "models.lock.json"
    lockfile.write_text(json.dumps(lock), encoding="utf-8")
    st = ModelStore(DSettings(), lock_path=lockfile,
                    models_dir=tmp_path / "models", status_file=tmp_path / "ms.json")
    return st, payload


def test_singleflight_and_ghost_keys(store_env, monkeypatch):
    st, payload = store_env
    calls = []
    barrier = threading.Barrier(2, timeout=5)   # 双线程同时冲进 ensure 才算并发验证

    class FakeResp:
        def __init__(self):
            self.it = iter([payload[:400], payload[400:], b""])

        def read(self, n):
            return next(self.it)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        time.sleep(0.3)         # 拉开窗口让第二个线程必然撞上 key 锁
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    res = {}

    def worker(tag):
        barrier.wait()
        res[tag] = st.ensure("k1")

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start(); t2.start(); t1.join(10); t2.join(10)
    assert res == {"a": True, "b": True}
    assert len(calls) == 1, "single-flight：同一 key 只许一次真实下载"
    snap = st.snapshot()
    assert set(snap) == {"k1"}, f"状态表不得混入幽灵键: {set(snap)}"
    assert (st.models_dir / "k1" / "pkg" / "tokens.txt").exists()


def test_abort_stops_download(store_env, monkeypatch):
    st, payload = store_env
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("abort 后不应发起下载"))
    st.abort.set()
    assert st.ensure("k1") is False


def test_symlink_member_rejected(tmp_path, monkeypatch):
    lock = {"k1": {"tarball": "pkg.tar.gz", "sha256": "", "top_dir": "pkg",
                   "required_files": ["tokens.txt"], "urls": []}}
    lockfile = tmp_path / "models.lock.json"
    lockfile.write_text(json.dumps(lock), encoding="utf-8")
    st = ModelStore(DSettings(), lock_path=lockfile,
                    models_dir=tmp_path / "models", status_file=tmp_path / "ms.json")
    # 恶意包：正常成员 + 指向 /etc/passwd 的 symlink
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"tok"
        ti = tarfile.TarInfo("pkg/tokens.txt"); ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
        li = tarfile.TarInfo("pkg/evil"); li.type = tarfile.SYMTYPE; li.linkname = "/etc/passwd"
        tf.addfile(li)
    st.import_dir.mkdir(parents=True, exist_ok=True)
    (st.import_dir / "pkg.tar.gz").write_bytes(buf.getvalue())
    assert st.ensure("k1") is True
    assert not (st.models_dir / "k1" / "pkg" / "evil").exists(), "symlink 成员必须被拒绝"
    assert (st.models_dir / "k1" / "pkg" / "tokens.txt").exists()


# ── F6: _atomic_write 并发唯一 tmp ─────────────────────────────
def test_atomic_write_concurrent(tmp_path):
    target = tmp_path / "status.json"
    payload = json.dumps({"big": "x" * 4000, "seq": 0})
    errors = []

    import sys
    def writer():
        for i in range(40):
            try:
                core_main._atomic_write(target, payload)
            except OSError:
                if sys.platform != "win32":
                    raise        # Linux 生产平台 replace 不该失败；Windows 允许偶发

    def reader():
        for _ in range(300):
            try:
                if target.exists():
                    json.loads(target.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                errors.append(e)     # 半写/截断内容才算违规
            except OSError:
                pass                 # Windows 上 mkstemp 瞬时拒读是平台行为，Linux 生产无此窗口
            time.sleep(0.001)

    ts = [threading.Thread(target=writer) for _ in range(4)] + [threading.Thread(target=reader)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert not errors, f"读到半写文件: {errors[:2]}"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "status.json"]
    assert not leftovers, f"tmp 残留: {leftovers}"


# ── F3: TextCNN 出事件循环 ─────────────────────────────────────
class OffloadProbe:
    available = True

    def __init__(self):
        self.thread_name = None

    def predict(self, text):
        self.thread_name = threading.current_thread().name
        return None


class FakeScenes:
    triggers = set()

    async def refresh(self, force=False):
        pass

    def check(self, text):
        return None

    async def verify_or_refresh(self, phrase):
        return False


def test_textcnn_predict_off_loop():
    probe = OffloadProbe()
    fp = FastPath(FakeScenes(), probe, DSettings({"nlu.textcnn_enabled": True}))

    async def go():
        return await fp.match("咕噜吧啦巴巴罗")

    asyncio.run(go())
    assert probe.thread_name is not None, "T1 未被调用（级联顺序变了？）"
    assert probe.thread_name != "MainThread", "TextCNN 推理仍在事件循环上跑"


# ── F7: 回执强引用 + SttSession.on_close ───────────────────────
class FakeWs:
    def __init__(self):
        self.closed = False
        self.sent = []

    async def send_str(self, s):
        self.sent.append(s)


def test_pong_tasks_strong_ref():
    async def go():
        sess = SttSession(FakeWs(), SimpleNamespace())
        assert sess._common({"type": "ping"})
        assert sess._common({"type": "hello"})
        assert len(sess._pending) == 2, "回执 task 必须持强引用"
        await asyncio.gather(*list(sess._pending))
        await asyncio.sleep(0)     # done-callback 清袋
        assert sess._pending == set()
        ws = sess.ws
        assert any('"pong"' in m for m in ws.sent)
    asyncio.run(go())


def test_stt_on_close_cancels_inflight():
    async def go():
        sess = SttSession(FakeWs(), SimpleNamespace())

        async def slow():
            await asyncio.sleep(30)

        sess._task = asyncio.create_task(slow())
        await asyncio.sleep(0.01)
        await sess.on_close()
        try:
            await sess._task
        except asyncio.CancelledError:
            pass
        assert sess._task.cancelled()
        sess.close_work()          # 幂等：已取消再调无事
    asyncio.run(go())
