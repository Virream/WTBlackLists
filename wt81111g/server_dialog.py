"""服务器设置对话框: 左侧名单拉取服务器, 右侧审核服务器(带登录/√标记)。"""
from __future__ import annotations

import threading

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from .account_dialog import AccountManagerDialog
from .login_dialog import LoginDialog
from .server_sync import parse_repo_url, platform_of, verify_cert, verify_login, verify_password


class ServerSettingsDialog(QDialog):
    # 登录完成信号: (row, username 或 None, 方式名, 参数字典, 错误信息)
    _login_done = pyqtSignal(int, object, str, dict, str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("服务器设置")
        self.resize(880, 480)

        lay = QHBoxLayout(self)

        # ---------------- 左: 名单拉取服务器 ----------------
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(QLabel("名单拉取服务器(公开仓库, 无需登录)"))
        self._fetch_list = QListWidget()
        self._reload_fetch()
        ll.addWidget(self._fetch_list, 1)
        fr = QHBoxLayout()
        add = QPushButton("➕ 添加")
        add.clicked.connect(self._add_fetch)
        rm = QPushButton("🗑 删除选中")
        rm.clicked.connect(self._remove_fetch)
        fr.addWidget(add)
        fr.addWidget(rm)
        fr.addStretch(1)
        ll.addLayout(fr)
        hint = QLabel("仅支持 GitHub / Gitee 仓库, 如:\nhttps://github.com/user/repo")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8a8a9a;font-size:11px;")
        ll.addWidget(hint)
        lay.addWidget(left, 1)

        # ---------------- 右: 审核服务器 ----------------
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(QLabel("审核服务器(需登录获取仓库写入权限)"))
        self._audit_list = QListWidget()
        self._audit_list.currentRowChanged.connect(self._refresh_audit_buttons)
        rl.addWidget(self._audit_list, 1)
        ar = QHBoxLayout()
        a_add = QPushButton("➕ 添加")
        a_add.clicked.connect(self._add_audit)
        self._a_login = QPushButton("🔑 登录")
        self._a_login.clicked.connect(self._login_audit)
        self._a_account = QPushButton("👤 账号管理")
        self._a_account.setToolTip("删除已登录的账号 / 证书")
        self._a_account.clicked.connect(self._manage_accounts)
        self._a_rm = QPushButton("🗑 删除选中")
        self._a_rm.clicked.connect(self._remove_audit)
        ar.addWidget(a_add)
        ar.addWidget(self._a_login)
        ar.addWidget(self._a_account)
        ar.addWidget(self._a_rm)
        ar.addStretch(1)
        rl.addLayout(ar)
        rl.addWidget(QLabel("√ 表示该仓库已登录且有权限。登录支持: Token / 账号密码 / 证书(SSH)"))  # noqa: RUF001
        lay.addWidget(right, 1)
        self._reload_audit()  # 需在按钮创建后调用(内部引用按钮)
        self._login_done.connect(self._apply_login_result)  # 只连接一次, 避免重复
        # 默认焦点放到列表而不是"添加"按钮, 避免误触
        self._fetch_list.setFocus()
        self.setTabOrder(self._fetch_list, self._audit_list)

    # ---------------- 拉取服务器 ----------------
    def _reload_fetch(self) -> None:
        self._fetch_list.clear()
        for s in self._settings.fetch_servers:
            item = QListWidgetItem(s.get("url", ""))
            item.setToolTip(s.get("url", ""))
            self._fetch_list.addItem(item)

    def _add_fetch(self) -> None:
        url, ok = QInputDialog.getText(self, "添加拉取服务器", "GitHub/Gitee 仓库地址:")
        url = (url or "").strip()
        if not ok or not url:
            return
        if not parse_repo_url(url):
            QMessageBox.warning(self, "地址无效", "仅支持 GitHub / Gitee 仓库地址")
            return
        self._settings.fetch_servers.append({
            "url": url,
            "platform": platform_of(url),
            "name": url,
        })
        self._settings.save()
        self._reload_fetch()

    def _remove_fetch(self) -> None:
        row = self._fetch_list.currentRow()
        if row < 0:
            return
        del self._settings.fetch_servers[row]
        self._settings.save()
        self._reload_fetch()

    # ---------------- 审核服务器 ----------------
    def _reload_audit(self) -> None:
        self._audit_list.clear()
        for s in self._settings.audit_servers:
            mark = "√ " if s.get("logged_in") else "   "
            name = s.get("username") or s.get("url", "")
            item = QListWidgetItem(f"{mark}{name}")
            item.setToolTip(s.get("url", ""))
            self._audit_list.addItem(item)
        self._refresh_audit_buttons()

    def _refresh_audit_buttons(self) -> None:
        row = self._audit_list.currentRow()
        if 0 <= row < len(self._settings.audit_servers):
            s = self._settings.audit_servers[row]
            self._a_login.setEnabled(True)
            self._a_account.setEnabled(True)
            self._a_rm.setEnabled(True)
            self._a_login.setText("🔑 登录" if not s.get("logged_in") else "🔑 重新登录")
        else:
            self._a_login.setEnabled(False)
            self._a_account.setEnabled(False)
            self._a_rm.setEnabled(False)

    def _add_audit(self) -> None:
        url, ok = QInputDialog.getText(self, "添加审核服务器", "GitHub/Gitee 仓库地址:")
        url = (url or "").strip()
        if not ok or not url:
            return
        if not parse_repo_url(url):
            QMessageBox.warning(self, "地址无效", "仅支持 GitHub / Gitee 仓库地址")
            return
        self._settings.audit_servers.append({
            "url": url,
            "platform": platform_of(url),
            "name": url,
            "token": "",
            "cert_path": "",
            "auth_method": "",
            "logged_in": False,
            "username": "",
        })
        self._settings.save()
        self._reload_audit()

    def _remove_audit(self) -> None:
        row = self._audit_list.currentRow()
        if row < 0:
            return
        del self._settings.audit_servers[row]
        self._settings.save()
        self._reload_audit()

    # ---------------- 登录 ----------------
    def _manage_accounts(self) -> None:
        dlg = AccountManagerDialog(self._settings, self)
        dlg.exec()
        self._reload_audit()

    def _login_audit(self) -> None:
        row = self._audit_list.currentRow()
        if row < 0 or row >= len(self._settings.audit_servers):
            return
        s = self._settings.audit_servers[row]
        plat = s.get("platform", platform_of(s.get("url", "")))
        url = s.get("url", "")
        dlg = LoginDialog(plat, url, self)
        if dlg.exec() != LoginDialog.DialogCode.Accepted:
            return
        method, params = dlg.selected_method()

        self._a_login.setEnabled(False)
        self._a_login.setText("⏳ 验证中...")

        def work() -> None:
            try:
                if method == "token":
                    username = verify_login(plat, params.get("token", ""))
                elif method == "password":
                    username = verify_password(
                        plat, params.get("username", ""), params.get("password", "")
                    )
                else:
                    username = verify_cert(plat, params.get("cert_path", ""))
                err = ""
            except Exception as exc:  # noqa: BLE001
                username = None
                err = str(exc)
            self._login_done.emit(row, username, method, params, err)

        threading.Thread(target=work, daemon=True).start()

    def _apply_login_result(self, row: int, username: str | None,
                            method: str, params: dict, err: str) -> None:
        if row < 0 or row >= len(self._settings.audit_servers):
            return  # 验证期间该服务器已被删除, 忽略结果
        if username:
            s = self._settings.audit_servers[row]
            # 清空旧登录信息后按方式保存
            s["token"] = params.get("token", "") if method == "token" else ""
            s["cert_path"] = params.get("cert_path", "") if method == "cert" else ""
            s["auth_method"] = method
            s["logged_in"] = True
            s["username"] = username
            self._settings.save()
            self._reload_audit()
            self._audit_list.setCurrentRow(row)
            QMessageBox.information(
                self, "登录成功", f"已登录: {username}\n该仓库现可推送/删除"
            )
        else:
            self._refresh_audit_buttons()
            QMessageBox.warning(self, "登录失败", err or "登录失败, 请检查输入与权限")
