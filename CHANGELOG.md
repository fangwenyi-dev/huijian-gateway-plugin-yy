# 变更日志

所有版本变更记录在此文件中。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。

## [1.0.4] - 2026-09-10

### 修复
- **NLU「办公室射灯」区域丢失（用户实机轨迹）**：射灯/灯带/吸顶灯/台灯/落地灯/
  床头灯/夜灯 原只在表二，设备词子串扫描与加分只认表一 → 找不到 len≥2 设备词、
  退单字「灯」且区域整个丢失，HA 等值匹配报"没找到这个设备"。表一/表二并为
  全集（③⑥⑦与加分共用；表二原样保留供 fast_path 前缀剥离）；区域提取统一走
  新增 `_area_of_prefix`（剥「的/里/得」属格 + 未知复合词二次前缀回捞，只回区域
  不臆造设备名）。端到端钉桩「打开 办公室射灯」→ `{area:办公室, name:射灯}`。
- **"射灯"类实体匹配不上（真栈 E2E 实锤）**：MQTT/z2m 实体注册表 name 常为 None，
  用户中文名只活在 friendly_name 组合串（"TSL2011 射灯"）。intent 匹配新增
  第6级 friendly_name 子串兜底：仅前五级全空时启用，带区域请求必须同区域
  （防跨房间过匹配），隐藏/禁用实体过滤。
- **集成 import 崩溃（HA 2026.x）**：`_build_candidate_entities` 注解 `er.Registry`
  已被核心移除且函数注解运行期求值——import 即 AttributeError、全集成瘫痪
  （2026-09 E2E 实证）。改 `er.EntityRegistry`，真栈 E2E 兼做该文件冒烟。
- **二维码 ha_internal 无端口（配对 status=-1 源头修复）**：HA internal_url 常配成
  裸 IP（http://192.168.1.91），设备固件 HttpClient 按 :80 打必死、CMD20 恒回 -1
  （小程序侧 v1.4.3/1.4.4 只能乐观猜 8123——双端各猜一次不如源头给对）。新增
  `_ensure_lan_port`：仅「http + IPv4 私有地址 + 无显式端口」补 HA 实际监听端口
  （`hass.http.server_port` 实况优先、缺失回落 8123）+ 裸根路径归一；域名/https
  （反代语义归用户）/公网/IPv6 一律原样放行。行为级钉桩 10 断言（AST 抠真实
  函数体 exec；IPv6 越界守卫为发布前审查实锤补钉）。

### 改进
- **管理页并入「星辰大海」设计体系**（与 LoRa 网关加载项同源）：千行内联样式拆为
  `css/huijian.css`（令牌+星野层）/`css/voice.css`（页面补充）/`js/starsky.js`
  （固定种子星空，mulberry32——种子不变同一片天）；与母本字节级防分叉守卫钉桩
  （修设计改母本再同步，禁单边演化）；静态资源挂 `?v=<版本>` cache-bust
  （网关 v1.7.1 教训：不硬刷拿不到新 UI）；零外链零 CDN、离线 LAN 可用不变。
- 加载项 icon / logo 视觉刷新。

## [1.0.3] - 2026-09-08

### 修复（紧急）
- **集成配置向导 500（v1.0.2 回归）**：`async_step_qrcode` 中
  `"ha_internal": internal` 引用了求值顺序在其后的 `internal` 变量 →
  `UnboundLocalError`，"无法加载配置向导"。修正求值顺序并新增行为级
  钉桩 `test_internal_bound_before_params_use`（去注释求值序断言——
  纯文本存在性检查拦不住顺序 bug）。

## [1.0.2] - 2026-09-08

- **修复（内嵌集成 config_flow）**：设备配对数据等待窗 60s→**300s**——实机日志
`Timeout waiting for setup data` 根因：扫码→贴令牌→BLE CMD20→设备 POST 的人肉链路
远超 60s；超时日志带可操作指引，setup_data 缺失时给中文引导话术（不再裸报
「配置类型未知」）；行为钉桩 test_integration_config_flow（3 条）。

### Added（三项目完美适配战役·批次1：加载项侧三修）
- **`:8000 GET /discover` 无凭据端点发现面**（判定书缺口2）：小程序/设备侧
  三扇门全关（/api/endpoints 被 nginx ACL 403、endpoints.json 不落静态根、
  微信 mDNS 读不到 TXT）。新端点回三通道路径+音频参数(require 16k/60ms)+
  require_token 状态；**响应体永不回 token**（含强制校验模式），契约测试
  双模钉桩（test_protocol_ws +2：结构+泄漏断言）。
