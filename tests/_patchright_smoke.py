# -*- coding: utf-8 -*-
"""验证 patchright 能驱动系统真实 Edge(冒烟测试)。

只验证驱动能力:启动 → 打开 about:blank → 读取标题/UA/webdriver 标记 → 关闭。
headless=True 仅用于冒烟测试(避免弹窗);生产代码用 headless=False 真实窗口。
"""
import sys
import tempfile

from patchright.sync_api import sync_playwright


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "chrome"
    user_data = tempfile.mkdtemp(prefix="wtbl_pr_")
    try:
        kwargs = {}
        if target in ("chrome", "msedge"):
            kwargs["channel"] = target
        else:
            kwargs["executable_path"] = target
        with sync_playwright() as p:
            print(f"尝试启动系统浏览器 ({target}) ...")
            try:
                ctx = p.chromium.launch_persistent_context(
                    user_data,
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                    **kwargs,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"启动失败: {exc}")
                return 1
            page = ctx.new_page()
            page.goto("about:blank")
            title = page.title()
            ua = page.evaluate("navigator.userAgent")
            webdriver = page.evaluate("navigator.webdriver")
            print(f"标题: {title!r}")
            print(f"UA: {ua}")
            print(f"navigator.webdriver = {webdriver!r}")
            ctx.close()
            print(f"PATCHRIGHT {target} SMOKE OK")
            return 0
    finally:
        import shutil
        shutil.rmtree(user_data, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
