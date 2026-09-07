# 慧尖语音插件仓库 · 工作纪律

三端拓扑：本仓（VO 加载项+集成）｜小程序 `E:\AI\ha-yy\weichat-huijian-hz`｜
固件 `E:\AI\0513gujian`。姊妹商店仓：网关
`E:\AI\huijian-gateway-plugin`（ha-gateway-plugin）。

## 铁律
- **禁止自动 commit/push**——仅用户明确指令时执行；推送必须**双推**
  （origin=GitHub + gitee=Gitee）。**Gitee 镜像是商店容灾源**：国内客户
  GitHub 被干扰时全靠它看新版本，漏推 Gitee = 一部分客户收不到更新。
- **改代码必回归**：`cd huijian_voice && python -m pytest tests -q`
  （2026-09-11 基线：203 全绿，其中 klar 一级 NLU 35 项）。
- **内部文档禁发**（会话纪要、内部工单文案等不随代码外发）。

## 版本一致性（test_release_consistency 钉死，五源同版本）
config.yaml / core/const.py 兜底 / www/index.html CURRENT_VERSION+3×?v= /
www/version.json（addon+integration 双键）/ 集成 manifest.json / CHANGELOG。
升版=六处齐改，一处漏即测试红。

## 结构速记
- `huijian_voice/core/`：级联 pipeline（字面表>klar>TextCNN>查询族>LLM）、
  executor（慧尖意图话术 + klar grounded 直调服务映射）、ha_client
  （intent/handle + services 双通道，永不抛全折叠）。
- `huijian_voice/nlu_data/` 为 TextCNN 资产随镜像；根 `nlu/` 目录 
  **gitignored**（klar-ha-nlu 上游参考源码，grep 工具默认跳过，查它用 
  Select-String；引擎二进制由 boot.sh 从 GitHub release 分发+SHA-256 核验）。
- 运行期零 GitHub 依赖是设计底线：镜像走阿里云 ACR，klar 拉取失败仅降级
  TextCNN（fail-open）。

## 审查
代码审查按 dsh-review-loop 流程；除非用户点名，不直接调 review 工具。
