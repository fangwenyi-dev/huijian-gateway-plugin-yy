"""自定义音色机制回归（merge/resolve/面板/上传，纯文件系统级，不依赖 sherpa）。

尺寸契约实证（2026-09-16 本机）：现役 v1_1 fp32 官方包 voices.bin =
53,790,720B ÷ 103 = 522,240B/音（510×256×float32），无 magic 纯拼接——
故「官方+自定义合并 = 尾部追加定长块」对任意布局的包都成立（尺寸运行时推）。
"""
import asyncio
import json

import pytest
from aiohttp import web, ClientSession

from core import const
from core.tts import TtsEngine, merge_custom_voices
from core import tts_voices_api


class _Settings:
    def __init__(self, d=None):
        self._d = d or {}

    def get(self, dotted, default=None):
        return self._d.get(dotted, default)


class _Store:
    def __init__(self, model_dir=None, count=3):
        self._dir = model_dir
        self._count = count

    def voices_count_for(self, key):
        return self._count

    def model_dir_for(self, key):
        return self._dir


def _engine(tmp, settings=None, count=3, n_spk=None):
    official = tmp / "voices.bin"
    if not official.exists():
        official.write_bytes(bytes(count * 64))       # 单音尺寸 64B
    eng = TtsEngine(_Settings(settings), _Store(tmp, count))
    eng._custom_sids = {}
    if n_spk is not None:
        from types import SimpleNamespace
        eng._tts = SimpleNamespace(num_speakers=n_spk)
    return eng


# ── merge_custom_voices ────────────────────────────────────────
def test_merge_appends_valid_and_skips_bad(tmp_path):
    eng_dir = tmp_path / "m"
    eng_dir.mkdir()
    official = eng_dir / "voices.bin"
    official.write_bytes(bytes(3 * 64))
    cdir = tmp_path / "cv"
    cdir.mkdir()
    (cdir / "老婆A.bin").write_bytes(bytes(64))
    (cdir / "坏货.bin").write_bytes(bytes(10))
    (cdir / ".隐藏.bin").write_bytes(bytes(64))
    out, names, skipped = merge_custom_voices(official, cdir, eng_dir / "merged.bin", 3)
    assert out == eng_dir / "merged.bin"
    assert names == {"老婆a": 3}                      # 主名小写、sid 从官方数起
    assert any("坏货" in s for s in skipped)
    assert (eng_dir / "merged.bin").stat().st_size == 4 * 64
    # 指纹复用：再调不重写盘
    mt0 = (eng_dir / "merged.bin").stat().st_mtime_ns
    out2, names2, _ = merge_custom_voices(official, cdir, eng_dir / "merged.bin", 3)
    assert (eng_dir / "merged.bin").stat().st_mtime_ns == mt0
    # 投递变更 → 重建
    (cdir / "z老b.bin").write_bytes(bytes(64))
    _, names3, _ = merge_custom_voices(official, cdir, eng_dir / "merged.bin", 3)
    assert names3 == {"z老b": 3, "老婆a": 4}      # 名字典序（ASCII 先于汉字）


def test_merge_no_custom_passthrough(tmp_path):
    official = tmp_path / "voices.bin"
    official.write_bytes(bytes(192))
    out, names, skipped = merge_custom_voices(official, tmp_path / "none",
                                              tmp_path / "merged.bin", 3)
    assert out == official and names == {} and skipped == []


def test_merge_unknown_count_disabled(tmp_path):
    official = tmp_path / "voices.bin"
    official.write_bytes(bytes(192))
    cdir = tmp_path / "cv"
    cdir.mkdir()
    (cdir / "x.bin").write_bytes(bytes(64))
    out, names, _ = merge_custom_voices(official, cdir, tmp_path / "merged.bin", 0)
    assert out == official and names == {}             # voices_count 缺失=机制关闭


# ── resolve_sid ────────────────────────────────────────────────
def test_resolve_sid_int_name_and_guards(tmp_path):
    eng = _engine(tmp_path, {"tts.sid": "18"}, n_spk=20)
    assert eng.resolve_sid() == 18
    eng._custom_sids = {"老婆": 5}
    eng.settings = _Settings({"tts.sid": "5"})
    assert eng.resolve_sid() == 5
    eng.settings = _Settings({"tts.sid": "老婆"})
    assert eng.resolve_sid() == 5
    eng.settings = _Settings({"tts.sid": "不存在"})
    assert eng.resolve_sid() == 18                     # 名字查不到 → 回落默认
    eng.settings = _Settings({"tts.sid": "99"})
    assert eng.resolve_sid() == 18                     # 越界 → 回落默认
    eng.settings = _Settings({})
    assert eng.resolve_sid() == 18                     # 未配置 → 默认


# ── voices_status / HTTP ───────────────────────────────────────
def _ctx(eng, store):
    from types import SimpleNamespace
    return SimpleNamespace(tts=eng, asr=None, store=store, settings=_Settings())


def test_voices_status_preview(tmp_path, monkeypatch):
    cdir = tmp_path / "cv"
    cdir.mkdir()
    (cdir / "好.bin").write_bytes(bytes(64))
    (cdir / "坏.bin").write_bytes(bytes(7))
    monkeypatch.setattr(const, "TTS_VOICES_DIR", cdir)
    eng = _engine(tmp_path)
    st = eng.voices_status()
    assert st["official_count"] == 3 and st["per_voice_bytes"] == 64
    by = {p["name"]: p for p in st["preview"]}
    assert by["好"]["valid"] and by["好"]["sid"] == 3
    assert not by["坏"]["valid"] and by["坏"]["sid"] is None


def _http(ctx, call):
    async def go():
        app = web.Application()
        tts_voices_api.setup(app, ctx)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with ClientSession() as s:
                return await call(s, f"http://127.0.0.1:{port}")
        finally:
            await runner.cleanup()
    return asyncio.run(go())


def test_upload_http_paths(tmp_path, monkeypatch):
    cdir = tmp_path / "cv"
    cdir.mkdir()
    monkeypatch.setattr(const, "TTS_VOICES_DIR", cdir)
    eng = _engine(tmp_path)
    ctx = _ctx(eng, eng.store)

    async def call(s, base):
        r1 = await s.post(base + "/api/tts/voices/upload?name=老婆", data=bytes(64))
        j1 = await r1.json()
        r2 = await s.post(base + "/api/tts/voices/upload?name=坏", data=bytes(10))
        r3 = await s.post(base + "/api/tts/voices/upload?name=../越权", data=bytes(64))
        r4 = await s.get(base + "/api/tts/voices")
        return j1, r2.status, r3.status, await r4.json()
    j1, s2, s3, panel = _http(ctx, call)
    assert j1["ok"] and (cdir / "老婆.bin").stat().st_size == 64
    assert s2 == 400                                   # 尺寸不符拒收
    assert s3 == 400                                   # 路径穿越名拒收
    assert panel["preview"][0]["name"] == "老婆" and panel["preview"][0]["valid"]


def test_store_without_voices_count_never_kills_load(tmp_path):
    """台架回归实锤：旧 fake store 无 voices_count_for 曾把 ensure_loaded 打死
    （num_speakers 校验行在合并 try 之外）。_voices_count 必须异常安全归 0。"""
    eng = TtsEngine(_Settings(), object())          # store 无任何方法
    assert eng._voices_count("tts_kokoro_multilang") == 0
    st = eng.voices_status()                        # 面板同样不得抛
    assert st["official_count"] == 0 and st["preview"] == []
