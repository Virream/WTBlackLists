# -*- coding: utf-8 -*-
"""上传审核请求对话框验证(offscreen)。"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication

from wt81111g.review_request_dialog import ReviewRequestDialog
from wt81111g.settings import AppSettings

app = QApplication([])
d = tempfile.mkdtemp()

REPO = "https://github.com/Virream/WTBlackListsData.git"
ENTRIES = [{
    "player_id": "123", "nickname": "abc", "reason": "x",
    "event_date": "2024-01-01", "replay_link": "https://r", "remarks": "备注",
}]

# 无登录服务器 → 上传禁用 + 提示
settings = AppSettings(os.path.join(d, "config.json"))
settings.audit_servers = [{
    "url": REPO, "platform": "github", "name": "官方",
    "token": "", "logged_in": False,
}]
dlg = ReviewRequestDialog(settings, ENTRIES, None)
assert dlg.table.rowCount() == 1
assert dlg._upload_btn.isEnabled() is False, "无登录服务器应禁用上传"
assert "无法提交" in dlg._count_label.text(), dlg._count_label.text()
dlg.close()

# 已登录服务器 → 可上传, 账号下拉显示 username
settings.audit_servers = [{
    "url": REPO, "platform": "github", "name": "官方",
    "token": "tok", "logged_in": True, "username": "Alice",
}]
dlg2 = ReviewRequestDialog(settings, ENTRIES, None)
assert dlg2._upload_btn.isEnabled() is True, "已登录应可上传"
assert "Alice" in dlg2._account_combo.currentText(), dlg2._account_combo.currentText()
assert dlg2.table.rowCount() == 1
assert dlg2.table.item(0, 0).text() == "123"
assert dlg2.table.item(0, 1).text() == "abc"
dlg2.close()

# 非法条目(缺 reason)被过滤 → 无可上传
bad = [{"player_id": "123", "nickname": "abc", "reason": "",
        "event_date": "", "replay_link": ""}]
dlg3 = ReviewRequestDialog(settings, bad, None)
assert dlg3.table.rowCount() == 0
assert dlg3._upload_btn.isEnabled() is False
dlg3.close()

print("REVIEW REQUEST DIALOG TEST PASSED")
