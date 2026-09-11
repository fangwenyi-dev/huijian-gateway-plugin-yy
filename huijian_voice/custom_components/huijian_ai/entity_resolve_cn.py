"""中文传感器描述 → 候选收束（纯函数，零 HA 依赖，tests 可直 load）。

v1.0.42 生产缺陷修复：语音创建自动化说「当办公室的温度大于三十度…」，
core 侧 creation.py 按设计把条件描述原文放进 trigger.entity_id（descriptor），
集成侧 _resolve_entity_id 负责解析——但旧解析：
  ① 中文无空格不分词，整串（含「的」）做子串匹配 → 「办公室的温度」永远不中；
  ② 完全不读 HA 区域(area registry)：现代 HA 的传感器名往往就叫「温度」，
     房间靠区域绑定，名字里根本没有「办公室」；
  ③ 区域无关的 class-unique 兜底会「指鹿为马」：说办公室、全屋唯一温度
     传感器在卧室 → 静默绑卧室（错绑比失败更危险）。

本模块做三件事：类别词切分（温度/湿度/…→device_class 集合）、区域检测
（描述含已知区域名/别名即命中，用真实区域表而非硬编码映射）、候选打分收束。
守卫：描述命中区域后，绝不跨区域回退绑全屋同类传感器。
"""
from __future__ import annotations

import re

# 类别词 → device_class。列表顺序即消费顺序：**长的在前**（温湿度 先于 温度/湿度，
# 门磁 先于 单字「门」），命中一个词只消费一次，残串留给名字匹配。
_CLASS_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("温湿度", ("temperature", "humidity")),
    ("光照度", ("illuminance",)),
    ("照度", ("illuminance",)),
    ("光照", ("illuminance",)),
    ("亮度", ("illuminance",)),
    ("温度", ("temperature",)),
    ("体温", ("temperature",)),
    ("湿度", ("humidity",)),
    ("甲醛", ("formaldehyde",)),
    ("pm2.5", ("pm25",)),
    ("pm25", ("pm25",)),
    ("pm10", ("pm10",)),
    ("tvoc", ("voc",)),
    ("voc", ("voc",)),
    ("二氧化碳", ("carbon_dioxide",)),
    ("一氧化碳", ("carbon_monoxide",)),
    ("co2", ("carbon_dioxide",)),
    ("天然气", ("gas",)),
    ("燃气", ("gas",)),
    ("煤气", ("gas",)),
    ("烟雾", ("smoke",)),
    ("烟感", ("smoke",)),
    ("人体", ("motion", "presence")),
    # v1.0.42 雷达=mmWave 存在传感器（「当雷达检测到有人」的 desc 就是「雷达」）
    ("毫米波", ("motion", "presence")),
    ("雷达", ("motion", "presence")),
    ("有人", ("presence",)),
    ("存在", ("presence",)),
    ("移动", ("motion",)),
    ("漏水", ("moisture",)),
    ("水浸", ("moisture",)),
    ("门磁", ("door",)),
    ("窗磁", ("window",)),
    ("电压", ("voltage",)),
    ("电流", ("current",)),
    ("功率", ("power",)),
    ("能耗", ("energy",)),
    ("用电", ("energy",)),
    ("电量", ("battery",)),
    ("电池", ("battery",)),
    ("气压", ("pressure",)),
    ("信号", ("signal_strength",)),
    # 单字兜底放最后，避免吞掉「门口/窗户」这类更长短语
    ("门", ("door",)),
    ("窗", ("window",)),
)

# 中文标签（错误话术用）
CN_LABEL = {
    "temperature": "温度", "humidity": "湿度", "illuminance": "光照",
    "formaldehyde": "甲醛", "pm25": "PM2.5", "pm10": "PM10", "voc": "VOC",
    "carbon_dioxide": "二氧化碳", "carbon_monoxide": "一氧化碳", "gas": "燃气",
    "smoke": "烟雾", "motion": "人体移动", "presence": "有人",
    "moisture": "水浸", "door": "门", "window": "窗", "voltage": "电压",
    "current": "电流", "power": "功率", "energy": "能耗", "battery": "电量",
    "pressure": "气压", "signal_strength": "信号",
}


def norm(s: str) -> str:
    """小写、下划线→空格、压空白；不做中文切分（切分由 detect_classes 负责）。"""
    return re.sub(r"\s+", " ", (s or "").lower().replace("_", " ")).strip()


def detect_classes(desc: str) -> tuple[list[str], str]:
    """从描述切出 device_class 集合与残串（去掉类别词/「的」/空白）。

    「办公室的温度」→ (["temperature"], "办公室")
    「客厅温湿度」  → (["temperature", "humidity"], "客厅")
    """
    text = norm(desc)
    classes: list[str] = []
    rest = text
    for word, cls in _CLASS_WORDS:
        if word in rest:
            rest = rest.replace(word, " ", 1)
            for c in cls:
                if c not in classes:
                    classes.append(c)
    rest = norm(rest.replace("的", " ").replace("地", " "))
    return classes, rest


def _name_hit(cand: dict, token: str) -> bool:
    """单个残串 token 对实体名/id 的子串命中。实体名侧先去「的」，
    让描述「办公室温度」与实体名「办公室的温度」互相兼容。"""
    if not token:
        return False
    name = norm(cand.get("name", "")).replace("的", "")
    eid = norm(cand.get("entity_id", ""))
    return token in name or token in eid


