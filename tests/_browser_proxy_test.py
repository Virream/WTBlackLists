# -*- coding: utf-8 -*-
"""浏览器兜底代理参数 验证(不真正启动浏览器)。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wt81111g.browser_capture import _build_browser_args
from wt81111g.proxy_config import set_proxy

URL = "https://warthunder.com/zh/community/userinfo/?uid=123/"


def main() -> int:
    # 1) 未设置代理 → 无 --proxy-server
    set_proxy("")
    args = _build_browser_args("C:/edge.exe", 9222, "/tmp/ud", URL)
    assert not any(a.startswith("--proxy-server") for a in args), args
    assert args[-1] == URL and "--remote-debugging-port=9222" in args

    # 2) 设置代理 → 浏览器带 --proxy-server(自动补 http://)
    set_proxy("127.0.0.1:7890")
    args = _build_browser_args("C:/edge.exe", 9222, "/tmp/ud", URL)
    proxy_args = [a for a in args if a.startswith("--proxy-server")]
    assert proxy_args == ["--proxy-server=http://127.0.0.1:7890"], args
    assert "--user-data-dir=/tmp/ud" in args

    # 3) 已带 http:// 前缀的代理
    set_proxy("http://1.2.3.4:8080")
    args = _build_browser_args("C:/chrome.exe", 9222, "/tmp/ud", URL)
    assert "--proxy-server=http://1.2.3.4:8080" in args

    # 4) URL 始终是最后一个参数
    assert args[-1] == URL

    set_proxy("")
    print("BROWSER PROXY TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
