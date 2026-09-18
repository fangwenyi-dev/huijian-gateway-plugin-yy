"""v1.0.93 退下旗的包内旁路（三端契约中段，2026-09-18 用户批准）。

数据流：加载项 LLM end 帧带 end_dialogue:1 → conversation 实体在消费
应答流时按 chat_log.conversation_id 在此记账 → assist_satellite 的
INTENT_END 事件按同一 id 弹出，转成下发固件的 kv ``end_dialogue="1"``。

为什么走旁路而不是 core 透传：HA core 2026.9.2 的
``ConversationResult.as_dict()`` 是**固定三键**（response/conversation_id/
continue_conversation），intent_output 上没有我们的字段可挂——要加键就得改
core，违"集成只用公开契约"的三端铁律。conversation_id 由 core 的 ChatLog
生成、随 as_dict 原样带回 INTENT_END，两侧天然同值（2026.9.2 源码
util.py:44 conversation_result.conversation_id=chat_log.conversation_id
实锤，守卫钉 tests/test_v1093_end_dialogue.py）。

纪律（v2.1.47 孤旗教训）：本旗是**正向专用**——只表达"停"，绝不与
continue_conversation 混用或互相推导；标记丢失=现状（8s 静默宽限兜底），
不存在误杀正常轮的路径。纯进程内 dict，同事件循环读写，无锁无跨进程面。
"""
from __future__ import annotations

from collections import OrderedDict

_MAX = 64  # 有界 FIFO：异常轮（INTENT_END 未达）的残账不得无界积累

_pending: "OrderedDict[str, int]" = OrderedDict()


def mark(conversation_id: object) -> None:
    """登记"该会话轮应答时声明了退出"。空 id 忽略（无从配对，宁可不发）。"""
    if not conversation_id:
        return
    key = str(conversation_id)
    _pending[key] = 1
    _pending.move_to_end(key)
    while len(_pending) > _MAX:
        _pending.popitem(last=False)


def consume(conversation_id: object) -> bool:
    """弹取一次（INTENT_END 消费点专用）。True=本轮带退出旗。"""
    if not conversation_id:
        return False
    return _pending.pop(str(conversation_id), None) is not None


def clear() -> None:
    """测试钩子：清空记账。"""
    _pending.clear()
