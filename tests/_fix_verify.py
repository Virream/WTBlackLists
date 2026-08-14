# -*- coding: utf-8 -*-
"""验证 H1+M2 / M1 / M3 修复。"""
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

from wt81111g.blacklist import BlacklistEntry
from wt81111g.evidence import evidence_folder
from wt81111g.main_window import MainWindow
from wt81111g.server_dialog import ServerSettingsDialog
from wt81111g.settings import AppSettings

app = QApplication([])

# 打桩所有模态弹框(测试环境不阻塞)
QMessageBox.exec = lambda self: QMessageBox.StandardButton.Yes
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)


# ---------------- H1 + M2: 服务器锁定条目不可编辑 ----------------
def test_h1_m2() -> None:
    win = MainWindow(os.path.join(tempfile.mkdtemp(), "b.json"), start_monitor=False)
    e = BlacklistEntry(nickname="服务器条目", player_id="999", remarks="原备注", locked=True)
    win.store.add(e)
    win._make_row(e)
    win.table.setCurrentCell(0, 0)
    QTest.qWait(30)

    # M2: 选中锁定条目 → 备注编辑器应只读
    assert win.remark_editor.isReadOnly(), "锁定条目备注编辑器应只读"

    # M2 数据层: _on_remark_editor_changed 应拒绝修改 locked 条目
    win._remark_locked = False
    win.remark_editor.setPlainText("试图修改")
    QTest.qWait(30)
    assert e.remarks == "原备注", f"锁定条目备注被修改: {e.remarks!r}"

    # H1: 全局锁 锁定再解锁, 服务器锁定行仍应只读
    win._toggle_lock()   # 全局锁
    win._toggle_lock()   # 全局解锁
    QTest.qWait(30)
    w = win._row_widgets.get(id(e))
    assert w is not None
    assert w["nick"].isReadOnly(), "服务器锁定条目 nick 应始终只读(全局解锁后)"
    assert w["remark"].isReadOnly(), "服务器锁定条目 remark 应始终只读(全局解锁后)"
    assert win.remark_editor.isReadOnly(), "锁定条目备注编辑器应始终只读"
    win.close()
    print("H1+M2 OK")


# ---------------- M1: 删除带证据条目后备注编辑器被清理 ----------------
def test_m1() -> None:
    tmp = tempfile.mkdtemp()
    win = MainWindow(os.path.join(tmp, "b.json"), start_monitor=False)
    e = BlacklistEntry(nickname="n", player_id="888",
                       event_date="2026-08-14", remarks="备注内容")
    win.store.add(e)
    win._make_row(e)
    # 生成 entry_id(需 player_id + event_date)
    e.entry_id = e.generate_entry_id()
    win.store.save()
    # 创建真实证据文件夹, 确保 removed_ev>0 走原"提前 return"分支
    eid = e.entry_id
    folder = evidence_folder("888", eid)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "s.jpg"), "w") as f:
        f.write("x")

    # 选中该行 → 备注编辑器加载
    win.table.setCurrentCell(0, 0)
    QTest.qWait(30)
    assert win.remark_editor.toPlainText() == "备注内容"

    # 打桩确认框返回 Yes
    QMessageBox.exec = lambda self: QMessageBox.StandardButton.Yes
    win._row_widgets[id(e)]["check"].setChecked(True)
    win._delete_selected()
    QTest.qWait(30)

    # 备注编辑器应被清空(M1 修复: 不再提前 return)
    assert win.remark_editor.toPlainText() == "", "删除后备注编辑器应被清空"
    assert win._remark_entry is None
    assert not os.path.exists(folder), "证据文件夹应被删除"
    win.close()
    print("M1 OK")


# ---------------- M3: 登录验证期间删除服务器不越界崩溃 ----------------
def test_m3() -> None:
    settings = AppSettings(os.path.join(tempfile.mkdtemp(), "s.json"))
    dlg = ServerSettingsDialog(settings, None)
    # 空列表: row=0 越界 → 应直接 return, 不抛 IndexError
    dlg._apply_login_result(0, "user", "token", {"token": "x"}, "")
    # 有 1 个服务器但 row 越界
    settings.audit_servers.append({"url": "https://github.com/a/b"})
    dlg._apply_login_result(5, "user", "token", {"token": "x"}, "")
    # 合法 row → 正常处理
    settings.audit_servers.append({"url": "https://github.com/c/d"})
    dlg._apply_login_result(1, "user", "token", {"token": "x"}, "")
    dlg.close()
    print("M3 OK")


test_h1_m2()
test_m1()
test_m3()
print("FIX VERIFY PASSED")
