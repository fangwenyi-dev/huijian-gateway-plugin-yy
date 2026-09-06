# 变更日志

所有版本变更记录在此文件中。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。

## [1.0.0] - 2026-09-08

首个完整版本 · 慧尖局域网语音助手加载项（huijian_voice）。

**形态定案（《语音助手落地方案-v4.1》）**：纯局域网卫星服务器（模式 B），
公网小智退役；单独加载项不并入 LoRa 网关（镜像体积/重启爆炸半径/发布节奏/
资源隔离/产品边界五理由）；同仓双镜像，体验层融合。

### Added
- **三端点小智协议子集 WS 服务**（`/xiaozhi/v1/{stt|tts|llm}`）：帧契约逐条实现
  并有 10 项 WS 级回归（STT 单条回执、TTS 顶替无孤儿帧、LLM `data` 字段、
  401 预升级拒绝、ping/pong、持久连接）。
- **STT 默认本地**：sherpa-onnx Paraformer 中英双语**流式**包
  （`asr_paraformer_bilingual`，int8 RTF≈0.04，10s 音频 0.48s 实测）。
  ⚠ 定案变更：原候选 `paraformer-zh-int8-2025-10-07` 经 tar 内 README 实证为
  **WSChuan 四川方言模型**，已从 models.lock 移除，勿再按旧文档引用。
- **TTS 默认本地**：Kokoro multi-lang v1_0（53 音色，**sid45=zf_xiaobei 小北**，
  k2-fsa 官方表实证；输出 **24000 Hz**——v4 文档 22.05k 为笔误，本版已按实测修正），
  运行时重采样至 16k 裸 opus 60ms 帧。
- **NLU 级联**：T0 正则 39 式（840 行 fast_path v1.5 逐字收编）→ T1 TextCNN 15 类
  （softmax 后按类阈值 OOS .85/Scene .5/其余 .7）→ 场景双检 → 本地查询族 →
  LLM（默认关）→ 固定兜底。
- **执行走 `POST /api/intent/handle`**（huijian_ai 集成 14 意图）；话术层中文本地化
  （含锁域反义修正启发、窗户动作中文化、错误英→中映射）。
- **零必填配置**：装完即用；token 自动生成；集成自动落盘；模型自动下载
  （modelscope→gh-proxy→GitHub/HF 三级回退 + `/data/models/import/` 离线投放口）。
- 管理页 Ingress :8001（状态/设置/模型/调试/配对五区，全中文）；管理 API 仅回环+
  Supervisor 网段可达（token 不外泄 LAN）；mDNS `_huijian-voice._tcp` 广播。
- CI 全链（网关同款 9-job 范式）：lint 五门禁 → prepare → init（官方
  prepare-multi-arch-matrix）→ build（builder split-actions@2026.06.0 双架构）→
  **e2e 真镜像硬门禁**（docker 构建本次交付镜像 + 真模型下载 + WS 三通道断言）→
  manifest + ghcr 匿名可拉性检查 → 国内镜像站缓存预热 → GitHub Release →
  Gitee Release 自动补发（幂等 + BOM 拦截）。
- 随包资产：`nlu_data/`（TextCNN intent.onnx + vocab + 阈值）；
  `custom_components/huijian_ai/`（boot 阶段按版本戳落盘，含 **HUIJIAN-PATCH D1**：
  mcp_endpoint 为空跳过挂载，纯 LAN 条目不再 setup 死路；manifest 版本与加载项
  同链对齐）；`models.lock.json`（2 包 sha256 钉版 + 多镜像 URL）；
  `icon.png`/`logo.png`；`www/version.json`（四源版本门禁输入）。

### Fixed（收编期，对 840 行 fast_path 母本的移植修正，均有回归钉桩）
1. TextCNN 输出未过 softmax 即与 0.7 阈值比较 → 恒放行级缺陷，已修；
2. 拼音候选首个 ≤5 距离即 break →「空调」被「筒灯」截胡，改全表择优 + 收紧 ≤2；
3. 设备词中段漏提（"暂停窗户动作"→净化器）→ 新增子串优先档；
4. delta 捕获残渣（"%"、"一点"）污染目标名 → 残渣归一为全屋调节；
5. 「区域+属性词」（卧室亮度调高一点）属性词被吃成设备名 → 属性词快捷路径转域目标；
6. ControlWindow 动作大写 A 依赖集成侧 lower → 本侧统一归一小写。

### Fixed（E2E-lite 真链路实证，Windows 全栈真模型跑出）
- **opus 绑定名修正**：opuslib-next 1.3.1 实际导入名为 `opuslib_next`（非 `opus`）；
  同场抓获 decode `frame_size` 单位错（样本数≠字节数，60ms@16k 应传 960 非 1920，
  真 libopus 下每帧补零翻倍——mock 测试拦不住）。
