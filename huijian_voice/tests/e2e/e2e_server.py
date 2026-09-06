# -*- coding: utf-8 -*-
"""E2E 服务端（本地真栈 / CI 容器内共用）：起真 core 服务 + 真模型 + 不可达 HA（验降级）。

- 容器形态：/data/options.json 存在即 Supervisor 布局，不改任何 env（const 默认路径
  对齐镜像）；模型经 ModelStore 自动下载（options.json 里开）。
- 本地形态：env 重定向到 <huijian_voice>/_e2e_data/；若研究目录存在 _hjmodels/
  （dev 机预置模型）则指向它并关自动下载；HA 端点故意不可达，验证全链降级不崩。
- Windows：py3.13 下 ctypes 找 opus.dll 只认 os.add_dll_directory，且 WSL interop
  不透传自定义 env → 自动探测仓旁 _winlibs/，HUIJIAN_OPUS_DLL_DIR 仅作显式覆盖。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # huijian_voice/
sys.path.insert(0, ROOT)

_container = os.path.exists("/data/options.json")

if not _container and not os.environ.get("HUIJIAN_DATA"):
    data = os.path.join(ROOT, "_e2e_data")
    os.environ.update({
        "HUIJIAN_DATA": data,
        "HUIJIAN_SETTINGS": os.path.join(data, "settings.json"),
        "HUIJIAN_NGINX_HTML": os.path.join(data, "www"),
        "HUIJIAN_STATUS_FILE": os.path.join(data, "www", "status.json"),
        "HUIJIAN_MODELS_STATUS_FILE": os.path.join(data, "www", "models_status.json"),
        "HUIJIAN_HA_TOKEN": "dummy-token-e2e",
        "HUIJIAN_HA_API": "http://127.0.0.1:9/api",   # 故意不可达：验证降级不崩
        "HUIJIAN_OPT_LOG_LEVEL": os.environ.get("HUIJIAN_OPT_LOG_LEVEL", "info"),
    })
    if not os.environ.get("HUIJIAN_MODELS_DIR"):
        cand = os.path.join(os.path.dirname(ROOT), "_hjmodels")
        if os.path.isdir(cand):
            os.environ["HUIJIAN_MODELS_DIR"] = cand
            os.environ.setdefault("HUIJIAN_OPT_MODEL_AUTO_DOWNLOAD", "false")

if os.name == "nt":
    _d = os.environ.get("HUIJIAN_OPUS_DLL_DIR") or os.path.join(os.path.dirname(ROOT), "_winlibs")
    if os.path.isdir(_d):
        os.environ["PATH"] = _d + os.pathsep + os.environ["PATH"]
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(_d)

sys.argv = ["core"]
from core.main import main as _main   # noqa: E402  （env 必须先于 const 装载）

_main()
