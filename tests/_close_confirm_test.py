# -*- coding: utf-8 -*-
"""点X弹窗选择关闭方式 + 托盘修复 验证(offscreen)。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication

from wt81111g.main_window import MainWindow

_APP = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def _make_win() -> MainWindow:
    d = tempfile.mkdtemp()
    win = MainWindow(os.path.join(d, "blacklist.json"), start_monitor=False)
    win.show()
    return win


def main() -> int:
    _app()

    # 1) 托盘修复: 不再依赖误报的 isSystemTrayAvailable, 应视为可用
    win = _make_win()
    assert win._tray_available is True, "托盘应视为可用(不再依赖误报的 isSystemTrayAvailable)"

    # 2) 选“收起到系统托盘” → 窗口隐藏, _really_quit 仍为 False
    win2 = _make_win()
    win2._ask_close_mode = lambda: "tray"
    win2.close()
    assert not win2.isVisible(), "选托盘应隐藏窗口"
    assert win2._really_quit is False, "选托盘不应触发真正退出"

    # 3) 选“关闭程序” → _really_quit 置 True, 窗口关闭
    win3 = _make_win()
    win3._ask_close_mode = lambda: "quit"
    win3.close()
    assert win3._really_quit is True, "选关闭应触发真正退出"
    assert not win3.isVisible(), "选关闭窗口应关闭"

    # 4) 从托盘菜单“退出应用”(_quit_app) → _really_quit=True 直接退出, 不弹窗
    win4 = _make_win()
    called = []
    win4._ask_close_mode = lambda: called.append("ask") or "tray"
    win4._quit_app()
    assert not called, "托盘退出不应再询问关闭方式"
    assert win4._really_quit is True

    print("CLOSE CONFIRM TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
