# 慧尖语音集成源码盘点（huijian_ai v1.1.0）— 2026-09-08

> 源码位置：`huijian-gateway-plugin-yy/yyjicheng/`（git 仓库，origin=`github.com/fangwenyi-dev/huijian-yy`，
> 最后提交 2026-05-25「同步 jicheng/」；开发母本 `e:\AI\0418huijianjicheng\jicheng`）。
> 性质：**ESPHome 官方 HA 集成的慧尖 fork** + 自研 `huijian/` 子包（小智协议 WS 传输 + 配网 HTTP）+
> 自研意图面（设备控制/语音场景/传感器自动化）。63 个 py 文件。
> 本文 = 语音落地方案的"事实契约"，所有结论带文件:行号。

---

## 0. 安全告警（先说最急的）

1. **`.git/config` 的 origin URL 内嵌 GitHub PAT 明文**（`ghp_Lir…`，仓库 fangwenyi-dev/huijian-yy）。
   → 立即到 GitHub → Settings → Developer settings 吊销重发；remote 清成干净 URL
   （慧尖网关插件 v1.6.3 同款定案：禁内嵌 token）。
2. `api.py` 9 个 HTTP 视图全部 `requires_auth=False`：语音场景 GET/DELETE/PUT、自动化 CRUD、
   测试触发、日志、管理页（api.py:97/132/178/189/248/280/517/537/594）——**HA 地址可达者即可
   匿名删改场景并触发执行**。公网/Nabu Casa 暴露时是实洞，整改见 §6-D2。
3. `setup/qrcode` 不带 `Authorization` 时"仅凭 uuid"即写入 setup_data（huijian/http.py:71-80，
   注释自认"backward compatibility=可绕"）；`remove`/`speakname` 的签名=`hash(path,params,MAC,salt)`
   ——**密钥是 MAC 地址**（设备蓝牙可读出，网络抓包可复算）。弱机密，LAN MVP 可接受、公网须换真凭据。

## 1. 两种条目形态（config_flow 的核心分叉）

