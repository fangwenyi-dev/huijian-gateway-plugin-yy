# huijian_voice · 慧尖HA语音插件（加载项开发文档）（开发文档）

## 目录结构

```
huijian_voice/
├── config.yaml            # Supervisor 清单（4 高级项 options，零必填）
├── Dockerfile             # debian-base(glibc) —— 非 Alpine！musl 无 wheel 实证
├── boot.sh                # cont-init：集成落盘 + 依赖自检
├── run.sh                 # s6 services.d/huijian-core/run（options→HUIJIAN_OPT_*）
├── nginx.sh + nginx-huijian.conf   # :8001 静态页 + /api/ 反代 :8002（源 IP 白名单）
├── requirements.txt       # 七钉（numpy2 线实证）
├── models.lock.json       # 2 模型包 sha256+多镜像 URL
├── nlu_data/              # TextCNN vendored（intent.onnx/vocab/阈值）
├── custom_components/huijian_ai/   # vendored 集成（含 HUIJIAN-PATCH D1）
├── www/index.html         # 管理页（单文件零外链全中文；CURRENT_VERSION 钉桩）
├── translations/{zh-Hans,zh-CN,en}.yaml   # schema 键全覆盖（钉桩）
├── tests/                 # 115 项：NLU/协议/管理面/并发守卫/基建契约/发布一致性
├── dev/                   # E2E-lite 真链路脚本（e2e_boot + e2e_client，非交付物）
└── core/
    ├── const.py settings.py audio.py model_store.py ha_client.py
    ├── asr.py tts.py executor.py agent.py pipeline.py
    ├── session.py ws_server.py admin_api.py mdns.py main.py
    └── nlu/  corrector.py textcnn.py targets.py scenes.py fast_path.py query.py
```

## 数据流（一句话）

卫星固件 opus→`/xiaozhi/v1/stt`→Paraformer→文本→`/xiaozhi/v1/llm`(detect)→
Pipeline 级联（T0 正则→T1 TextCNN→场景→查询→LLM→兜底）→ `/api/intent/handle` 执行 →
话术→ `/xiaozhi/v1/tts` detect→Kokoro→24k PCM→重采样16k→opus 帧流→固件播报。

## 关键实证结论（踩坑记录，勿回头路）

1. **musl 无 wheel**：sherpa-onnx/onnxruntime/soxr 均无 musllinux → 基础镜像必须
   glibc（hassio-addons/debian-base）。网关加载项的 Alpine 模式不可照抄。
2. **`OnlineRecognizer.from_paraformer(feature_dim=560)` 静默空串**：
   paraformer 特征维由模型决定，显式必须 80（fatal pitfall，运行时实证）。
3. **Kokoro v1_0 输出 24000 Hz**（v4 文档 22.05k 系笔误）；sid45=zf_xiaobei 小北
   以 k2-fsa 官方 kokoro.rst 为权威（53 音色 voices.bin 布局）。
4. **`paraformer-zh-int8-2025-10-07` 实为 WSChuan 四川方言包**（tar README 原文
   实证）→ 已从 models.lock 移除；默认 STT=bilingual-zh-en 流式包。
5. **TextCNN 输出为 raw logits**：与阈值比较前必须 softmax（收编缺陷一）。
6. **协议硬约束**（《小智协议子集-服务器契约.md》）：STT 每 stop 恰一条 stt；TTS
   JSON 帧禁含 "error" 键、每 detect 必有 stop、顶替无孤儿帧；LLM 句子帧字段名是
   `data`；拒绝=HTTP 401 预升级；ping→pong；服务端不主动 close。10 项 WS 级测试锁死。
7. **ControlWindow action 槽**：集成 `find_action_in_text` 会把动作 lower 后查
   WINDOW_ACTION_MAPPING（键含 "a"）——本侧统一归一小写 `a`=内倒。
8. **lock 域话术反转**：TurnDeviceOn=上锁（集成语义），control_targets 无 domain
   字段 → 话术层用名称含「锁」启发式反义（已锁→"已上锁/已解锁"）。
9. `/api/intent/handle` 失败体用 `error` 键（英文）→ executor 的 zh_error 映射兜住。

## 本地开发/测试（无 Docker 也可全跑）

```bash
cd huijian_voice
pip install -r requirements.txt pytest pyyaml   # + 系统 libopus0（Linux）
python -m pytest tests -q                        # 115 项
# 起全服务（假 HA token 即可，REST 失败不致命）：
HUIJIAN_DATA=/tmp/hj HUIJIAN_OPT_MODEL_AUTO_DOWNLOAD=false \
HUIJIAN_HA_TOKEN=x HUIJIAN_MODELS_DIR=<模型解包父目录> python -m core
# 管理 API： curl 127.0.0.1:8002/api/health
```

`nlu_data/` vendored 使 NLU 测试零下载。**真链路 E2E** 两级（断言脚本单一事实源
`tests/e2e/e2e_client.py`）：

- 本地（无 docker）：`bash tests/e2e/run_local.sh`（`PYTHON=` 可覆写解释器）。
  起真服务+真模型+不可达 HA 验降级，三通道断言 + reload 卸载/惰性重载复验。
  Windows 需 opus.dll（conda-forge win-64 libopus 解到仓旁 `_winlibs/` 自动探测）；
  Linux 需 `apt install libopus0`。2026-09-08 实证：STT 168 帧 0.5s 出文 /
  LLM 降级话术 / TTS 51 帧≈3.1s。
- CI 发布硬门禁：`bash tests/e2e/run_e2e.sh` —— docker **构建交付镜像本体**、
  裸容器（伪造 /data/options.json）+ ModelStore 真下载 + 容器内三通道 + nginx
  反代面 + 版本链 + 静态根 token 缺席 + 优雅停机全断言。

## 发布

- 版本四源一致：`config.yaml` == `www` CURRENT_VERSION == `www/version.json` ==
  vendored `huijian_ai/manifest.json`（钉桩 + CI lint 双门禁；`const.py` 兜底随
  `const.addon_version()` 单一版本链）。
- schema 每键三份翻译齐全（同上钉桩）；schema 值只准文档化语法（红线见 CLAUDE.md）。
- 改 `config.yaml` version + 更新根 `CHANGELOG.md`（`## [x.y.z]` 头）→ push main：
  CI 九 job 全链 lint→prepare→init→build×2→**e2e 真镜像**→manifest(+匿名可拉
  检查)→国内镜像站预热→GitHub Release→Gitee Release 自动补发。
- 商店安装走 `ghcr.1ms.run` 透传站（image 字段），CI 推 ghcr.io 源站并预热。
- 真机 E2E（HA OS aarch64 + 固件 + 小程序）为发布前人工补测项，本机/CI 绿≠可交付。
