# CLAUDE.md - 慧尖语音加载项（huijian_voice）开发指南

本仓遵循与 [ha-gateway-plugin](https://github.com/fangwenyi-dev/ha-gateway-plugin)
完全相同的交付标准。开工前读完本文件。

## 核心规则

### 三步流程（铁律）
**先诊断 → 后动手 → 最后检查。** 修改代码后必须跑回归测试
（`cd huijian_voice && python -m pytest tests -q`），测试全绿后还必须检查
有无引入新问题（副作用/回归/兼容性），确认无新问题才算完成。

### 验证规矩（2026-09-01 定案）
修复是否真正解决问题，必须由**我方在真实栈自行实证**，不得拿客户当测试。
本项目实证链分两级：
1. **本地 E2E-lite**（Windows/Linux 均可）：`bash huijian_voice/tests/e2e/run_local.sh`
   ——真 sherpa-onnx/onnxruntime/libopus/Kokoro 跑三通道 WS 协议断言；
2. **CI 真镜像 E2E**（发布硬门禁）：`huijian_voice/tests/e2e/run_e2e.sh`
   ——docker 构建交付镜像本体，容器内自动下载真模型，三通道+管理面+nginx 全断言。
HA OS 实机 + 真固件 + 小程序的端到端仍属发布前人工补测项（需测试 HA 实例）。

### 推送与发布
- **项目边界（2026-09-08 用户定案，最高优先）**：本仓=慧尖**语音**加载项，与慧尖
  **LoRa 网关**（`E:\AI\huijian-gateway-plugin` / `fangwenyi-dev/ha-gateway-plugin`）
  是两个完全不同的项目。本仓一切提交/推送只准进
  `origin = github.com/fangwenyi-dev/huijian-gateway-plugin-yy`（Gitee 侧仅在用户
  明确确认后推同名仓）；**严禁**把本仓任何内容提交到网关仓（含其工作目录），也严禁
  把网关仓文件复制进本仓交付面（只读参考其 CI/测试范式）。两仓目录名高度相似
  （`huijian-gateway-plugin` vs `huijian-gateway-plugin-yy`），push 前必须
  `git remote -v` 核对 origin URL 逐字符为 `-yy` 结尾。
- **禁止自动推送**。仅当用户明确说「推送」才 `git push`。若用户确认本仓需要 Gitee
  镜像（当前待定，同名仓已建但**未推送**），则每次推送必须双远端同推：
  `git push origin main && git push gitee main`，且 Gitee 仓**必须**是同名
  `huijian-gateway-plugin-yy`（绝不是网关的 ha-gateway-plugin）。
- 每次发布必须提升版本号（改 `huijian_voice/config.yaml` 的 `version`，
  四源一致性由测试与 CI 自动校验：config.yaml == www/index.html CURRENT_VERSION
  == www/version.json == custom_components/huijian_ai/manifest.json）。
- 推送 main 后必须检查 GitHub Actions 状态：
  ```bash
  gh run list --repo fangwenyi-dev/huijian-gateway-plugin-yy --limit 3
  gh run view <run-id> --repo fangwenyi-dev/huijian-gateway-plugin-yy --log-failed
  ```
  CI 九 job 全绿才算发布完成；GitHub Release 与 Gitee Release 均由 CI 自动创建
  （Gitee 只发最新版，历史不补）。
- 密钥（GITEE_TOKEN 等）只进 GitHub Secrets / 本机凭据管理器，
  严禁写入仓库或记忆文件。

### 配置界面中文本地化
config.yaml schema **每加一个配置项**必须同步：
`translations/zh-Hans.yaml`、`zh-CN.yaml`（与前两者逐字同文）、`en.yaml` 的
`configuration.<key>.name + description`（缺一条 Supervisor 拒收整份文件），
钉桩 `tests/test_release_consistency.py` 漏讲即红。「用户尽量不要配置」是
产品定案——新增配置项默认值必须开箱可用。

### Schema 语法红线（v1.7.16 事故换来的）
Supervisor 的 `RE_SCHEMA_ELEMENT` 只接受
`type / type(min,max) / list(a|b|c) / 尾缀?` 四种形态——
**`=默认值` 是臆造语法**，会导致加载项从商店**静默消失**（无任何前端报错）。
默认值唯一正规途径 = `options:` 块。改 schema 任何值之前，必须对上游源码或
官方文档实证语法存在。钉桩：`tests/test_store_schema.py`（逐字抄录上游 RE_SCHEMA_ELEMENT 全值匹配）+ `test_release_consistency` 翻译覆盖。

## 已付过学费的坑（动手前先对表）

| 坑 | 防线 |
|---|---|
| schema `=默认值` 臆造语法 → 商店静默消失 | 纯文档语法钉桩；上游源码实证习惯 |
| 基镜像 tag 凭记忆写（7.2.6 不存在） | build.yaml/Dockerfile 双源一致钉桩 + ghcr 实查 |
| builder 参数文法（单体版 `--docker` 等必炸） | 已迁 split-actions@2026.06.0（网关实证范式） |
| `import opus`（实为 `opuslib_next`） | Dockerfile/boot.sh 正则钉桩 |
| opus decode frame_size 单位=样本数非字节 | E2E-lite 真 libopus 编解码回环 |
| www 根绝对路径 fetch 在 Ingress 全灭 | INGRESS_BASE 范式 + 钉桩禁根绝对 fetch |
| 含 token 文件落 nginx 静态根 | endpoints.json → /data/run/ + 钉桩 |
| make_app 顶层 handler 引用裸全局 ctx | app[AppKey] 取袋 + 路由级测试 |
| 引擎 unload/推理竞态段错误 | 快照+busy 计数，在飞跳过（守卫测试） |
| 并发双下载 | ModelStore per-key single-flight（守卫测试） |
| WSL `ln -s` 建的符号链接 Windows 侧 1920 | 用 `cmd /c mklink /J` junction；model_dir_for 有 OSError 守卫 |
| bash 命令行里 pkill/kill -f | 禁——先 write 成脚本再执行，或 netstat 定位精确 PID |
| 版本 bump 全局 sed | 只准替换字段值行；测试四源一致兜底 |

## 目录职责

```
huijian_voice/          # 交付物本体（加载项目录）
├── core/               # 运行时（ws/admin/nlu/asr/tts/model_store/…）
├── tests/              # 111 项钉桩 + e2e/（真镜像 CI 门禁 + run_local 本地）
├── www/                # Ingress 管理页（全中文，版本四源之一）
├── custom_components/  # 随镜像分发的 huijian_ai（D1 补丁，版本对齐加载项）
├── DOCS.md / README.md # 商店文档页 / 开发架构文档
CHANGELOG.md            # 唯一变更日志（Keep a Changelog，CI Release 正文来源）
repository.yaml         # 商店仓库清单
.github/workflows/ci.yaml  # 九 job 全链（lint→…→gitee-release）
```

`asr/`、`tts/`、`nlu/`、`yyjicheng/`、`_hjmodels/`、`_winlibs/`、`.omo/`、`dev/`
是**研究输入**（模型母本/上游源码/工具链），.gitignore 已排除，不进交付仓；
根目录设计文档（方案 v1–v4、协议契约、源码盘点、沟通记录）**随仓收录**——
定案依据与全史留痕是客户交接与审计的一手材料（网关仓「会话纪要/」同款做法）。
