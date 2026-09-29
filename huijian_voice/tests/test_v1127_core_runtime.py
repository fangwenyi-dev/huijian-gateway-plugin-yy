"""v1.1.27 core 运行期修复批钉桩（7 项，全部真行为：真类直调 / 真替身注入）。

项1 core/session.py —— STT 分因链：
    · 静音轮/`_dec_err` 轮此前从进程级单槽 asr.last_reason 取分因（AsrEngine 全进程
      唯一，main.py:77/91）⇒ 沿用上一轮/另一台卫星的陈旧值；libopus 缺失路径既无
      reason 也无兜底，与"用户没说话"逐字节同形。
    修：分因按**本轮**生成（静音轮不读任何槽；引擎侧新增 per-round 通道）。
项2 core/asr.py —— unload() 只查 _busy 不查 _loading（另两处 :166/:191 都查）
    ⇒ 换绑/冷载并发时"卸载空转 + 面板谎报已卸载"（admin_api 回"STT 已卸载"，
    随后 _load_one 又写回）。
项3 core/tts.py —— 句级缓存键与 voice_fingerprint 取配置档 _provider()
    ⇒ 换绑让位窗口内旧引擎以新档键落盘/命中缓存（跨引擎投毒）；指纹进 HA
    无 TTL 盘缓存 ⇒ 换档后永久错嗓。
项4 core/model_store.py —— imp.unlink() 在 sha 校验之前 ⇒ 附属文件"盘上坏字节
    + import/ 放了好包"时把救急包销毁、只能走网络。
项5 core/settings.py —— masked() 漏 klar.token（真凭据，nlu/klar_client.py 发
    x-klar-token）⇒ GET /api/settings 原文回显浏览器。
项6 core/audio.py —— 单帧解码异常静默 b""（仅 debug、无计数）⇒ 整帧语音丢失/
    整轮空在生产不可见。
项7 core/mdns.py —— update_props 全仓零调用且实现必失效（zeroconf 的
    _info.properties 只读 + 裸 except 吞），删除。
"""
import asyncio
import hashlib
import json
import time
import types
from pathlib import Path

import pytest

from core import audio, mdns
from core.asr import AsrEngine
from core.session import SttSession
from core.tts import TtsEngine

_ROOT = Path(__file__).resolve().parents[1]


# ══ 公共替身 ═══════════════════════════════════════════════════════
class _S:
    def __init__(self, d=None):
        self.d = dict(d or {})

    def get(self, k, dv=None):
        return self.d.get(k, dv)


class _AsrStore:
    """真 AsrEngine 的离线 store：两档目录都缺（快败 + 具名分因路径）。"""

    def __init__(self):
        self.async_calls = []

    def model_dir_for(self, key):
        return None

    def ensure(self, key):
        return False

    def ensure_async(self, key, force=False):
        self.async_calls.append(key)


class _Ws:
    def __init__(self):
        self.closed = False
        self.texts = []
        self.bins = []

    async def send_str(self, s):
        self.texts.append(s)

    async def send_bytes(self, b):
        self.bins.append(b)


class _FixedDecoder:
    """协议测试同款：定长假 PCM（解码成功路径）。"""

    def decode(self, packet):
        return b"\x01\x02" * 960


class _BoomDecoder:
    """解码必炸的替身（帧损坏形态）。"""

    def decode(self, packet):
        raise RuntimeError("corrupt frame")


def _boom_factory():
    raise audio.OpusError("libopus 缺失（替身）")


def _ctx(asr, factory=_FixedDecoder):
    ctx = types.SimpleNamespace(asr=asr)
    ctx.decoder_factory = factory
    return ctx


async def _drive(ctx, frames=()):
    ws = _Ws()
    sess = SttSession(ws, ctx)
    await sess.on_text(json.dumps({"type": "listen", "state": "start"}))
    for f in frames:
        await sess.on_binary(f)
    await sess.on_text(json.dumps({"type": "listen", "state": "stop"}))
    end = time.monotonic() + 5.0
    while not ws.texts and time.monotonic() < end:
        await asyncio.sleep(0.02)
    assert ws.texts, "未收到 stt 回执"
    return json.loads(ws.texts[-1])


def _round(ctx, frames=()):
    return asyncio.run(_drive(ctx, frames))


