"""事件发生日期字段: 时钟按钮 + 日期时间选择(年月日时分)。"""
from __future__ import annotations

from PyQt6.QtCore import QDateTime, pyqtSignal
from PyQt6.QtWidgets import QDateTimeEdit, QHBoxLayout, QToolButton, QWidget

# 哨兵值: 表示"未选择"
_EMPTY = QDateTime(2000, 1, 1, 0, 0)
_DISPLAY_FMT = "yyyy-MM-dd HH:mm"


class DateTimeField(QWidget):
    """点击时钟按钮自动填入当前系统时间, 也可手动选择年月日时分。"""

    valueChanged = pyqtSignal(str)  # "" 表示未选择

    def __init__(self, value: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.clock_btn = QToolButton()
        self.clock_btn.setText("🕒")
        self.clock_btn.setToolTip("点击填入当前系统时间")
        self.clock_btn.setFixedSize(26, 26)
        layout.addWidget(self.clock_btn)

        self.date_edit = QDateTimeEdit()
        self.date_edit.setDisplayFormat(_DISPLAY_FMT)
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setMinimumDateTime(_EMPTY)
        self.date_edit.setSpecialValueText("未选择")
        self.date_edit.setDateTime(_EMPTY)
        layout.addWidget(self.date_edit, 1)

        self.clock_btn.clicked.connect(
            lambda: self.date_edit.setDateTime(QDateTime.currentDateTime())
        )
        self.date_edit.dateTimeChanged.connect(self._emit)

        if value:
            self.set_value(value)

    def set_value(self, value: str) -> None:
        """用字符串('yyyy-MM-dd HH:mm' 或 'yyyy-MM-dd')设置;空串/非法值视为未选择。"""
        if value:
            dt = QDateTime.fromString(value, _DISPLAY_FMT)
            if not dt.isValid():
                dt = QDateTime.fromString(value, "yyyy-MM-dd")
            if dt.isValid():
                self.date_edit.setDateTime(dt)
                return
        self.date_edit.setDateTime(_EMPTY)

    def setReadOnly(self, readonly: bool) -> None:
        """设为只读: 禁用编辑与时钟按钮。"""
        self.date_edit.setReadOnly(bool(readonly))
        self.clock_btn.setEnabled(not readonly)

    def isReadOnly(self) -> bool:
        """当前是否只读。"""
        return self.date_edit.isReadOnly()

    def value(self) -> str:
        dt = self.date_edit.dateTime()
        if dt <= _EMPTY:
            return ""
        return dt.toString(_DISPLAY_FMT)

    def _emit(self, dt: QDateTime) -> None:
        self.valueChanged.emit(self.value())
