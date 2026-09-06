"""NLU 级联（v4.1 §2-E 结构）：
  corrector  ASR 热词纠错（58 条基础表 + 用户扩展）
  fast_path  T0 正则级联（移植自 061701 树 840 行 v1.5 修复版，三缺陷已含修复）
  textcnn    T1 分类器（15 类含 OOS，按类阈值，字符级无分词，MAX_LEN=30）
  scenes     语音场景触发缓存（HassListVoiceScenes 60s + 未命中强刷）
  query      查询族（"客厅多少度"本地读回，M1 定案）
宁漏判不误执行：任何一步置信不足都返回 None 落到下一层。
"""
