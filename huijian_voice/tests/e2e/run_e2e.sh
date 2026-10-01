#!/usr/bin/env bash
# CI 发布硬门禁：用 docker **构建本次交付的镜像本体**（Dockerfile/boot.sh/run.sh/
# nginx 全链路），裸容器（伪造 /data/options.json 模拟 Supervisor 注入）起服务，
# ModelStore 真下载双模型，随后跑与本地完全同源的三通道协议断言 + 管理面/反代
# 面断言 + 版本链断言 + 优雅停机。e2e 失败 = manifest 不合并 = 不发版。
# （真 HA OS 实机 + 真固件端到端仍属发布前人工补测项，见 CLAUDE.md 验证规矩。）
set -Eeuo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
ADDON="$(cd "$DIR/../.." && pwd)"
DATA=/tmp/hj-e2e-data
NAME=hj-e2e

diag() {
    echo "!! E2E 编排失败于: ${1:-unknown}"
    docker logs --tail 150 "$NAME" 2>&1 || true
}
trap 'diag "line $LINENO"' ERR

echo "==== 1. 构建交付镜像 ===="
docker build -t huijian-voice-e2e:dev "$ADDON"

echo "==== 2. 裸容器起服务（options 模拟 Supervisor 注入）===="
rm -rf "$DATA"; mkdir -p "$DATA"
cat > "$DATA/options.json" <<'EOF'
{"model_auto_download": true, "auto_install_integration": false,
 "idle_unload_minutes": 0, "log_level": "info"}
EOF
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --network host -v "$DATA:/data" huijian-voice-e2e:dev >/dev/null

echo "==== 3. 等管理面存活 ===="
for i in $(seq 1 90); do
    curl -sf http://127.0.0.1:8002/api/health >/dev/null 2>&1 && break
    docker inspect -f '{{.State.Running}}' "$NAME" | grep -q true \
        || { diag "容器启动失败"; exit 1; }
    sleep 2
done
curl -sf http://127.0.0.1:8002/api/health >/dev/null || { diag "health 超时"; exit 1; }

echo "==== 4. 等运行期所需模型真下载就绪（主档 ASR+Kokoro ≈1.4GB，给 25 分钟）===="
# v4.2 教训（run 34364440982 实红 25min 超时）：等值集合必须是「运行期 need」，
# 不能是 lock 全清单——paraformer 已降兼容回落档，新装根本不会主动下载它，
# all(models_ready.values()) 恒 false。
# v1.1.17：need 集改**从 models.lock 派生**（default_provider:true 的键）——写死模型键
# 的那版换默认档必漂（test_asr_engine::test_e2e_need_is_derived_from_lock_not_hardcoded
# 钉死：脚本里不得再出现任何模型键字面量）。
for i in $(seq 1 300); do
    R=$(curl -sf --max-time 5 http://127.0.0.1:8002/api/health | \
        HJ_LOCK="$ADDON/models.lock.json" python3 -c \
        'import json,os,sys
j=json.load(sys.stdin); m=j.get("models_ready") or {}
d=json.load(open(os.environ["HJ_LOCK"], encoding="utf-8"))
need=[k for k,v in d.items() if isinstance(v,dict) and v.get("default_provider")]
# 同 run_local.sh 的收口（2026-10-01）：`models_ready` 只证明**包解好了**，不证明
# 引擎装载完成。门只看前者时，客户端在"冷启动双载闩"放行前推第一句 ⇒ 拿到
# `{'text':'','reason':'模型正在加载…'}` 而假红（本地真栈四轮同签名实证）。
# 真装态在同一份 health 的 `asr_loaded`/`tts_loaded` 里，本闸给足 25 分钟不怕多等。
print("yes" if need and all(m.get(k) for k in need)
      and j.get("asr_loaded") and j.get("tts_loaded") else "")' \
        2>/dev/null | tr -d '\r' || true)
    [ "$R" = "yes" ] && break
    sleep 5
done
[ "${R:-}" = "yes" ] || { diag "models 未就绪"; exit 1; }

echo "==== 5. 版本链断言（镜像 stamp == config.yaml 版本，防三链漂移回归）===="
CFG_VER=$(grep '^version:' "$ADDON/config.yaml" | head -1 | awk -F'"' '{print $2}' | tr -d '\r')
[ -n "$CFG_VER" ] || { echo "::error title=版本提取失败"; exit 1; }
curl -sf http://127.0.0.1:8002/api/health | grep -q "\"version\": *\"$CFG_VER\"" \
    || { diag "health version != $CFG_VER"; exit 1; }
echo "✅ 镜像内版本链 $CFG_VER"

echo "==== 6. 三通道协议断言（容器内跑，依赖与生产完全同源）===="
docker cp "$DIR/e2e_client.py" "$NAME":/tmp/e2e_client.py
docker cp "$DIR/assets/0.wav" "$NAME":/tmp/0.wav
docker exec -e E2E_WAV=/tmp/0.wav -e E2E_APP_ROOT=/opt/huijian/app \
    "$NAME" /opt/huijian/bin/python /tmp/e2e_client.py

echo "==== 7. 管理面经 nginx 反代（:8001 白名单=回环，runner 本机正命中）===="
curl -sf http://127.0.0.1:8001/healthz | grep -q '"ok":true'
curl -sf http://127.0.0.1:8001/status.json | grep -q "\"version\": *\"$CFG_VER\""
curl -sf http://127.0.0.1:8001/models_status.json >/dev/null
curl -sf http://127.0.0.1:8001/api/settings | grep -q security
curl -sf http://127.0.0.1:8001/api/endpoints | grep -q '"token"'
curl -sf --max-time 120 -X POST -H 'Content-Type: application/json' \
    -d '{"text":"你好，我是慧尖语音助手。"}' http://127.0.0.1:8001/api/tts/test \
    -o /tmp/hj-e2e-tts.bin
head -c 4 /tmp/hj-e2e-tts.bin | grep -q RIFF && echo "✅ /api/tts/test 返回合法 WAV"
grep -q '"token"' "$DATA/run/endpoints.json" 2>/dev/null || diag "run/endpoints.json 未生成（非致命）"
docker exec "$NAME" sh -c 'test ! -e /usr/share/nginx/html/endpoints.json' \
    && echo "✅ 容器静态根无 token 文件"

echo "==== 8. 优雅停机（watchdog 周期对时 15s 内应完成）===="
docker stop -t 20 "$NAME" >/dev/null
docker rm "$NAME" >/dev/null
echo "==== E2E 真镜像全链完成 ✅ ===="
