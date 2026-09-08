# -*- coding: utf-8 -*-
"""零改动音乐过渡带（用户定向 2026-09-12：先验证「语音点歌」价值再立 A/B 项）。

解析纯 regex（不依赖 NLU 资产/引擎）；执行走 HA core 标准服务族：
点歌 = media_player.play_media（media_content_type=music + 明文
media_content_id，MA 托管播放器做智能检索，契约见 music-assistant.io
Play Media 指南）；暂停/继续/停止/上下曲 = media_player 标准播控。
端点未配置一律一句配置指引收口——绝不放进 LLM 闲聊吞点歌令。
"""
import re
from typing import Optional

# 判序：播控词先于「播放XX」（"停止播放"含"播放"，先整句精确匹配）
_CTRL: list[tuple[str, re.Pattern]] = [
    ("stop",   re.compile(r"^(停止播放|停止音乐|关掉音乐|关闭音乐|音乐关掉?"
                          r"|音乐停(一下|下)?|把音乐停(了|一下)?|把音乐关了?"
                          r"|别放了|停(一下|下)?(音乐|播放|歌)|停音乐)$")),
    ("pause",  re.compile(r"^(暂停播放|暂停音乐|音乐暂停|暂停一下(音乐|播放)?)$")),
    ("resume", re.compile(r"^(继续播放|继续放(音乐|歌)?|恢复播放|接着放(音乐|歌)?)$")),
    ("next",   re.compile(r"^(下一首|切歌|换一首|换首歌|来首别的|切下一首|再来一首)$")),
    ("prev",   re.compile(r"^(上一首|切回去|退回上一首)$")),
]

_PLAY = re.compile(
    r"^(?:帮我|请|麻烦|我想)?(?:播放|播一下|播首|来(?:一点|点|一首|首)"
    r"|我想听|听(?:一首|首)|放(?:一首|首|点|一点))"
    r"(?P<q>.+)$")

# 剥尾：《晴天》的歌 / 这首歌 / 歌曲 / 音乐
_PLAY_STRIP = re.compile(r"(?:的(?:歌曲?|音乐)|这首歌|歌曲|音乐)$")
# 守卫尾：设备词收尾不是点歌（"播放客厅的灯"该走控制/LLM，不是歌名）
_DEVICE_TAIL = re.compile(r"(灯|灯带|筒灯|射灯|窗帘|空调|门|锁|窗|扇|机|开关|插座|阀)$")
# 口语假点歌黑名单（"放轻松"≠点《轻松》）
_BLACKLIST = frozenset({"轻松", "假", "学", "下", "松", "大声音"})
# 泛点歌（差点名）：宾格词做整个查询 / 整句即"动词+量词+音乐词"
_GENERIC = frozenset({"音乐", "歌", "歌曲", "一首歌", "曲子", "曲", "点儿音乐", "点音乐"})
GENERIC_WORDS = _GENERIC   # fast_path 让位守卫用（"关掉音乐"非设备控制）
_GENERIC_SENT = re.compile(
    r"^(?:帮我|请|麻烦|我想)?(?:来|放|听|播|播放)"
    r"(?:一?[点些]|一?首|首)?(?:歌曲?|音乐)(?:吧|呀)?$")


def parse_music(text: str) -> Optional[dict]:
    """点歌/播控令 → {"action","query"}；非音乐令返回 None（保守放行级联）。"""
    t = re.sub(r"[\s。，,！!？?~～.]+$", "", (text or "").strip())
    if not t:
        return None
    for act, rx in _CTRL:
        if rx.match(t):
            return {"action": act, "query": ""}
    if _GENERIC_SENT.match(t):                     # 泛点歌整句（放首歌/来点音乐…）
        return {"action": "play", "query": ""}
    m = _PLAY.match(t)
    if not m:
        return None
    q = m.group("q").strip("。，、 ")
    if q in _GENERIC:                              # "来点音乐"：泛点歌差点名
        q = ""
    for _ in range(2):
        # 只剥独立尾缀且**剩余 ≥2 字**——"纯音乐"绝不能剥成"纯"
        sfx = _PLAY_STRIP.search(q)
        if not sfx or len(q) - len(sfx.group(0)) < 2:
            break
        q = q[:sfx.start()].strip("。，、 ")
    if q and (q in _BLACKLIST or _DEVICE_TAIL.search(q)):
        return None                                # 设备词收尾→不抢控制/LLM 的活
    return {"action": "play", "query": q}          # 空 q=泛点歌，执行侧引导差点名
