"""「地图管理」对话框: 查看/命名/删除本地缓存的地图图片。

8111 **不提供地图名**(实测: `/map_info.json`/`/mission.json`/`/hud_map` 都没有,
官方 8111 页面本身也只用到 `/map.img`、`/map_info.json`、`/map_obj.json`、`/mission.json`),
地图身份只能靠图片哈希。所以这里让用户把哈希对应成看得懂的名字,
名字存进 `RouteStore.map_names`, 左侧侧栏会立刻显示这个名字。

列表列: 预览 / 名称 / 图片大小 / 轨迹数 / 首次缓存时间

名称改在窗口顶部固定的「地图名称」输入框里 —— 表格内部**完全禁止编辑**。
之前用“双击单元格就地编辑”, 编辑器会落在图片所在的那一格里被缩略图盖住
(只看得到左右各一条竖线), 所以改成结构上不可能出错的方案。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QBrush, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from .map_cache import MapCache, MapEntry
from .route_store import RouteStore

log = logging.getLogger("map_manager")

_THUMB = 84          # 预览缩略图边长(px)
_NAME_COL_W = 300    # “名称”列宽度(必须给足, 否则编辑框会压在预览图上)


class MapManagerDialog(QDialog):
    """缓存地图管理(命名 / 预览 / 删除缓存图片)。"""

    def __init__(self, store: RouteStore, cache: MapCache, parent=None):
        super().__init__(parent)
        self.store = store
        self.cache = cache
        self.renamed = 0
        self.setWindowTitle("地图管理")
        self.resize(940, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        tip = QLabel(
            "8111 不提供地图名, 所以这里用「图片内容哈希」区分地图。\n"
            "改名字的步骤: 先在下面的列表里**选中一张地图**, 再在顶部「地图名称」框里输入,\n"
            "按回车(或点「保存名称」)立即生效 —— 表格里的名字只是显示, 不用双击去改。\n"
            "名称会显示在左侧列表, 也会写进已录制的轨迹记录; 缓存图片在 data/maps 下,"
            "同一张图只存一份; 删除图片不会删除轨迹记录。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#94a3b8;")
        root.addWidget(tip)

        # ---- 名称输入区(固定位置, 永远和表格里的图片隔着两行, 不可能重叠) ----
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        lbl_name = QLabel("地图名称:")
        lbl_name.setStyleSheet("font-weight:bold;")
        name_row.addWidget(lbl_name)
        self.edit_name = QLineEdit()
        self.edit_name.setPlaceholderText("先选中下面的一张地图, 然后在这里输入名称, 回车保存")
        self.edit_name.setClearButtonEnabled(True)
        self.edit_name.setEnabled(False)
        self.edit_name.returnPressed.connect(self._apply_name)
        self.edit_name.editingFinished.connect(self._apply_name)   # 失焦也保存
        name_row.addWidget(self.edit_name, 1)
        self.btn_apply = QPushButton("保存名称")
        self.btn_apply.setEnabled(False)
        self.btn_apply.clicked.connect(self._apply_name)
        name_row.addWidget(self.btn_apply)
        root.addLayout(name_row)

        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["预览", "名称", "图片", "轨迹", "首次缓存"])
        self.table.verticalHeader().setVisible(False)
        self.table.setIconSize(QSize(_THUMB, _THUMB))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # 关键: 表格里一律不许就地编辑 —— 以前开启 DoubleClicked 时, 双击到图片那一格
        # 会在图片格上叠一个输入框(被缩略图盖住, 只剩左右两条竖线)。
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self._loading = False
        self._cur_key: str | None = None     # 名称输入框当前对应的地图
        # 列宽: 预览固定; 名称列伸缩; 其余按内容。
        head = self.table.horizontalHeader()
        head.setStretchLastSection(False)
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col in (2, 3, 4):
            head.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnWidth(0, _THUMB + 16)
        self.setMinimumWidth(_THUMB + 16 + _NAME_COL_W + 300)
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

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.accept)
        row.addWidget(buttons)
        root.addLayout(row)

        self.reload()

    # ------------------------------------------------------------------ 列表
    def reload(self) -> None:
        """重建列表(按首次缓存时间倒序)。"""
        self._loading = True
        try:
            entries = self.cache.entries()
            self.table.setRowCount(0)
            for entry in entries:
                self._add_row(entry)
        finally:
            self._loading = False
        missing = [k for k in self.store.ordered_map_keys() if not self.cache.has(k)]
        extra = ""
        if missing:
            extra = (f"    另有 {len(missing)} 张地图只有轨迹、没有缓存图片"
                     f"(未在游戏里再进过该图时无法显示底图)")
        self.lbl_summary.setText(
            f"共 {len(entries)} 张缓存地图, 合计 "
            f"{self.cache.total_bytes() / 1024.0 / 1024.0:.1f} MB{extra}")
        if entries and self.table.currentRow() < 0:
            self.table.setCurrentCell(0, 0)      # 默认选中第一张, 输入框直接可用
        self._on_selection_changed()

    def _add_row(self, entry: MapEntry) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setRowHeight(row, _THUMB + 10)

        # 预览(不可编辑: 否则双击图片会把输入框开在图片格里)
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

        name = QTableWidgetItem()          # 只显示, 编辑在顶部输入框里做
        name.setData(Qt.ItemDataRole.UserRole, entry.key)
        name.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        self.table.setItem(row, 1, name)
        self._update_name_cell(entry.key)

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

    def _update_name_cell(self, key: str) -> None:
        """把「名称」列刷新成当前真实值(没名字显示灰色“未命名”)。"""
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 1)
            if it is None or it.data(Qt.ItemDataRole.UserRole) != key:
                continue
            name = self.store.name_of(key)
            it.setText(name or "未命名")
            it.setForeground(QBrush() if name else QBrush(Qt.GlobalColor.gray))
            default = self.store.display_name(key)
            it.setToolTip(f"地图键: {key}\n留空则按顺序自动编号为「{default}」")
            return

    # ------------------------------------------------------------------ 操作
    def _on_selection_changed(self) -> None:
        """选中另一张地图 → 把上一张刚输入的内容落盘, 再把新选中项的现有名称填进输入框。"""
        if self._loading:
            return
        self._apply_name()                  # 先保存上一张(还没回车就换行的情况)
        key = self._current_key()
        self._cur_key = key
        if key is None:
            self.edit_name.clear()
            self.edit_name.setEnabled(False)
            self.btn_apply.setEnabled(False)
            return
        self.edit_name.setEnabled(True)
        self.btn_apply.setEnabled(True)
        self.edit_name.setText(self.store.name_of(key))
        self.edit_name.setToolTip(f"地图键: {key}")
        self.edit_name.selectAll()

    def _apply_name(self) -> None:
        """把顶部输入框的内容写盘(立即生效, 不关窗口, 可接着改下一张)。

        以前是“双击单元格就地编辑 + 点保存才写入”: 编辑器没提交时名字会静默丢掉,
        现在输入框独立在表格外, 回车 / 失焦 / 切行 都会保存。
        """
        key = self._cur_key
        if not key:
            return
        new = self.edit_name.text().strip()
        if new == self.store.name_of(key):
            return
        self.store.rename_map(key, new)
        self.renamed += 1
        self._update_name_cell(key)
        shown = new or self.store.display_name(key)
        self.lbl_summary.setText(
            f"已保存: {shown}(第 {self.renamed} 次修改, 左侧列表已同步)")
        log.info("地图管理: 重命名 %s -> %r", key, new)

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
        self._apply_name()
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

    def done(self, result: int) -> None:
        """关窗(含 Esc / 关闭按钮)前把输入框里还没回车的名字保存下来。"""
        self._apply_name()
        super().done(result)


def _image_size_text(path: str) -> str:
    """尽力给出图片像素尺寸(取不到就返回空串)。"""
    pm = QPixmap(path)
    if pm.isNull():
        return ""
    return f"{pm.width()}×{pm.height()}"
