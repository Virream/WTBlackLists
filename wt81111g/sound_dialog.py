"""「音效设置」对话框: 开关 / 音量 / 每个动作自定义 WAV(都带试听)。

改动**立即生效并落盘**(和「地图管理」一样), 关掉窗口不需要再点保存。
"""
from __future__ import annotations

import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QSlider, QVBoxLayout, QWidget,
)

from .sound import ACTION_NAMES, SoundSettings, play

# 对话框里按这个顺序列出四个音效
_ORDER = ("start", "stop", "mark", "error")


class SoundSettingsDialog(QDialog):
    """音效设置: 启用开关 + 音量 + 四个动作各自的音效文件。"""

    def __init__(self, settings: SoundSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("音效设置")
        self.setMinimumWidth(620)
        self.snd = settings
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        # ---- 开关
        self.chk_on = QCheckBox("启用提示音(开始/结束/标记时播放音效)")
        self.chk_on.setChecked(self.snd.enabled)
        self.chk_on.toggled.connect(self.snd.set_enabled)
        layout.addWidget(self.chk_on)

        # ---- 音量
        vol_row = QHBoxLayout()
        vol_row.setSpacing(8)
        vol_row.addWidget(QLabel("音量:"))
        self.sld_vol = QSlider(Qt.Orientation.Horizontal)
        self.sld_vol.setRange(0, 100)
        self.sld_vol.setValue(self.snd.volume)
        self.sld_vol.setMinimumWidth(320)
        self.sld_vol.valueChanged.connect(self._on_volume)
        vol_row.addWidget(self.sld_vol, 1)
        self.lbl_vol = QLabel(f"{self.snd.volume}%")
        self.lbl_vol.setMinimumWidth(46)
        vol_row.addWidget(self.lbl_vol)
        btn_preview = QPushButton("试听")
        btn_preview.clicked.connect(lambda: self._preview("start"))
        vol_row.addWidget(btn_preview)
        layout.addLayout(vol_row)
        hint = QLabel("提示: 音量靠把音效样本按比例重新采样后播放, 不会改动系统音量。")
        hint.setStyleSheet("color:#94a3b8; font-size:11px;")
        layout.addWidget(hint)

        # ---- 每个动作的音效文件
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        self._labels: dict[str, QLabel] = {}
        for row, action in enumerate(_ORDER):
            name = QLabel(ACTION_NAMES[action])
            name.setMinimumWidth(96)
            grid.addWidget(name, row, 0)
            lbl = QLabel()
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lbl.setMinimumWidth(200)
            grid.addWidget(lbl, row, 1)
            self._labels[action] = lbl

            btn_pick = QPushButton("选择…")
            btn_pick.clicked.connect(lambda _=False, a=action: self._pick(a))
            grid.addWidget(btn_pick, row, 2)
            btn_try = QPushButton("试听")
            btn_try.clicked.connect(lambda _=False, a=action: self._preview(a))
            grid.addWidget(btn_try, row, 3)
            btn_default = QPushButton("恢复默认")
            btn_default.clicked.connect(lambda _=False, a=action: self._reset(a))
            grid.addWidget(btn_default, row, 4)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        note = QLabel("默认用的是 Windows 自带音效(在 %SystemRoot%\\Media 里); "
                      "自定义请选 .wav 文件。")
        note.setStyleSheet("color:#94a3b8; font-size:11px;")
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self._refresh_labels()

    # ------------------------------------------------------------------
    def _refresh_labels(self) -> None:
        for action, lbl in self._labels.items():
            lbl.setText(self.snd.label_for(action))
            path = self.snd.path_for(action)
            lbl.setToolTip(path or "系统提示音(MessageBeep)")
            lbl.setEnabled(self.chk_on.isChecked())

    def _on_volume(self, value: int) -> None:
        self.snd.set_volume(value)
        self.lbl_vol.setText(f"{value}%")

    def _pick(self, action: str) -> None:
        start_dir = os.path.dirname(self.snd.path_for(action)) or ""
        path, _ = QFileDialog.getOpenFileName(
            self, f"选择{ACTION_NAMES[action]}的音效文件", start_dir,
            "WAV 音频 (*.wav);;所有文件 (*)")
        if not path:
            return
        self.snd.set_file(action, path)
        self._refresh_labels()
        self._preview(action)                    # 选完直接听一下

    def _reset(self, action: str) -> None:
        self.snd.set_file(action, "")
        self._refresh_labels()

    def _preview(self, action: str) -> None:
        """试听: 忽略"启用"开关(否则关着音效就没法调), 但用当前音量。"""
        play(action, force=True)
