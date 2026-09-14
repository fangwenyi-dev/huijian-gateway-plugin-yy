"""v1.0.70 深审修复批（②④⑤⑧⑨⑩⑪⑭）回归钉。

对应 15 项审计的加载项/集成侧收口（①在 v1.0.69 已钉）。跑法同仓内其它测试：
cd huijian_voice && python -m pytest tests -q
"""
import asyncio
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import const  # noqa: E402
from core.session import TtsSession  # noqa: E402


class _S:
    def __init__(self, d=None):
        self.d = dict(d or {})

    def get(self, k, dflt=None):
        return self.d.get(k, dflt)


# ── ②：detect 文本超上限截断 → 整流仍"自然收束"，但 stop 帧必须带 truncated ──
def test_text_cap_stop_carries_truncated():
    sent = []

    class _WS:
        closed = False

        async def send_str(self, x):
            sent.append(x)

        async def send_bytes(self, x):
            sent.append(x)

    class _Ctx:
        class tts:
            @staticmethod
            def stream_opus(text, engine_out=None):
                async def _gen():
                    yield b"\x01"
                    yield b"\x02"
                return _gen()
        settings = _S()

    async def scenario():
        s = TtsSession(_WS(), _Ctx)
        await s.on_text(json.dumps({"type": "tts", "state": "detect",
                                    "text": "啊" * (4001)}))
        if s._task:
            await asyncio.wait_for(s._task, 10)
    asyncio.run(scenario())

    stops = [json.loads(x) for x in sent if isinstance(x, str) and '"stop"' in x]
    assert stops, "cap 截断流也必须收 stop（契约：每 detect 必有 stop）"
    assert stops[-1].get("truncated") is True, (
        f"②回潮：>4000 字 cap 截断必须带 truncated 旗让集成侧 error 收口，"
        f"否则 HA 以原文哈希缓存缺尾音频（内存+落盘、跨重启）—— {stops[-1]!r}")


# ── ⑧：预算算术对账（改动任何一项数字都必须重算这条和式）──
def test_budget_arithmetic_under_client_60s():
    send = TtsSession._SEND_TIMEOUT_S
    total = const.TTS_STREAM_BUDGET_S + 2 * send
    assert send <= 3.0, f"发送闸必须 ≤3s（现 {send}）——它是求和项不是装饰"
    assert total <= 58.0, f"TTS 最坏收口 {total}s 必须 ≤ 58（客户端 60s - 2s 网络余量）"
    stt_total = const.STT_RESULT_BUDGET_S + 2 * send
    assert stt_total <= 58.0, f"STT 最坏收口 {stt_total}s 必须 ≤ 58"


# ── ⑤：TTS 合成/编码走自建池，与 ASR/TextCNN 的默认池隔离 ──
class _Store:
    def model_dir_for(self, key):
        return None

    def ensure(self, key):
        return False

    def lock_entry(self, key):
        return {}

    def voices_count_for(self, key):
        return 0


def test_tts_uses_dedicated_executor():
    from core.tts import TtsEngine

    e = TtsEngine(_S(), _Store())
    p = e._pool()
    assert isinstance(p, ThreadPoolExecutor)
    assert p is e._pool(), "池必须懒建后复用（每引擎一份，不随调用漂移）"
    assert getattr(p, "_max_workers", 99) <= 2, "专用池 2 工位封顶（1 合成 + 1 编码重叠）"

    src = (ROOT / "core" / "tts.py").read_text(encoding="utf-8")
    code = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())
    hits = re.findall(r"run_in_executor\(\s*None", code)
    assert not hits, f"⑤回潮：TTS 热路径再现 {len(hits)} 处默认池调用（播报风暴饿死 ASR）"


# ── ⑨⑩：ws_transport 源钉（基类判死窗 / send 闸 / 心跳计活动）────────────
def _load_ws_transport_src():
    return (ROOT / "custom_components" / "huijian_ai" / "huijian" /
            "ws_transport.py").read_text(encoding="utf-8")


