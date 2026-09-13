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
                  "response_format": "", "sample_rate": 0,   # 预设透传项（空/0=pcm@24k）
                  # v1.0.52 云档分段读超时（秒）：停摆端几秒内失败→云→本地回落即时触发；
                  # total 刻意放宽（> const.TTS_STREAM_BUDGET_S 55s），慢而持续产出
                  # 的合成不得被误砍。first_byte 覆盖「响应头 + 首个音频数据块」，
                  # 整包回音频（非流式）的平台可适当调大。
                  "connect_timeout_s": 8.0, "first_byte_timeout_s": 6.0,
                  "read_timeout_s": 10.0, "total_timeout_s": 120.0},
    },
    "nlu": {
        "enabled": True,                       # 本地理解总开关：关=快速通道/场景契约/
                                               # 查询族/场景自动化本地承接全线让位，仅 LLM 兜底
        "textcnn_enabled": True,               # T1 分类器总开关
        "query_local": True,                   # 查询族（"客厅多少度"）本地读回（M1 定案）
        "creation_enabled": True,              # 场景/自动化语音句本地承接总开关（零 LLM）
        "thresholds_override": {},             # 按类阈值微调（默认用 intent.thresholds.json）
        "corrections_extra": {},               # 用户自定义热词纠错（追加到 61 条基础表）
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
        self._env_overrides: dict[str, Any] = {}     # v1.0.40：options 只覆盖运行期
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
        changed = self._repair_nodes()
        changed = self._ensure_secrets() or changed
        self._apply_env_overrides()
        if changed:
            self._write_locked()

    def _repair_nodes(self) -> bool:
        """节点类型修复（v1.0.40 修复 A4）：settings.json 被手改或异常写成
        `"security": null` 这类非 dict 值时，旧实现会在 `_ensure_secrets` 直接
        `AttributeError: 'NoneType' object has no attribute 'get'` **崩在启动路径**
        （真机形态：改过文件/写盘被截断 → 服务起不来）。按 DEFAULTS 恢复为默认
        dict，只警告不崩。返回是否发生修复。

        C3（2026-09-22 审查批）：旧实现只查**顶层**节点——S9 钉的是 ws_token 位
        的脏叶子，但 DEFAULTS 里 dict 套 dict（stt.cloud / tts.cloud）脏一层就
        漏一层：`{"stt":{"cloud":"garbage"}}` 经 _deep_merge 覆写落盘后，
        masked() 的 `c.get("api_key")` AttributeError → **GET /api/settings 恒
        500，Web 设置面板永久打不开**（唯一出路是手改 /data/settings.json）。
        现按 DEFAULTS 结构递归核验：凡默认是 dict 的位置，实际非 dict 即整子树
        恢复默认（脏值本就不可用，回默认是语义最近的修复）。"""
        fixed = False

        def walk(dst: dict, ref: dict, path: str) -> None:
            nonlocal fixed
            for key, default in ref.items():
                if not isinstance(default, dict):
                    continue
                cur = dst.get(key)
                sub = f"{path}{key}"
                if not isinstance(cur, dict):
                    logger.warning("[配置] %s 节点类型异常(%s)，已恢复默认",
                                   sub, type(cur).__name__)
                    dst[key] = copy.deepcopy(default)
                    fixed = True
                else:
                    walk(cur, default, sub + ".")

        walk(self._data, DEFAULTS, "")
        return fixed

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
        """Supervisor options（run.sh 导出）覆盖层——**只作用于运行期，绝不落盘**。

        v1.0.40 修复（D4）：旧实现直接改 `self._data`（随后被 `_write_locked()` 落盘），
        而 run.sh 每次启动都导出 options（`idle_unload_minutes` 默认 0）⇒ ① options
        值被写进 settings.json；② **每次启动都压过 Web UI**（UI 里设的省电档一重启
        就被 0 抹回，实测 BOOT1 写 42、BOOT2 撤掉 env 仍是 42）。
        现改为运行期覆盖字典：`get()` 优先命中它，落盘永不涉及。且省电档改「仅有
        非 0 值才覆盖」——0 是 options 的默认（=不干预），此时以 UI 的值为准。
        """
        self._env_overrides = {}
        if (v := os.environ.get("HUIJIAN_OPT_IDLE_UNLOAD_MIN")) not in (None, ""):
            try:
                n = max(0, int(v))
                if n > 0:                       # 0 = 不干预，交回 Web UI（单一事实源）
                    self._env_overrides["power.unload_when_idle_min"] = n
            except ValueError:
                logger.warning("[配置] HUIJIAN_OPT_IDLE_UNLOAD_MIN 非法值 %r，忽略", v)
        if (v := os.environ.get("HUIJIAN_STT_PROVIDER")):
            self._env_overrides["stt.provider"] = v

    # ── 读写 ────────────────────────────────────────────────────
    def get(self, dotted: str, default: Any = None) -> Any:
        if dotted in self._env_overrides:            # v1.0.40：options 覆盖层（不落盘）
            return self._env_overrides[dotted]
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
            # C3 读侧纵深：脏 cloud 节点（str/None/int）不得让脱敏崩成 500——
            # _repair_nodes 已递归兜底，这里再挡一切手工塞进来的怪值（S9 同款思路）。
            if isinstance(c, dict) and c.get("api_key"):
                c["api_key"] = "****"
        if d["llm"].get("api_key"):
            d["llm"]["api_key"] = "****"
        # v1.0.41 审查 S9：脏叶子（repair 只查节点顶层类型，dict/int 可藏在 ws_token 位）
        # 会在切片处 TypeError → GET /api/settings 恒 500。非 str 一律按 **** 封顶
        # （异常形态的凭据本来就更不能出门）。
        for k in ("ws_token", "pairing_token"):
            t = d["security"].get(k)
            if not t:
                continue
            if isinstance(t, str):
                d["security"][k] = t[:4] + "…" + t[-4:]
            else:
                d["security"][k] = "****"
        return d

    def update(self, patch: dict) -> dict:
        """深合并写入（Web 保存）。拒绝把脱敏占位 **** 回写成 api_key。"""
        with self._lock:
            # v1.0.41 审查 S3：`{"security": null}` 这类非 dict 脏节点经 _deep_merge
            # 覆盖后会被 _repair_nodes 重置默认 → _ensure_secrets 静默重生成
            # ws_token/pairing_token → **全部已配对卫星/小程序握手失效且零提示**。
            # 写入侧直接丢弃脏节点（保住既有值并大声告警）；读侧 repair 兜底不变。
            dirty = [k for k, v in list(patch.items())
                     if isinstance(DEFAULTS.get(k), dict) and not isinstance(v, dict)]
            if dirty:
                logger.error("[配置] 丢弃非 dict 脏节点写入（保护既有凭据）: %s",
                             ", ".join(f"{k}({type(patch[k]).__name__})" for k in dirty))
                patch = {k: v for k, v in patch.items() if k not in dirty}
            self._scrub_masked(patch)
            self._data = _deep_merge(self._data, patch)
            self._repair_nodes()          # v1.0.40：patch 也可能把节点写成非 dict
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
        sec = patch.get("security")
        if isinstance(sec, dict):
            for key in ("ws_token", "pairing_token"):
                v = sec.get(key)
                # v1.0.40 修复（A3）：旧的哨兵只认 `""/None/**** 前缀`，而 `masked()`
                # 的输出格式是 `t[:4]+"…"+t[-4:]`——两者不匹配 ⇒ 任何"GET /api/settings
                # 后原样 POST 回 security 块"的客户端会把真 token **写成 9 字符残串**
                # （实测 32→9，且不报错）。凡"空值/任何脱敏形态"一律丢弃。
                if v is None or v == "" or (
                        isinstance(v, str) and (v.startswith("****") or "…" in v)):
                    sec.pop(key, None)

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