PCM_FRAMES = [b"\xfe\xfe" * 40, b"\xfe\xfe" * 40, b"\xfe\xfe" * 40]


# ══ 项1 STT 分因链 ═════════════════════════════════════════════════
def test_silent_round_never_inherits_stale_engine_reason():
    """真 AsrEngine（全进程单例形态）：连接 A 说话 → 本轮快败写下分因；
    连接 B 真静音 → 回执不得吃 A 留下的陈旧分因（与既有钉
    test_stt_silence_replies_empty 同向，但那钉的槽一开始就是空的）。"""
    eng = AsrEngine(_S(), _AsrStore())
    ctx = _ctx(eng)
    a = _round(ctx, PCM_FRAMES)
    assert a["text"] == "" and "模型资产缺失" in a.get("reason", ""), a
    assert eng.last_reason, "前置：引擎全局槽确已被上一轮写脏"
    b = _round(ctx, [])                       # 另一台卫星：真静音
    assert b["text"] == "" and "reason" not in b, \
        f"静音轮吃了陈旧分因（跨连接污染）：{b}"


class _PerRoundAsr:
    """真 AsrEngine v1.1.27 契约替身：last_reason 是**全局槽**（可被他轮覆盖），
    transcribe_pcm_with_reason 才交**本轮**具名分因。"""

    def __init__(self):
        self.last_reason = "全局槽·他轮残值"
        self.calls = []

    async def transcribe_pcm(self, pcm):
        self.calls.append(len(pcm))
        return ""

    async def transcribe_pcm_with_reason(self, pcm):
        self.calls.append(len(pcm))
        return "", "本轮具名分因"


def test_empty_round_uses_per_round_reason_not_global_slot():
    """有上行帧（真调了引擎）但结果为空：分因必须来自本轮通道，
    不得读进程级单槽（他轮/他卫星的残值）。"""
    asr = _PerRoundAsr()
    msg = _round(_ctx(asr), PCM_FRAMES)
    assert asr.calls, "识别未被调用 ⇒ 本钉判的路径未走到"
    assert msg["text"] == ""
    assert msg.get("reason") == "本轮具名分因", msg


def test_libopus_missing_round_reports_named_reason():
    """libopus 缺失（_dec_err）路径：此前既无 reason 也无兜底，与真静音同形。"""
    eng = AsrEngine(_S(), _AsrStore())
    msg = _round(_ctx(eng, _boom_factory), PCM_FRAMES)
    assert msg["text"] == ""
    reason = msg.get("reason", "")
    assert "opus" in reason.lower() and "解码器" in reason, \
        f"缺 libopus 的空结果必须给具名因：{msg}"


def test_decode_failed_frames_carried_into_round_reason():
    """整帧解码全失败的轮：契约仍是 text:""，但必须说清"音频没解出来"
    （此前静默丢帧 ⇒ 整轮空与"用户没说话"同形，生产不可见）。"""
    eng = AsrEngine(_S(), _AsrStore())
    msg = _round(_ctx(eng, _BoomDecoder), PCM_FRAMES)
    assert msg["text"] == ""
    assert "解码失败 3 帧" in msg.get("reason", ""), msg


# ══ 项2 asr.unload 的 _loading 护栏 ═══════════════════════════════
def test_asr_unload_defers_while_loading():
    eng = AsrEngine(_S(), _AsrStore())
    eng._rec = object()
    eng._loading = True                        # 冷载/换绑在飞（_load_one 即将写回）
    assert eng.unload() is False, "加载在飞不得卸载（会空转 + 面板谎报已卸载）"
    assert eng._rec is not None
    eng._loading = False
    assert eng.unload() is True
    assert eng._rec is None


# ══ 同类孪生：tts.unload 同形缺 _loading 护栏（按"同类全类修"补齐）══
def test_tts_unload_defers_while_loading():
    eng = _tts()
    eng._loading = True          # 冷载/换绑在飞（_ensure_loaded_inner 即将写回 _tts）
    assert eng.unload() is False, "加载在飞不得卸载（会空转；面板会谎报已卸载）"
    assert eng._tts is not None
    eng._loading = False
    assert eng.unload() is True
    assert eng._tts is None


