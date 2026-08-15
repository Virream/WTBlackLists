"""昵称缓存查看窗口: 展示各玩家ID对应的昵称及缓存剩余有效时间。"""
from __future__ import annotations

import time

from PyQt6.QtCore import Qt
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .monitor import MonitorWorker  # 复用缓存有效期常量
from .nickname_cache import NicknameCache


def _fmt_remaining(seconds: float) -> str:
    if seconds <= 0:
        return "已过期"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    if h > 0:
        return f"{h}小时{m}分"
    return f"{m}分钟"


class CacheDialog(QDialog):
    """非模态窗口, 直接读取独立昵称缓存数据库(ID → 昵称 + 失效倒计时)。

    缓存与黑名单条目解耦: 删除条目后缓存仍保留, 重新添加相同ID也能看到已缓存的昵称。
    含"自动更新"开关: 勾选后 24h 过期昵称在进入新对局时自动更新(先WTLive, 失败静默浏览器)。
    """

    auto_changed = pyqtSignal(bool)  # 自动更新开关变化

    def __init__(self, cache: NicknameCache, settings=None, parent=None):
        super().__init__(parent)
        self.cache = cache
        self._settings = settings
        self.setWindowTitle("昵称缓存")
        self.setMinimumSize(560, 380)
        self.setWindowFlag(Qt.WindowType.Window)
        self._build()

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        ttl_h = MonitorWorker.PROFILE_FETCH_TTL // 3600
        tip = QLabel(
            "以下为通过「玩家ID」访问 WT Live 获取到的昵称缓存(独立缓存数据库):\n"
            f"· 缓存有效期 {ttl_h} 小时, 失效后进入新对局会自动重新抓取;\n"
            "· 命中缓存不访问 WT Live, 不增加「WT Live 访问(本次)」次数;\n"
            "· 缓存与黑名单条目相互独立, 删除条目后缓存仍保留, 重新添加相同ID也可看到已缓存的昵称;\n"
            "· 窗口不会自动关闭, 每次打开自动刷新, 也可点「刷新」查看最新剩余时间。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#8a8a9a;")
        lay.addWidget(tip)

        self._auto_check = QCheckBox("自动更新")
        self._auto_check.setToolTip(
            "勾选后, 24小时过期的昵称在进入新对局时自动更新:\n"
            "先通过 WTLive/官网, 失败后静默通过内置浏览器抓取;\n"
            "抓取失败(网络/人机验证)仅在底部通知, 可点击「刷新昵称」手动抓取。"
        )
        if self._settings is not None:
            self._auto_check.setChecked(bool(self._settings.auto_browser))
        self._auto_check.toggled.connect(self._on_auto_toggled)
        lay.addWidget(self._auto_check)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["玩家ID", "昵称", "剩余有效时间"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        lay.addWidget(self.table)

        row = QHBoxLayout()
        row.addStretch(1)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        row.addWidget(refresh_btn)
        row.addWidget(close_btn)
        lay.addLayout(row)

    def _on_auto_toggled(self, checked: bool) -> None:
        """自动更新开关: 写回设置并通知主窗口同步 monitor。"""
        if self._settings is not None:
            self._settings.auto_browser = bool(checked)
            self._settings.save()
        self.auto_changed.emit(bool(checked))
        self.refresh()

    def showEvent(self, event) -> None:
        """每次打开窗口时自动刷新缓存表格, 无需手动点「刷新」。"""
        super().showEvent(event)
        self.refresh()

    def refresh(self) -> None:
        """从独立昵称缓存数据库重建表格(只显示已有有效缓存的记录)。"""
        now = time.time()
        ttl = MonitorWorker.PROFILE_FETCH_TTL
        rows: list[tuple[str, str, str]] = []
        for pid, nick, fetched_at, _invalid in self.cache.items():
            pid = (pid or "").strip()
            nick = (nick or "").strip()
            if not pid or not nick:
                continue
            remaining = fetched_at + ttl - now
            rows.append((pid, nick, _fmt_remaining(remaining)))
        rows.sort(key=lambda r: r[0])

        self.table.setRowCount(0)
        expired_color = QColor("#c0392b")
        for pid, nick, rem in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, val in enumerate((pid, nick, rem)):
                item = QTableWidgetItem(val)
                if c == 2 and rem == "已过期":
                    item.setForeground(expired_color)
                self.table.setItem(r, c, item)
