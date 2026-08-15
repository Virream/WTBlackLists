"""应用内浏览器(Edge WebView2)抓取昵称 —— 子进程模式。

主应用是 Qt 事件循环, 无法与 pywebview(自带 GUI 循环)共存,
因此抓取在独立子进程中完成:
- 主进程: run_capture() 用 sys.executable --webview2-capture <uid> <outfile>
  启动子进程, 等待其写入结果 JSON 后读取返回。
- 子进程: child_main() 弹出 pywebview(WebView2)窗口加载官网 userinfo 页,
  Cloudflare 验证通常可自动通过(实测 Squirlykid14938 页自动通过),
  轮询读取昵称 DOM, 结果写入 outfile 后退出。

打包后 sys.executable 为 WTBlackList.exe, 同样通过 --webview2-capture 进入。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

from .config import WEBSITE_USERINFO_TEMPLATE

_SELECTOR = "li.user-profile__data-nick"
_TIMEOUT = 180  # 子进程抓取超时(秒)
_WINDOW_TITLE = "WTBlackList 浏览器"


def _capture_js() -> str:
    return (
        "(() => { const el = document.querySelector(%r);"
        " return el ? el.textContent.trim() : ''; })()" % _SELECTOR
    )


def child_main(uid: str, outfile: str) -> int:
    """子进程入口: 运行 pywebview 窗口抓取昵称, 结果写入 outfile。"""
    import webview

    url = WEBSITE_USERINFO_TEMPLATE.format(player_id=uid)
    js = _capture_js()
    nickname = ""

    def poll(window) -> None:
        nonlocal nickname
        start = time.time()
        while time.time() - start < _TIMEOUT:
            try:
                res = window.evaluate_js(js)
            except Exception:  # noqa: BLE001
                res = None
            if res:
                nickname = str(res)
                window.destroy()
                return
            time.sleep(1)
        window.destroy()

    window = webview.create_window(_WINDOW_TITLE, url, width=1100, height=850)
    webview.start(poll, window)

    try:
        with open(outfile, "w", encoding="utf-8") as f:
            json.dump({"ok": bool(nickname), "nickname": nickname}, f)
    except OSError:
        pass
    return 0


def run_capture(player_id: str, timeout: float = 200) -> tuple[str | None, str]:
    """主进程调用: 启动子进程跑 WebView2 抓取, 返回 (昵称或 None, 状态说明)。"""
    fd, outfile = tempfile.mkstemp(prefix="wtbl_wv2_", suffix=".json")
    os.close(fd)
    try:
        proc = subprocess.Popen(
            [sys.executable, "--webview2-capture", player_id, outfile],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            return None, "WebView2 抓取超时"
        try:
            with open(outfile, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None, "WebView2 未返回结果"
        nick = str(data.get("nickname") or "")
        if data.get("ok") and nick:
            return nick, "应用内浏览器自动抓取成功"
        return None, "应用内浏览器未抓到昵称"
    finally:
        try:
            os.remove(outfile)
        except OSError:
            pass
