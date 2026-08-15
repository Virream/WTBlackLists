"""代理设置对话框: 设置后软件所有网络请求都通过代理发送。"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout,
)

from .proxy_config import set_proxy


class ProxyDialog(QDialog):
    """代理设置: 地址(支持 http/https/socks), 留空=不使用代理直连。"""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("代理设置")
        self.setMinimumWidth(440)

        lay = QVBoxLayout(self)
        tip = QLabel(
            "设置后, 软件的所有网络请求(WT Live/官网/服务器同步/共享昵称表)\n"
            "都会通过代理发送。格式示例: 127.0.0.1:7890 或 http://127.0.0.1:7890,\n"
            "留空则直连。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#8a8a8a;")
        lay.addWidget(tip)

        form = QFormLayout()
        self._edit = QLineEdit(settings.proxy)
        self._edit.setPlaceholderText("127.0.0.1:7890")
        form.addRow("代理地址:", self._edit)
        lay.addLayout(form)

        row = QHBoxLayout()
        self._save_btn = QPushButton("保存")
        self._save_btn.clicked.connect(self._save)
        self._clear_btn = QPushButton("清除代理")
        self._clear_btn.clicked.connect(self._clear)
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(self._save_btn)
        row.addWidget(self._clear_btn)
        row.addWidget(self._cancel_btn)
        lay.addLayout(row)

    def _apply(self, url: str) -> None:
        self._settings.proxy = url
        self._settings.save()
        set_proxy(url)

    def _save(self) -> None:
        self._apply(self._edit.text().strip())
        self.accept()

    def _clear(self) -> None:
        self._edit.clear()
        self._apply("")
        self.accept()