- **huijian_ai `/api/huijian-ai/device-info`**（判定书缺口4）：小程序
  queryHaDevice 按 mac/speak_id 查设备 host:port（真源=config entry data），
  此前 404 静默失败（配网页设备 IP 恒空）。`requires_auth=True`——回内网
  拓扑必须 HA token；assist 类无 host 条目跳过不误报。

### Fixed
- **DOCS「卫星固件直连 :8000」话术修正（判定书 D-8）**：本仓 xiaozhi 卫星
  （0513gujian）主路径是模式 B（ESPHome API :6053 → HA 管线），直连 :8000
  为集成消费端与 matter-broker 形态；端口表补 /discover 行。
- 测试 fixture 残留 `access_logger=None` 臆造 kwargs → `access_log=None`
  （与 v1.0.1 core 侧修复同族）。

## [1.0.1] - 2026-09-08

### Added
- **镜像主源迁至自有阿里云 ACR（v1.0.0 首装卡下载的根治，用户开通）**：
  `config.yaml` image 改 `crpi-…cn-shanghai.personal.cr.aliyuncs.com/
  fangwenyi-dev/huijian-gateway-plugin-yy`（个人版公开仓，**单仓多架构 OCI
  index**——Supervisor 拉同 tag 由 docker 自动选架构，弃 `{arch}` 仓名模板）。
  CI 新增 `push-acr` job（**release 硬前置**）：`scripts/acr_transcode.py`
  从 ghcr 双架构仓下载层 → zstd 解压 → 重压缩 gzip → 校验 diff_ids → 重写
  manifest 推 ACR，再合成 `$VERSION`/`latest` 双 tag index；自带匿名端到端
  实证（index→子 manifest 层全 gzip→真拉层 blob 验 sha256+魔数）。凭据
  `ACR_USER/ACR_PASS` 入 GitHub Secrets。ghcr.io 双架构仓+manifest 原样
  保留=灾备源（DOCS FAQ 给完整可抄换源串）。
  两代实发教训：imagetools 按 tag 复制连 provenance attestation blob 一起
  搬→ACR 403；@digest 绕过后 zstd 层仍 403——本地鉴别实验（真 gzip=202 /
  zstd 魔数=403 / 纯字节=403）定案 **ACR 个人版层流仅收 gzip**，复制无解、
  必须转码（层内容零改变，config/diff_ids 原样）。
- **透传站体系整体退役**：`warm-mirrors` job 与手动补热 `warm.yaml` 删除
  （1ms/nju 边缘缓存覆盖靠运气的结构性缺陷实锤：新 tag blob 33KB/s 慢滴/
  0B 假活）；钉桩 `test_image_source_acr_strategy` 禁复活 +
  `test_store_schema` 域名白名单换 `{ACR, ghcr.io}` 并逐字钉死主源路径。

### Changed
- **商店显示名「慧尖语音助手」→「慧尖HA语音插件」**（用户定案）：与 LoRa
  网关加载项在商店/文档/管理页三处同屏场景下明确区分。触点：config.yaml
  `name`（商店卡片权威源）、Ingress 管理页 title/H1、商店 DOCS 全部卡片话术
  （用户照文案找卡片，一处不能漏）、根 README。slug `huijian_voice` 与镜像名
  `huijian-voice` 不变——改 slug 会断老用户升级路径。

### Fixed（v1.0.0 实机日志三修，钉桩 ×3 于 test_concurrency_guards）
- **`models_status.json` 权限回写 bug**：ModelStore 进度写者 `_write_status`
  用 mkstemp（0600）漏 fchmod——每次下载进度落盘都把主循环写好的 644 文件
  刷回 600，nginx worker 读走 13 → 管理页模型状态整段刷不出（v1.0.0 实机
  开下载后 `Permission denied` 日志刷屏根因）。
- **aiohttp 每请求 WARNING 刷屏**：`web.AppRunner(..., access_logger=None)`
  是臆造 kwargs（3.12+ 起每次建请求 handler 打
  `Failed to create request handler with custom kwargs` 回落告警）；官方
  禁访问日志参数是 `access_log=None`，两 Runner 均已改。
- **mDNS 阻塞与失败显形**：Zeroconf 构造/register/unregister 含阻塞网络
  I/O，从事件循环直调改为 `asyncio.to_thread`（启动期曾卡 tick 11s）；
  广播失败日志 `%s`→`%r`（v1.0.0 实机异常文本为空无从诊断，下次带类型
  显形）。`host_network: true` 在位，静态端口接入不受影响。

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
  语法 `"int(0,1440)"` / `"list(debug|info|warning|error)"`，默认值一律走
  options 块，并由 `test_schema_documented_grammar_only` 钉死。
  （本条早前版本曾误写「姊妹仓写法生产正常」——与网关 CHANGELOG v1.7.16
  事故记录矛盾，已更正。教训同源：改 schema 任何值前必须对上游源码实证语法。）
