"""v1.0.73 归因链四钉（黑洞定凶手专用）回归。

场景（2026-09-14 16:15 三方日志案）：下行半开时"HA 没答"与"HA 答了但死在路上"
在旧日志里不可区分。四钉：
  ① handle_pipeline_start 入口 INFO（收到请求+应答发出）
  ② VA 订阅建立/解除 INFO（订阅窗口卡时刻）
  ③ _drain_stale_pipeline 拆轮者自报（取消旧轮+年龄）
  ④ SttTransport.recognize 被外部取消归因（发送相/等转录相）
下次黑洞：①有④有 → 下行半开；①无②在 → 请求未达；③④成对 → 正常拆轮噪声。
"""
import asyncio
import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SATELLITE = (ROOT / "custom_components" / "huijian_ai" /
             "assist_satellite.py").read_text(encoding="utf-8")
STT = (ROOT / "custom_components" / "huijian_ai" / "huijian" /
       "stt_transport.py").read_text(encoding="utf-8")


class _Log:
    def __init__(self):
        self.infos = []
        self.warns = []

    def info(self, fmt, *a):
        self.infos.append(fmt % a if a else fmt)

    def warning(self, fmt, *a):
        self.warns.append(fmt % a if a else fmt)

    def debug(self, *a, **k):
        pass

    def exception(self, *a, **k):
        self.warns.append(str(a))


def _extract(src: str, header: str) -> str:
    m = re.search(rf"(    (?:async )?def {header}.*?)(?=\n    (?:async )?def |\nclass |\Z)",
                  src, re.S)
    assert m, f"提取失败：{header}（结构漂移，钉需随迁）"
    return m.group(0)


# ── 源级钉：四行齐全 + 关键顺序 ────────────────────────────────────────────
def test_four_attribution_lines_present():
    assert "收到设备开轮请求" in SATELLITE and "应答随后发出" in SATELLITE, "①钉丢失"
    assert "VA 订阅建立" in SATELLITE and "VA 订阅解除" in SATELLITE, "②钉丢失"
    assert "新一轮接管，取消旧 pipeline 轮" in SATELLITE, "③钉丢失"
    assert "STT 事务被外部取消" in STT, "④钉丢失"
    assert STT.count("STT 事务被外部取消（") >= 2, "④钉必须发送相/等转录相各一枚"


def test_start_info_precedes_first_await():
    """①钉打在 impl 首行（先于任何 await）——入口晚于 await 就没有'必被应答'
    的证力；wrapper 必须保持零新增全局引用（v1043 提取执行纪律）。"""
    impl = _extract(SATELLITE, "_handle_pipeline_start_impl")
    i_info = impl.find("收到设备开轮请求")
    i_await = impl.find("await ")
    assert 0 < i_info < i_await, "①钉 INFO 必须先于 impl 内任何 await"
    wrap = _extract(SATELLITE, "handle_pipeline_start")
    assert "收到设备开轮请求" not in wrap, "①钉不得回流 wrapper"
    assert "VoiceAssistantCommandFlag" not in wrap, "wrapper 引入新全局=提取执行测试炸"


def test_cancel_handlers_re_raise():
    """④钉吞取消=任务取消纪律被破：每个 CancelledError 支必须 raise。"""
    fn = _extract(STT, "recognize")
    for m in re.finditer(r"except asyncio\.CancelledError:(.*?)(?=\n            except |\n            finally:)", fn, re.S):
        body = m.group(1)
        assert "raise" in body, "④钉存在吞取消支"
    assert fn.count("except asyncio.CancelledError:") >= 2


# ── 行为钉：①所在链——wrapper 纯直通三态（成功 port / 异常 None / 取消上抛）──
# ①钉 INFO 本体在 impl 首行，由 test_start_info_precedes_first_await 源级钉锁死
# （impl 依赖 VoiceAssistantFeature/tts 等整堆符号，行为提取不现实；wrapper
#  提取执行是 v1043 立的纪律，①入 impl 正是为了让 wrapper 保持可提取纯直通）。
def test_wrapper_pure_passthrough_three_states():
    ns = {"_LOGGER": _Log(), "asyncio": asyncio,
          "VoiceAssistantAudioSettings": object}  # wrapper 签名注解，def 时求值
    src = _extract(SATELLITE, "handle_pipeline_start")

    def build(impl_src, logger=None):
        body = ("class _S:\n    async def _handle_pipeline_start_impl(self, c, f, a, w):\n"
                + impl_src + "\n" + src)
        nsx = dict(ns)
        if logger is not None:
            nsx["_LOGGER"] = logger
        exec(compile(body, "<①wrapper>", "exec"), nsx)
        return nsx["_S"]()

    assert asyncio.run(build("        return 0\n")
                       .handle_pipeline_start("conv", 1, None, "w")) == 0
    lg_err = _Log()
    assert asyncio.run(build("        raise RuntimeError('boom')\n", lg_err)
                       .handle_pipeline_start("conv", 1, None, "w")) is None
    assert lg_err.warns, "异常兜底必须 exception 留痕"

    async def scenario():
        try:
            await build("        raise asyncio.CancelledError\n") \
                .handle_pipeline_start("conv", 1, None, "w")
            return "returned"
        except asyncio.CancelledError:
            return "cancelled"
    assert asyncio.run(scenario()) == "cancelled", "①链不得吞取消（v1043 同律）"


