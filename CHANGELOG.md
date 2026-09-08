# 变更日志

所有版本变更记录在此文件中。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。

## [1.0.15] - 2026-09-08

### 修复

- **删除 assist 引擎条目必炸 remove 回调**（2026-09-08 台架实发 `AttributeError: 'ConfigEntry' object has no attribute 'runtime_data'`）：HA core 的删除流程是 unload（成功后 `object.__delattr__(entry, "runtime_data")`）→ `async_remove_entry`，本 fork `get_entry_data` 的 assist 分支与 `diagnostics` 均裸访问该属性——setup 失败或 unload 后的条目必抛。三处收口：`get_entry_data` assist 分支 `getattr` 缺席守卫（读语义与空 dict 对齐，写路径仅存在于 setup 期不受影响）、`diagnostics` 未加载条目降级返回、回归测试 `tests/test_runtime_data_guards.py`（stub 真 import 复现最小场景 + 源码防回退钉桩）。
- **mcp_transport 清理不可达 → 关闭时点移到 unload**：assist 的 runtime_data 在 remove 回调时已被 core 删除，`async_remove_entry` 里再取 transport 永远拿不到（配置了 MCP 的场景 WS 泄漏跨 reload）。`async_unload_entry` 关闭列表并入 `mcp_transport`（transport 的 `async_remove_entry` 自带 pop，remove 回调二次触发为 no-op，无双重关闭）。
- **validation-error 两端都看不见原因**（同次台架实发：删引擎条目后按唤醒键，设备只有空文案 `code=validation-error error=`，HA 侧零日志）：卫星 `on_pipeline_event` 对 `validation-error` 且域内无 assist 条目时输出修复指引 WARNING（按既有定案不静默补回被用户删除的条目，只保证可诊断）。配套固件 v2.1.10：ERROR 事件载荷同时接受 `error`/`message` 键名（core 实际发 `message`，此前真实文案被吞成空串）。

### 配套（固件仓 0513gujian v2.1.10）

- **按钮/唤醒 0ms 假超时根修**：`voice_assistant.loop()` 循环顶 `const now` 早于 wake 块内 `set_state(START_PIPELINE)` 刷新的 `state_since_ms_`，uint32 相减下溢成巨值 → 进 switch 首帧即判超时自拆会话（台架日志 send 与 "did not answer" 同毫秒实锤；HA 的 port=0 Response 仅 20ms 后到达）。wake 块后刷新 `now` 基准。
- ERROR 事件 `message` 键名解析（见上条）。

### 排障

- 商店更新竞态窗口入档：`main` 推送即商店可见新版本，而镜像 ~15 分钟后才经 CI（e2e→manifest→push-acr）到 ACR；窗口内点「更新」报 `unknown error with app ... Check Supervisor logs`。恢复 = 稍后重试；发布方自检用 `scripts/verify_release.py`（ACR/ghcr 双源 manifest+blob 探活）。根治方案（tag 触发发版、main 殿后）见 DOCS 排障节，待整链实发验证后实施。
- 巡检发现 ACR 无 tag 管理新政实发：v1.0.11 镜像当日 CI 全绿推送、数小时后 404（仅最近两版+latest 存活）；正常升级不受影响（按目标 tag 精确拉），回滚需从 ghcr 灾备源回推。

## [1.0.14] - 2026-09-08

### 修复

- **配选实体 translation placeholders 告警**（2026-09-08 实机配对后 HA 日志钉出）：`EsphomeAssistPipelineSelect` 携带 `{index}` 模板名却无对应占位符，每次条目加载刷 `entity.py:706` 告警「translation placeholders '{}' do not match the name 'Assistant{index}'」。根因在上游 core#152245：`AssistPipelineSelect` 的 `pipeline`/`pipeline_n` 键均不传 `translation_placeholders`，本仓 translations 沿用带 `{index}` 模板名 → 台架 2026.9.1（上游 PR#165676 未及版本）触发。子类 `__init__` 照抄同文件 wake_word 实体形态补齐（index=0→`""`、index≥1→`str(index+1)`，与 core 键语义一致）；上游修复发布后本补强幂等无害。仅消噪，不改用户可见名称。
- 三端契约定稿配套：本次实机验证扫码配对链路——固件 v2.1.8 `persistNoisePskSync` 配网模式 NVS 直写成功、HA 条目建成握手一次通过；CMD20 槽5/6 三端冻结恒空、`mcp_endpoint` 无 `?token=` 垃圾值；集成侧 `_clean_mcp_endpoint` 归一逻辑经检验对冻结空值天然免疫（存量未刷机设备亦被覆盖）。集成 v1.0.13→v1.0.14 仅上项消噪，三端协议零变更。

