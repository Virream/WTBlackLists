"""共享昵称表同步对话框: 拉取合并到本地 + 开关网络同步 + issue 上传。"""
from __future__ import annotations

import threading

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from .nickname_cache import NicknameCache
from .nickname_sync import (
    collect_pending, fetch_shared_table, merge_shared_into_cache, submit_issue,
)
from .settings import AppSettings


class NicknameSyncDialog(QDialog):
    """共享昵称表: 拉取 / 开关 / 上传(通过 GitHub issue)。"""

    _done = pyqtSignal(str)  # 后台线程完成 → 状态消息

    def __init__(self, settings: AppSettings, cache: NicknameCache, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._cache = cache
        self.setWindowTitle("共享昵称表")
        self.setModal(True)
        self.setMinimumWidth(460)
        self._done.connect(self._show_status)

        lay = QVBoxLayout(self)

        desc = QLabel(
            "共享表存于公开仓库的 nickname.json。\n"
            "• 拉取: 把别人已验证的昵称合并进本地缓存(WTLive/官网兜底)。\n"
            "• 上传: 你通过官网兜底抓到的昵称, 可提交到共享表帮助其他玩家。"
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#b8c4d4;")
        lay.addWidget(desc)

        # 上传服务器选择(从服务器设置已添加的审核服务器中选, 用对应登录账号发 issue)
        srv_row = QHBoxLayout()
        srv_row.addWidget(QLabel("上传服务器:"))
        self.server_combo = QComboBox()
        self.server_combo.setToolTip("选择用哪个已登录账号提交 issue")
        for s in settings.audit_servers:
            name = s.get("name") or s.get("url") or "(未命名)"
            if s.get("logged_in") and s.get("token"):
                label = f"{name} ({s.get('username') or '已登录'})"
            else:
                label = f"{name} (未登录)"
            self.server_combo.addItem(label, s)
        self.server_combo.currentIndexChanged.connect(self._refresh_login_hint)
        srv_row.addWidget(self.server_combo, 1)
        lay.addLayout(srv_row)

        self.login_hint = QLabel("")
        self.login_hint.setStyleSheet("color:#e67e22;")
        lay.addWidget(self.login_hint)

        # 按钮行
        row = QVBoxLayout()
        self.pull_btn = QPushButton("🔽 拉取共享表并合并到本地")
        self.pull_btn.clicked.connect(self._pull)
        row.addWidget(self.pull_btn)
        self.upload_btn = QPushButton("⬆️ 上传待同步昵称(提交 issue)")
        self.upload_btn.clicked.connect(self._upload)
        row.addWidget(self.upload_btn)
        lay.addLayout(row)

        self.status = QLabel("就绪")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#7ec8e3;")
        lay.addWidget(self.status)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        lay.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignHCenter)

        self._refresh_login_hint()

    # ------------------------------------------------------------------
    def _selected_server(self) -> dict | None:
        idx = self.server_combo.currentIndex()
        if idx < 0:
            return None
        return self.server_combo.itemData(idx)

    def _refresh_login_hint(self) -> None:
        s = self._selected_server()
        if s is None:
            self.login_hint.setText("⚠ 未添加审核服务器, 请先在「服务器设置」中添加")
            self.upload_btn.setEnabled(False)
            return
        if not (s.get("logged_in") and s.get("token")):
            self.login_hint.setText("⚠ 当前服务器未登录, 无法上传; 请先在「服务器设置」中登录")
            self.upload_btn.setEnabled(False)
        else:
            self.login_hint.setText(
                f"将以 {s.get('username') or '该账号'} 的账号提交 issue")
            self.upload_btn.setEnabled(True)

    def _repo(self) -> str | None:
        """上传/拉取用仓库: 优先第一个拉取服务器, 否则第一个已登录审核服务器。"""
        if self._settings.fetch_servers:
            url = self._settings.fetch_servers[0].get("url", "")
            if url:
                return url
        for s in self._settings.audit_servers:
            if s.get("url"):
                return str(s.get("url"))
        return None

    def _background(self, fn) -> None:
        def wrap() -> None:
            try:
                msg = fn()
            except Exception as exc:  # noqa: BLE001
                msg = f"错误: {exc}"
            self._done.emit(msg or "完成")
        threading.Thread(target=wrap, daemon=True).start()

    def _show_status(self, msg: str) -> None:
        self.status.setText(msg)
        self.pull_btn.setEnabled(True)
        # 上传按钮状态由所选服务器登录态决定, 避免覆盖禁用状态
        self._refresh_login_hint()

    # ------------------------------------------------------------------
    def _pull(self) -> None:
        url = self._repo()
        if not url:
            self.status.setText("未配置仓库, 请先在「服务器设置」中添加 GitHub/Gitee 仓库")
            return
        self.pull_btn.setEnabled(False)
        self.status.setText("拉取中…")
        self._background(lambda: self._do_pull(url))

    def _do_pull(self, url: str) -> str:
        remote = fetch_shared_table(url)
        if not remote:
            return "共享表为空或仓库无 nickname.json(可能是首次部署)"
        added, updated = merge_shared_into_cache(remote, self._cache)
        return f"拉取完成: 新增 {added} 条, 更新 {updated} 条"

    def _upload(self) -> None:
        s = self._selected_server()
        if s is None:
            self.status.setText("未添加审核服务器, 请先在「服务器设置」中添加")
            self._refresh_login_hint()
            return
        url = str(s.get("url") or "").strip()
        token = str(s.get("token") or "").strip()
        if not (s.get("logged_in") and url and token):
            self.status.setText("当前服务器未登录, 无法上传; 请先在「服务器设置」中登录")
            self._refresh_login_hint()
            return
        self.upload_btn.setEnabled(False)
        self.status.setText("对比共享表并提交中…")
        self._background(lambda: self._do_upload(url, token))

    def _do_upload(self, url: str, token: str) -> str:
        remote = fetch_shared_table(url)
        pending = collect_pending(remote, self._cache)
        if not pending:
            return "没有待上传的昵称(共享表已包含你本地的新昵称)"
        number, html = submit_issue(url, token, pending)
        return f"已提交 issue #{number}:\n{html}\n服务端处理后昵称会进入共享表"
