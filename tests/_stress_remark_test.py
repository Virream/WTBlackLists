# -*- coding: utf-8 -*-
"""压力测试: 快速乱搓输入大量字符, 复现备注编辑器崩溃。"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from wt81111g.main_window import MainWindow, _REMARK_MAX

app = QApplication([])
win = MainWindow(os.path.join(tempfile.mkdtemp(), "bl.json"), start_monitor=False)
win._add_row()
win.table.setCurrentCell(0, 0)
QTest.qWait(30)
editor = win.remark_editor
editor.setFocus()

chars = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
for i in range(4000):
    QTest.keyClicks(editor, chars[i % len(chars)])
    if i % 500 == 0:
        QTest.qWait(5)

QTest.qWait(300)
print("最终长度:", len(editor.toPlainText()))
assert len(editor.toPlainText()) <= _REMARK_MAX
print("STRESS OK")
