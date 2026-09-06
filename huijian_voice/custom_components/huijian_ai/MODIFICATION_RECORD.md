# HA集成代码修改记录

## 修改时间
2026-05-03

## 修改文件
`e:\AI\0418huijianjicheng\jicheng\custom_components\huijian_ai\intent_voice_scene.py`

## 修改原因
LLM在创建语音场景时，错误地将窗户控制放在了`TurnDeviceOn`/`TurnDeviceOff`的action中，并使用错误的`domains: ["cover"]`。

## 修改内容

### 1. 添加窗户关键词检测方法

在类中添加 `WINDOW_KEYWORDS` 常量和 `_is_window_device` 方法：

```python
WINDOW_KEYWORDS = ["窗", "窗户", "平推窗", "平开窗", "推拉窗", "天窗", "飘窗", "推拉门", "内开内倒窗"]

def _is_window_device(self, params: dict[str, Any]) -> tuple[bool, str]:
    """检测是否为窗户设备.

    Returns:
        tuple: (is_window, action) - 是否是窗户设备, 以及窗户动作(open/close)
    """
    targets = params.get("target", [])
    for target in targets:
        devices = target.get("devices", [])
        for device in devices:
            name = device.get("name", "")
            domains = device.get("domains", [])
            if any(kw in name for kw in self.WINDOW_KEYWORDS):
                action = "open"
                if "关" in name or "close" in name.lower():
                    action = "close"
                return True, action
    return False, ""
```

### 2. 修改 _execute_intent 方法

在处理 `TurnDeviceOn`/`TurnDeviceOff` 时，先检测是否为窗户设备，如果是则自动路由到 `_execute_control_window`：

```python
if intent_name in ["TurnDeviceOn", "TurnDeviceOff"]:
    is_window, window_action = self._is_window_device(params)
    if is_window:
        _LOGGER.info(f"检测到窗户设备，自动路由到_control_window, action={window_action}")
        window_params = {"target": params.get("target", []), "action": window_action}
        return await self._execute_control_window(hass, intent_obj, window_params)
    return await self._execute_turn_device(hass, intent_obj, params, "turn_on" if intent_name == "TurnDeviceOn" else "turn_off")
```

### 3. 扩展 ControlWindow 的intent名称兼容

```python
elif intent_name in ["ControlWindow", "WindowControl", "OpenWindow", "CloseWindow"]:
    return await self._execute_control_window(hass, intent_obj, params)
```

## 映射逻辑

| LLM发送的intent | LLM发送的domain | 实际处理 |
|-----------------|-----------------|---------|
| TurnDeviceOn | cover | → 路由到ControlWindow |
| TurnDeviceOff | cover | → 路由到ControlWindow |
| ControlWindow | button | → 正常处理 |
| WindowControl | button | → 正常处理 |

## 回滚方案

如需回滚，删除以下修改：

1. 删除 `WINDOW_KEYWORDS` 常量
2. 删除 `_is_window_device` 方法
3. 恢复 `_execute_intent` 方法为原来的逻辑

### 回滚后的代码应该是：

```python
async def _execute_intent(self, intent_obj: intent.Intent, intent_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Execute an intent action.

    This internally calls the appropriate HA services based on intent type.
    """
    hass = intent_obj.hass

    if intent_name == "TurnDeviceOn":
        return await self._execute_turn_device(hass, intent_obj, params, "turn_on")
    elif intent_name == "TurnDeviceOff":
        return await self._execute_turn_device(hass, intent_obj, params, "turn_off")
    elif intent_name == "AdjustDeviceAttribute":
        return await self._execute_adjust_attribute(hass, intent_obj, params)
    elif intent_name == "SetDeviceMode":
        return await self._execute_set_mode(hass, intent_obj, params)
    elif intent_name == "ControlWindow":
        return await self._execute_control_window(hass, intent_obj, params)
    else:
        raise ValueError(f"不支持的intent类型: {intent_name}")
```

## 测试验证

部署后，测试以下场景：

1. LLM发送 `TurnDeviceOn` + `domains: ["cover"]` + `name: "平推窗"` → 应正确控制窗户
2. LLM发送 `TurnDeviceOn` + `domains: ["cover"]` + `name: "筒灯"` → 应正常控制灯
3. LLM发送 `ControlWindow` + `domains: ["button"]` + `name: "平推窗"` → 应正常控制窗户

---

# HA集成代码修改记录 v2（当前生效）

## 修改时间
2026-05-05（v2.1修复：排除"窗帘"误判，增加空值保护）

## 修改文件
`e:\AI\0418huijianjicheng\jicheng\custom_components\huijian_ai\intent_voice_scene.py`

## 修改目标
解决LLM错误使用`cover`/`window` domain控制窗户，以及错误将窗户操作放在`TurnDeviceOn`/`TurnDeviceOff`中的问题。

## 方案选择理由
- **方案A（HA代码标准化）**：在_execute_intent入口做参数检测和domain映射
- **优势**：不改动LLM、不改训练数据，在HA侧做兼容处理，记录日志便于监控
- **劣势**：需少量代码维护

## 修改内容
在 `_execute_intent` 方法入口（intent分发前）添加参数标准化逻辑：

### 1. 窗户设备检测
遍历 `params.target[].devices[].name`，匹配窗户关键词（排除"窗帘"误判）：
```python
WINDOW_KEYWORDS = ["窗户", "平推窗", "平开窗", "推拉窗", "天窗", "飘窗", "推拉门", "内开内倒窗"]
WINDOW_EXCLUDE_KEYWORDS = ["窗帘"]
```

