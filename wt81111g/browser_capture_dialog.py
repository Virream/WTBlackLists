"""交互式浏览器兜底对话框: 启动本机浏览器, 用户手动通过验证, 自动检测并抓取昵称。

流程:
1. 用户点击"打开浏览器" → 软件启动独立浏览器实例加载 userinfo 页面。
2. 对话框显示提示"请在浏览器中完成人机验证"。
3. 后台线程轮询 CDP, 检测到昵称元素后自动抓取。
4. 抓取成功后通过 nickname_captured 信号回传, 主窗口更新表格。
"""
from __future__ import annotations

import threading

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from .browser_capture import capture_nickname_via_browser


class BrowserCaptureDialog(QDialog):
    # 抓取完成: (player_id, nickname 或 None, 状态说明)
    nickname_captured = pyqtSignal(str, object, str)

    def __init__(self, player_id: str, current_nickname: str, settings=None, parent=None):
        super().__init__(parent)
        self._player_id = player_id
        self._current_nickname = current_nickname
        self._settings = settings
        self._running = False
        self.setWindowTitle("浏览器抓取昵称")
        self.setMinimumWidth(520)

        lay = QVBoxLayout(self)
        self._info = QLabel(
            f"玩家ID: {player_id}\n"
            f"当前昵称: {current_nickname or '(未填写)'}\n\n"
            "该玩家无法通过 WTLive/官网接口直接查询。\n"
            "推荐使用应用内浏览器(可自动通过人机验证并抓取昵称);\n"
            "若自动抓取失败, 可展开下方选项使用系统浏览器手动验证。"
        )
        self._info.setWordWrap(True)
        lay.addWidget(self._info)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color:#5ab0ff;")
        lay.addWidget(self._status)

        # 首选: 应用内浏览器(WebView2, 自动过验证), 视觉上突出引导
        row = QHBoxLayout()
        self._auto_check = QCheckBox("下次自动打开浏览器")
        self._auto_check.setToolTip(
            "勾选后, 下次需要浏览器获取昵称时自动打开应用内浏览器并自动抓取,"
            "不弹窗打断游戏"
        )
        if settings is not None:
            self._auto_check.setChecked(bool(settings.auto_browser))
        self._auto_check.toggled.connect(self._on_auto_toggled)
        self._wv2_btn = QPushButton("🔷 应用内浏览器(推荐)")
        self._wv2_btn.setToolTip("使用应用内 WebView2 浏览器, 通常可自动通过人机验证")
        self._wv2_btn.clicked.connect(self._start_webview2)
        self._wv2_btn.setStyleSheet(
            "QPushButton { background:#1a6fb0; color:#ffffff; border:none; "
            "border-radius:6px; padding:7px 20px; font-weight:bold; }"
            "QPushButton:hover { background:#2a80c0; }"
            "QPushButton:disabled { background:#4a5a6a; color:#b0b0c0; }"
        )
        self._close_btn = QPushButton("取消")
        self._close_btn.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(self._auto_check)
        row.addWidget(self._wv2_btn)
        row.addWidget(self._close_btn)
        lay.addLayout(row)

        # 次要: 系统浏览器(默认折叠, 点击链接展开)
        link_row = QHBoxLayout()
        link_row.addStretch(1)
        self._open_btn = QPushButton("🌐 打开浏览器并开始检测")
        self._open_btn.setToolTip("启动本机系统浏览器(Edge/Chrome)手动验证")
        self._open_btn.clicked.connect(self._start)
        self._open_btn.hide()
        link_row.addWidget(self._open_btn)
        self._toggle_btn = QPushButton("使用系统浏览器手动验证")
        self._toggle_btn.setFlat(True)
        self._toggle_btn.setStyleSheet("color:#7ec8e3;")
        self._toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle_btn.clicked.connect(self._toggle_system_browser)
        link_row.addWidget(self._toggle_btn)
        lay.addLayout(link_row)

        self.nickname_captured.connect(self._on_captured)

    def _on_auto_toggled(self, checked: bool) -> None:
        """勾选框状态持久化到设置。"""
        if self._settings is not None:
            self._settings.auto_browser = bool(checked)
            self._settings.save()

    def _toggle_system_browser(self) -> None:
        """展开/收起'打开系统浏览器'的次要选项。"""
        if self._open_btn.isHidden():
            self._open_btn.show()
            self._toggle_btn.setText("收起系统浏览器选项")
        else:
            self._open_btn.hide()
            self._toggle_btn.setText("使用系统浏览器手动验证")

    def _start(self) -> None:
        if self._running:
            return
        self._running = True
        self._open_btn.setEnabled(False)
        self._wv2_btn.setEnabled(False)
        self._open_btn.setText("⏳ 正在检测浏览器页面…")
        self._status.setText(
            "浏览器已打开, 请在浏览器窗口中完成人机验证(若有)。\n"
            "加载完成后会自动抓取昵称…"
        )
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self) -> None:
        nick, state = capture_nickname_via_browser(self._player_id)
        self.nickname_captured.emit(self._player_id, nick, state)

    def _start_webview2(self) -> None:
        """应用内浏览器(WebView2): 子进程抓取, 通常可自动过验证。"""
        if self._running:
            return
        self._running = True
        self._open_btn.setEnabled(False)
        self._wv2_btn.setEnabled(False)
        self._open_btn.setText("⏳ 正在检测…")
        self._status.setText(
            "正在启动应用内浏览器(WebView2), 会自动通过验证并抓取昵称…"
        )
        threading.Thread(target=self._work_wv2, daemon=True).start()

    def _work_wv2(self) -> None:
        from .webview2_capture import run_capture
        nick, state = run_capture(self._player_id)
        self.nickname_captured.emit(self._player_id, nick, state)

    def _on_captured(self, pid: str, nick: object, state: str) -> None:
        self._running = False
        self._wv2_btn.setEnabled(True)
        if nick:
            self._status.setText(f"✅ 已抓取昵称: {nick}")
            self._open_btn.setText("完成")
            self._open_btn.setEnabled(True)
            self._open_btn.clicked.disconnect()
            self._open_btn.clicked.connect(self.accept)
            self._wv2_btn.setEnabled(False)
        else:
            self._status.setText(f"❌ {state}")
            self._open_btn.setText("重试")
            self._open_btn.setEnabled(True)