- **Ingress 管理页失效**（www 根绝对路径 fetch 打到 HA Core）→ 网关 huijian.js
  实证范式 `INGRESS_BASE` 前缀；直连 :8001 同口径兼容。
- **安全边界收口**：含 WS token 的 endpoints.json 迁出 nginx 静态根 →
  `/data/run/`（固件/小程序零消费者实证扫描后执行）；nginx 注释与实况对齐。
- **`_loop_models` pend NameError 边缘**（异常路径打死模型保障循环）+ 商店清单
  更名 `repository.yaml`（新规范）。

### Fixed — CI 首跑实证修（v1.0.0 发布过程中，2026-09-08 首推 run 34033427692）

- **Dockerfile 全局作用域违规**：`LABEL org.opencontainers.image.source` 误置于首个
  `FROM` 之前——Docker 规定 stage 之前只接受 ARG/parser 指令/注释，buildx 报
  `no build stage in current context`，amd64/aarch64 双 build job 同炸，
  e2e/manifest/release 全链级联 skip（门禁设计如此，未发布任何半成品 ✓）。
  修复：LABEL 移入 stage 内；钉桩 `test_dockerfile_pre_from_scope_only_args`
  静态扫描 FROM 前所有非注释行，复发即红。


- **镜像 CMD 与 base ENTRYPOINT 重复**（v1.0.0 run4 e2e 实炸）：`CMD ["/init"]`
  叠加 base 自带 `ENTRYPOINT ["/init"]` 成 `/init /init`，argv 污染打崩 s6 v3
  legacy services（`s6-overlay-suexec: fatal: can only run as pid 1`，全服务停摆）。
  Supervisor 真机会覆写 CMD 故本地形态不可见——真镜像 e2e 门禁独有能力。钉桩
  `test_dockerfile_no_redundant_init_cmd`。
- **版本显示链 0.0.0 毒值**（v1.0.0 e2e step5 前瞻拦截）：split-action 不注入
  `BUILD_VERSION`，裸 docker build 把 ENV 烘成 0.0.0 且穿透到 health/管理页。
  版本戳权威源改为镜像内 `www/version.json`（四源一致已被钉保护），const 过滤
  0.0.0/dev。钉桩 `test_version_stamp_poison_guard` + e2e step5 版本链断言。
- **e2e 容器内客户端路径推导**（run5 实炸）：客户端拷至容器 /tmp 后按仓内
  相对层级找 `core` 包 → ModuleNotFound。`E2E_APP_ROOT` 显式注入（编排+客户端两侧）。
- **nginx 读事实文件 403**（run6 实炸，POSIX-only 面）：`_atomic_write` 的 mkstemp
  默认 0600，root 写者落盘后 www-data worker 读 `status.json` Permission denied。
  修复 fchmod 0644；钉桩 `test_atomic_write_public_readable`（Windows 权限模型
  不可见，CI 独有抓面）。
- **模型就绪判定竞态（flaky 根治）**（run7 实炸）：`extractall` 直解 target，
  250MB onnx 半写时 `exists()` 级就绪误报 → STT 读残档 `Protobuf parsing failed`，
  run 间时好时坏。新增 `.extracted_ok` 完成章（全部成员解包成功后才写），
  就绪判定升级为内容级原子。钉桩 `test_extract_marker_atomicity`（半写目录
  不得就绪）+ run_local 对 dev 预解包目录补章迁移。
- **Gitee Release BOM 拦截守卫立功**（run8）：本机 `.gitee_token` 文件带 UTF-8 BOM，
  配进 GitHub Secret 后 CI 逐字节守卫当场拒收（正是网关 v1.6.21 事故换防线）；
  Secret 以 `utf-8-sig` 清洗重配。Gitee 仓可见性需网页手动切公开（API 强制 private）。

### 验证与测试基线
- **115 项 pytest 钉桩**全绿（NLU 矩阵/WS 协议契约/管理面路由/并发守卫/基建契约/
  发布一致性），CI lint 硬门禁。
- **Windows 全栈 E2E-lite**（真 sherpa-onnx + 真 onnxruntime + 真 Kokoro + 真
  libopus）：三通道两轮 + 卸载/惰性重载/busy 避让/single-flight 实测。
- 尚欠（发布前必须补，见 CI e2e job 与 DOCS「安装步骤」）：Docker 双架构真机构建、
  HA OS 实机 + 固件 + 小程序端到端。

### 已知边界（非缺陷，见 DOCS.md「限制」节）
- LLM 关闭时未理解语句播固定兜底；「保存为场景/创建自动化」类 M1 再接管；
- 空调类指令缺区域信息时按母本守卫拒执行（需说出区域，或开 LLM）；
- PlayMusic 意图显式不接管。
