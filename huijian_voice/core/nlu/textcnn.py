"""TextCNN 15 类意图分类器（T1 档）。

资产：加载项目录 nlu_data/（6/8 版，intent.onnx 11KB + intent.onnx.data 1.9MB +
vocab.json + intent.classes.json + intent.thresholds.json），随镜像分发（~2MB，
v3 定案）。预处理硬约束（训练脚本 train_textcnn.py 实证）：
  字符级无分词；vocab.json 字符→id（<PAD>=0 <UNK>=1）；MAX_LEN=30 截断+零填充
  int64；ONNX 输入名 "input"；输出即 softmax 概率。
阈值（收编三修之三）：按类阈值，默认表 OOS=0.85 / SceneTrigger=0.5 / 其余 0.7
（算法=95% 召回最大阈值，train_textcnn.py L121-161）；settings 可覆盖。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("huijian.textcnn")

MAX_LEN = 30


class TextCNN:
    def __init__(self, data_dir: Path, thresholds_override: Optional[dict] = None):
        self.data_dir = Path(data_dir)
        self._lock = threading.Lock()
        self._session = None
        self.classes: list[str] = []
        self.vocab: dict[str, int] = {}
        self.thresholds: dict[str, float] = {}
        self.thresholds_override = thresholds_override or {}
        self.available = False

    def _ensure(self) -> bool:
        if self.available:
            return True
        with self._lock:
            if self.available:
                return True
            onnx = self.data_dir / "intent.onnx"
            cls = self.data_dir / "intent.classes.json"
            voc = self.data_dir / "vocab.json"
            thr = self.data_dir / "intent.thresholds.json"
            if not (onnx.exists() and cls.exists() and voc.exists()):
                logger.warning("[T1] nlu_data 不完整（%s）", self.data_dir)
                return False
            try:
                import onnxruntime as ort
                sess = ort.InferenceSession(str(onnx), providers=["CPUExecutionProvider"])
                self.classes = json.loads(cls.read_text(encoding="utf-8"))
                self.vocab = json.loads(voc.read_text(encoding="utf-8"))
                self.thresholds = json.loads(thr.read_text(encoding="utf-8")) if thr.exists() else {}
                self.thresholds.update({k: float(v) for k, v in (self.thresholds_override or {}).items()})
                self._session = sess
                self.available = True
                logger.warning("[T1] TextCNN 已加载：%d 类", len(self.classes))
                return True
            except Exception as e:
                logger.warning("[T1] 加载失败：%s", e)
                return False

    def set_thresholds_override(self, overrides: dict) -> None:
        self.thresholds_override = dict(overrides or {})
        if self.available:
            base = json.loads((self.data_dir / "intent.thresholds.json").read_text(encoding="utf-8")) \
                if (self.data_dir / "intent.thresholds.json").exists() else {}
            base.update({k: float(v) for k, v in self.thresholds_override.items()})
            self.thresholds = base

    def _threshold(self, label: str) -> float:
        return float(self.thresholds.get(label, 0.7))

    def predict(self, text: str) -> Optional[tuple[str, float]]:
        """返回 (类名, 置信度)；低于按类阈值或 OOS → None（宁漏不误）。

        ⚠ 实测钉桩（2026-09-08，onnxruntime 对本仓 intent.onnx 跑分）：输出层是
        **raw logits**（argmax 值可达 10+），必须先 softmax 再对阈值——阈值表
        （OOS 0.85/SceneTrigger 0.5/其余 0.7）在训练侧按概率标定。
        （原 840 版直接拿 raw 值比 0.7 = 恒放行，属收编缺陷四，此处修复。）
        """
        if not text or not self._ensure():
            return None
        try:
            x = np.zeros((1, MAX_LEN), dtype=np.int64)
            for j, c in enumerate(text[:MAX_LEN]):
                x[0, j] = self.vocab.get(c, 1)
            logits = self._session.run(None, {"input": x})[0][0].astype(np.float64)
            e = np.exp(logits - logits.max())
            probs = e / e.sum()
            idx = int(np.argmax(probs))
            label = self.classes[idx]
            prob = float(probs[idx])
            if label == "OOS" or prob < self._threshold(label):
                return None
            return label, prob
        except Exception as e:
            logger.warning("[T1] 推理异常：%s", e)
            return None
