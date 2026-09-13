"""统一归一入口（v1.0.62 P1-7）：级联各档吃同一份文本。

现状纠偏：corrector 61+extra 与礼貌语剥离此前只在 fast_path.match 内生效，
query 族 / TextCNN / LLM 兜底 / 确认环拿的都是**未纠错原句**——同一句
「开床器电量多少」fp 认得（纠错后）、query 不认得（原句），档位间视野
不一致就是漂移源。本模块把「纠错 → 礼貌语剥离」定为一个幂等入口，
_pipeline._cascade 顶部执行一次_，全链统一起点。

幂等纪律：corrector 表无自映射键（替换后不再命中）、normalize_polite 剥
前后缀二次无害——fp.match 内部保留原调用作纵深（被单测直接裸调用时行为
不变），重复执行不改变结果。klar 侧 fix_zh_pinyin 是**引擎请求方言层**
（发往 klar 的 speech 预处理），语义正交，保留不动。
"""
from __future__ import annotations


def canonical(text: str, settings=None) -> str:
    """ASR 原句 → 全链统一形态。永不抛（内部表坏时退回原文）。"""
    raw = (text or "").strip()
    if not raw:
        return raw
    try:
        from . import corrector
        from .fast_path import normalize_polite
        extra = None
        if settings is not None:
            try:
                extra = settings.get("nlu.corrections_extra") or None
            except Exception:
                extra = None
        return normalize_polite(corrector.apply(raw, extra or {}))
    except Exception:
        return raw
