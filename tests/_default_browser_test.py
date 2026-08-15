# -*- coding: utf-8 -*-
"""浏览器兜底默认浏览器解析 + 候选优先级 验证。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wt81111g.browser_capture import (
    _default_browser_path, _find_browser_candidates, _is_chromium,
)


def main() -> int:
    # 1) Chromium 判定
    assert _is_chromium(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")
    assert _is_chromium(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    assert _is_chromium(r"C:\Users\x\AppData\Local\BraveSoftware\Brave-Browser\brave.exe")
    assert not _is_chromium(r"C:\Program Files\Mozilla Firefox\firefox.exe")
    assert not _is_chromium("")

    # 2) 默认浏览器解析(本机 Windows 通常有默认浏览器)
    default = _default_browser_path()
    print("系统默认浏览器:", default)

    # 3) 候选列表: 非空 / 全部存在 / 去重 / 默认浏览器(Chromium)在最前
    cands = _find_browser_candidates()
    assert cands, "应至少有一个可用浏览器候选"
    for c in cands:
        assert os.path.isfile(c), f"候选不存在: {c}"
        assert _is_chromium(c), f"候选应为 Chromium 系: {c}"
    assert len(cands) == len({c.lower() for c in cands}), "候选应去重"
    if default and _is_chromium(default) and os.path.isfile(default):
        assert cands[0].lower() == default.lower(), \
            f"默认浏览器应优先: {cands[0]} vs {default}"
    print("候选:", cands)

    print("DEFAULT BROWSER TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
