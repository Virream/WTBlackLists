# -*- coding: utf-8 -*-
"""共享昵称表对话框验证(offscreen)。"""
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

dlg = NicknameSyncDialog(settings, cache, None)
assert dlg.sync_check is not None and dlg.pull_btn is not None
assert dlg.upload_btn is not None and dlg.status is not None

# 开关状态与 settings 同步
assert dlg.sync_check.isChecked() == settings.sync_enabled
dlg.sync_check.setChecked(True)
assert settings.sync_enabled is True, "开关应写回 settings"
# 重新加载确认持久化
s2 = AppSettings(os.path.join(d, "config.json"))
assert s2.sync_enabled is True, "sync_enabled 应持久化"

# 默认仓库(首次预置)应存在
assert settings.fetch_servers and "Virream/WTBlackLists" in settings.fetch_servers[0]["url"]
print("仓库:", settings.fetch_servers[0]["url"])

dlg.close()
print("NICKNAME SYNC DIALOG TEST PASSED")