# ══ 项3 TTS 缓存键/指纹取在载引擎 ═════════════════════════════════
_TTS_CFG = {"tts.provider": "local_melo", "tts.sid": 0, "tts.speed": 1.0,
            "tts.cache_enabled": True}


class _TtsStore:
    def lock_entry(self, key):
        return {}

    def model_dir_for(self, key):
        return None

    def ensure(self, key):
        return False

    def voices_count_for(self, key):
        return 0


def _tts(loaded="local_kokoro", **cfg):
    eng = TtsEngine(_S({**_TTS_CFG, **cfg}), _TtsStore())
    eng._tts = types.SimpleNamespace(num_speakers=103)     # 在载=kokoro（换绑让位窗口）
    eng._loaded_prov = loaded
    return eng


def _collect(eng, text):
    out, eo = [], {}

    async def go():
        async for pkt in eng.stream_opus(text, engine_out=eo):
            out.append(pkt)
    asyncio.run(go())
    return out, eo


def test_sentence_cache_key_follows_loaded_engine():
    """换绑让位窗口：配置档=melo、在载档=kokoro。配置档键下预热的音频不得被
    在载档（kokoro）复用；新落键必须带在载档身份（跨引擎投毒闸）。"""
    eng = _tts("local_kokoro")
    synth = []
    eng._synth = lambda sent, sid, speed: (synth.append(sid), b"\x00\x00" * 480)[1]
    eng._encode = lambda pcm: [b"PKT"]
    eng._cache[("local_melo", "开灯了。", 0, 1.0)] = ([b"STALE"], 5)   # 配置档预热
    out, eo = _collect(eng, "开灯了。")
    assert out == [b"PKT"] and synth == [0], \
        f"在载档(旧引擎)命中了配置档键缓存：out={out} synth={synth}"
    assert any(k[0] == "local_kokoro" for k in eng._cache), \
        f"落键未用在载档身份：{list(eng._cache)}"


def test_fingerprint_follows_loaded_engine_not_config():
    """指纹进 HA 无 TTL 盘缓存：窗口内实际产出仍是旧引擎 ⇒ 指纹必须说旧嗓，
    否则新指纹下的旧嗓音频永久驻留（换档后"永久错嗓"）。"""
    eng = _tts("local_kokoro")
    fp = eng.voice_fingerprint()
    assert fp.startswith("local:") and not fp.startswith("local_melo:"), \
        f"窗口内指纹取了配置档（口是心非）：{fp}"
    eng._loaded_prov = "local_melo"            # 换绑完成
    assert eng.voice_fingerprint().startswith("local_melo:"), \
        "换绑完成后指纹必须跟着在载档轮换"


def test_fingerprint_cloud_branch_stays_config_driven():
    """反向钉：云档判定仍按配置（云优先，本地只是回落）——本次修复不得把
    云档指纹也拖去跟本地在载引擎漂。"""
    eng = _tts("local_kokoro", **{"tts.provider": "cloud_openai_compat",
                                  "tts.cloud": {"voice": "anna"}})
    assert eng.voice_fingerprint().startswith("cloud:anna:"), eng.voice_fingerprint()


# ══ 项4 model_store 附属文件救急包 ════════════════════════════════
_GOOD = b"GOOD-VOCOS-BYTES"


def _extra_store(tmp_path, dest_bytes, imp_bytes):
    from core import model_store as ms
    entry = {"tarball": "m.tar.bz2", "sha256": "deadbeef", "top_dir": "mtop",
             "required_files": ["model.onnx", "v.onnx"],
             "extra_files": [{"file": "v.onnx",
                              "sha256": hashlib.sha256(_GOOD).hexdigest(),
                              "urls": ["https://example.invalid/v.onnx"]}]}
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"mk": entry}), encoding="utf-8")
    store = ms.ModelStore(_S({"power.auto_download": True}), lock_path=lock,
                          models_dir=tmp_path / "models",
                          status_file=tmp_path / "status.json")
    keydir = tmp_path / "models" / "mk" / "mtop"
    keydir.mkdir(parents=True)
    (keydir / "model.onnx").write_bytes(b"1")
    (tmp_path / "models" / "mk" / ".extracted_ok").write_text(
        "m.tar.bz2|deadbeef", encoding="utf-8")
    dest = keydir / "v.onnx"
    if dest_bytes is not None:
        dest.write_bytes(dest_bytes)
    imp = tmp_path / "models" / "import" / "v.onnx"
    imp.parent.mkdir(parents=True, exist_ok=True)
    if imp_bytes is not None:
        imp.write_bytes(imp_bytes)
    return store, entry, dest, imp


