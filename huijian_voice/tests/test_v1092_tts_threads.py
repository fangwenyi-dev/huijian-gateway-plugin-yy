# -*- coding: utf-8 -*-
"""v1.0.92 本地 Kokoro 推理线程数钉（引擎提速开放项的"可抬不可默认"边界）。

台架实锤（2026-09-17，主机直跑、与现场同 kokoro-multi-lang-v1_1 **fp32** 包，
65 字整句 RTF）：threads2=0.42 │ threads4=0.29 │ threads8=0.23——提速真实存在。
但现场加载项跑在 HAOS 容器、CPU 配额未实证：盲抬默认线程会跟 HA 主进程抢核，
违反「不得拿客户当测试」的交付纪律。⇒ 本批只开**显式覆盖通道**
（HUIJIAN_TTS_THREADS），默认值逐字不变。此约定由以下钉死：
  ① env 未设/空 ⇒ 2（默认行为保持）；
  ② env 合法正整数 ⇒ 原样生效（现场抬线程无需发版）；
  ③ env 垃圾值（abc/0/-1/小数）⇒ 回落 2 且**永不抛**（引擎加载路径不许炸）；
  ④ tts.py 不得回退到 num_threads=2 字面量硬编码（防"顺手改回"）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import tts                                                    # noqa: E402

TTS_PY = ROOT / "core" / "tts.py"


def test_default_is_two(monkeypatch):
    monkeypatch.delenv("HUIJIAN_TTS_THREADS", raising=False)
    assert tts._engine_threads() == 2


def test_empty_env_is_two(monkeypatch):
    monkeypatch.setenv("HUIJIAN_TTS_THREADS", "")
    assert tts._engine_threads() == 2


def test_valid_override(monkeypatch):
    monkeypatch.setenv("HUIJIAN_TTS_THREADS", "4")
    assert tts._engine_threads() == 4
    monkeypatch.setenv("HUIJIAN_TTS_THREADS", "8")
    assert tts._engine_threads() == 8


def test_garbage_never_raises(monkeypatch):
    for junk in ("abc", "0", "-1", "2.5", "  ", "4x"):
        monkeypatch.setenv("HUIJIAN_TTS_THREADS", junk)
        assert tts._engine_threads() == 2, f"垃圾值 {junk!r} 必须回落默认 2，不得抛"


def test_source_no_longer_hardcodes():
    # 硬编码点必须走 _engine_threads()；若有人改回字面量赋值，本钉红。
    src = TTS_PY.read_text(encoding="utf-8")
    assert "model_cfg.num_threads = _engine_threads()" in src
    assert "model_cfg.num_threads = 2" not in src


def test_threads_logged_without_rewriting_existing_marker():
    # threads 走独立新行；v1.0.52 的「耗时 %dms」字面量必须原样在位
    # （现场 grep 口径，受 test_v1052 钉保护）——不得把 threads 塞回旧行。
    src = TTS_PY.read_text(encoding="utf-8")
    assert '"[TTS] Kokoro 引擎就绪，耗时 %dms"' in src, "旧遥测字面量被改写"
    assert "[TTS] Kokoro 推理线程 threads=%d" in src, "缺 threads 独立可观测行"

