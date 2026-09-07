#!/usr/bin/with-contenv bashio
# =====================================================================
# Klar NLU 引擎（s6 longrun，services.d/klar-engine/run）——一级确定性 NLU
#   • 仅绑 loopback :10520（core 同容器回环调用，免 token）
#   • home graph：直读 /homeassistant/.storage（map homeassistant_config
#     已有，零额外挂载；引擎自带 .storage watcher，无需推送快照）
#   • overlay/会话/支持包数据：/data/klar-data（持久，随加载项卸载清除）
# 二进制由 boot.sh 分发（GitHub release + sha256 核验）。缺席（首装无网/
# 分发失败）时此处 60s 周期等待自愈——加载项重启/更新会重跑 boot；引擎
# 缺席不影响其它服务，KlarClient 侧恒降级 TextCNN。
# =====================================================================
set -u
mkdir -p /data/klar-data
if ! [ -x /data/klar/klar ]; then
  bashio::log.info "Klar 引擎未就位：一级 NLU 暂走本地 TextCNN，等待分发（60s 轮询）"
  while ! [ -x /data/klar/klar ]; do sleep 60; done
  bashio::log.info "Klar 引擎已就位，启动"
fi
bashio::log.info "Klar 引擎启动：v$(cat /data/klar/.version 2>/dev/null || echo '?') → http://127.0.0.1:10520（仅回环）"
exec /data/klar/klar \
  --http 127.0.0.1:10520 \
  --config-dir /homeassistant \
  --data-dir /data/klar-data
