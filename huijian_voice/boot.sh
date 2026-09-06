#!/usr/bin/with-contenv bashio
# =====================================================================
# 慧尖语音助手 · 启动前置（cont-init，root，阻塞至完成）
#  1) 数据目录预建（models/import 手动投放位 = ModelStore 导入优先链首）
#  2) huijian_ai 集成自动落盘（auto_install_integration；版本戳幂等）
#  3) 只读自检打印（python deps / lock 文件）——日志即诊断
# =====================================================================
set -u
bashio::log.info "═══ 慧尖语音助手 boot ═══"

mkdir -p /data/models/import /data/run
chmod 777 /data/run || true
# 版本戳落盘（admin_api._addon_version 与主服务 _version 的权威来源）
echo "${HUIJIAN_VERSION:-dev}" > /data/version.txt

# ── 集成自动安装（体验层融合三件套之一）──────────────────────────
SRC=/opt/huijian/integration/custom_components/huijian_ai
DST=/homeassistant/custom_components/huijian_ai
if bashio::config.true 'auto_install_integration'; then
  if bashio::fs.directory_exists "${SRC}"; then
    want=$(jq -r .version "${SRC}/manifest.json" 2>/dev/null || echo unknown)
    have=$(cat "${DST}/.huijian_voice_stamp" 2>/dev/null || echo none)
    if bashio::fs.directory_exists "${DST}" && [ "${want}" = "${have}" ]; then
      bashio::log.info "huijian_ai 集成已是目标版本 v${want}，跳过落盘"
    else
      mkdir -p /homeassistant/custom_components
      rm -rf "${DST}"
      cp -a "${SRC}" "${DST}"
      echo "${want}" > "${DST}/.huijian_voice_stamp"
      # 剔除 vendored 目录可能带来的 __pycache__，防止跨 python 版本脏字节码
      find "${DST}" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
      bashio::log.warning "已落盘 huijian_ai v${want}（含 D1 门禁补丁）→ 请重启 HA Core 生效（首次安装必需；已运行则重载集成即可）"
    fi
  else
    bashio::log.warning "镜像内未找到集成源 ${SRC}（跳过自动安装）"
  fi
else
  bashio::log.info "auto_install_integration=false：不自动落盘集成"
fi

# ── 自检 ─────────────────────────────────────────────────────────
if ! /opt/huijian/bin/python -c "import sherpa_onnx, onnxruntime, aiohttp, opuslib_next, numpy" 2>/dev/null; then
  bashio::log.error "运行时依赖自检失败！"
  /opt/huijian/bin/python -c "import sherpa_onnx" 2>&1 | tail -1 || true
  bashio::fail "deps check"
fi
bashio::log.info "deps 自检通过 | arch=${HUIJIAN_BUILD_ARCH:-?} version=${HUIJIAN_VERSION:-?}"
[ -f /opt/huijian/app/models.lock.json ] || bashio::log.error "models.lock.json 缺失"
exit 0
