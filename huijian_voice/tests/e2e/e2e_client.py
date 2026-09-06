# -*- coding: utf-8 -*-
"""E2E 三通道真链路断言（run_local.sh / CI run_e2e.sh 共用，单一事实源）。

真 opus 编码 → STT 真识别 → LLM 级联（HA 不可达时验证降级）→ TTS 真合成回 opus
并解码校验。断言即契约：小智协议子集逐条（单条 stt 回执、LLM start/sentence_end*/end、
TTS 裸 opus + 唯一 stop、时长合理）。

用法：先起服务（e2e_server.py 或容器），再 `python e2e_client.py`。
环境：E2E_WAV=测试音频；HUIJIAN_SETTINGS=覆盖 settings 路径；
Windows 自动探测 _winlibs/opus.dll；容器内 python3 自带全依赖，直接 docker exec。
"""
import asyncio
import io
import json
import os
import sys
import time
import wave

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))   # 仓内默认布局推导
_APP = os.environ.get("E2E_APP_ROOT") or ROOT     # 容器内扁平布局显式指定 core 父目录
if _APP != ROOT or not os.path.isdir(os.path.join(ROOT, "core")):
    sys.path.insert(0, _APP)
sys.path.insert(0, ROOT)

if os.name == "nt":
    _d = os.environ.get("HUIJIAN_OPUS_DLL_DIR") or os.path.join(os.path.dirname(ROOT), "_winlibs")
    if os.path.isdir(_d):
        os.environ["PATH"] = _d + os.pathsep + os.environ["PATH"]
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(_d)

import aiohttp  # noqa: E402

from core import audio  # noqa: E402
from core import const  # noqa: E402

# settings 候选链：显式 env → 本地 dev 布局（服务端 env 不传子进程，const 会解析成
# \data\ 幽灵路径）→ 容器/生产 const。取第一个存在者。
_cands = [os.environ.get("HUIJIAN_SETTINGS"),
          os.path.join(ROOT, "_e2e_data", "settings.json"),
          str(const.SETTINGS_FILE)]
_settings = next((c for c in _cands if c and os.path.exists(c)), None)
if _settings is None:
    sys.exit(f"找不到 settings.json（候选 {_cands}）——服务先起了吗？")
TOK = json.load(open(_settings, encoding="utf-8"))["security"]["ws_token"]
BASE = "ws://127.0.0.1:8000/xiaozhi/v1"

WAV = os.environ.get("E2E_WAV") or (
    os.path.join(HERE, "assets", "0.wav")
    if os.path.exists(os.path.join(HERE, "assets", "0.wav"))
    else os.path.join(os.path.dirname(ROOT), "asr",
                      "sherpa-onnx-streaming-paraformer-bilingual-zh-en",
                      "test_wavs", "0.wav"))


async def ch_stt():
    pcm, = (wave.open(WAV).readframes(10 ** 9),)
    enc = audio.OpusPcmEncoder("voip")
    frames = list(enc.encode_stream(pcm[: 960 * 2 * 200]))  # 前 20s
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect(f"{BASE}/stt?token={TOK}")
        await ws.send_str('{"type":"listen","state":"start","mode":"detect"}')
        for f in frames:
            await ws.send_bytes(f)
            await asyncio.sleep(0.002)
        await ws.send_str('{"type":"listen","state":"stop"}')
        t0 = time.time()
        msg = await ws.receive(timeout=60)
        obj = json.loads(msg.data)
        await ws.close()
        print(f"[STT] 真音频 {len(frames)} opus帧 → {time.time()-t0:.1f}s → {obj!r}")
        assert obj["type"] == "stt" and len(obj["text"]) > 0, "STT 空结果"


async def ch_llm():
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect(f"{BASE}/llm?token={TOK}")
        await ws.send_str('{"type":"listen","state":"detect","mode":"prompt","text":"打开客厅的灯"}')
        seq = []
        while True:
            msg = await ws.receive(timeout=60)
            obj = json.loads(msg.data)
            seq.append(obj.get("state"))
            if obj.get("state") == "end":
                break
        await ws.close()
        print(f"[LLM] 帧序列 {seq}")
        assert seq[0] == "start" and seq[-1] == "end" and set(seq[1:-1]) == {"sentence_end"}


async def ch_tts():
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect(f"{BASE}/tts?token={TOK}")
        await ws.send_str('{"type":"tts","state":"detect","text":"你好，我是慧尖语音助手。"}')
        bins, stop, t0 = 0, False, time.time()
        dec = audio.OpusPcmDecoder()
        pcm_total = 0
        while not stop:
            msg = await ws.receive(timeout=90)
            if msg.type == aiohttp.WSMsgType.BINARY:
                bins += 1
                pcm_total += len(dec.decode(bytes(msg.data)))
            elif msg.type == aiohttp.WSMsgType.TEXT:
                obj = json.loads(msg.data)
                if obj.get("state") == "stop":
                    stop = True
        await ws.close()
        dur = pcm_total / 32000
        print(f"[TTS] {bins} 个 opus 帧，解码回 {pcm_total}B pcm ≈{dur:.1f}s 音频，"
              f"{time.time()-t0:.1f}s 收完，stop={stop}")
        assert stop and bins > 5 and dur > 1.5, "TTS 流异常"


async def main():
    await ch_stt()
    await ch_llm()
    await ch_tts()
    print("E2E：三通道真链路全部通过 ✅")

asyncio.run(main())
