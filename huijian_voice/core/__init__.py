"""慧尖语音加载项 core 包。

布局（对齐《语音助手落地方案-v4.1》§3 职责清单）：
  main.py        进程入口（双 aiohttp 应用 + 后台任务）
  ws_server.py   小智协议子集 WS 服务器（stt/tts/llm 三端点）——契约见仓根《小智协议子集-服务器契约.md》
  session.py     三通道会话状态机
  asr.py tts.py   模型封装（本地默认 + 云可配回落）
  nlu/           理解级联（纠错表/T0正则/T1TextCNN/场景缓存/查询族）
  pipeline.py    一回合理解调度
  executor.py    /api/intent/handle 执行 + 真话术生成
  agent.py       可选 LLM 档（OpenAI 兼容 + 15 工具 function-call）
  ha_client.py   HA REST/WS 客户端（supervisor token）
  model_store.py 模型首启下载/校验/手动导入
  admin_api.py   管理 HTTP API（经 nginx /api/local/ 代理，前端零凭据）
  settings.py    /data/settings.json 单一事实源
  const.py       路径/端口/环境常量
  mdns.py        _huijian-voice._tcp 广播（零配置发现主通道）
"""