- **admin_api/ws_server 全局 ctx 缺陷**：handler 引用不存在的全局名（移植手误），
  统一 `app[AppKey]` 取袋 + 路由级 7 项钉桩常态化拦截。
- **ha_bridge 语义拆分**：`ok`=已配置凭证、新增 `reachable`=真连通（初值 False
  自愈式点亮）；状态页/health 改用后者——修复「HA 掉线仍显示在线」的 UI 撒谎。
- **mdns 接线修正**：props（stt/tts/llm 路径+version）改构造期传入。
- **对外版本三条独立链互不一致** → 统一 `const.addon_version()`
  （容器 stamp 优先、env 次之、常量兜底）。

### Fixed（两轮审查环——Python 层 8 项 + 基础设施层 8 项，逐项对上游源码实证）
- **引擎卸载/推理互斥**（锁内快照当代对象 + busy 计数，在飞跳过；reaper/管理重载/
  真机三处实证「合成中，跳过」）；**ModelStore single-flight**（per-key 锁 + 唯一
  `.part` 后缀 + 幽灵状态键修正）；**TextCNN 推理出事件循环**；**停机链收束**
  （abort 位打断下载、在飞 task 取消、gather 带超时）；**TTS 预算逐包 wait_for**；
  **状态文件 tmp 唯一化**；**pong/hello 持强引用 + 断连取消在飞转写**；
  **Zeroconf 失败路径 close**。
- **CI 必炸**：builder@2025.11.0 参数文法不含 `--docker/--build-arg/--tag/--label`
  （catch-all → exit.nok）——先按文法修正，随后整体迁至网关已验证的
  split-actions@2026.06.0 范式，问题不复存在。
- **基镜像幻觉 tag**：`hassio-addons/debian-base:7.2.6` ghcr 实查不存在 →
  **7.8.3**（实测多架构）；`build.yaml`（builder 读）与 Dockerfile ARG 默认（裸
  构建读）双源钉桩防漂移；Supervisor≥2026.04 起不再注入 BUILD_FROM。
- **`import opus` 必炸两处**（Dockerfile 构建自检 / boot.sh 运行自检）→
  `opuslib_next`，加正则钉桩。
- **schema `=默认值` 是臆造语法**：本仓初版同样写过 `int=0`/`str=info`——姊妹仓
  网关 v1.7.12 因同款语法触发「**加载项从商店整体静默消失**」P0 回归
  （上游 `RE_SCHEMA_ELEMENT` 实证从无 `=` 文法；商店刷新校验失败即 continue，
  无任何前端报错），网关 v1.7.16 已修复并记录在案。本仓上线前即收敛为纯文档
  语法 `"int(0, 1440)"` / `"list(debug|info|warning|error)"`，默认值一律走
  options 块，并由 `test_schema_documented_grammar_only` 钉死。
  （本条早前版本曾误写「姊妹仓写法生产正常」——与网关 CHANGELOG v1.7.16
  事故记录矛盾，已更正。教训同源：改 schema 任何值前必须对上游源码实证语法。）
- **Ingress 管理页失效**（www 根绝对路径 fetch 打到 HA Core）→ 网关 huijian.js
  实证范式 `INGRESS_BASE` 前缀；直连 :8001 同口径兼容。
- **安全边界收口**：含 WS token 的 endpoints.json 迁出 nginx 静态根 →
  `/data/run/`（固件/小程序零消费者实证扫描后执行）；nginx 注释与实况对齐。
- **`_loop_models` pend NameError 边缘**（异常路径打死模型保障循环）+ 商店清单
  更名 `repository.yaml`（新规范）。

### 验证与测试基线
- **105 项 pytest 钉桩**全绿（NLU 矩阵/WS 协议契约/管理面路由/并发守卫/基建契约/
  发布一致性），CI lint 硬门禁。
- **Windows 全栈 E2E-lite**（真 sherpa-onnx + 真 onnxruntime + 真 Kokoro + 真
  libopus）：三通道两轮 + 卸载/惰性重载/busy 避让/single-flight 实测。
- 尚欠（发布前必须补，见 CI e2e job 与 DOCS「安装步骤」）：Docker 双架构真机构建、
  HA OS 实机 + 固件 + 小程序端到端。

### 已知边界（非缺陷，见 DOCS.md「限制」节）
- LLM 关闭时未理解语句播固定兜底；「保存为场景/创建自动化」类 M1 再接管；
- 空调类指令缺区域信息时按母本守卫拒执行（需说出区域，或开 LLM）；
- PlayMusic 意图显式不接管。
