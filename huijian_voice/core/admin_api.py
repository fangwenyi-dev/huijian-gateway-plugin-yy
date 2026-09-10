"""管理 API（127.0.0.1:8002；nginx 以 /api/local/ 反代，仅经 HA ingress 可达）。

能力对齐 v2 §5 Web UI 五分区：状态/设置/模型/调试(理解级联 dry-run)/配对(endpoints)。
注意：/api/nlu/test 只跑级联不执行（调试面板绝不能真开灯）。
"""
from __future__ import annotations

import logging
import re
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
    # v1.0.34 场景/自动化页内生命周期（不引流去 HA 集成页）
    app.router.add_post("/api/scenes/delete", _scene_delete)
    app.router.add_post("/api/scenes/rename", _scene_rename)
    app.router.add_post("/api/scenes/test", _scene_test)
    app.router.add_post("/api/automations/delete", _auto_delete)
    app.router.add_post("/api/automations/test", _auto_test)
    app.router.add_post("/api/automations/edit", _auto_edit)
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
        "ha_error": (getattr(ctx.ha, "last_error", "") if ctx.ha else ""),
        "sessions": len(ctx.sessions),
        "models_ready": {k: ctx.store.is_ready(k) for k in ctx.store.keys()},
        "asr_loaded": bool(ctx.asr.ready()) if ctx.asr else False,
        "tts_loaded": bool(ctx.tts.ready()) if ctx.tts else False,
        "llm_enabled": bool(s.get("llm.enabled")),
        # 本地理解总开关（首页状态位数据源）：关掉后场景触发词/本地建・改・删/
        # 查询族/音乐带全停，必须一眼可见，不能静默降级
        "nlu_enabled": bool(s.get("nlu.enabled", True)),
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
    """场景 dict → 页面行（动作压成人读摘要，缺字段不炸）。永不抛。

    v1.0.40：现役 pipeline（`_build_actions`）存的是 **list 形态**
    `params.target = [{"area":…, "devices":[{"name":…}]}]`，本函数旧实现却按
    **dict** 读（`isinstance(tgt, dict)` 恒假）→ area/设备名全丢、bits 空，
    页面只剩英文意图名（`TurnDeviceOn`）。改为优先复用同一文件里自动化页已在
    用的 `_hv_action_cn`（list 形态正确），并保留平铺形态的旧渲染作兜底——
    历史/异构数据行为不变。
    """
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
        # 慧尖现役形态判据：target 为 list；或意图自带语义（无目标也成人话）。
        # 不满足即走下方平铺兜底——`_hv_action_cn` 对未知意图只会回吐裸英文
        # 意图名（非空），若按"非空即用"会把平铺形态的 entity_id 一起吞掉。
        if isinstance(tgt, list) or intent in ("SetDeviceMode",
                                               "AdjustDeviceAttribute",
                                               "ControlWindow"):
            cn = _hv_action_cn(a)
            if cn:
                sums.append(cn)
                continue
        # ── 兜底：非慧尖形态（平铺 params/entity_id）保持原渲染，兼容历史数据 ──
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


_HV_INTENT_CN = {"TurnDeviceOn": "打开", "TurnDeviceOff": "关闭",
                 "ControlWindow": "开/关窗", "AdjustDeviceAttribute": "调节",
                 "SetDeviceMode": "设为"}


def _hv_trigger_cn(trig: dict) -> str:
    """语音自动化 trigger 结构 → 中文短语（v1.0.32，列表展示用）。永不抛。"""
    try:
        at = str(trig.get("at") or "").strip()
        if at:
            h, m = int(at[:2]), at[3:5]
            seg = ("凌晨" if h < 6 else "早上" if h < 11 else "中午" if h < 13
                   else "下午" if h < 18 else "晚上")
            hh = h if 1 <= h <= 12 else (h - 12 if h >= 13 else 12)
            tail = "半" if m == "30" else "" if m == "00" else f"{int(m)}分"
            return f"每天{seg}{hh}点{tail}"
        ent = str(trig.get("entity_id") or "") or "?"
        if trig.get("to") is not None:
            return f"{ent} 有人" if str(trig["to"]) == "on" else f"{ent} 无人"
        parts = []
        if trig.get("above") is not None:
            parts.append(f"高于{trig['above']:g}")
        if trig.get("below") is not None:
            parts.append(f"低于{trig['below']:g}")
        return f"{ent}（{'、'.join(parts)}）" if parts else ent
    except Exception:
        return str(trig)


