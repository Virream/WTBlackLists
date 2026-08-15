# -*- coding: utf-8 -*-
"""抓取昵称自动替换玩家昵称 + 曾用昵称维护 验证(offscreen)。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication

from wt81111g.blacklist import BlacklistEntry
from wt81111g.main_window import MainWindow

_APP = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def _make_win() -> MainWindow:
    d = tempfile.mkdtemp()
    win = MainWindow(os.path.join(d, "bl.json"), start_monitor=False)
    win.show()
    return win


def _add(win: MainWindow, nick: str, pid: str, fetched: str = "",
         locked: bool = False) -> BlacklistEntry:
    e = BlacklistEntry(nickname=nick, player_id=pid,
                       fetched_nickname=fetched, locked=locked)
    win.store.entries.append(e)
    win._make_row(e)
    return e


def main() -> int:
    _app()
    win = _make_win()

    # 1) 用户填旧昵称 + 旧抓取 A → 新抓取"新昵称": 替换玩家昵称, 旧抓取与用户手填都记曾用
    e = _add(win, "旧昵称", "1001", fetched="A")
    win._apply_fetched_nickname(e, "新昵称")
    assert e.nickname == "新昵称", e.nickname
    assert e.fetched_nickname == "新昵称"
    assert "A" in e.previous_nicknames, e.previous_nicknames
    assert "旧昵称" in e.previous_nicknames, e.previous_nicknames
    w = win._row_widgets[id(e)]
    assert w["nick"].text() == "新昵称", w["nick"].text()
    assert "旧昵称" in w["prev"].text(), w["prev"].text()

    # 2) 用户昵称为空 → 直接填入官方昵称, 不记曾用
    e2 = _add(win, "", "1002")
    win._apply_fetched_nickname(e2, "官方名")
    assert e2.nickname == "官方名", e2.nickname
    assert e2.previous_nicknames == [], e2.previous_nicknames

    # 3) 抓取与用户填写一致 → 完全不变
    e3 = _add(win, "相同", "1003")
    win._apply_fetched_nickname(e3, "相同")
    assert e3.nickname == "相同"
    assert e3.previous_nicknames == []
    assert e3.fetched_nickname == "相同"

    # 4) 锁定条目 → 只更新内部 fetched, 玩家昵称字段与表格不替换, 手填不记曾用
    e4 = _add(win, "锁定昵称", "1004", locked=True)
    win._apply_fetched_nickname(e4, "官方新")
    assert e4.nickname == "锁定昵称", e4.nickname
    assert e4.fetched_nickname == "官方新"
    assert "锁定昵称" not in e4.previous_nicknames, e4.previous_nicknames
    w4 = win._row_widgets[id(e4)]
    assert w4["nick"].text() == "锁定昵称", w4["nick"].text()

    print("AUTO NICKNAME TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
