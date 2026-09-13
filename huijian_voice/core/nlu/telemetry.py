"""NLU 漏斗指标 + 兜底语料回流（v1.0.62 P0-1 / P0-2）。

Funnel：级联各档（按 reply.source 原值分档）命中数 / 成功率 / 时延，进程内
    观测件——只记录、永不干预路径、永不抛。全时段累计 + 1h 滚动滑窗，
    回答「klar 上线后 fp 掉没掉」「多少句子最终落到拒答」这类以前只能翻日志
    的问题。经 GET /api/telemetry 暴露，Web 面板一屏直读。

Mining：把「本地理解没接住」的句子（固定拒答 / LLM 兜底 / 本地档执行失败）
    追加进持久卷 nlu_mining.jsonl——**仅本机存放、从不外传、不进日志**，
    版本周期人工审阅后分流进字面表 / klar 语料 / TextCNN 训练集。这是把
    corrector「平台窗→平开窗」式的现场人工实锤固化为飞轮。行数超限自动轮转
    （保新丢旧），settings.nlu.mining_enabled 一键关。隐私定案：语料含家庭
    信息，落盘位置与既有 settings.json 同目录（同信任域、跨加载项升级存活），绝不上传。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger("huijian.telemetry")

_WINDOW_S = 3600.0
_WIN_CAP = 20000          # 滑窗样本上限（4 台麦 × 高频话术也够 1h）
_FALLBACK_SOURCES = frozenset({"fallback"})
_LLM_SOURCES = frozenset({"llm"})


class Funnel:
    """按 source 分档的计数器 + 1h 滑窗。线程安全（锁开销纳秒级，可忽略）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by: dict[str, dict] = {}
        self._win: deque = deque()          # (mono_ts, source, ok)
        self.total = 0

    def record(self, source: str, ok: bool, seconds: float) -> None:
        s = str(source or "?")[:24]
        ms = max(0.0, float(seconds)) * 1000.0
        now = time.monotonic()
        with self._lock:
            b = self._by.setdefault(s, {"n": 0, "ok": 0, "ms_sum": 0.0, "ms_max": 0.0})
            b["n"] += 1
            b["ok"] += 1 if ok else 0
            b["ms_sum"] += ms
            b["ms_max"] = max(b["ms_max"], ms)
            self.total += 1
            self._win.append((now, s, bool(ok)))
            cutoff = now - _WINDOW_S
            while self._win and (self._win[0][0] < cutoff or len(self._win) > _WIN_CAP):
                self._win.popleft()

    def snapshot(self) -> dict:
        now = time.monotonic()
        win: dict[str, list] = {}
        with self._lock:
            by = {k: dict(v) for k, v in self._by.items()}
            for _ts, s, ok in self._win:
                win.setdefault(s, []).append(ok)
        out = {}
        for k, v in by.items():
            n = v["n"]
            w = win.get(k, [])
            out[k] = {
                "n": n,
                "ok_pct": round(100.0 * v["ok"] / n, 1) if n else None,
                "avg_ms": round(v["ms_sum"] / n, 1) if n else None,
                "max_ms": round(v["ms_max"], 1) if n else None,
                "win1h": len(w),
                "win1h_ok_pct": round(100.0 * sum(1 for x in w if x) / len(w), 1) if w else None,
            }
        _ = now
        return {"total": self.total, "window_s": int(_WINDOW_S), "by_source": out}


class Mining:
    """兜底语料本地回流（jsonl 追加 + 行数轮转）。永不抛，写坏只限流留痕。"""

    def __init__(self, settings, path: Path) -> None:
        self._settings = settings
        self.path = Path(path)
        self._lock = threading.Lock()
        self._err_ts = 0.0
        self._lines = -1                     # -1=未知，append 前懒统计

    def _enabled(self) -> bool:
        try:
            return bool(self._settings.get("nlu.mining_enabled", True))
        except Exception:
            return True

    def _cap(self) -> int:
        try:
            v = int(self._settings.get("nlu.mining_max_lines", 1500))
            return v if v >= 50 else 1500
        except Exception:
            return 1500

    def should(self, source: str, ok: bool) -> bool:
        """回流判定：固定兜底 / LLM 兜底（=NLU 未覆盖）/ 本地档执行失败。"""
        s = str(source or "")
        return s in _FALLBACK_SOURCES or s in _LLM_SOURCES or (not ok)

    def maybe(self, text: str, source: str, ok: bool) -> None:
        if not self._enabled() or not text or not self.should(source, ok):
            return
        rec = {"ts": int(time.time()), "text": str(text)[:200],
               "source": str(source or "?")[:24], "ok": bool(ok)}
        try:
            with self._lock:
                if self._lines < 0:
                    self._lines = self._count()
                if self._lines >= self._cap():
                    self._rotate()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self._lines += 1
        except OSError as e:
            now = time.monotonic()
            if now - self._err_ts >= 60.0:      # 60s 限流留痕，防刷屏
                self._err_ts = now
                logger.warning("[mining] 回流盘写失败（不影响链路）: %s", str(e)[:120])

    def _count(self) -> int:
        try:
            with open(self.path, "rb") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def _rotate(self) -> None:
        """保最新一半（+空文件容错）。轮转失败不抛，下次再试。"""
        keep = max(1, self._cap() // 2)
        try:
            with open(self.path, "r", encoding="utf8") as f:
                lines = f.readlines()
        except OSError:
            self._lines = 0
            return
        with open(self.path, "w", encoding="utf8") as f:
            f.writelines(lines[-keep:])
        self._lines = min(len(lines), keep)

    def status(self) -> dict:
        try:
            return {"enabled": self._enabled(), "file": self.path.name,
                    "lines": self._count()}
        except Exception:
            return {"enabled": False, "file": self.path.name, "lines": -1}


class Telemetry:
    """Funnel+Mining 合体：pipeline.handle 单点钩子 observe()。"""

    def __init__(self, settings, install_dir: Path) -> None:
        self.funnel = Funnel()
        self.mining = Mining(settings, Path(install_dir) / "nlu_mining.jsonl")

    def observe(self, text: str, source: str, ok: bool, seconds: float) -> None:
        try:
            self.funnel.record(source, ok, seconds)
        except Exception:                        # 观测件永不干扰主链
            pass
        try:
            self.mining.maybe(text, source, ok)
        except Exception:
            pass

    def snapshot(self) -> dict:
        return {"funnel": self.funnel.snapshot(), "mining": self.mining.status()}