## [1.0.13] - 2026-09-08

### 修复（上游模型资产重打包批）

- models.lock.json TTS Kokoro sha256 回填：上游 k2-fsa/sherpa-onnx 重打包重传了 kokoro-multi-lang-v1_0.tar.bz2（本机直下复核：377 文件、model.onnx 325560556B、required_files 与旧实测逐项一致——内容未变、字节变），旧锁值在 v1.0.12 E2E 硬门禁双源 5 连败。不修影响：新装/重下模型的设备 TTS 陷入下载-校验死循环（E2E 只是最先撞上）。paraformer ASR 包双源实测未变，不动。

- 本发布顺带携全 v1.0.12 内容（6053 重启窗 30s 校准 + 窗户误动作闸）：v1.0.12 镜像被 E2E 门禁阻断、从未进入 ACR 仓库，v1.0.13 是其唯一分发载体；HA 商店更新卡片将由 1.0.11 直接到 1.0.13。

### 测试

- 升锁后 E2E 真镜像门禁全链通过（Lint/Build×2/E2E/Manifest/Push ACR）。pytest 248 全绿不变（仅动 lock 与版本 pin）。

## [1.0.12] - 2026-09-08

### 修复（2026-09-08 真机配对闭环批）
- 6053 重启窗校准：v1.0.11 的 6s×2=12s 窗只盖住固件 10s 延迟重启本身，漏算 esp_restart 引导 + WiFi 重连 + API 起听（实机串口：CMD20 完成 → +10.01s 复位 → :6053 就绪 ≈ POST 后 18~25s），重配对恒差约 13s 撞墙。重试窗拉宽为 6s×5=30s；30s 末仍 connection refused 视为非时序问题（IP 漂移/设备未起），照常报错。配套固件修复（0513gujian）：Noise PSK 首次 save 被空 old_psk 快路径吞掉、主循环标志先吃后看条件、重启任务不等 PSK 落盘——InvalidEncryptionKey 死循环三处已修。
- 窗户误动作闸：ControlWindow（慧尖独占）执行失败降级 klar 时，若 klar 兜底计划目标不含窗类语义，禁止降级——实机「打开 办公室平开窗」在集成未加载环境下被 klar 模糊匹配点亮办公室灯，误动作比礼貌失败糟糕。窗帘/纱窗=标准 cover 契约不受影响；klar 兜底命中真窗类实体（开合器以 cover 暴露、名含窗型）仍放行。不降级时如实播集成诊断话术。

### 测试
- 重启窗 floor 从 ≥10s（假达标）改为 ≥25s（覆盖重启+引导+回连）；窗户闸 3 项（灯误伤封死/窗实体放行/窗帘直通）。245 → **248 全绿**。

## [1.0.11] - 2026-09-08

### 修复（配对时序与超时体验，2026-09-08 审查批）
- 6053 重启窗重试：CMD20 成功后固件延迟 10s 重启，期间设备 :6053 未监听，集成 `fetch_device_info` 撞上即 `Connection refused`（Errno 111）误判配对失败。新增 `_fetch_device_info_through_reboot`：仅 connection_error 重试（6s×2 覆盖重启窗），鉴权/PSK 等确定性错误不重试。
- 超时话术翻译补全：config flow 两处（等待超时/未知类型兜底）悬挂的 `unknown_config_type` 键已补 zh-Hans/en 双语——此前用户 5 分钟超时看到的是无翻译红字。
- 等待超时「再等一轮」：超时表单新增 rewait 勾选，保持同一 setup_uuid 续等设备迟到 POST（旧表单再提交只会重复同一条错误的死胡同封死）；取消勾选干净退出（新增 abort 键 `no_setup_data`）。
- 配套：小程序 v1.4.8——扫码解析补 mac/speak_id 多设备定址（多设备「重新配置」必判 ambiguous 的断链根治）、配对成功弹窗矛盾双窗收口、HA 账户地址双源统一、手输尾斜杠 //api 404 根治、配对表单「助手模式」死 UI 退役。

