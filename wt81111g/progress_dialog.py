"""通用模态进度窗口: 后台任务执行时显示进度/当前文件/统计, 阻塞主界面操作。"""
from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QDialog, QLabel, QProgressBar, QVBoxLayout

from .import_export import format_size


class ProgressDialog(QDialog):
    """模态进度窗。后台线程通过 emit progress_updated / task_finished 驱动更新。"""

    progress_updated = pyqtSignal(int, int, str, int)  # done, total, name, size
    task_finished = pyqtSignal(object, str)            # stats 或 None, error

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(480)
        self.stats = None
        self.error = ""

        lay = QVBoxLayout(self)
        self._file_label = QLabel("准备中…")
        self._file_label.setWordWrap(True)
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._stat_label = QLabel("")
        lay.addWidget(self._file_label)
        lay.addWidget(self._bar)
        lay.addWidget(self._stat_label)

        self.progress_updated.connect(self._on_progress)
        self.task_finished.connect(self._on_finished)

    # ------------------------------------------------------------------
    def _on_progress(self, done: int, total: int, name: str, size: int) -> None:
        if total > 0:
            self._bar.setMaximum(total)
            self._bar.setValue(max(0, min(done, total)))
        self._file_label.setText(f"当前: {name}")
        size_txt = f"  ({format_size(size)})" if size else ""
        self._stat_label.setText(f"已完成 {done}/{total}{size_txt}")

    def _on_finished(self, stats, error) -> None:
        self.stats = stats
        self.error = error
        self.accept()
