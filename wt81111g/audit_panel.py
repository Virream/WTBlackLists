"""审核功能区: 审核员昵称选择、上传到服务器、从服务器删除。"""
from __future__ import annotations

import threading

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from .server_sync import delete_entries, upload_entries


class AuditPanel(QGroupBox):
    # 信号: upload_started() / delete_started() 供主窗口禁用界面
    upload_started = pyqtSignal()
    upload_finished = pyqtSignal(object)  # 上传结果 dict 或 None
    delete_started = pyqtSignal()
    delete_finished = pyqtSignal(object)
    retry_notice = pyqtSignal(str, str)  # (操作名, 重试提示) 供主窗口提醒用户
    review_pulled = pyqtSignal(object)   # 拉取的待审核条目 dict 或 None
    review_error = pyqtSignal(str)       # 拉取失败原因

    def __init__(self, main_window, parent=None):
        super().__init__("审核功能区", parent)
        self._mw = main_window
        self.setStyleSheet("QGroupBox { font-weight: bold; }")

        lay = QVBoxLayout(self)

        # 审核员昵称(从已登录账号选择, 不允许自定义)
        row0 = QHBoxLayout()
        row0.addWidget(QLabel("审核员昵称:"))
        self._auditor_combo = QComboBox()
        self._auditor_combo.setToolTip("只能从已登录的审核服务器账号中选择, 不可自定义")
        row0.addWidget(self._auditor_combo, 1)
        lay.addLayout(row0)

        # 拉取审核请求(放在上传/删除上方)
        self._pull_review_btn = QPushButton("📥 拉取审核请求")
        self._pull_review_btn.setToolTip(
            "从待审核队列拉取 1 条(自动标记为正在审核, 防止多人拉同一条);\n"
            "一次只能审核一条, 审核完上传后自动移除待审核请求"
        )
        self._pull_review_btn.clicked.connect(self._pull_review)
        lay.addWidget(self._pull_review_btn)

        # 上传 / 删除
        row1 = QHBoxLayout()
        self._upload_btn = QPushButton("⬆ 上传到服务器")
        self._upload_btn.clicked.connect(self._upload_checked)
        self._delete_btn = QPushButton("⬇ 从服务器中删除")
        self._delete_btn.clicked.connect(self._delete_checked)
        row1.addWidget(self._upload_btn)
        row1.addWidget(self._delete_btn)
        lay.addLayout(row1)

        hint = QLabel(
            "上传前请勾选表格中要上传的条目; 删除仅支持服务器下载的不可编辑条目;\n"
            "拉取审核请求会将一条待审核条目加入表格(锁定), 审核完勾选上传即可。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8a8a9a;font-size:11px;")
        lay.addWidget(hint)

        self._refresh_auditors()

    # ------------------------------------------------------------------
    def _refresh_auditors(self) -> None:
        """从已登录审核服务器账号刷新审核员昵称列表。"""
        current = self._auditor_combo.currentText()
        self._auditor_combo.clear()
        users: list[str] = []
        for s in self._mw.app_settings.audit_servers:
            if s.get("logged_in") and s.get("username"):
                if s["username"] not in users:
                    users.append(s["username"])
        if users:
            self._auditor_combo.addItems(users)
            idx = users.index(current) if current in users else 0
            self._auditor_combo.setCurrentIndex(idx)
            self._upload_btn.setEnabled(True)
            self._delete_btn.setEnabled(True)
            self._pull_review_btn.setEnabled(True)
        else:
            self._upload_btn.setEnabled(False)
            self._delete_btn.setEnabled(False)
            self._pull_review_btn.setEnabled(False)
            self._auditor_combo.addItem("(未登录审核账号)")
        # 审核员昵称不可编辑
        self._auditor_combo.setEditable(False)

    # ------------------------------------------------------------------
    def _checked_entries(self) -> list:
        """返回表格中勾选的条目对象。"""
        return [
            e for e in self._mw.store.entries
            if self._mw._row_widgets.get(id(e), {}).get("check", None)
            and self._mw._row_widgets[id(e)]["check"].isChecked()
        ]

    def _validate_upload(self, e) -> str:
        """上传前五项检查, 返回缺失项描述; 全部满足返回空串。"""
        fields = [
            ("玩家昵称", e.nickname),
            ("玩家ID", e.player_id),
            ("原因", e.reason),
            ("事件发生日期", e.event_date),
            ("录像链接", e.replay_link),
        ]
        missing = [name for name, val in fields if not (val or "").strip()]
        return "、".join(missing)

    # ------------------------------------------------------------------
    def _upload_checked(self) -> None:
        entries = self._checked_entries()
        if not entries:
            QMessageBox.information(self, "未选择", "请先在表格中勾选要上传的条目")
            return
        # 五项检查
        bad = [(e, self._validate_upload(e)) for e in entries]
        bad = [(e, m) for e, m in bad if m]
        if bad:
            names = "、".join(
                f"「{e.nickname or e.player_id or '(未命名)'}」缺 {m}" for e, m in bad
            )
            QMessageBox.warning(
                self, "信息不完整",
                f"以下条目缺少必填项, 无法上传:\n{names}\n\n"
                "必须包含: 玩家昵称 / 玩家ID / 原因 / 事件发生日期 / 录像链接",
            )
            return
        auditor = self._auditor_combo.currentText()
        if not auditor or auditor.startswith("("):
            QMessageBox.warning(self, "未登录", "请先在服务器设置中登录审核账号")
            return
        # 选择要上传的仓库
        servers = [s for s in self._mw.app_settings.audit_servers if s.get("logged_in")]
        if not servers:
            QMessageBox.warning(self, "无可用仓库", "没有已登录的审核服务器")
            return
        from .upload_dialog import UploadDialog
        dlg = UploadDialog(servers, parent=self)
        if dlg.exec() != UploadDialog.DialogCode.Accepted:
            return
        targets = dlg.selected_servers()
        if not targets:
            return
        # 构造条目数据(含 cloud_id, 不上传证据)
        payload = []
        for e in entries:
            if not e.cloud_id:
                e.cloud_id = self._new_cloud_id()
            payload.append({
                "cloud_id": e.cloud_id,
                "nickname": e.nickname, "player_id": e.player_id,
                "reason": e.reason, "event_date": e.event_date,
                "replay_link": e.replay_link, "remarks": e.remarks,
                "previous_nicknames": e.previous_nicknames,
                "audited": True, "auditor": auditor,
                "source": "server", "locked": True,
            })
        # 模态上传进度窗: 阻塞主界面直到上传完成
        from .progress_dialog import ProgressDialog
        dlg = ProgressDialog("正在上传到服务器", self._mw)
        dlg.setMinimumWidth(520)
        dlg._file_label.setText(f"正在上传 {len(payload)} 条到 {len(targets)} 个仓库…")
        dlg._bar.setRange(0, 0)  # 不确定总量, 使用忙碌指示
        self.upload_started.emit()

        def work() -> None:
            try:
                res = self._upload_worker_sync(targets, payload, auditor)
                dlg.task_finished.emit(res, "")
            except Exception as exc:  # noqa: BLE001
                dlg.task_finished.emit(None, str(exc))

        threading.Thread(target=work, daemon=True).start()
        dlg.exec()
        self._mw._set_io_busy("audit", False)
        self.upload_finished.emit(dlg.stats)

    def _upload_worker_sync(self, targets: list[dict], payload: list[dict],
                            auditor: str) -> dict:
        """同步上传所有目标仓库, 返回结果 dict(供进度窗线程调用)。"""
        results = []
        errors = []

        def on_retry(n: int) -> None:
            self.retry_notice.emit("上传", f"检测到服务器文件被他人修改, 正在自动重试 ({n}次)...")

        for s in targets:
            try:
                res = upload_entries(s["url"], s.get("token", ""), payload,
                                     message=f"WTBlackList 审核上传 - {auditor}",
                                     on_retry=on_retry)
                retry_txt = f"(含 {res['retries']} 次自动重试)" if res.get("retries") else ""
                results.append(f"{s.get('url')}: 上传 {res['uploaded']} 条 {retry_txt}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{s.get('url')}: {exc}")
        return {
            "results": results, "errors": errors, "servers": targets,
        }

    @staticmethod
    def _new_cloud_id() -> str:
        import uuid
        return str(uuid.uuid4())

    # ------------------------------------------------------------------
    def _pull_review(self) -> None:
        """从待审核队列拉取 1 条(打标正在审核, 防止多人拉同一条)。"""
        # 一次只能审核一条: 本地已有审核拉取的条目未完成则禁止拉取
        active = [
            e for e in self._mw.store.entries
            if e.source == "review" or getattr(e, "review_id", "")
        ]
        if active:
            QMessageBox.information(
                self, "已有待审核条目",
                "本地已有一条审核拉取的条目未完成。\n"
                "请先审核完毕(上传到服务器)或删除该条目, 才能拉取下一条。",
            )
            return
        auditor = self._auditor_combo.currentText()
        if not auditor or auditor.startswith("("):
            QMessageBox.warning(self, "未登录", "请先在服务器设置中登录审核账号")
            return
        servers = [s for s in self._mw.app_settings.audit_servers
                   if s.get("logged_in") and s.get("token")]
        if not servers:
            QMessageBox.warning(self, "无可用仓库", "没有已登录的审核服务器")
            return
        s = servers[0]
        url = str(s.get("url") or "")
        token = str(s.get("token") or "")
        self._mw._set_io_busy("audit", True)

        def work() -> None:
            try:
                from .review_sync import pull_next_review
                item = pull_next_review(url, token, auditor)
                if item is not None:
                    # 记录正在审核的 (仓库, 条目ID), 上传成功后据此删除待审核请求
                    self._mw._active_review = (url, str(item.get("id") or ""))
                self.review_pulled.emit(item)
            except Exception as exc:  # noqa: BLE001
                self.review_error.emit(str(exc))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------
    def _delete_checked(self) -> None:
        # 只能删除服务器下载的不可编辑条目
        entries = [
            e for e in self._checked_entries()
            if e.locked and e.source == "server" and e.cloud_id
        ]
        if not entries:
            QMessageBox.information(
                self, "无法删除",
                "只能删除从服务器下载的不可编辑条目(已锁定)。\n"
                "本地创建的条目无法从服务器删除。",
            )
            return
        servers = [s for s in self._mw.app_settings.audit_servers if s.get("logged_in")]
        if not servers:
            QMessageBox.warning(self, "无可用仓库", "没有已登录的审核服务器")
            return
        names = "、".join(f"「{e.nickname or e.player_id}」" for e in entries)
        ret = QMessageBox.question(
            self, "二次确认",
            f"确认从所有已登录服务器中删除以下条目?\n\n{names}\n\n"
            "删除会同步移除服务器文件中的对应条目, 不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        ids = {e.cloud_id for e in entries}
        self._mw._last_delete_ids = ids  # 供主窗口删除完成后同步本地
        self.delete_started.emit()
        threading.Thread(
            target=self._delete_worker, args=(servers, ids), daemon=True
        ).start()

    def _delete_worker(self, servers: list[dict], ids: set[str]) -> None:
        results = []
        errors = []

        def on_retry(n: int) -> None:
            self.retry_notice.emit("删除", f"检测到服务器文件被他人修改, 正在自动重试 ({n}次)...")

        for s in servers:
            try:
                res = delete_entries(s["url"], s.get("token", ""), ids,
                                     on_retry=on_retry)
                retry_txt = f"(含 {res['retries']} 次自动重试)" if res.get("retries") else ""
                results.append(f"{s.get('url')}: 删除 {res['removed']} 条 {retry_txt}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{s.get('url')}: {exc}")
        self.delete_finished.emit({"results": results, "errors": errors})
