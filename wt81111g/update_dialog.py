"""更新日志对话框: 发现新版本时显示更新日志并提供下载 / 打开 GitHub。"""
from __future__ import annotations

import html

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout,
)


class UpdateDialog(QDialog):
    """展示新版本信息: 新/旧版本 + 更新日志 + 下载 / 打开GitHub / 暂不更新。"""

    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self._info = info
        self.setWindowTitle(f"发现新版本 {info.get('version', '')}")
        self.setModal(True)
        self.setMinimumSize(540, 460)
        self.setStyleSheet("QDialog { background-color: #1e1e2e; }")

        lay = QVBoxLayout(self)
        head = QLabel(
            f'<div style="font-size:15px; font-weight:bold; color:#ffffff;">'
            f'发现新版本 <span style="color:#7ec8e3;">{html.escape(info.get("version", ""))}</span>'
            f'<br><span style="font-size:12px; font-weight:normal; color:#8a8a8a;">'
            f'当前版本: {html.escape(info.get("current", ""))}</span></div>'
        )
        head.setWordWrap(True)
        lay.addWidget(head)

        body = info.get("body") or "暂无更新日志"
        browser = QTextBrowser()
        browser.setHtml(
            f'<div style="color:#e0e0e0; font-size:13px; line-height:1.6;">'
            f'{html.escape(body).replace(chr(10), "<br>")}</div>'
        )
        browser.setStyleSheet(
            "QTextBrowser { background:#1a1a2e; border:1px solid #3a3a5c; "
            "border-radius:8px; color:#e0e0e0; font-size:13px; }"
        )
        lay.addWidget(browser, 1)

        row = QHBoxLayout()
        if info.get("download_url"):
            dl = QPushButton("⬇ 下载新版本安装包")
            dl.clicked.connect(
                lambda: QDesktopServices.openUrl(QUrl(info["download_url"]))
            )
            row.addWidget(dl)
        gh = QPushButton("🌐 打开 GitHub")
        gh.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(info.get("html_url", "")))
        )
        row.addWidget(gh)
        later = QPushButton("暂不更新")
        later.clicked.connect(self.accept)
        row.addStretch(1)
        row.addWidget(later)
        lay.addLayout(row)
