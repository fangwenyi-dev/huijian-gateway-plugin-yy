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


def test_auto_numeric_no_connector_拒绝():
    assert cr.parse("当温度超过28度打开空调") is None  # 无锚点宁缺勿错


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
    for s in ("删除场景", "把场景删了", "删除所有场景", "删了"):
        p = cr.parse(s)
        assert p is None or p.get("kind") != "delete_scene", s


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
    # 反例：单动作/含动形名词/把字句/超3段嫌疑 → 不切碎（回退整句）
    for keep in ("关闭所有灯", "把窗帘拉上", "把卧室灯亮度调到百分之三十",
                 "调亮客厅灯", "关闭电视打开音响暂停播放器"):
        got = cr.split_actions(keep)
        assert got == [keep], f"{keep} 被切碎: {got}"
    p = cr.parse("当我说打开办公室空调的时候就帮我同时打开办公室的空调"
                 "关闭办公室的平台窗")
    assert p["kind"] == "scene" and p["y"] == \
        "同时打开办公室的空调关闭办公室的平台窗"
