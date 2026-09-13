"""golden_utterances.jsonl 再生成器（v1.0.62 P1-4，人工审阅后落仓）。

用法：cd huijian_voice && python tests/golden_gen.py > /tmp/golden_new.jsonl
逐行人工 diff（意图变了必须有解释），确认后覆盖 nlu_data/golden_utterances.jsonl。

契约面（刻意只覆盖**确定性本地引擎**，不 mock 级联外壳）：
  FastPath.match（字面表 T0/剥壳/前缀/同音 + 场景触发 + TextCNN T1 真资产）
  QueryZone.answer（fake ha 固定实体集）。compound/confirm/LLM 各环已有
  专项行为钉千余条，不在此重复——golden 钉的是"这句话该被哪档怎么理解"。
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.nlu.fast_path import FastPath                     # noqa: E402
from core.nlu.textcnn import TextCNN                        # noqa: E402
from core.nlu.query import QueryZone                        # noqa: E402

# 语料纪律（2026-09-22 校准）：全部句子必须**语义上应被 fp/query/scene 接住**——
# 裸「空调」缺区域守卫等设计性拒绝句不收（那是守卫专项钉的活，不是 golden 的）。
# 场景触发词走 FakeScenes 固定集（场景契约=触发词表判等，环境可复制）。
SENTENCES = [
    # ── 字面表族（T0）──
    "打开客厅的灯", "把客厅的灯打开", "开灯", "关灯", "关掉卧室空调", "把书房空调关掉",
    "打开窗帘", "拉上窗帘", "打开风扇", "客厅空调调到26度", "设为26度",
    "卧室空调设为制冷", "客厅空调设为制热", "书房空调开除湿", "卧室空调自动模式",
    "亮度调高", "调亮一点", "亮度调到50%", "色温调低", "风量大一点",
    "回家模式", "观影模式", "睡眠模式", "离家模式",
    "打开加湿器", "关掉加湿器", "打开净化器", "打开热水器", "关掉热水器",
    "所有灯打开", "全部关闭",
    "内倒展厅的窗户", "外开窗打开", "打开大门", "锁上大门", "给大门上锁",
    "打开卧室的空调", "关闭书房的灯", "把厨房灯关了", "打开卫生间的排风扇",
    # ── T1 泛化（字面表外、模型兜语义）──
    "开一下灯呗", "麻烦把客厅空调关了", "灯打开吧", "让风扇转起来",
    "暗一点", "亮一些", "窗帘开一半", "把声音关掉",
    "净化器启动", "加湿器开起来", "热水器关闭",
    # ── ASR 同音/纠错（corrector 表内实词）──
    "打开客厅的空条", "关掉门所", "打开电士", "湿式器打开",
    # ── 查询族 ──
    "客厅多少度", "卧室湿度多少", "现在几点了", "今天星期几", "办公室有没有人",
    "客厅空调设定温度多少", "灯现在多亮", "窗户电池电量多少", "客厅有多少灯开着",
    # ── 负样本（本地不该接：OOS/闲聊/外域）──
    "今天天气怎么样", "讲个笑话", "你好啊", "你是谁", "推荐一本书",
    "帮我写首诗", "股票怎么样", "播放周杰伦的歌", "明天会下雨吗",
    "翻译成英文", "最近的餐厅在哪", "怎么注册账号",
    # ── 易混对（同形不同域）──
    "打开灯", "关灯", "客厅温度多少", "客厅空调调到24度", "湿度多少", "加湿器打开",
]


class FakeScenes:
    """场景契约=触发词判等表（HA 场景名），golden 固定这四个词可复制生产行为。"""
    triggers = {"回家模式", "观影模式", "睡眠模式", "离家模式"}

    async def refresh(self, force=False):
        pass

    def check(self, text):
        for t in self.triggers:
            if text == t or text.startswith(t):
                return t
        return None

    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def verify_or_refresh(self, phrase):
        return phrase in self.triggers


class FakeHa:
    _areas = {}          # 区域注册表：golden 环境走后缀启发式提取
    _entity_area = {}    # 实体→区域映射空 → 传感器全量唯一命中路径

    async def states(self):                      # dict[eid, entity]（_sensor_answer 消费形）
        return {e["entity_id"]: e for e in await self.find_entities()}

    async def find_entities(self, area="", domains=()):
        return [
            {"entity_id": "sensor.lamp_temp", "state": "26.5",
             "attributes": {"friendly_name": "客厅温度", "device_class": "temperature",
                            "unit_of_measurement": "°C"}},
            {"entity_id": "sensor.lamp_hum", "state": "55",
             "attributes": {"friendly_name": "客厅湿度", "device_class": "humidity",
                            "unit_of_measurement": "%"}},
            {"entity_id": "binary_sensor.office_pir", "state": "on",
             "attributes": {"friendly_name": "办公室人感", "device_class": "occupancy"}},
            {"entity_id": "climate.ac1", "state": "heat",
             "attributes": {"friendly_name": "空调", "temperature": 26,
                            "unit_of_measurement": "°C"}},
            {"entity_id": "light.l1", "state": "on",
             "attributes": {"friendly_name": "筒灯", "brightness": 180}},
            {"entity_id": "sensor.win_bat", "state": "87",
             "attributes": {"friendly_name": "平开窗电池", "device_class": "battery",
                            "unit_of_measurement": "%"}},
        ]

    async def get_config(self):
        return {"time_zone": "Asia/Shanghai"}


class S:
    def get(self, k, d=None):
        return {"nlu.textcnn_enabled": True, "nlu.query_local": True}.get(k, d)


async def main():
    tc = TextCNN(Path(os.environ.get("HUIJIAN_NLU_DATA", "nlu_data")))
    tc._ensure()
    fp = FastPath(FakeScenes(), tc, S())
    qz = QueryZone(FakeHa(), S())
    for text in SENTENCES:
        row = {"text": text}
        try:
            plan = await fp.match(text)          # 返回 Optional[Plan]（trace 在 plan.trace 内）
        except Exception as e:                      # 生成器要能跑完（坏句记 error）
            plan, row["error"] = None, str(e)[:80]
        if plan is not None:
            row.update(intent=plan.intent, source=plan.source)
        else:
            ans = await qz.answer(text)
            if ans:
                row.update(intent="_query", source="query")
            else:
                row.update(intent=None, source="miss")
        print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
