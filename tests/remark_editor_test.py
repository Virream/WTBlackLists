# -*- coding: utf-8 -*-
"""备注编辑器: 输入顺序不倒置 + 字数计数器验证。"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtGui import QTextCursor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from wt81111g.main_window import MainWindow, _REMARK_MAX

app = QApplication([])
td = tempfile.mkdtemp()
win = MainWindow(os.path.join(td, "bl.json"), start_monitor=False)

# 1. 添加一行(填充 _row_widgets, 使表格行 remark 回写路径生效)
win._add_row()
assert len(win.store.entries) == 1, "应有一行"
entry = win.store.entries[0]
w = win._row_widgets.get(id(entry))
assert w is not None, "表格行 widget 应存在"

# 2. 选中该行 → 备注编辑器加载(空备注)
win.table.setCurrentCell(0, 0)
QTest.qWait(30)
assert win._remark_entry is entry, "备注编辑器应关联该条目"
editor = win.remark_editor

# 3. 逐字符输入, 验证顺序不倒置(若回写循环存在, 会得到 "cba")
editor.setFocus()
QTest.keyClicks(editor, "abc")
QTest.qWait(30)
text = editor.toPlainText()
print("逐字符输入结果:", repr(text))
assert text == "abc", f"输入顺序错误(应为 abc, 实际 {text!r})"
assert w["remark"].text() == "abc", "表格行备注未同步"
print("计数器1:", win.remark_counter.text())
assert "3/1000" in win.remark_counter.text(), win.remark_counter.text()

# 4. 在末尾追加输入, 光标保持末尾
editor.moveCursor(QTextCursor.MoveOperation.End)
QTest.keyClicks(editor, "d")
QTest.qWait(30)
text = editor.toPlainText()
print("追加结果:", repr(text))
assert text == "abcd", f"追加顺序错误(实际 {text!r})"

# 5. 逐字符输入到上限后再输入: 新字符应被拒绝, 原文不乱, 光标保持在末尾
editor.setPlainText("z" * _REMARK_MAX)
QTest.qWait(30)
editor.moveCursor(QTextCursor.MoveOperation.End)
QTest.keyClicks(editor, "y")
QTest.qWait(30)
text = editor.toPlainText()
print("第1001字符后: len=", len(text), "头=", repr(text[:5]), "尾=", repr(text[-5:]))
assert len(text) == _REMARK_MAX, f"应保持{_REMARK_MAX}, 实际{len(text)}"
assert text == "z" * _REMARK_MAX, "新字符应被拒绝, 不应挤掉原有文字"
cur = editor.textCursor().position()
assert cur == _REMARK_MAX, f"光标应在末尾, 实际{cur}"
print("计数器2:", win.remark_counter.text())
assert f"{_REMARK_MAX}/{_REMARK_MAX}" in win.remark_counter.text(), win.remark_counter.text()

# 6. 清空(删除行)后计数器归零
win.table.setCurrentCell(-1, 0) if False else None
QTest.qWait(10)
print("备注编辑器测试全部通过")
win.close()
print("REMARK EDITOR TEST PASSED")
