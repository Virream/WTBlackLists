"""主窗口: 黑名单表格 + 对局监控面板。"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time

from PyQt6.QtCore import QRegularExpression, Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import (
    QFont, QIcon, QRegularExpressionValidator, QTextCursor,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .about_dialog import AboutDialog
from .audit_panel import AuditPanel
from .blacklist import DEFAULT_REASON, REASON_CHOICES, BlacklistEntry, BlacklistStore
from .browser_capture_dialog import BrowserCaptureDialog
from .cache_dialog import CacheDialog
from .config import APP_VERSION, evidences_dir, resource_path
from .datetime_field import DateTimeField
from .evidence import ensure_and_open, evidence_folder, open_folder
from .export_dialog import ExportDialog
from .import_dialog import ImportModeDialog
from .import_export import export_zip, format_size, import_zip
from .monitor import MonitorWorker
from .progress_dialog import ProgressDialog
from .nickname_cache import NicknameCache
from .nickname_sync_dialog import NicknameSyncDialog
from .proxy_config import set_proxy as _apply_proxy
from .proxy_dialog import ProxyDialog
from .overlay import OverlayWindow
from .overlay_settings_dialog import OverlaySettingsDialog
from .server_dialog import ServerSettingsDialog
from .server_sync import merge_entries
from .sync_dialog import CompareDialog, SyncDialog
from .settings import AppSettings

log = logging.getLogger("main_window")

HEADERS = ["勾选", "玩家昵称", "玩家ID", "原因", "事件发生日期", "曾用昵称", "录像链接", "证据", "备注", "审核", "审核员", "条目ID"]

_REMARK_MAX = 1000  # 备注最大字数


class _RemarkEditor(QPlainTextEdit):
    """限制最大字数的备注编辑器。

    在文本插入前拦截(键入/IME 提交/粘贴/拖放), 超长字符直接拒绝,
    让超长文本根本不会进入文档——避免 textChanged 里 setPlainText
    重建文档带来的输入错乱与(IME 合成期间的)原生崩溃。
    """

    def __init__(self, max_chars: int, parent=None):
        super().__init__(parent)
        self._max_chars = max_chars

    def _remaining(self) -> int:
        return max(0, self._max_chars - len(self.toPlainText()))

    def insertPlainText(self, text: str) -> None:
        if not text:
            return
        super().insertPlainText(text[: self._remaining()])

    def insertFromMimeData(self, source) -> None:
        text = source.text() if source is not None else ""
        if text:
            super().insertPlainText(text[: self._remaining()])


class MainWindow(QMainWindow):
    # 后台导入/导出完成信号: (stats 或 None, error 字符串)
    _export_finished = pyqtSignal(object, str)
    _import_finished = pyqtSignal(object, str)

    def __init__(self, store_path: str | None = None, start_monitor: bool = True,
                 nickname_cache: NicknameCache | None = None):
        super().__init__()
        self.store = BlacklistStore(store_path)
        if nickname_cache is not None:
            self.nickname_cache = nickname_cache
        elif store_path:
            # 测试/隔离场景: 缓存库与黑名单同目录
            self.nickname_cache = NicknameCache(
                os.path.join(os.path.dirname(store_path), "nickname_cache.json")
            )
        else:
            self.nickname_cache = NicknameCache()
        self.app_settings = AppSettings()
        _apply_proxy(self.app_settings.proxy)  # 应用已保存的代理, 所有网络请求走代理
        self._thread: QThread | None = None
        self._worker: MonitorWorker | None = None
        self._id_labels: dict[int, QLabel] = {}  # id(entry) -> 条目ID label
        self._overlay = OverlayWindow(self.app_settings.overlay)
        self._overlay_enabled = True
        self._really_quit = False
        self._locked = False
        self._row_widgets: dict[int, dict] = {}
        self._remark_entry: BlacklistEntry | None = None  # 备注编辑器当前关联的条目
        self._remark_locked = False                      # 加载备注时防止回写循环
        self._last_export_path = ""
        self._last_import_mode = "append"
        self._last_import_restore = True
        self._last_delete_ids: set[str] = set()
        self._export_finished.connect(self._on_export_done)
        self._import_finished.connect(self._on_import_done)
        self._build_ui()
        self._reload_table()
        self._setup_tray()
        if getattr(self.store, "load_error", ""):
            QMessageBox.warning(
                self, "数据文件损坏",
                "黑名单数据文件读取失败, 原文件已备份为 .corrupt-* 文件, "
                "列表已重置为空:\n" + self.store.load_error,
            )
        if start_monitor:
            self._start_monitor()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.setWindowTitle(f"WTBlackList 战争雷霆黑名单助手 v{APP_VERSION}")
        self.resize(1280, 640)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ================= 左半区: 功能按钮 + 条目表格 =================
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(4, 4, 4, 4)
        ll.setSpacing(6)

        def _glabel(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setStyleSheet("color:#5ab0ff;font-weight:bold;padding-right:2px;")
            return lbl

        def _vline() -> QFrame:
            v = QFrame()
            v.setFrameShape(QFrame.Shape.VLine)
            v.setStyleSheet("color:#34445a;")
            return v

        # ---- 顶部功能区: 三个分组(用竖分割条分隔, 按钮双排/列优先) ----
        top_func = QWidget()
        tf = QHBoxLayout(top_func)
        tf.setContentsMargins(0, 0, 0, 0)
        tf.setSpacing(12)

        def _func_group(title: str):
            """创建一个功能区: 顶部标签 + 下方双排按钮网格(列优先, 2行)。"""
            box = QVBoxLayout()
            box.setSpacing(2)
            box.addWidget(_glabel(title))
            grid = QGridLayout()
            grid.setSpacing(4)
            box.addLayout(grid)
            return box, grid

        def _fill_grid(grid: QGridLayout, buttons: list) -> list:
            """把按钮按“列优先”填到 2 行网格(先上下排满第一列, 再排第二列)。"""
            created: list = []
            col = 0
            row = 0
            for text, tip, cb in buttons:
                b = QPushButton(text)
                if tip:
                    b.setToolTip(tip)
                b.clicked.connect(cb)
                grid.addWidget(b, row, col)
                created.append(b)
                row += 1
                if row >= 2:
                    row = 0
                    col += 1
            return created

        # 左区: 导入导出
        g1, g1g = _func_group("导入导出")
        _made = _fill_grid(g1g, [
            ("📤 导出", "导出勾选的条目为文件", self._export_selected),
            ("📥 导入", "从别人分享的文件导入条目", self._import_entries),
        ])
        self._export_action, self._import_action = _made
        tf.addLayout(g1)

        tf.addWidget(_vline())

        # 中区: 数据维护(按钮顺序: 刷新/同步/缓存/服务器设置/共享表/代理)
        g2, g2g = _func_group("数据维护")
        _fill_grid(g2g, [
            ("🔁 刷新昵称", "重新抓取黑名单玩家昵称", self._manual_check),
            ("🔄 同步服务器名单", "从已配置的服务器下载共享名单并合并到本地", self._sync_servers),
            ("🗄 昵称缓存", "查看已抓取的昵称缓存", self._open_cache),
            ("🌐 服务器设置", "配置名单拉取服务器与审核服务器", self._open_server_settings),
            ("☁️ 共享昵称表", "拉取公开仓库的 nickname.json 合并到本地 / 上传抓取到的昵称(需登录)", self._open_nickname_sync),
            ("⚙ 代理设置", "设置代理, 所有网络请求均通过代理发送", self._open_proxy_settings),
        ])
        tf.addLayout(g2)

        tf.addWidget(_vline())

        # 右区: 界面
        g3, g3g = _func_group("界面")
        _fill_grid(g3g, [
            ("⚙ 叠加层设置", "配置叠加层外观与显示文本", self._open_overlay_settings),
            ("ℹ 关于", "查看版本与使用说明", self._show_about),
        ])
        tf.addLayout(g3)

        tf.addStretch(1)
        ll.addWidget(top_func)

        # ---- 与表格功能的分割条 ----
        _sep = QFrame()
        _sep.setFrameShape(QFrame.Shape.HLine)
        _sep.setStyleSheet("background-color:#34445a;")
        _sep.setFixedHeight(1)
        ll.addWidget(_sep)

        # ---- 表头正上方一行(宽度与表格同步): 表格相关操作 ----
        table_row = QWidget()
        tr = QHBoxLayout(table_row)
        tr.setContentsMargins(0, 0, 0, 0)
        tr.setSpacing(6)
        # 排序(放在添加条目之前)
        tr.addWidget(QLabel("排序:"))
        self._sort_combo = QComboBox()
        self._sort_combo.addItems([
            "玩家ID ↑", "玩家ID ↓", "玩家昵称 ↑", "玩家昵称 ↓",
            "事件日期 ↑", "事件日期 ↓",
        ])
        self._sort_combo.setToolTip(
            "按玩家ID/玩家昵称/事件日期 正向或逆向排序, 立即生效并保存"
        )
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        tr.addWidget(self._sort_combo)
        tr.addSpacing(8)
        self._add_action = QPushButton("➕ 添加条目")
        self._add_action.clicked.connect(self._add_row)
        tr.addWidget(self._add_action)
        self._delete_action = QPushButton("🗑 删除选中")
        self._delete_action.clicked.connect(self._delete_selected)
        tr.addWidget(self._delete_action)
        self._lock_action = QPushButton("🔒 锁定条目")
        self._lock_action.clicked.connect(self._toggle_lock)
        tr.addWidget(self._lock_action)
        _b = QPushButton("📁 打开证据根目录")
        _b.clicked.connect(lambda: open_folder(evidences_dir()))
        tr.addWidget(_b)
        _b = QPushButton("🧹 未使用证据检测")
        _b.setToolTip("检测证据目录中没有对应条目的文件夹")
        _b.clicked.connect(self._detect_unused_evidence)
        tr.addWidget(_b)
        tr.addStretch(1)
        ll.addWidget(table_row)

        # ---- 条目表格 ----
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        # 勾选列表头: 用于全选/取消全选(左键切换), 右键弹出 反选/全选/取消全选
        self._header_check_item = QTableWidgetItem("☐ 全选")
        self.table.setHorizontalHeaderItem(0, self._header_check_item)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)  # 勾选
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)  # 原因
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)  # 日期
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)  # 证据
        header.setSectionResizeMode(9, QHeaderView.ResizeMode.ResizeToContents)  # 审核
        header.setSectionResizeMode(10, QHeaderView.ResizeMode.ResizeToContents)  # 审核员
        header.setSectionResizeMode(11, QHeaderView.ResizeMode.ResizeToContents)  # 条目ID
        for c in (1, 2, 5, 6, 8):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.currentCellChanged.connect(self._on_table_cell_changed)
        header.sectionClicked.connect(self._on_header_clicked)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._on_header_context)
        ll.addWidget(self.table, 1)

        splitter.addWidget(left)

        # 右侧面板
        right = QWidget()
        right.setObjectName("rightPane")
        right.setStyleSheet(
            "#rightPane { border-left: 1px solid #34445a; padding-left: 8px; }"
        )
        rl = QVBoxLayout(right)
        self.conn_label = QLabel("8111 连接状态: 未连接")
        self.feed_label = QLabel("WT Live 访问: 未检测")
        self.feed_label.setWordWrap(True)
        feed_check_btn = QPushButton("检测")
        feed_check_btn.setFixedWidth(56)
        feed_check_btn.setToolTip("手动检测 WT Live 连通性")
        feed_check_btn.clicked.connect(self._check_feed)
        feed_row = QWidget()
        feed_lay = QHBoxLayout(feed_row)
        feed_lay.setContentsMargins(0, 0, 0, 0)
        feed_lay.setSpacing(4)
        feed_lay.addWidget(self.feed_label, 1)
        feed_lay.addWidget(feed_check_btn)
        self.wtlive_label = QLabel("WT Live 访问(本次): 0 次")
        self.battle_label = QLabel("对局状态: 等待检测")
        self.battle_label.setWordWrap(True)
        group = QGroupBox("当前对局已收集昵称")
        gl = QVBoxLayout(group)
        self.nick_list = QListWidget()
        gl.addWidget(self.nick_list)
        rl.addWidget(self.conn_label)
        rl.addWidget(feed_row)
        rl.addWidget(self.wtlive_label)
        rl.addWidget(self.battle_label)
        rl.addWidget(group)
        # 条目备注编辑窗口: 与表格选中条目的备注联动
        remark_group = QGroupBox("条目备注编辑(与选中条目联动)")
        rgl = QVBoxLayout(remark_group)
        self.remark_editor = _RemarkEditor(_REMARK_MAX)
        self.remark_editor.setPlaceholderText("在表格中选中一条, 在此编辑其备注(自动同步到表格)")
        self.remark_editor.setMaximumHeight(150)
        self.remark_editor.textChanged.connect(self._on_remark_editor_changed)
        rgl.addWidget(self.remark_editor)
        # 已输入字数 / 最大字数 计数器
        self.remark_counter = QLabel(f"已输入 0/{_REMARK_MAX} 字")
        self.remark_counter.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.remark_counter.setStyleSheet("color: #888888; font-size: 12px;")
        rgl.addWidget(self.remark_counter)
        rl.addWidget(remark_group)
        # 昵称变更文字提醒(仅右侧昵称统计区下方, 不弹窗)
        self.reminder_label = QLabel("")
        self.reminder_label.setWordWrap(True)
        self.reminder_label.setStyleSheet("color:#e67e22;font-weight:bold;")
        self.reminder_label.hide()
        rl.addWidget(self.reminder_label)
        # 审核功能区(审核员昵称 / 上传 / 删除)
        self._audit_panel = AuditPanel(self)
        rl.addWidget(self._audit_panel)
        self._audit_panel.upload_started.connect(lambda: self._set_io_busy("audit", True))
        self._audit_panel.upload_finished.connect(self._on_upload_done)
        self._audit_panel.delete_started.connect(lambda: self._set_io_busy("audit", True))
        self._audit_panel.delete_finished.connect(self._on_delete_done)
        self._audit_panel.retry_notice.connect(self._on_audit_retry)
        rl.addStretch(1)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)

        self.setCentralWidget(splitter)
        self.statusBar().showMessage("就绪")

    # ------------------------------------------------------------------
    # 表格
    # ------------------------------------------------------------------
    def _reload_table(self) -> None:
        self.table.setRowCount(0)
        self._id_labels.clear()
        self._row_widgets.clear()
        for entry in self.store.entries:
            self._make_row(entry)

    def _make_row(self, entry: BlacklistEntry) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)

        check = QCheckBox()
        check.setStyleSheet("margin-left:7px;")
        check.setToolTip("勾选该条目(用于导出/删除等操作)")
        check.toggled.connect(self._update_header_check)
        self.table.setCellWidget(row, 0, check)

        nick = QLineEdit(entry.nickname)
        nick.setPlaceholderText("玩家昵称")
        nick.setMaxLength(32)  # 官网: 昵称不超过 16 字符, 但部分昵称可达 32
        nick.setValidator(QRegularExpressionValidator(
            QRegularExpression(r"[\p{L}\p{N}_\-\s#]*"), nick
        ))  # 字母/数字/_/-/#/空格(含 Unicode 中文)
        nick.setToolTip("昵称: 不超过32字符, 仅字母/数字/_/-/#/空格")
        self.table.setCellWidget(row, 1, nick)

        pid = QLineEdit(entry.player_id)
        pid.setPlaceholderText("玩家ID(数字)")
        pid.setMaxLength(16)  # ID 不超过 16 位数字
        pid.setValidator(QRegularExpressionValidator(
            QRegularExpression(r"\d{0,16}"), pid
        ))  # 纯数字
        pid.setToolTip("玩家ID: 纯数字, 不超过16位")
        self.table.setCellWidget(row, 2, pid)

        combo = QComboBox()
        combo.addItems(REASON_CHOICES)
        combo.setCurrentIndex(REASON_CHOICES.index(entry.reason) if entry.reason in REASON_CHOICES else 0)
        self.table.setCellWidget(row, 3, combo)

        date = DateTimeField(entry.event_date)
        self.table.setCellWidget(row, 4, date)

        prev = QLineEdit("、".join(entry.previous_nicknames))
        prev.setPlaceholderText("无")
        prev.setReadOnly(True)  # 曾用昵称由程序自动维护, 用户不可更改
        prev.setStyleSheet("background:transparent;border:none;")
        prev.setToolTip("、".join(entry.previous_nicknames) or "暂无曾用昵称")
        self.table.setCellWidget(row, 5, prev)

        link = QLineEdit(entry.replay_link)
        link.setPlaceholderText("https://...")
        link.setMaxLength(128)  # 录像链接不超过 128 字符
        link.setToolTip("录像链接: 不超过128字符")
        self.table.setCellWidget(row, 6, link)

        btn = QToolButton()
        btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon))
        btn.setToolTip("创建/打开证据文件夹")
        btn.setFixedSize(26, 26)
        self.table.setCellWidget(row, 7, btn)

        remark = QLineEdit(entry.remarks)
        remark.setPlaceholderText("备注")
        remark.setMaxLength(1000)  # 备注不超过 1000 字符
        remark.setToolTip("备注: 不超过1000字符")
        self.table.setCellWidget(row, 8, remark)

        # 审核状态(网络拉取的条目自动勾选, 用户不可修改)
        audit = QCheckBox()
        audit.setEnabled(False)  # 审核状态由网络来源/审核员决定, 用户不可改
        audit.setChecked(bool(entry.audited))
        audit.setToolTip("是否已审核(网络条目自动勾选, 不可修改)")
        self.table.setCellWidget(row, 9, audit)

        # 审核员(网络条目来源, 用户不可修改)
        auditor = QLabel(entry.auditor or "—")
        auditor.setAlignment(Qt.AlignmentFlag.AlignCenter)
        auditor.setStyleSheet("color:#8a8a9a;")
        auditor.setToolTip(entry.auditor or "本地条目, 无审核员")
        self.table.setCellWidget(row, 10, auditor)

        id_item = QLabel(entry.entry_id)
        id_item.setAlignment(Qt.AlignmentFlag.AlignCenter)
        small = QFont()
        small.setPointSize(8)
        id_item.setFont(small)
        id_item.setStyleSheet("color:#8a8a9a;")
        id_item.setToolTip(entry.entry_id)
        id_item.setWordWrap(True)
        self.table.setCellWidget(row, 11, id_item)
        self._id_labels[id(entry)] = id_item

        self._row_widgets[id(entry)] = {
            "check": check, "nick": nick, "prev": prev, "pid": pid, "link": link,
            "combo": combo, "btn": btn, "date": date, "remark": remark,
            "audit": audit, "auditor": auditor,
        }
        self._bind_row(entry, check, nick, prev, pid, link, combo, btn, date, remark)

    def _bind_row(self, entry: BlacklistEntry, check: QCheckBox, nick: QLineEdit,
                  prev: QLineEdit, pid: QLineEdit, link: QLineEdit, combo: QComboBox,
                  btn: QToolButton, date: DateTimeField, remark: QLineEdit) -> None:
        # ---- 服务器条目锁定: 所有编辑控件只读, 仅可查看/删除 ----
        locked = bool(entry.locked)
        if locked:
            for w in (nick, pid, link, remark):
                w.setReadOnly(True)
            date.setReadOnly(True)
            combo.setEnabled(False)
            btn.setEnabled(False)
            for w in (nick, pid, link, date, remark):
                w.setToolTip("来自服务器下载的条目, 锁定不可编辑")
                w.setStyleSheet("background:#1a2230;color:#b8c4d4;")
            check.setToolTip("服务器条目可勾选用于删除或导出")

        def on_nick(text: str) -> None:
            if locked:
                return
            entry.nickname = text
            self.store.save()
            self._update_nickname_reminder()

        def on_pid(text: str) -> None:
            if locked:
                return
            entry.player_id = text
            self._maybe_generate_id(entry)
            self.store.save()

        def on_link(text: str) -> None:
            if locked:
                return
            entry.replay_link = text
            self.store.save()

        def on_date(text: str) -> None:
            if locked:
                return
            entry.event_date = text
            self._maybe_generate_id(entry)
            self.store.save()

        def on_reason(index: int) -> None:
            if locked:
                return
            entry.reason = REASON_CHOICES[index]
            self.store.save()

        def on_remark(text: str) -> None:
            if locked:
                return
            entry.remarks = text
            self._schedule_save()  # 防抖保存
            # 若该条正是备注编辑器当前关联的条目, 同步到编辑器(防循环)
            if self._remark_entry is entry and not self._remark_locked:
                self._remark_locked = True
                self.remark_editor.setPlainText(text)
                self._remark_locked = False
                self._update_remark_counter()

        def on_folder() -> None:
            if not entry.entry_id:
                QMessageBox.warning(
                    self, "缺少信息",
                    '需要补充更多信息:"玩家ID"与"事件发生日期"',
                )
                return
            pid_text = (entry.player_id or "").strip()
            if not pid_text:
                QMessageBox.warning(
                    self, "缺少信息",
                    '需要补充更多信息:"玩家ID"与"事件发生日期"',
                )
                return
            try:
                path = ensure_and_open(pid_text, entry.entry_id)
                self.statusBar().showMessage(f"已打开证据文件夹: {path}", 6000)
            except Exception as exc:  # noqa: BLE001
                log.exception("open evidence folder failed")
                QMessageBox.warning(self, "错误", f"打开证据文件夹失败:\n{exc}")

        nick.textChanged.connect(on_nick)
        pid.textChanged.connect(on_pid)
        link.textChanged.connect(on_link)
        date.valueChanged.connect(on_date)
        combo.currentIndexChanged.connect(on_reason)
        btn.clicked.connect(on_folder)
        remark.textChanged.connect(on_remark)

    def _maybe_generate_id(self, entry: BlacklistEntry) -> None:
        if entry.entry_id:
            return  # 已生成,保持稳定不再变动
        if not entry.needs_entry_id():
            return
        base = entry.generate_entry_id()
        used = self.store.entry_ids()
        eid = base
        k = 1
        while eid in used:
            k += 1
            eid = f"{base}_{k}"
        entry.entry_id = eid
        label = self._id_labels.get(id(entry))
        if label is not None:
            label.setText(eid)
            label.setToolTip(eid)
        self.store.save()
        log.info("generated entry_id %s", eid)

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def _add_row(self) -> None:
        entry = BlacklistEntry(reason=DEFAULT_REASON)
        self.store.add(entry)
        self._make_row(entry)
        self.table.scrollToBottom()
        self.statusBar().showMessage("已添加新条目,请填写玩家ID与事件发生日期", 4000)

    def _delete_selected(self) -> None:
        # 基于勾选列删除
        rows = [r for r, e in enumerate(self.store.entries)
                if self._row_widgets.get(id(e), {}).get("check") is not None
                and self._row_widgets[id(e)]["check"].isChecked()]
        if not rows:
            QMessageBox.information(self, "提示", "请先勾选要删除的行")
            return
        box = QMessageBox(self)
        box.setWindowTitle("确认删除")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"确定删除勾选的 {len(rows)} 行?")
        del_ev = QCheckBox("同时删除对应的证据文件夹")
        del_ev.setChecked(True)  # 默认开启
        box.setCheckBox(del_ev)
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        deleted: list[BlacklistEntry] = []
        for r in reversed(rows):
            if 0 <= r < len(self.store.entries):
                entry = self.store.entries[r]
                deleted.append(entry)
                self._id_labels.pop(id(entry), None)
                self._row_widgets.pop(id(entry), None)
                self.store.remove_at(r)
                self.table.removeRow(r)
        # 删除对应的证据文件夹(默认勾选)
        removed_ev = 0
        if del_ev.isChecked():
            for e in deleted:
                pid = (e.player_id or "").strip()
                eid = (e.entry_id or "").strip()
                if not pid.isdigit() or not eid:
                    continue
                try:
                    folder = evidence_folder(pid, eid)
                    if os.path.isdir(folder):
                        shutil.rmtree(folder)
                        removed_ev += 1
                except Exception:  # noqa: BLE001
                    pass
        # 若备注编辑器关联的条目被删除, 清空编辑器(无论是否删了证据都要清理)
        if self._remark_entry is not None and self._remark_entry in deleted:
            self._remark_entry = None
            self._remark_locked = True
            self.remark_editor.clear()
            self._remark_locked = False
            self._update_remark_counter()
        if removed_ev:
            self.statusBar().showMessage(
                f"已删除 {len(deleted)} 行及 {removed_ev} 个证据文件夹", 4000
            )
        else:
            self.statusBar().showMessage("已删除勾选条目", 3000)

    def _detect_unused_evidence(self) -> None:
        """检测证据目录中没有对应条目的文件夹, 询问是否删除并列出目录。"""
        root = evidences_dir()
        used = {(e.entry_id or "").strip() for e in self.store.entries if e.entry_id}
        orphans: list[tuple[str, str, str]] = []  # (pid, eid, path)
        if os.path.isdir(root):
            for pid in sorted(os.listdir(root)):
                ppath = os.path.join(root, pid)
                if not os.path.isdir(ppath) or not pid.isdigit():
                    continue
                for eid in sorted(os.listdir(ppath)):
                    epath = os.path.join(ppath, eid)
                    if os.path.isdir(epath) and eid not in used:
                        orphans.append((pid, eid, epath))
        if not orphans:
            QMessageBox.information(
                self, "未使用证据检测", "没有找到未使用(无对应条目)的证据文件夹。"
            )
            return
        lines = "\n".join(f"{pid}\\{eid}" for pid, eid, _ in orphans[:50])
        more = f" 等共 {len(orphans)} 个" if len(orphans) > 50 else f"共 {len(orphans)} 个"
        box = QMessageBox(self)
        box.setWindowTitle("未使用证据检测")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"发现无对应条目的证据文件夹({more}):\n\n{lines}\n\n是否删除这些文件夹?")
        del_ev = QCheckBox("删除文件夹及其内容")
        del_ev.setChecked(True)
        box.setCheckBox(del_ev)
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        if del_ev.isChecked():
            for pid, eid, epath in orphans:
                try:
                    shutil.rmtree(epath)
                except OSError:
                    pass
            self.statusBar().showMessage(
                f"已删除 {len(orphans)} 个未使用证据文件夹", 4000
            )

    # ------------------------------------------------------------------
    # 勾选辅助: 全选 / 取消全选 / 反选
    # ------------------------------------------------------------------
    def _select_all(self) -> None:
        for w in self._row_widgets.values():
            w["check"].setChecked(True)

    def _deselect_all(self) -> None:
        for w in self._row_widgets.values():
            w["check"].setChecked(False)

    def _invert_selection(self) -> None:
        for w in self._row_widgets.values():
            w["check"].setChecked(not w["check"].isChecked())

    # ------------------------------------------------------------------
    # 勾选列表头: 全选 / 取消全选 / 反选(集成到表格勾选按钮)
    # ------------------------------------------------------------------
    def _all_checked(self) -> bool:
        return bool(self.store.entries) and all(
            self._row_widgets.get(id(e), {}).get("check") is not None
            and self._row_widgets[id(e)]["check"].isChecked()
            for e in self.store.entries
        )

    def _update_header_check(self) -> None:
        n = len(self.store.entries)
        if n == 0:
            self._header_check_item.setText("☐ 全选")
            return
        checked = sum(
            1 for e in self.store.entries
            if self._row_widgets.get(id(e), {}).get("check") is not None
            and self._row_widgets[id(e)]["check"].isChecked()
        )
        if checked == 0:
            self._header_check_item.setText("☐ 全选")
        elif checked == n:
            self._header_check_item.setText("☑ 全选")
        else:
            self._header_check_item.setText("◪ 部分")

    def _on_header_clicked(self, logical: int) -> None:
        if logical != 0:
            return
        if self._all_checked():
            self._deselect_all()
        else:
            self._select_all()
        self._update_header_check()

    def _on_header_context(self, pos) -> None:
        if self.table.horizontalHeader().logicalIndexAt(pos) != 0:
            return
        menu = QMenu(self)
        menu.addAction("全选")
        menu.addAction("取消全选")
        menu.addAction("反选")
        chosen = menu.exec(self.table.horizontalHeader().mapToGlobal(pos))
        if chosen is None:
            return
        text = chosen.text()
        if text == "全选":
            self._select_all()
        elif text == "取消全选":
            self._deselect_all()
        elif text == "反选":
            self._invert_selection()
        self._update_header_check()

    # ------------------------------------------------------------------
    # 备注编辑器联动
    # ------------------------------------------------------------------
    def _on_table_cell_changed(self, row: int, column: int,
                               prev_row: int, prev_col: int) -> None:
        self._on_table_row_changed(row, prev_row)

    def _on_table_row_changed(self, current: int, previous: int) -> None:
        if 0 <= current < len(self.store.entries):
            self._load_remark(current)
        else:
            self._remark_entry = None
            self._remark_locked = True
            self.remark_editor.clear()
            self._remark_locked = False
            self._update_remark_counter()

    def _load_remark(self, row: int) -> None:
        e = self.store.entries[row]
        # 旧数据可能超长, 截断到 _REMARK_MAX 字符
        if e.remarks and len(e.remarks) > _REMARK_MAX:
            e.remarks = e.remarks[:_REMARK_MAX]
        self._remark_entry = e
        self._remark_locked = True
        self.remark_editor.setPlainText(e.remarks)
        self._remark_locked = False
        # 服务器锁定条目: 备注编辑器同样只读(防止右侧编辑绕过锁定)
        self.remark_editor.setReadOnly(bool(e.locked) or self._locked)
        self._update_remark_counter()

    def _update_remark_counter(self) -> None:
        n = len(self.remark_editor.toPlainText())
        self.remark_counter.setText(f"已输入 {n}/{_REMARK_MAX} 字")

    def _on_remark_editor_changed(self) -> None:
        if self._remark_locked or self._remark_entry is None:
            return
        if self._remark_entry.locked:
            return  # 服务器锁定条目不可编辑(数据层保护, 双保险)
        text = self.remark_editor.toPlainText()
        # 正常输入已被 _RemarkEditor 拦截, 这里兜底处理 undo/redo 等边缘超长
        if len(text) > _REMARK_MAX:
            self._schedule_truncate()
            text = text[:_REMARK_MAX]
        e = self._remark_entry
        e.remarks = text
        self._schedule_save()  # 防抖: 快速输入不逐键写盘
        w = self._row_widgets.get(id(e))
        if w is not None:
            # 加锁防回写(表格行→编辑器 setPlainText 会把光标重置到开头)
            self._remark_locked = True
            w["remark"].setText(text)
            self._remark_locked = False
        self._update_remark_counter()

    def _schedule_truncate(self) -> None:
        if not getattr(self, "_truncate_pending", False):
            self._truncate_pending = True
            QTimer.singleShot(0, self._truncate_remark)

    def _truncate_remark(self) -> None:
        self._truncate_pending = False
        if self._remark_entry is None:
            return
        editor = self.remark_editor
        text = editor.toPlainText()
        if len(text) <= _REMARK_MAX:
            return
        # 用光标局部删除超出部分(保留开头), 不重建整个文档——
        # setPlainText 在 IME 合成/高频输入下会破坏输入法上下文导致原生崩溃。
        pos = editor.textCursor().position()
        self._remark_locked = True
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.movePosition(
            QTextCursor.MoveOperation.Left,
            QTextCursor.MoveMode.KeepAnchor,
            len(text) - _REMARK_MAX,
        )
        cursor.removeSelectedText()
        # 恢复光标(不超出新长度)
        c = editor.textCursor()
        c.setPosition(min(pos, _REMARK_MAX))
        editor.setTextCursor(c)
        self._remark_locked = False
        self._update_remark_counter()
        # 同步截断后的数据与表格行
        e = self._remark_entry
        e.remarks = editor.toPlainText()
        self._schedule_save()
        w = self._row_widgets.get(id(e))
        if w is not None:
            self._remark_locked = True
            w["remark"].setText(e.remarks)
            self._remark_locked = False

    def _schedule_save(self) -> None:
        if not hasattr(self, "_save_timer"):
            self._save_timer = QTimer(self)
            self._save_timer.setSingleShot(True)
            self._save_timer.setInterval(500)
            self._save_timer.timeout.connect(self._flush_save)
        self._save_timer.start()

    def _flush_save(self) -> None:
        try:
            self.store.save()
        except Exception:  # noqa: BLE001
            log.exception("自动保存失败")

    def _manual_check(self) -> None:
        if self._worker is not None:
            self._worker.manual_requested.emit()

    def _check_feed(self) -> None:
        if self._worker is not None:
            self._worker.feed_requested.emit()

    # ------------------------------------------------------------------
    # 锁定条目 / 关于
    # ------------------------------------------------------------------
    def _toggle_lock(self) -> None:
        self._locked = not self._locked
        self._apply_lock_state()
        self.statusBar().showMessage(
            "已锁定,禁止修改所有条目" if self._locked else "已解锁,可正常修改", 3000
        )

    def _apply_lock_state(self) -> None:
        locked = self._locked
        # 每行只读 = 全局锁定 或 该条目本身是服务器锁定条目(叠加, 互不覆盖)
        for e in self.store.entries:
            w = self._row_widgets.get(id(e))
            if w is None:
                continue
            row_locked = locked or bool(e.locked)
            w["nick"].setReadOnly(row_locked)
            w["pid"].setReadOnly(row_locked)
            w["link"].setReadOnly(row_locked)
            w["remark"].setReadOnly(row_locked)
            w["combo"].setEnabled(not row_locked)
            w["btn"].setEnabled(not row_locked)
            w["date"].setEnabled(not row_locked)
        # 备注编辑器只读: 全局锁定, 或当前关联条目是服务器锁定条目
        editor_locked = locked or bool(self._remark_entry is not None
                                       and self._remark_entry.locked)
        self.remark_editor.setReadOnly(editor_locked)
        self._lock_action.setText("🔓 解锁条目" if locked else "🔒 锁定条目")
        self._add_action.setEnabled(not locked)
        self._delete_action.setEnabled(not locked)
        self._import_action.setEnabled(not locked)

    # ------------------------------------------------------------------
    # 导入 / 导出
    # ------------------------------------------------------------------
    def _selected_entries(self) -> list[BlacklistEntry]:
        return [e for e in self.store.entries
                if self._row_widgets.get(id(e), {}).get("check") is not None
                and self._row_widgets[id(e)]["check"].isChecked()]

    def _export_selected(self) -> None:
        entries = self._selected_entries()
        if not entries:
            QMessageBox.information(self, "提示", "请先在表格中勾选要导出的条目")
            return
        dlg = ExportDialog(entries, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        out = dlg.out_path()
        if not out:
            return
        include_ev = dlg.include_evidence()
        self._last_export_path = out
        snapshot = list(entries)
        self._set_io_busy("export", True)

        def task(cb):
            return export_zip(snapshot, out, include_evidence=include_ev,
                              progress_callback=cb)

        stats, error = self._run_progress("正在导出…", task)
        self._on_export_done(stats, error)

    @pyqtSlot(object, str)
    def _on_export_done(self, stats, error) -> None:
        self._set_io_busy("export", False)
        self.statusBar().clearMessage()
        if error:
            QMessageBox.warning(self, "导出失败", f"导出失败:\n{error}")
            return
        if not stats:
            return
        ev_text = f"已包含({stats['evidence_files']} 个文件)" if stats["evidence"] else "未包含"
        QMessageBox.information(
            self, "导出完成",
            f"已导出 {stats['entries']} 条条目\n"
            f"证据文件: {ev_text}\n"
            f"文件大小: {format_size(stats['size'])}\n"
            f"保存位置: {self._last_export_path}",
        )

    def _import_entries(self) -> None:
        dlg = ImportModeDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        path = dlg.path()
        if not path:
            return
        mode = dlg.mode()
        restore_ev = dlg.restore_evidence()
        self._last_import_mode = mode
        self._last_import_restore = restore_ev
        self._set_io_busy("import", True)

        def task(cb):
            return import_zip(path, self.store, mode=mode,
                              restore_evidence=restore_ev, progress_callback=cb)

        stats, error = self._run_progress("正在导入…", task)
        self._on_import_done(stats, error)

    def _run_progress(self, title: str, task) -> tuple:
        """后台线程执行 task(callback), 模态进度窗阻塞主界面, 返回 (stats, error)。"""
        dlg = ProgressDialog(title, self)

        def work() -> None:
            try:
                stats = task(dlg.progress_updated.emit)
                dlg.task_finished.emit(stats, "")
            except Exception as exc:  # noqa: BLE001
                dlg.task_finished.emit(None, str(exc))

        threading.Thread(target=work, daemon=True).start()
        dlg.exec()
        return dlg.stats, dlg.error

    @pyqtSlot(object, str)
    def _on_import_done(self, stats, error) -> None:
        self._set_io_busy("import", False)
        self.statusBar().clearMessage()
        if error:
            QMessageBox.warning(self, "导入失败", f"导入失败:\n{error}")
            return
        if not stats:
            return
        self._reload_table()
        mode = self._last_import_mode
        restore_ev = self._last_import_restore
        mode_name = "追加模式" if mode == "append" else "玩家ID导入"
        if stats["has_evidence"]:
            if restore_ev:
                ev_text = f"是 (已恢复 {stats['evidence_restored']} 个文件)"
            else:
                ev_text = "文件含证据(未勾选导入, 已跳过)"
        else:
            ev_text = "否"
        truncated_warn = (
            "\n\n⚠ 磁盘空间不足, 证据文件未完全导入, 仅恢复了部分文件"
            if stats.get("evidence_truncated") else ""
        )
        failed_warn = ""
        if stats.get("evidence_failed"):
            failed_warn = (f"\n\n⚠ 有 {stats['evidence_failed']} 个证据文件读取/写入失败,"
                           " 已跳过(条目不受影响)")
        QMessageBox.information(
            self, "导入统计",
            f"导入方式: {mode_name}\n\n"
            f"导入条目: {stats['imported']} 条\n"
            f"新增玩家ID: {stats['new_ids']} 个\n"
            f"包含证据文件: {ev_text}\n"
            f"文件大小: {format_size(stats['size'])}"
            f"{truncated_warn}{failed_warn}",
        )

    def _set_io_busy(self, kind: str, busy: bool) -> None:
        """导入/导出后台执行期间禁用相关控件, 结束时全部恢复(kind 仅标识)。"""
        self._export_action.setEnabled(not busy)
        self._import_action.setEnabled(not busy)
        self._add_action.setEnabled(not busy and not self._locked)
        self._delete_action.setEnabled(not busy and not self._locked)
        self._sort_combo.setEnabled(not busy)

    # ------------------------------------------------------------------
    # 排序
    # ------------------------------------------------------------------
    def _on_sort_changed(self, index: int) -> None:
        if index < 0:
            return
        text = self._sort_combo.currentText()
        field = "pid" if "玩家ID" in text else ("nick" if "玩家昵称" in text else "date")
        desc = text.endswith("↓")

        def is_empty(e: BlacklistEntry) -> bool:
            if field == "pid":
                return not (e.player_id or "").strip()
            if field == "nick":
                return not (e.nickname or "").strip()
            return not (e.event_date or "").strip()

        def key(e: BlacklistEntry):
            if field == "pid":
                v = (e.player_id or "").strip()
                return (0, int(v)) if v.isdigit() else (1, v)
            if field == "nick":
                return (e.nickname or "").strip()
            return (e.event_date or "").strip()

        entries = list(self.store.entries)
        nonempty = [e for e in entries if not is_empty(e)]
        empty = [e for e in entries if is_empty(e)]
        nonempty.sort(key=key)
        if desc:
            nonempty.reverse()
        self.store.entries = nonempty + empty
        self.store.save()
        self._reload_table()
        self.statusBar().showMessage(f"已按「{text}」排序", 3000)

    def _show_about(self) -> None:
        dlg = AboutDialog(self)
        dlg.exec()

    # ------------------------------------------------------------------
    # 叠加层设置
    # ------------------------------------------------------------------
    def _open_overlay_settings(self) -> None:
        dlg = OverlaySettingsDialog(self.app_settings.overlay, self)
        dlg.settings_changed.connect(self._on_overlay_settings_changed)
        dlg.show()
        self._overlay_settings_dialog = dlg  # 保持引用, 防止被垃圾回收

    def _on_overlay_settings_changed(self) -> None:
        self._overlay.apply_settings()
        self.app_settings.save()

    # ------------------------------------------------------------------
    # 服务器设置 / 同步 / 审核
    # ------------------------------------------------------------------
    def _open_server_settings(self) -> None:
        dlg = ServerSettingsDialog(self.app_settings, self)
        dlg.exec()
        # 审核服务器可能变化, 刷新审核员列表
        self._audit_panel._refresh_auditors()

    def _sync_servers(self) -> None:
        servers = self.app_settings.fetch_servers
        if not servers:
            QMessageBox.information(
                self, "未配置服务器",
                "尚未配置名单拉取服务器。\n请先在「🌐 服务器设置」中添加 GitHub/Gitee 仓库。",
            )
            self._open_server_settings()
            return
        names = "\n".join(f"• {s.get('url','')}" for s in servers)
        ret = QMessageBox.question(
            self, "同步服务器名单",
            f"是否从以下服务器下载名单并合并到本地?\n\n{names}\n\n"
            "(服务器条目下载后锁定为不可编辑, 由审核员维护)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._set_io_busy("sync", True)
        self._sync_dialog = SyncDialog(servers, self)
        self._sync_dialog.finished.connect(
            lambda _res: self._set_io_busy("sync", False)
        )
        self._sync_dialog.exec()

        # SyncDialog 在 _on_finished 中 accept, 这里拿到合并后的条目
        entries = getattr(self._sync_dialog, "_all", [])
        errors = getattr(self._sync_dialog, "_errors", [])
        if errors:
            self.statusBar().showMessage(
                f"同步完成, {len(errors)} 个服务器失败", 5000
            )
        if not entries:
            return
        self._merge_remote_entries(entries)

    def _merge_remote_entries(self, remote: list[dict]) -> None:
        """把服务器条目合并进本地, 处理冲突(覆盖/追加)。"""
        added = 0
        overwritten = 0
        skipped = 0
        # 以 player_id + event_date 判断本地是否已存在(覆盖只更新信息, 不删证据)
        for rd in remote:
            pid = (rd.get("player_id") or "").strip()
            date = (rd.get("event_date") or "").strip()
            found = None
            for e in self.store.entries:
                if (e.player_id or "").strip() == pid and (e.event_date or "").strip() == date:
                    found = e
                    break
            if found is None:
                # 新增
                self._append_remote_entry(rd)
                added += 1
                continue
            # 存在相同条目 → 对比窗口
            local_dict = {
                "nickname": found.nickname, "player_id": found.player_id,
                "reason": found.reason, "event_date": found.event_date,
                "remarks": found.remarks,
            }
            dlg = CompareDialog(local_dict, rd, self)
            choice = dlg.exec()
            if choice == 1:  # 覆盖: 更新信息, 保留证据文件夹与本地锁状态
                overwritten += 1
                self._overwrite_entry(found, rd)
            elif choice == 2:  # 追加: 作为新条目加入
                self._append_remote_entry(rd)
                added += 1
            else:  # 跳过
                skipped += 1
        self.store.save()
        self._reload_table()
        self._audit_panel._refresh_auditors()
        self.statusBar().showMessage(
            f"同步完成: 新增 {added} 条, 覆盖 {overwritten} 条, 跳过 {skipped} 条", 6000
        )

    def _append_remote_entry(self, d: dict) -> None:
        e = BlacklistEntry()
        e.nickname = str(d.get("nickname") or "")
        e.player_id = str(d.get("player_id") or "")
        e.reason = str(d.get("reason") or "")
        e.event_date = str(d.get("event_date") or "")
        e.replay_link = str(d.get("replay_link") or "")
        e.remarks = str(d.get("remarks") or "")
        e.previous_nicknames = [str(x) for x in (d.get("previous_nicknames") or [])]
        e.audited = True
        e.auditor = str(d.get("auditor") or "")
        e.cloud_id = str(d.get("cloud_id") or "")
        e.locked = True
        e.source = "server"
        e.entry_id = self._make_remote_entry_id(e)
        self.store.entries.append(e)

    def _overwrite_entry(self, e: BlacklistEntry, d: dict) -> None:
        """覆盖: 只更新条目信息, 保留本地证据文件夹。"""
        e.nickname = str(d.get("nickname") or e.nickname)
        e.reason = str(d.get("reason") or e.reason)
        e.replay_link = str(d.get("replay_link") or e.replay_link)
        e.remarks = str(d.get("remarks") or e.remarks)
        e.previous_nicknames = [str(x) for x in (d.get("previous_nicknames") or [])]
        e.audited = True
        e.auditor = str(d.get("auditor") or e.auditor)
        e.cloud_id = str(d.get("cloud_id") or e.cloud_id)
        e.locked = True
        e.source = "server"

    def _make_remote_entry_id(self, e: BlacklistEntry) -> str:
        """为服务器条目生成稳定的条目ID(基于 player_id + 云端ID哈希), 避免与本地冲突。"""
        import hashlib
        if e.cloud_id:
            suffix = hashlib.sha1(e.cloud_id.encode()).hexdigest()[:6]
            return f"{e.player_id}_{suffix}"
        base = e.generate_entry_id() or f"{e.player_id}_{e.event_date.replace('-','')}"
        return f"{base}_{hashlib.sha1(base.encode()).hexdigest()[:4]}"

    def _on_audit_retry(self, op: str, msg: str) -> None:
        """审核上传/删除遇到乐观锁冲突自动重试时提醒用户。"""
        self.statusBar().showMessage(f"⚠ {op} {msg}", 5000)
        if self._tray_available:
            self._tray.showMessage(
                "WTBlackList - 自动重试", f"{op}: {msg}",
                QSystemTrayIcon.MessageIcon.Warning, 3000,
            )

    def _on_upload_done(self, result: object) -> None:
        self._set_io_busy("audit", False)
        if not isinstance(result, dict):
            return
        errors = result.get("errors", [])
        results = result.get("results", [])
        if errors:
            QMessageBox.warning(
                self, "上传部分失败",
                "\n".join(results + ["⚠ " + x for x in errors]),
            )
            return
        QMessageBox.information(self, "上传成功", "\n".join(results))
        self._audit_panel._refresh_auditors()
        # 上传完成后从服务器重新获取刚上传的条目, 追加为本地锁定条目
        self._refetch_after_upload(result)

    def _refetch_after_upload(self, result: dict) -> None:
        """上传成功后, 从上传目标服务器重新拉取, 把刚上传的条目作为锁定条目追加。"""
        servers = result.get("servers", [])
        if not servers:
            return
        # 拉取(不弹窗, 静默合并)
        all_entries: list[dict] = []
        errors: list[str] = []
        for s in servers:
            try:
                from .server_sync import fetch_entries
                all_entries.extend(fetch_entries(s.get("url", "")))
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
        if not all_entries:
            return
        added = 0
        for rd in all_entries:
            pid = (rd.get("player_id") or "").strip()
            date = (rd.get("event_date") or "").strip()
            # 避免重复: 已存在相同 cloud_id 或 相同 pid+date 的服务器条目则不追加
            dup = any(
                (x.cloud_id and x.cloud_id == rd.get("cloud_id")) or
                (x.source == "server" and x.player_id == pid and x.event_date == date)
                for x in self.store.entries
            )
            if dup:
                continue
            self._append_remote_entry(rd)
            added += 1
        if added:
            self.store.save()
            self._reload_table()
            self._audit_panel._refresh_auditors()
            self.statusBar().showMessage(f"已从服务器追加 {added} 条审核条目", 5000)

    def _on_delete_done(self, result: object) -> None:
        self._set_io_busy("audit", False)
        if not isinstance(result, dict):
            return
        errors = result.get("errors", [])
        results = result.get("results", [])
        if errors:
            QMessageBox.warning(
                self, "删除部分失败",
                "\n".join(results + ["⚠ " + x for x in errors]),
            )
        else:
            QMessageBox.information(self, "删除完成", "\n".join(results))
        # 删除后本地对应的服务器条目也应移除(保持同步)
        self._remove_local_server_entries(result)

    def _remove_local_server_entries(self, result: dict) -> None:
        """删除完成后, 移除本地对应 cloud_id 的服务器锁定条目。"""
        errors = result.get("errors", [])
        if errors:
            return  # 有失败不清理本地, 避免数据不一致
        # 只在所有服务器都成功时才清理本地
        ids = self._last_delete_ids
        self._last_delete_ids = set()
        if not ids:
            return
        to_remove = [i for i, e in enumerate(self.store.entries)
                     if e.locked and e.source == "server" and e.cloud_id in ids]
        for i in reversed(to_remove):
            e = self.store.entries[i]
            self._id_labels.pop(id(e), None)
            self._row_widgets.pop(id(e), None)
            self.store.remove_at(i)
            self.table.removeRow(i)
        self.store.save()
        if to_remove:
            self.statusBar().showMessage(f"已同步移除本地 {len(to_remove)} 条服务器条目", 4000)

    # ------------------------------------------------------------------
    # 系统托盘
    # ------------------------------------------------------------------
    def _setup_tray(self) -> None:
        # 实测: QSystemTrayIcon.isSystemTrayAvailable() 在本机误报 False,
        # 但 show() 后托盘实际可正常显示。因此不依赖该返回值, 直接 show。
        self._tray_available = True
        icon = QIcon(resource_path("app.ico"))
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.setWindowIcon(icon)
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("WTBlackList 战争雷霆黑名单助手")

        menu = QMenu()
        self._overlay_action = menu.addAction("停用叠加层", self._toggle_overlay)
        menu.addAction("显示主界面", self._show_main_window)
        menu.addSeparator()
        menu.addAction("退出应用", self._quit_app)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        # 真实平台(platformName != offscreen)显示托盘图标;
        # offscreen(测试/无托盘环境)跳过, 避免 QSystemTrayIcon.show() 挂起
        if QApplication.platformName() != "offscreen":
            self._tray.show()

    def _toggle_overlay(self) -> None:
        self._overlay_enabled = not self._overlay_enabled
        self._overlay.set_enabled(self._overlay_enabled)
        self._update_overlay_action()

    def _update_overlay_action(self) -> None:
        self._overlay_action.setText(
            "停用叠加层" if self._overlay_enabled else "启用叠加层"
        )

    def _show_main_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._show_main_window()

    def _quit_app(self) -> None:
        self._really_quit = True
        try:
            self._tray.hide()
        except Exception:  # noqa: BLE001
            pass
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # ------------------------------------------------------------------
    # 监控线程
    # ------------------------------------------------------------------
    def _start_monitor(self) -> None:
        self._thread = QThread(self)
        self._worker = MonitorWorker(self.store, nickname_cache=self.nickname_cache)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.connection_changed.connect(self._on_conn)
        self._worker.new_battle.connect(self._on_new_battle)
        self._worker.battle_ended.connect(self._on_battle_ended)
        self._worker.nicknames_updated.connect(self._on_nicknames)
        self._worker.blacklist_alert.connect(self._on_alert)
        self._worker.blacklist_found.connect(self._on_blacklist_found)
        self._worker.profiles_updated.connect(self._on_profiles)
        self._worker.feed_status.connect(self._on_feed_status)
        self._worker.wtlive_count.connect(self._on_wtlive_count)
        self._worker.cache_updated.connect(self._refresh_cache_dialog)
        self._worker.prefetch_progress.connect(self._on_prefetch_progress)
        self._worker.nickname_manual_needed.connect(self._on_nickname_manual_needed)
        self._thread.start()

    @pyqtSlot(bool)
    def _on_conn(self, ok: bool) -> None:
        self.conn_label.setText("8111 连接状态: 已连接" if ok else "8111 连接状态: 未连接")
        self.conn_label.setStyleSheet(
            "font-weight:bold;" + ("color:#27ae60;" if ok else "color:#c0392b;")
        )

    @pyqtSlot()
    def _on_new_battle(self) -> None:
        self.battle_label.setText("对局状态: 检测到新对局,开始收集昵称…")
        self.battle_label.setStyleSheet("color:#e67e22;font-weight:bold;")
        self._overlay.set_battle(True)
        self._overlay.set_found([])

    @pyqtSlot()
    def _on_battle_ended(self) -> None:
        self.battle_label.setText("对局状态: 对局结束/离开")
        self.battle_label.setStyleSheet("color:#7f8c8d;font-weight:bold;")
        self._overlay.set_battle(False)

    @pyqtSlot(list)
    def _on_nicknames(self, names: list) -> None:
        self.nick_list.clear()
        self.nick_list.addItems(names)
        self.statusBar().showMessage(f"当前对局已记录 {len(names)} 个昵称", 3000)

    @pyqtSlot(str, str, str)
    def _on_alert(self, nickname: str, player_id: str, reason: str) -> None:
        # 叠加层已在屏幕上提示, 这里仅作非侵入式记录, 不再弹窗
        log.info("blacklist player in battle: nick=%s id=%s reason=%s",
                 nickname, player_id, reason)
        self.statusBar().showMessage(
            f"发现黑名单中玩家: {nickname} ({player_id}) 原因: {reason}", 5000
        )

    @pyqtSlot(str, str)
    def _on_nickname_manual_needed(self, player_id: str, current_nickname: str) -> None:
        """WTLive 与官网都查不到该玩家 → 弹交互式浏览器兜底对话框。"""
        dlg = BrowserCaptureDialog(player_id, current_nickname, self)
        dlg.nickname_captured.connect(self._on_browser_nickname_captured)
        dlg.show()
        self._browser_capture_dialog = dlg  # 保持引用

    def _apply_fetched_nickname(self, entry: BlacklistEntry, nick: str) -> None:
        """用抓取到的官方昵称更新条目:
        - 官方改名历史: 旧抓取昵称与新抓取不同 → 记入曾用昵称;
        - 需求: 用官方昵称替换“玩家昵称”字段, 用户手填旧昵称不同 → 记入曾用昵称;
        - 同步表格(曾用昵称列 + 玩家昵称输入框)。
        锁定条目(服务器条目)只更新内部字段, 不改写用户可见字段。
        """
        old_fetched = (entry.fetched_nickname or "").strip()
        if old_fetched and old_fetched != nick:
            entry.push_previous_nickname(old_fetched)
        entry.fetched_nickname = nick
        entry.fetched_at = time.time()

        if not entry.locked:
            manual = (entry.nickname or "").strip()
            if manual and manual != nick:
                entry.push_previous_nickname(manual)
            if manual != nick:
                entry.nickname = nick

        w = self._row_widgets.get(id(entry))
        if w is not None:
            joined = "、".join(entry.previous_nicknames)
            w["prev"].setText(joined)
            w["prev"].setToolTip(joined or "暂无曾用昵称")
            if not entry.locked:
                w["nick"].setText(nick)

    @pyqtSlot(str, object, str)
    def _on_browser_nickname_captured(self, player_id: str, nick: object, state: str) -> None:
        """浏览器兜底抓到昵称 → 替换玩家昵称 + 维护曾用昵称 + 缓存。"""
        nick_str = str(nick) if nick else ""
        if not nick_str:
            self.statusBar().showMessage(f"玩家ID {player_id} 昵称抓取失败: {state}", 5000)
            return
        from .nickname_util import clean_wtlive_nickname
        nick_clean = clean_wtlive_nickname(nick_str)
        changed = False
        for entry in self.store.entries:
            if (entry.player_id or "").strip() == player_id:
                self._apply_fetched_nickname(entry, nick_clean)
                changed = True
                self.nickname_cache.set(player_id, nick_clean, entry.fetched_at, save=True)
        if changed:
            self.store.save()
            self._refresh_cache_dialog()
            self._update_nickname_reminder()
            self.statusBar().showMessage(f"已通过浏览器更新昵称: {nick_clean}", 5000)
        else:
            self.statusBar().showMessage(f"未找到玩家ID {player_id} 的条目", 5000)

    @pyqtSlot(dict)
    def _on_profiles(self, result: dict) -> None:
        """收到通过ID访问 WT Live 获取到的昵称: 替换玩家昵称 + 维护曾用昵称 + 持久化。"""
        if not result:
            return
        changed = False
        for entry in self.store.entries:
            pid = (entry.player_id or "").strip()
            nick = result.get(pid)
            if not nick:
                continue
            self._apply_fetched_nickname(entry, nick)
            changed = True
        if changed:
            self.store.save()
            self._refresh_cache_dialog()
            self._update_nickname_reminder()
        self.statusBar().showMessage(f"已获取 {len(result)} 个黑名单玩家昵称", 4000)

    def _update_nickname_reminder(self) -> None:
        """当通过ID获取到的昵称与用户填写的玩家昵称不一致时, 在右侧统计区下方文字提醒。"""
        msgs = []
        for entry in self.store.entries:
            manual = (entry.nickname or "").strip()
            fetched = (entry.fetched_nickname or "").strip()
            pid = (entry.player_id or "").strip()
            if manual and fetched and manual != fetched:
                msgs.append(
                    f"⚠ 玩家ID {pid or '?'} 昵称已变更为「{fetched}」, "
                    f"与您填写的「{manual}」不一致, 请点击「🔁 刷新ID对应昵称」后更新玩家昵称"
                )
        if msgs:
            self.reminder_label.setText("\n".join(msgs))
            self.reminder_label.show()
        else:
            self.reminder_label.clear()
            self.reminder_label.hide()

    def _open_nickname_sync(self) -> None:
        """打开共享昵称表同步对话框(拉取合并 / 开关 / issue 上传)。"""
        dlg = NicknameSyncDialog(self.app_settings, self.nickname_cache, self)
        dlg.exec()

    def _open_proxy_settings(self) -> None:
        """打开代理设置对话框: 设置后所有网络请求走代理。"""
        dlg = ProxyDialog(self.app_settings, self)
        dlg.exec()

    def _open_cache(self) -> None:
        dlg = getattr(self, "_cache_dialog", None)
        if dlg is None:
            dlg = CacheDialog(self.nickname_cache, self)
            self._cache_dialog = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _refresh_cache_dialog(self) -> None:
        dlg = getattr(self, "_cache_dialog", None)
        if dlg is not None and dlg.isVisible():
            dlg.refresh()

    @pyqtSlot(list)
    def _on_blacklist_found(self, names: list) -> None:
        self._overlay.set_found(list(names))

    @pyqtSlot(str, str)
    def _on_feed_status(self, level: str, text: str) -> None:
        self.feed_label.setText(f"WT Live 访问: {text}")
        color = {"good": "#27ae60", "warn": "#e67e22", "bad": "#c0392b"}.get(level, "#7f8c8d")
        self.feed_label.setStyleSheet(f"font-weight:bold;color:{color};")
        self.statusBar().showMessage(f"WT Live 连通性检测: {text}", 4000)

    @pyqtSlot(int)
    def _on_wtlive_count(self, count: int) -> None:
        self.wtlive_label.setText(f"WT Live 访问(本次): {count} 次")

    @pyqtSlot(int, int)
    def _on_prefetch_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.statusBar().showMessage(f"正在抓取黑名单昵称 {done}/{total}…", 2000)

    # ------------------------------------------------------------------
    def _ask_close_mode(self) -> str:
        """点X时询问关闭方式, 返回 'quit'(关闭程序) 或 'tray'(收起到系统托盘)。"""
        # offscreen(自动化测试/无桌面)不弹窗, 直接视为关闭, 避免模态框阻塞测试
        if QApplication.platformName() == "offscreen":
            return "quit"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("退出确认")
        box.setText(
            "关闭主界面后程序仍会在后台监控对局, 可从系统托盘重新打开。\n"
            "请选择关闭方式:"
        )
        btn_quit = box.addButton("关闭程序", QMessageBox.ButtonRole.AcceptRole)
        btn_tray = box.addButton("收起到系统托盘", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return "quit" if box.clickedButton() is btn_quit else "tray"

    def closeEvent(self, event) -> None:  # noqa: N802
        # 点击“X” → 弹窗询问: 关闭程序 or 收起到系统托盘
        if not self._really_quit:
            if self._ask_close_mode() == "tray":
                event.ignore()
                self.hide()
                try:
                    self._tray.showMessage(
                        "WTBlackList",
                        "应用已最小化到系统托盘",
                        QSystemTrayIcon.MessageIcon.Information,
                        2000,
                    )
                except Exception:  # noqa: BLE001
                    pass
                return
            self._really_quit = True
        self._do_quit()
        super().closeEvent(event)

    def _do_quit(self) -> None:
        """真正退出: 冲刷保存 → 停监控线程 → 关叠加层 → 隐藏托盘。"""
        self._flush_save()  # 冲刷防抖保存, 避免丢失最后修改
        if self._worker is not None:
            self._worker.stop()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
        try:
            self._overlay.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._tray.hide()
        except Exception:  # noqa: BLE001
            pass
