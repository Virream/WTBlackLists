"""导入设置对话框: 模式选择 + 文件选择 + 是否导入证据 + 统计预估。"""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from .import_export import format_size, preview_import


def _fmt_time(sec: float) -> str:
    if sec < 60:
        return f"约 {max(1, int(sec))} 秒"
    return f"约 {sec / 60:.1f} 分钟"


class ImportModeDialog(QDialog):
    """用户选择导入方式: 追加 / 玩家ID查重; 选择文件; 是否导入证据; 显示统计预估。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导入黑名单条目")
        self.setMinimumWidth(520)
        self._file_path = ""
        self._preview = None
        lay = QVBoxLayout(self)

        title = QLabel("请选择导入方式:")
        f = title.font()
        f.setBold(True)
        title.setFont(f)
        lay.addWidget(title)

        self._append_radio = QRadioButton("追加模式")
        self._append_radio.setChecked(True)
        lay.addWidget(self._append_radio)
        append_desc = QLabel("直接把文件中的所有条目导入软件进行追加, 不查重。")
        append_desc.setWordWrap(True)
        append_desc.setStyleSheet("color:#6b7686;font-size:12px;padding-left:24px;")
        lay.addWidget(append_desc)

        self._pid_radio = QRadioButton("玩家ID导入")
        lay.addWidget(self._pid_radio)
        pid_desc = QLabel(
            "导入时按玩家ID查重, 已有相同玩家ID则跳过该条, 不重复添加。"
        )
        pid_desc.setWordWrap(True)
        pid_desc.setStyleSheet("color:#6b7686;font-size:12px;padding-left:24px;")
        lay.addWidget(pid_desc)

        self._group = QButtonGroup(self)
        self._group.addButton(self._append_radio)
        self._group.addButton(self._pid_radio)

        # 文件选择
        file_row = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setReadOnly(True)
        self._path_edit.setPlaceholderText("请选择要导入的 zip 文件")
        browse = QPushButton("选择文件…")
        browse.clicked.connect(self._browse)
        file_row.addWidget(self._path_edit, 1)
        file_row.addWidget(browse)
        lay.addSpacing(6)
        lay.addLayout(file_row)

        # 是否导入证据
        self._restore_ev = QCheckBox("导入证据文件")
        self._restore_ev.setChecked(False)
        self._restore_ev.setToolTip("勾选后会把文件中的证据文件一并恢复到本地")
        lay.addWidget(self._restore_ev)

        # 统计
        self._stat_label = QLabel("选择文件后显示导入统计")
        self._stat_label.setWordWrap(True)
        self._stat_label.setStyleSheet("color:#6b7686;")
        lay.addWidget(self._stat_label)

        # 按钮
        btn_row = QHBoxLayout()
        ok = QPushButton("确定导入")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btn_row.addStretch(1)
        btn_row.addWidget(cancel)
        btn_row.addWidget(ok)
        lay.addSpacing(8)
        lay.addLayout(btn_row)

    # ------------------------------------------------------------------
    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择要导入的黑名单导出包", "", "黑名单导出包 (*.zip)"
        )
        if not path:
            return
        self._file_path = path
        self._path_edit.setText(path)
        try:
            self._preview = preview_import(path)
        except Exception as exc:  # noqa: BLE001
            self._preview = None
            self._stat_label.setText(f"⚠ 无法预览: {exc}")
            return
        p = self._preview
        self._stat_label.setText(
            f"共将导入 {p['entry_count']} 条条目\n"
            f"导入文件: {format_size(p['file_size'])}\n"
            f"含证据文件: {p['evidence_count']} 个\n"
            f"预计耗时: {_fmt_time(p['est_seconds'])}"
        )

    def mode(self) -> str:
        return "pid" if self._pid_radio.isChecked() else "append"

    def path(self) -> str:
        return self._file_path

    def restore_evidence(self) -> bool:
        return self._restore_ev.isChecked()
