"""交互式真人浏览器兜底: 启动用户本机真实浏览器(Edge/Chrome), 抓取官网昵称。

合规设计(不伪造环境、不自动绕过验证):
- 不用 patchright/playwright 伪造或隐藏自动化特征, 不自动通过 Cloudflare 验证。
- 用 subprocess 启动系统真实浏览器, 窗口用户可见、可操作; 验证完全交给真实
  浏览器环境与用户(静默通过则零操作, 弹交互验证则由用户点击)。
- 程序只负责: 导航到目标 URL → 轮询等待页面加载 → 检测 .user-profile__data-nick
  元素并读取昵称。绝不自动刷新、绝不自动通过验证。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time

import requests

from .config import WEBSITE_USERINFO_TEMPLATE

_NICK_SELECTOR = "li.user-profile__data-nick"


def _pick_free_port() -> int:
    """获取一个空闲端口作为 CDP 调试端口。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cdp_pages(port: int) -> list[dict]:
    """通过 CDP HTTP 接口获取当前标签页列表。"""
    try:
        r = requests.get(f"http://127.0.0.1:{port}/json", timeout=3)
        if r.status_code == 200:
            return r.json()
    except requests.RequestException:
        pass
    return []


def _find_browser_candidates() -> list[str]:
    """返回按优先级排列的可用浏览器列表(Edge 优先, 其次 Chrome)。"""
    edge = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
    ]
    chrome = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    out = [c for c in edge + chrome if c and os.path.isfile(c)]
    for name in ("msedge", "chrome"):
        w = shutil.which(name)
        if w and w not in out:
            out.append(w)
    return out


def capture_nickname_via_browser(player_id: str,
                                 wait_timeout: float | None = None) -> tuple[str | None, str]:
    """启动系统真实浏览器, 由真实浏览器环境/用户通过真人验证, 自动检测并抓取昵称。

    真人浏览器兜底: 不伪造环境、不隐藏自动化特征、不自动绕过验证。
    打开真实可见的 Edge/Chrome 窗口(继承系统代理, 如 Clash), 验证交给真实环境;
    程序只负责导航、等待页面加载、检测并读取昵称 DOM。
    无限制等待: 直到抓到昵称, 或用户关闭浏览器窗口。
    返回 (昵称或 None, 状态说明)。
    """
    url = WEBSITE_USERINFO_TEMPLATE.format(player_id=player_id)
    candidates = _find_browser_candidates()
    if not candidates:
        return None, "未找到本机 Edge/Chrome 浏览器"
    last_err = ""
    for browser in candidates:
        nick, state = _capture_with_browser(browser, url, player_id)
        if nick:
            return nick, state
        if "启动失败" in state:
            last_err = state
            continue
        return nick, state
    return None, last_err or "浏览器抓取失败"


def _capture_with_browser(browser: str, url: str, player_id: str) -> tuple[str | None, str]:
    """用系统真实浏览器启动并轮询抓取昵称(真人验证兜底)。

    注意: 不添加 --disable-blink-features=AutomationControlled 等隐藏自动化特征
    参数, 保持浏览器真实原样——验证完全交给真实浏览器环境与用户。
    """
    port = _pick_free_port()
    user_data = tempfile.mkdtemp(prefix="wtbl_capture_")
    args = [
        browser,
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=%s" % user_data,
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        url,
    ]
    proc = None
    try:
        try:
            proc = subprocess.Popen(args, shell=False)
        except OSError as exc:
            return None, f"浏览器启动失败: {exc}"
        from websockets.sync.client import connect as ws_connect

        while True:
            # 浏览器进程已退出(用户手动关闭) → 抓取失败
            if proc.poll() is not None:
                return None, "浏览器已关闭, 未检测到昵称"
            pages = _cdp_pages(port)
            target = None
            for pg in pages:
                if str(player_id) in pg.get("url", "") or "userinfo" in pg.get("url", ""):
                    target = pg
                    break
            if target is None:
                # 浏览器还没加载目标页
                time.sleep(0.8)
                continue
            ws_url = target.get("webSocketDebuggerUrl")
            if not ws_url:
                time.sleep(0.8)
                continue
            # 通过 WebSocket 执行 Runtime.evaluate 抓 DOM
            nick = _eval_nickname(ws_connect, ws_url)
            if nick:
                return nick, "浏览器验证通过, 自动抓取成功"
            # 间隔轮询
            time.sleep(0.8)
    except Exception as exc:  # noqa: BLE001
        return None, f"浏览器抓取异常: {exc}"
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        try:
            import shutil as _sh
            _sh.rmtree(user_data, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


def _eval_nickname(ws_connect, ws_url: str) -> str | None:
    """通过 CDP WebSocket 执行 JS, 抓取昵称元素文本。"""
    try:
        with ws_connect(ws_url, timeout=5) as ws:
            expr = (
                "(() => { const el = document.querySelector(%r); "
                "return el ? el.innerText.trim() : null; })()" % _NICK_SELECTOR
            )
            msg = {
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {"expression": expr, "returnByValue": True},
            }
            ws.send(json.dumps(msg))
            resp = json.loads(ws.recv())
            result = (resp.get("result", {}) or {}).get("result", {}) or {}
            val = result.get("value")
            if val:
                return str(val).strip() or None
    except Exception:  # noqa: BLE001
        pass
    return None
