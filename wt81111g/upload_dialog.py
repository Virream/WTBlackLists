"""上传对话框: 审核员勾选要上传到哪些仓库。"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)


class UploadDialog(QDialog):
    def __init__(self, servers: list[dict], parent=None):
        super().__init__(parent)
        self._servers = servers
        self._boxes: list[QCheckBox] = []
        self.setWindowTitle("选择上传仓库")
        self.resize(560, 320)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("勾选要上传到的审核服务器(已登录, 可多选):"))

        for s in servers:
            name = s.get("username") or s.get("url", "")
            cb = QCheckBox(f"{name}  ({s.get('url','')})")
            cb.setChecked(True)
            self._boxes.append(cb)
            lay.addWidget(cb)

        btn_row = QHBoxLayout()
        ok = QPushButton("上传")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btn_row.addStretch(1)
        btn_row.addWidget(ok)
        btn_row.addWidget(cancel)
        lay.addLayout(btn_row)

    def selected_servers(self) -> list[dict]:
        return [
            s for s, cb in zip(self._servers, self._boxes) if cb.isChecked()
        ]
