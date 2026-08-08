"""
Amazon ASIN 采集系统 v4 - 代理管理模块

TPS 模式（每次请求换 IP）：
- 固定代理地址（帐密认证），如 http://user:pwd@host:port
- 每次 HTTP 请求通过代理自动获取不同出口 IP
"""
import asyncio
import logging
import socket
from urllib.parse import urlparse, urlunparse
from typing import Optional, Dict, Tuple

from common import config

logger = logging.getLogger(__name__)


def _resolve_proxy_hostname(proxy_url: str) -> str:
    """
    预解析代理 URL 中的域名为 IP 地址。

    curl_cffi 内置的 c-ares DNS 解析器不走系统 DNS (getaddrinfo)，
    在 Surge/Clash 等代理工具的增强模式下会解析失败。
    用 Python socket.getaddrinfo() 预解析后，传 IP 给 curl_cffi 即可绕过。
    """
    if not proxy_url:
        return proxy_url
    parsed = urlparse(proxy_url)
    hostname = parsed.hostname
    if not hostname:
        return proxy_url
    # 已经是 IP 地址则跳过
    try:
        socket.inet_aton(hostname)
        return proxy_url
    except socket.error:
        pass
    # 用系统 DNS 解析域名
    try:
        infos = socket.getaddrinfo(hostname, parsed.port or 80, socket.AF_INET)
        if infos:
            resolved_ip = infos[0][4][0]
            # 替换 URL 中的域名为 IP
            new_netloc = parsed.netloc.replace(hostname, resolved_ip)
            resolved = urlunparse(parsed._replace(netloc=new_netloc))
            logger.info(f"代理域名预解析: {hostname} -> {resolved_ip}")
            return resolved
    except socket.gaierror as e:
        logger.warning(f"代理域名预解析失败: {hostname} -> {e}，使用原始 URL")
    return proxy_url


class ProxyManager:
    """
    TPS 代理管理器
    每次请求通过固定代理地址自动换 IP，无需复杂的通道管理
    """

    def __init__(self):
        raw_url: str = config.PROXY_URL or ""
        self._proxy_url: str = _resolve_proxy_hostname(raw_url)
        self._total_blocked = 0
        self._total_requests = 0

        if self._proxy_url:
            masked = self._proxy_url.split("@")[-1] if "@" in self._proxy_url else self._proxy_url
            logger.info(f"代理初始化: TPS 模式, proxy=***@{masked}")
        else:
            logger.warning("代理未配置 (PROXY_URL 为空)")

    async def get_proxy(self) -> Optional[str]:
        """获取代理地址"""
        self._total_requests += 1
        return self._proxy_url or None

    async def report_blocked(self):
        """报告被封锁（TPS 模式下 IP 自动换，只记录统计）"""
        self._total_blocked += 1
        logger.warning(f"代理被封 (第 {self._total_blocked} 次)，下次请求将自动换 IP")

    def get_stats(self) -> Dict:
        masked = self._proxy_url.split("@")[-1] if "@" in self._proxy_url else self._proxy_url
        return {
            "mode": "tps",
            "proxy": f"***@{masked}" if self._proxy_url else "not configured",
            "total_requests": self._total_requests,
            "total_blocked": self._total_blocked,
        }


# ==================== 全局单例 ====================

_proxy_manager: Optional[ProxyManager] = None


def get_proxy_manager() -> ProxyManager:
    global _proxy_manager
    if _proxy_manager is None:
        _proxy_manager = ProxyManager()
    return _proxy_manager