| | `config_type="device"`（设备条目） | `config_type="assist"`（服务条目） |
|---|---|---|
| unique_id | 设备 MAC | `haid`（HA 实例 UUID，config_flow.py:271/:276） |
| data 键 | host/port(6053)/password/noise_psk/speak_id/speak_name/**mcp_endpoint** | speak_id/device_name/**mcp_endpoint/llm_endpoint/stt_endpoint/tts_endpoint**（:254-263） |
| setup 行为 | 完整 ESPHome 栈：APIClient 连设备、manager、卫星实体、**强制 mcp_transport**（__init__.py:80-105） | 按端点键存在与否转发 CONVERSATION/STT/TTS 平台 + **强制 mcp_transport**（:60-71） |
| 谁创建 | 小程序扫码后 POST setup_data 回填（或 reconfigure 携 mac） | 小程序扫码回填（HouzzKit 时代=云函数注入云端 endpoint） |

**鉴权设计**：无独立 token 键——服务器地址与鉴权全编码在 endpoint URL 的 query 里
（`ws://…/xiaozhi/v1/?token=*** config_flow.py:253-263）；401→清 endpoint+reauth，
close 1008（被顶号）→禁重连（ws_transport.py:210-215/:269-272）。
options 步骤（:1315-1355）：`allow_service_calls`、`subscribe_logs`、`debounce_minutes`、
**`tts_entity_id`/`stt_entity_id`**（默认 `tts.huijian_speech`/`stt.huijian_asr`，可换成任意 HA 实体）。

## 2. 两种音频模式（本集成最重要的架构事实）

```
模式 A（小智全链路，现网默认）：
  固件 ←(小智WS, opus16k/60ms上行, 下行服务器协商)→ 小智服务器(STT/NLU/LLM/TTS)
       ←(MCP-over-WS，本集成反向外拨 mcp_endpoint，伪装 mcp-server v2.2.0)→ HA 执行意图
  音频完全不经过 HA；本集成只是工具端。

模式 B（HA 原生卫星，代码已完整落地、固件侧缺件）：
  固件 ←(ESPHome API 6053 protobuf / UDP)→ assist_satellite.py → HA assist_pipeline
       → stt.huijian_asr ─┐
       → conversation.huijian_agent ─┤ 三条 WS 客户端外拨（小智 hello/listen/tts 协议子集）
       → tts.huijian_speech ─┘       → "任意会说小智协议的服务器"（=我们要做的加载项）
```

### 2.1 卫星协议（assist_satellite.py，877 行，上游同构）
- **能力门**：固件 `device_info.voice_assistant_feature_flags_compat` 非零才建卫星（:108-111；
  断连→卫星实体消失 :manager.py:712-717；能力声明用到的 flags：`API_AUDIO/SPEAKER/TIMERS/ANNOUNCE/START_CONVERSATION`，
  :235/:255/:267/:282）。**慧尖现有 xiaozhi 固件未实现该组件 → 模式 B 的固件缺口在这里**（改法见 v4 §5）。
- mic 上行：`API_AUDIO`→`cli.subscribe_voice_assistant` 裸 **PCM s16le 16kHz 单声道**（非 opus，HA 不解码）；
  无 flag 则随机口 UDP（:719-736，端口回传固件 :481-491）。
- TTS 下行（SPEAKER flag）：强制 **wav/16k/mono/16bit**，校验后按 **512 样本/块** `send_voice_assistant_audio`
  流出，节奏=播放时长×0.9，前后 `TTS_STREAM_START/END` 事件（:634-696）。无 SPEAKER→media_player
  ANNOUNCEMENT + `supported_formats` URL 拉流（经 `/api/esphome/ffmpeg_proxy/...` 转码）。
- 事件回灌：STT_END 文本 / INTENT_PROGRESS（早流式）/ INTENT_END(continue_conversation **连续对话**) /
  WAKE_WORD_END；`send_voice_assistant_announcement_await_response`（宣告可起会话）（:301-461）。
- 多管线：select 实体 pipeline/_2、vad_sensitivity、wake_word/_2（select.py:49-57）；
  自定义唤醒词 tflite 经 `/api/esphome/wake_words` HTTP 供固件拉取（:805-877）。

### 2.2 三条 WS 传输（huijian/ws_transport.py 基类 + stt/tts/llm；**小智协议子集**）
- 握手 `send_hello`（:282-295）＝`{"type":"hello","version":1,"transport":"websocket","audio_params":{"format":"opus","sample_rate":16000,"channels":1,"frame_duration":60}}`。
- **STT**（stt.py:96-113）：`hello → {"type":"listen","state":"start"} → 二进制 opus 帧（入参 WAV/PCM 先 wav_to_opus，audio.py:127-154）→ {"type":"listen","state":"stop"} → 取 {"type":"stt"|"tts","text":…}`（整句一次性，非流式上行但服务器可流式吃帧）。
- **TTS**（tts.py:63-107）：发 `{"type":"tts","state":"detect","text":…}`（**无 hello**）→ 收二进制 **opus 帧（解码硬编码 16k/mono/60ms）**，直到 `{"state":"stop"}`（tts_transport.py:43-46）→ s16le→ffmpeg 转 mp3/wav 交回 HA。**结论：加载项 TTS 通道必须输出 16kHz opus**（Kokoro 22.05k 需重采样）。
- **LLM**（conversation.py:88-100 + llm_transport.py:42-51）：发 `{"type":"listen","state":"detect","text":…}` → 聚合 **`type=text`、`state=sentence_end`** 的句子直到 end → 喂 ChatLog delta 流。**精确字段现网抓包钉版（v4 M0 任务）**。
- 生命周期：懒连+`ensure_connected`(≤15s)、重连退避 3→60s、空闲 **180s 自关**、55s ping（ws_transport.py:34-350）。
- **MCP**（mcp_transport.py）：本集成=**MCP 服务器**反向外拨；工具表来自 `llm.async_get_api(hass, LLM_API_ASSIST)`（HA 全意图）；版本硬标 `"2.2.0"` 骗过小智 mcp-server 识别（:101）。模式 B 不需要它，但**setup 强制非空 endpoint 否则抛 EntryAuthFailedError**（:37-41）→ 离线死路，改造点 §6-D1。

## 3. 意图面（intent.py:22-39 注册 14 个，进 HA intent registry → `/api/intent/handle` 可直接调）

| 组 | intent | slots 摘要 | 语义要点 |
|---|---|---|---|
| 设备 | TurnDeviceOn / TurnDeviceOff | target:[{area, devices:[{domains,name}]}] | 14 domain 白名单（README：light/cover/climate/switch/lock/valve/fan/humidifier/media_player/alarm/vacuum/water_heater/sensor/binary_sensor）；**三级匹配**（严格→宽松→device_class 兜底）+ 域名别名（window/curtain/blind/shutter→cover，plug/outlet→switch） |
| 调节 | AdjustDeviceAttribute | attribute ∈ brightness/color/temperature/fan_speed/humidity/position | 灯光亮度色温色、空调温度、风扇风速、加湿器湿度、**窗帘&窗户 position** |
| 模式 | SetDeviceMode | mode | 空调制冷/制热/除湿/送风/自动、加湿器静音等 |
| 窗户 | ControlWindow | action ∈ open/close/pause/**a(内倒)**、target | button 域窗控特殊协议；**新语义（重要）**："打开窗户"→LLM 发 TurnDeviceOn，集成自动转换 ControlWindow（intent_voice_scene.py:196-205 + intent_device_shared.WINDOW_KEYWORDS 路由 v2.1） |
| 上下文 | huijianGetLiveContext | 无 | 实时设备/区域状态 JSON（"灯开着吗"、"创建场景前先查设备"） |
| 场景 | HassCreate/Trigger/Delete/List VoiceScene | trigger_phrase, actions:[{intent,name,params/parameters}] | 存储 `.storage/huijian_voice_scenes` v1：`{version,scenes:{id:…},trigger_index:{phrase:id}}`；创建时 **_auto_supplement_windows**（LLM 漏窗自动补 ControlWindow，intent_voice_scene.py:210-260）；README 声称的 HassUpdateVoiceScene **代码未实现**（D4） |
| 自动化 | HassCreate/Delete/List/Update Automation | trigger:{entity_id,above/below}, actions 同场景格式 | 存储 `.storage/huijian_automations` v1；**传感器 entity_id 自动修正**（22 device_class 中英文关键词，_resolve_entity_id:23+）；manager 跟踪状态+去抖(debounce_minutes)+创建即查（:620-626）；日志 automation-logs |
| 其他 | HassBroadcast / GetDateTime / HassCancelAllTimers / HassClimateSetTemperature | — | README 列 15；**注册表实有 14**（Broadcast/DateTime/CancelTimers 未见注册——走 HA 原生/上游 intent？M0 抓 `/api/intent/handle` 实测核对） |

执行链：intent handler → `_execute_intent` 分派 →（窗户路由）→ HA services + 返回 JSON
`{success, …}`；管理 REST（`/api/huijian-ai/voice-scenes|automations|test-*|automation-logs|manage-page`）+
`/huijian-ai/manage` 内嵌 HTML 管理页（场景/自动化 CRUD+一键测试+触发日志）。

### 3.1 执行语义细节（二轮意图深读补充，落地方须逐条对齐）
- **对外入口**：注册在 `async_setup`（全局、非条目级），HA core `intent.async_handle` **原样返回 handler 的裸 dict**——
  加载项执行器走 `POST /api/intent/handle`（长时令牌）即拿到 `{success, control_targets/message/error…}` 结构化结果，
  **无需解析 IntentResponse 文本**；返回 dict 供上层二次组织话术（现网由小智 LLM 做，v4 由加载项做）。
- **中文数字归一化**（intent_window_const.py:45-92）："五号窗"→"5号窗"，turn/adjust/set_mode/window 四入口统一调用。
- **锁语义反转**（intent_turn.py:212-231）：`turn_on=lock_lock`、`turn_off=unlock`——话术生成端必须按此翻译，
  否则"打开门锁"播报会拧着。
- 泛称窗户（"把窗户都打开"）→ 全窗按钮逐个 press、**0.5s 间隔**（防总线并发）；climate turn_on 按
  `heat_cool>heat>cool>auto>fan_only>dry` 选首个支持模式。
- 场景触发执行：动作白名单仅 6 intent（Turn*/Adjust/SetMode/ControlWindow/VoiceScene 系，WindowControl 归一 ControlWindow），
  **每动作 30s 超时**，全成功才 `success:true`，逐动作 `executed_actions[]` 带 detail。
