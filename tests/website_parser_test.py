"""官网 userinfo 页面昵称解析测试(用真实保存的页面快照验证)。"""
from __future__ import annotations

import glob
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wt81111g.warthunder import _parse_website_nickname

results: list[str] = []


def check(name: str, cond: bool) -> None:
    if cond:
        results.append(f"OK: {name}")
    else:
        results.append(f"FAIL: {name}")
        raise AssertionError(name)


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")  # GBK 控制台兼容
    # 查找用户保存的真实页面快照(战争雷霆*.html)
    pages = glob.glob(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "战争雷霆*.html"))
    if not pages:
        # 无快照时用内嵌的最小样例(结构来自实测)
        sample = (
            '<li class=user-profile__data-nick>\n  目击而道存矣\n</li>'
        )
        nick = _parse_website_nickname(sample)
        check("内嵌样例解析", nick == "目击而道存矣")
    else:
        html = open(pages[0], encoding="utf-8", errors="replace").read()
        nick = _parse_website_nickname(html)
        check("真实快照解析出昵称(非None)", nick is not None)
        check("昵称非整页标题", "寻找玩家" not in (nick or ""))
        print(f"真实快照昵称: {nick!r}")
    # 空页面 / 挑战页
    check("空页返回None", _parse_website_nickname("") is None)
    check("挑战页返回None", _parse_website_nickname(
        '<title>请稍候…</title><div>正在验证您是否是真人</div>') is None)
    print("\n".join(results))
    print("WEBSITE PARSER TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
