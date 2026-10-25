"""解析器公共工具。"""

import socket
from urllib.parse import urlparse

import httpx
from astrbot.api import logger

from ..constants import COMMON_TIMEOUT
from ..exception import ParseException

# 代理连通性探测用的目标（只为确认"代理能出去"，不关心内容）
PROBE_HTTP = "https://www.bilibili.com/robots.txt"
PROBE_SOCKS = "http://www.bilibili.com/robots.txt"


def is_proxy_alive(proxy: str, timeout: float = 1.5) -> bool:
    """代理端口是否有进程在监听（纯 TCP 连接测试，不走 HTTP）

    v2rayN 的 10808（http）/ 10809（socks）可能只在某一个上监听，
    所以先做这个快速判断，端口都不通就完全不用代理。
    """
    try:
        u = urlparse(proxy)
        host, port = u.hostname, u.port
        if not host or not port:
            return False
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


async def probe_proxy(proxy: str, timeout: float = 6.0) -> bool:
    """探测代理是否真的可用

    依次尝试：
    1. 原样使用（通常是 http 代理，CONNECT 隧道）
    2. 换成 SOCKS 模式（部分客户端 http/socks 共用同一端口）

    返回 True 表示代理能连通外网。
    """
    u = urlparse(proxy)
    if not u.hostname or not u.port:
        return False

    candidates = [proxy]
    alt_scheme = {
        "http": "socks5",
        "https": "socks5",
        "socks5": "http",
        "socks5h": "http",
    }
    if u.scheme in alt_scheme:
        candidates.append(f"{alt_scheme[u.scheme]}://{u.hostname}:{u.port}")

    for cand in candidates:
        url = PROBE_SOCKS if cand.startswith("socks") else PROBE_HTTP
        try:
            async with httpx.AsyncClient(
                proxy=cand, timeout=timeout, verify=False
            ) as client:
                resp = await client.get(url)
                if resp.status_code < 500:
                    return True
        except Exception as e:
            logger.debug(f"代理探测失败 ({cand}): {type(e).__name__}")
    return False


def build_mounts(proxy: str | None) -> dict[str, httpx.AsyncHTTPTransport] | None:
    """为 httpx.AsyncClient 构造 mounts（http/https 都走同一个代理）

    B 站接口是 https，视频 CDN 可能是 http 直链，两种目标都要走代理，
    用 mounts 统一指定比只传 proxy= 更稳妥。
    """
    if not proxy:
        return None
    return {
        "http://": httpx.AsyncHTTPTransport(proxy=proxy),
        "https://": httpx.AsyncHTTPTransport(proxy=proxy),
    }


async def get_redirect_url(
    url: str,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
) -> str:
    """获取重定向后的真实地址（不跟随跳转，只取 Location）

    Args:
        url: 原始短链
        headers: 附加请求头
        proxy: 可选代理，形如 http://127.0.0.1:10808

    Returns:
        最终地址；请求失败时返回原 url
    """
    try:
        async with httpx.AsyncClient(
            timeout=COMMON_TIMEOUT,
            verify=False,
            follow_redirects=False,
            mounts=build_mounts(proxy),
        ) as client:
            resp = await client.get(url, headers=headers or {})
            return resp.headers.get("Location", url)
    except Exception as e:
        raise ParseException(f"获取重定向地址失败: {e}") from e
