"""通知记录窗口: 展示软件通知过的各种信息(时间 + 内容)。"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QPushButton, QVBoxLayout,
)


class NotificationsDialog(QDialog):
    """通知历史: 按时间倒序(最新在前)显示 [时间] 内容。"""

    def __init__(self, notifications: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("通知记录")
        self.setModal(True)
        self.setMinimumSize(600, 440)

        lay = QVBoxLayout(self)
        head = QLabel(f"软件通知记录(共 {len(notifications)} 条)")
        head.setStyleSheet("font-weight:bold; color:#ffffff;")
        lay.addWidget(head)

        self._list = QListWidget()
        for ts, msg in reversed(notifications):
            self._list.addItem(f"[{ts}] {msg}")
        lay.addWidget(self._list, 1)

        row = QHBoxLayout()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        row.addStretch(1)
        row.addWidget(close_btn)
        lay.addLayout(row)
