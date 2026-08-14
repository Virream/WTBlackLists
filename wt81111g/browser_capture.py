"""交互式浏览器兜底: 启动用户本机浏览器(真人可通过验证), 自动检测页面加载并抓取昵称。

原理:
1. 用 subprocess 启动 Chrome/Edge 的独立实例, 带 --remote-debugging-port(CDP 调试端口)
   和独立 --user-data-dir(不影响用户现有浏览器会话)。
2. 加载目标 URL(官网 userinfo 页面)。
3. 软件通过 CDP HTTP 接口 /json 轮询页面状态, 等待用户手动通过真人验证。
4. 一旦页面出现 .user-profile__data-nick 元素, 自动抓取昵称并返回。

注: Cloudflare 会拦截自动化浏览器, 所以这里"浏览器窗口用户可见", 由用户手动完成
验证(真人浏览器可通过), 软件只负责检测与抓取, 不做自动绕过。
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
from .warthunder import _parse_website_nickname

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
    """启动浏览器, 由用户通过真人验证, 自动检测并抓取昵称。

    三级降级:
    1. patchright 驱动系统真实浏览器(Edge → Chrome), 网络层指纹真实, 自动过验证概率最高;
    2. Playwright 内置 Chromium(真实窗口 + 用户手动兜底);
    3. 系统浏览器 CDP 方式(最后兜底)。
    无限制等待: 持续轮询直到抓到昵称, 或用户关闭浏览器窗口。
    返回 (昵称或 None, 状态说明)。
    """
    url = WEBSITE_USERINFO_TEMPLATE.format(player_id=player_id)
    # 1. patchright 驱动系统真实浏览器(指纹真实, 首选)
    nick, state = _capture_with_patchright(url, player_id)
    if nick:
        return nick, state
    if not _should_fallback(state):
        return nick, state  # 用户关闭窗口等 → 直接返回, 不重复弹窗
    # 2. Playwright 内置 Chromium(真实窗口 + 用户手动兜底)
    nick, state = _capture_with_playwright(url, player_id)
    if nick:
        return nick, state
    if state not in ("内置浏览器不可用",) and "异常" not in state:
        return nick, state
    # 3. 系统浏览器 CDP 方式兜底
    candidates = _find_browser_candidates()
    if not candidates:
        return None, "未找到本机 Edge/Chrome 浏览器"
    last_err = ""
    for browser in candidates:
        nick, state = _capture_with_browser(browser, url, player_id)
        if nick:
            return nick, state
        if "启动失败" in state:  # 该浏览器无法启动 → 继续试下一个
            last_err = state
            continue
        return nick, state
    return None, last_err or "浏览器抓取失败"


def _bundle_browser_path() -> str | None:
    """定位打包内置的 Chromium(放在 _internal/browsers/chromium/ 下)。

    打包后 chromium 结构: _internal/browsers/chromium/chrome-win64/chrome.exe
    开发环境返回 None(用 playwright 默认缓存)。
    """
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 打包后=_internal
    candidates = [
        os.path.join(base, "browsers", "chromium", "chrome-win64", "chrome.exe"),
        os.path.join(base, "browsers", "chromium", "chrome.exe"),
        # 开发/相对项目根
        os.path.join(os.path.dirname(os.path.dirname(base)), "browsers", "chromium", "chrome-win64", "chrome.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _system_proxy() -> str | None:
    """读取 Windows 系统代理设置(如 Clash 的 127.0.0.1:7897)。

    内置浏览器默认不继承系统代理, 这里读取注册表并把地址传给 Chromium,
    使其与系统浏览器走同一代理通道, 提高 Cloudflare 验证通过率。
    """
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        if enable and server:
            server = str(server).strip()
            # 处理 "http=127.0.0.1:7897;https=..." 或纯 "host:port"
            if "=" in server:
                for part in server.split(";"):
                    if part.lower().startswith("http="):
                        return part.split("=", 1)[1].strip()
                return None
            return server
    except Exception:  # noqa: BLE001
        pass
    return None


_FALLBACK_MARKERS = ("patchright 不可用", "系统浏览器启动失败", "系统浏览器异常")


def _should_fallback(state: str) -> bool:
    """patchright 路径失败时, 判断是否值得降级到下一个方案。

    只有 patchright 本身不可用 / 系统浏览器启动或运行异常才降级;
    用户主动关闭窗口未抓到昵称, 不重复弹窗降级。
    """
    return any(m in state for m in _FALLBACK_MARKERS)


def _capture_with_patchright(url: str, player_id: str) -> tuple[str | None, str]:
    """用 patchright 驱动系统真实浏览器(Edge 优先 → Chrome)。

    patchright 是 undetected 的 Playwright 移植: 驱动系统真实浏览器二进制,
    网络层 TLS/HTTP2 指纹与真人一致(内置 Chromium 是专用编译二进制, 指纹不同),
    因此 Cloudflare Turnstile 自动验证通过率显著更高。
    保持真实窗口(headless=False) + 无自动刷新 + 用户手动兜底。
    注意: 不覆盖 user_agent/locale, 让系统浏览器保持原生特征, 避免 UA 与
    TLS 指纹不一致被反爬标记。
    返回 (昵称或 None, 状态说明)。
    """
    try:
        from patchright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        return None, "patchright 不可用"
    user_data = tempfile.mkdtemp(prefix="wtbl_pr_")
    proxy = _system_proxy()
    try:
        with sync_playwright() as p:
            ctx = None
            last_err = ""
            for channel in ("msedge", "chrome"):
                try:
                    ctx = p.chromium.launch_persistent_context(
                        user_data,
                        channel=channel,
                        headless=False,  # 真实窗口, 用户手动兜底
                        viewport={"width": 1280, "height": 800},
                        args=[
                            "--disable-blink-features=AutomationControlled",
                            "--disable-infobars",
                            "--no-first-run",
                            "--no-default-browser-check",
                        ],
                        **({"proxy": {"server": proxy}} if proxy else {}),
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    last_err = f"{channel}: {exc}"
                    ctx = None
            if ctx is None:
                return None, f"系统浏览器启动失败: {last_err}"
            # 隐藏自动化痕迹(双保险: patchright 已处理, 这里再补一层)
            try:
                ctx.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
            except Exception:  # noqa: BLE001
                pass
            page = ctx.new_page()
            try:
                # 不等待 load 事件: Cloudflare 挑战页常不触发 load, 默认会白等 60s。
                # 用 domcontentloaded + 短超时, 失败也不致命, 交给轮询抓取。
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001
                pass
            # 无限制等待: 直到抓到昵称, 或用户关闭窗口。绝不自动刷新。
            while True:
                open_pages = [pg for pg in ctx.pages if not pg.is_closed()]
                if not open_pages:
                    return None, "浏览器已关闭, 未检测到昵称"
                # 遍历所有打开页面(含 Cloudflare 重定向产生的新页), 任一出现昵称即抓取
                for pg in open_pages:
                    try:
                        el = pg.locator("li.user-profile__data-nick").first
                        if el.count() > 0:
                            nick = el.inner_text().strip()
                            if nick:
                                return nick, "浏览器验证通过, 自动抓取成功"
                    except Exception:  # noqa: BLE001
                        continue
                time.sleep(0.8)
    except Exception as exc:  # noqa: BLE001
        return None, f"系统浏览器异常: {exc}"
    finally:
        try:
            shutil.rmtree(user_data, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


def _capture_with_playwright(url: str, player_id: str) -> tuple[str | None, str]:
    """用 Playwright 内置 Chromium 启动真实窗口, 用户手动验证后自动抓取昵称。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:  # noqa: BLE001
        return None, "内置浏览器不可用"

    user_data = tempfile.mkdtemp(prefix="wtbl_pw_")
    exe_path = _bundle_browser_path()
    launch_kwargs = {}
    if exe_path:
        launch_kwargs["executable_path"] = exe_path
    # 内置浏览器不继承系统代理, 显式读取(Clash 等)并传给 Chromium
    proxy = _system_proxy()
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data,
                headless=False,  # 真实窗口, 用户手动通过真人验证
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/131.0 Safari/537.36"),
                locale="zh-CN",
                viewport={"width": 1280, "height": 800},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--no-first-run", "--no-default-browser-check",
                ],
                **launch_kwargs,
            )
            # 隐藏自动化痕迹, 降低 Cloudflare 检测概率
            try:
                ctx.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
            except Exception:  # noqa: BLE001
                pass
            page = ctx.new_page()
            try:
                # 不等待 load 事件: Cloudflare 挑战页常不触发 load, 默认会白等 60s。
                # 用 domcontentloaded + 短超时, 失败也不致命, 交给轮询抓取。
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001
                pass
            # 无限制等待: 直到抓到昵称, 或用户关闭窗口(浏览器进程退出)。
            # 注意: 绝不自动刷新页面。Cloudflare 挑战页自身会定时重试,
            # 一切刷新交给用户或挑战页自己处理。
            while True:
                open_pages = [pg for pg in ctx.pages if not pg.is_closed()]
                if not open_pages:
                    return None, "浏览器已关闭, 未检测到昵称"
                # 遍历所有打开页面(含 Cloudflare 重定向产生的新页), 任一出现昵称即抓取
                for pg in open_pages:
                    try:
                        el = pg.locator("li.user-profile__data-nick").first
                        if el.count() > 0:
                            nick = el.inner_text().strip()
                            if nick:
                                return nick, "浏览器验证通过, 自动抓取成功"
                    except Exception:  # noqa: BLE001
                        continue
                time.sleep(0.8)
    except Exception as exc:  # noqa: BLE001
        return None, f"内置浏览器异常: {exc}"
    finally:
        try:
            import shutil as _sh
            _sh.rmtree(user_data, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


def _capture_with_browser(browser: str, url: str, player_id: str) -> tuple[str | None, str]:
    """用指定浏览器启动并轮询抓取昵称。"""
    port = _pick_free_port()
    user_data = tempfile.mkdtemp(prefix="wtbl_capture_")
    args = [
        browser,
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=%s" % user_data,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
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
