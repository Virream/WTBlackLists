"""同步服务器名单对话框: 多服务器下载、逐服务器进度/统计、条目对比(覆盖/追加)。"""
from __future__ import annotations

import threading

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout,
)

from .server_sync import fetch_entries, merge_entries


class SyncDialog(QDialog):
    """下载名单对话框。信号: finished(all_entries: list[dict], errors: list[str])。"""

    _finished = pyqtSignal(list, list)
    _progress = pyqtSignal(str, str)  # (服务器提示, 统计提示)

    def __init__(self, servers: list[dict], parent=None):
        super().__init__(parent)
        self._servers = servers
        self._all: list[dict] = []
        self._errors: list[str] = []
        self.setWindowTitle("同步服务器名单")
        self.resize(560, 320)

        lay = QVBoxLayout(self)
        self._status = QLabel("正在对服务器中的条目进行下载…")
        self._status.setWordWrap(True)
        lay.addWidget(self._status)

        self._server_label = QLabel("")
        self._server_label.setWordWrap(True)
        lay.addWidget(self._server_label)

        self._stat_label = QLabel("")
        self._stat_label.setWordWrap(True)
        lay.addWidget(self._stat_label)

        btn_row = QHBoxLayout()
        self._close_btn = QPushButton("完成")
        self._close_btn.setEnabled(False)
        self._close_btn.clicked.connect(self.accept)
        btn_row.addStretch(1)
        btn_row.addWidget(self._close_btn)
        lay.addLayout(btn_row)

        self._finished.connect(self._on_finished)
        self._progress.connect(self._on_progress)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        total_added = 0
        for i, s in enumerate(self._servers):
            url = s.get("url", "")
            self._progress.emit(
                f"服务器 {i + 1}/{len(self._servers)}: {url}", ""
            )
            try:
                entries = fetch_entries(url)
                merged, added = merge_entries(self._all, entries, prefer="local")
                self._all = merged
                total_added += added
                self._progress.emit(
                    "", f"已下载: {len(entries)} 条, 新增/更新: {added} 条"
                )
            except Exception as exc:  # noqa: BLE001
                self._errors.append(f"{url}: {exc}")
                self._progress.emit("", f"⚠ {url} 下载失败: {exc}")
        self._finished.emit(self._all, self._errors)

    def _on_progress(self, server_text: str, stat_text: str) -> None:
        if server_text:
            self._server_label.setText(server_text)
        if stat_text:
            self._stat_label.setText(stat_text)

    def _on_finished(self, entries: list[dict], errors: list[str]) -> None:
        self._close_btn.setEnabled(True)
        if errors:
            self._status.setText(
                f"下载完成, 共获取 {len(entries)} 条, 其中 {len(errors)} 个服务器失败"
            )
        else:
            self._status.setText(f"下载完成, 共获取 {len(entries)} 条")
        self._stat_label.setText(f"全部服务器共汇总 {len(entries)} 条名单")
        self.accept()


class CompareDialog(QDialog):
    """相同条目冲突对比: 用户选择 覆盖 或 追加。"""

    def __init__(self, local: dict, remote: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("发现相同条目")
        self.resize(560, 380)

        lay = QVBoxLayout(self)
        intro = QLabel(
            "服务器中的条目与本地条目(玩家ID+事件日期)相同, 请选择处理方式:\n"
            "覆盖: 用服务器条目更新本地条目信息(不删除你的证据文件);\n"
            "追加: 保留本地条目, 同时把服务器条目作为新条目加入。"
        )
        intro.setWordWrap(True)
        lay.addWidget(intro)

        def _fmt(d: dict) -> str:
            return (
                f"昵称: {d.get('nickname','')}\n"
                f"玩家ID: {d.get('player_id','')}\n"
                f"原因: {d.get('reason','')}\n"
                f"日期: {d.get('event_date','')}\n"
                f"备注: {d.get('remarks','')}"
            )

        two = QHBoxLayout()
        local_box = QLabel(f"【本地条目】\n{_fmt(local)}")
        local_box.setWordWrap(True)
        local_box.setStyleSheet("border:1px solid #34445a;padding:6px;")
        remote_box = QLabel(f"【服务器条目】\n{_fmt(remote)}")
        remote_box.setWordWrap(True)
        remote_box.setStyleSheet("border:1px solid #34445a;padding:6px;")
        two.addWidget(local_box, 1)
        two.addWidget(remote_box, 1)
        lay.addLayout(two)

        btn_row = QHBoxLayout()
        self._overwrite_btn = QPushButton("覆盖")
        self._append_btn = QPushButton("追加")
        self._cancel_btn = QPushButton("跳过")
        btn_row.addStretch(1)
        btn_row.addWidget(self._overwrite_btn)
        btn_row.addWidget(self._append_btn)
        btn_row.addWidget(self._cancel_btn)
        lay.addLayout(btn_row)

        self._overwrite_btn.clicked.connect(lambda: self.done(1))
        self._append_btn.clicked.connect(lambda: self.done(2))
        self._cancel_btn.clicked.connect(lambda: self.done(0))
