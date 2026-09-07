"""ESPHome constants."""

from typing import Final

from awesomeversion import AwesomeVersion

DOMAIN = "huijian_ai"

CONF_ALLOW_SERVICE_CALLS = "allow_service_calls"
CONF_SUBSCRIBE_LOGS = "subscribe_logs"
CONF_DEVICE_NAME = "device_name"
CONF_NOISE_PSK = "noise_psk"
CONF_BLUETOOTH_MAC_ADDRESS = "bluetooth_mac_address"
CONF_DEBOUNCE_MINUTES = "debounce_minutes"
CONF_TTS_ENTITY_ID = "tts_entity_id"
CONF_STT_ENTITY_ID = "stt_entity_id"

# ── assist 语音引擎条目（config_type="assist"）端点键 ──
# 统一字符串常量：config_flow 建/更条目、__init__ 装配三平台、huijian/*_transport
# 读端点、huijian/http.py device-info 返回，多处共用；改动键名必须同步本文件。
CONF_LLM_ENDPOINT = "llm_endpoint"
CONF_STT_ENDPOINT = "stt_endpoint"
CONF_TTS_ENDPOINT = "tts_endpoint"
CONF_MCP_ENDPOINT = "mcp_endpoint"
CONF_CONFIG_TYPE = "config_type"

# 语音引擎默认端点模板：assist 条目自动装配时指向本机加载项 WS 端口。
# 集成与 huijian_voice 加载项同宿主（host_network），默认取 HA internal URL
# 的 host 拼 :8000；用户可在条目「重新配置」中改成局域网内其它加载项地址。
VOICE_WS_PORT = 8000
VOICE_CHANNELS = ("llm", "stt", "tts")

DEFAULT_ALLOW_SERVICE_CALLS = True
DEFAULT_NEW_CONFIG_ALLOW_ALLOW_SERVICE_CALLS = False
DEFAULT_DEBOUNCE_MINUTES = 5

DEFAULT_PORT: Final = 6053

STABLE_BLE_VERSION_STR = "2025.11.0"
STABLE_BLE_VERSION = AwesomeVersion(STABLE_BLE_VERSION_STR)
PROJECT_URLS = {
    "esphome.bluetooth-proxy": "https://esphome.github.io/bluetooth-proxies/",
}
# ESPHome always uses .0 for the changelog URL
STABLE_BLE_URL_VERSION = f"{STABLE_BLE_VERSION.major}.{STABLE_BLE_VERSION.minor}.0"
DEFAULT_URL = f"https://esphome.io/changelog/{STABLE_BLE_URL_VERSION}.html"

NO_WAKE_WORD: Final[str] = "no_wake_word"

WAKE_WORDS_DIR_NAME = "custom_wake_words"
WAKE_WORDS_API_PATH = "/api/esphome/wake_words"