### 测试
- 重启窗重试 4 项 + 审查修复钉桩（B 翻译键/F rewait 消费）2 项。243 → **245 全绿**。

## [1.0.10] - 2026-09-08

### 修复与适配（HA 2026.8 端口时代）
- 调试面板「真执行」改走 _cascade 全链：显示哪个计划就执行哪个计划，三层裁决/互为降级/LLM 次序与真实流量完全一致，回显实际执行的 source 与执行轨迹（旧版面板显示 klar 裁决、实际执行 t0 计划，两条通道各挂各的，调试结论不可信）。
- klar 直调通道失败话术纠偏：Supervisor/HA 5xx 不再误播「慧尖 AI 集成还没生效」（09-08 实机误导：klar 灯句直调失败被归因集成，引用户去装用不上的依赖）；改播「和 Home Assistant 的连接没有走通，请检查 HA 核心与加载项 API 配置」。慧尖意图通道原集成话术保留。
- fast_path 窗型纠正：12 窗型词（平开窗/推拉窗/内开内倒窗/推拉门/天窗/飘窗…，与集成 WINDOW_ACTION_MAPPING 对齐）落进设备名时由 TurnDeviceOn/Off 纠正为 ControlWindow（open/close）；parse_target 把「推拉门」的推/拉当残留动词切碎时从 rest 尾部找回完整窗名。窗帘/纱窗=标准 cover 不受影响。
- 背景：Home Assistant 2026.8 起官方告别 8123（既有安装自动迁 :80；/api/ 未鉴权由 200 改 401+Bearer）。本加载项走 supervisor/core/api 代理与 LAN 端口解耦；配套小程序 v1.4.7 已废除「探测失败猜 8123」，改为透传 HA 真实监听地址（带端口/无端口双形态支持）。

### 测试
- 话术归属 2 项、面板全链源码桩 1 项、窗型纠正 3 项（含 MATRIX 2 例）。232 → **239 全绿**。

## [1.0.9] - 2026-09-08

### 级联重构（用户三条指令定案）
- 三层裁决取代「字面表恒先」：scene 触发词契约最高 > 慧尖独占意图（窗户/模式/属性调节/语音场景与自动化管理/实时上下文，含目标名带「窗」句式）> klar 标准控制恒先于 t0/T1 剩余 > 字面表兜底。
- 执行期两路互为降级：慧尖意图失败（典型根因=集成未加载）且 klar 同句命中 → klar grounded 直调服务兜底；klar 失败 → 回退字面表计划（scene 永不作第二路径）。两路全挂时优先播点破「集成」根因的话术。
- LLM 维持「用户配置才启用」的最终兜底：快速通道（klar+慧尖）双双用尽后复议；未配置 = 完全无视。
- 窗户保护：窗帘/纱窗=标准 cover 放行 klar；窗户/天窗/内倒窗/开合器/推拉门留在慧尖意图（按钮按压与内倒/暂停语义 klar 无法表达）。

### 测试
- 级联仲裁旧契约反转重写；新增降级纯函数 4 项、_cascade 行为 4 项。基线 220 → 232 全绿。

## [1.0.8] - 2026-09-11

### 修复
- **B0（严重）assist 自动注册真机失效根治**：v1.0.7 的
  `__init__._assist_default_data` 使用 `CONF_DEVICE_NAME` 键但漏了 import——
  真机只要 host 解析成功即抛 NameError，SOURCE_IMPORT 自动补建从未生效
  （fire-and-forget 任务异常无人收割，v1.0.7 的字符串钉桩测不到这类 bug）。
  现改用 async_step_import 实际消费的 `speak_name` 键，并把端点推导调用点
  一并纳入 try（推导异常也 fail-open，绝不产生未收割任务异常）。
