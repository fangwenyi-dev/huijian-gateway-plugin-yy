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
# 上排队"的窄窗口，不为一条仍在流的旧流干等（加载项最坏收口 58s，v1.0.83 口径）。
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
    # v1.0.83（修#1 三端算术，见加载项 const ⑧块）：整轮总闸。timeout 参数
    # 自本版起是**逐帧间隙窗**（收到任何一条消息即重置）——只杀停滞、不杀
    # "慢而持续"的长播报；永动僵尸流由本总闸有界收口。
    # 对账链：加载项间隙 52+2×3 ≤ 60-2（本类逐帧窗）；加载项整轮 660+2×3
    # ≤ 720-2（本闸）；720 < 1200（固件 T_LIVE_HARD_CAP）——HA 链永远先于
    # 设备收口，协议截断先于看门狗。
    _ROUND_TOTAL_BUDGET_S = 720.0

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
        # 收口（加载项最坏收口 58s，v1.0.83 口径）后出声。
        self._request_lock = asyncio.Lock()
        # v1.0.48（P5）：服务端音色指纹——tts 通道建连欢迎帧 + web 保存推送
        # 双通道递送（base._on_server_settings tap）。restart_connection 不换
        # transport 对象，指纹跨重连存续；与加载项失联期间配置漂移由重连
        # 欢迎帧补正。
        self.voice_fp: str | None = None

    def _on_server_settings(self, data) -> None:
        fp = str(data.get("voice_fp") or "").strip()
        if fp and fp != self.voice_fp:
            _LOGGER.info("huijian TTS 音色指纹更新: %s → %s（HA 缓存键轮换）",
                         self.voice_fp, fp)
            self.voice_fp = fp

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

        v1.0.69（根因①，2026-09-14 现场「exit cancel scope in a different
        task」三处炸点=播报整句静音的根治）：本生成器的任一 anyio cancel
        scope **绝不横跨 yield**。旧实现 `with anyio.fail_after(timeout)` 把
        `yield data` 包在 scope 内——scope 的任务仿射绑定在「驱动到首块」的
        任务上（tts.py:287 peek 在 provider 调用任务 A 进入），而 HA 的
        TTSCache 用 `async_create_background_task(_load_data_into_cache)`
        （任务 B）续跑并在 B 收口 → `__exit__` 与 `__enter__` 异任务 →
        anyio 抛 RuntimeError → 被下方 except 吞成「读取失败」→ 整句音频
        作废（最小跨任务复现钉在 tests，本机 py313+anyio 实证报错原文与
        现场一字不差）。现改为单调 deadline + 逐条 receive 独立短 scope：
        enter/exit 恒在同一次 `__anext__` 步内（中间无 yield），yield 点
        零存活 scope，任务切换安全。
        v1.0.83（修#1）：`timeout` 自本版起是**最大帧间隙窗**（每收到一条
        消息即重置，只杀停滞）；整轮另有 `_ROUND_TOTAL_BUDGET_S` 总闸兜
        永动僵尸流。旧"整轮 60s 墙钟"会把慢而持续的长播报腰斩（truncated/
        timeout → error 收口 → 设备只播前半段），与加载项 52s 整流、固件
        v2.1.42 帧间隙心跳语义三端对账见类常量注释。
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
                deadline = time.monotonic() + timeout
                round_deadline = time.monotonic() + self._ROUND_TOTAL_BUDGET_S
                while True:
                    now = time.monotonic()
                    if now >= round_deadline:
                        # v1.0.83：整轮总闸——逐帧窗心跳语义下必须有界收口的
                        # 第二道（永动僵尸流），正常播报远够不到。
                        self.logger.warning(
                            "TTS 整轮总闸超时（%ss）: %r",
                            self._ROUND_TOTAL_BUDGET_S, text[:40])
                        yield Dict(error="Response timeout")
                        return
                    remaining = min(deadline, round_deadline) - now
                    if remaining <= 0:
                        # 帧间隙窗耗尽（timeout 语义=v1.0.83 起为逐帧停滞判定）
                        yield Dict(error="Response timeout")
                        return
                    eof = False
                    data = None
                    # 短 scope 内只有 receive、没有 yield：enter/exit 恒同任务
                    with anyio.move_on_after(remaining) as _scope:
                        try:
                            data = await self._recv_reader.receive()
                        except (anyio.EndOfStream, anyio.ClosedResourceError):
                            eof = True
                    if _scope.cancelled_caught:
                        yield Dict(error="Response timeout")
                        return
                    # 任何一条消息到达 = 服务端还活着：间隙窗重置（心跳语义）
                    deadline = time.monotonic() + timeout
                    if eof:
                        # v1.0.65（TTS 深审 T1）：EOF=连接被静默关闭（旧版
                        # for-else 支）。不 error 收口，截断音频会被 core 缓存
                        # 任务当「正常收束」写进无 TTL 盘缓存，同句永久缺尾
                        # ——与 v1.0.55 truncated-stop 同毒、不同入口；不变量
                        # 「非 stop 收口必须异常收口」自此全支路成立。
                        self.logger.warning(
                            "TTS 流未收到 stop 即断流（按错误收口，防缓存毒化）: %r",
                            text[:40],
                        )
                        yield Dict(error="huijian TTS 流提前断开（未收到 stop）")
                        return
                    if isinstance(data, bytes):
                        yield data  # scope 外 yield：不携带任何存活 cancel scope
                        continue
                    if getattr(data, "state", None) == "stop":
                        clean = True
                        if getattr(data, "truncated", None):
                            # v1.0.55（深审定案②）：加载项声明"半截音频"
                            # （整流超预算/合成停滞/云端半途断流收束）。必须以
                            # error 收口让实体 raise——HA core 只在异常时 pop
                            # 缓存；按普通 stop 收口=截断音频进消息哈希缓存
                            # （内存+落盘、跨重启），同句永久缺尾且不自愈。
                            # clean=False 顺带断连清算，下一轮全新连接开始。
                            clean = False
                            self.logger.warning(
                                "TTS 被服务端截断（半截音频按错误收口，防缓存毒化）: %r",
                                text[:40],
                            )
                            yield Dict(error="huijian TTS 音频被服务端截断")
                        return
                    self.logger.info("Received unknown message: %s", data)
            except TimeoutError:
                yield Dict(error="Response timeout")
            except anyio.get_cancelled_exc_class():
                raise
            except Exception as err:  # reader 被关闭等
                self.logger.warning("TTS 对话读取异常: %s", err)
                # v1.0.65（T1 第二支路）：ClosedResourceError 等被吞后同样不得
                # 以「正常耗尽」收口——error yield 让实体 raise、core pop 缓存。
                yield Dict(error=f"huijian TTS 读取失败: {err}")
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
