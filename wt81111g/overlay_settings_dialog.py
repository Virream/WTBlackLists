"""叠加层设置对话框(在主界面中打开, 改动实时生效)。"""
from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDialog,
    QDoubleSpinBox,
    QFontComboBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from .settings import OverlaySettings


class OverlaySettingsDialog(QDialog):
    settings_changed = pyqtSignal()

    def __init__(self, settings: OverlaySettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("叠加层设置")
        self.setModal(False)

        root = QVBoxLayout(self)
        form = QFormLayout()
        root.addLayout(form)

        # 位置
        self.x_spin = QDoubleSpinBox()
        self.x_spin.setRange(0.0, 100.0)
        self.x_spin.setSingleStep(1.0)
        self.x_spin.setDecimals(1)
        self.x_spin.setSuffix(" %")
        self.x_spin.setValue(settings.pos_x_pct)
        form.addRow("水平位置(X%)", self.x_spin)

        self.y_spin = QDoubleSpinBox()
        self.y_spin.setRange(0.0, 100.0)
        self.y_spin.setSingleStep(1.0)
        self.y_spin.setDecimals(1)
        self.y_spin.setSuffix(" %")
        self.y_spin.setValue(settings.pos_y_pct)
        form.addRow("垂直位置(Y%, 顶部对齐)", self.y_spin)

        self.lock_check = QCheckBox("锁定位置(禁用位置调整)")
        self.lock_check.setChecked(settings.locked)
        form.addRow("锁定", self.lock_check)

        # 背景
        self.color_btn = QPushButton()
        self.color_btn.setFixedWidth(96)
        self.color_btn.clicked.connect(self._pick_color)
        form.addRow("背景颜色", self.color_btn)

        self.alpha_spin = QSpinBox()
        self.alpha_spin.setRange(0, 100)
        self.alpha_spin.setSuffix(" %")
        self.alpha_spin.setValue(round(settings.bg_alpha / 255 * 100))
        form.addRow("背景不透明度", self.alpha_spin)

        # 字体
        self.font_spin = QSpinBox()
        self.font_spin.setRange(10, 72)
        self.font_spin.setSuffix(" px")
        self.font_spin.setValue(settings.font_size)
        form.addRow("字体大小", self.font_spin)

        self.font_combo = QFontComboBox()
        self.font_combo.setCurrentFont(QFont(settings.font_family))
        form.addRow("字体", self.font_combo)

        self.font_color_btn = QPushButton()
        self.font_color_btn.setFixedWidth(96)
        self.font_color_btn.clicked.connect(self._pick_font_color)
        form.addRow("字体颜色", self.font_color_btn)

        self.radius_spin = QSpinBox()
        self.radius_spin.setRange(0, 50)
        self.radius_spin.setSuffix(" px")
        self.radius_spin.setValue(settings.corner_radius)
        form.addRow("背景圆角弧度", self.radius_spin)

        # 内容: 是否显示原因 + 自定义文本
        self.show_reason_check = QCheckBox("在命中提示中显示原因")
        self.show_reason_check.setChecked(settings.show_reason)
        form.addRow("显示原因", self.show_reason_check)

        self.text_checking_edit = QLineEdit(settings.text_checking)
        self.text_checking_edit.setPlaceholderText("正在确认名单中...")
        form.addRow("确认中文本", self.text_checking_edit)

        self.text_found_edit = QLineEdit(settings.text_found)
        self.text_found_edit.setPlaceholderText("发现肃反人员")
        form.addRow("命中标题文本", self.text_found_edit)

        # 按钮
        btns = QHBoxLayout()
        reset_btn = QPushButton("恢复默认")
        close_btn = QPushButton("完成")
        btns.addWidget(reset_btn)
        btns.addStretch(1)
        btns.addWidget(close_btn)
        root.addLayout(btns)

        reset_btn.clicked.connect(self._reset)
        close_btn.clicked.connect(self.close)

        # 实时生效
        self.x_spin.valueChanged.connect(self._on_change)
        self.y_spin.valueChanged.connect(self._on_change)
        self.alpha_spin.valueChanged.connect(self._on_change)
        self.font_spin.valueChanged.connect(self._on_change)
        self.font_combo.currentFontChanged.connect(self._on_change)
        self.radius_spin.valueChanged.connect(self._on_change)
        self.lock_check.toggled.connect(self._on_change)
        self.show_reason_check.toggled.connect(self._on_change)
        self.text_checking_edit.textChanged.connect(self._on_change)
        self.text_found_edit.textChanged.connect(self._on_change)

        self._update_lock_state()
        self._update_color_btn()
        self._update_font_color_btn()

    # ------------------------------------------------------------------
    def _on_change(self, *_args) -> None:
        self.settings.pos_x_pct = float(self.x_spin.value())
        self.settings.pos_y_pct = float(self.y_spin.value())
        self.settings.bg_alpha = int(round(self.alpha_spin.value() / 100 * 255))
        self.settings.font_size = int(self.font_spin.value())
        self.settings.font_family = self.font_combo.currentFont().family()
        self.settings.corner_radius = int(self.radius_spin.value())
        self.settings.locked = self.lock_check.isChecked()
        self.settings.show_reason = self.show_reason_check.isChecked()
        self.settings.text_checking = self.text_checking_edit.text().strip() or "正在确认名单中..."
        self.settings.text_found = self.text_found_edit.text().strip() or "发现肃反人员"
        self._update_lock_state()
        self.settings_changed.emit()

    def _update_lock_state(self) -> None:
        locked = self.settings.locked
        self.x_spin.setEnabled(not locked)
        self.y_spin.setEnabled(not locked)

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(
            QColor(self.settings.bg_color), self, "选择背景颜色"
        )
        if color.isValid():
            self.settings.bg_color = color.name()
            self._update_color_btn()
            self.settings_changed.emit()

    def _update_color_btn(self) -> None:
        self.color_btn.setText(self.settings.bg_color)
        self.color_btn.setStyleSheet(
            f"background-color: {self.settings.bg_color}; color: #ffffff;"
        )

    def _pick_font_color(self) -> None:
        color = QColorDialog.getColor(
            QColor(self.settings.font_color), self, "选择字体颜色"
        )
        if color.isValid():
            self.settings.font_color = color.name()
            self._update_font_color_btn()
            self.settings_changed.emit()

    def _update_font_color_btn(self) -> None:
        self.font_color_btn.setText(self.settings.font_color)
        self.font_color_btn.setStyleSheet(
            f"background-color: {self.settings.font_color}; color: #ffffff;"
        )

    def _reset(self) -> None:
        d = OverlaySettings()
        self.x_spin.setValue(d.pos_x_pct)
        self.y_spin.setValue(d.pos_y_pct)
        self.lock_check.setChecked(d.locked)
        self.settings.bg_color = d.bg_color
        self.alpha_spin.setValue(round(d.bg_alpha / 255 * 100))
        self.font_spin.setValue(d.font_size)
        self.font_combo.setCurrentFont(QFont(d.font_family))
        self.settings.font_color = d.font_color
        self.radius_spin.setValue(d.corner_radius)
        self.show_reason_check.setChecked(d.show_reason)
        self.text_checking_edit.setText(d.text_checking)
        self.text_found_edit.setText(d.text_found)
        self._update_color_btn()
        self._update_font_color_btn()
        # 兜底: 即使系统未安装默认字体(下拉框回退), 也恢复默认字体名
        self.settings.font_family = d.font_family
        self._on_change()
