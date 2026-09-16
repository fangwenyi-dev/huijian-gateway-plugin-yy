import asyncio
import logging
import random
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

# v1.0.88（下行流标识 Stage 1）：加载项在建连欢迎帧里申报的协议代次达到本值，
# 本类才启用边带身份（detect 带 rid → 对端回声 stream_start/stop 同 rid）。
# 未达标（旧加载项/未收到欢迎帧）一律走旧语义——方向恒为 fail-open：宁可少
# 一档保护，也绝不因"等一个不会来的 ack"把播报哑掉。
_TTS_PROTO_STREAM_ID = 2
# stream_start ack 等待预算。加载项在 detect 之后**立刻**回 ack（不等合成，
# session.py 的 stream_start 先于整流），故正常路径亚秒级；真超 3s 说明对端
# 是"声称支持却没发 ack"的坏形态 → fail-open 全收（等价 v1.0.87 行为）并点名。
_SYNC_BUDGET_S = 3.0


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
        # ── v1.0.88：轮次认领（claim）——把"谁有权收这条连接的帧"变成身份 ──
        # 旧语义（v1.0.45）是"持 _request_lock 者独占这条对话"，但锁只在
        # stream() 自己跑的时候有效：上一轮的消费者被 cancel 后没走 finally
        # （core 的缓存任务被撤销时确实可能不等 aclose），锁当场自由，而加载项
        # 的旧流还在往同一条连接吐帧——下一轮要么把残帧当自己的（=播报头部混
        # 半句旧音频，现场主诉），要么靠 _drain_stale 的时间预算猜（只清得掉
        # "已排队"的，清不掉"稍后才到"的）。
        # 现规则：任一时刻至多一个认领者；非认领期到达的消息由基类交付钩子
        # **就地丢弃**（连 reader 都不进），认领者收口时交还认领并按既有承诺
        # 换连清算。于是"下一轮看到的第一个 ack 就是自己的 ack"成立。
        self._round_active = False
        # 认领时的连接代次：轮进行中连接被换掉 = 本轮 detect 随旧连接作废
        # （新加载项会话从没见过它），立刻按错误收口，不再干等 60s 间隙窗。
        self._claim_conn_gen = -1
        self._unclaimed_total = 0
        # 加载项欢迎帧申报的协议代次（0=未知/旧加载项 → 完全旧语义）。
        self._proto = 0
        # rid=0 恒为 legacy 哨兵 ⇒ 计数器从随机基起并跳过 0（换连不重置：旧
        # 连接残料已被就地丢弃/断连清算，但没必要冒险让两连同值撞号）。
        self._rid = random.getrandbits(31) | 1
        # v1.0.48（P5）：服务端音色指纹——tts 通道建连欢迎帧 + web 保存推送
        # 双通道递送（base._on_server_settings tap）。restart_connection 不换
        # transport 对象，指纹跨重连存续；与加载项失联期间配置漂移由重连
        # 欢迎帧补正。
        self.voice_fp: str | None = None

    def _next_rid(self) -> int:
        rid = self._rid
        self._rid = rid + 1 if rid < 0xFFFFFFFF else 1
        return rid

    async def _create_streams(self):
        await super()._create_streams()
        # 协商结果属"这一条连接"：换连即归零，等新连接的欢迎帧重新申报。
        # 认领位**不**在这里清——若本轮还活着，清它等于把它的帧也丢掉。
        self._proto = 0

    def _on_incoming(self, item):
        """交付前过滤：无人认领 ⇒ 就地丢弃（基类说明，TTS 通道专用）。"""
        if self._round_active:
            return False
        # 到达时没有认领者 = 上一轮的消费者已经消失（被取消/超时后没走收口）。
        # 这条消息属**那一轮**，漂到下一轮头上就是 v1.0.45 那种"张冠李戴"。
        self._unclaimed_total += 1
        if self._unclaimed_total == 1 or self._unclaimed_total % 20 == 0:
            self.logger.warning(
                "TTS 通道无人认领，就地丢弃消息（旧轮消费端已消失，累计 %d 条）"
                "——残料不再可能混进下一轮头部", self._unclaimed_total)
        return True

    def _claim_round(self) -> int:
        """取得本轮认领权（必须在 _request_lock 内调用）。返回连接代次。

        发现上一轮的认领未释放 ⇒ 那个消费者永远不会再收这条流了，当场接管：
        先按既有承诺换连清算（不等待——等一个不会来的释放=必然停滞），再认领。
        """
        if self._round_active:
            self.logger.warning(
                "TTS 上一轮认领未释放（消费端未走收口）——本轮接管并断连清算残留")
            self._round_active = False
            if self.is_connected:
                self._schedule_restart("TTS stale claim taken over")
        self._round_active = True
        return self._conn_gen

    def _release_round_claim(self) -> None:
        self._round_active = False

    def _on_server_settings(self, data) -> None:
        fp = str(data.get("voice_fp") or "").strip()
        if fp and fp != self.voice_fp:
            _LOGGER.info("huijian TTS 音色指纹更新: %s → %s（HA 缓存键轮换）",
                         self.voice_fp, fp)
            self.voice_fp = fp
        # v1.0.88：欢迎帧同处申报协议代次（缺失=旧加载项 → 0 → 完全旧语义）
        try:
            proto = int(data.get("tts_proto") or 0)
        except (TypeError, ValueError):
            proto = 0
        if proto != self._proto:
            _LOGGER.info("huijian TTS 协议代次: %s → %s（下行流标识%s）",
                         self._proto, proto,
                         "启用" if proto >= _TTS_PROTO_STREAM_ID else "不启用")
            self._proto = proto

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

        v1.0.88（下行流标识 Stage 1，第四重保证 = 身份配对）：加载项欢迎帧申报
        `tts_proto>=2` 时，detect 带 `rid`（本类 mint，谁可能被污染谁 mint），
        对端在本流任何帧之前回声 `{"state":"stream_start","rid":N}`、收口 stop
        带同 rid。本方法据此把"这帧是不是我的"从**猜**改成**判**：
          • 未配对到本流 ack 前到达的 binary → 丢弃（旧流残段的唯一入口）；
          • rid 不配对的 stop → 不当作本轮收口（否则半截音频以"正常收口"进
            core 的无 TTL 盘缓存 = v1.0.55 定案②的毒化形态回潮）；
          • 认领位（`_claim_round`）期间之外到达的一切消息在基类交付点就被丢，
            连队列都不进 → 下一轮看到的第一个 ack 必然是它自己的 ack。
        未申报（旧加载项）/ ack 迟迟不来（坏形态）→ fail-open 退回旧语义：
        方向恒为"宁可少一档保护，也不把播报哑掉"。轮中连接被换掉 = 本轮 detect
        随旧连接作废，立即 error 收口，不再干等 60s 间隙窗。

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
            # v1.0.88：本轮认领这条连接（换连接连清算），并记下认领时的代次
            claimed_gen = self._claim_round()
            rid = self._next_rid() if self._proto >= _TTS_PROTO_STREAM_ID else 0
            acked = rid == 0            # 旧协议：无需配对，视为已同步
            # paired=身份**已证实**（见到本流 stream_start）；acked 也可能是
            # fail-open 撑出来的。两者必须分开：fail-open 之后不带 rid 的 stop
            # 要按旧语义认（否则把"少一档保护"做成"整段静音"），而**明确属于别的
            # rid** 的 stop 任何时候都不认——它指名道姓不是本轮的。
            paired = rid == 0
            sync_deadline = time.monotonic() + _SYNC_BUDGET_S
            stale_frames = 0            # 本轮丢掉的"未配对旧流帧"计数（收口对账）
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
                                # 边带身份：仅协商成功才带上（旧加载项未知键忽略）
                                **({"rid": rid} if rid else {}),
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
                    if self._conn_gen != claimed_gen:
                        # 轮进行中连接被换掉：本轮 detect 从没进过新会话的耳朵，
                        # 再等只是白等（旧形态要等满 60s 间隙窗才报错）
                        self.logger.warning(
                            "TTS 轮进行中断连换连（detect 随旧连接作废）: %r",
                            text[:40])
                        yield Dict(error="huijian TTS 连接已更换")
                        return
                    if not acked and now >= sync_deadline:
                        # fail-open：对端声称支持却没发 ack（坏形态）——继续等
                        # 只会把一次播报拖成静音，退回 v1.0.87 的旧语义并点名
                        self.logger.warning(
                            "TTS 未等到本流 stream_start(rid=%s) 回声，%.0fs 到点"
                            "——fail-open 收后续帧（等价旧行为）: %r",
                            rid, _SYNC_BUDGET_S, text[:40])
                        acked = True
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
                        if not acked:
                            # v1.0.88：还没跟本流 ack 配对，这一帧就不属于本轮
                            # （旧流残段/上一轮漏网帧）——就地丢，绝不进新流头部
                            stale_frames += 1
                            if stale_frames == 1 or stale_frames % 20 == 0:
                                self.logger.warning(
                                    "TTS 丢弃未配对帧（本轮 rid=%s 未收到 stream_start，"
                                    "累计 %d 帧）: %r", rid, stale_frames, text[:40])
                            continue
                        yield data  # scope 外 yield：不携带任何存活 cancel scope
                        continue
                    state = getattr(data, "state", None)
                    if state == "stream_start":
                        # 身份配对点：只有同 rid 才算"本流开始"；旧流晚到的 ack
                        # 配不上本轮，等它=白等，直接忽略
                        if int(getattr(data, "rid", 0) or 0) == rid:
                            acked = paired = True
                        elif rid:
                            self.logger.info(
                                "TTS 忽略旧流 ack（来流 rid=%s 本轮 rid=%s）",
                                getattr(data, "rid", None), rid)
                        continue
                    if state == "stop":
                        if rid:
                            got = int(getattr(data, "rid", 0) or 0)
                            if got and got != rid:
                                # 明确属于另一条流：任何时候都不当本轮收口。
                                # 半截音频以"正常收口"交回 core = v1.0.55 定案②
                                # 盘缓存毒化回潮（内存+落盘、跨重启命中、不自愈）
                                self.logger.warning(
                                    "TTS 忽略旧流 stop（rid=%s ≠ 本轮 %s），继续等本流收口: %r",
                                    got, rid, text[:40])
                                continue
                            if not got and not acked:
                                # 本流还没配对，这条 stop 又不带身份 → 属旧格式
                                # /旧流；fail-open 之后（acked）才按旧语义认
                                self.logger.warning(
                                    "TTS 忽略未配对 stop（本轮 rid=%s 尚未收到 stream_start）: %r",
                                    rid, text[:40])
                                continue
                        if acked and not paired:
                            self.logger.warning(
                                "TTS 按 fail-open 语义收口（本轮 rid=%s 始终没等到"
                                " stream_start，对端形态异常）: %r", rid, text[:40])
                        elif rid and not acked:
                            self.logger.warning(
                                "TTS 收到配对 stop 但未见 stream_start 回声"
                                "（rid=%s，按收口处理）: %r", rid, text[:40])
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
                # v1.0.88：先交还认领权（此后这一条连接上属于本轮/无主的消息在
                # 基类交付点就被丢弃），再按既有承诺决定要不要换连清算。
                self._release_round_claim()
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
