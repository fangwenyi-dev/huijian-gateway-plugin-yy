# 慧尖加载项仓库（huijian-gateway-plugin-yy）

**慧尖HA语音插件（huijian_voice）** 的 Home Assistant 加载项商店仓库：
纯局域网语音链路服务器——本地 STT/TTS、Klar+TextCNN 双引擎理解级联、
HA 意图执行；零必填配置，卫星固件/小程序连上即用。

## 添加仓库

HA 中 **设置 → 加载项 → 加载项商店 → ⋮ → 仓库**，添加以下**任一** URL：

| 网络环境 | 仓库 URL |
|---|---|
| 默认（GitHub） | `https://github.com/fangwenyi-dev/huijian-gateway-plugin-yy` |
| GitHub 拉取失败/超时（Gitee 镜像，逐提交同步） | `https://gitee.com/fangwenyi-dev/huijian-gateway-plugin-yy` |

> **二选一，勿同时添加**（同 slug 会在商店重复出现）。
> Supervisor 日志若刷 `supervisor.store.git … unexpected eof while reading`
> 或 `Could not reload repository … StoreGitError`，是 GitHub 被网络侧干扰
> （SNI 重置/DNS 污染），换 Gitee 源即可。**已安装的加载项不受影响**：
> 镜像走自有阿里云 ACR 国内直连，运行期纯局域网，两者都不依赖 GitHub。

## 内容

- `huijian_voice/` — 慧尖HA语音插件（加载项 + huijian_ai 集成自动落盘）
- 说明与排障：[huijian_voice/DOCS.md](huijian_voice/DOCS.md) ｜ [变更日志](CHANGELOG.md)
- 姊妹仓：慧尖 LoRa 网关加载项 `https://github.com/fangwenyi-dev/ha-gateway-plugin`
  （Gitee 镜像 `https://gitee.com/fangwenyi-dev/ha-gateway-plugin`）
