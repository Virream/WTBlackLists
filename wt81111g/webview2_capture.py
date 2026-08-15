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


# 页面顶部悬浮工具条: 显示当前连接地址 + 刷新按钮(幂等, 已存在则不重复注入)
_BAR_JS = (
    "(() => {"
    " if (document.getElementById('wtbl-bar')) return;"
    " var bar = document.createElement('div');"
    " bar.id = 'wtbl-bar';"
    " bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:999999;'"
    "  + 'background:rgba(20,20,40,0.92);color:#fff;font:12px \\'Microsoft YaHei\\',sans-serif;'"
    "  + 'padding:4px 8px;display:flex;align-items:center;gap:6px;';"
    " var url = document.createElement('span');"
    " url.style.cssText = 'flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';"
    " url.textContent = location.href;"
    " var btn = document.createElement('button');"
    " btn.textContent = '\\u27F3 刷新';"
    " btn.style.cssText = 'background:#1a6fb0;color:#fff;border:none;border-radius:4px;'"
    "  + 'padding:2px 12px;cursor:pointer;';"
    " btn.onclick = function(){ location.reload(); };"
    " bar.appendChild(url); bar.appendChild(btn);"
    " if (document.body) { document.body.prepend(bar); }"
    "})()"
)


def child_main(uid: str, outfile: str, hidden: bool = False) -> int:
    """子进程入口: 运行 pywebview 窗口抓取昵称, 结果写入 outfile。

    hidden=True 时窗口全程隐藏(自动模式, 不打断游戏、不抢焦点、不打扰用户);
    无论是否通过验证都不显示, 结果(成功或超时)写入 outfile, 用户游戏结束后
    从应用界面状态即可看到结果。

    轮询在独立线程进行(不阻塞 GUI 事件循环), 并监听窗口关闭事件——
    用户主动关闭浏览器窗口时立即结束并写出结果, 主进程不会长时间卡住。
    """
    import threading

    import webview

    url = WEBSITE_USERINFO_TEMPLATE.format(player_id=uid)
    js = _capture_js()
    nickname = ""
    closed = threading.Event()

    def poll_worker() -> None:
        nonlocal nickname
        start = time.time()
        while not closed.is_set() and time.time() - start < _TIMEOUT:
            # 非隐藏模式: 注入页面顶部悬浮工具条(地址栏 + 刷新按钮, 幂等)
            if not hidden:
                try:
                    window.evaluate_js(_BAR_JS)
                except Exception:  # noqa: BLE001
                    pass
            try:
                res = window.evaluate_js(js)
            except Exception:  # noqa: BLE001
                res = None
            if res:
                nickname = str(res)
                break
            time.sleep(1)
        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass

    def on_ready(_window) -> None:
        # GUI 事件循环启动后, 在后台线程轮询, 避免阻塞窗口关闭事件
        threading.Thread(target=poll_worker, daemon=True).start()

    def on_closed() -> None:
        closed.set()

    window = webview.create_window(
        _WINDOW_TITLE, url, width=1100, height=850, hidden=hidden,
    )
    window.events.closed += on_closed
    webview.start(on_ready, window)

    try:
        with open(outfile, "w", encoding="utf-8") as f:
            json.dump({"ok": bool(nickname), "nickname": nickname}, f)
    except OSError:
        pass
    return 0


def run_capture(player_id: str, hidden: bool = False,
                timeout: float = 200) -> tuple[str | None, str]:
    """主进程调用: 启动子进程跑 WebView2 抓取, 返回 (昵称或 None, 状态说明)。"""
    fd, outfile = tempfile.mkstemp(prefix="wtbl_wv2_", suffix=".json")
    os.close(fd)
    try:
        cmd = [sys.executable, "--webview2-capture", player_id, outfile]
        if hidden:
            cmd.append("--hidden")
        proc = subprocess.Popen(
            cmd,
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
