"""「快捷键设置」对话框: 三个动作各自选按键(可选右Shift 之类的修饰键)。

改动**立即生效**(重新注册热键 + 更新轮询), 关掉窗口不用保存。
游戏里会同时收到这个键(我们不拦截按键), 所以尽量选游戏里没绑功能的键;
若发现按了没反应, 里面会显示"系统热键注册失败(该键多半被别的程序占用了)"。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout,
)

from .global_hotkey import (
    ACTION_LABELS, DEFAULT_BINDINGS, KEY_CHOICES, MODIFIER_LABELS, describe_binding,
    key_label,
)

# 界面上的修饰键选项(常用几个; 右Shift 是旧默认, 留着方便老用户换回去)
_MOD_CHOICES = ("", "rshift", "lshift", "ralt", "lalt", "rctrl", "lctrl")

_ORDER = ("start", "stop", "mark")


class HotkeyDialog(QDialog):
    """快捷键设置: 每个动作一行「修饰键 + 按键」, 改动立刻生效。"""

    def __init__(self, window, parent=None):
        super().__init__(parent or window)
        self.setWindowTitle("快捷键设置")
        self.setMinimumWidth(560)
        self.win = window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        self._mod_boxes: dict[str, QComboBox] = {}
        self._key_boxes: dict[str, QComboBox] = {}
        self._labels: dict[str, QLabel] = {}
        for row, action in enumerate(_ORDER):
            name = QLabel(ACTION_LABELS[action])
            name.setMinimumWidth(96)
            grid.addWidget(name, row, 0)

            mod_box = QComboBox()
            for mod in _MOD_CHOICES:
                mod_box.addItem(MODIFIER_LABELS.get(mod, mod), mod)
            grid.addWidget(mod_box, row, 1)
            grid.addWidget(QLabel("+"), row, 2)
            key_box = QComboBox()
            for key in KEY_CHOICES:
                key_box.addItem(key_label(key), key)
            grid.addWidget(key_box, row, 3)

            state = QLabel()
            state.setStyleSheet("font-size:11px;")
            grid.addWidget(state, row, 4, 1, 3)
            self._mod_boxes[action] = mod_box
            self._key_boxes[action] = key_box
            self._labels[action] = state

            def _changed(_=0, a=action):
                self._apply(a)

            mod_box.currentIndexChanged.connect(_changed)
            key_box.currentIndexChanged.connect(_changed)

        grid.setColumnStretch(4, 1)
        layout.addLayout(grid)

        hint = QLabel(
            "游戏里也会收到这个键(本程序不拦截按键): 建议选游戏里没绑功能的键, "
            "或把游戏里对应的绑定解掉。\n"
            "不带修饰键的单键(如 F5)最省事 —— 不需要和 W 之类的键同时按。"
        )
        hint.setStyleSheet("color:#94a3b8; font-size:11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        bottom = QHBoxLayout()
        btn_default = QPushButton("恢复默认")
        btn_default.clicked.connect(self._reset)
        bottom.addWidget(btn_default)
        bottom.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        bottom.addWidget(buttons)
        layout.addLayout(bottom)

        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        """按当前绑定填控件(期间不触发 _apply)。"""
        bindings = self.win.hotkeys.bindings
        for action, (mod, key) in bindings.items():
            mod_box, key_box = self._mod_boxes[action], self._key_boxes[action]
            mod_box.blockSignals(True)
            key_box.blockSignals(True)
            idx = mod_box.findData(mod)
            mod_box.setCurrentIndex(idx if idx >= 0 else 0)
            idx = key_box.findData(key)
            key_box.setCurrentIndex(idx if idx >= 0 else 0)
            mod_box.blockSignals(False)
            key_box.blockSignals(False)
        self._refresh_tips()

    def _current(self) -> dict[str, tuple[str, str]]:
        return {a: (str(self._mod_boxes[a].currentData() or ""),
                    str(self._key_boxes[a].currentData() or "")) for a in _ORDER}

    def _apply(self, action: str) -> None:
        """某个动作改了就整体应用一次(注册/更新两套通道并存盘)。"""
        binding = (str(self._mod_boxes[action].currentData() or ""),
                   str(self._key_boxes[action].currentData() or ""))
        self.win.apply_hotkey(action, binding)
        self._refresh_tips()

    def _refresh_tips(self) -> None:
        for action, lbl in self._labels.items():
            mod = str(self._mod_boxes[action].currentData() or "")
            key = str(self._key_boxes[action].currentData() or "")
            txt = describe_binding((mod, key))
            # 注册失败信息挂在 GlobalHotkey 上(轮询那条通道从不失败)
            failed = (getattr(getattr(self.win, "sys_hotkeys", None), "failed", None)
                      or {}).get(action)
            if failed:
                lbl.setText(f"{txt} —— ⚠ {failed}")
                lbl.setStyleSheet("color:#f59e0b; font-size:11px;")
            else:
                lbl.setText(f"{txt} ✓")
                lbl.setStyleSheet("color:#22c55e; font-size:11px;")

    def _reset(self) -> None:
        for action in _ORDER:
            self.win.apply_hotkey(action, DEFAULT_BINDINGS[action])
        self._load()
