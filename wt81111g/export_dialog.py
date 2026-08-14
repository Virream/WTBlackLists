"""导出设置对话框: 是否含证据、保存位置选择、统计预估。"""
from __future__ import annotations

import os
import time

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from .import_export import estimate_export, format_size


def _fmt_time(sec: float) -> str:
    if sec < 60:
        return f"约 {max(1, int(sec))} 秒"
    return f"约 {sec / 60:.1f} 分钟"


class ExportDialog(QDialog):
    def __init__(self, entries, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导出黑名单条目")
        self.setMinimumWidth(520)
        self._entries = entries

        lay = QVBoxLayout(self)
        form = QFormLayout()

        self._include_ev = QCheckBox("导出证据文件")
        self._include_ev.setChecked(False)
        self._include_ev.setToolTip("勾选后会把对应条目的证据文件夹一起打包进导出文件")
        form.addRow("证据文件", self._include_ev)

        path_row = QHBoxLayout()
        default = os.path.join(
            os.path.expanduser("~"),
            f"WTBlackList_导出_{time.strftime('%Y%m%d_%H%M%S')}.zip",
        )
        self._path_edit = QLineEdit(default)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(self._path_edit, 1)
        path_row.addWidget(browse)
        form.addRow("保存位置", path_row)

        self._stat_label = QLabel("")
        self._stat_label.setWordWrap(True)
        form.addRow("统计", self._stat_label)
        lay.addLayout(form)

        btns = QHBoxLayout()
        ok = QPushButton("确定导出")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay.addLayout(btns)

        self._include_ev.toggled.connect(self._refresh_stats)
        self._refresh_stats()

    # ------------------------------------------------------------------
    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "选择导出保存位置", self.out_path() or "", "黑名单导出包 (*.zip)"
        )
        if path:
            self._path_edit.setText(path)

    def _refresh_stats(self) -> None:
        entry_count, total, files, est = estimate_export(
            self._entries, self._include_ev.isChecked()
        )
        self._stat_label.setText(
            f"共 {entry_count} 条条目\n"
            f"导出大小约: {format_size(total)}\n"
            f"证据文件: {files} 个\n"
            f"预计耗时: {_fmt_time(est)}"
        )

    def include_evidence(self) -> bool:
        return self._include_ev.isChecked()

    def out_path(self) -> str:
        return self._path_edit.text().strip()
