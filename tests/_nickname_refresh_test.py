# -*- coding: utf-8 -*-
"""刷新昵称窗口 验证(offscreen): 统计/选项/勾选持久化/回传更新。"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication

from wt81111g.blacklist import BlacklistEntry
from wt81111g.main_window import MainWindow
from wt81111g.nickname_refresh_dialog import NicknameRefreshDialog

_APP = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def main() -> int:
    _app()
    d = tempfile.mkdtemp()
    win = MainWindow(os.path.join(d, "bl.json"), start_monitor=False)
    win.show()

    # 添加: 一条有ID, 一条无ID
    e1 = BlacklistEntry(player_id="1001", nickname="旧昵称")
    e2 = BlacklistEntry(player_id="", nickname="无ID")
    win.store.entries.append(e1)
    win.store.entries.append(e2)
    win._make_row(e1)
    win._make_row(e2)

    dlg = NicknameRefreshDialog(win.store, win.nickname_cache, win.app_settings, win)
    # 1) 统计只含"有玩家ID"且24h内未抓取过的条目
    assert len(dlg._items) == 1, dlg._items
    assert dlg._items[0][0] == "1001"
    assert dlg._table.rowCount() == 1
    assert dlg._table.columnCount() == 4, "应有4列(对齐缓存窗口)"

    # 1.5) 24h 内已抓取的 ID 应被跳过
    e1.fetched_at = time.time() - 3600  # 1小时前抓过
    dlg2 = NicknameRefreshDialog(win.store, win.nickname_cache, win.app_settings, win)
    assert len(dlg2._items) == 0, "24h内已抓取应被跳过"
    assert dlg2._skipped == 1, dlg2._skipped
    dlg2.close()
    e1.fetched_at = 0.0

    # 2) 勾选框: 读 settings + 勾选写回(可在此取消)
    assert dlg._auto_check.isChecked() is False
    dlg._auto_check.setChecked(True)
    assert win.app_settings.auto_browser is True, "勾选应写回 settings"
    dlg._auto_check.setChecked(False)
    assert win.app_settings.auto_browser is False, "可在此取消"

    # 3) 互斥单选: 默认内置浏览器
    assert dlg._wv2_radio.isChecked() is True
    assert dlg._sys_radio.isChecked() is False
    dlg._sys_radio.setChecked(True)
    assert dlg._wv2_radio.isChecked() is False, "互斥"

    # 4) 回传更新: 清洗 @live、替换昵称、记曾用、写缓存
    win._on_refresh_fetched("1001", "新昵称@live")
    e1u = next(e for e in win.store.entries if e.player_id == "1001")
    assert e1u.nickname == "新昵称", e1u.nickname
    assert "旧昵称" in e1u.previous_nicknames, e1u.previous_nicknames
    assert win.nickname_cache.get("1001")["nickname"] == "新昵称"
    assert len(win._notifications) >= 1, "应有通知"

    dlg.close()
    print("NICKNAME REFRESH TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
