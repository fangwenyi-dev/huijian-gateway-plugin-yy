"""自定义音色管理端点（v1.0.45，独立小模块——避开 admin_api 热区的并行冲突）。

挂载：admin_api.make_admin_app 内 `tts_voices_api.setup(app, ctx)` 一行。
契约（与 core/tts.py merge_custom_voices 同源，尺寸权威=官方包单音字节数）：
  GET  /api/tts/voices          → 面板：官方音色数/单音尺寸/投递目录/预览/已注入
  POST /api/tts/voices/upload   → ?name=主名，body=原始字节；尺寸硬校验后原子
                                   落盘 const.TTS_VOICES_DIR/<name>.bin。
                                   点「重新加载模型」→ 惰性重载时合并生效。
"""
from __future__ import annotations

import logging
import os
import re

from aiohttp import web

from . import const

logger = logging.getLogger("huijian.admin")

_KEY = "tts_kokoro_multilang"
# 主名：中文/字母/数字/下划线/连字符/点，1-40 字符，不得以点开头（隐藏文件被扫略）
_NAME_RE = re.compile(r"[A-Za-z0-9_\u4e00-\u9fff.\-]{1,40}")


def _voices_status(ctx) -> dict:
    """面板数据。引擎缺席/未加载时给降级视图（目录预览仍有效）。永不抛。"""
    if getattr(ctx, "tts", None) is not None and hasattr(ctx.tts, "voices_status"):
        try:
            return ctx.tts.voices_status()
        except Exception as e:
            logger.warning("[音色] 面板读数失败：%s", e)
    official_n = int(ctx.store.voices_count_for(_KEY) or 0) if getattr(ctx, "store", None) else 0
    return {"official_count": official_n, "per_voice_bytes": 0,
            "dir": str(const.TTS_VOICES_DIR), "injected": {}, "preview": [],
            "degraded": True}


def setup(app, ctx):
    async def _voices(request):
        return web.json_response(_voices_status(ctx))

    async def _upload(request):
        name = str(request.query.get("name", "")).strip()
        if not _NAME_RE.fullmatch(name) or name.startswith("."):
            return web.json_response(
                {"ok": False, "message": "音色主名非法（中文/字母/数字/_-.，1~40 字符，不以点开头）"},
                status=400)
        st = _voices_status(ctx)
        per = int(st.get("per_voice_bytes") or 0)
        if not per:
            return web.json_response(
                {"ok": False, "message": "官方模型未就绪或包缺 voices_count 声明，无法确定单音尺寸"},
                status=503)
        body = await request.read()
        if len(body) != per:
            return web.json_response(
                {"ok": False,
                 "message": f"文件 {len(body)}B ≠ 单音尺寸 {per}B"
                            f"（float32×{per // 4} 风格向量，须与当前 TTS 模型包同布局导出）"},
                status=400)
        try:
            const.TTS_VOICES_DIR.mkdir(parents=True, exist_ok=True)
            out = const.TTS_VOICES_DIR / f"{name}.bin"
            tmp = const.TTS_VOICES_DIR / f".{name}.bin.tmp"
            tmp.write_bytes(body)
            os.replace(tmp, out)
        except OSError as e:
            return web.json_response({"ok": False, "message": f"落盘失败：{e}"}, status=500)
        logger.info("[音色] 上传 %s.bin（%dB），点「重新加载模型」后生效", name, len(body))
        return web.json_response({"ok": True, "note": "已保存；点「重新加载模型」生效",
                                  "voices": _voices_status(ctx)})

    app.router.add_get("/api/tts/voices", _voices)
    app.router.add_post("/api/tts/voices/upload", _upload)
