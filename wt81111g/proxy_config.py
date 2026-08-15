"""全局代理配置。

设置后, 项目内所有 requests 网络请求(模块级 requests.get/post 与
requests.Session 实例如 warthunder._SESSION)统一走代理。

实现: patch requests.Session.request, 在每次请求前把当前代理同步到该 session。
requests.get/post 内部也是用 Session().request(...), 因此一个 patch 全覆盖。
支持运行中修改代理立即生效(每次请求前同步)。
"""
from __future__ import annotations

import requests as _requests

_proxy_url = ""


def set_proxy(url: str) -> None:
    """设置代理地址(如 '127.0.0.1:7890' 或 'http://127.0.0.1:7890'); 传空串清除。"""
    global _proxy_url
    _proxy_url = (url or "").strip()


def get_proxy() -> str:
    """返回当前代理地址(未设置为空串)。"""
    return _proxy_url


def proxies() -> dict | None:
    """返回 requests 风格 proxies 字典; 未设置代理返回 None。"""
    url = _proxy_url
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return {"http": url, "https": url}


_orig_request = _requests.Session.request


def _request_with_proxy(self, method: str, url: str, **kwargs):
    p = proxies()
    if p is not None:
        self.proxies.update(p)
    elif self.proxies:
        self.proxies.clear()
    return _orig_request(self, method, url, **kwargs)


_requests.Session.request = _request_with_proxy
