"""上传审核请求对话框: 二次确认勾选条目 + 上传进度(xx/xx)。

仅上传文本字段(昵称/ID/原因/日期/录像链接/备注/曾用昵称), 不含任何证据文件,
避免 GitHub 仓库体积膨胀。通过 issue 提交, 由服务端解析进 review_pending.json。
"""
from __future__ import annotations

import threading

from PyQt6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .review_sync import submit_review_request, validate_entry

# 每个 issue 提交的最大条目数(避免正文超 GitHub 大小限制)
BATCH_SIZE = 50


class ReviewRequestDialog(QDialog):
    """二次确认窗口: 展示已勾选条目, 选择提交账号, 上传并显示进度 xx/xx。"""

    def __init__(self, settings, entries: list, parent=None):
        super().__init__(parent)
        self._settings = settings
        # 过滤出可提交的合法条目(玩家ID/昵称/原因必填, 服务端会再次校验)
        self._entries = [e for e in entries if validate_entry(e)]
        self._servers = [
            s for s in getattr(settings, "audit_servers", []) or []
            if s.get("logged_in") and s.get("token")
        ]
        self.setWindowTitle("上传审核请求 - 二次确认")
        self.setMinimumSize(680, 420)
        self._build()
        self._refresh_account_hint()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        lay = QVBoxLayout(self)

        tip = QLabel(
            "以下为将提交给审核员的条目(二次确认):\n"
            "· 仅上传文本字段, 不含证据文件(截图/录像);\n"
            "· 提交后由服务端加入待审核队列, 审核员审核通过后才会进入服务器名单。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#8a8a9a;")
        lay.addWidget(tip)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["玩家ID", "玩家昵称", "原因", "事件发生日期", "录像链接", "备注"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        for e in self._entries:
            r = self.table.rowCount()
            self.table.insertRow(r)
            vals = (
                (e.get("player_id") or "").strip(),
                (e.get("nickname") or "").strip(),
                (e.get("reason") or "").strip(),
                (e.get("event_date") or "").strip(),
                (e.get("replay_link") or "").strip(),
                (e.get("remarks") or "").strip(),
            )
            for c, v in enumerate(vals):
                self.table.setItem(r, c, QTableWidgetItem(v))
        lay.addWidget(self.table, 1)

        # 提交账号(已登录审核服务器)
        row = QHBoxLayout()
        row.addWidget(QLabel("提交账号:"))
        self._account_combo = QComboBox()
        for s in self._servers:
            self._account_combo.addItem(
                f"{s.get('username') or '已登录'}  ({s.get('url')})", s)
        self._account_combo.setToolTip("以该登录账号提交审核请求 issue")
        row.addWidget(self._account_combo, 1)
        lay.addLayout(row)

        self._count_label = QLabel("")
        self._count_label.setStyleSheet("color:#e67e22;")
        lay.addWidget(self._count_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._upload_btn = QPushButton("⬆ 上传审核请求")
        self._upload_btn.clicked.connect(self._upload)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(self._upload_btn)
        btn_row.addWidget(cancel)
        lay.addLayout(btn_row)

    def _refresh_account_hint(self) -> None:
        if not self._servers:
            self._count_label.setText("⚠ 没有已登录的审核服务器, 无法提交")
            self._upload_btn.setEnabled(False)
            return
        s = self._account_combo.currentData()
        total = len(self._entries)
        self._count_label.setText(
            f"将以 {s.get('username')} 的账号提交 {total} 条审核请求")
        self._upload_btn.setEnabled(total > 0)

    # ------------------------------------------------------------------
    def _upload(self) -> None:
        s = self._account_combo.currentData()
        if not s:
            self._count_label.setText("⚠ 没有已登录的审核服务器, 无法提交")
            return
        url = str(s.get("url") or "").strip()
        token = str(s.get("token") or "").strip()
        submitter = str(s.get("username") or "").strip() or "unknown"
        entries = [dict(e) for e in self._entries]
        if not entries:
            return

        from .progress_dialog import ProgressDialog
        dlg = ProgressDialog("正在提交审核请求", self)
        dlg._file_label.setText(f"正在提交 {len(entries)} 条审核请求…")
        dlg._bar.setRange(0, len(entries))
        self._upload_btn.setEnabled(False)

        def work() -> None:
            try:
                total = len(entries)
                done = 0
                issues: list[int] = []
                for i in range(0, total, BATCH_SIZE):
                    batch = entries[i:i + BATCH_SIZE]
                    num, _url = submit_review_request(url, token, batch, submitter)
                    issues.append(num)
                    done = min(i + BATCH_SIZE, total)
                    dlg.progress_updated.emit(done, total, f"已提交 issue #{num}", 0)
                dlg.task_finished.emit({"issues": issues, "total": total}, "")
            except Exception as exc:  # noqa: BLE001
                dlg.task_finished.emit(None, str(exc))

        threading.Thread(target=work, daemon=True).start()
        dlg.exec()
        if dlg.error:
            QMessageBox.warning(self, "提交失败", dlg.error)
            self._upload_btn.setEnabled(True)
            return
        stats = dlg.stats or {}
        self.accept()
        QMessageBox.information(
            self, "提交成功",
            f"已提交 {stats.get('total', len(entries))} 条审核请求"
            f"({len(stats.get('issues', []))} 个 issue)。\n"
            "服务端将定时解析并加入待审核队列, 审核员审核通过后才会进入服务器名单。",
        )
