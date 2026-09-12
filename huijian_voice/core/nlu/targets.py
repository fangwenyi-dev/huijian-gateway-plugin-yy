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
                       # 2026-09-16 内倒语序洞：窗型词补全（fast_path 窗型纠正/集成侧
                       # valid window names 认这套，设备词表此前缺一半——「内开内倒窗」
                       # 这类长词落不进子串扫描，尾置动作句整体失配）
                       "内开窗", "外开窗", "推拉门", "智能窗", "内开内倒窗", "单内倒窗", "外装平开窗",
                       "内倒窗",
                       # 2026-09 开窗器名称洞（用户令优化「开窗器名称识别」）：
                       # 「关闭开窗器」此前被剥成 name="窗" 残渣、意图错判 Turn*——
                       # 开窗器/开合器/推窗器 是设备词，必须整词保留（fast_path 窗族纠正联动）。
                       "开窗器", "开合器", "推窗器",
                       # 2026-09 悬窗族（用户点名开窗器机型按「区域+窗名」命名：
                       # 客厅的上悬窗/下悬窗/悬窗/提升窗）。提升窗 原只在本表，
                       # 缺窗型纠正→「客厅提升窗关闭」错走 Turn*——一并入三处同步。
                       "下悬窗", "上悬窗", "悬窗",
                       "空调", "风扇", "窗户", "窗帘", "加湿器", "热水器", "净化器", "灯", "窗", "幕布",
                       "门", "电视", "投影", "音箱",
                       # v1.0.42 家电族：冷启动（registry 动态词表未同步时）也能
                       # 直呼这些常用设备名。
                       "扫地机器人", "扫拖机器人", "吸尘器", "拖地机", "雷达",
                       # 2026-09 通用智能家居产品词（用户令按市面常见品类扩充；
                       # 动态词表 sync_vocab 只覆盖已接入实体，这些是冷启动兜底）。
                       # 清洁：擦窗机器人**含窗字但属家电**——device_shared 排除表联动
                       "洗地机", "除螨仪", "擦窗机器人",
                       # 环境：新风/换气多挂 switch/fan，域提示宁缺毋滥
                       "新风机", "新风", "除湿机", "换气扇", "排风扇", "循环扇",
                       "取暖器", "电暖器", "暖风机", "浴霸", "香薰机",
                       # 厨卫大电
                       "洗衣机", "烘干机", "干衣机", "洗碗机", "油烟机",
                       "燃气灶", "灶具", "微波炉", "烤箱", "电饭煲", "空气炸锅",
                       "电磁炉", "破壁机", "榨汁机", "咖啡机", "饮水机", "净水器",
                       "电水壶", "养生壶",
                       # 生活/安防/控制：门锁走 lock 域话术道（打开门锁=上锁 D7 已钉）
                       "晾衣架", "晾衣机", "按摩椅", "音响",
                       "门锁", "智能门锁", "智能锁", "猫眼", "智能猫眼",
                       "摄像头", "监控", "门铃", "智能门铃",
                       "插座", "智能插座"]
KNOWN_DEVICES_PREFIX = ["空调", "风扇", "加湿器", "净化器", "热水器", "电视", "投影", "音箱", "幕布",
                        "窗帘", "窗户", "筒灯", "射灯", "灯带", "吸顶灯", "台灯", "落地灯", "床头灯", "夜灯",
                        # 2026-09-16 内倒语序洞（现场实锤「展厅平开窗内倒」→None）：
                        # ②③ 前缀剥离只认本表——「区域+窗型词+尾置动作」（X内倒/把X内倒/
                        # X关闭）此前全部够不到，落给 klar 产歧义目标再被过宽闸 clarify。
                        # 词表与 fast_path._WINDOW_TYPES/集成 valid names 同集合，守卫测试钉。
                        "平开窗", "平推窗", "内开内倒窗", "单内倒窗", "外装平开窗", "内开窗",
                        "内倒窗",
                        "外开窗", "推拉窗", "推拉门", "智能窗", "天窗", "飘窗",
                        # 开窗器/开合器/推窗器 同进前缀表：「开窗器关闭/开窗器打开」
                        # SOV 形靠 ② 前缀剥离回捞（fast_path 通用 开 字冠对器字已设护栏）。
                        "开窗器", "开合器", "推窗器",
                        # 悬窗族/提升窗（与 _WINDOW_TYPES/集成映射三方同步，守卫钉）。
                        "下悬窗", "上悬窗", "提升窗", "悬窗",
                        "扫地机器人", "扫拖机器人", "吸尘器", "拖地机",
                        # 2026-09 通用智能家居产品词（与表一同步，SOV「X关闭」回捞）
                        "洗地机", "除螨仪", "擦窗机器人", "新风机", "新风", "除湿机",
                        "换气扇", "排风扇", "循环扇", "取暖器", "电暖器", "暖风机",
                        "浴霸", "香薰机", "洗衣机", "烘干机", "干衣机", "洗碗机",
                        "油烟机", "燃气灶", "灶具", "微波炉", "烤箱", "电饭煲",
                        "空气炸锅", "电磁炉", "破壁机", "榨汁机", "咖啡机", "饮水机",
                        "净水器", "电水壶", "养生壶", "晾衣架", "晾衣机", "按摩椅",
                        "音响", "门锁", "智能门锁", "智能锁", "猫眼", "智能猫眼",
                        "摄像头", "监控", "门铃", "智能门铃", "插座", "智能插座"]
