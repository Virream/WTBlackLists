# -*- coding: utf-8 -*-
"""复用已通过 Cloudflare 验证的 profile, 用 patchright 读取昵称(端到端验证)。

用法: python tests/_reuse_profile_read.py [uid]
复用 profile 里的 cf_clearance cookie, 免去再次人机验证。
headless=False 真实窗口(与生产一致), 抓到昵称立即关闭。
"""
import os
import sys
import time

from patchright.sync_api import sync_playwright

PROFILE = r"C:\Users\18287\AppData\Local\Temp\wtbl_pr_z2wg40po"
UID = sys.argv[1] if len(sys.argv) > 1 else "207605680"
URL = f"https://warthunder.com/zh/community/userinfo/?uid={UID}/"


def main() -> int:
    if not os.path.isdir(PROFILE):
        print("profile 不存在:", PROFILE)
        return 1
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE,
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.new_page()
        try:
            page.goto(URL, timeout=60000)
        except Exception as exc:  # noqa: BLE001
            print(f"goto 异常: {type(exc).__name__}: {exc}")
        # 短轮询(60s)找昵称, 复用修复后的遍历所有页面逻辑
        for _ in range(60):
            open_pages = [pg for pg in ctx.pages if not pg.is_closed()]
            for pg in open_pages:
                try:
                    el = pg.locator("li.user-profile__data-nick").first
                    if el.count() > 0:
                        nick = el.inner_text().strip()
                        if nick:
                            print("昵称:", nick)
                            print("REUSE PROFILE OK")
                            ctx.close()
                            return 0
                except Exception:  # noqa: BLE001
                    continue
            time.sleep(1)
        # 诊断: 打印各页标题/URL/body 片段
        for pg in ctx.pages:
            if pg.is_closed():
                continue
            try:
                print("页面标题:", pg.title())
                print("URL:", pg.url)
                print("BODY:", pg.inner_text("body")[:400].replace("\n", " | "))
            except Exception as exc:  # noqa: BLE001
                print("读取异常:", exc)
        ctx.close()
        print("未找到昵称")
        return 1


if __name__ == "__main__":
    sys.exit(main())
