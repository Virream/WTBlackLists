"""浏览器抓取逻辑测试(不启动真实浏览器, 验证真人浏览器兜底/降级/退出检测)。"""
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


def test_success() -> None:
    """浏览器抓到昵称 → 直接返回。"""
    calls: list[str] = []
    bc._find_browser_candidates = lambda: ["/x/edge.exe"]
    bc._capture_with_browser = lambda b, u, p: calls.append(b) or ("抓到昵称", "成功")
    nick, state = bc.capture_nickname_via_browser("123")
    check("返回昵称", nick == "抓到昵称")
    check("只试一个", calls == ["/x/edge.exe"])


def test_no_candidates() -> None:
    """无浏览器候选 → 提示未找到。"""
    bc._find_browser_candidates = lambda: []
    nick, state = bc.capture_nickname_via_browser("123")
    check("提示未找到", "未找到" in state)


def test_launch_fail_try_next() -> None:
    """Edge 启动失败(权限/不存在) → 继续试 Chrome。"""
    calls: list[str] = []
    bc._find_browser_candidates = lambda: ["/x/edge.exe", "/x/chrome.exe"]
    bc._capture_with_browser = lambda b, u, p: (
        calls.append(b) or
        (None, "浏览器启动失败: EACCES" if "edge" in b else (None, "浏览器已关闭"))
    )
    nick, state = bc.capture_nickname_via_browser("123")
    check("Edge失败后试Chrome", calls == ["/x/edge.exe", "/x/chrome.exe"])


def test_normal_close() -> None:
    """浏览器正常等待后用户关闭(非异常) → 不试下一个, 尊重用户操作。"""
    calls: list[str] = []
    bc._find_browser_candidates = lambda: ["/x/edge.exe", "/x/chrome.exe"]
    bc._capture_with_browser = lambda b, u, p: calls.append(b) or (None, "浏览器已关闭, 未检测到昵称")
    nick, state = bc.capture_nickname_via_browser("123")
    check("不试Chrome", calls == ["/x/edge.exe"])
    check("返回关闭提示", "浏览器已关闭" in state)


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")  # GBK 控制台兼容
    test_success()
    test_no_candidates()
    test_launch_fail_try_next()
    test_normal_close()
    print("\n".join(results))
    print("BROWSER CAPTURE LOGIC TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