def pick(desc: str, cands: list[dict], areas: dict[str, str]) -> dict:
    """收束一条中文/英文传感器描述。

    desc:   trigger.entity_id 里的描述原文；
    cands:  [{"entity_id","name","dc","area"}]，area 为**规范区域名**
            （调用方限定 sensor/binary_sensor）；
    areas:  {归一化区域名/别名: 规范区域名}（来自区域注册表）。

    返回 dict：
      best        解析出的 entity_id（唯一高置信），否则 None；
      ambiguous   多候选 entity_id 列表（供「请指定」话术）；
      no_in_area  True=描述命中区域但该区域无此类传感器（禁止跨区回退）；
      others      其他区域同类的邻近候选（仅 no_in_area 时有意义，供提示）；
      area_hit / classes / residue 解析中间量（供话术与调试）。
    """
    d = norm(desc)
    classes, residue = detect_classes(d)
    hit_tokens = [t for t in areas if t and t in d]
    area_hit = max(hit_tokens, key=len) if hit_tokens else None
    canon = areas.get(area_hit or "", "")

    # 残串去掉区域词：「办公室空调温度」的名字线索是「空调」而非「办公室空调」
    residue_clean = norm(residue.replace(area_hit, " ")) if area_hit else residue
    d_clean = norm(d.replace(area_hit, " ")) if area_hit else d

    cls_pool = [c for c in cands if (not classes) or (c.get("dc") or "") in classes]

    def _in_area(c: dict) -> bool:
        return bool(canon) and (c.get("area", "") or "") == canon

    def _ok(best_id: str) -> dict:
        return {"best": best_id, "ambiguous": [], "no_in_area": False, "others": [],
                "area_hit": area_hit, "classes": classes, "residue": residue_clean}

    def _amb(ids: list[str]) -> dict:
        return {"best": None, "ambiguous": ids, "no_in_area": False, "others": [],
                "area_hit": area_hit, "classes": classes, "residue": residue_clean}

    # —— ① 描述命中区域：区域优先，绝不跨区回退 ——
    if area_hit:
        pool = cls_pool if classes else cands
        in_area = [c for c in pool if _in_area(c)]
        if in_area:
            if len(in_area) == 1:
                only = in_area[0]
                # 类别词在场：区域+类别已是强证据（「办公室空调温度」绑办公室的
                # 温度传感器，即使残串「空调」不中名字）。无类别词（state 触发
                # 描述如「办公室空调」）：必须名字佐证，防区域里任意传感器被乱绑。
                if classes or not residue_clean or _name_hit(only, residue_clean):
                    return _ok(only["entity_id"])
                return _amb([])
            narrowed = [c for c in in_area if _name_hit(c, residue_clean)] if residue_clean else in_area
            if len(narrowed) == 1:
                return _ok(narrowed[0]["entity_id"])
            return _amb([c["entity_id"] for c in narrowed])
        # 该区域无候选：先给「整串名字命中」的老数据一次机会（friendly_name
        # 习惯内嵌区域名的用户），仍无果才判区域缺失——跨区守卫成立但不倒退。
        if classes:
            nm = [c for c in cls_pool if _name_hit(c, d) or _name_hit(c, d_clean)]
            if len(nm) == 1:
                return _ok(nm[0]["entity_id"])
            if len(nm) > 1:
                return _amb([c["entity_id"] for c in nm])
            return {"best": None, "ambiguous": [], "no_in_area": True,
                    "others": [c["entity_id"] for c in cls_pool if not _in_area(c)][:5],
                    "area_hit": area_hit, "classes": classes, "residue": residue_clean}
        nm = [c for c in cands if _name_hit(c, d) or _name_hit(c, d_clean)]
        if len(nm) == 1:
            return _ok(nm[0]["entity_id"])
        return _amb([c["entity_id"] for c in nm])

    # —— ② 无区域：整串（原形态 + 去「的」形态）名字匹配全局收束 ——
    for token in dict.fromkeys((d, norm(d.replace("的", " ")))):
        if not token:
            continue
        nm = [c for c in (cls_pool if classes else cands) if _name_hit(c, token)]
        if len(nm) == 1:
            return _ok(nm[0]["entity_id"])
        if len(nm) > 1:
            return {"best": None, "ambiguous": [c["entity_id"] for c in nm],
                    "no_in_area": False, "others": [],
                    "area_hit": None, "classes": classes, "residue": residue_clean}

    # —— ③ 无区域 + 类别在全屋唯一：沿用历史「修正」语义（无更优信息） ——
    if classes:
        if len(cls_pool) == 1:
            return _ok(cls_pool[0]["entity_id"])
        return {"best": None, "ambiguous": [c["entity_id"] for c in cls_pool],
                "no_in_area": False, "others": [],
                "area_hit": None, "classes": classes, "residue": residue_clean}

    return {"best": None, "ambiguous": [], "no_in_area": False, "others": [],
            "area_hit": None, "classes": classes, "residue": residue_clean}


def cn_classes(classes: list[str]) -> str:
    return "、".join(CN_LABEL.get(c, c) for c in classes) or "传感器"
