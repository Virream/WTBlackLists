"""录制叠加层的设置对话框(改动实时生效, 主窗口负责保存)。"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QSlider, QSpinBox, QVBoxLayout,
)

from .record_overlay import ANCHORS, RecordOverlaySettings


class RecordOverlayDialog(QDialog):
    """「设置 → 叠加层 → 叠加层设置…」。

    值一改就发 `settings_changed`, 主窗口立刻应用到叠加层(所见即所得);
    点「确定/取消」由主窗口决定是否保留 —— 取消会回滚到打开对话框时的值。
    """

    settings_changed = pyqtSignal()

    def __init__(self, settings: RecordOverlaySettings, parent=None):
        super().__init__(parent)
        self.settings = settings.normalized()
        self.setWindowTitle("录制叠加层设置")
        self.setMinimumWidth(360)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)

        # ---- 开关
        self.chk_enabled = QCheckBox("启用录制叠加层(只在录制时出现)", self)
        self.chk_enabled.setChecked(self.settings.enabled)
        self.chk_enabled.setToolTip(
            "勾选后, 开始录制(单条 / A·B 队)时叠加层出现, 结束录制时自动隐藏。\n"
            "叠加层是个普通透明置顶窗, 不注入游戏 —— 要浮在游戏上请用「全屏窗口(无边框)」模式。")
        root.addWidget(self.chk_enabled)

        # ---- 位置与外观
        box = QGroupBox("外观", self)
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.cmb_anchor = QComboBox(box)
        for key, name in ANCHORS.items():
            self.cmb_anchor.addItem(name, key)
        idx = self.cmb_anchor.findData(self.settings.anchor)
        self.cmb_anchor.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow("屏幕位置", self.cmb_anchor)

        self.spn_margin = QSpinBox(box)
        self.spn_margin.setRange(0, 400)
        self.spn_margin.setSuffix(" px")
        self.spn_margin.setValue(self.settings.margin)
        self.spn_margin.setToolTip("面板与屏幕边缘的距离。")
        form.addRow("边距", self.spn_margin)

        self.sld_opacity, self.spn_opacity, w_op = self._pct_row(box, self.settings.opacity,
                                                               "背景不透明度")
        self.spn_opacity.setToolTip("0 = 完全透明(只剩文字), 100 = 不透明的深色底。")
        form.addRow("背景不透明度", w_op)

        self.spn_corner = QSpinBox(box)
        self.spn_corner.setRange(0, 60)
        self.spn_corner.setSuffix(" px")
        self.spn_corner.setValue(self.settings.corner)
        self.spn_corner.setToolTip("面板四个角的圆角半径; 0 = 直角。")
        form.addRow("圆角", self.spn_corner)

        self.spn_font = QSpinBox(box)
        self.spn_font.setRange(6, 40)
        self.spn_font.setSuffix(" pt")
        self.spn_font.setValue(self.settings.font_size)
        form.addRow("字号", self.spn_font)
        root.addWidget(box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        # ---- 联动
        self.chk_enabled.toggled.connect(self._emit)
        self.cmb_anchor.currentIndexChanged.connect(self._emit)
        self.spn_margin.valueChanged.connect(self._emit)
        self.sld_opacity.valueChanged.connect(self.spn_opacity.setValue)
        self.spn_opacity.valueChanged.connect(self.sld_opacity.setValue)
        self.spn_opacity.valueChanged.connect(self._emit)
        self.spn_corner.valueChanged.connect(self._emit)
        self.spn_font.valueChanged.connect(self._emit)

    # ------------------------------------------------------------------ 内部
    def _pct_row(self, parent, value: int, tip: str = ""):
        """滑块 + 数值框(0~100)的组合控件。"""
        wrap = QHBoxLayout()
        sld = QSlider(Qt.Orientation.Horizontal, parent)
        sld.setRange(0, 100)
        sld.setValue(value)
        sld.setToolTip(tip)
        spn = QSpinBox(parent)
        spn.setRange(0, 100)
        spn.setSuffix(" %")
        spn.setValue(value)
        spn.setToolTip(tip)
        wrap.addWidget(sld, 1)
        wrap.addWidget(spn)
        return sld, spn, wrap

    def _emit(self) -> None:
        self.settings = RecordOverlaySettings(
            enabled=self.chk_enabled.isChecked(),
            anchor=self.cmb_anchor.currentData() or "tr",
            margin=self.spn_margin.value(),
            opacity=self.spn_opacity.value(),
            corner=self.spn_corner.value(),
            font_size=self.spn_font.value(),
            font_family=self.settings.font_family,
        ).normalized()
        self.settings_changed.emit()
