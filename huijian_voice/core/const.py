"""常量与运行时环境（网关同款「env 可覆盖」惯例，便于本机 pytest/E2E）。"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "huijian_voice"
# 裸 docker build 会把 ENV 烘成占位值 0.0.0：毒值不得穿透成对外版本
_env_ver = os.environ.get("HUIJIAN_VERSION", "")
APP_VERSION = _env_ver if _env_ver and _env_ver != "0.0.0" else "1.1.3"


def addon_version() -> str:
    """对外版本单一事实源：boot.sh 落盘的容器 stamp 优先，回 env/兜底常量。
    （审查教训：admin/main 曾各持一条含 "dev" 字面量兜底的重复链。）"""
    try:
        vf = DATA_DIR / "version.txt"
        if vf.exists():
            v = vf.read_text(encoding="utf-8").strip()
            if v and v not in ("dev", "0.0.0"):
                return v
    except OSError:
        pass
    return APP_VERSION

# ── 路径 ─────────────────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("HUIJIAN_DATA", "/data"))            # Supervisor 持久卷
MODELS_DIR = Path(os.environ.get("HUIJIAN_MODELS_DIR", str(DATA_DIR / "models")))
MODEL_IMPORT_DIR = MODELS_DIR / "import"                             # 手动导入投递口
TTS_VOICES_DIR = Path(os.environ.get("HUIJIAN_TTS_VOICES", str(DATA_DIR / "tts_voices")))
# ↑ 自定义音色投递口：每 .bin=一路纯 float32 风格向量（尺寸=官方单音尺寸），
#   sid 从官方音色数起编号，tts.sid 可填文件主名（见 core/tts.py merge_custom_voices）
SETTINGS_FILE = Path(os.environ.get("HUIJIAN_SETTINGS", str(DATA_DIR / "settings.json")))
INSTALL_DIR = Path(os.environ.get("HUIJIAN_INSTALL_DIR", str(Path(__file__).resolve().parent.parent)))
NLU_DATA_DIR = Path(os.environ.get("HUIJIAN_NLU_DATA", str(INSTALL_DIR / "nlu_data")))
MODELS_LOCK_FILE = Path(os.environ.get("HUIJIAN_MODELS_LOCK", str(INSTALL_DIR / "models.lock.json")))
# ── ESP32 固件仓（OTA 方案 Phase 2 加载项侧，2026-09-23）────────────────
# public=验过可下发区（token 一次性领取）；import=投递口（同 models 导入口纪律）。
# 发版锁可被持久卷同名文件覆盖（现场运维热修不重打镜像），与 models 双源惯例一致。
FIRMWARE_DIR = Path(os.environ.get("HUIJIAN_FIRMWARE_DIR", str(DATA_DIR / "firmware")))
FIRMWARE_PUBLIC_DIR = FIRMWARE_DIR / "public"
FIRMWARE_IMPORT_DIR = FIRMWARE_DIR / "import"
FIRMWARE_LOCK_FILE = Path(os.environ.get("HUIJIAN_FIRMWARE_LOCK", str(INSTALL_DIR / "firmware.lock.json")))

# 事实文件（nginx 静态目录，网关 status.json 同款模式）
STATUS_FILE = Path(os.environ.get("HUIJIAN_STATUS_FILE", "/usr/share/nginx/html/status.json"))
MODELS_STATUS_FILE = Path(os.environ.get("HUIJIAN_MODELS_STATUS_FILE", "/usr/share/nginx/html/models_status.json"))
NGINX_HTML = Path(os.environ.get("HUIJIAN_NGINX_HTML", "/usr/share/nginx/html"))  # 事实文件目录（nginx 静态暴露）

# ── 端口（host_network：监听即宿主端口）──────────────────────────────
WS_PORT = int(os.environ.get("HUIJIAN_WS_PORT", "8000"))            # 小智协议子集（集成三条外拨连接的目标）
ADMIN_PORT = int(os.environ.get("HUIJIAN_ADMIN_PORT", "8002"))      # 仅 127.0.0.1，经 nginx 代理出 ingress

# ── HA 侧（网关 discovery_proxy 同款定式：env 覆盖供 E2E）────────────
HA_API_DEFAULT = os.environ.get("HUIJIAN_HA_API", "http://supervisor/core/api")
HA_TOKEN_ENV = "HUIJIAN_HA_TOKEN"          # 覆盖 SUPERVISOR_TOKEN（E2E 用长时令牌直连真 HA）

# ── 协议钉死参数（《小智协议子集-服务器契约.md》§2/§3）────────────────
SAMPLE_RATE = 16000        # 上下行都钉死 16k mono（集成侧 tts.py:27-30 / audio.py:127 硬编码，不可协商）
CHANNELS = 1
FRAME_MS = 60
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000   # 960
FRAME_BYTES = FRAME_SAMPLES * 2                  # s16le = 1920B PCM/帧

# v1.0.88 下行流标识（边带身份，Stage 1）协议代次：tts 通道建连欢迎帧申报
# 本值，集成见 >=2 才 mint rid 并启用"未同步到自己 rid 前一律丢弃"。
# 缺失/0 = 旧协议（旧集成把未知 state 只记一条 INFO，行为逐字节不变）。
# 形状与不变量见 session.py 模块 docstring tts 段；带内逐帧头（1a）是本协议
# 的兼容超集，将来升代不改边带语义。
TTS_PROTO_VERSION = 2

# v1.0.92 STT 轮次身份（与 TTS Stage-1 对偶）：stt 通道建连欢迎帧申报本值，
# 集成见 >=2 才在 listen start/stop 携带 rid（客户端 mint），服务端把同一 rid
# 回显进 {"type":"stt"} 回执——消费端据此把**旧轮迟到的回执**就地丢弃而非
# 误认成本轮转写（现场：STT 30s 交付判死换连、张冠李戴转写的根修）。
# 缺失/0 = 旧协议：回执逐字节不变（旧集成零暴露）。
STT_PROTO_VERSION = 2

# 超时预算（服务器侧收尾必须早于客户端预算，防滞留帧污染下一请求，契约 §6-5）
# v1.0.70（深审⑧算术钉死）：预算不是单数字，是「整流 + 在飞帧发送 + 收口
# stop 发送」的总和对账。旧值 55+5+5=65s > 客户端 60s——超时竞跑现场表现=
# 客户端先收 60s 断连、服务器迟到的 stop 帧成下一条残留（"清掉上一轮残留"
# 每轮一条）。发送闸见 session._SEND_TIMEOUT_S；数字改任何一个都必须重算。
# v1.0.83（修#1：播报截尾根治）：本预算自 session._stream 起一直是**每轮墙钟**
# ——4000 字 cap 的最长合成（v2.1.44 账：≈590s 墙钟）必在第 52 秒被"整流超
# 预算截断"，truncated 以 error 收口 → 固件只播前半段（现场=长播报缺尾）。
# 而固件 v2.1.42/44 早已把设备端改成帧间隙心跳语义并按 950s 音频推导 1200s
# 硬顶——三端账不拢，本层就是那把新刀。现语义拆分：
#   • TTS_STREAM_BUDGET_S = **最大帧间隙窗**（逐帧重置的停滞判定）：合法最坏
#     单句间隙 46s（300 字/speed0.5/RTF0.33，v2.1.44 账）< 52s；且仍 > 设备
#     T_DL_STALL=48s——真停滞由我们显性 stop+truncated 先收口、设备看门狗只
#     兜 HA 搬运僵死的排序不变。
#   • TTS_STREAM_TOTAL_BUDGET_S = **整轮总闸**（反僵尸）：4000 字最坏合成
#     ≈590s + 余量 = 660s；对账链 660+2×3 ≤ 720-2（集成整轮闸
#     tts_transport._ROUND_TOTAL_BUDGET_S）< 1200（固件 T_LIVE_HARD_CAP）。
# 改本值/分句上限/语速下限任一项，须同步固件台架 [VA] 预算对账钉
# 与 tests/test_v1083_playback_budget_round_guard.py::test_budget_arithmetic_three_tier。
STT_RESULT_BUDGET_S = 52.0    # 客户端等 stt 帧 60s（stt.py:109）
TTS_STREAM_BUDGET_S = 52.0    # 逐帧间隙窗（客户端逐帧窗 60s，tts_transport.stream timeout）
TTS_STREAM_TOTAL_BUDGET_S = 660.0  # 整轮总闸（客户端整轮闸 720s，防永动僵尸流）
LLM_TURN_BUDGET_S = 50.0      # 客户端外层 fail_after 60（conversation.py:73-80）
CONNECT_GATE_S = 14.0         # 客户端 ensure_connected 15s 上限——upgrade 前禁止慢操作

# 兜底话术（LLM 关且级联全 miss；可在 settings.dialog.fallback_text 覆盖）
FALLBACK_TEXT = "这句话我还不会，可以说「打开客厅的灯」或「客厅多少度」试试。"

# ── v1.0.93 连续对话语音退出（2026-09-18 用户批准收词表）──────────────────
# 收词只进 fast_path 字面表且**整句精确**；本常量=执行话术，退出旗经 LLM end
# 帧 end_dialogue 键透传（三端契约：加载项→集成 INTENT_END kv→固件单轮旗）。
END_DIALOGUE_SAY = "好的，我先退下了，随时再叫我。"

# 事件旁路（v4 §2 旁路：每回合 trace 供自动化与排障）
EVENT_NAME = "huijian_voice_utterance"

# 语言判定：文本首字符是否中文（话术中英分支，沿用 fast_path _friendly_text 定式）
def is_chinese(text: str) -> bool:
    for ch in (text or "")[:1] or "中":
        return "\u4e00" <= ch <= "\u9fff"
    return True
