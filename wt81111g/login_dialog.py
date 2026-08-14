"""登录方式选择对话框: Token / 账号密码 / 证书(SSH)。"""
from __future__ import annotations

import os

from PyQt6.QtWidgets import (
    QButtonGroup, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QRadioButton, QStackedWidget, QVBoxLayout, QWidget,
)


class LoginDialog(QDialog):
    """提供多种登录方式供用户选择。exec 返回后通过 selected_method() 获取。"""

    def __init__(self, platform: str, repo_url: str, parent=None):
        super().__init__(parent)
        self._platform = platform
        self.setWindowTitle(f"登录审核服务器 - {platform.upper()}")
        self.resize(520, 320)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"仓库: {repo_url}\n请选择登录方式:"))

        # ---- 方式单选 ----
        self._rb_token = QRadioButton("Token 登录(推荐)")
        self._rb_password = QRadioButton("账号密码登录")
        self._rb_cert = QRadioButton("证书(SSH Key)登录")
        self._rb_token.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self._rb_token)
        group.addButton(self._rb_password)
        group.addButton(self._rb_cert)
        lay.addWidget(self._rb_token)
        lay.addWidget(self._rb_password)
        lay.addWidget(self._rb_cert)
        group.buttonClicked.connect(self._switch)

        # ---- 各方式输入区 ----
        self._stack = QStackedWidget()

        # Token
        w1 = QWidget()
        l1 = QVBoxLayout(w1)
        self._token_edit = QLineEdit()
        self._token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._token_edit.setPlaceholderText(
            "GitHub Personal Access Token / Gitee 私人令牌 (需 repo 写权限)"
        )
        l1.addWidget(QLabel("Token:"))
        l1.addWidget(self._token_edit)
        self._stack.addWidget(w1)

        # 账号密码
        w2 = QWidget()
        l2 = QVBoxLayout(w2)
        self._user_edit = QLineEdit()
        self._user_edit.setPlaceholderText("账号 / 邮箱")
        self._pass_edit = QLineEdit()
        self._pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._pass_edit.setPlaceholderText("密码")
        l2.addWidget(QLabel("账号:"))
        l2.addWidget(self._user_edit)
        l2.addWidget(QLabel("密码:"))
        l2.addWidget(self._pass_edit)
        note = QLabel(
            "GitHub 已停用密码直连; Gitee 账号密码登录需开放平台应用凭据。\n"
            "若失败建议改用 Token 或证书。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#8a8a9a;font-size:11px;")
        l2.addWidget(note)
        self._stack.addWidget(w2)

        # 证书
        w3 = QWidget()
        l3 = QVBoxLayout(w3)
        row = QHBoxLayout()
        self._cert_edit = QLineEdit()
        self._cert_edit.setPlaceholderText("SSH 私钥文件路径 (如 C:\\Users\\xxx\\.ssh\\id_ed25519)")
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_cert)
        row.addWidget(self._cert_edit, 1)
        row.addWidget(browse)
        l3.addWidget(QLabel("私钥文件:"))
        l3.addLayout(row)
        self._stack.addWidget(w3)

        lay.addWidget(self._stack)

        # 按钮
        btns = QHBoxLayout()
        ok = QPushButton("登录")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        lay.addLayout(btns)

    def _switch(self) -> None:
        if self._rb_token.isChecked():
            self._stack.setCurrentIndex(0)
        elif self._rb_password.isChecked():
            self._stack.setCurrentIndex(1)
        else:
            self._stack.setCurrentIndex(2)

    def _browse_cert(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 SSH 私钥文件")
        if path:
            self._cert_edit.setText(path)

    def selected_method(self) -> tuple[str, dict]:
        """返回 (方式名, 参数字典)。方式: token / password / cert。"""
        if self._rb_token.isChecked():
            return "token", {"token": self._token_edit.text().strip()}
        if self._rb_password.isChecked():
            return "password", {
                "username": self._user_edit.text().strip(),
                "password": self._pass_edit.text(),
            }
        return "cert", {"cert_path": self._cert_edit.text().strip()}
