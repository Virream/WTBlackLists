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
import time

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QRadioButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .browser_capture import capture_nickname_via_browser
from .monitor import MonitorWorker
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

        # 统计: 有玩家ID 且 24h 内未抓取过的条目(移除未超过24h的ID, 减少重复抓取)
        now = time.time()
        ttl = MonitorWorker.PROFILE_FETCH_TTL
        self._items: list[tuple[str, str, float]] = []
        self._skipped = 0
        for e in store.entries:
            pid = (e.player_id or "").strip()
            if not pid:
                continue
            fetched_at = float(getattr(e, "fetched_at", 0) or 0)
            if fetched_at and (now - fetched_at) < ttl:
                self._skipped += 1  # 24h 内已抓取 → 跳过
                continue
            self._items.append((pid, (e.nickname or "").strip(), fetched_at))

        lay = QVBoxLayout(self)
        skip_note = (f"(已跳过 {self._skipped} 个 24h 内已抓取的)"
                     if self._skipped else "")
        head = QLabel(
            f"共 {len(self._items)} 个条目需要更新昵称{skip_note}:\n"
            "先通过 WTLive/战雷官网 获取, 失败后按下方选择走浏览器兜底。"
        )
        head.setWordWrap(True)
        lay.addWidget(head)

        self._table = QTableWidget(len(self._items), 4)
        self._table.setHorizontalHeaderLabels(
            ["玩家ID", "当前昵称", "剩余有效时间", "状态"]
        )
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        for c in (0, 2, 3):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for i, (pid, nick, fetched_at) in enumerate(self._items):
            self._table.setItem(i, 0, QTableWidgetItem(pid))
            self._table.setItem(i, 1, QTableWidgetItem(nick))
            self._table.setItem(i, 2,
                                QTableWidgetItem(self._fmt_remaining(fetched_at)))
            self._table.setItem(i, 3, QTableWidgetItem(_STATUS_PENDING))
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

    @staticmethod
    def _fmt_remaining(fetched_at: float) -> str:
        """返回距离 24h 缓存过期的剩余时间描述(与昵称缓存窗口一致)。"""
        if not fetched_at:
            return "未抓取过"
        remaining = MonitorWorker.PROFILE_FETCH_TTL - (time.time() - fetched_at)
        if remaining <= 0:
            return "已过期"
        h = int(remaining // 3600)
        m = int((remaining % 3600) // 60)
        return f"{h}小时{m}分"

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
        for i, (pid, _nick, _fetched_at) in enumerate(self._items):
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
