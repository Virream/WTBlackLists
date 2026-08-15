"""共享昵称表同步对话框: 拉取合并到本地 + 开关网络同步 + issue 上传。"""
from __future__ import annotations

import threading

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QLabel, QPushButton, QVBoxLayout,
)

from .nickname_cache import NicknameCache
from .nickname_sync import (
    collect_pending, fetch_shared_table, find_github_token,
    merge_shared_into_cache, submit_issue,
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

        # 网络同步开关
        self.sync_check = QCheckBox("启用网络同步(上传抓取到的昵称到共享表, 需登录 GitHub)")
        self.sync_check.setChecked(bool(settings.sync_enabled))
        self.sync_check.toggled.connect(self._on_toggle)
        lay.addWidget(self.sync_check)

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
    def _refresh_login_hint(self) -> None:
        if not self._settings.sync_enabled:
            self.login_hint.setText("网络同步已关闭")
            return
        if not find_github_token(self._settings):
            self.login_hint.setText("⚠ 未登录 GitHub, 无法上传; 请先在「服务器设置」中登录")
            self.upload_btn.setEnabled(False)
        else:
            self.login_hint.setText("已登录 GitHub, 可上传")
            self.upload_btn.setEnabled(True)

    def _on_toggle(self, checked: bool) -> None:
        self._settings.sync_enabled = checked
        self._settings.save()
        self._refresh_login_hint()

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
        self.upload_btn.setEnabled(True)

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
        if not self._settings.sync_enabled:
            self.status.setText("请先勾选「启用网络同步」")
            return
        token = find_github_token(self._settings)
        if not token:
            self.status.setText("未登录 GitHub, 无法上传; 请在服务器设置中登录")
            return
        url = self._repo()
        if not url:
            self.status.setText("未配置仓库")
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
