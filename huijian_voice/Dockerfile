# ═══════════════════════════════════════════════════════════════════
# 慧尖语音助手 · 加载项镜像
# 基础镜像定案（研究轮硬结论）：sherpa-onnx/onnxruntime/numpy 无 musllinux wheel
# → 必须 Debian glibc；hassio-addons/debian-base 自带 s6-overlay v3 + bashio。
# 版本一致性：运行时权威 = www/version.json（boot.sh 落 stamp）；build-arg 仅官方 builder 注入。
# ═══════════════════════════════════════════════════════════════════
# 首个 FROM 之前 Dockerfile 全局作用域只准放 ARG/解析指令——LABEL 等必须落 stage 内
# （v1.0.0 CI 首炸实证：stage 前 LABEL → buildx "no build stage in current context"）
ARG BUILD_FROM=ghcr.io/hassio-addons/debian-base:7.8.3
FROM ${BUILD_FROM}

# OCI 元数据（builder split-action 不传 --label，按文档走 Dockerfile LABEL）
LABEL org.opencontainers.image.source="https://github.com/fangwenyi-dev/huijian-gateway-plugin-yy"

ARG BUILD_ARCH=amd64
ARG BUILD_VERSION=0.0.0

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HUIJIAN_VERSION=${BUILD_VERSION} \
    HUIJIAN_BUILD_ARCH=${BUILD_ARCH}

USER root

# ── 系统依赖 ─────────────────────────────────────────────────────
# libopus0=opuslib-next 的 ctypes 后端；nginx=ingress 静态+反代；jq=脚本自检；
# python3-venv：PEP668 下 Debian 禁止系统 pip 安装，一律 venv /opt/huijian。
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl jq nginx libopus0 \
      python3 python3-venv python3-pip \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/huijian

# ── Python 依赖（三级镜像回退：清华→aliyun→官方，国内 CI 与海外 CI 都能过）──
COPY requirements.txt /tmp/requirements.txt
RUN set -e; ok=0; \
    for idx in \
        https://pypi.tuna.tsinghua.edu.cn/simple \
        https://mirrors.aliyun.com/pypi/simple \
        https://pypi.org/simple; do \
      echo ">>> pip via ${idx}"; \
      if /opt/huijian/bin/pip install -q --index-url "${idx}" -r /tmp/requirements.txt; then ok=1; break; fi; \
    done; \
    [ "${ok}" = "1" ] && /opt/huijian/bin/python -c "import sherpa_onnx, onnxruntime, aiohttp, opuslib_next, pypinyin, zeroconf, numpy; print('deps ok', sherpa_onnx.__version__, onnxruntime.__version__, numpy.__version__)"

# ── 应用与资产 ───────────────────────────────────────────────────
COPY core/            /opt/huijian/app/core/
COPY models.lock.json /opt/huijian/app/models.lock.json
COPY nlu_data/        /opt/huijian/app/nlu_data/
# huijian_ai 集成：boot 阶段按版本戳落盘 /homeassistant/custom_components（run.sh 逻辑）
COPY custom_components/ /opt/huijian/integration/custom_components/
# 管理页（事实文件与静态页同根，nginx root=/usr/share/nginx/html）
COPY www/             /usr/share/nginx/html/

# ── s6 装配 ──────────────────────────────────────────────────────
COPY boot.sh   /etc/cont-init.d/01-huijian-voice.sh
COPY run.sh    /etc/services.d/huijian-core/run
COPY nginx.sh  /etc/services.d/nginx/run
COPY nginx-huijian.conf /etc/huijian/nginx.conf
RUN chmod +x /etc/cont-init.d/01-huijian-voice.sh \
             /etc/services.d/huijian-core/run \
             /etc/services.d/nginx/run

# 注意：**禁止**再写 CMD/ENTRYPOINT ["/init"]。base 的 ENTRYPOINT 已是 /init
# （ghcr 7.8.3 config 实证），重复 CMD 会变成 `/init /init`——s6-overlay v3 的
# legacy services 装配拿 argv[1] 当初始化脚本，直接
# `s6-overlay-suexec: fatal: can only run as pid 1` 全服务停摆
# （v1.0.0 CI e2e 硬门禁抓出：Supervisor 真机因覆写 CMD 才没暴露）。

