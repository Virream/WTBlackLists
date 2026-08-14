# -*- coding: utf-8 -*-
"""打包环境自检: 验证 patchright 在 PyInstaller frozen 环境能驱动系统浏览器。

单独用 PyInstaller 打包本脚本并运行, 确认 driver 在 frozen 环境可被定位。
"""
import sys
import tempfile

from patchright.sync_api import sync_playwright


def main() -> int:
    user_data = tempfile.mkdtemp(prefix="wtbl_sc_")
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data,
                channel="chrome",
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = ctx.new_page()
            page.goto("about:blank")
            webdriver = page.evaluate("navigator.webdriver")
            ua = page.evaluate("navigator.userAgent")
            print(f"webdriver={webdriver!r}")
            print(f"UA={ua}")
            ctx.close()
        print("PACKED PATCHRIGHT OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"PACKED PATCHRIGHT FAIL: {type(exc).__name__}: {exc}")
        return 1
    finally:
        import shutil
        shutil.rmtree(user_data, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
