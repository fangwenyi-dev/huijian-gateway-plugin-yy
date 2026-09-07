#!/usr/bin/env python3
"""klar 引擎 zh_cn 现场质量探针（仓库工具，不进镜像，无第三方依赖）。

用途：加载项 v1.0.8 起 klar 是一级 NLU，zh_cn 语言包质量是真实体验的命门。
本机无 Docker 做不了离线基准——此脚本就是真机基准：在 HA 宿主上跑
（host_network 下容器 127.0.0.1:10520 = 宿主回环），或加载项容器内跑。

    python3 klar_probe.py                      # 默认打 http://127.0.0.1:10520
    python3 klar_probe.py http://192.168.1.91:10520 --token XXX
    python3 klar_probe.py --only 开,锁          # 只跑含关键词的用例

用例分四组（EXPECT 列 = 期望 decision；控制组还校验 intent 在白名单）：
  CTRL  控制族：应 execute + 白名单意图 → 我们会接管
  PASS  查询/媒体族：应 execute 但意图在白名单外 → 我们放行给既有链
  REJ   闲聊/模糊：应非 execute → 交 TextCNN/LLM
  MULTI 多分句：应 execute 两步

注意：execute 与否取决于你家真实 home graph（房间名/设备名）。探针报
"MISMATCH" 不一定=引擎差，可能是你家没有"射灯"。看汇总，别揪单条。
"""
import argparse
import json
import sys
import time
import urllib.request

WHITELIST = {  # 与 core/nlu/klar_client.py KLAR_CONTROL_INTENTS 同步
    "HassTurnOn", "HassTurnOff", "HassToggle", "HassLightSet",
    "HassClimateSetTemperature", "HassClimateSetHumidity", "HassSetPosition",
    "HassLock", "HassUnlock", "HassFanSetSpeed", "HassFanSetPresetMode",
    "HassVacuumStart", "HassVacuumPause", "HassVacuumReturnToBase",
}

# (组, 语句, 期望decision)
CASES = [
    ("CTRL", "打开办公室的射灯", "execute"),
    ("CTRL", "把客厅的灯关掉", "execute"),
    ("CTRL", "关灯", "execute"),
    ("CTRL", "开灯", "execute"),
    ("CTRL", "打开卧室窗户", "execute"),
    ("CTRL", "空调调到二十六度", "execute"),
    ("CTRL", "把温度调低一点", "execute"),
    ("CTRL", "风扇打开", "execute"),
    ("CTRL", "锁上门", "execute"),
    ("CTRL", "解锁前门", "execute"),
    ("CTRL", "打开扫地机器人", "execute"),
    ("CTRL", "让吸尘器回充", "execute"),
    ("CTRL", "把客厅灯调亮一点", "execute"),
    ("CTRL", "餐厅灯换成蓝色", "execute"),
    ("CTRL", "关闭全部灯光", "execute"),
    ("CTRL", "把车库门打开", "execute"),
    ("CTRL", "打开书房的加湿器", "execute"),
    ("CTRL", "关闭空气净化器", "execute"),
    ("CTRL", "把窗帘拉上", "execute"),
    ("CTRL", "开一下卫生间的灯", "execute"),
    ("PASS", "客厅现在多少度", "execute"),      # 应 execute/HassGetState（白名单外=放行）
    ("PASS", "卧室灯开着吗", "execute"),
    ("PASS", "现在几点了", "execute"),
    ("PASS", "客厅湿度是多少", "execute"),
    ("REJ", "今天天气怎么样", None),            # 任意非 execute（或 execute 亦可接受，见判定）
    ("REJ", "给我讲个笑话", None),
    ("REJ", "你是谁", None),
    ("REJ", "asdfghjkl", None),
    ("REJ", "嗯那个什么来着", None),
    ("MULTI", "关灯并且锁门", "execute"),
    ("MULTI", "打开客厅空调然后把窗帘拉上", "execute"),
]


def parse(base, text, token, timeout=5.0):
    body = json.dumps({"text": text, "language": "zh-CN"}).encode("utf-8")
    req = urllib.request.Request(f"{base}/api/v2/parse", data=body,
                                 headers={"Content-Type": "application/json",
                                          **({"x-klar-token": token} if token else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base", nargs="?", default="http://127.0.0.1:10520")
    ap.add_argument("--token", default="")
    ap.add_argument("--only", default="", help="只跑语句含此子串的用例")
    args = ap.parse_args()
    ok = bad = skip = 0
    t_all = time.time()
    for group, text, expect in CASES:
        if args.only and args.only not in text:
            continue
        try:
            obj = parse(args.base, text, args.token)
        except Exception as e:
            print(f"[ERR ] {text!r:24} → 引擎不可达/出错: {e}")
            skip += 1
            if skip == 3:
                print("引擎连续不可达——确认加载项已升级到 v1.0.8 且 boot 日志里klar 已落位。")
                return 2
            continue
        dec = (obj.get("decision") or {}).get("type", "?")
        conf = obj.get("plan", obj).get("confidence") if isinstance(obj.get("plan", obj), dict) else None
        if dec == "execute":
            steps = (obj.get("plan") or {}).get("steps") or []
            names = [(st.get("intent") or {}).get("name") for st in steps]
            detail = ",".join(names) + (f" conf={conf:.2f}" if isinstance(conf, (int, float)) else "")
            if group == "CTRL":
                good = all(n in WHITELIST for n in names) and names
            elif group == "PASS":
                good = all(n not in WHITELIST for n in names)   # 放行给既有链
            else:                                               # MULTI
                good = len(names) >= 2 and all(n in WHITELIST for n in names)
        else:
            detail = f"decision={dec} speech={(obj.get('speech') or '')[:24]!r}"
            good = expect is None and dec != "execute"          # REJ 组：非 execute 即对
            if group != "REJ":
                good = expect is None                           # CTRL/PASS 给出非 execute = 漏
        mark = "OK  " if good else "MISM"
        ok += good
        bad += not good
        print(f"[{mark}] {group:5} {text!r:26} → {detail}")
    print(f"\n汇总：{ok} 符合预期 / {bad} 偏差 / {skip} 引擎错误，用时 {time.time()-t_all:.1f}s")
    print("提示：'MISM'≠坏——控制组漏判多半是家里没有同名设备；PASS 组被接管"
          "说明白名单需要再收紧。把整份输出贴回来即可做分派调优。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
