"""常量与运行时环境（网关同款「env 可覆盖」惯例，便于本机 pytest/E2E）。"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "huijian_voice"
# 裸 docker build 会把 ENV 烘成占位值 0.0.0：毒值不得穿透成对外版本
_env_ver = os.environ.get("HUIJIAN_VERSION", "")
APP_VERSION = _env_ver if _env_ver and _env_ver != "0.0.0" else "1.0.43"


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
SETTINGS_FILE = Path(os.environ.get("HUIJIAN_SETTINGS", str(DATA_DIR / "settings.json")))
INSTALL_DIR = Path(os.environ.get("HUIJIAN_INSTALL_DIR", str(Path(__file__).resolve().parent.parent)))
NLU_DATA_DIR = Path(os.environ.get("HUIJIAN_NLU_DATA", str(INSTALL_DIR / "nlu_data")))
MODELS_LOCK_FILE = Path(os.environ.get("HUIJIAN_MODELS_LOCK", str(INSTALL_DIR / "models.lock.json")))

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

# 超时预算（服务器侧收尾必须早于客户端预算，防滞留帧污染下一请求，契约 §6-5）
STT_RESULT_BUDGET_S = 55.0    # 客户端等 stt 帧 60s（stt.py:109）
TTS_STREAM_BUDGET_S = 55.0    # 客户端整流 fail_after 60（tts_transport.py:38）
LLM_TURN_BUDGET_S = 50.0      # 客户端外层 fail_after 60（conversation.py:73-80）
CONNECT_GATE_S = 14.0         # 客户端 ensure_connected 15s 上限——upgrade 前禁止慢操作

# 兜底话术（LLM 关且级联全 miss；可在 settings.dialog.fallback_text 覆盖）
FALLBACK_TEXT = "这句话我还不会，可以说「打开客厅的灯」或「客厅多少度」试试。"

# 事件旁路（v4 §2 旁路：每回合 trace 供自动化与排障）
EVENT_NAME = "huijian_voice_utterance"

# 语言判定：文本首字符是否中文（话术中英分支，沿用 fast_path _friendly_text 定式）
def is_chinese(text: str) -> bool:
    for ch in (text or "")[:1] or "中":
        return "\u4e00" <= ch <= "\u9fff"
    return True
