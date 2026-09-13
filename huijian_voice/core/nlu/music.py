# -*- coding: utf-8 -*-
"""音乐过渡带（v1.0.21 零改动版 + P1 增强 2026-09-25）。

解析纯 regex（不依赖 NLU 资产/引擎）；执行走 HA core 标准服务族：
点歌 = media_player.play_media（media_content_type=music + 明文
media_content_id，MA 托管播放器做智能检索，契约见 music-assistant.io
Play Media 指南）；暂停/继续/停止/上下曲 = media_player 标准播控；
「现在放的是什么歌」= now_playing 查询（执行侧读端点态）。

P1 增强（方案 §5.1）：
- **区域定向**：`known_areas` 词表（HA 区域注册表 ∪ satellite_areas ∪
  music.area_entities 键，pipeline 侧供给）使「在卧室播放周杰伦」
  「播放周杰伦，客厅的音箱」抽出 area；词表取不到的前缀（"在书房…"且
  书房未注册）一律不瞎抽，整句自然放行级联。
- **保守纪律**：区域短语与曲名粘连（"播放青花瓷的音箱"——曲名里恰含
  区域字）宁漏不误，绝不剥；设备词收尾仍让位控制/LLM。
缺省 `known_areas=None` = 空集，区域定向关闭，返回形态与 v1.0.21 逐字段
一致（老钉测与调用方零改动）。
"""
import re
from typing import Optional

# 判序：正在播放查询最先（"现在放的是什么歌"既是问句又含"放"，必须先于
# 播控与点歌判掉）；播控词先于「播放XX」（"停止播放"含"播放"，整句精确匹配）。
_NOW_PLAYING = re.compile(
    r"(?:现在|正在|这会儿)?(?:放的|播的|听的)(?:是什么|是啥|什么|啥)"
    r"(?:歌|曲子|歌曲|音乐)?"
    r"|^(?:什么歌|哪首歌|这是(?:什么|哪首)歌|这首歌(?:是谁唱的|叫啥|叫什么))"
    r"|(?:谁唱的|叫什么名)$")

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
# 守卫尾：设备词收尾不是点歌（"播放客厅的灯"该走控制/LLM，不是歌名；
# 音箱族同律——"播放周杰伦的音箱"不是点歌令，宁漏不误）
_DEVICE_TAIL = re.compile(
    r"(灯|灯带|筒灯|射灯|窗帘|空调|门|锁|窗|扇|机|开关|插座|阀"
    r"|音响|音箱|喇叭|播放器)$")
# 区域定向短语里的设备词（配合区域名参与尾部抽取，与 _DEVICE_TAIL 同族）
_DEVICE_TAIL_WORDS = ("音响", "音箱", "喇叭", "播放器")
# 口语假点歌黑名单（"放轻松"≠点《轻松》）
_BLACKLIST = frozenset({"轻松", "假", "学", "下", "松", "大声音"})
# 泛点歌（差点名）：宾格词做整个查询 / 整句即"动词+量词+音乐词"
_GENERIC = frozenset({"音乐", "歌", "歌曲", "一首歌", "曲子", "曲", "点儿音乐", "点音乐"})
GENERIC_WORDS = _GENERIC   # fast_path 让位守卫用（"关掉音乐"非设备控制）
_GENERIC_SENT = re.compile(
    r"^(?:帮我|请|麻烦|我想)?(?:来|放|听|播|播放)"
    r"(?:一?[点些]|一?首|首)?(?:歌曲?|音乐)(?:吧|呀)?$")

# 尾部动词前瞻（剥区域短语后核心句仍以动词开头时交给 _PLAY/_CTRL 判）
_AREA_VERB = "放|播|听|来|切|换|点|暂停|继续|停"


def _extract_area(t: str, known_areas: set) -> tuple[str, str]:
    """头部「在/用/换到 + 区域 + (音箱) + 音乐动词」引导剥离 → (area, 核心句)。

    区域名必须 ∈ known_areas（长名优先，防「客厅沙发」截胡「客厅」）；
    动词用前瞻不吃进核心句，剥完仍交 _PLAY/_CTRL 整句判。"""
    if not known_areas:
        return "", t
    for name in sorted(known_areas, key=len, reverse=True):
        m = re.match(
            r"^(?:在|用|换到|换成|到)" + re.escape(name) +
            r"(?:里面|里|上)*(?:的)?(?:音箱|音响|播放器|喇叭)?"
            r"(?=" + _AREA_VERB + ")", t)
        if m:
            return name, t[m.end():]
    return "", t


def _extract_area_tail(q: str, known_areas: set) -> tuple[str, str]:
    """曲名尾部「区域+音箱」短语剥离 → (area, 剩余曲名)。

    边界纪律（宁漏不误）：剥掉「区域名+衬词+设备词」后的剩余必须为空，
    或以逗号/顿号/分号/空格（曲名后停顿）收尾——以文字直接粘连者
    （"青花瓷的音箱"，曲名可能自带区域字）一律不剥，交 _DEVICE_TAIL 让位。"""
    if not known_areas or not q:
        return "", q
    for name in sorted(known_areas, key=len, reverse=True):
        for w in _DEVICE_TAIL_WORDS:
            for glue in ("里的", "的", "里", ""):
                tail = name + glue + w
                if not q.endswith(tail):
                    continue
                pre = q[:len(q) - len(tail)].rstrip()
                if pre and pre[-1] not in "，,、; ；":
                    continue                      # 曲名粘连 → 拒剥
                rest = pre.rstrip("，,、; ；").strip("。，、 ")
                if len(rest) >= 2:
                    return name, rest
                return "", q                      # 剩余太短：整串视作短语，不剥
    return "", q


def parse_music(text: str, known_areas: Optional[set] = None) -> Optional[dict]:
    """点歌/播控/正在播放令 → {"action","query"[,"area"]}；非音乐令 None。

    known_areas：已知区域词表（pipeline 侧 HA 注册表 ∪ satellite_areas ∪
    area_entities 键）；缺省 None=空集，区域定向关闭，返回形态与
    v1.0.21 逐字段一致。"""
    t = re.sub(r"[\s。，,！!？?~～.]+$", "", (text or "").strip())
    if not t:
        return None
    if _NOW_PLAYING.search(t):
        return {"action": "now_playing", "query": ""}
    areas = known_areas or set()
    area, tt = _extract_area(t, areas)            # tt=剥区域引导后的核心句
    for act, rx in _CTRL:                          # 播控词先于「播放XX」整句锚定
        if rx.match(tt):
            out = {"action": act, "query": ""}
            if area:
                out["area"] = area
            return out
    if _GENERIC_SENT.match(tt):                     # 泛点歌整句（放首歌/来点音乐…）
        out = {"action": "play", "query": ""}
        if area:
            out["area"] = area
        return out
    m = _PLAY.match(tt)
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
    q_area, q_rest = _extract_area_tail(q, areas)  # 「…，客厅的音箱」收尾→选端点
    if q_area:
        area, q = (area or q_area), q_rest
    if q and (q in _BLACKLIST or _DEVICE_TAIL.search(q)):
        return None                                # 设备词收尾→不抢控制/LLM 的活
    out = {"action": "play", "query": q}           # 空 q=泛点歌，执行侧引导差点名
    if area:
        out["area"] = area
    return out