### 2. domain自动映射
如果检测到窗户设备且 `domains` 包含 `cover` 或 `window`，自动改为 `["button"]`（带空值保护）：
```python
domains = device.get("domains")
if isinstance(domains, list) and any(d in ["cover", "window"] for d in domains):
    _LOGGER.info(f"窗户domain标准化: domains={domains} -> [\"button\"]")
    device["domains"] = ["button"]
```

### 3. intent自动路由
如果intent是 `TurnDeviceOn`/`TurnDeviceOff` 且检测到窗户设备，自动路由到 `_execute_control_window`：
```python
if intent_name in ("TurnDeviceOn", "TurnDeviceOff") and is_window_device:
    params["action"] = "open" if intent_name == "TurnDeviceOn" else "close"
    return await self._execute_control_window(hass, intent_obj, params)
```

### 4. 日志记录
所有标准化操作均记录INFO级别日志，便于追踪LLM的行为模式。

### 5. v2.1修复内容
- **排除"窗帘"误判**：`WINDOW_EXCLUDE_KEYWORDS = ["窗帘"]`，防止"窗"关键词匹配到"窗帘"
- **空值保护**：`domains` 做 `isinstance(domains, list)` 检查，防止 `domains: null` 时崩溃
- **类型保护**：`targets` 和 `devices` 增加 `isinstance` 类型检查
- **补全窗户类型映射**：`WINDOW_NAME_MAPPING` 增加平开窗、推拉窗、天窗、飘窗、推拉门、内开内倒窗，使所有窗型都能被正确识别（之前只有"平推窗"能成功路由，其他窗型路由后会因找不到名称而报错）
- **同步更新 intent_window_control.py**：原始 ControlWindow 处理器也有独立的 WINDOW_NAME_MAPPING，同步更新以支持所有窗型

## 修改文件清单

| 文件 | 修改内容 |
|------|---------|
| `intent_voice_scene.py` | `_execute_intent` 添加参数标准化（L267-L301），`_execute_control_window` 更新WINDOW_NAME_MAPPING（L502-L513） |
| `intent_window_control.py` | 更新 WINDOW_NAME_MAPPING（L16-L28），支持所有窗型 |

## 映射逻辑

| 场景 | LLM发送内容 | 标准化后 |
|------|------------|---------|
| 场景1：domain错误 | `TurnDeviceOn` + `domains:["cover"]` + `name:"平推窗"` | routes to ControlWindow, domains→["button"] |
| 场景2：intent错误 | `TurnDeviceOn` + `domains:["button"]` + `name:"平推窗"` | routes to ControlWindow + action:open |
| 场景3：完全正确 | `ControlWindow` + `domains:["button"]` + `name:"平推窗"` | 正常处理，无需标准化 |
| 场景4：非窗户设备 | `TurnDeviceOn` + `domains:["light"]` + `name:"筒灯"` | 不匹配，正常走TurnDeviceOn逻辑 |
| 场景5：窗帘排除 | `TurnDeviceOn` + `domains:["cover"]` + `name:"窗帘"` | 被排除，正常走TurnDeviceOn，cover自动开合 |

## 回滚方案
删除 `_execute_intent` 方法中从 `# ===== 参数标准化` 到 `# ===== 参数标准化结束 =====` 之间的代码块。

## 日志排查
部署后可通过检查HA日志中以下关键信息定位问题：
- `"窗户domain标准化: device=..."` - 确认LLM发送了错误domain
- `"窗户intent标准化: TurnDeviceOn -> ControlWindow"` - 确认intent路由成功

## 测试验证

1. **domain错误测试**：LLM发送 `TurnDeviceOn` + `domains:["cover"]` + `name:"平推窗"` → 日志记录domain标准化，路由到ControlWindow，成功控制窗户
2. **intent错误测试**：LLM发送 `TurnDeviceOn` + `name:"平推窗"`（无正确domain）→ 检测到窗户关键词，路由到ControlWindow
3. **非窗户不受影响**：LLM发送 `TurnDeviceOn` + `name:"筒灯"` → 不匹配窗户关键词，正常控制灯光
4. **正常窗户控制**：LLM发送 `ControlWindow` + `domains:["button"]` + `name:"平推窗"` → 正常处理
5. **窗帘不被误判**：LLM发送 `TurnDeviceOn` + `domains:["cover"]` + `name:"卧室窗帘"` → 不匹配窗户条件，正常走TurnDeviceOn，`cover` domain自动用 `open_cover`/`close_cover` 服务

---

# huijian_voice 加载项发行版补丁

## 修改时间
2026-09-08

## 修改文件
`huijian/mcp_transport.py`（`async_setup_entry`）

## 修改原因（盘点 §6-D1）
纯局域网形态（慧尖语音助手加载项，模式 B）不存在云端 MCP 端点；
原逻辑 `endpoint 为空 → raise EntryAuthFailedError` 会让 assist 条目
setup 永久失败（离线死路）。

## 修改内容
端点为空时记录 info 日志并 `return True`（跳过 MCP 传输挂载）；
stt/tts/llm 三传输与平台装载路径不受影响。代码内注释标记
`HUIJIAN-PATCH D1`，上游合并时按此锚点重放。


## 版本对齐（交付标准，非功能修改）

`manifest.json.version` 由上游 1.1.0 改为 **1.0.0**：与加载项 config.yaml、
www/version.json 同链（网关加载项 `window_controller_gateway` 的
「四源版本一致」CI 硬门禁同款范式）。boot.sh 落盘比对读该字段，逻辑不受影响。
