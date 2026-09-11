import asyncio
import logging
import time

import anyio
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from . import Dict, EntryAuthFailedError, get_entry_data
from .ws_transport import WsTransport

_LOGGER = logging.getLogger(__name__)
ATTR_ENDPOINT = "tts_endpoint"
ATTR_TRANSPORT = "tts_transport"

# v1.0.45：detect 前排残料的兜底预算。主清理通道是 restart_connection
# （取消即断连，残留随旧连接物理消失）；本兜底只处理"残料恰好已在 reader
# 上排队"的窄窗口，不为一条仍在流的旧流干等（加载项预算 55s）。
_STALE_DRAIN_BUDGET_S = 1.0


def get_entry_transport(hass: HomeAssistant, entry: ConfigEntry) -> "TtsTransport":
    """Set up from a config entry."""
    endpoint: str | None = entry.data.get(ATTR_ENDPOINT)
    if not endpoint:
        raise EntryAuthFailedError(hass, entry)

    this_data: dict = get_entry_data(hass, entry)
    transport: TtsTransport | None = this_data.get(ATTR_TRANSPORT)
    if transport and transport.endpoint == endpoint and transport.available:
        return transport

    _LOGGER.info(
        "Creating new TtsTransport for entry: %s %s", entry.entry_id, entry.title
    )
    transport = TtsTransport(hass, entry, endpoint, ATTR_ENDPOINT, _LOGGER)
    this_data[ATTR_TRANSPORT] = transport
    return transport


class TtsTransport(WsTransport):
    _transport_type = "tts"
    # 本通道服务端契约：无 detect 不推帧、stream() 以锁保证恰好一个消费者，
    # 交付超时唯一可能就是消费端已消失（见基类注释，其它通道保持无限等）。
    _CONSUMER_HANDOFF_TIMEOUT_S = 5.0

    def __init__(self, hass, entry, endpoint, attr_endpoint, logger=None):
        super().__init__(hass, entry, endpoint, attr_endpoint, logger)
        # v1.0.45（播报"经常少字/整段静音"的错位毒化根治）：一条 WS 连接就是
        # 一条对话管道，卫星管线 / tts.speak / 场景播报都经这一条。旧实现无
        # 序列化：并发两个 detect 在加载项侧互相顶替（后到者令前流静默作废、
        # 不发 stop），两个消费端再共抢同一帧流——先闭口的拿到"混着两句话的
        # 半截音频"（现场=播报缺字），没拿到 stop 的那个 60s 超时（现场=整段
        # 静音），且被取消消费留下的残帧残 stop 会继续毒化后续每一轮，形成
        # 持久的"张冠李戴"错位（台架实证：请求1收 20 帧杂流+stop、请求2
        # TIMEOUT）。整段对话上锁串行是唯一解；等锁的下一句最迟在前一句
        # 收口（加载项 55s 预算）后出声。
        self._request_lock = asyncio.Lock()

    async def _drain_stale(self) -> None:
        """detect 前排净连接上已排队的残帧/残 stop（兜底，见常量注释）。

        读到一条 stop 即认为上一轮残料收口完毕；预算内读空也照常放行。
        """
        drained = 0
        saw_stop = False
        deadline = time.monotonic() + _STALE_DRAIN_BUDGET_S
        while time.monotonic() < deadline:
            try:
                data = self._recv_reader.receive_nowait()
            except (anyio.WouldBlock, anyio.EndOfStream, anyio.ClosedResourceError):
                break
            drained += 1
            if not isinstance(data, bytes) and getattr(data, "state", None) == "stop":
                saw_stop = True
                break
        if drained:
            self.logger.warning(
                "TTS 连接上清掉上一轮残留：%d 条消息（含 stop=%s）——此前有播报"
                "被中途取消，已按错位风险处理",
                drained,
                saw_stop,
            )

    async def stream(self, text: str, timeout: int = 60):
        """跑一整轮 {"tts","detect"} → 裸 opus 帧… → {"tts","stop"} 对话。

        yield bytes = 一帧 opus；yield Dict(error=…) = 让消费端 raise。
        三重保证：
        - `_request_lock`：同连接同时只允许一轮对话（见 __init__ 注释）；
        - `_drain_stale`：起 detect 前排净已排队的残料；
        - finally 判卷：凡不是以 stop 收口（消费端取消/超时/断连），
          `restart_connection` 断连清算残留，下一轮在全新连接上开始。
        消费端务必用 finally 里的 `aclose()` 确定性关闭本生成器
        （tts.py 已接），不要赌 GC 时机。
        """
        async with self._request_lock:
            if not await self.ensure_connected():
                yield Dict(error="WebSocket connection unavailable")
                return
            clean = False
            try:
                await self._drain_stale()
                try:
                    # ensure_connected 与发送之间的断连窗口：writer 侧 send
                    # 同样必须可超时（否则卡死持锁，全通道永堵）。
                    await asyncio.wait_for(
                        self.send_message(
                            {
                                "type": "tts",
                                "state": "detect",
                                "text": text,
                            }
                        ),
                        timeout=10,
                    )
                except Exception as err:
                    yield Dict(error=f"Send detect failed: {err}")
                    return
                with anyio.fail_after(timeout):
                    async for data in self._recv_reader:
                        if isinstance(data, bytes):
                            yield data
                        elif data.state == "stop":
                            clean = True
                            break
                        else:
                            self.logger.info("Received unknown message: %s", data)
            except TimeoutError:
                yield Dict(error="Response timeout")
            except anyio.get_cancelled_exc_class():
                raise
            except Exception as err:  # reader 被关闭等
                self.logger.warning("TTS 对话读取异常: %s", err)
            finally:
                if not clean:
                    await self.restart_connection(
                        "TTS 对话未以 stop 收口（取消/超时/断连），断连清算残留"
                    )

    async def async_remove_entry(self):
        entry = self.entry
        this_data: dict = get_entry_data(self.hass, entry)
        transport: TtsTransport | None = this_data.pop(ATTR_TRANSPORT, None)
        self.logger.info(
            "Remove entry from TTS transport: title=%s id=%s",
            entry.title,
            entry.entry_id,
        )
        if transport:
            await transport.stop("Remove entry")
