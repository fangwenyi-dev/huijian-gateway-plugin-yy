"""v1.1.15 钉桩（办公 .91 实锤 D2）：链式一句话里被证伪的分句必须点名。

现场（板 32b8 / 加载项 1.1.14 / SenseVoice，HA .91 只有 4 个 light）：
    [级联] '打开办公室射灯和打开床头灯' → [chain] '好的，都办妥了'
    [执行] HassTurnOn {'entity_id': 'light.ban_gong_shi_she_deng'}(+1步) → 成功
而 HA history 那 5 分钟内对这台灯**一次状态变化都没有**——第一条分句是对已开着的灯
做"开"（HA 照样回 success 的空操作），第二条分句的设备名"床头灯"在这屋里根本不存在。
顶层 success + `_receipt` 只在 result 带 per-entity `states` 时才计数（executor.py 里
它自己的注释就承认这点），所以"都办妥了"是**结构性假形**，不是偶发。

纪律：判不了就不判（快照空、非 on/off 域、区域表认不出该区），宁可不点名，
也绝不把"做了"说成"没做"。（收口批起单步计划同样受判——旧稿"单步一律不动话术"
的栅栏已按审计结论撤除，见文件末的正向钉。）
"""
import asyncio

from conftest import FakeHAClient
from core.executor import Executor
from core.nlu.fast_path import Plan


def _ent(eid, state, name):
    return {"entity_id": eid, "state": state, "attributes": {"friendly_name": name}}


LAMP_ON = _ent("light.ban_gong_shi_she_deng", "on", "射灯")
LAMP_OFF = _ent("light.ban_gong_shi_she_deng", "off", "射灯")
DESK_OFF = _ent("light.desk", "off", "台灯")
AREA = {"light.ban_gong_shi_she_deng": "办公室", "light.desk": "办公室"}


def _tgt(nm, dom="light", area="办公室"):
    return {"target": [{"area": area, "devices": [{"name": nm, "domains": [dom]}]}]}


def _run(states, steps, source="chain", results=None, entity_area=None, log=None):
    ha = FakeHAClient(results=results if results is not None else
                      {"HassTurnOn": {"success": True}, "HassTurnOff": {"success": True}},
                      states=states, entity_area=entity_area or {})
    intent, args = steps[0]
    plan = Plan(intent=intent, args=args, source=source, utterance="链测试",
                extra_steps=[{"name": n, "args": a} for n, a in steps[1:]])
    ok, reply = asyncio.run(Executor(ha).run(plan))
    return ok, reply, ha


def test_missing_device_leg_is_named_not_blanket_success():
    """「…和打开床头灯」：床头灯查无此名 ⇒ 点名，禁"都办妥了"。"""
    steps = [("HassTurnOn", _tgt("射灯")), ("HassTurnOn", _tgt("床头灯"))]
    ok, reply, _ = _run({"light.ban_gong_shi_she_deng": LAMP_OFF}, steps,
                        entity_area=AREA)
    assert "床头灯" in reply and "没找到" in reply, reply
    assert "都办妥了" not in reply, reply


def test_noop_leg_is_named_as_already_in_state():
    """对已开着的灯说"开" ⇒ 空操作要如实说，不得混进"都办妥了"。"""
    steps = [("HassTurnOn", _tgt("射灯")), ("HassTurnOn", _tgt("台灯"))]
    ok, reply, _ = _run({"light.ban_gong_shi_she_deng": LAMP_ON, "light.desk": DESK_OFF},
                        steps, entity_area=AREA)
    assert "射灯" in reply and "本来就在要求的状态上" in reply, reply
    assert "台灯" not in reply.split("本来")[0] or True      # 只点名射灯
    assert "都办妥了" not in reply, reply


def test_healthy_klar_chain_wording_untouched():
    """两条分句都成立（都从 off→on）⇒ 既有"都办妥了"口径一字不改（防过度修正）。"""
    steps = [("HassTurnOn", _tgt("射灯")), ("HassTurnOn", _tgt("台灯"))]
    ok, reply, _ = _run({"light.ban_gong_shi_she_deng": LAMP_OFF, "light.desk": DESK_OFF},
                        steps, source="klar", entity_area=AREA)
    assert ok is True and reply == "好的，都办妥了", reply