def _hv_action_cn(act: dict) -> str:
    """{intent, params} → 「打开客厅射灯」式短语。永不抛。

    v1.0.40：补齐两处会丢信息的意图——`ControlWindow` 的开关动作（open/close/
    pause/a）与 `AdjustDeviceAttribute` 的「属性+数值」（旧 `_scene_row` 本就想
    显示 brightness/temperature，list 形态下被吃掉）。SetDeviceMode 原样。
    """
    try:
        intent = str(act.get("intent") or act.get("name") or "")
        p = act.get("params") or act.get("parameters") or {}
        if not isinstance(p, dict):
            return ""
        if intent == "SetDeviceMode":
            from .executor import MODE_CN
            mode = str(p.get("mode") or "")
            return f"设为{MODE_CN.get(mode, mode)}模式"
        words = []
        for t in (p.get("target") or []):
            if not isinstance(t, dict):
                continue
            for d in (t.get("devices") or []):
                if isinstance(d, dict):
                    words.append(f"{t.get('area', '')}{d.get('name', '')}")
        targets = "、".join(w for w in words if w)
        if intent == "AdjustDeviceAttribute":
            from .executor import ATTR_CN
            raw_attr = str(p.get("attribute") or "")
            attr = ATTR_CN.get(raw_attr, raw_attr)
            delta = str(p.get("delta") or "")
            delta = {"max": "最大", "min": "最小"}.get(delta, delta)
            return (f"调节{targets}{attr}{delta}").strip()
        if intent == "ControlWindow":
            from .executor import ACT_CN
            verb = ACT_CN.get(str(p.get("action") or "").lower(),
                              _HV_INTENT_CN.get(intent, intent))
        else:
            verb = {"TurnDeviceOff": "关闭", "TurnDeviceOn": "打开"}.get(
                intent, _HV_INTENT_CN.get(intent, intent))
        return verb + targets
    except Exception:
        return ""


async def _automations(request):
    """自动化 = 两引擎合并视图（v1.0.32）：
    ① 慧尖语音自动化（.storage 私有引擎，经集成 /api/huijian-ai/automations；
       集成未装/掉线 fail-open——只剩核心档并如实提示，不整表报错）；
    ② HA 核心 REST（YAML/UI 自动化，原行为）。"""
    ctx = request.app[CTX_KEY]
    out: list[dict] = []
    note = ""
    try:
        data = await ctx.ha.rest_get("/api/huijian-ai/automations")
        for a in ((data or {}).get("automations", [])
                  if isinstance(data, dict) else []):
            if not isinstance(a, dict):
                continue
            out.append({
                "kind": "huijian",
                "id": str(a.get("automation_id") or ""),
                "alias": _hv_trigger_cn(a.get("trigger") or {}),
                "trigger_raw": a.get("trigger") if isinstance(a.get("trigger"),
                                                              dict) else {},
                "description": "；".join(
                    x for x in (_hv_action_cn(act)
                                for act in (a.get("actions") or [])[:3]) if x),
                "last_triggered": str(a.get("last_triggered") or ""),
                "state": "语音引擎",
            })
    except Exception as e:                        # 集成缺席不拦核心列表
        note = f"语音自动化读取失败（集成未装/未响应）：{str(e)[:80]}"
    cfg = await ctx.ha.rest_get("/api/config/automations/config")
    items = cfg.get("automations") if isinstance(cfg, dict) else None
    if not isinstance(items, list):
        return web.json_response({"automations": out,
                                  "error": note or "读不到自动化配置（HA API 不可达或未鉴权）"})
    states = await ctx.ha.states()
    for a in items:
        if not isinstance(a, dict):
            continue
        aid = str(a.get("id") or "")
        st = states.get(f"automation.{aid}") or {}
        attrs = st.get("attributes") or {}
        out.append({"kind": "core",
                    "id": aid,
                    "alias": str(a.get("alias") or attrs.get("friendly_name") or aid),
                    "description": str(a.get("description") or ""),
                    "last_triggered": str(a.get("last_triggered")
                                          or attrs.get("last_triggered") or ""),
                    "state": str(st.get("state") or "")})
    return web.json_response({"automations": out, "error": note})


async def _safe_intent(ctx, name, data) -> dict:
    try:
        j = await ctx.ha.handle_intent(name, data)
        return j if isinstance(j, dict) else {"success": False,
                                              "error": "bad response"}
    except Exception as e:
        return {"success": False, "error": str(e)[:120]}


async def _safe_rest(ctx, method, path, body) -> dict:
    try:
        return await ctx.ha.rest_write(method, path, body)
    except Exception as e:
        return {"success": False, "error": str(e)[:120]}


def _op_response(j) -> web.Response:
    """集成/意图操作结果 → 页面统一信封 {ok, error}。"""
    ok = bool(isinstance(j, dict) and j.get("success"))
    err = "" if ok else str((j or {}).get("error")
                            or (j or {}).get("message") or "操作未成功")
    return web.json_response({"ok": ok, "error": err})


# v1.0.34（审查 M3）：scene_id（voice_scene_<14位>）/automation_id（uuid 带-）
# 要插进 REST URL 路径，yarl 会归一化点段——`../states/x` 一类输入可携加载项
# 令牌打到核心任意写路径。白名单不放**任何点**（真 id 形态本就没有），从根
# 上封死穿越；不合规一律拒（永不抛纪律不受影响，返回的还是 {ok:False}）。
_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
# 触发词字符集与语音侧同一纪律（creation 正则全部排除这些字符）——网页改名
# 出去含空格/标点的触发词，语音既触发不了也永远删不到（审查 L4）。
_PHRASE_RE = re.compile(r"[^「」\"』，。;；\s]{1,12}")


