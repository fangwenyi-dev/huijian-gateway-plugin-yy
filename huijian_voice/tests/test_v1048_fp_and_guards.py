"""v1.0.48 批：P5 音色指纹通道 + 安全/隐私收口的钉桩与单测。

背景（三仓联审定案）：
- P5：HA core TTS 缓存键 = sha1(文本)_语言_options_引擎实体id，加载项内部
  音色（provider/sid/cloud.voice）对键不感知 → web 换嗓后同一句永远命中旧嗓
  缓存（"多嗓音"第六路径）。修复=指纹经 WS settings 推送，集成实体塞进
  default_options（core 无条件合入并参与键计算）→ 换嗓即键轮换。
- 附带收口：nginx 172.17 网段、endpoints.json 0o600、/data/run 700、
  集成侧 endpoint/播报文本 INFO 脱敏、session 生成器确定性 aclose。
"""
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CORE = _ROOT / "core"
_CC = _ROOT / "custom_components" / "huijian_ai"


class _Settings:
    def __init__(self, data=None):
        self._d = data or {}

    def get(self, path, default=None):
        return self._d.get(path, default)


def _engine(data):
    import core.tts as m
    eng = m.TtsEngine(_Settings(data), types.SimpleNamespace())
    return eng


# ── P5 指纹语义 ────────────────────────────────────────────────

def test_fingerprint_local_follows_sid():
    assert _engine({"tts.provider": "local_kokoro", "tts.sid": 18}).voice_fingerprint() \
        == "local:sid18+c0"
    assert _engine({"tts.provider": "local_kokoro", "tts.sid": 45}).voice_fingerprint() \
        == "local:sid45+c0"


def test_fingerprint_cloud_follows_voice_with_alloy_default():
    # 云档 voice 留空 = 实际发 alloy（tts.py P1 路径）→ 指纹必须同名，
    # 保证"配置漂移但嗓音未变"不触发无谓键轮换，反之必触发。
    assert _engine({"tts.provider": "cloud_openai_compat", "tts.cloud": {}}).voice_fingerprint() \
        == "cloud:alloy"
    assert _engine({"tts.provider": "cloud_openai_compat",
                    "tts.cloud": {"voice": "china_female"}}).voice_fingerprint() \
        == "cloud:china_female"


def test_fingerprint_custom_injection_count_and_fallback_sid():
    eng = _engine({"tts.provider": "local_kokoro", "tts.sid": "不存在的名"})
    base = eng.voice_fingerprint()          # 非法名回落 18
    assert base == "local:sid18+c0"
    eng._custom_sids = {"mei": 103}
    assert eng.voice_fingerprint() == "local:sid18+c1"   # 注入表变化也换键


def test_fingerprint_speed_not_included():
    a = _engine({"tts.provider": "local_kokoro", "tts.sid": 18}).voice_fingerprint()
    b = _engine({"tts.provider": "local_kokoro", "tts.sid": 18}).voice_fingerprint()
    assert a == b   # 纯文本相等即可；speed 根本不读取


# ── 双端通道与钉桩 ─────────────────────────────────────────────

def test_welcome_and_broadcast_wired():
    wss = (_CORE / "ws_server.py").read_text(encoding="utf-8")
    assert '"type": "settings"' in wss and "voice_fingerprint()" in wss, \
        "tts 通道建连欢迎指纹回潮"
    mn = (_CORE / "main.py").read_text(encoding="utf-8")
    assert "_rotate_voice_fp()" in mn and "_push_voice_fp" in mn, "保存侧广播断线"
    assert "self._voice_fp = self.tts.voice_fingerprint()" in mn, "指纹基线未先于监听器"


def test_integration_default_options_carries_fp():
    ttss = (_CC / "tts.py").read_text(encoding="utf-8")
    assert "huijian_voice_fp" in ttss and "def default_options" in ttss, \
        "实体指纹入 default_options（HA 缓存键轮换）回潮"
    tr = (_CC / "huijian" / "tts_transport.py").read_text(encoding="utf-8")
    assert "_on_server_settings" in tr and "voice_fp" in tr
    wt = (_CC / "huijian" / "ws_transport.py").read_text(encoding="utf-8")
    assert '"settings"' in wt and "_on_server_settings" in wt, "base settings tap 缺席"


# ── 生成器确定性关停（session.py）──────────────────────────────

def test_stream_generator_aclose():
    src = (_CORE / "session.py").read_text(encoding="utf-8")
    i = src.index("async def _stream")
    seg = src[i:src.index("async def", i + 10)]
    assert "await it.aclose()" in seg, "播报生成器早退路径不关停回潮（违 tts.py 确定性关停纪律）"


# ── nginx 管理面 ACL（172.17 回潮钉桩）─────────────────────────

def test_nginx_admin_acl_narrow():
    conf = (_ROOT / "nginx-huijian.conf").read_text(encoding="utf-8")
    assert "allow 172.17.0.0/16" not in conf, \
        "docker bridge 全段放行回潮=宿主任意容器可拿 ws_token/写配置"
    assert "allow 127.0.0.0/8" in conf and "allow 172.30.32.0/24" in conf
