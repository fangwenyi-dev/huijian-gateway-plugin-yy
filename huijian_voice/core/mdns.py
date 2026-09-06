"""mDNS 服务广播：_huijian-voice._tcp.local（v4 §3：配网发现入口）。

固件不广播 mDNS（源码实证），本服务广播加载项自身；小程序/配网向导据此发现网关
地址与三通道 URL。零conf 发布失败不致命（静态 IP 直连仍可用），只告警。
"""
from __future__ import annotations

import logging
import socket

from . import const

logger = logging.getLogger("huijian.mdns")


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 53))     # 无发包，仅取路由出口 IP
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class Publisher:
    def __init__(self, port: int = const.WS_PORT, props: dict | None = None, version: str = "1.0.0"):
        self.port = port
        self.props = props or {}
        self.version = version
        self._zco = None
        self._info = None

    def start(self) -> None:
        try:
            from zeroconf import ServiceInfo, Zeroconf
            ip = local_ip()
            self._info = ServiceInfo(
                "_huijian-voice._tcp.local.",
                f"huijian-voice._huijian-voice._tcp.local.",
                addresses=[socket.inet_aton(ip)],
                port=self.port,
                properties={k: str(v) for k, v in self.props.items()},
                server="huijian-voice.local.",
            )
            self._zco = Zeroconf()
            self._zco.register_service(self._info)
            logger.warning("[mDNS] 广播 %s:%d @ %s", "_huijian-voice._tcp.local.", self.port, ip)
        except Exception as e:
            logger.warning("[mDNS] 广播失败（不影响静态接入）: %r", e)  # %r：v1.0.0 实机异常 str 为空，必须带类型显形
            if self._zco is not None:
                try:   # register 失败也必须拆 Zeroconf（UDP socket/引擎线程），否则泄漏
                    self._zco.close()
                except Exception:
                    pass
            self._zco = None

    def update_props(self, props: dict) -> None:
        self.props.update(props)
        if self._zco and self._info:
            try:
                self._info.properties = {k: str(v) for k, v in self.props.items()}
                self._zco.update_service(self._info)
            except Exception:
                pass

    def close(self) -> None:
        try:
            if self._zco and self._info:
                self._zco.unregister_service(self._info)
                self._zco.close()
        except Exception:
            pass
