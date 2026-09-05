# ============================================================
# chiguo_net.py — 统一的本地回环与代理隔离助手
# 收敛分散在 cli/dispatch、runner/loop、chiguo_envcheck、netease/bridge 的
# localhost 判定 + ProxyHandler({}) 回环直连逻辑
# ============================================================

import ipaddress
import urllib.request


def is_local_host(host: str) -> bool:
    """判断 host 是否为本地回环（等价 --noproxy '*'）。
    P2-07: ipaddress.is_loopback 覆盖 127.0.0.0/8 全段与 ::ffff:127.0.0.1 等
    IPv4 映射变体；纯白名单集合漏 127.0.0.2/8、::ffff 变体。"""
    try:
        if (host or "").lower() == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError, AttributeError):
        return False


def build_no_proxy_opener():
    """构建禁用代理的 opener（回环直连，防 MUSIC_U 随代理外泄）。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def is_local_url(url: str) -> bool:
    """url 是否指向本地回环（用于白名单校验）。"""
    try:
        from urllib.parse import urlparse

        return is_local_host(urlparse(url).hostname or "")
    except (ValueError, TypeError, OSError):
        return False