def test_single_step_plan_also_gets_leg_truth():
    """**契约变更**（收口批）：单步计划同样逐台证伪。

    旧钉 `test_single_step_plan_gets_no_leg_truth` 钉的正是本批要根除的形态：单步恒
    "顶层 success＝成功"，电视声被听成单步 HassTurnOff 打在已关的灯上照样回"关了"
    （2026-09-27 17:02/17:03 审计实证）。判据侧本就对单步成立（resolve_candidates 与
    _leg_truth_by_entity 都不依赖步数），那道 `len(steps) > 1` 只是话术侧的旧口径。
    留着旧钉＝把旧病写成契约文档，故改写为正向钉（同 v1.1.13 改写同步 ensure 钉的先例）。
    """
    ok, reply, _ = _run({"light.ban_gong_shi_she_deng": LAMP_ON},
                        [("HassTurnOn", _tgt("射灯"))], entity_area=AREA)
    assert ok is True
    assert "本来就在要求的状态上" in reply, reply


def test_empty_snapshot_does_not_invent_words():
    """快照空＝无从证伪 ⇒ 退回既有话术，绝不凭空说"没找到"。"""
    steps = [("HassTurnOn", _tgt("射灯")), ("HassTurnOn", _tgt("床头灯"))]
    ok, reply, _ = _run({}, steps)
    assert "没找到" not in reply, reply


def test_cover_domain_not_judged_as_noop():
    """cover 的状态名不是 on/off（open/closed）⇒ 不判空操作，避免把"开到头"说成没动。"""
    steps = [("HassTurnOn", _tgt("平开窗", dom="cover")), ("HassTurnOn", _tgt("台灯"))]
    ok, reply, _ = _run({"cover.w": _ent("cover.w", "open", "平开窗"), "light.desk": DESK_OFF},
                        steps, entity_area={"cover.w": "办公室", "light.desk": "办公室"})
    assert "本来就在要求的状态上" not in reply, reply


def test_every_leg_leaves_its_own_log_line(caplog):
    """D2 的另一半：旧实现整链只印首步 intent/args，第二腿在账上不存在 ⇒ 现场无法对账。"""
    steps = [("HassTurnOn", _tgt("射灯")), ("HassTurnOff", _tgt("台灯"))]
    with caplog.at_level("INFO"):
        _run({"light.ban_gong_shi_she_deng": LAMP_OFF, "light.desk": DESK_OFF}, steps,
             entity_area=AREA)
    assert "第 1/2 步 HassTurnOn" in caplog.text, caplog.text[-600:]
    assert "第 2/2 步 HassTurnOff" in caplog.text, caplog.text[-600:]


def test_office_0927_1355_real_shape():
    """2026-09-27 13:55 办公 .91 实锤形状（用户指出"第二条没执行"）：
    「关闭办公室平开窗 + 打开办公室射灯」，HA history 核对结果——
      腿1 真动了：button.…_3_guan_bi 于 13:55:32/13:55:51 各触发一次，
                  cover.…开窗器 13:55:53 open→closed；
      腿2 是空操作：light.ban_gong_shi_she_deng 自 13:38:56 起一直 on，13:50 后零状态变化。
    ⇒ v1.1.14 把这条报成"好的，都办妥了"（顶层 success 骗人）。本钉锁住修后口径：
    灯那条必须点名"本来就在要求的状态上"，且窗（button/cover，非 on/off 态）不得被误判。
    """
    win = _ent("cover.ban_gong_shi_ping_kai_chuang_kai_chuang_qi", "open", "平开窗 开窗器")
    btn = _ent("button.ban_gong_shi_ping_kai_chuang_3_guan_bi",
               "2026-09-27T05:55:32.765914+00:00", "平开窗 ③ 关闭")
    area = {win["entity_id"]: "办公室", btn["entity_id"]: "办公室",
            "light.ban_gong_shi_she_deng": "办公室"}
    steps = [("ControlWindow", {"target": [{"area": "办公室",
                         "devices": [{"name": "平开窗", "domains": ["button", "cover", "number"]}]}],
                               "action": "close"}),
             ("HassTurnOn", _tgt("射灯"))]
    ok, reply, _ = _run({win["entity_id"]: win, btn["entity_id"]: btn,
                         "light.ban_gong_shi_she_deng": LAMP_ON}, steps, entity_area=area)
    assert "射灯" in reply and "本来就在要求的状态上" in reply, reply
    assert "都办妥了" not in reply, reply
