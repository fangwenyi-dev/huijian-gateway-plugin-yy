import hashlib
import hmac
import json
import logging

from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView

from ..const import CONF_STT_ENTITY_ID, CONF_TTS_ENTITY_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_https(hass: HomeAssistant):
    this_data = hass.data.setdefault(DOMAIN, {})
    if this_data.get("https_setup"):
        return
    this_data["https_setup"] = True
    hass.http.register_view(HuijianSetupView)
    hass.http.register_view(HuijianRemoveView)
    hass.http.register_view(HuijianSetNameView)
    hass.http.register_view(HuijianTtsSttView)


class HuijianHttpView(HomeAssistantView):
    requires_auth = False

    async def check_sign(self, request: web.Request, speak_id=None):
        hass = request.app[KEY_HASS]
        params = request.query
        if request.method in ("PUT", "POST"):
            params = await request.json() or {}
        if not speak_id:
            speak_id = params.get("speak_id") or request.query.get("speak_id", "")
        entry = None
        for ent in hass.config_entries.async_loaded_entries(DOMAIN):
            if speak_id == ent.data.get("speak_id"):
                entry = ent
                break
        if not entry:
            return None
        salt = request.headers.get("Salt", "")
        ret = request.headers.get("Authorization") == calculate_sign(
            request.path,
            params,
            entry.data.get("mac", "").lower(),
            salt,
        )
        return entry if ret else False


class HuijianSetupView(HuijianHttpView):
    url = "/api/huijian-ai/setup/qrcode"
    name = "api:huijian-ai:setup-qrcode"

    async def post(self, request: web.Request):
        hass = request.app[KEY_HASS]
        this_data = hass.data.setdefault(DOMAIN, {})
        if not (uuid := request.query.get("uuid")):
            return self.json_message("uuid missing", 400)
        if uuid not in this_data:
            return self.json_message("uuid invalid", 400)

        setup_data = await request.json() or {}

        # Verify signature using UUID as shared secret
        # If no signature is provided, fall back to UUID-only check
        # for backward compatibility with existing clients
        auth_header = request.headers.get("Authorization", "")
        if auth_header:
            expected_sign = hmac.new(
                uuid.encode("utf-8"),
                json.dumps(setup_data, separators=(",", ":")).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if auth_header != expected_sign:
                _LOGGER.warning("Setup request with invalid signature for uuid=%s", uuid)
                return self.json_message("invalid signature", 401)

        _LOGGER.info("Setup qrcode from miniprogram: %s", setup_data)

        this_data[uuid] = setup_data
        return self.json_message("ok")


class HuijianRemoveView(HuijianHttpView):
    url = "/api/huijian-ai/remove"
    name = "api:huijian-ai:remove"

    async def delete(self, request: web.Request):
        hass = request.app[KEY_HASS]
        if not (speak_id := request.query.get("speak_id")):
            return self.json_message("speak_id missing", 400)
        entry = await self.check_sign(request, speak_id)
        if not entry:
            return self.json_message("params error", 400)

        _LOGGER.info("Remove entry: %s", entry.entry_id)
        await hass.config_entries.async_remove(entry.entry_id)
        return self.json_message("ok")


class HuijianSetNameView(HuijianHttpView):
    url = "/api/huijian-ai/update/speakname"
    name = "api:huijian-ai:update:speakname"

    async def post(self, request: web.Request):
        hass = request.app[KEY_HASS]
        entry = await self.check_sign(request)
        if not entry:
            return self.json_message("params error", 400)
        data = await request.json() or {}
        if not (name := data.get("speak_name")):
            return self.json_message("speak_name missing", 400)
        mac = entry.data.get("mac")
        device_registry = dr.async_get(hass)
        device_entry = device_registry.async_get_device(
            connections={(dr.CONNECTION_NETWORK_MAC, mac)},
        )
        if not device_entry:
            return self.json_message("device not found", 400)
        device_registry.async_update_device(device_entry.id, name=name)
        hass.config_entries.async_update_entry(entry, title=name)
        return self.json_message("ok")


class HuijianTtsSttView(HuijianHttpView):
    requires_auth = True
    url = "/api/huijian-ai/tts-stt"
    name = "api:huijian-ai:tts-stt"

    async def get(self, request: web.Request):
        hass = request.app[KEY_HASS]
        message = request.query.get("message")

        default_tts = "tts.huijian_speech"
        default_stt = "stt.huijian_asr"
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            default_tts = entry.options.get(CONF_TTS_ENTITY_ID, default_tts)
            default_stt = entry.options.get(CONF_STT_ENTITY_ID, default_stt)

        tts_entity = request.query.get("tts_entity", default_tts)
        stt_entity = request.query.get("stt_entity", default_stt)

        try:
            stream = hass.data["tts_manager"].async_create_result_stream(
                engine=tts_entity,
                use_file_cache=not request.query.get("nocache"),
                options=request.query.get("options") or {},
            )
        except Exception as err:
            return self.json({"error": str(err)}, 400)
        stream.async_set_message(message)

        stt_entity = hass.data["stt"].get_entity(stt_entity)
        if not stt_entity:
            return self.json_message("stt entity not found", 400)

        from homeassistant.components import stt

        from .audio import async_convert_audio

        metadata = stt.SpeechMetadata(
            language="zh",
            format=stt.AudioFormats.WAV,
            codec=stt.AudioCodecs.PCM,
            bit_rate=stt.AudioBitRates.BITRATE_16,
            sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
            channel=stt.AudioChannels.CHANNEL_MONO,
        )
        converting = async_convert_audio(
            hass,
            stream.async_stream_result(),
            stream.extension,
            to_extension=metadata.format.value,
            to_sample_rate=metadata.sample_rate.value,
        )
        result = await stt_entity.async_process_audio_stream(metadata, converting)
        return self.json(
            {
                "text": result.text,
                "result": result.result,
            }
        )


def calculate_sign(uri, params, mac, salt):
    """
    签名算法:
    1. n = sha256(uri)
    2. 拼接参数字符串并计算 m = sha256(参数字符串)
    3. response = sha256(m + n + mac + salt)
    """
    # 步骤1: 计算 n = sha256(uri)
    n = hashlib.sha256(uri.encode("utf-8")).hexdigest()

    # 步骤2: 拼接参数并计算 m = sha256(参数字符串)
    # 将参数排序后拼接成 key=value 格式
    sorted_params = sorted(params.items(), key=lambda x: x[0])
    param_str = "&".join([f"{k}={v}" for k, v in sorted_params])
    m = hashlib.sha256(param_str.encode("utf-8")).hexdigest()

    # 步骤3: 计算最终摘要
    response_str = f"{m}{n}{mac}{salt}"
    return hashlib.sha256(response_str.encode("utf-8")).hexdigest()