# 修A（2026-09）：射灯/灯带/吸顶灯/台灯/落地灯/床头灯/夜灯 原只在表二，而候选⑥设备词子串扫描
# 与设备词加分只认表一——「办公室射灯」找不到 len≥2 设备词退单字「灯」(4+1=5)，
# ⑤区域候选("办公室","射灯",3+1=4)反而落败，区域整个丢失。
# 设备词全集并为一张表（③⑥⑦与加分共用）；表二保持原样（fast_path 前缀剥离/候选④依赖）。
KNOWN_DEVICES = sorted(set(_KNOWN_DEVICES_TAIL) | set(KNOWN_DEVICES_PREFIX), key=len, reverse=True)
EN_DEVICES = ["light", "lamp", "fan", "ac", "airconditioner", "switch", "outlet", "window",
              "curtain", "blind", "tv", "speaker", "heater", "humidifier", "downlight"]

# 属性/参数词：句中残留含这些词时禁入拼音模糊档（⑦）——它们是调节参数名，
# 近音撞进设备表就是"目标幻觉"（亮度→浴霸 事故形）。
_ATTR_NO_PINYIN = ("亮度", "色温", "温度", "湿度", "风量", "风速",
                   "开合度", "模式", "档位", "音量")

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
    # 2026-09 通用智能家居品类扩充（擦窗机器人含窗字但属 vacuum 族——
    # 域提示在窗/帘判定之后、与按压窗控无涉；换气/排气/循环扇生态恒 fan）。
    if any(w in n for w in ("扫地", "吸尘", "机器人", "洗地机", "拖地机", "除螨仪")):
        return ["vacuum"]
    if any(w in n for w in ("换气扇", "排风扇", "循环扇")):
        return ["fan"]
    if any(w in n for w in ("门锁", "智能锁")):
        return ["lock"]
    if any(w in n for w in ("插座",)):
        return ["switch"]
    if any(w in n for w in ("摄像头", "监控", "猫眼")):
        return ["camera"]
    if "电视" in n or "tv" in n or "音响" in n:
        return ["media_player"]
    # v1.0.42 家电域提示补齐（此前加湿器/净化器等 domains=[] ，集成端只能
    # 全实体面找名——同名歧义面大）。净化器 HA 生态多挂 fan 域，两域并集提示。
    if "加湿" in n or "除湿" in n:
        return ["humidifier"]
    if "净化" in n:
        return ["fan", "humidifier"]
    if "投影" in n:
        return ["media_player"]
    if "热水器" in n:
        return ["water_heater"]
    if "洗碗" in n:
        return ["dishwasher"]
    if "洗衣机" in n or "烘干" in n:
        return ["laundry_washer", "laundry_dryer"]
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
    # 2026-09-21 目标幻觉实锤再收紧（「调高亮度到80%」曾把 liangdudao80% 窗口
    # 撞 dist=2 配成"浴霸"）：①两字设备（拼音 ≤5 字母）容差降到 1——4 字母
    # 窗口错 2 个=半句皆可错，短词误配是必然；②残段含属性词禁入⑦——亮度/色温
    # 这类**参数名**永远不该升格成设备目标（属性句走 no-target+上下文继承）。
    if (not _hit_dev and len(stripped) <= 8
            and not any(w in stripped for w in _ATTR_NO_PINYIN)):
        try:
            from pypinyin import lazy_pinyin
            py_raw = "".join(lazy_pinyin(stripped))
            best = None  # (dist, -len(d), d)
            for d in ALL_DEVICES:
                if len(d) < 2:
                    continue
                py_dev = "".join(lazy_pinyin(d))
                tol = 1 if len(py_dev) <= 5 else 2
                dist = 99
                for i in range(max(0, len(py_raw) - len(py_dev) + 1)):
                    dist = min(dist, levenshtein(py_raw[i: i + len(py_dev)], py_dev))
                if dist <= tol and (best is None or dist < best[0] or (dist == best[0] and len(d) > -best[1])):
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