def test_extra_files_corrupt_dest_keeps_import_rescue(tmp_path, monkeypatch):
    """附属文件盘上坏字节 + import/ 放了好包：救急包必须先于网络被用掉
    （旧序先 unlink 导入口 ⇒ 好包被销毁、坏 dest 也删了、只能跨境重下）。"""
    store, entry, dest, imp = _extra_store(tmp_path, b"BAD-BYTES", _GOOD)
    calls = []
    monkeypatch.setattr(store, "_download_any",
                        lambda e, d: (calls.append(1), False)[1])
    assert store._ensure_extra_files("mk", entry) is True
    assert dest.read_bytes() == _GOOD, "坏 dest 未用 import/ 好包恢复"
    assert calls == [], "有救急包时必须零网络请求"


def test_extra_files_good_dest_still_clears_import_copy(tmp_path):
    """既有语义不回退：盘上文件已就位（校验通过）时，导入口残留照旧清掉
    （防同名旧包反复触发重下）。"""
    store, entry, dest, imp = _extra_store(tmp_path, _GOOD, _GOOD)
    assert store._ensure_extra_files("mk", entry) is True
    assert not imp.exists(), "已就位文件的导入口残留必须清掉"


# ══ 项5 settings.masked 脱敏 klar.token ═══════════════════════════
def test_masked_hides_klar_token(settings):
    """klar.token 是真凭据（nlu/klar_client.py 以 x-klar-token 上行远端引擎），
    GET /api/settings 不得原文回显——本仓纪律：只留长度+首字节。"""
    settings.update({"klar": {"token": "klar-secret-9876"}})
    m = settings.masked()
    assert "klar-secret-9876" not in json.dumps(m, ensure_ascii=False)
    assert m["klar"]["token"] == "k…(16位)", m["klar"]["token"]


def test_masked_klar_token_writeback_noop(settings):
    """脱敏值原样回写不得覆盖真凭据（api_key/security token 的既有同款纪律）。"""
    settings.update({"klar": {"token": "klar-secret-9876"}})
    settings.update(settings.masked())
    assert settings.get("klar.token") == "klar-secret-9876", "脱敏串被写回成真 token"


# ══ 项6 audio 解码失败计数 ═════════════════════════════════════════
def test_opus_decoder_counts_failed_frames():
    try:
        dec = audio.OpusPcmDecoder()
    except audio.OpusError:
        pytest.skip("libopus 不可用（环境性）")
    enc = audio.OpusPcmEncoder("voip")
    good = next(iter(enc.encode_stream(b"\x00\x00" * 960)))
    assert dec.decode(good), "好帧必须解出 PCM"
    assert dec.failed_frames == 0, "好帧不得计数"
    # 坏帧形态（真库抛异常）：镜像替身，行为与真 libopus 一致（解码失败）
    dec._d = types.SimpleNamespace(
        decode=lambda p, n: (_ for _ in ()).throw(RuntimeError("bad opus")))
    assert dec.decode(b"\xc0\xff\xff\xff") == b""
    assert dec.failed_frames == 1, "坏帧必须计数（生产可见）"


# ══ 项7 mdns 死代码删除 ═══════════════════════════════════════════
def test_mdns_update_props_dead_code_removed():
    assert not hasattr(mdns.Publisher, "update_props"), \
        "update_props 全仓零调用且实现必失效（_info.properties 只读被裸 except 吞）"
    assert hasattr(mdns.Publisher, "start") and hasattr(mdns.Publisher, "close")
    me = Path(__file__).resolve()
    hits = []
    for p in _ROOT.rglob("*.py"):
        if "__pycache__" in p.parts or p.resolve() == me:
            continue
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "update_props" in ln and not ln.lstrip().startswith("#"):
                hits.append(f"{p.relative_to(_ROOT)}:{ln.strip()[:60]}")
    assert hits == [], f"仍有代码引用：{hits}"
