"""「地图管理」对话框: 查看/命名/删除本地缓存的地图图片。

8111 **不提供地图名**(实测: `/map_info.json`/`/mission.json`/`/hud_map` 都没有,
官方 8111 页面本身也只用到 `/map.img`、`/map_info.json`、`/map_obj.json`、`/mission.json`),
地图身份只能靠图片哈希。所以这里让用户把哈希对应成看得懂的名字,
名字存进 `RouteStore.map_names`, 左侧侧栏会立刻显示这个名字。

列表列: 预览 / 名称(可直接双击改) / 图片大小 / 轨迹数 / 首次缓存时间
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .map_cache import MapCache, MapEntry
from .route_store import RouteStore

log = logging.getLogger("map_manager")

_THUMB = 84          # 预览缩略图边长(px)


class MapManagerDialog(QDialog):
    """缓存地图管理(命名 / 预览 / 删除缓存图片)。"""

    def __init__(self, store: RouteStore, cache: MapCache, parent=None):
        super().__init__(parent)
        self.store = store
        self.cache = cache
        self.renamed = 0
        self.setWindowTitle("地图管理")
        self.resize(820, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        tip = QLabel(
            "8111 不提供地图名, 所以这里用**图片内容哈希**区分地图。\n"
            "双击“名称”列即可改名(名称会显示在左侧列表, 也会写进已录制的轨迹记录)。\n"
            "缓存图片保存在 data/maps 下, 同一张图只存一份; 删除图片不会删除轨迹记录。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#94a3b8;")
        root.addWidget(tip)

        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["预览", "名称", "图片", "轨迹", "首次缓存"])
        self.table.verticalHeader().setVisible(False)
        self.table.setIconSize(QSize(_THUMB, _THUMB))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col in (2, 3, 4):
            head.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnWidth(0, _THUMB + 16)
        root.addWidget(self.table, 1)

        self.lbl_summary = QLabel("")
        self.lbl_summary.setStyleSheet("color:#94a3b8; font-size:11px;")
        root.addWidget(self.lbl_summary)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_reload = QPushButton("刷新")
        self.btn_reload.clicked.connect(self.reload)
        row.addWidget(self.btn_reload)
        self.btn_open = QPushButton("打开缓存文件夹")
        self.btn_open.clicked.connect(self._open_dir)
        row.addWidget(self.btn_open)
        self.btn_remove = QPushButton("删除选中的缓存图片")
        self.btn_remove.clicked.connect(self._remove_current)
        row.addWidget(self.btn_remove)
        row.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存名称")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        root.addLayout(row)

        self.reload()

    # ------------------------------------------------------------------ 列表
    def reload(self) -> None:
        """重建列表(按首次缓存时间倒序)。"""
        entries = self.cache.entries()
        self.table.setRowCount(0)
        for entry in entries:
            self._add_row(entry)
        missing = [k for k in self.store.ordered_map_keys() if not self.cache.has(k)]
        extra = ""
        if missing:
            extra = (f"    另有 {len(missing)} 张地图只有轨迹、没有缓存图片"
                     f"(未在游戏里再进过该图时无法显示底图)")
        self.lbl_summary.setText(
            f"共 {len(entries)} 张缓存地图, 合计 "
            f"{self.cache.total_bytes() / 1024.0 / 1024.0:.1f} MB{extra}")

    def _add_row(self, entry: MapEntry) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setRowHeight(row, _THUMB + 10)

        # 预览
        thumb = QTableWidgetItem()
        pm = QPixmap(entry.path)
        if pm.isNull():
            thumb.setText("无法预览")
        else:
            thumb.setIcon(QIcon(pm.scaled(_THUMB, _THUMB, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation)))
        thumb.setData(Qt.ItemDataRole.UserRole, entry.key)
        thumb.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.table.setItem(row, 0, thumb)

        # 名称(可编辑): 默认值用“用户起的名字”, 没有就留空并用水印提示
        name = QTableWidgetItem(self.store.name_of(entry.key))
        name.setData(Qt.ItemDataRole.UserRole, entry.key)
        default = self.store.display_name(entry.key)
        name.setToolTip(f"留空则按顺序自动编号为「{default}」\n地图键: {entry.key}")
        if not self.store.name_of(entry.key):
            name.setForeground(Qt.GlobalColor.gray)
        self.table.setItem(row, 1, name)

        size = QTableWidgetItem(f"{entry.size_text}  {_image_size_text(entry.path)}")
        size.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.table.setItem(row, 2, size)

        count = QTableWidgetItem(str(sum(1 for r in self.store.routes
                                         if r.map_key == entry.key)))
        count.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.table.setItem(row, 3, count)

        when = QTableWidgetItem(entry.saved_at_text)
        when.setFlags(Qt.ItemFlag.ItemIsEnabled)
        when.setToolTip(f"地图键: {entry.key}")
        self.table.setItem(row, 4, when)

    # ------------------------------------------------------------------ 操作
    def _current_key(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _open_dir(self) -> None:
        path = self.cache.dir
        try:
            if sys.platform == "win32":
                os.startfile(path)          # noqa: S606 - 只在 Windows 用
            else:
                subprocess.Popen(["xdg-open", path])   # noqa: S603,S607
        except OSError as exc:
            log.warning("打开目录失败 %s: %s", path, exc)

    def _remove_current(self) -> None:
        key = self._current_key()
        if key is None:
            QMessageBox.information(self, "删除缓存图片", "请先选中一行")
            return
        used = sum(1 for r in self.store.routes if r.map_key == key)
        ok = QMessageBox.question(
            self, "删除缓存图片",
            f"删除这张缓存图片吗?\n\n地图键: {key}\n名称: {self.store.display_name(key)}\n"
            f"该地图已有 {used} 条轨迹记录(记录与名称都会保留, 只是以后没有底图可显示)")
        if ok != QMessageBox.StandardButton.Yes:
            return
        if self.cache.remove(key):
            self.reload()

    def _save(self) -> None:
        """把“名称”列写回 store.map_names。"""
        changed = 0
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            if item is None:
                continue
            key = item.data(Qt.ItemDataRole.UserRole)
            new = item.text().strip()
            if new != self.store.name_of(key):
                self.store.rename_map(key, new)
                changed += 1
        self.renamed = changed
        log.info("地图管理: 保存了 %d 个地图名称", changed)
        self.accept()


def _image_size_text(path: str) -> str:
    """尽力给出图片像素尺寸(取不到就返回空串)。"""
    pm = QPixmap(path)
    if pm.isNull():
        return ""
    return f"{pm.width()}×{pm.height()}"
