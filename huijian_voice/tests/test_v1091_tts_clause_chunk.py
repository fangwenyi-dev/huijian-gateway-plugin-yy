"""v1.0.91 钉：TTS 出帧单元切小到 ≤20 字（治"一句话分好几次说完"的中段空洞）。

现场实测依据（直连加载项 tts 通道的出帧时间戳探针，全新未合成句，绕开句级缓存）：
  63 字答复  → 首帧 4.02s；分句各自一次倒完 44~50 帧；分句之间空 3.48s / 3.68s
  9/14/22 字 → 合成 2.33/3.34/4.21s，音频 1.62/2.70/3.60s ⇒ RTF≈1.17~1.44
  拟合：合成耗时 ≈ 0.13×字数 + 1.2s 固定开销
旧切法只在 >40 字且含逗号时二次切 ⇒ 20~28 字分句要"合成 4~5 秒 / 播 3 秒"，
播放侧半路抽干。切到 20 字后单段空洞 ≤~0.5s，落在设备队列(2.05s)+HA 预灌
(1.536s) 的吸收范围内。
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TTS = ROOT / "core" / "tts.py"

# 现场实测常数（改引擎/换机器需重新拟合）
SEC_PER_CHAR = 0.13      # 合成秒/字
FIXED_S = 1.2            # 每次 generate 的固定开销（前端+推理启动）
CHARS_PER_SEC = 5.7      # 中文播报语速（字/秒，与 split/闸值同源近似）
SLACK_S = 2.05 + 1.536   # 设备播放队列 + HA 预灌水位（v2.1.51 / 1.0.89 起）


def _fn():
    src = TTS.read_text(encoding="utf-8")
    tree = ast.parse(src)
    chunk = None
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "_CHUNK_CHARS":
            chunk = ast.literal_eval(node.value)
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "_CHUNK_CHARS":
            chunk = ast.literal_eval(node.value)
    assert chunk, "_CHUNK_CHARS 常量失踪"
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "split_sentences":
            g = {"_CHUNK_CHARS": chunk}
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(TTS), "exec"), g)
            return g["split_sentences"], chunk
    raise AssertionError("split_sentences 未找到")


SPLIT, CHUNK = _fn()
norm = lambda s: "".join(s.split())     # 旧实现本就 strip 段边界空白


def test_no_content_lost():
    for s in ("目前有3个语音场景：1，我回来了就打开办公室射灯；2，晚安就关闭办公室射灯；"
              "3，我有点热就关闭办公室平开窗、打开办公室射灯",
              "好的，已经帮你办妥了。下一步要不要继续？可以！就这样：结束",
              "Set the room temperature to 24 degrees，然后关闭窗帘",
              "数字混排 30% 与 50%，以及 2026 年 9 月 16 日的日程安排确认一下"):
        out = SPLIT(s)
        assert out, s
        assert norm("".join(out)) == norm(s), f"分段丢字/改字：{out!r} ← {s!r}"


def test_every_clause_within_limit():
    corpus = [
        "展厅推纱窗开到百分之五十",
        "提醒一下明早九点有客户来访，先把展厅的射灯和空调准备好，再把电动窗帘合上",
        "无标点" * 40,                                  # 120 字纯硬切
        "甲" * 59 + "，" + "乙" * 40,                   # 对抗：逗号在 59 字之后
        "丙" * 4000,                                   # session 入口上限形态
    ]
    for s in corpus:
        out = SPLIT(s)
        bad = [p for p in out if len(p) > CHUNK]
        assert not bad, f"{s[:12]}… 出现超长段 {max(len(p) for p in out)} 字：{bad[:2]}"
        assert norm("".join(out)) == norm(s)


def test_short_sentences_not_fragmented():
    assert SPLIT("办公室射灯开了") == ["办公室射灯开了"], "短句被切碎＝白增韵律停顿"
    assert SPLIT("好的") == ["好的"]


def test_predicted_hole_fits_the_buffer():
    """最坏段（CHUNK 字）的空洞必须远小于设备+HA 的总余量，否则仍会抽干。"""
    audio = CHUNK / CHARS_PER_SEC
    synth = CHUNK * SEC_PER_CHAR + FIXED_S
    hole = synth - audio
    assert hole < SLACK_S, f"预测空洞 {hole:.2f}s ≥ 余量 {SLACK_S:.2f}s——切分粒度需再调小"
    assert hole < 1.2, f"单段空洞 {hole:.2f}s 已可闻，CHUNK={CHUNK} 偏大"


def test_first_frame_latency_and_the_tradeoff():
    """首帧＝第一段合成耗时；但**切越小反而更亏**——每次 generate 有 FIXED_S 固定
    开销，摊到更短音频上会让"每段空洞"变大（20 字空洞 0.29s，14 字反而 0.57s）。
    故 20 字是"首帧 ↔ 固定开销摊销"的折中：本钉只要求首帧低于现场实测值(4.9s)
    且空洞保持可吸收；真要再降首帧得从引擎侧（线程/精度/CPU 配额）下手，不是切句。
    """
    audio = CHUNK / CHARS_PER_SEC
    synth = CHUNK * SEC_PER_CHAR + FIXED_S
    assert synth < 4.9, f"首帧 {synth:.2f}s 未优于现场实测 4.9s"
    assert synth - audio < 1.2, f"段空洞 {synth - audio:.2f}s 已可闻"
    # 反证：更小不等于更好——14 字段的空洞必须比 20 字段大（守住这个折中不被"顺手切更细"推翻）
    h14 = (14 * SEC_PER_CHAR + FIXED_S) - 14 / CHARS_PER_SEC
    assert h14 > synth - audio, "14 字空洞未大于 20 字——固定开销模型变了，重估 CHUNK"
