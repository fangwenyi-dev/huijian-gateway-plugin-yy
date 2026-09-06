"""查询族（v4.1 定案 M1：本地读回，不走 LLM）。

fast_path 的复杂查询守卫把 "客厅多少度" 放行（原代码即为温度查询预留），
此处接管：解析区域+量纲 → ha_client 状态缓存 → 中文短句。
未命中返回 None 继续向 LLM/兜底层流动。支持：温度/湿度/照度读数、开关状态、
时间问答（GetDateTime 本地时钟 + HA 时区）。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger("huijian.query")

_AREA_SUFFIX = ("室", "厅", "房", "间", "区", "馆", "楼", "卫", "厨")
_DEVICE_WORDS = {
    "灯": ("light",), "筒灯": ("light",), "射灯": ("light",), "灯带": ("light",),
    "吸顶灯": ("light",), "台灯": ("light",),
    "空调": ("climate",), "风扇": ("fan",), "窗": ("cover",), "窗帘": ("cover",),
    "加湿器": ("humidifier",), "净化器": ("fan",),
}


class QueryZone:
    def __init__(self, ha, settings):
        self.ha = ha
        self.settings = settings
        self._tz = None

    async def timezone(self):
        if self._tz is None:
            try:
                cfg = await self.ha.get_config()
                self._tz = cfg.get("time_zone") or None
            except Exception:
                self._tz = None
        return self._tz

    # ── 主入口：命中返回中文答案，否则 None ─────────────────────
    async def answer(self, text: str) -> Optional[str]:
        if self.settings is not None and not self.settings.get("nlu.query_local", True):
            return None
        text = text.strip().rstrip("。？！?!，,")
        # 时间
        if re.search(r"(现在)?(几点了?|什么时间|几点钟|时间)", text) and not re.search(r"定时|预约", text):
            return await self._time_answer()
        area = self._find_area(text)
        # 温度/湿度/照度
        m = re.search(r"(温度|湿度|照度|亮度)", text)
        if m and re.search(r"(多少|几|怎样|怎么样|如何)", text):
            kind = {"温度": "temperature", "湿度": "humidity", "照度": "illuminance", "亮度": "illuminance"}[m.group(1)]
            return await self._sensor_answer(area, kind, m.group(1))
        # 「多少度/几度」裸形（fast_path 守卫专门放行给本层，必须接住）
        if re.search(r"(多少度|几度)", text):
            return await self._sensor_answer(area, "temperature", "温度")
        # 设备开关状态："客厅灯开着吗/窗帘关到位没"
        m = re.search(r"(.{0,6}?(?:灯|空调|风扇|窗帘|窗|加湿器|净化器))\s*(开着吗|开着没|是开的吗|关了没|关着吗|是关的吗|什么状态|现在怎样)", text)
        if m:
            word = re.sub(r"^(.*?(?=灯|空调|风扇|窗帘|窗|加湿器|净化器))", "", m.group(1)) or m.group(1)[-2:]
            return await self._state_answer(area, word.strip() or None)
        return None

    def _find_area(self, text: str) -> Optional[str]:
        """区域词提取：优先注册区域名（ha._areas），退化为后缀启发。"""
        names = sorted(set(self.ha._areas.values()), key=len, reverse=True) if self.ha._areas else []
        for n in names:
            if n and n in text:
                return n
        m = re.search(r"([\u4e00-\u9fff]{2,4}?(?:" + "|".join(_AREA_SUFFIX) + r"))", text)
        return m.group(1) if m else None

    async def _time_answer(self) -> Optional[str]:
        """GetDateTime → 本地时钟（HA 时区经 /api/config 缓存解析）。"""
        try:
            from zoneinfo import ZoneInfo
            tz = await self.timezone()
            now = datetime.now(ZoneInfo(tz)) if tz else datetime.now()
        except Exception:
            now = datetime.now()
        return f"现在是 {now.hour} 点 {now.minute} 分。"

    async def _sensor_answer(self, area: Optional[str], device_class: str, cn: str) -> Optional[str]:
        states = await self.ha.states()
        best = None
        for eid, ent in states.items():
            attrs = ent.get("attributes") or {}
            if attrs.get("device_class") != device_class:
                continue
            name = (attrs.get("friendly_name") or "")
            ent_area = self.ha._entity_area.get(eid, "")
            if area and ent_area != area and area not in name:
                continue
            try:
                val = float(ent.get("state"))
            except (TypeError, ValueError):
                continue
            best = (name, val)
            if area and ent_area == area:
                break
        if not best:
            return None
        name, val = best
        unit = {"temperature": "度", "humidity": "%", "illuminance": "勒克斯"}[device_class]
        val_s = f"{val:.1f}".rstrip("0").rstrip(".")   # 26.5→「26.5」，26.0→「26」
        prefix = f"{area}的" if area else ""
        return f"{prefix}{cn}是 {val_s} {unit.replace('勒克斯','lx')}。"

    async def _state_answer(self, area: Optional[str], device_word: Optional[str]) -> Optional[str]:
        domains = _DEVICE_WORDS.get(device_word or "", ())
        ents = await self.ha.find_entities(area=area or "", domains=domains or ())
        ents = [e for e in ents if e["entity_id"].split(".")[0] in (domains or ("light", "climate", "fan", "cover", "humidifier", "switch"))]
        if not ents:
            return None
        on_words = ("on", "open", "heating", "cooling", "auto", "fan_only", "dry", "heat_cool", "eco")
        states_cn = {"on": "开着", "off": "关着", "open": "开着", "closed": "关着", "opening": "正在开", "closing": "正在关",
                     "heat": "制热中", "cool": "制冷中", "dry": "除湿中", "fan_only": "送风中", "auto": "自动模式", "idle": "待机"}
        lines = []
        for e in ents[:3]:
            st = str(e.get("state", ""))
            cn = states_cn.get(st, "开着" if st in on_words else f"处于 {st}")
            nm = (e.get("attributes") or {}).get("friendly_name") or e["entity_id"]
            lines.append(f"{nm}{cn}")
        return "，".join(lines) + "。" if lines else None
