"""settings 管理器钉桩（首启自动生成、脱敏、原子写、热回调、options 覆盖层）。"""
import json
import os


def test_first_boot_creates_and_persists(settings):
    assert settings.path.exists()
    tok = settings.get("security.ws_token")
    assert tok and len(tok) >= 20
    raw = json.loads(settings.path.read_text(encoding="utf-8"))
    assert raw["security"]["ws_token"] == tok          # token 落盘持久（重启不漂移）


def test_defaults_match_decisions(settings):
    assert settings.get("stt.provider") == "local_paraformer"
    assert settings.get("tts.provider") == "local_kokoro"
    assert settings.get("tts.sid") == 45               # 定案默认音色：小北
    assert settings.get("llm.enabled") is False        # LLM 默认关
    assert settings.get("power.unload_when_idle_min") == 0


def test_masked_hides_secrets(settings):
    settings.update({"llm": {"api_key": "sk-supersecret"}})
    m = settings.masked()
    assert "supersecret" not in json.dumps(m)
    assert m["llm"]["api_key"] == "****"
    assert "…" in m["security"]["ws_token"] or m["security"]["ws_token"].count("…") == 1


def test_masked_writeback_noop(settings):
    settings.update({"llm": {"api_key": "sk-real"}})
    settings.update({"llm": {"api_key": "****"}})      # UI 回显脱敏值不得覆盖真钥
    assert settings.get("llm.api_key") == "sk-real"


def test_corrupt_file_rebuilt(settings):
    settings.path.write_text("{ broken", encoding="utf-8")
    settings.load_or_create()
    assert settings.get("tts.sid") == 45
    assert settings.path.with_suffix(".json.bak").exists()


def test_listener_fires(settings):
    hits = []
    settings.add_listener(lambda d: hits.append(d.get("tts", {}).get("sid")))
    settings.update({"tts": {"sid": 47}})
    assert hits == [47]


def test_env_override_options(monkeypatch, tmp_path):
    monkeypatch.setenv("HUIJIAN_OPT_IDLE_UNLOAD_MIN", "15")
    from core.settings import Settings
    s = Settings(tmp_path / "s.json")
    assert s.get("power.unload_when_idle_min") == 15


def test_endpoint_urls_shape(settings):
    u = settings.endpoint_urls("192.168.1.9")
    for ch in ("stt", "tts", "llm"):
        assert u[ch].startswith(f"ws://192.168.1.9:8000/xiaozhi/v1/{ch}?token=")