# ── 并列宾语展开（2026-09-21 用户令第③点：「打开展厅内倒窗和推拉窗」必须两扇
#     都开）────────────────────────────────────────────────────────
# 病灶：共享动词的"打开A和B"从不进分句通路——T0 单发把 parse_target 吃到的
# 那一个执行掉并回「办好了」，另一半**静默丢弃**（半执行+谎报，链发纪律里
# 最危险形态）。本函数把它改写成"打开A、打开B"动词连排形态，交既有
# serial_clauses/split_actions 链发（全有全无+区域上下文注入全部复用）。
# 判据宁严勿松：句首显式动词、连接词两侧**全部**以已知设备词结尾、任一分片
# 含动词或长度越界 → 返回 None 原样交既有通道。"开合器"等词无「和」字零冲突。
_COORD_HEAD = re.compile(
    r"^(打开|开启|关闭|关掉|关上|拉开|拉上|拉下|播放|停止|暂停)"
    r"(?:一下)?[把将]?[\s]*(?=\S)")
_COORD_CONJ = re.compile(r"[和与]")
# 动词片只认**双字动词形**——单字表会误杀设备词（"推_拉_窗""空_调_"），
# 首版实测即栽在此（coord_clauses 恒 []）。宁可漏判（漏→原通道，不误伤）。
_COORD_VERBISH = ("打开", "开启", "关闭", "关掉", "关上", "调高", "调低",
                  "调亮", "调暗", "调到", "设为", "设到", "设置", "设定",
                  "播放", "停止", "暂停", "锁上", "解锁", "拉上", "拉下",
                  "拉开", "摇上", "摇下", "换成", "变到")


def _coord_ends_device(seg: str) -> bool:
    return any(seg.endswith(d) for d in _ALL_MIN2)


def coord_clauses(text: str) -> list[str]:
    """「打开A和B」→ ["打开A", "打开B的补区域形"]；不是并列形态 → []。"""
    text = (text or "").strip().strip("。！？!?")
    if not (4 <= len(text) <= 30):
        return []
    m = _COORD_HEAD.match(text)
    if not m:
        return []
    verb = m.group(1)
    segs = [s.strip(" 的") for s in _COORD_CONJ.split(text[m.end():])]
    segs = [s for s in segs if s]
    if not (2 <= len(segs) <= 8):
        return []
    fixed: list[str] = []
    for s in segs:
        s = re.sub(r"^(?:帮我把|帮我|请|麻烦|把)", "", s).strip(" 的")
        if not (2 <= len(s) <= 12):
            return []
        if any(v in s for v in _COORD_VERBISH):
            return []                       # 片内藏动词=正常连排/复合句，交原通道
        if not _coord_ends_device(s):
            return []                       # 任何一片不是已知设备尾词 → 不扩
        fixed.append(s)
    area = _area_of_prefix(fixed[0]) or ""
    out: list[str] = []
    for s in fixed:
        if area and not _area_of_prefix(s):
            s = f"{area}的{s}"              # 共享区域回填（同句同房间语义）
        out.append(verb + s)
    return out


def coord_refuse(text: str) -> bool:
    """「打开A和…」且 A 以已知设备词结尾 → 单发通路必须拒猜。

    coord_clauses 只在全部分片都是已知设备时扩链；「打开内倒窗和不存在的X」
    这类右片听不懂的并列句若放给 T0 单发，parse_target 会吃掉左片执行并
    谎报「办好了」——右片被静默丢弃=半执行。本判据说的是：**只要并列连词
    挂在已识别设备尾之后**，单发怎么裁都错，如实交 fallback/LLM 兜底。"""
    text = (text or "").strip().strip("。！？!?")
    m = _COORD_HEAD.match(text)
    if not m:
        return False
    segs = [s.strip(" 的") for s in _COORD_CONJ.split(text[m.end():])]
    segs = [s for s in segs if s]
    if len(segs) < 2 or not (2 <= len(segs[0]) <= 12):
        return False
    return _coord_ends_device(segs[0])
