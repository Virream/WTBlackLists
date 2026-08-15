# -*- coding: utf-8 -*-
"""共享昵称表对话框验证(offscreen): 上传服务器选择 + 未登录提示。"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication

from wt81111g.nickname_cache import NicknameCache
from wt81111g.nickname_sync_dialog import NicknameSyncDialog
from wt81111g.settings import AppSettings

app = QApplication([])
d = tempfile.mkdtemp()
settings = AppSettings(os.path.join(d, "config.json"))
cache = NicknameCache(os.path.join(d, "nc.json"))

# 首次预置: audit_servers 含官方但未登录
dlg = NicknameSyncDialog(settings, cache, None)
assert dlg.server_combo is not None and dlg.pull_btn is not None
assert dlg.upload_btn is not None and dlg.status is not None
assert not hasattr(dlg, "sync_check"), "启用网络同步开关应已删除"
assert dlg.upload_btn.isEnabled() is False, "未登录应禁用上传"
assert "未登录" in dlg.login_hint.text(), dlg.login_hint.text()
# 未登录点击上传 → 更新下方提示
dlg._upload()
assert "未登录" in dlg.status.text(), dlg.status.text()
# 默认仓库(拉取用)应存在
assert settings.fetch_servers and "Virream/WTBlackLists" in settings.fetch_servers[0]["url"]
print("仓库:", settings.fetch_servers[0]["url"])
dlg.close()

# 已登录审核服务器 → 可上传, 用对应账号
settings.audit_servers = [{
    "url": "https://github.com/Virream/WTBlackLists.git",
    "platform": "github", "name": "官方共享仓库 (Virream/WTBlackLists)",
    "token": "tok", "logged_in": True, "username": "Alice",
}]
dlg2 = NicknameSyncDialog(settings, cache, None)
assert dlg2.upload_btn.isEnabled() is True, "已登录应可上传"
assert "Alice" in dlg2.login_hint.text(), dlg2.login_hint.text()
dlg2.close()

# 未登录服务器 → 禁用 + 提示
settings.audit_servers = [{
    "url": "https://github.com/Virream/WTBlackLists.git",
    "platform": "github", "name": "官方", "token": "", "logged_in": False,
}]
dlg3 = NicknameSyncDialog(settings, cache, None)
assert dlg3.upload_btn.isEnabled() is False
assert "未登录" in dlg3.login_hint.text()
dlg3.close()

print("NICKNAME SYNC DIALOG TEST PASSED")