def _bad_id(v: str) -> bool:
    return not _ID_RE.fullmatch(v or "")


def _bad_phrase(v: str) -> bool:
    return not _PHRASE_RE.fullmatch(v or "")


async def _scene_delete(request):
    """页内删场景：走 intent（trigger_phrase 精准），删后强刷缓存。"""
    ctx = request.app[CTX_KEY]
    body = await _json_body(request)
    tp = str(body.get("trigger_phrase") or "").strip()
    if not tp:
        return web.json_response({"ok": False, "error": "缺场景名"})
    j = await _safe_intent(ctx, "HassDeleteVoiceScene", {"trigger_phrase": tp})
    if isinstance(j, dict) and j.get("success"):
        try:
            await ctx.scenes.refresh(force=True)
        except Exception:
            pass
    return _op_response(j)


async def _scene_rename(request):
    """页内改触发词：PUT 集成场景视图（actions 不动）。"""
    ctx = request.app[CTX_KEY]
    body = await _json_body(request)
    sid = str(body.get("scene_id") or "").strip()
    new = str(body.get("new_phrase") or "").strip()
    if _bad_id(sid):
        return web.json_response({"ok": False, "error": "场景ID形态不合规"})
    if not sid or _bad_phrase(new):
        return web.json_response({"ok": False, "error": "新触发词需 1~12 字且不含标点空格"})
    j = await _safe_rest(
        ctx, "PUT", f"/api/huijian-ai/voice-scenes/{sid}",
        {"trigger_phrase": new})
    if isinstance(j, dict) and j.get("success"):
        try:
            await ctx.scenes.refresh(force=True)
        except Exception:
            pass
    return _op_response(j)


async def _scene_test(request):
    """页内测试触发：集成 test-scene 端点真执行动作。"""
    ctx = request.app[CTX_KEY]
    body = await _json_body(request)
    tp = str(body.get("trigger_phrase") or "").strip()
    if not tp:
        return web.json_response({"ok": False, "error": "缺场景名"})
    j = await _safe_rest(ctx, "POST", "/api/huijian-ai/test-scene",
                         {"trigger_phrase": tp})
    return _op_response(j)


async def _auto_delete(request):
    ctx = request.app[CTX_KEY]
    body = await _json_body(request)
    aid = str(body.get("automation_id") or "").strip()
    if not aid:
        return web.json_response({"ok": False, "error": "缺自动化ID"})
    j = await _safe_intent(ctx, "HassDeleteAutomation", {"automation_id": aid})
    return _op_response(j)


async def _auto_test(request):
    """页内测试触发：立即执行该自动化的动作（集成记录 test 日志）。"""
    ctx = request.app[CTX_KEY]
    body = await _json_body(request)
    aid = str(body.get("automation_id") or "").strip()
    if not aid:
        return web.json_response({"ok": False, "error": "缺自动化ID"})
    j = await _safe_rest(ctx, "POST", "/api/huijian-ai/test-automation",
                         {"automation_id": aid})
    return _op_response(j)


async def _auto_edit(request):
    """页内编辑数值阈值（表单只给数值形态行）。PUT 集成端 trigger 全量替换，
    entity_id 原样带回；非数值形态（at/to）拒绝——与语音侧边界一致。"""
    ctx = request.app[CTX_KEY]
    body = await _json_body(request)
    aid = str(body.get("automation_id") or "").strip()
    trig = body.get("trigger") if isinstance(body.get("trigger"), dict) else {}
    ent = str(trig.get("entity_id") or "").strip()
    if _bad_id(aid):
        return web.json_response({"ok": False, "error": "自动化ID形态不合规"})
    if not aid or not ent:
        return web.json_response({"ok": False, "error": "缺自动化ID或传感器"})
    new_trig: dict = {"entity_id": ent}
    for k in ("above", "below"):
        v = trig.get(k)
        if v is not None and str(v).strip() != "":
            try:
                f = float(v)
            except (TypeError, ValueError):
                return web.json_response({"ok": False, "error": f"{k} 不是数值"})
            if f != f or f in (float("inf"), float("-inf")):
                # 审查 L6：NaN/inf 过闸后进 JSON 体在集成侧炸不透明错误。
                return web.json_response({"ok": False, "error": f"{k} 不是有效数值"})
            new_trig[k] = f
    if "above" in new_trig and "below" in new_trig and \
            new_trig["above"] >= new_trig["below"]:
        return web.json_response({"ok": False, "error": "高于值必须小于低于值（区间）"})
    j = await _safe_rest(ctx, "PUT", f"/api/huijian-ai/automations/{aid}",
                         {"trigger": new_trig})
    return _op_response(j)


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
