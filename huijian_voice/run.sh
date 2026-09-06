#!/usr/bin/with-contenv bashio
# =====================================================================
# 慧尖语音助手 · 核心服务（s6: services.d/huijian-core/run）
# s6 负责崩溃自动重启与退避；watchdog(:8000) 负责假死告警。
# Supervisor options → HUIJIAN_OPT_* 环境变量（const/settings 的覆盖层约定）。
# 路径类环境变量无需导出：core/const.py 默认值已对齐镜像布局。
# =====================================================================
set -u
cd /opt/huijian/app

export HUIJIAN_OPT_MODEL_AUTO_DOWNLOAD="$(bashio::config 'model_auto_download')"
export HUIJIAN_OPT_IDLE_UNLOAD_MIN="$(bashio::config 'idle_unload_minutes')"
export HUIJIAN_OPT_LOG_LEVEL="$(bashio::config 'log_level')"

# homeassistant_api:true 授权注入 SUPERVISOR_TOKEN（HAClient 自动读取）；
# 独立开发机可用 HUIJIAN_HA_TOKEN/HUIJIAN_HA_API 覆写（const.HA_TOKEN_ENV）。
bashio::log.info "启动 core：ws :${HUIJIAN_WS_PORT:-8000} | admin :${HUIJIAN_ADMIN_PORT:-8002} | auto_dl=${HUIJIAN_OPT_MODEL_AUTO_DOWNLOAD} idle=${HUIJIAN_OPT_IDLE_UNLOAD_MIN}m log=${HUIJIAN_OPT_LOG_LEVEL}"

exec /opt/huijian/bin/python -m core
