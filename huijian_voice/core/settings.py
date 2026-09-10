"""/data/settings.json 单一事实源管理器。

定案依据：
- v2 §4.2「唯一生效配置、首次启动自动生成全默认值 → 装完即用零必填」；
- v3 §4.5 Provider 键结构；
- v4.1 ②「STT/TTS/LLM 三项均用户可配：STT/TTS 默认本地，LLM 默认关」。
Supervisor options（高级四项）经 run.sh 以 HUIJIAN_OPT_* 环境变量注入，本模块
负责在读取时做「options 覆盖层」（v2 §4.3 分层入口定式）。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Optional

from . import const

logger = logging.getLogger("huijian.settings")

# ── 默认值（=「装完即用」的全部内置值；除 token 外均可 Web UI 修改）──
DEFAULTS: dict[str, Any] = {
    "stt": {
        # provider：local_paraformer(=本地引擎总开关值，历史兼容，勿改字面) | cloud_openai_compat
        # v4.2：本地具体引擎由 local_model 决定；provider 字面值保持兼容存量 settings.json
        "provider": "local_paraformer",
        "local_model": "sensevoice",          # sensevoice(默认中英粤) | paraformer(双语流式兼容回落档)
        "language": "zh-CN",
        # 云档示例：{"provider":"cloud_openai_compat","base_url":"https://dashscope.aliyuncs.com/compatible-mode/v1","api_key":"","model":"paraformer 或 whisper 兼容名"}
        # 云失败自动回落本地（v4.1-②）
        "cloud": {"provider": "", "base_url": "", "api_key": "", "model": ""},
    },
    "tts": {
        "provider": "local_kokoro",           # local_kokoro | cloud_openai_compat
        "sid": 18,                             # v1.1 定案默认：zf_026（女，声纹最似晓晓；用户知情拍板 2026-09-13）。103 音色 web 全可选
        "speed": 1.0,                          # 0.8–1.2
        "cache_enabled": True,                 # 体验批 P0-4：句级 opus LRU 缓存（本地档）
        "cloud": {"provider": "", "base_url": "", "api_key": "", "model": "", "voice": "",
                  "response_format": "", "sample_rate": 0},   # 预设透传项（空/0=pcm@24k）
    },
    "nlu": {
        "enabled": True,                       # 本地理解总开关：关=快速通道/场景契约/
                                               # 查询族/场景自动化本地承接全线让位，仅 LLM 兜底
        "textcnn_enabled": True,               # T1 分类器总开关
        "query_local": True,                   # 查询族（"客厅多少度"）本地读回（M1 定案）
        "creation_enabled": True,              # 场景/自动化语音句本地承接总开关（零 LLM）
        "thresholds_override": {},             # 按类阈值微调（默认用 intent.thresholds.json）
        "corrections_extra": {},               # 用户自定义热词纠错（追加到 58 条基础表）
    },
    "klar": {
        # 一级确定性 NLU（klar-ha-nlu 引擎，容器内 s6 服务 loopback :10520）。
        # 关/引擎缺失/连续失败熔断 → 恒降级 TextCNN，语音链不断（fail-open）。
        "enabled": True,
        "url": "http://127.0.0.1:10520",
        "language": "zh-CN",                   # 按请求绑定语言包（防多包词表碰撞）
        "timeout_s": 2.0,
        "min_confidence": 0.80,                # 客户端第二道门（与引擎 execute band 同值）
        "token": "",                           # loopback 默认免 token；外接远端引擎才需
    },
    "llm": {
        "enabled": False,                      # v4.1 定案默认关；4C8G 无可用本地 LLM
        "base_url": "",                        # OpenAI 兼容（百炼/火山方舟[慧尖SFT模板]/LAN ollama）
        "api_key": "",
        "model": "",
        "temperature": 0.3,
        "history_rounds": 10,
        "max_tool_rounds": 3,
        "allow_scene_write": True,             # LLM 写语音场景（本地句已零 LLM 覆盖，LLM 是兜底）
        "allow_automation_write": False,       # v2 定案：LLM 写自动化默认关（本地句可直接建）
        "stream": True,                        # 体验批 P2-15：SSE 流式（平台不认自动回退整包）
    },
    "dialog": {
        "fallback_text": const.FALLBACK_TEXT,
        "dedup_window_s": 2.0,                 # 相同文本短时去重（防重复执行，契约 §1.4-②）
        "context_enabled": True,               # 体验批 P2-10：跨轮目标继承 + LLM 真历史
        "context_ttl_s": 90.0,                 # 继承窗口：说"关掉它"距上一句不超过 90s 才复用目标
        "chain_enabled": True,                 # 体验批 P2-12：复合句分句链发（全命中才链）
        "confirm_risky": True,                 # 体验批 P2-13：解锁/删场景先问「确认」再办
        "confirm_ttl_s": 30.0,                 # 确认问句存活：超过 30s 不回即作废
    },
    "spatial": {
        # 体验批 P2-11：卫星 IP → 区域名映射（"192.168.1.31": "卧室"）。
        # 无目标句（"开灯"）默认落说话卫星所在区域；明示目标句零影响。空=行为同旧。
        "satellite_areas": {},
    },
    "music": {
        # 零改动音乐过渡带（用户定向 2026-09-12）：语音点歌/播控直连 HA 标准
        # media_player 服务；端点填 MA 托管播放器的 entity_id。空=点歌只回配置指引。
        "player_entity": "",
    },
    "power": {
        "unload_when_idle_min": 0,             # 0=模型常驻；>0 空闲 N 分钟卸载（省电档）
    },
    "security": {
        "ws_token": "",                        # 首启自动生成；WS URL query token
        "require_token": False,                # LAN MVP 默认放行匿名（契约 §5-3），公网形态前一键开
        "pairing_token": "",                   # 预留给配网页（M3）
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    """线程安全的 settings 持有者。Web 热改 → save() → 订阅回调（Provider 级热应用，
    v2 §4.1 生效规则：不重启不断设备连接）。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else const.SETTINGS_FILE
        self._lock = threading.Lock()
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self._listeners: list = []
        self.load_or_create()

    # ── 生命周期 ────────────────────────────────────────────────
    def load_or_create(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("settings.json 顶层必须是对象")
                self._data = _deep_merge(DEFAULTS, raw)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # 坏文件防御：备份后重建，服务不能因配置炸掉
            logger.error("[配置] settings.json 解析失败(%s)，备份为 %s.bak 并重建默认", e, self.path.name)
            try:
                self.path.rename(self.path.with_suffix(".json.bak"))
            except OSError:
                pass
            self._data = copy.deepcopy(DEFAULTS)
        changed = self._ensure_secrets()
        self._apply_env_overrides()
        if changed:
            self._write_locked()

    def _ensure_secrets(self) -> bool:
        """ws_token/pairing_token 自动生成且持久（凭据定案：不是给用户改的，
        重置走 Web UI 高级按钮）。返回是否发生变更。"""
        changed = False
        sec = self._data["security"]
        for key in ("ws_token", "pairing_token"):
            if not sec.get(key):
                sec[key] = secrets.token_urlsafe(24)
                changed = True
        return changed

    def _apply_env_overrides(self) -> None:
        """Supervisor options（run.sh 导出）覆盖层——只覆盖运行级键。"""
        if (v := os.environ.get("HUIJIAN_OPT_IDLE_UNLOAD_MIN")) not in (None, ""):
            try:
                self._data["power"]["unload_when_idle_min"] = max(0, int(v))
            except ValueError:
                logger.warning("[配置] HUIJIAN_OPT_IDLE_UNLOAD_MIN 非法值 %r，忽略", v)
        if (v := os.environ.get("HUIJIAN_STT_PROVIDER")):
            self._data["stt"]["provider"] = v
        if (v := os.environ.get("HUIJIAN_DATA_DIR_FOR_TEST")):  # pytest 注入
            pass

    # ── 读写 ────────────────────────────────────────────────────
    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self._data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    @property
    def data(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def masked(self) -> dict:
        """给 Web UI 的视图：api_key/token 脱敏（网关「密码不回显」同款纪律）。"""
        d = self.data
        for prov in ("stt", "tts"):
            c = d[prov].get("cloud") or {}
            if c.get("api_key"):
                c["api_key"] = "****"
        if d["llm"].get("api_key"):
            d["llm"]["api_key"] = "****"
        if d["security"].get("ws_token"):
            t = d["security"]["ws_token"]
            d["security"]["ws_token"] = t[:4] + "…" + t[-4:]
        return d

    def update(self, patch: dict) -> dict:
        """深合并写入（Web 保存）。拒绝把脱敏占位 **** 回写成 api_key。"""
        with self._lock:
            self._scrub_masked(patch)
            self._data = _deep_merge(self._data, patch)
            self._ensure_secrets()
            self._write_locked()
        for cb in list(self._listeners):
            try:
                cb(self._data)
            except Exception:  # 订阅者炸不影响写入方
                logger.exception("[配置] 热应用回调异常")
        return self.data

    @staticmethod
    def _scrub_masked(patch: dict) -> None:
        for prov in ("stt", "tts", "llm"):
            node = patch.get(prov)
            if not isinstance(node, dict):
                continue
            if node.get("api_key") == "****":
                del node["api_key"]
            cloud = node.get("cloud")
            if isinstance(cloud, dict) and cloud.get("api_key") == "****":
                del cloud["api_key"]
        if patch.get("security", {}).get("ws_token") in ("", None) or patch.get("security", {}).get("ws_token", "").startswith(("****",)):
            patch.get("security", {}).pop("ws_token", None)

    def save(self) -> None:
        with self._lock:
            self._write_locked()

    def _write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)   # tmp+mv 原子替换（网关 jq 写 JSON 同款纪律）

    def add_listener(self, cb) -> None:
        self._listeners.append(cb)

    # ── 便捷访问器（热路径少拷贝）───────────────────────────────
    def endpoint_urls(self, host: str) -> dict:
        tok = self.get("security.ws_token", "")
        base = f"ws://{host}:{const.WS_PORT}/xiaozhi/v1"
        return {k: f"{base}/{k}?token={tok}" for k in ("stt", "tts", "llm")}