def test_ws_transport_pins():
    src = _load_ws_transport_src()
    assert "_CONSUMER_HANDOFF_TIMEOUT_S: float | None = 30.0" in src, \
        "⑨回潮：基类交付判死窗回到 None = LLM 通道消费端消失后 reader 永挂"
    m = re.search(r"async def send_message\(self, message\):(.*?)\n    def ",
                  src, re.S)
    assert m and "_SEND_HANDOFF_TIMEOUT_S" in m.group(1), \
        "⑨回潮：send_message 失去交付超时闸"
    assert "_schedule_restart" in m.group(1), "⑨：交付超时必须走换连自愈而非静默吞"

    hb = re.search(r"async def _heartbeat_task.*?\n    (?:async def|def) ", src, re.S)
    assert hb, "找不到 _heartbeat_task"
    body = re.sub(r"#[^\n]*", "", hb.group(0))
    assert re.search(r"wait_for\(self\._current_ws\.ping\(\), 10\)\s*\n\s*"
                     r"self\.update_activity_time\(\)", body), \
        "⑩回潮：ping 成功不计活动 → 健康闲置链路仍被 180s 判死，首唤付冷握手"


def test_ws_transport_send_message_timeout_behavior():
    """行为钉：writer 消费者消失（send 永挂）时 send_message 必须有界返回并触发自愈换连。"""
    src = _load_ws_transport_src()
    fn = re.search(r"    async def send_message\(self, message\):(.*?)\n    (?=async def|def |\Z)",
                   src, re.S)
    helper = re.search(r"    def _schedule_restart\(self, reason:.*?\n    (?=async def|def |\Z)",
                       src, re.S)
    assert fn and helper, "⑨源码结构变化，行为钉需随迁（send_message/_schedule_restart 提取失败）"

    class _Warn:
        def __init__(self):
            self.warns = []

        def warning(self, *a):
            self.warns.append(a)

        def exception(self, *a):
            self.warns.append(a)

    body = (
        "class _T:\n"
        "    _SEND_HANDOFF_TIMEOUT_S = 0.05\n"
        "    hass = None\n"
        "    def __init__(self):\n"
        "        self.logger = _Warn()\n"
        "        self.calls = 0\n"
        "    def update_activity_time(self):\n"
        "        self.calls += 1\n"
        "    async def restart_connection(self, reason=''):\n"
        "        self.restarted = reason\n"
        + fn.group(0) + "\n" + helper.group(0)
    )
    ns = {"asyncio": asyncio, "_Warn": _Warn}
    exec(compile(body, "<⑨behav>", "exec"), ns)
    t = ns["_T"]()

    class _HangWriter:
        async def send(self, item):
            await asyncio.Event().wait()   # 消费者永不出现 → 永挂

    async def scenario():
        t._send_writer = _HangWriter()
        await asyncio.wait_for(t.send_message(b"x"), 2.0)   # 有界完成
        await asyncio.sleep(0.05)          # 让 _schedule_restart 的后台任务起跑
    asyncio.run(scenario())
    assert getattr(t, "restarted", "") == "send stalled", "⑨：交付超时后未触发自愈换连"
    assert t.logger.warns, "⑨：自愈动作必须留 WARN（不静默）"


# ── ⑭：fork assist_satellite 下行流异常支必须收口 ──
def test_fork_stream_tts_early_returns_converge():
    src = (ROOT / "custom_components" / "huijian_ai" /
           "assist_satellite.py").read_text(encoding="utf-8")
    assert "def _converge_response()" in src, "⑭尾收口助手消失（早退支会重新跳过收口）"
    fn = re.search(r"async def _stream_tts_audio.*?(?=\n    async def )", src, re.S)
    assert fn, "找不到 _stream_tts_audio"
    body = fn.group(0)
    converges = len(re.findall(r"_converge_response\(\)", body))
    assert converges >= 3, (
        f"⑭回潮：非 WAV / WAV 不可播 / 下行流异常三条早退支都要收口，实见 {converges}")
    m = re.search(r"except asyncio\.CancelledError.*?(?=\n        finally:)", body, re.S)
    assert m and "_converge_response" not in m.group(0), \
        "⑭：CancelledError 支必须维持原语义（打断方自会收口，此处豁免）"


# ── ⑪：服务端会话闸在位（watchdog 判据钉在 test_release_consistency）──────
def test_ws_server_session_cap_in_place():
    src = (ROOT / "core" / "ws_server.py").read_text(encoding="utf-8")
    assert "_MAX_SESSIONS" in src and '"too many sessions"' in src, \
        "⑪回潮：sessions 无上限 = 事件循环卡死场景的泄漏放大器"
    assert re.search(r"len\(ctx\.sessions\) >= _MAX_SESSIONS", src), "闸须在 ws.prepare 之前"
    assert "web.WebSocketResponse(heartbeat=None" in src, \
        "⑪边界钉：服务端 heartbeat 不许开启——:8000 还有嵌入式/小程序消费端，无 PONG 保证"