- **B1 assist 条目 reauth 死端**：端点 401/失效时 ws_transport 清空端点并触发
  reauth，但 `async_step_reauth` 对 assist 条目仍进 qrcode 流 5 分钟死等设备
  POST（assist 无设备侧 POST 来源）。现 reauth 与 reconfigure 同款分流：
  assist → 直接进端点编辑表单（复用 async_step_assist_reconfigure）。
- **执行失败话术误导修正（web 调试台实锤）**：全新环境未安装 huijian_ai
  集成时，HA 对 `POST /api/intent/handle` 的未注册 intent（TurnDeviceOn
  等由集成注册）回 5xx → 旧 500 专属话术只说"刚升级没重启"，完全误导。
  现 500 话术双场景（首次安装指引 + 升级重启指引），并新增
  "Unknown intent" → 安装指引映射。
- **B4 assist 条目名与 uuid 便签**：`async_step_import` 现兼容
  speak_name/device_name 两种键（条目 device_name 不再静默落空串）；
  `_async_create_or_update_assist` finalize 前 `clean_setup()` 弹掉
  `hass.data[DOMAIN]` 的 uuid 便签（create_entry 路径不经 async_abort，
  原实现每次 boot 自动补建都滞留一条内存泄漏）。

### 新增
- **一级确定性 NLU：接入 klar-ha-nlu 引擎（架构扩展）**。Rust 规则引擎
  （MIT，FABBricate-IT-Solutions/klar-ha-nlu v2026.9.2）随加载项以 s6 服务
  运行（新 klar-engine.sh → services.d/klar-engine/run，仅绑回环 :10520，
  home graph 直读 /homeassistant/.storage——零额外挂载、零快照推送）；
  boot.sh 拉官方 GitHub release 二进制并强制 SHA-256 digest 核验（缺
  digest 拒装），无网可容忍（只警告，版本戳幂等）。分派仲裁：
  字面表（T0/场景触发）> klar > TextCNN T1——用户配的触发词是产品契约，
  任何模型不许抢；klar 仅在 decision=execute、多分句全部命中标准控制族
  白名单（HassTurnOn/Off/Toggle/LightSet/ClimateSetTemperature/SetPosition/
  Lock/Unlock/Fan/Vacuum…，HA 内置 handler 可直接执行、不依赖 huijian_ai
  集成）、置信 ≥0.80 时接管，查询/媒体/计时/日历放行给既有查询族/LLM。
  引擎自带中文播报优先于话术层模板；多分句按序执行。全程 fail-open：
  缺席/超时/形制漂移恒降级，连续 5 败熔断 300s（熔断期零外拨零延迟），
  引擎不存在时行为与 v1.0.7 逐字一致。配置键 `klar.enabled/url/language/
  timeout_s/min_confidence/token`（默认即开，Supervisor options 零新增）。
- `assist_reconfigure` 步骤中英双语 UI 文案（含"加载项开启 token 强制校验时
  端点追加 ?token="指引）。

### Web（平台预设一键接入）
- **STT/TTS/LLM 三张卡新增「平台预设」下拉**：选平台即填好 Base URL/模型名/
  音色并标注 API Key 申请入口，用户只剩粘贴 Key 一步。收录纪律 = 仅标准
  OpenAI 兼容端点：LLM 12 家（DeepSeek/百炼/火山方舟/智谱/Kimi/硅基流动/讯飞星火/
  Gemini/OpenAI/Ollama 局域网免 Key 等）、STT 5 家（百炼 Qwen3-ASR/硅基
  SenseVoice/Groq/OpenAI/302.AI）、TTS 3 家（硅基 CosyVoice2 八预置中文音色/
  OpenAI/302.AI）；讯飞/火山/腾讯/百度语音为私有协议**明确不收**（注释钉纪律 +
  测试红线防回潮）。对照小智官方平台清单逐项核实兼容端点。
- **云 TTS 健壮性配套**：预设透传 response_format/sample_rate（硅基 pcm 默认
  44.1kHz 坑）；RIFF/WAVE 嗅探自动拆封取真实采样率（平台不守 format 也不出爆音），
  mp3/ogg 明确报错指引改配置；TTS 云档新增「模型名」输入框（此前只能改 settings 文件）。

