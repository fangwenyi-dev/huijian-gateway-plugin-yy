"""管理 API（127.0.0.1:8002；nginx 以 /api/local/ 反代，仅经 HA ingress 可达）。

能力对齐 v2 §5 Web UI 五分区：状态/设置/模型/调试(理解级联 dry-run)/配对(endpoints)。
注意：/api/nlu/test 只跑级联不执行（调试面板绝不能真开灯）。
"""
from __future__ import annotations

import logging
import time

from aiohttp import web

from . import const

logger = logging.getLogger("huijian.admin")

try:
    CTX_KEY: web.AppKey = web.AppKey("admin_ctx", object)
except AttributeError:
    CTX_KEY = "admin_ctx"


def make_admin_app(ctx) -> web.Application:
    app = web.Application()
    app[CTX_KEY] = ctx
    app.router.add_get("/api/health", _health)
    app.router.add_get("/api/settings", _get_settings)
    app.router.add_post("/api/settings", _post_settings)
    app.router.add_get("/api/models", _models)
    app.router.add_post("/api/models/download", _model_download)
    app.router.add_get("/api/endpoints", _endpoints)
    app.router.add_post("/api/token/regenerate", _regen_token)
    app.router.add_post("/api/nlu/test", _nlu_test)
    app.router.add_post("/api/tts/test", _tts_test)
    app.router.add_get("/api/scenes", _scenes)
    app.router.add_get("/api/automations", _automations)
    app.router.add_post("/api/system/reload_models", _reload_models)
    return app


async def _health(request):
    ctx = request.app[CTX_KEY]
    s = ctx.settings
    return web.json_response({
        "ok": True,
        "version": _addon_version(),
        "uptime_s": int(time.time() - ctx.started_at),
        "ha_bridge": bool(ctx.ha and ctx.ha.reachable),
        "sessions": len(ctx.sessions),
        "models_ready": {k: ctx.store.is_ready(k) for k in ctx.store.keys()},
        "asr_loaded": bool(ctx.asr.ready()) if ctx.asr else False,
        "tts_loaded": bool(ctx.tts.ready()) if ctx.tts else False,
        "llm_enabled": bool(s.get("llm.enabled")),
    })


def _addon_version() -> str:
    return const.addon_version()  # 唯一版本链（曾自读 /data/version.txt+"dev" 兜底，与 const 漂移）


async def _get_settings(request):
    ctx = request.app[CTX_KEY]
    return web.json_response(ctx.settings.masked())


async def _post_settings(request):
    ctx = request.app[CTX_KEY]
    try:
        patch = await request.json()
    except Exception:
        return web.json_response({"message": "bad json"}, status=400)
    if not isinstance(patch, dict):
        return web.json_response({"message": "body must be object"}, status=400)
    ctx.settings.update(patch)   # 热应用回调在 main 注册（TTS 音色/阈值/卸载策略等）
    return web.json_response({"ok": True, "settings": ctx.settings.masked()})


async def _models(request):
    ctx = request.app[CTX_KEY]
    return web.json_response(ctx.store.snapshot())


async def _model_download(request):
    ctx = request.app[CTX_KEY]
    body = await _json_body(request)
    key = str(body.get("key", ""))
    if key not in ctx.store.keys():
        return web.json_response({"message": f"未知模型 {key}"}, status=400)
    ctx.store.ensure_async(key)
    return web.json_response({"ok": True})


async def _endpoints(request):
    ctx = request.app[CTX_KEY]
    host = ctx.host or "homeassistant.local"
    urls = ctx.settings.endpoint_urls(host)
    tok = ctx.settings.get("security.ws_token", "")
    return web.json_response({
        "stt_endpoint": urls["stt"], "tts_endpoint": urls["tts"], "llm_endpoint": urls["llm"],
        "token": tok, "require_token": bool(ctx.settings.get("security.require_token")),
        "note": "填入 huijian_ai 集成的 llm/stt/tts_endpoint 配置项"})


async def _regen_token(request):
    ctx = request.app[CTX_KEY]
    import secrets
    ctx.settings.update({"security": {"ws_token": secrets.token_urlsafe(24)}})
    return web.json_response({"ok": True})


async def _nlu_test(request):
    ctx = request.app[CTX_KEY]
    body = await _json_body(request)
    text = str(body.get("text", "")).strip()
    if not text:
        return web.json_response({"message": "text 必填"}, status=400)
    dry = await ctx.pipeline.dry_run(text)
    executed = None
    if body.get("execute"):    # 显式勾选「真实执行」才走执行（调试面板默认关）
        # v1.0.9：必须与真实流量同一条 _cascade 全链（三层裁决+互为降级+LLM
        # 次序）。旧版在这里只跑 fast_path.match——面板显示裁决为 klar、实际
        # 执行的却是 t0 计划，两条通道各挂各的，调试结论全是错的。
        r = await ctx.pipeline._cascade(text)
        executed = {"ok": r.ok, "speech": r.text,
                    "source": r.source, "trace": r.trace}
    return web.json_response({"input": text, "cascade": dry, "executed": executed,
                              "textcnn": {"available": bool(ctx.textcnn and ctx.textcnn.available)}})