- 自动化引擎只吃**数值传感器**：状态变更后 `float()` 失败即跳过（intent_automation.py:408-411）——
  **门窗磁/人体等 binary_sensor 触发"温度大于29度开窗"类语句的 on/off 场景当前不工作**（产品缺口，见 D18）；
  above/below 为严格 `>`/`<`；去抖 `debounce_minutes` 默认 5 分钟（取 DOMAIN 首条目 options）；创建即查当前值。
- 存储 JSON 字段级（供加载项缓存对账）：
  `.storage/huijian_voice_scenes`=`{version:1, scenes:{<id>:{scene_id:"voice_scene_<时间戳>", trigger_phrase, actions:[{name/intent, parameters/params:{target:[{area,devices:[{domains,name}]}], action?}}], created_at}}, trigger_index:{phrase:id}}`；
  `.storage/huijian_automations`=`{version:1, automations:{<id>:{automation_id, trigger:{entity_id, above?, below?}, actions, created_at, last_triggered, updated_at?}}}`。
  trigger_phrase **全局唯一**；update_scene 仅 REST PUT 路径使用（无 intent 化——D4 的另一半）。

## 4. 慧尖特有实体与播报通道
- `stt.huijian_asr`、`tts.huijian_speech`、`conversation.huijian_agent`（三个"服务实体"，挂管线用）。
- **`text.play_voice_text` 特判**（text.py:31-58/:81-95）：HA 侧用 **edge-tts（微软云免费，需互联网！）**
  合成 MP3 → 写 `config/www/huijian_tts/`（留 10 个）→ media_player `play_media(announce=True)`。
  离线形态应改为优先走 `tts.huijian_speech`（=加载项本地 Kokoro），见 §6-D5。
