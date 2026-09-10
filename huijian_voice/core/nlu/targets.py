"""目标/参数提取公共件（自 fast_path v1.5 逐段移植，去掉 MCP 依赖）。

包含：_cn2num、_normalize_name、_extract_prefix、_levenshtein、拼音模糊、
候选六法（的/里分割、英文词、中文设备词、前缀剥离、区域前缀、拼音）+ 质量评分。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("huijian.targets")

# v1.0.40：「两」补入表——creation.py 的时间/阈值正则（_AUTO_TIME_RE 的 h、
# _AUTO_NUM_RE 的 n、_CN_DIGITS）本就收录「两」，唯独本表漏了，导致
# 「下午两点」被算成 12:00（"两"分支取 CN_MAP.get("两",1)*100/10 的兜底）、
# 「超过两百度」算成 100。加一行即自愈：两百→200、两百五→205、两点→2。
CN_MAP = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100}
DEVICE_SUFFIX = ["室", "厅", "房", "间", "楼", "区", "馆", "灯", "扇", "机", "窗", "调", "备"]
AREA_SUFFIX = ["室", "厅", "房", "间", "楼", "区", "馆"]

_KNOWN_DEVICES_TAIL = ["提升窗", "平开窗", "推拉窗", "平推窗", "天窗", "飘窗", "百叶窗", "筒灯", "灯泡",
                       "空调", "风扇", "窗户", "窗帘", "加湿器", "热水器", "净化器", "灯", "窗", "幕布",
                       "门", "电视", "投影", "音箱"]
KNOWN_DEVICES_PREFIX = ["空调", "风扇", "加湿器", "净化器", "热水器", "电视", "投影", "音箱", "幕布",
                        "窗帘", "窗户", "筒灯", "射灯", "灯带", "吸顶灯", "台灯", "落地灯", "床头灯", "夜灯"]
# 修A（2026-09）：射灯/灯带/吸顶灯/台灯/落地灯/床头灯/夜灯 原只在表二，而候选⑥设备词子串扫描
# 与设备词加分只认表一——「办公室射灯」找不到 len≥2 设备词退单字「灯」(4+1=5)，
# ⑤区域候选("办公室","射灯",3+1=4)反而落败，区域整个丢失。
# 设备词全集并为一张表（③⑥⑦与加分共用）；表二保持原样（fast_path 前缀剥离/候选④依赖）。
KNOWN_DEVICES = sorted(set(_KNOWN_DEVICES_TAIL) | set(KNOWN_DEVICES_PREFIX), key=len, reverse=True)
EN_DEVICES = ["light", "lamp", "fan", "ac", "airconditioner", "switch", "outlet", "window",
              "curtain", "blind", "tv", "speaker", "heater", "humidifier", "downlight"]

# ── 动态设备词表（体验批 P2-17：别名自学习）────────────────────
# 静态 KNOWN_DEVICES 是通用词表；真实部署里设备叫「氛围灯带/玄关射灯/新风机」等
# 千奇百怪。从 HA 实体 friendly_name 派生每装专属词表，parse_target/拼音模糊
# 共用——设备改名即自动跟上（每 30s 随状态缓存节流重派生），零持久化零学习风险。
_VOCAB_STOP = {"开关", "状态", "电量", "信号", "电池", "亮度", "色温", "温度", "湿度",
               "待机", "在线", "离线", "主开关", "设置", "传感器", "实体", "慧尖",
               "左", "右", "上", "下", "中", "全部", "全屋"}
_dyn_vocab: tuple[str, ...] = ()      # 已排序（长在前），整体替换赋值（GIL 原子）


def _name_tokens(friendly: str) -> list[str]:
    toks = re.split(r"[\s_\-/·，,、()（）\[\]【】]+", friendly or "")
    out = []
    for t in toks:
        t = t.strip()
        if not (2 <= len(t) <= 8) or t in _VOCAB_STOP:
            continue
        if not all("\u4e00" <= c <= "\u9fff" for c in t):
            continue
        out.append(t)
    return out


def sync_vocab(states: dict) -> None:
    """从 ha 状态缓存派生动态词表（O(实体数) 小任务，pipeline 节流调用）。"""
    names: set[str] = set()
    for eid, ent in (states or {}).items():
        if not str(eid).split(".", 1)[0] in (
                "light", "cover", "climate", "fan", "switch", "humidifier",
                "lock", "vacuum", "media_player"):
            continue
        fn = str(((ent or {}).get("attributes") or {}).get("friendly_name") or "")
        names.update(_name_tokens(fn))
    global _dyn_vocab, ALL_DEVICES, ALL_SET, _ALL_MIN2
    _dyn_vocab = tuple(sorted(names, key=len, reverse=True))
    merged = sorted(set(_STATIC_SET) | set(_dyn_vocab), key=len, reverse=True)
    ALL_DEVICES = tuple(merged)
    ALL_SET = frozenset(merged)
    _ALL_MIN2 = tuple(d for d in ALL_DEVICES if len(d) >= 2)   # 已长→短


def clear_vocab() -> None:      # 测试隔离
    global _dyn_vocab, ALL_DEVICES, ALL_SET, _ALL_MIN2
    _dyn_vocab = ()
    ALL_DEVICES = tuple(sorted(_STATIC_SET, key=len, reverse=True))
    ALL_SET = frozenset(_STATIC_SET)
    _ALL_MIN2 = tuple(d for d in ALL_DEVICES if len(d) >= 2)


_STATIC_DEVICES = tuple(KNOWN_DEVICES)          # 已按长度倒序
_STATIC_SET = frozenset(KNOWN_DEVICES)
ALL_DEVICES = _STATIC_DEVICES                   # 静态+动态合并视图（parse_target 用）
ALL_SET = _STATIC_SET
_ALL_MIN2 = tuple(d for d in ALL_DEVICES if len(d) >= 2)


def cn2num(s: str) -> str:
    """中文数字→阿拉伯数字字符串（"二十三"→"23"，递归处理"一百二十三"）。原样移植。"""
    if not s:
        return "0"
    if s.isdigit():
        return s
    if "零" in s:
        s = re.sub(r"零+", "", s)
        if not s:
            return "0"
    if "百" in s:
        parts = s.split("百", 1)
        front = CN_MAP.get(parts[0], 1 if parts[0] else 1) * 100
        if len(parts) > 1 and parts[1]:
            return str(front + int(cn2num(parts[1])))
        return str(front)
    if "十" in s:
        if s == "十":
            return "10"
        if s.startswith("十"):
            return str(10 + CN_MAP.get(s[1], 0))
        if s.endswith("十") and len(s) == 2:
            return str(CN_MAP.get(s[0], 1) * 10)
        parts = s.split("十", 1)
        front = CN_MAP.get(parts[0], 1) * 10
        if len(parts) > 1 and parts[1]:
            return str(front + CN_MAP.get(parts[1], 0))
        return str(front)
    return str(CN_MAP.get(s, "0"))


_NUM_PAT = re.compile(r"[零一二三四五六七八九十百]+")


def normalize_name(name: str) -> str:
    """"一号测试窗"→"1号测试窗"（HA 实体名多为阿拉伯数字）。"""
    if not name:
        return name
    return _NUM_PAT.sub(lambda m: cn2num(m.group(0)), name)


def levenshtein(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        s1, s2 = s2, s1
    if not s2:
        return len(s1)
    prev = range(len(s2) + 1)
    for c1 in s1:
        curr = [prev[0] + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + (0 if c1 == c2 else 1)))
        prev = curr
    return prev[-1]


def extract_prefix(text: str, start: int = 2, end: int = 10) -> tuple:
    """头部前缀扫描：返回 (prefix, suffix)|(None, text)。
    ⚠ 原 v1.5 语义用 _DEVICE_SUFFIX（含 灯/窗/扇…，「窗户内倒」「卧室灯关」靠它拆），
    移植初版误收窄为 AREA_SUFFIX 导致 "窗户内倒一下" 整段失配，此处回改忠实原码。"""
    _SUF = ["室", "厅", "房", "间", "楼", "区", "馆", "灯", "扇", "机", "窗", "调", "备"]
    for i in range(start, min(end, len(text))):
        prefix, suffix = text[:i], text[i:].lstrip()
        if suffix and any(prefix.endswith(w) for w in _SUF):
            return prefix, suffix
    return None, text


def _area_of_prefix(pre: str) -> str | None:
    """目标词前缀→区域名（修A）：尾字区域词命中（"办公室"）→ 剥属格「的/里/得」重试
    （"办公室的"）→ 二次前缀扫描回捞（未知复合词 "办公室吊灯" 的单字残段→"办公室"）。
    只回区域不拼设备名，避免拿未知残字重构出臆造名词。"""
    pre = re.sub(r"[的里得]+$", "", (pre or "").strip())
    if not pre:
        return None
    if any(pre.endswith(w) for w in AREA_SUFFIX):
        return pre
    return extract_prefix(pre)[0]


def strip_modal(raw: str) -> str:
    """剥离语气词/英文冠词残留 + 处置介词「把/将」头部（"把灯打开"核心化）。"""
    raw = re.sub(r"^[把将]\s*", "", (raw or "").strip())
    raw = re.sub(r"(这个|那个|一下|吧|嘛|啊|啦|哦|哟)$", "", raw).strip()
    for prefix in ("the ", "in the ", "in ", "at "):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix):].strip()
            break
    return raw


_NAME_TAIL_VERBS = re.compile(r"(打开|关闭|关掉|关了|开一下|关上|开|关|动作|一些|全部|都)$")


def clean_name(name: str) -> str:
    """目标名清洗：反复剥离尾部动词残留与首部助词（"灯打开"→"灯"，"的筒灯"→"筒灯"）。"""
    prev = None
    while name != prev:
        prev = name
        name = _NAME_TAIL_VERBS.sub("", name).strip()
        name = re.sub(r"^[的地得了了]", "", name).strip()
    return name


def domain_hint(name: str) -> list[str]:
    """设备名词表→HA 域提示（原 fast_path hint_domains 逻辑逐字移植）。"""
    n = (name or "").lower()
    if "空调" in n or "空調" in n:
        return ["climate"]
    if "灯" in n or "照明" in n:
        return ["light"]
    if "窗帘" in n or "百叶" in n:
        return ["cover"]
    if "风扇" in n:
        return ["fan"]
    if "扫地" in n or "吸尘" in n:
        return ["vacuum"]
    if "电视" in n or "tv" in n:
        return ["media_player"]
    return []


def parse_target(raw: str, action_match=None) -> tuple[str | None, str | None, int]:
    """候选六法 + 质量评分（v1.5 并行提取段移植）。
    action_match: 可选谓词 callable(rest)->bool，命中给 ④ 前缀候选加分（原 4 分档）。
    返回 (area, name, score)；解析失败时 area=None、name=清洗后的 raw。"""
    raw = strip_modal(raw or "")
    if not raw:
        return None, None, 0
    candidates: list[tuple[str, str, int]] = []
    # ① 的/里 分割
    for sep in ("的", "里"):
        if sep in raw:
            p = raw.split(sep, 1)
            if len(p[1]) > 0 and len(p[0]) <= 6:
                candidates.append((p[0].strip(), p[1].strip(), 3))
    # ② 英文尾词
    words = raw.strip().split()
    if len(words) >= 2:
        if words[-1].lower() in EN_DEVICES:
            candidates.append((" ".join(words[:-1]), words[-1], 3))
    # ③ 中文设备尾词
    stripped = raw.strip()
    for d in KNOWN_DEVICES:
        if stripped.endswith(d) and len(stripped) > len(d):
            candidates.append((stripped[: -len(d)].strip(), d, 2))
            break
    # ④ 已知设备前缀剥离（"空调风量大一点"→设备=空调；剩余匹配动作→4 分）
    for d in sorted(KNOWN_DEVICES_PREFIX, key=len, reverse=True):
        if stripped.startswith(d) and len(stripped) > len(d):
            rest = stripped[len(d):].strip()
            if action_match and action_match(rest):
                candidates.append((d, rest, 4))
            else:
                candidates.append((d, rest, 3))
            break
    # ⑤ 区域前缀扫描
    for i in range(2, min(5, len(stripped))):
        pre, suf = stripped[:i], stripped[i:]
        if suf and any(pre.endswith(w) for w in AREA_SUFFIX):
            candidates.append((pre, suf, 3))
    # ⑥ 设备词子串优先（"暂停窗户动作"→窗户；移植期新增，堵原表中段词漏提）。
    #    先做 len≥2；无果退单字通用词（灯/窗/门），再往后才轮到拼音档，
    #    防 "灯打开" 被近音 "灯泡"(dist=2) 截胡（tie 先到优先）。
    #    修A：区域提取统一走 _area_of_prefix（剥「的」+二次回捞），不再因残字丢区域。
    _hit_dev = False
    for d in _ALL_MIN2:
        idx = stripped.find(d)
        if idx >= 0:
            candidates.append((_area_of_prefix(stripped[:idx]), d, 5))
            _hit_dev = True
            break
    if not _hit_dev:
        for d in ("灯", "窗", "门"):
            idx = stripped.find(d)
            if idx >= 0:
                candidates.append((_area_of_prefix(stripped[:idx]), d, 4))
                _hit_dev = True
                break
    # ⑦ 拼音模糊（v1.5 两缺陷修正：a) 首个 ≤5 即 break 会让「空调 kongtiao」被
    #    「筒灯 tongdeng」(dist=5) 截胡；b) 阈值过松噪音大。改为全表择优 + 收紧 ≤2
    #    + 仅在无子串命中时启用；短文本才跑）
    if not _hit_dev and len(stripped) <= 8:
        try:
            from pypinyin import lazy_pinyin
            py_raw = "".join(lazy_pinyin(stripped))
            best = None  # (dist, -len(d), d)
            for d in ALL_DEVICES:
                if len(d) < 2:
                    continue
                py_dev = "".join(lazy_pinyin(d))
                dist = 99
                for i in range(max(0, len(py_raw) - len(py_dev) + 1)):
                    dist = min(dist, levenshtein(py_raw[i: i + len(py_dev)], py_dev))
                if dist <= 2 and (best is None or dist < best[0] or (dist == best[0] and len(d) > -best[1])):
                    best = (dist, -len(d), d)
            if best is not None:
                candidates.append(("", best[2], 4 if best[0] <= 1 else 3))
        except ImportError:
            pass
        except Exception:
            pass

    best_score, area, name = 0, None, stripped
    for a, n, base in candidates:
        score = base
        if n in ALL_SET or any(k in n.lower() for k in EN_DEVICES):
            score += 1
        if a and any(a.endswith(w) for w in AREA_SUFFIX):
            score += 1
        if score > best_score:
            best_score, area, name = score, a, n
    # 质量门：低分且无显式分隔符 → 交上层回退（原 score<3 规则）
    if best_score < 3 and "的" not in stripped and "里" not in stripped and " " not in stripped:
        return None, None, best_score
    cleaned = clean_name(normalize_name(name))
    return area, cleaned or name, best_score
