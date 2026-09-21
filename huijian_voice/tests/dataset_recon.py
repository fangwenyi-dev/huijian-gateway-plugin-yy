# -*- coding: utf-8 -*-
"""意图数据集对账复算（v1.1.1 批次起的常驻审计工具）。

两份慧尖数据集（SFT合并 + 完整数据集）全量 user 句 → 本地 NLU 级联复跑，
按**数据集自己声明的 intent/slot** 逐句裁决。与 tests/golden_gen.py 同族
（golden=82 句人工终审快照，本工具=786 句全量台账）。

口径（与"命中率"一词绑死，防各次复算口径漂移）：
  · 裁决面 = FastPath（① 字面表 + ②③ 剥离 + ④ 场景 + ⑤ T1 TextCNN 真资产）。
    klar 引擎（需 VM HA）与 LLM 兜底**不在本面**——它们是级联下游档，
    计入会把"本地不接管"误判成"整体失能"，掩盖真实缺口。
  · 数据集期望 intent 从 assistant 句的 `调用intent: X, 参数: {…}` 抽取；
    一句多 intent（并列双动作）单列 COMPOUND，不计 in-scope 分母。
  · 状态/时间/场景创建/播放族本层设计上不接管（交查询族/创建侧/上层），
    按 SCOPE 表单列 OUT_OF_SCOPE，不混进 miss。
  · HIT=意图对；ARG=意图对但关键槽错（Adjust 比 attribute+delta、
    ControlWindow 比 action、Turn* 比目标区域是否落地）；WRONG=意图错；
    MISS=本层 None。后三类进缺口台账。

用法（cwd=huijian_voice）：
    python tests/dataset_recon.py                 # 默认数据集目录
    python tests/dataset_recon.py --dir "E:/备份/小智服务器相关/061701/documents/意图数据集"
    python tests/dataset_recon.py --only-missing --out _recon.txt
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_recon_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

DEFAULT_DIR = "E:/AI/.trae/documents/意图数据集"
FILES = ("慧尖HA集成_SFT训练数据集_合并.jsonl", "慧尖HA集成完整数据集.jsonl")

# 数据集期望 intent → 本地裁决面归属
SCOPE = {
    "TurnDeviceOn": "fp", "TurnDeviceOff": "fp", "ControlWindow": "fp",
    "AdjustDeviceAttribute": "fp", "SetDeviceMode": "fp", "PauseDevice": "fp",
    "HassClimateSetTemperature": "fp", "HassLightSet": "fp",
    "HassTurnOn": "fp", "HassTurnOff": "fp", "HassUnlock": "fp", "HassLock": "fp",
    # 本层设计上不接管（级联下游档）
    "huijianGetLiveContext": "query",       # 状态查询族
    "GetDateTime": "query",
    "HassGetCurrentTime": "query",
    "HassCreateVoiceScene": "create",       # 场景创建侧
    "HassTriggerVoiceScene": "create",
    "HassDeleteVoiceScene": "create",
    "HassListVoiceScenes": "create",
    "HassCreateAutomation": "create",
    "PlayMusic": "music",
    "HassBroadcast": "broadcast",
    "HassCancelAllTimers": "other",
}
_PARAM_RE = re.compile(r'调用intent:\s*(\w+)\s*,\s*参数:\s*(\{.*?\})\s*$')
_INTENT_ONLY_RE = re.compile(r'调用intent:\s*(\w+)')


class FakeScenes:
    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def refresh(self, force=False):
        pass

    def check(self, text):
        return None

    async def verify_or_refresh(self, phrase):
        return False


def load_rows(d: str):
    """→ [(文件, 行号, user 句, [(intent, params), ...])]，assistant 全 intent 抽净。"""
    rows = []
    for fn in FILES:
        p = Path(d) / fn
        if not p.exists():
            raise SystemExit(f"数据集缺失：{p}")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            msgs = obj.get("messages") or []
            user = next((m.get("content", "") for m in msgs if m.get("role") == "user"), "")
            ans = next((m.get("content", "") for m in reversed(msgs)
                        if m.get("role") == "assistant"), "")
            steps = []
            for seg in ans.split("\n"):
                m = _PARAM_RE.search(seg.strip())
                if m:
                    try:
                        steps.append((m.group(1), json.loads(m.group(2))))
                    except Exception:  # noqa: BLE001 数据集个别行参数截断
                        steps.append((m.group(1), {}))
            if not steps:
                for m in _INTENT_ONLY_RE.finditer(ans):
                    steps.append((m.group(1), {}))
            if user:
                rows.append((fn, i, user.strip(), steps))
    return rows


def _expect_key(intent, params):
    """数据集期望的"可比较指纹"：同句多形 = 数据集自身不一致。"""
    p = params or {}
    return (intent, str(p.get("attribute", "")), str(p.get("delta", "")),
            str(p.get("action", "")).lower(), str(p.get("mode", "")))


def find_ambiguous(rows):
    """同一 user 句在两份数据集里被标了 2 种以上答案 → 数据集内部矛盾集。

    实锤形：「把客厅灯调亮一点」一处标 +10、另一处标 +30（我方 +20）——任何
    实现都不可能同时满足，这类句失分不该记在 NLU 头上。
    """
    seen = collections.defaultdict(set)
    for _fn, _ln, user, steps in rows:
        if steps:
            seen[user.strip()].add(_expect_key(*steps[0]))
    return {t for t, ks in seen.items() if len(ks) > 1}


def window_label_conflict(text, exp_intent, plan):
    """数据集**自身口诀**冲突：其说明段明文"看到「窗」字 → ControlWindow(button)、
    看到「帘」字 → TurnDeviceOn/Off"，但逐行标注把「打开客厅的窗户」这类窗句标成
    Turn*。本地按口诀产 ControlWindow 是 v1.0.40 实机定案（开窗器=button 域，
    按 Turn* 找开关设备必错）。此类句是数据集内部矛盾，不是 NLU 缺口。
    """
    try:
        from core.nlu.fast_path import _CURTAIN_ROOT_WORDS, _window_type
        if exp_intent not in ("TurnDeviceOn", "TurnDeviceOff"):
            return False
        if plan is None or plan.intent != "ControlWindow":
            return False
        if any(w in text for w in _CURTAIN_ROOT_WORDS):
            return False
        return bool(_window_type(text)
                    or text.rstrip("的地得开关闭合上下了").endswith(("窗", "窗户")))
    except Exception:  # noqa: BLE001 判据故障=不豁免（保守计入我方缺口）
        return False


def _plan_targets(plan):
    out = []
    for t in (plan.args.get("target") or []):
        if t.get("area"):
            out.append(str(t["area"]))
        for d in (t.get("devices") or []):
            if d.get("name"):
                out.append(str(d["name"]))
            out.extend(f"dom:{x}" for x in (d.get("domains") or []))
    return out


def judge(plan, exp_intent, exp_params):
    """→ (verdict, 说明)。verdict ∈ HIT/ARG/WRONG/MISS。"""
    if plan is None:
        return "MISS", ""
    if plan.intent != exp_intent:
        # 温度改道是**同源代码级**设计（H1：无设备名的绝对温度句走 HA 内置
        # HassClimateSetTemperature，见 fast_path._build_plan），双向等价：
        # 数据集标 Adjust+temperature 而我方改道内置意图，执行结果同一条命令。
        same_temp_lane = {
            ("HassClimateSetTemperature", "AdjustDeviceAttribute"),
            ("AdjustDeviceAttribute", "HassClimateSetTemperature"),
        }
        if ((exp_intent, plan.intent) in same_temp_lane
                and (str(plan.args.get("attribute", "")) == "temperature"
                     or exp_params.get("attribute") == "temperature"
                     or "temperature" in plan.args or "temperature" in exp_params)):
            return "HIT", "温度改道同源"
        return "WRONG", f"现测 {plan.intent}"
    if exp_intent == "AdjustDeviceAttribute":
        ea, ed = exp_params.get("attribute"), str(exp_params.get("delta", ""))
        if ea and plan.args.get("attribute") != ea:
            return "ARG", f"attribute 现测 {plan.args.get('attribute')} 期望 {ea}"
        if ed and str(plan.args.get("delta", "")) != ed:
            return "ARG", f"delta 现测 {plan.args.get('delta')} 期望 {ed}"
        return "HIT", ""
    if exp_intent == "ControlWindow":
        ea = str(exp_params.get("action", "")).lower()
        pa = str(plan.args.get("action", "")).lower()
        if ea and pa != ea:
            if plan.args.get("position") is not None and ea in ("open", ""):
                return "HIT", "百分比开度形态"
            return "ARG", f"action 现测 {pa or None} 期望 {ea}"
        return "HIT", ""
    if exp_intent in ("SetDeviceMode",):
        em, pm = exp_params.get("mode"), plan.args.get("mode")
        if em and pm != em:
            return "ARG", f"mode 现测 {pm} 期望 {em}"
        return "HIT", ""
    return "HIT", ""


async def amain(args):
    from core.nlu import targets as T
    from core.nlu.fast_path import FastPath
    from core.nlu.textcnn import TextCNN
    from core.settings import Settings

    tc = TextCNN(Path(os.environ["HUIJIAN_NLU_DATA"]))
    tc._ensure()
    fp = FastPath(FakeScenes(), tc, Settings(Path(os.environ["HUIJIAN_DATA"]) / "recon.json"))

    rows = load_rows(args.dir)
    ambiguous = find_ambiguous(rows)
    tally = collections.Counter()
    by_intent = collections.defaultdict(collections.Counter)
    missing = []
    for fn, ln, user, steps in rows:
        if not steps:
            tally["NO_EXPECTATION"] += 1
            continue
        T.clear_vocab()
        exp_intent, exp_params = steps[0]
        bucket = SCOPE.get(exp_intent, "other")
        if len(steps) > 1:
            tally["COMPOUND"] += 1
            by_intent[exp_intent]["COMPOUND"] += 1
            if not args.compound:
                continue
        if bucket != "fp":
            tally[f"OOS_{bucket}"] += 1
            by_intent[exp_intent][f"OOS_{bucket}"] += 1
            continue
        try:
            plan = await fp.match(user)
        except Exception as e:  # noqa: BLE001 审计器绝不因单句崩掉整轮
            tally["CRASH"] += 1
            missing.append((user, exp_intent, exp_params, f"CRASH {e}"))
            continue
        verdict, why = judge(plan, exp_intent, exp_params)
        if verdict == "WRONG" and window_label_conflict(user, exp_intent, plan):
            tally["CONFLICT"] += 1
            by_intent[exp_intent]["CONFLICT"] += 1
            continue
        if verdict in ("MISS", "ARG", "WRONG") and user in ambiguous:
            tally["AMBIGUOUS"] += 1
            by_intent[exp_intent]["AMBIGUOUS"] += 1
            continue
        tally[verdict] += 1
        by_intent[exp_intent][verdict] += 1
        if verdict in ("MISS", "ARG", "WRONG", "CRASH"):
            missing.append((user, exp_intent, exp_params, f"{verdict} {why}".strip()))

    inscope = sum(tally[v] for v in ("HIT", "ARG", "WRONG", "MISS", "CRASH"))
    excused = tally["CONFLICT"] + tally["AMBIGUOUS"]
    if args.out:
        args.out = io.open(args.out, "w", encoding="utf-8")
    out = args.out or sys.stdout
    w = (lambda s: out.write(s + "\n")) if hasattr(out, "write") else print
    w("=== 数据集对账复算（裁决面=FastPath 本地档，口径见文件头）===")
    w(f"数据目录：{args.dir}")
    w(f"数据集行数（user 句）：{len(rows)}")
    for k in ("OOS_query", "OOS_create", "OOS_music", "OOS_broadcast", "OOS_other",
              "COMPOUND", "NO_EXPECTATION"):
        if tally[k]:
            w(f"  不计分母 {k:14s} {tally[k]}")
    w(f"  in-scope 句数         {inscope}（另有 {excused} 句经数据集一致性核查后豁免，见下）")
    for v in ("HIT", "ARG", "WRONG", "MISS", "CRASH"):
        if tally[v]:
            w(f"  {v:5s}{'':9s} {tally[v]:4d}  "
              f"{(100.0 * tally[v] / inscope if inscope else 0):5.1f}%")
    w(f"  CONFLICT   {tally['CONFLICT']:4d}  期望标注与数据集自身口诀矛盾"
      f"（窗句标 Turn*，本地按口诀产 ControlWindow）")
    w(f"  AMBIGUOUS  {tally['AMBIGUOUS']:4d}  同句在两份数据集被标 2 种以上答案"
      f"（任何实现都无法同时满足）")
    hit = tally["HIT"]
    w(f"  → 严格 HIT {(100.0 * hit / inscope if inscope else 0):.1f}% / "
      f"意图对上(含槽错) {(100.0 * (hit + tally['ARG']) / inscope if inscope else 0):.1f}% / "
      f"真缺口 {(tally['WRONG'] + tally['MISS'] + tally['CRASH'])} 句")
    w("")
    w("=== 分 intent 明细（in-scope）===")
    for it in sorted(by_intent, key=lambda x: -sum(by_intent[x].values())):
        c = by_intent[it]
        n = sum(c[v] for v in ("HIT", "ARG", "WRONG", "MISS", "CRASH"))
        if not n:
            continue
        w(f"{it:26s} n={n:4d} HIT={c['HIT']:4d} ARG={c['ARG']:3d} "
          f"WRONG={c['WRONG']:3d} MISS={c['MISS']:3d} "
          f"豁免(口诀矛盾/同句多标)={c['CONFLICT']:3d}/{c['AMBIGUOUS']:3d}")
    w("")
    w(f"=== 缺口台账（{len(missing)} 句）===")
    for user, it, p, why in missing:
        if args.only_missing and why.startswith("HIT"):
            continue
        w(f"{user}\t{it}\t期望槽={json.dumps(p, ensure_ascii=False)}\t{why}")
    if args.out:
        out.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--out", default=None)
    ap.add_argument("--only-missing", action="store_true")
    ap.add_argument("--compound", action="store_true", help="并列双动作句也进裁决")
    a = ap.parse_args()
    asyncio.run(amain(a))
