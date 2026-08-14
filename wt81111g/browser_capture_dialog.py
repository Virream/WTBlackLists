"""交互式浏览器兜底对话框: 启动本机浏览器, 用户手动通过验证, 自动检测并抓取昵称。

流程:
1. 用户点击"打开浏览器" → 软件启动独立浏览器实例加载 userinfo 页面。
2. 对话框显示提示"请在浏览器中完成人机验证"。
3. 后台线程轮询 CDP, 检测到昵称元素后自动抓取。
4. 抓取成功后通过 nickname_captured 信号回传, 主窗口更新表格。
"""
from __future__ import annotations

import threading

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from .browser_capture import capture_nickname_via_browser


class BrowserCaptureDialog(QDialog):
    # 抓取完成: (player_id, nickname 或 None, 状态说明)
    nickname_captured = pyqtSignal(str, object, str)

    def __init__(self, player_id: str, current_nickname: str, parent=None):
        super().__init__(parent)
        self._player_id = player_id
        self._current_nickname = current_nickname
        self._running = False
        self.setWindowTitle("浏览器抓取昵称")
        self.setMinimumWidth(520)

        lay = QVBoxLayout(self)
        self._info = QLabel(
            f"玩家ID: {player_id}\n"
            f"当前昵称: {current_nickname or '(未填写)'}\n\n"
            "该玩家无法通过 WTLive/官网接口直接查询。\n"
            "点击下方按钮打开浏览器, 若页面要求人机验证请手动完成,\n"
            "软件会自动检测页面加载完成并抓取昵称。"
        )
        self._info.setWordWrap(True)
        lay.addWidget(self._info)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color:#5ab0ff;")
        lay.addWidget(self._status)

        row = QHBoxLayout()
        self._open_btn = QPushButton("🌐 打开浏览器并开始检测")
        self._open_btn.clicked.connect(self._start)
        self._close_btn = QPushButton("取消")
        self._close_btn.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(self._open_btn)
        row.addWidget(self._close_btn)
        lay.addLayout(row)

        self.nickname_captured.connect(self._on_captured)

    def _start(self) -> None:
        if self._running:
            return
        self._running = True
        self._open_btn.setEnabled(False)
        self._open_btn.setText("⏳ 正在检测浏览器页面…")
        self._status.setText(
            "浏览器已打开, 请在浏览器窗口中完成人机验证(若有)。\n"
            "加载完成后会自动抓取昵称…"
        )
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self) -> None:
        nick, state = capture_nickname_via_browser(self._player_id)
        self.nickname_captured.emit(self._player_id, nick, state)

    def _on_captured(self, pid: str, nick: object, state: str) -> None:
        self._running = False
        if nick:
            self._status.setText(f"✅ 已抓取昵称: {nick}")
            self._open_btn.setText("完成")
            self._open_btn.setEnabled(True)
            self._open_btn.clicked.disconnect()
            self._open_btn.clicked.connect(self.accept)
        else:
            self._status.setText(f"❌ {state}")
            self._open_btn.setText("重试")
            self._open_btn.setEnabled(True)
