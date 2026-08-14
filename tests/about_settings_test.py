"""关于对话框(白字/阴影/分段/客服对话链接) + 客服对话窗口 + first_run + 按钮改名验证。"""
import os
import re
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QLabel,
    QTextBrowser,
)

from wt81111g import about_dialog
from wt81111g.about_dialog import AboutDialog, ConversationDialog, _DESC
from wt81111g.main_window import MainWindow
from wt81111g.settings import AppSettings


def main() -> int:
    app = QApplication([])

    # 1. 关于对话框
    dlg = AboutDialog()
    label = dlg.findChildren(QLabel)[0]
    txt = label.text()
    assert "color:#ffffff" in txt, "白字缺失"
    assert "&emsp;&emsp;" in txt, "制表符缩进缺失"
    assert "点击查看作者与gaijin客服的对话" in txt, "客服对话链接缺失"
    assert "已向Gaijin客服进行过确认" not in txt, "应已删除该表述"
    n_semi = len(re.findall(r"[;；]", _DESC))
    n_paras = txt.count('<p style="margin:4px 0; color:#ffffff;">&emsp;&emsp;')
    assert n_paras == n_semi + 2, (n_paras, n_semi)  # 分句段数 + 客服对话链接段
    effect = label.graphicsEffect()
    assert isinstance(effect, QGraphicsDropShadowEffect), "阴影缺失"
    print(f"about dialog OK: paras={n_paras} shadow={type(effect).__name__}")

    # 2. 客服对话窗口内容
    cd = ConversationDialog()
    browser = cd.findChildren(QTextBrowser)[0]
    plain = browser.toPlainText()
    assert "尊敬的管理员你好" in plain, "提问缺失"
    assert "祝您生活愉快" in plain, "提问结尾缺失"
    assert "Support Specialist" in plain, "客服落款缺失"
    assert "termsofservice" in plain, "ToS 链接缺失"
    print("conversation dialog OK")

    # 3. 链接路由: about:conversation -> 打开对话窗口
    calls = []
    original = about_dialog.ConversationDialog

    class _FakeDialog:
        def __init__(self, parent=None):
            calls.append("open")

        def exec(self):
            calls.append("exec")

    about_dialog.ConversationDialog = _FakeDialog
    dlg._on_link("about:conversation")
    assert calls == ["open", "exec"], calls
    about_dialog.ConversationDialog = original
    print("link routing OK")

    # 4. first_run 持久化
    td = tempfile.mkdtemp()
    s = AppSettings(os.path.join(td, "c.json"))
    assert s.first_run is True
    s.first_run = False
    s.save()
    s2 = AppSettings(os.path.join(td, "c.json"))
    assert s2.first_run is False
    print("first_run persistence OK")

    # 5. 按钮改名
    win = MainWindow(os.path.join(tempfile.mkdtemp(), "bl.json"), start_monitor=False)
    actions = win.findChildren(QAction)
    assert any("刷新ID对应昵称" in a.text() for a in actions), "按钮未改名"
    print("button renamed OK")

    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