# ── 行为钉：③ 拆轮者自报年龄 ──────────────────────────────────────────────
def test_drain_reports_cancellation():
    lg = _Log()
    ns = {"_LOGGER": lg, "asyncio": asyncio}
    body = ("class _S:\n    _pipeline_task_t0 = 0.0\n"
            + _extract(SATELLITE, "_drain_stale_pipeline"))
    exec(compile(body, "<③behav>", "exec"), ns)
    s = ns["_S"]()

    async def scenario():
        async def hang():
            await asyncio.Event().wait()
        old = asyncio.create_task(hang())
        await asyncio.sleep(0)
        s._pipeline_task_t0 = asyncio.get_running_loop().time() - 5.0
        ok = await s._drain_stale_pipeline(old, timeout=2.0)
        return ok
    ok = asyncio.run(scenario())
    assert ok is True, "③钉语义：旧轮取消后应报收口成功"
    hit = [x for x in lg.infos if "取消旧 pipeline 轮" in x]
    assert hit and "在途 5" in hit[0], f"③钉未报年龄：{lg.infos}"


# ── 行为钉：④ recognize 取消归因（等转录相）────────────────────────────────
def _stt_send_timeout():
    m = re.search(r"_SEND_TIMEOUT_S = ([\d.]+)", STT)
    assert m, "stt_transport 常量漂移"
    return float(m.group(1))


def test_recognize_cancelled_attribution():
    # CI 无 anyio（v1.0.69 scope 钉同规）：recognize 的 fail_after 需真 anyio，
    # 函数级 importorskip——源级钉不陪跳（CI 上照跑）。
    anyio = pytest.importorskip("anyio", reason="recognize 需真 anyio（取消语义）")
    lg = _Log()
    ns = {"asyncio": asyncio, "anyio": anyio, "time": time,
          "_SEND_TIMEOUT_S": _stt_send_timeout(), "_LOGGER": lg}
    body = ("class _S:\n"
            "    def __init__(self):\n"
            "        self.logger = lg_holder['lg']\n"
            "        self._request_lock = asyncio.Lock()\n"
            "        self.restarted = ''\n"
            "    async def ensure_connected(self):\n"
            "        return True\n"
            "    def _drain_stale(self):\n"
            "        return 0\n"
            "    async def send_hello(self):\n"
            "        return None\n"
            "    async def send_message(self, m):\n"
            "        return None\n"
            "    async def restart_connection(self, reason=''):\n"
            "        self.restarted = reason\n"
            "    async def _hang_iter(self):\n"
            "        if False:\n"
            "            yield None\n"
            + _extract(STT, "recognize"))
    ns["lg_holder"] = {"lg": lg}
    # _hang_iter 需成为 async iterator：包一层
    exec(compile(body, "<④behav>", "exec"), ns)
    s = ns["_S"]()

    class _Hang:
        def __init__(self):
            self.ev = asyncio.Event()
        def __aiter__(self):
            return self
        async def __anext__(self):
            await self.ev.wait()
            raise StopAsyncIteration
    s._recv_reader = _Hang()

    async def chunks():
        yield b"frame-1"

    async def scenario():
        t = asyncio.get_running_loop().create_task(s.recognize(chunks(), timeout=30))
        await asyncio.sleep(0.1)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            return
        raise AssertionError("④钉支不得吞掉取消（recognize 必须以 cancelled 收尾）")
    asyncio.run(scenario())
    hit = [x for x in lg.warns if "STT 事务被外部取消" in x]
    assert hit and "等转录相" in hit[0] and "已发1帧" in hit[0], f"④钉未归因：{lg.warns}"
    assert "拆" not in "".join(lg.infos), "归因走 warns 即可"
    assert s.restarted, "取消后 finally 的断连清算必须照常（残帧不跨轮）"
