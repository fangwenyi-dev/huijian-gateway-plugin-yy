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
# 版本戳落盘（admin_api._addon_version 与主服务 _version 的权威来源）。
# 权威源=镜像内 www/version.json（四源一致由钉桩+CI lint 双保）；ENV HUIJIAN_VERSION
# 只有官方 builder 注入 build-arg 时才有值，裸 docker build 烘进 0.0.0 → 不可依赖。
ver=$(jq -r '.addon_version // empty' /usr/share/nginx/html/version.json 2>/dev/null || true)
[ "$ver" = "0.0.0" ] && ver=""
[ -z "$ver" ] && ver="${HUIJIAN_VERSION:-dev}"
[ "$ver" = "0.0.0" ] && ver="dev"
echo "$ver" > /data/version.txt

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
      # v1.0.40 修复（A5）：旧实现 `rm -rf DST` 后 `cp -a` 再写版本戳——中途失败
      # （磁盘满/中断）会留下**内容残缺但版本戳正确**的集成：HA 加载失败，且下次
      # 启动因版本戳相同而跳过修复（永久坏）。与同脚本下载 klar 时的 sha256 纪律
      # 也不一致。现改为：拷到临时目录 → 校验（manifest 可解析）→ 原子换名 →
      # 失败回滚；版本戳只在换名成功后写。
      mkdir -p /homeassistant/custom_components
      TMP="${DST}.new.$$"
      OLD="${DST}.old.$$"
      rm -rf "${TMP}" "${OLD}"
      if cp -a "${SRC}" "${TMP}" && [ -f "${TMP}/manifest.json" ] \
         && jq -e . "${TMP}/manifest.json" >/dev/null 2>&1; then
        # 剔除 vendored 目录可能带来的 __pycache__，防止跨 python 版本脏字节码
        find "${TMP}" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
        [ -d "${DST}" ] && mv "${DST}" "${OLD}"
        if mv "${TMP}" "${DST}"; then
          rm -rf "${OLD}"
          echo "${want}" > "${DST}/.huijian_voice_stamp"
          bashio::log.warning "已落盘 huijian_ai v${want}（含 D1 门禁补丁）→ 请重启 HA Core 生效（首次安装必需；已运行则重载集成即可）"
        else
          rm -rf "${DST}"
          [ -d "${OLD}" ] && mv "${OLD}" "${DST}"
          rm -rf "${TMP}"
          bashio::log.error "集成换名失败，已回滚到上一版本（本次不做变更）"
        fi
      else
        rm -rf "${TMP}"
        bashio::log.error "集成拷贝/校验失败（源 ${SRC} 不可用），保留现有安装不动"
      fi
    fi
  else
    bashio::log.warning "镜像内未找到集成源 ${SRC}（跳过自动安装）"
  fi
else
  bashio::log.info "auto_install_integration=false：不自动落盘集成"
fi

# ── Klar NLU 引擎分发（一级确定性 NLU；全程 fail-open）───────────
# 引擎 = klar-ha-nlu（MIT）Rust 静态二进制，GitHub release 资产
# klar-linux-<arch>.tar.gz；SHA-256 用 GitHub API asset.digest 字段核验
# （与其自家集成 archive.py require_sha256 同法）。缺网/缺资产 → 只警告，
# 一级 NLU 由 KlarClient 的熔断自动降级为 TextCNN，语音链不受影响。
KLAR_VER="${HUIJIAN_KLAR_VERSION:-2026.9.2}"
KLAR_REPO="FABBricate-IT-Solutions/klar-ha-nlu"
case "$(uname -m)" in
  x86_64|amd64)  KLAR_ASSET="klar-linux-x86_64.tar.gz" ;;
  aarch64|arm64) KLAR_ASSET="klar-linux-aarch64.tar.gz" ;;
  *)             KLAR_ASSET="" ;;
esac
if [ -z "$KLAR_ASSET" ]; then
  bashio::log.warning "Klar 引擎：架构 $(uname -m) 无官方构建，一级 NLU 降级为本地 TextCNN"
elif [ "$(cat /data/klar/.version 2>/dev/null || echo none)" = "$KLAR_VER" ] && [ -x /data/klar/klar ]; then
  bashio::log.info "Klar 引擎 v${KLAR_VER} 已就位，跳过下载"
else
  klar_ok=0
  for tag in "v${KLAR_VER}" "${KLAR_VER}"; do
    meta=$(curl -fsSL --max-time 20 "https://api.github.com/repos/${KLAR_REPO}/releases/tags/${tag}" 2>/dev/null || true)
    url=$(echo "$meta"   | jq -r --arg a "$KLAR_ASSET" '.assets[]? | select(.name==$a) | .browser_download_url' 2>/dev/null | head -1)
    digest=$(echo "$meta" | jq -r --arg a "$KLAR_ASSET" '.assets[]? | select(.name==$a) | .digest' 2>/dev/null | head -1)
    [ -n "$url" ] && [ "$url" != "null" ] && break
  done
  if [ -z "${url:-}" ] || [ "$url" = "null" ]; then
    bashio::log.warning "Klar 引擎：release ${KLAR_VER} 无本架构资产或网络不可达 → 一级 NLU 降级为本地 TextCNN"
  elif [[ "${digest:-}" != sha256:* ]]; then
    bashio::log.warning "Klar 引擎：release 资产缺 SHA-256 digest，拒装（供应链纪律）→ 降级本地"
  else
    mkdir -p /data/klar
    if curl -fsSL --max-time 180 -o /data/klar/tmp.tar.gz "$url" \
      && echo "${digest#sha256:}  /data/klar/tmp.tar.gz" | sha256sum -c - >/dev/null 2>&1; then
      rm -rf /data/klar/x && mkdir -p /data/klar/x
      # 成员名兼容 "klar" 与 "klar-linux-*"（其集成 pick_klar_member 同款规则）
      if tar -xzf /data/klar/tmp.tar.gz -C /data/klar/x 2>/dev/null; then
        bin=$(find /data/klar/x -maxdepth 1 -type f \( -name klar -o -name 'klar-linux-*' \) | head -1)
        if [ -n "$bin" ]; then
          # v1.0.41 审查 S10：旧写法 mv 失败（同设备跨文件系统 EXDEV 之外的权限/满盘等）
          # 仍继续往下写 .version+klar_ok=1+成功日志 → **假成功**：下次启动因
          # [ .version == KLAR_VER ] 直接跳过安装，引擎永远缺席且无人知。mv 必须门控。
          if mv -f "$bin" /data/klar/klar && chmod 755 /data/klar/klar; then
            echo "$KLAR_VER" > /data/klar/.version
            klar_ok=1
            bashio::log.info "Klar 引擎 v${KLAR_VER} 落位 /data/klar/klar（sha256 已核验；s6 服务将拉起 loopback :10520）"
          else
            bashio::log.warning "Klar 引擎落位 mv/chmod 失败（磁盘满/权限?）→ 不写 .version，下次启动重试"
          fi
        fi
      fi
    fi
    [ "$klar_ok" = 0 ] && bashio::log.warning "Klar 引擎下载/校验/解包失败 → 一级 NLU 降级为本地 TextCNN"
    rm -rf /data/klar/tmp.tar.gz /data/klar/x
  fi
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
