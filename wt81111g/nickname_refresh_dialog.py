"""刷新昵称对话框: 统计需要更新昵称的条目, 逐一通过 WTLive 获取, 失败后浏览器兜底。

整合了原先"浏览器抓取昵称"窗口的选项:
- "下次自动打开浏览器"勾选框(可随时在此取消)
- 互斥选择 WTLive 失败后的兜底浏览器: 内置浏览器(推荐) / 系统浏览器

流程: 用户点击"开始刷新" → 后台线程逐一处理列表 →
  先 fetch_profile_best_effort(WTLive/官网) → 成功则回传更新;
  永久失败(404) → 按互斥选择走内置/系统浏览器兜底 → 回传更新。
"""
from __future__ import annotations

import threading

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QRadioButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .browser_capture import capture_nickname_via_browser
from .webview2_capture import run_capture
from .warthunder import fetch_profile_best_effort

_STATUS_PENDING = "待更新"
_STATUS_FETCHING = "抓取中…"


class NicknameRefreshDialog(QDialog):
    # 抓取到一个昵称: (player_id, nickname 原始值, 需主窗口清洗后应用)
    nickname_fetched = pyqtSignal(str, str)
    # 后台线程 → 主线程的 UI 更新信号
    status_updated = pyqtSignal(int, str)
    progress_updated = pyqtSignal(int, int)
    finished = pyqtSignal(int, int)  # (成功数, 总数)

    def __init__(self, store, nickname_cache, settings, parent=None):
        super().__init__(parent)
        self._store = store
        self._cache = nickname_cache
        self._settings = settings
        self._running = False
        self.setWindowTitle("刷新昵称")
        self.setMinimumSize(680, 500)

        # 统计: 有玩家ID 的条目
        self._items = [
            ((e.player_id or "").strip(), (e.nickname or "").strip())
            for e in store.entries if (e.player_id or "").strip()
        ]

        lay = QVBoxLayout(self)
        head = QLabel(
            f"共 {len(self._items)} 个条目将逐一更新昵称:\n"
            "先通过 WTLive/战雷官网 获取, 失败后按下方选择走浏览器兜底。"
        )
        head.setWordWrap(True)
        lay.addWidget(head)

        self._table = QTableWidget(len(self._items), 3)
        self._table.setHorizontalHeaderLabels(["玩家ID", "当前昵称", "状态"])
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for i, (pid, nick) in enumerate(self._items):
            self._table.setItem(i, 0, QTableWidgetItem(pid))
            self._table.setItem(i, 1, QTableWidgetItem(nick))
            self._table.setItem(i, 2, QTableWidgetItem(_STATUS_PENDING))
        lay.addWidget(self._table, 1)

        # ---- 选项区 ----
        self._auto_check = QCheckBox("下次自动打开浏览器")
        self._auto_check.setToolTip(
            "勾选后, 后续 WTLive 获取失败自动触发时, 后台自动打开应用内浏览器抓取,\n"
            "不弹窗打断游戏; 可随时在此取消勾选"
        )
        if settings is not None:
            self._auto_check.setChecked(bool(settings.auto_browser))
        self._auto_check.toggled.connect(self._on_auto_toggled)
        lay.addWidget(self._auto_check)

        radio_row = QHBoxLayout()
        radio_row.addWidget(QLabel("WTLive 获取失败时兜底:"))
        self._wv2_radio = QRadioButton("使用内置浏览器(推荐)")
        self._sys_radio = QRadioButton("使用系统浏览器")
        self._wv2_radio.setChecked(True)
        radio_row.addWidget(self._wv2_radio)
        radio_row.addWidget(self._sys_radio)
        radio_row.addStretch(1)
        lay.addLayout(radio_row)

        self._progress_label = QLabel("")
        lay.addWidget(self._progress_label)

        # ---- 按钮 ----
        btns = QHBoxLayout()
        self._start_btn = QPushButton("开始刷新")
        self._start_btn.clicked.connect(self._start)
        self._close_btn = QPushButton("关闭")
        self._close_btn.clicked.connect(self.accept)
        btns.addStretch(1)
        btns.addWidget(self._start_btn)
        btns.addWidget(self._close_btn)
        lay.addLayout(btns)

        self.status_updated.connect(self._on_status)
        self.progress_updated.connect(self._on_progress)
        self.finished.connect(self._on_finished)

    def _on_auto_toggled(self, checked: bool) -> None:
        if self._settings is not None:
            self._settings.auto_browser = bool(checked)
            self._settings.save()

    def _start(self) -> None:
        if self._running:
            return
        self._running = True
        self._start_btn.setEnabled(False)
        self._start_btn.setText("刷新中…")
        # 重置状态
        for i in range(len(self._items)):
            self.status_updated.emit(i, _STATUS_PENDING)
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self) -> None:
        ok = 0
        total = len(self._items)
        for i, (pid, _nick) in enumerate(self._items):
            self.status_updated.emit(i, _STATUS_FETCHING)
            nick, status = fetch_profile_best_effort(pid)
            if nick:
                self.nickname_fetched.emit(pid, nick)
                self.status_updated.emit(i, f"✅ {nick}")
                ok += 1
            elif status == 404:  # 永久失败(无效ID) → 浏览器兜底
                if self._wv2_radio.isChecked():
                    nick2, state = run_capture(pid)
                else:
                    nick2, state = capture_nickname_via_browser(pid)
                if nick2:
                    self.nickname_fetched.emit(pid, nick2)
                    self.status_updated.emit(i, f"✅ {nick2}(浏览器)")
                    ok += 1
                else:
                    self.status_updated.emit(i, f"❌ {state}")
            else:
                self.status_updated.emit(i, f"❌ 抓取失败(status={status})")
            self.progress_updated.emit(i + 1, total)
        self.finished.emit(ok, total)

    def _on_status(self, i: int, text: str) -> None:
        if 0 <= i < self._table.rowCount():
            self._table.item(i, 2).setText(text)

    def _on_progress(self, done: int, total: int) -> None:
        self._progress_label.setText(f"进度: {done}/{total}")

    def _on_finished(self, ok: int, total: int) -> None:
        self._running = False
        self._start_btn.setEnabled(True)
        self._start_btn.setText("重新刷新")
        self._progress_label.setText(f"完成: 成功 {ok}/{total}")
