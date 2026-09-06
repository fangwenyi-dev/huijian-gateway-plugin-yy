# 慧尖HA语音插件 · HA 加载项仓库

慧尖局域网语音助手服务器：纯内网、零必填配置、本地 STT/TTS/NLU。
ESP 语音卫星设备（小智协议子集）直连本加载项即可对话控制 Home Assistant，
不经任何公网服务器。

**⚠ 这是独立项目**：本仓（慧尖**语音**加载项）与慧尖 LoRa 网关项目
[ha-gateway-plugin](https://github.com/fangwenyi-dev/ha-gateway-plugin) 代码、
发版、远端**完全分离**——仓名仅一字之差（`-yy` 后缀），任何提交/推送前务必核对
`git remote -v`，严禁互推。

姊妹仓 [ha-gateway-plugin](https://github.com/fangwenyi-dev/ha-gateway-plugin)
（LoRa 网关加载项）负责设备接入层，本仓负责语音层；两者独立发版、互不阻塞
（v4.1 定案「同仓双镜像/独立升级」，本仓可独立添加，也可后续并入）。

## 可用插件

| 插件 | 说明 | 形态 |
|---|---|---|
| **慧尖HA语音插件**（huijian_voice） | STT/TTS/NLU 服务器 + Ingress 管理页 + huijian_ai 集成自动落盘 | Debian 容器（amd64 / aarch64） |

## 安装方法

1. HA「设置 → 加载项 → 商店 → ⋮ → 仓库」添加本仓库 URL；
2. 商店安装「慧尖HA语音插件」，启动加载项（首启自动下载模型，约 450MB，
   进度见加载项「文档/信息」下方的管理页 → 模型区）；
3. 重启一次 HA Core（首次安装时落盘的 `huijian_ai` 集成需要，管理页有提示）。

国内加速：`config.yaml` 的 image 指向自有阿里云 ACR 公开仓（v1.0.1 起，单仓
多架构 OCI index 自动匹配 amd64/aarch64；CI `push-acr` job 用 buildx imagetools
把 ghcr 已过 e2e 门禁的双架构仓在 registry 侧合并推送，为 release 硬前置）。
历史：v1.0.0 曾走 ghcr.1ms.run 透传站，因新 tag 边缘缓存冷致首装卡下载而退役。
若 ACR 故障可手动改 image 为 `ghcr.io` 灾备源（DOCS FAQ 有完整换源串）。

### 什么时候需要重启 HA？

- **首次安装**：加载项 boot 阶段把集成复制到 `/config/custom_components`，
  必须重启 HA Core 才生效（普通「重启加载项」不够）；
- **加载项更新且集成版本变化**：同上重启一次；boot 日志与 Ingress 管理页
  状态区会明确提示「需要重启 HA」。

## 设备接入

- 固件/客户端连 `ws://<HA宿主IP>:8000/xiaozhi/v1/{stt|tts|llm}`（配对页可复制
  带 token 的完整端点）；
- mDNS 服务类型 `_huijian-voice._tcp.local`，TXT 携带三端点路径与版本；
- 执行链：语音 → 本加载项 NLU 级联 → `POST /api/intent/handle` → huijian_ai
  14 意图 → 你 HA 里的真实设备。

## 端口一览

| 端口 | 协议 | 暴露范围 | 用途 |
|---|---|---|---|
| 8000 | WS | LAN（token 可选） | 设备三通道（小智协议子集） |
| 8001 | HTTP | 仅回环/Supervisor 网段 | 管理页与 /api（经 HA Ingress） |
| 8002 | HTTP | 仅容器内回环 | 管理 API 真身（不外露） |

## 排障速查

- **商店里找不到卡片**：Supervisor 日志搜 `Can't read ... config.yaml`
  （schema 语法回归的历史形态，v1.7.16 网关事故教训）；本仓有钉桩防线。
- **首句慢**：模型空闲卸载后惰性重载 +~2s（可在配置页关闭 `idle_unload_minutes`）。
- **管理页空白**：从 HA 侧边栏进入（Ingress），不要直接收藏 :8001。

详细文档：[DOCS.md](huijian_voice/DOCS.md)（商店「文档」页同源）·
[限制与诚实清单](huijian_voice/DOCS.md#限制已知边界)

## 开发

- 架构与实证结论：[huijian_voice/README.md](huijian_voice/README.md)
- 测试：`cd huijian_voice && python -m pytest tests -q`（116 项钉桩）
- CI 全链门禁：lint → build(双架构) → **e2e 真镜像三通道** → manifest →
  **push-acr（国内主源，硬前置）** → GitHub/Gitee 双 Release（[.github/workflows/ci.yaml](.github/workflows/ci.yaml)）
- 贡献规则与陷阱清单：[CLAUDE.md](CLAUDE.md)

## 版本与历史

版本号唯一真源 `huijian_voice/config.yaml` 的 `version`；
全部记录见 [CHANGELOG.md](CHANGELOG.md)。发布 = 改版本号推送 main，CI 自动
构建、E2E、双平台发 Release——任何一步红都不出包。

## License

[MIT](LICENSE) © 2026 fangwenyi-dev
