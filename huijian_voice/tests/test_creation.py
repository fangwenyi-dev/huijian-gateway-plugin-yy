"""creation.py 句式解析矩阵钉（v1.0.30 零 LLM 语音创建）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.nlu import creation as cr  # noqa: E402


# ── 语音场景 ──────────────────────────────────────────────────
def test_scene_basic():
    c = cr.parse("当我说晚安的时候帮我关闭卧室灯")
    assert c == {"kind": "scene", "trigger_phrase": "晚安", "y": "关闭卧室灯"}


def test_scene_no_comma_no_shihou():
    c = cr.parse("当我说我回来了就打开客厅灯")
    assert c["trigger_phrase"] == "我回来了" and c["y"] == "打开客厅灯"


def test_scene_no_connector_variant():
    """ASR 吞"就"：动作字开头变体必须接住。"""
    c = cr.parse("当我说晚安关卧室灯")
    assert c and c["trigger_phrase"] == "晚安" and c["y"] == "关卧室灯"


def test_scene_meta_prefix():
    c = cr.parse("帮我创建一个语音场景，当我说吃饭的时候把餐厅灯打开")
    assert c["trigger_phrase"] == "吃饭" and c["y"] == "餐厅灯打开"


def test_scene_multi_y():
    parts = cr.split_actions("关闭客厅窗帘并打开卧室灯")
    assert parts == ["关闭客厅窗帘", "打开卧室灯"]
    assert cr.split_actions("关灯") == ["关灯"]
    assert len(cr.split_actions("关A并且开B然后调暗C再D")) <= 3 + 1  # 超限退化整句


def test_scene_half_utterance_none():
    assert cr.parse("当我说晚安") is None           # 无 Y 不建
    assert cr.parse("打开客厅灯") is None            # 普通命令零影响
    assert cr.parse("当天热了怎么办") is None         # 闲聊不误吞


# ── 自动化·数值 ──────────────────────────────────────────────
def test_auto_numeric_above():
    c = cr.parse("当客厅温度超过28度时就帮我打开空调")
    assert c["kind"] == "automation"
    assert c["trigger"] == {"entity_id": "客厅温度", "above": 28.0}
    assert c["y"] == "打开空调" and c["desc"] == "客厅温度"


def test_auto_numeric_below_cn():
    c = cr.parse("如果卧室湿度低于二十五就关掉加湿器")
    assert c["trigger"]["below"] == 25.0


def test_auto_numeric_percent():
    c = cr.parse("当客厅湿度超过80%就开除湿机")
    assert c["trigger"] == {"entity_id": "客厅湿度", "above": 80.0}


def test_auto_decimal_forms():
    """钉死"点"小数：混写 26点5 与纯中文 二十六点五（cn2num 直吃会静默截成 26）。"""
    c = cr.parse("当客厅温度超过26点5度就打开空调")
    assert c and c["trigger"]["above"] == 26.5
    c = cr.parse("如果书房温度低于二十六点五度就关窗")
    assert c and c["trigger"]["below"] == 26.5
    assert cr.parse("当客厅温度超过26点就打开空调") is None   # 残缺小数宁缺勿错


def test_auto_numeric_no_connector_contract_v1124():
    """v1.1.24 契约变更（原「无锚点宁缺勿错」按用户令放宽为"自然说法补全"）：
    无连接词时 y 以**动作字**开头即认（「当温度超过28度打开空调」）；
    y 不以动作字开头仍拒（半句绝不建单）。"""
    c = cr.parse("当温度超过28度打开空调")
    assert c and c["kind"] == "automation" and c["trigger"]["above"] == 28.0, c
    assert cr.parse("当客厅有人活动") is None


# ── 自动化·状态 ──────────────────────────────────────────────
def test_auto_presence_on():
    c = cr.parse("当书房检测到有人时帮我打开书房灯")
    assert c["trigger"]["entity_id"] == "书房人体"
    assert c["trigger"]["to"] == "on"


def test_auto_presence_off():
    c = cr.parse("当客厅没人就把灯关了")
    assert c is not None
    assert c["trigger"] == {"entity_id": "客厅人体", "to": "off"}


# ── 自动化·时间 ──────────────────────────────────────────────
def test_auto_time_morning():
    c = cr.parse("每天早上7点帮我打开客厅窗帘")
    assert c["kind"] == "automation" and c["trigger"] == {"at": "07:00"}
    assert c["y"] == "打开客厅窗帘"


def test_auto_time_evening_half():
    c = cr.parse("每天晚上10点半就关闭所有窗帘")
    assert c["trigger"]["at"] == "22:30"


def test_auto_time_pm_ambiguity():
    assert cr.parse("下午3点开空调的时候") is None
    c = cr.parse("每天下午3点就开空调")
    assert c["trigger"]["at"] == "15:00"


# ── v1.0.40：「两」口径统一（CN_MAP 漏收「两」的回归钉）────────────
# creation.py 的时间/阈值正则本就收录「两」，唯 targets.CN_MAP 漏了 →
# 「下午两点」被算成 12:00、「超过两百度」算成 100。零测试覆盖，故补钉。
def test_cn_map_has_liang():
    from core.nlu.targets import CN_MAP, cn2num
    assert CN_MAP["两"] == 2
    assert cn2num("两") == "2"
    assert cn2num("两百") == "200"          # 「百」分支自愈
    assert cn2num("两百五") == "205"
    assert cn2num("两百五十") == "250"      # 「十」分支自愈


def test_auto_time_liang_hour():
    """「两点」是高频说法：下午两点=14:00、凌晨两点=02:00。"""
    assert cr.parse("每天下午两点就打开窗帘")["trigger"]["at"] == "14:00"
    assert cr.parse("每天凌晨两点就关闭所有灯")["trigger"]["at"] == "02:00"
    assert cr.parse("每天早上两点半就开空调")["trigger"]["at"] == "02:30"


def test_auto_threshold_liang():
    c = cr.parse("当温度超过两百度就打开空调")
    assert c["trigger"]["above"] == 200


# ── 白名单对齐（与集成侧六 intent 闭环）────────────────────────
def test_actionable_whitelist_alignment():
    src = (Path(__file__).resolve().parents[1] /
           "custom_components" / "huijian_ai" / "intent_voice_scene.py").read_text(
        encoding="utf-8")
    seg = src.split("async def _execute_intent", 1)[1][:1200]
    for name in cr.ACTIONABLE_INTENTS:
        assert name in seg, f"集成侧白名单缺 {name}，本地 ACTIONABLE 需同步收缩"


# ── v1.0.33 场景删除本地句 ─────────────────────────────────────
def test_scene_delete_phrases():
    cases = {"删除场景晚安": "晚安", "删掉场景 我回来了": "我回来了",
             "把场景晚安删了": "晚安", "帮我删除场景「早安」": "早安",
             "移除语音场景午休": "午休", "把场景「我回家了」删除": "我回家了"}
    for s, want in cases.items():
        p = cr.parse(s)
        assert p and p["kind"] == "delete_scene" and p["trigger_phrase"] == want, s


def test_scene_delete_requires_explicit_name():
    """裸删（不带名字）本地只出"列清单+编号引导"（trigger_phrase 空=不执行删除），
    批量语义（"删除所有场景"）与无意义残句仍不接——一句话清库绝不本地承接。"""
    for s in ("删除场景", "删除语音场景", "把场景删了", "把场景给我删了"):
        p = cr.parse(s)
        assert p is not None and p.get("kind") == "delete_scene", s
        assert p.get("trigger_phrase") == "", s          # 空名=引导，非删除目标
    for s in ("删除所有场景", "删了"):
        p = cr.parse(s)
        assert p is None or p.get("kind") != "delete_scene", s


def test_auto_numeric_forms_without_dang_or_jiu():
    """v1.1.24：数值自动化补两种自然口语形——无「当」前缀 / 无「就」连接词
    （旧骨架要求「当…就」全齐，这两种最常说法的句子整句落兜底）。"""
    for s in ("客厅温度超过28度就打开空调",
              "当客厅温度超过28度时打开空调",
              "客厅温度超过28度的时候打开空调"):
        p = cr.parse(s)
        assert p and p["kind"] == "automation" and p["trigger"]["above"] == 28.0, s
        assert p["y"] == "打开空调", s
    # 反向：无连接词时 y 必须以动作字开头（"有人活动"类半句绝不建单）
    assert cr.parse("当客厅有人活动") is None
    assert cr.parse("客厅温度超过28度了") is None


def test_auto_delete_missing_word_orders():
    """v1.1.24：自动化删除补场景侧已有的三种语序（第N条 / 我的 / 名词先行）。"""
    p = cr.parse("删除第1条自动化")
    assert p and p["kind"] == "delete_automation" and cr.auto_target(p["target"]) == 1, p
    p = cr.parse("自动化1删了")
    assert p and p["kind"] == "delete_automation" and cr.auto_target(p["target"]) == 1, p
    p = cr.parse("删除我的自动化1")
    assert p and p["kind"] == "delete_automation" and cr.auto_target(p["target"]) == 1, p
    # 反向：既有语序不回归 + 批量语义仍不接
    assert cr.auto_target(cr.parse("删除自动化2")["target"]) == 2
    assert cr.parse("删除所有自动化") is None


def test_scene_modify_noun_first_and_de_wart():
    """v1.1.24：改场景补名词先行形；并修「把场景X的改成Y」把「的」抓进名字的 wart。"""
    p = cr.parse("把我有点热场景改成关闭射灯")
    assert p and p["kind"] == "modify_scene" and p["trigger_phrase"] == "我有点热", p
    p = cr.parse("把场景晚安的改成关灯")
    assert p and p["trigger_phrase"] == "晚安", p
    assert cr.parse("场景晚安改成关灯")["trigger_phrase"] == "晚安"      # 反向


def test_list_verbs_extended():
    """v1.1.24：列表说法补「列一下/列出来/列个」（旧表只有 列出/查看/看看…）。"""
    assert cr.parse("列一下场景")["kind"] == "list_scenes"
    assert cr.parse("列出来自动化")["kind"] == "list_automations"
    assert cr.parse("场景列表") is None            # 裸名词仍不接（设计）


def test_scene_create_without_dang():
    """v1.1.24：场景创建补无「当」的「我说X就Y」（含无连接词变体）。"""
    p = cr.parse("我说下班了就关灯")
    assert p and p["kind"] == "scene" and p["trigger_phrase"] == "下班了", p
    p = cr.parse("我说下班了关灯")
    assert p and p["kind"] == "scene" and p["trigger_phrase"] == "下班了", p
    assert cr.parse("打开客厅的灯") is None         # 反向：普通命令不得被创建句吞


def test_scene_delete_verb_first_name_middle():
    """v1.1.22 现场语序（办公实锤）：「删除我有点热语音场景」——动词在前、名字居中、
    「(语音)场景」在尾。旧表只认"场景在名前"或"动词在句尾" ⇒ 整句落兜底"我还不会"。"""
    for s in ("删除我有点热语音场景", "删掉我有点热场景", "删除我有点热场景"):
        p = cr.parse(s)
        assert p and p["kind"] == "delete_scene" and p["trigger_phrase"] == "我有点热", s
    # 反向：既有语序不回归；泛称/代词仍不得被当名字（清库与误删双护栏）
    assert cr.parse("删除场景我有点热")["trigger_phrase"] == "我有点热"
    assert cr.parse("删除所有场景") is None
    p = cr.parse("删除这个场景")
    assert p is None or p.get("trigger_phrase") != "这个"


# ── v1.0.34 生命周期句式（列出/删自动化/改场景）─────────────────
def test_list_phrases():
    for s in ("列出场景", "有哪些场景", "我有什么自动化", "查看语音场景",
              "场景都有哪些", "查下自动化", "有多少个场景"):
        p = cr.parse(s)
        assert p is not None, s
        want = "list_automations" if "自动化" in s else "list_scenes"
        assert p["kind"] == want, s
    assert cr.parse("场景") is None                    # 裸名词不接
    assert cr.parse("我的场景") is None
    assert cr.parse("打开场景灯") is None              # 非查询句不误伤


def test_list_phrases_with_leading_filler():
    """2026-09-10 真机补洞钉桩：「现在有哪些语音场景」曾整句落兜底（正则锚定不留
    前导时间/语气词位）。正例=用户自然说法，反例=裸名词/命令句/其他族不得被吞。"""
    for s in ("现在有哪些语音场景", "目前有哪些语音场景", "当前有哪些语音场景",
              "现在有哪些场景", "现在有几个语音场景", "现在有哪几个语音场景",
              "现在都有哪些场景", "看看现在有哪些场景", "现在有多少个语音场景",
              "现在有哪些自动化", "现在有哪些语音自动化", "现在有哪些语音场景呢"):
        p = cr.parse(s)
        assert p is not None, s
        want = "list_automations" if "自动化" in s else "list_scenes"
        assert p["kind"] == want, (s, p)
    for s in ("现在打开客厅的灯", "现在几点了", "所有灯", "现在删除场景", "观影模式"):
        p = cr.parse(s)
        assert p is None or p.get("kind") not in ("list_scenes", "list_automations"), (s, p)


def test_delete_automation_phrases():
    p = cr.parse("删除自动化2");      assert p["kind"] == "delete_automation" and cr.auto_target(p["target"]) == 2
    p = cr.parse("删掉自动化一");     assert cr.auto_target(p["target"]) == 1
    p = cr.parse("删除自动化第一个"); assert cr.auto_target(p["target"]) == 1
    p = cr.parse("删除自动化温度");   assert cr.auto_target(p["target"]) == "温度"
    p = cr.parse("把自动化2删了");    assert cr.auto_target(p["target"]) == 2
    p = cr.parse("删除自动化「窗帘」"); assert cr.auto_target(p["target"]) == "窗帘"
    p = cr.parse("删除自动化");       assert p["kind"] == "delete_automation" and cr.auto_target(p["target"]) is None


def test_modify_scene_phrases():
    p = cr.parse("把场景晚安改成关闭所有灯")
    assert p["kind"] == "modify_scene" and p["trigger_phrase"] == "晚安" and p["y"] == "关闭所有灯"
    p = cr.parse("删除场景晚安改成开窗帘")   # 删除正则在前：删字头但"改成"尾巴 → 删除组 x 吃掉整串失败回退
    p2 = cr.parse("场景「早安」换成播放音乐")
    assert p2["kind"] == "modify_scene" and p2["trigger_phrase"] == "早安"
    assert cr.parse("把场景晚安改成") is None          # 没给新动作


def test_delete_index_phrases():
    for s, n in (("删第2条", 2), ("删除第三条", 3), ("把第2个删了", 2),
                 ("删掉第1条", 1), ("删除第十一号", 11)):
        p = cr.parse(s)
        assert p is not None and p["kind"] == "delete_index" and p["n"] == n, s
    assert cr.parse("第二件事") is None
    assert cr.parse("打开第2个灯") is None          # 非删字头不接（fp 设备控制）
    assert cr.parse("删第999条")["n"] == 999        # 越界由 pipeline 层如实报


# ── v1.0.34 动词连排切分（真机原句驱动）──────────────────────────
def test_serial_split_real_case():
    # 真机日志原句：两动作间零标点
    assert cr.split_actions("同时打开办公室的空调关闭办公室的平台窗") == \
        ["打开办公室的空调", "关闭办公室的平台窗"]
    assert cr.split_actions("帮我同时打开办公室的空调关闭办公室的平台窗") == \
        ["打开办公室的空调", "关闭办公室的平台窗"]
    assert cr.split_actions("打开客厅的射灯关闭客厅的窗帘") == \
        ["打开客厅的射灯", "关闭客厅的窗帘"]
    assert cr.split_actions("打开空调调到26度") == ["打开空调", "调到26度"]
    assert cr.split_actions("关闭书房的灯并把空调设定为制冷") == \
        ["关闭书房的灯", "把空调设定为制冷"]


def test_serial_split_no_overcut():
    # 反例：单动作/含动形名词/把字句 → 不切碎（回退整句）
    for keep in ("关闭所有灯", "把窗帘拉上", "把卧室灯亮度调到百分之三十",
                 "调亮客厅灯"):
        got = cr.split_actions(keep)
        assert got == [keep], f"{keep} 被切碎: {got}"
    # 「暂停播放器」整段保留（2026-09 切点 播放(?!器) 护栏）：全句本来就是
    # 三件真动作，切 3 段是正解——旧版退整句只因「播放器」被腰斩成 4 碎段。
    assert cr.split_actions("关闭电视打开音响暂停播放器") == \
        ["关闭电视", "打开音响", "暂停播放器"]
    assert cr.split_actions("暂停播放器") == ["暂停播放器"]
    p = cr.parse("当我说打开办公室空调的时候就帮我同时打开办公室的空调"
                 "关闭办公室的平台窗")
    assert p["kind"] == "scene" and p["y"] == \
        "同时打开办公室的空调关闭办公室的平台窗"
