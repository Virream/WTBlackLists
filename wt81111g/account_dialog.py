"""账号管理对话框: 列出已登录账号与证书, 支持删除。"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QVBoxLayout,
)

_METHOD_NAMES = {"token": "Token", "password": "账号密码", "cert": "证书(SSH)"}


def _server_label(s: dict) -> str:
    url = s.get("url", "")
    platform = s.get("platform", "")
    username = s.get("username", "")
    method = _METHOD_NAMES.get(s.get("auth_method", "token"), "Token")
    if s.get("logged_in"):
        return f"√ {username}  ({platform.upper()} · {method} · {url})"
    return f"  {url}  (未登录)"


class AccountManagerDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("账号管理")
        self.resize(640, 360)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("已登录账号 / 证书(点击选中后可删除):"))
        self._list = QListWidget()
        self._reload()
        lay.addWidget(self._list, 1)

        row = QHBoxLayout()
        remove = QPushButton("🗑 删除选中登录信息")
        remove.clicked.connect(self._remove_selected)
        close = QPushButton("完成")
        close.clicked.connect(self.accept)
        row.addWidget(remove)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)

    def _reload(self) -> None:
        self._list.clear()
        for s in self._settings.audit_servers:
            item = QListWidgetItem(_server_label(s))
            item.setToolTip(s.get("url", ""))
            item.setData(Qt.ItemDataRole.UserRole, id(s))
            self._list.addItem(item)

    def _remove_selected(self) -> None:
        row = self._list.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先选中一个账号")
            return
        if row >= len(self._settings.audit_servers):
            return
        s = self._settings.audit_servers[row]
        if not s.get("logged_in"):
            QMessageBox.information(self, "提示", "该服务器未登录, 无需删除")
            return
        name = s.get("username") or s.get("url", "")
        ret = QMessageBox.question(
            self, "确认删除",
            f"确定删除账号「{name}」的登录信息(token/证书/账号密码)吗?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        # 清除登录相关信息, 保留服务器配置
        s["token"] = ""
        s["cert_path"] = ""
        s["auth_method"] = ""
        s["logged_in"] = False
        s["username"] = ""
        self._settings.save()
        self._reload()
        QMessageBox.information(self, "完成", "已删除登录信息, 该仓库需重新登录")