- `execute_command_text` / `ask_and_execute` / `mic_switch` / `wakeup_button`：HA 代码**无特判**——纯固件
  声明的 ESPHome 实体镜像；即"文本模拟语音指令/直接执行命令"通道在固件已具备（对加载项 UI 测试框极有用）。
- `custom_llm_api.py` `HuijianControlAPI`（412 行）：定义完整但**全库无注册引用=死代码**（D6）。
- 连接状态无暴露实体（transport.available 只进日志，D8）。

## 5. 与慧尖网关加载项（window_controller_gateway）的衔接
LoRa 窗在客户 HA 里= **cover 实体（位置）+ button 实体（内倒/模式）**。本集成的窗户语义演进后已与之自洽：
窗帘类走 cover Turn*，慧尖窗控走 ControlWindow(button) 且自动路由/自动补窗。`split_actions_by_device`
（intent_device_shared.py）负责一语多设备的动作拆分。**语音→慧尖窗（含内倒/百分比/暂停）全链路现成**。

## 6. 缺陷/缺口清单（编号供 v4 方案引用）

| # | 问题 | 位置 | 处置建议 |
|---|---|---|---|
| D1 | 两类条目 setup **强制 mcp_endpoint 非空** → 纯 LAN 无法配出 | mcp_transport.py:37-41；__init__.py:69/105 | 端点为空时跳过挂载（几行门禁）；v4 集成改造点① |
| D2 | 9 个管理/场景/自动化端点 `requires_auth=False` | api.py 各处 | 改 True 或加 LAN 令牌；前端管理页带 token；**上生产前必修** |
| D3 | assist 条目只能靠扫码回填创建，无手动/发现路径；manifest `zeroconf:[]` 禁用了设备发现（handler 还在） | config_flow.py:149-153/:456 | 加载项广播 `_huijian-voice._tcp` + 集成加 zeroconf→assist 自动填充（v4 §6 零配置主通道） |
| D4 | README 声称 HassUpdateVoiceScene，代码无 | intent.py vs README | 补 handler 或改文档 |
| D5 | play_voice_text 走 edge-tts（断网即哑；www 目录堆积） | text.py:31-58 | 改优先 `tts.huijian_speech` 实体（本地） |
| D6 | custom_llm_api.py 死代码 412 行 | — | 清理或转正（其 schema 可作加载项 llm 端点的参考） |
| D7 | `mcp_server` 仅 after_dependencies 但顶层 import | mcp_transport.py:6-7 vs manifest | 改惰性 import（HA 未启用 mcp_server 时不炸 setup） |
| D8 | 语音服务器连接状态无实体/传感器 | ws_transport 仅日志 | 加 binary_sensor（离线排障刚需，客户现场价值高） |
| D9 | README 发现能力宣称与代码不符（mDNS/MQTT 实际不通） | README vs manifest | 文档订正 |
| D10 | MAC 派生签名=弱机密；qrcode 回填可不签名 | http.py:30-51/:71-80 | LAN 可接受；公网形态换 speak_id 派生密钥（v4 风险项） |
| D11 | wav_to_opus 全量入内存转换（长广播高内存） | audio.py | 流式化（P2） |
| D12 | 本仓库 `.git/config` 明文 PAT | origin URL | **立即吊销**+清 URL（§0-1） |
| D13 | 开发母本 `e:\AI\0418huijianjicheng` 与多处 zip 并存，版本源易漂移 | git log「同步 jicheng/×N」 | v4 定唯一主干：本仓库转正，母本归档 |
| D14 | `AdjustDeviceAttribute` 对 number 实体：直接取 delta 值 set_value，**不叠加当前值**（"调大一点"变成"设成一点"） | intent_adjust_attribute.py:579-591 | 改增量语义（读 state 叠加后钳制） |
| D15 | 自动化 `last_triggered` 创建后置 None **永不回写**，管理页恒显示"尚未触发" | intent_automation.py:253 | 触发时回写 store（或 UI 改读内存 cache/日志） |
| D16 | REST PUT 更新自动化绕过 `_resolve_entity_id`/`split_actions` 且**不刷新 manager**（与 intent 路径行为分叉） | api.py:556-590 | PUT 内部改走 intent handler 同一函数链 |
| D17 | ControlWindow 只消费 `targets[0]`，多区域请求其余目标**静默丢弃** | intent_window_control.py:77 | 循环处理全部 targets |
| D18 | 自动化触发仅支持数值传感器：binary_sensor on/off `float()` 失败被跳过——"检测到有人/门被打开就XX"**语音创建会成功但永不触发**（体验事故源） | intent_automation.py:408-411 | on/off→1/0 归一后比较；创建回执如实说明 |
| D19 | `_run_then_background`：服务调用 10s 超时转后台后结果仍会迟到落地，与重试叠加有重复下发窗口 | intent_turn.py:399-424 | 幂等保护或超时语义标注 pending |
| D20 | `custom_llm_api.py` 模块级 import 即 NameError（`er` 仅函数内 import 却在签名注解引用）——佐证死代码 | custom_llm_api.py:85 | 清理或转正时修 |

## 7. 资产结论（对落地方案的含义）
1. **HA 侧大脑已建成 80%**：15 工具意图面、场景/自动化存储与管理页、卫星、三传输、入驻端点、LLM 容错——
   v2/v3 方案里"要新写的 huijian_voice 集成/HA 桥"**不存在必要，改为复用+微改**。
2. **加载项要做的东西收窄且契约明确**：一个"会说小智协议子集的服务器"（stt/tts/llm 三类消息，opus16k/60ms，
   type=text/sentence_end 聚合）+ 本地 Paraformer/Kokoro + fast_path NLU 移植 + `/api/intent/handle` 执行器。
3. **固件是唯一的真缺口**（模式 B 的 voice_assistant 组件）+ 用户已明示"固件也要跟着改"→ v4 含固件改造清单。
4. fast_path/TextCNN/纠错表/数据集（上一轮盘点）与本集成语义**同一工具命名空间**（TurnDeviceOn/ControlWindow/…
   逐字一致）——移植即插可用，映射表已核对。
