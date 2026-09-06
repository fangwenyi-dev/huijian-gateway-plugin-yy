#!/usr/bin/env bash
# 本地 E2E-lite（无 docker 环境的自证通道，Windows-gitbash/WSL/Linux 通用）：
# 真 core 服务 + 真模型（_hjmodels 预置或自动下载）+ 不可达 HA 降级 + 三通道断言
# + reload 卸载/惰性重载复验。CI 侧对应用 docker 跑交付镜像本体的 run_e2e.sh。
# 用法：bash tests/e2e/run_local.sh   （PYTHON=/path/to/python 可覆写解释器）
set -Eeuo pipefail
cd "$(dirname "$0")/../.."
PY="${PYTHON:-python3}"
LOG=/tmp/hj-e2e-server.log

# dev 预置 _hjmodels 为手工解包产物：老布局无完成标记 → 补章迁移（生产镜像
# 由 ModelStore 自行解包盖章，此段仅本地开发路径）
MJ="$(dirname "$(pwd)")/_hjmodels"
[ -d "$MJ" ] && for d in "$MJ"/*/; do [ -e "$d/.extracted_ok" ] || echo "dev-migration" > "$d/.extracted_ok"; done

echo "==== 1. 起服务 ===="
rm -rf _e2e_data
"$PY" tests/e2e/e2e_server.py >"$LOG" 2>&1 &
SRV=$!
cleanup() { kill "$SRV" 2>/dev/null || true; wait "$SRV" 2>/dev/null || true; }
trap cleanup EXIT

echo "==== 2. 等 health + models_ready（自动下载形态可能数分钟）===="
ready=""
for i in $(seq 1 600); do
    kill -0 "$SRV" 2>/dev/null || { echo "!! 服务进程已退出"; tail -40 "$LOG"; exit 1; }
    if [ -z "$ready" ]; then
        curl -sf http://127.0.0.1:8002/api/health >/dev/null 2>&1 && { echo "health ok"; }
    fi
    ready=$(curl -sf --max-time 5 http://127.0.0.1:8002/api/health 2>/dev/null \
        | "$PY" -c 'import json,sys
try: j=json.load(sys.stdin); print("yes" if all(j.get("models_ready",{}).values()) else "")
except Exception: print("")' | tr -d '\r' || true)
    [ "$ready" = "yes" ] && break
    sleep 2
done
[ "$ready" = "yes" ] || { echo "!! models 未就绪"; tail -40 "$LOG"; exit 1; }

echo "==== 3. 三通道断言（第一轮：常驻模型）===="
"$PY" tests/e2e/e2e_client.py

echo "==== 4. 空闲卸载 → 惰性重载（第二轮走快照/busy/executor 新链）===="
curl -sf -X POST --max-time 30 http://127.0.0.1:8002/api/system/reload_models
echo
"$PY" tests/e2e/e2e_client.py

echo "run_local：本地真栈 E2E 全绿 ✅"