### 文档
- **商店源容灾指引（真机客户故障 2026-09-07 实证）**：Supervisor 对三个商店
  仓库 `git ls-remote` 全刷 `SSL unexpected eof / StoreGitError`——GitHub 被
  网络侧干扰，商店拉不到清单即看不到新版本（已装加载项不受影响，镜像走 ACR）。
  源码实证根因边界：**2026 版 Supervisor 已无「互联网代理」配置项**，老教程
  `--proxy-url` 不再适用，国内唯一硬解 = 镜像源。落地：根 README（原为空
  文件，补齐商店入口页）、repository.yaml 用法注释、DOCS 安装步 + 排障新增
  Gitee 镜像源指引（`gitee.com/fangwenyi-dev/…` 逐提交同步实测、二选一勿
  同加、静态 DNS 缓解、第三方仓库可移除消刷屏）；姊妹仓网关商店 README
  安装步同步双源。发布纪律追加：**每次推送必双推（GitHub+Gitee），否则
  Gitee 源客户看不到新版**。

### 测试
- `test_integration_config_flow.py` +4 钉桩：assist reauth 分流、import
  键名兼容、clean_setup 泄漏清理，以及**模块级名字解析 AST 静态钉桩**
  （_unresolved_names 扫 `__init__.py`/`config_flow.py`，B0 这类
  "用了未导入的名字"从此被测试拦下）。
- 新增 `test_klar_nlu.py` 35 项：裁决纪律（execute-only / 白名单
  all-or-nothing / 置信门 / slot 形制容忍）、fail-open 与熔断计数、级联仲裁
  纯函数、多分句顺序执行与 klar 播报优先，以及 boot 分发 / s6 只绑回环 /
  Dockerfile 装配 / settings 默认值的形状钉桩。顺手修复 `_EN_ERR_MAP`
  死键 "no.*match"（该表按子串字面匹配，正则键永不命中；改为 "no match"）
  ——klar 走 HA 内置 handler 的 no-match 400 由此才有中文话术；klar grounded
  步骤（引擎已解析出 entity_id）新增 `HAClient.call_service` 直调通道
  （intent handler 不认 entity_id 槽，klar 自家集成亦走此路线），含锁 D7
  语义对齐、灯光属性键裁剪、多目标列表透传等 9 项执行映射测试。
- `test_cloud_presets.py` 17 项：RIFF 拆封/奇数块对齐/mp3·opus 拒收/wav 实际
  采样率优先/请求体透传/预设目录形状与私有协议红线钉桩。220 全绿。

## [1.0.7] - 2026-09-11

### 新增
- **assist 语音引擎条目自动注册（三端配合 P0-1 修复）**：语音卫星设备
  （config_type=device）入驻成功后，集成自动经 SOURCE_IMPORT 补建一条
  config_type=assist 的语音引擎服务条目——stt.huijian_asr /
  tts.huijian_speech / conversation.huijian_agent 三实体随慧尖设备安装自动
  注册，端点默认指向本机加载项 `ws://<HA 局域网地址>:8000/xiaozhi/v1/…`
  （与加载项 host_network 同宿主）。此前 assist 型条目无任何可达创建路径
  （固件 CMD20 恒 device、小程序 setupData 恒 device），装了语音卫星也选不到
  本地引擎。自动补建 fail-open：缺 host/已存在 assist/失败均不影响设备装配，
  删除 assist 条目后不静默补回（置位标记防重）。
- **assist 端点后期可改**：assist 条目「重新配置」现走专用端点编辑步
  （原分流到 device 扫码流对 assist 不适用），llm/stt/tts/mcp 四条端点可改。

### 修复
- **H6 语义澄清**：`/api/huijian-ai/device-info` 注释明确 requires_auth=True
  服务对象是"已持 HA 长期令牌的小程序/客户端"（扫码入驻走 uuid 通道不经此
  View，token 为空属正常态）；assist 类无 host 条目被跳过属预期。
- **H2 签名约定文档化**：`calculate_sign` 补跨端约定注释——uri 必须纯路径
  （固件 hashAuthorization 同款），params 字典序、mac 小写，防未来误卷
  host/query 导致签名失配。