async def _tts_test(request):
    ctx = request.app[CTX_KEY]
    body = await _json_body(request)
    text = str(body.get("text", "好的，客厅的灯打开了")).strip()[:200]
    pcm = await ctx.tts.synthesize_pcm(text)
    if not pcm:
        return web.json_response({"message": "TTS 不可用（模型未就绪或合成失败）"}, status=503)
    from . import audio
    return web.Response(body=audio.pcm_to_wav(pcm), content_type="audio/wav",
                        headers={"Content-Disposition": 'inline; filename="tts_test.wav"'})


def _scene_row(sc: dict) -> dict:
    """场景 dict → 页面行（动作压成人读摘要，缺字段不炸）。永不抛。"""
    acts = sc.get("actions") or []
    sums = []
    for a in acts[:6]:
        if not isinstance(a, dict):
            continue
        intent = str(a.get("intent") or a.get("name") or "")
        pr = a.get("params") or a.get("args") or {}
        if not isinstance(pr, dict):
            pr = {}
        tgt = pr.get("target")
        area = tgt.get("area") if isinstance(tgt, dict) else ""
        bits = [str(x) for x in (pr.get("entity_id"), area, pr.get("state"),
                                 pr.get("brightness"), pr.get("temperature"))
                if x not in (None, "")]
        sums.append((f"{intent} {' '.join(bits[:3])}").strip())
    return {"trigger": str(sc.get("trigger_phrase") or ""),
            "name": str(sc.get("name") or sc.get("trigger_phrase") or ""),
            "scene_id": sc.get("scene_id"),
            "action_count": len(acts),
            "actions": [s for s in sums if s],
            "created_at": sc.get("created_at")}


async def _scenes(request):
    ctx = request.app[CTX_KEY]
    error = ""
    try:
        await ctx.scenes.refresh(force=True)
    except Exception as e:                       # 集成掉线/超时：页面如实说明
        error = f"场景列表拉取失败：{str(e)[:120]}"
    rows = [_scene_row(sc) for sc in
            (ctx.scenes.all() if hasattr(ctx.scenes, "all") else [])
            if isinstance(sc, dict)]
    # triggers 键保留（旧前端/排障脚本兼容）
    return web.json_response({"triggers": ctx.scenes.triggers,
                              "scenes": rows, "error": error})


async def _automations(request):
    """自动化 = HA 核心 REST（零集成依赖，集成没装也有的看）；
    开关态从 states 的 automation.<id> 联查。"""
    ctx = request.app[CTX_KEY]
    cfg = await ctx.ha.rest_get("/api/config/automations/config")
    items = cfg.get("automations") if isinstance(cfg, dict) else None
    if not isinstance(items, list):
        return web.json_response({"automations": [],
                                  "error": "读不到自动化配置（HA API 不可达或未鉴权）"})
    states = await ctx.ha.states()
    out = []
    for a in items:
        if not isinstance(a, dict):
            continue
        aid = str(a.get("id") or "")
        st = states.get(f"automation.{aid}") or {}
        attrs = st.get("attributes") or {}
        out.append({"id": aid,
                    "alias": str(a.get("alias") or attrs.get("friendly_name") or aid),
                    "description": str(a.get("description") or ""),
                    "last_triggered": str(a.get("last_triggered")
                                          or attrs.get("last_triggered") or ""),
                    "state": str(st.get("state") or "")})
    return web.json_response({"automations": out, "error": ""})


async def _reload_models(request):
    ctx = request.app[CTX_KEY]
    # F1：卸载走 executor（重析构不出循环）；推理在飞则本轮跳过并如实回报。
    import asyncio
    loop = asyncio.get_running_loop()
    notes = []
    if ctx.asr:
        done = await loop.run_in_executor(None, ctx.asr.unload)
        notes.append("STT 已卸载" if done else "STT 推理中，跳过（稍后再点或空闲自动重载）")
    if ctx.tts:
        done = await loop.run_in_executor(None, ctx.tts.unload)
        notes.append("TTS 已卸载" if done else "TTS 合成中，跳过")
    if not notes:
        notes.append("无本地引擎在载")
    logger.info("[管理] reload → %s", "；".join(notes))
    return web.json_response({"ok": True, "note": "；".join(notes) + "。下次请求惰性重新加载"})


async def _json_body(request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
