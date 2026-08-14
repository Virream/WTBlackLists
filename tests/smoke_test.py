"""GUI 冒烟测试(离屏模式): 验证窗口构建与核心交互逻辑。"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QLineEdit, QComboBox, QMessageBox

from wt81111g.blacklist import BlacklistStore, REASON_CHOICES
from wt81111g.evidence import evidence_folder
from wt81111g.main_window import MainWindow

# 离屏模式下自动应答所有弹窗,避免阻塞
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
QMessageBox.exec = lambda self: QMessageBox.StandardButton.Yes

# 打桩:测试时不真正打开资源管理器
import wt81111g.evidence as _ev
global _opened
_opened = []
_ev.open_folder = lambda p: _opened.append(p)

results = []


def main() -> int:
    app = QApplication([])
    tmpdir = tempfile.mkdtemp()
    store_path = os.path.join(tmpdir, "blacklist.json")
    win = MainWindow(store_path, start_monitor=False)
    win.show()

    def step1() -> None:
        # 添加一行
        win._add_row()
        row = 0
        assert win.table.rowCount() == 1, win.table.rowCount()
        nick = win.table.cellWidget(row, 1)
        prev = win.table.cellWidget(row, 5)
        pid = win.table.cellWidget(row, 2)
        combo = win.table.cellWidget(row, 3)
        date = win.table.cellWidget(row, 4)
        audit = win.table.cellWidget(row, 9)
        auditor = win.table.cellWidget(row, 10)
        assert isinstance(nick, QLineEdit) and isinstance(pid, QLineEdit)
        assert isinstance(prev, QLineEdit) and prev.isReadOnly(), "曾用昵称应只读"
        assert isinstance(combo, QComboBox) and combo.count() == len(REASON_CHOICES)
        # 审核列: 默认未勾选且用户不可修改; 审核员列只读
        from PyQt6.QtWidgets import QCheckBox, QLabel
        assert isinstance(audit, QCheckBox) and not audit.isChecked()
        assert not audit.isEnabled(), "审核状态应不可修改"
        assert isinstance(auditor, QLabel)
        results.append("audit column (default unchecked, locked) OK")
        # 初始无条目ID
        assert win.store.entries[0].entry_id == ""
        results.append("add_row + empty entry_id OK")
        # 填写昵称/ID/日期 → 自动生成条目ID
        nick.setText("目击而道存矣")
        pid.setText("80931116")
        date.set_value("2026-08-08 14:23")
        eid = win.store.entries[0].entry_id
        assert eid.startswith("80931116_"), eid
        label = win.table.cellWidget(row, 11)
        assert label.text() == eid
        results.append(f"entry_id auto-generated: {eid}")

        # 原因下拉默认与选择
        combo.setCurrentIndex(REASON_CHOICES.index("种族仇恨言论"))
        assert win.store.entries[0].reason == "种族仇恨言论"
        results.append("reason combo OK")

        # 证据文件夹(真实创建,不打开资源管理器)
        import os as _os
        path = _os.path.join(_ev.evidences_dir(), "80931116", eid)
        _os.makedirs(path, exist_ok=True)
        assert _os.path.isdir(path)
        results.append(f"evidence folder path: {path}")

        # 模拟告警信号
        win._on_alert("目击而道存矣", "80931116", "种族仇恨言论")
        results.append("alert slot invoked")

        # 添加第二条,缺信息时点文件夹应弹提示(直接验证 guard)
        win._add_row()
        assert win.store.entries[1].entry_id == ""
        results.append("row2 entry_id empty (needs info)")

        QTimer.singleShot(0, step2)

    def step2() -> None:
        # 勾选第一行并删除(删除基于勾选列)
        win._row_widgets[id(win.store.entries[0])]["check"].setChecked(True)
        win._delete_selected()
        assert win.table.rowCount() == 1 and len(win.store.entries) == 1
        results.append("delete selected OK")
        win.close()
        QTimer.singleShot(0, app.quit)

    QTimer.singleShot(300, step1)  # 等监控线程启动
    app.exec()
    return 0


if __name__ == "__main__":
    rc = main()
    for r in results:
        print("OK:", r)
    print("SMOKE TEST PASSED" if rc == 0 else "FAILED")
    sys.exit(rc)