### 测试
- `test_integration_config_flow.py` +5 钉桩：SOURCE_IMPORT 自动注册入口存在、
  默认端点构造行为（8000/xiaozhi/v1 三通道）、assist reconfigure 分流、
  qrcode_done 复用 helper、`__init__` 自动补建 hook。162 全绿。

## [1.0.6] - 2026-09-11

### 修复
- **「抱歉，这一步没有执行成功（）」空括号话术（真机实锤，v1.0.5 之后浮出）**:
  v1.0.5 修通 URL 后，意图请求首次真正抵达 HA 侧 handler；若客户 HA 内存中
  仍在运行升级前加载的旧版集成代码（加载项升级只重启加载项容器，HA Core 不
  自动热重载 custom_components），旧 handler 对匹配失败分支走 `assert` 抛未
  捕获异常 → HTTP 500 纯文本 → 加载项侧被折叠成空 message → 播报只剩空括号。
  真栈逐字复现后才定位。三层加固：
  - **集成侧永不抛**：`intent_turn` / `intent_adjust_attribute` /
    `intent_set_mode` 三处 `assert candidate_entities` 改为结构化
    `{"success": false, "error": "No available devices found"}` 返回——
    匹配失败永远走响应体，不再制造 500。
  - **加载项侧 5xx 结构化**：HA REST 客户端对 5xx 统一给
    `HA 内部错误(<状态码>)`，任何上游内部错误都不再洗出空话术。
  - **话术映射补齐**：`No available devices found` 播报「没找到符合条件的
    设备，试试带上房间名或换个叫法」；`HA 内部错误` 播报附可操作指引
    （「多半是集成刚升级还没重启生效，请在 Supervisor 重启 HA Core 再试」）。
- 升级配套提醒：加载项自动落盘新集成后，**需重启一次 HA Core 生效**
  （Supervisor → 系统 → 主机 → 重新启动）——这是本次空括号现象的客户侧根因。

### 测试
- 新增 `test_error_phrasing.py` 6 例：5xx 永不空 message、no_match/内部错误
  专属话术、意图 handler 裸 assert 防回潮钉桩。真栈验收矩阵三段全过
  （旧集成 500 → 指引话术；新集成 no_match → 中文话术；「打开办公室射灯」
  原输入 → 成功播报）。157 全绿。


## [1.0.5] - 2026-09-11

### 修复
- **真执行全量 404（用户实机「抱歉，没找到这个设备」根因）**：HA REST 客户端在
  默认 base（`http://supervisor/core/api`，本身即 API 根）之后又逐点拼
  `/api/...`，七个端点全部请求成 `/api/api/...` → HA 一律返回
  `404 Not Found` → 话术层把 "not found" 误译成「抱歉，没找到这个设备」。
  受影响不止意图执行：状态页设备/区域列表、实体注册表、config 读取、事件
  旁路在真机上全部失效。新增 `_url()` 唯一拼接真源（LoRa 网关加载项
  `ha_api` 同款定式）：base 尾部 `/api` 剥除、由路径统一带上——supervisor
  代理形态、LAN 直连形态（含/不含 `/api`、带尾斜杠）四种写法收敛为恰好
  一个 `/api`，旧 `HUIJIAN_HA_API` 直连配置无需变更。
- **状态灯假绿**：可达判据 `status < 500` 会把上述 404 点亮成「已连通」，
  整版本失明。states 探面判据收紧为 `200/401/403`——404（路径/部署错误）
  判不可达，401/403（URL 正确而鉴权失败）仍算可达。

### 测试
- 新增 URL 拼接回归钉桩 8 例：默认 supervisor base 形态全端点 URL 清单
  （新增端点必须同步清单）、三种 base 写法归一、legacy 回落形态、
  404 不点亮可达/401 点亮。发布前另以真实 HA 双形态（LAN 直连 +
  supervisor 代理前缀仿真）跑通意图开灯→实体状态翻转→关灯全链路真执行。
- 杂务：移除 v1.0.0 误入仓的内部文档与 CI 转码调试日志残留。


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
