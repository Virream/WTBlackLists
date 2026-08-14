"""浏览器抓取逻辑测试(不启动真实浏览器, 验证三级降级链/退出检测)。"""
from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wt81111g.browser_capture as bc

results: list[str] = []


def check(name: str, cond: bool) -> None:
    if cond:
        results.append(f"OK: {name}")
    else:
        results.append(f"FAIL: {name}")
        raise AssertionError(name)


def test_patchright_success() -> None:
    """patchright 抓到昵称 → 直接返回, 不降级。"""
    calls: list[str] = []
    bc._capture_with_patchright = lambda url, pid: calls.append("pr") or ("抓到昵称", "成功")
    bc._capture_with_playwright = lambda url, pid: calls.append("pw") or (None, "x")
    bc._capture_with_browser = lambda b, u, p: calls.append("sys") or (None, "x")
    bc._find_browser_candidates = lambda: ["/x/edge.exe"]
    nick, state = bc.capture_nickname_via_browser("123")
    check("返回昵称", nick == "抓到昵称")
    check("不降级", calls == ["pr"])


def test_patchright_unavailable_fallback_playwright() -> None:
    """patchright 不可用 → 降级 Playwright, Playwright 抓到昵称返回。"""
    calls: list[str] = []
    bc._capture_with_patchright = lambda url, pid: calls.append("pr") or (None, "patchright 不可用")
    bc._capture_with_playwright = lambda url, pid: calls.append("pw") or ("pw昵称", "成功")
    bc._capture_with_browser = lambda b, u, p: calls.append("sys") or (None, "x")
    bc._find_browser_candidates = lambda: ["/x/edge.exe"]
    nick, state = bc.capture_nickname_via_browser("123")
    check("降级到Playwright", "pw" in calls)
    check("返回昵称", nick == "pw昵称")
    check("不再降级系统", "sys" not in calls)


def test_playwright_unavailable_fallback_system() -> None:
    """patchright 不可用 + Playwright 不可用 → 降级系统浏览器(Edge 优先)。"""
    calls: list[str] = []
    bc._capture_with_patchright = lambda url, pid: calls.append("pr") or (None, "patchright 不可用")
    bc._capture_with_playwright = lambda url, pid: calls.append("pw") or (None, "内置浏览器不可用")
    bc._capture_with_browser = lambda b, u, p: calls.append("sys:" + os.path.basename(b)) or (None, "浏览器已关闭, 未检测到昵称")
    bc._find_browser_candidates = lambda: ["/x/edge.exe", "/x/chrome.exe"]
    nick, state = bc.capture_nickname_via_browser("46155613")
    check("降级到Edge", "sys:edge.exe" in calls)
    check("Edge正常启动后不再试Chrome", "sys:chrome.exe" not in calls)
    check("最终返回关闭提示", state == "浏览器已关闭, 未检测到昵称")


def test_system_launch_fail_try_next() -> None:
    """系统 Edge 启动失败(权限/不存在) → 继续试 Chrome。"""
    calls: list[str] = []
    bc._capture_with_patchright = lambda url, pid: (None, "patchright 不可用")
    bc._capture_with_playwright = lambda url, pid: (None, "内置浏览器不可用")
    bc._capture_with_browser = lambda b, u, p: (
        calls.append("sys:" + os.path.basename(b)) or
        (None, "浏览器启动失败: EACCES" if "edge" in b else "浏览器已关闭")
    )
    bc._find_browser_candidates = lambda: ["/x/edge.exe", "/x/chrome.exe"]
    nick, state = bc.capture_nickname_via_browser("46155613")
    check("Edge失败后试Chrome", calls == ["sys:edge.exe", "sys:chrome.exe"])


def test_no_fallback_when_user_closes() -> None:
    """patchright 正常等待后用户关闭(非异常) → 不应降级。"""
    calls: list[str] = []
    bc._capture_with_patchright = lambda url, pid: calls.append("pr") or (None, "浏览器已关闭, 未检测到昵称")
    bc._capture_with_playwright = lambda url, pid: calls.append("pw") or (None, "x")
    bc._capture_with_browser = lambda b, u, p: calls.append("sys") or (None, "x")
    bc._find_browser_candidates = lambda: ["/x/edge.exe"]
    nick, state = bc.capture_nickname_via_browser("123")
    check("不降级", calls == ["pr"])
    check("返回关闭提示", "浏览器已关闭" in state)


def test_patchright_launch_fail_fallback_playwright() -> None:
    """patchright 系统浏览器启动失败 → 降级 Playwright。"""
    calls: list[str] = []
    bc._capture_with_patchright = lambda url, pid: calls.append("pr") or (None, "系统浏览器启动失败: msedge: EACCES; chrome: x")
    bc._capture_with_playwright = lambda url, pid: calls.append("pw") or ("pw昵称", "成功")
    bc._capture_with_browser = lambda b, u, p: calls.append("sys") or (None, "x")
    bc._find_browser_candidates = lambda: ["/x/edge.exe"]
    nick, state = bc.capture_nickname_via_browser("123")
    check("降级到Playwright", "pw" in calls)
    check("返回昵称", nick == "pw昵称")


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")  # GBK 控制台兼容
    test_patchright_success()
    test_patchright_unavailable_fallback_playwright()
    test_playwright_unavailable_fallback_system()
    test_system_launch_fail_try_next()
    test_no_fallback_when_user_closes()
    test_patchright_launch_fail_fallback_playwright()
    print("\n".join(results))
    print("BROWSER CAPTURE LOGIC TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
