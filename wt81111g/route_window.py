"""跑图线路测试器 —— 主窗口。

布局(左 / 右 / 下 三部分):
    ┌────────┬──────────────────────────┐
    │        │                          │
    │ 侧栏   │        地图显示区         │
    │ (全高) │     (右上角向下向左延伸)  │
    │  两级  ├──────────────────────────┤
    │  树形  │       信息显示区          │
    └────────┴──────────────────────────┘

- 侧栏: 一级=地图名(可展开), 二级=线路记录(右键删除、双击回放);
- 地图: 8111 地图图片 + 玩家位置 + 轨迹(按速度着色) + 标记圈与时间;
- 信息: 顶部为轨迹回放(与下方录制功能用分割线隔开),
        下方为载具型号 / 速度 / 计时器 + 三个录制按钮(支持全局快捷键)。
        换图(地图图片指纹变化)时自动清除不属于当前地图的轨迹。
"""
from __future__ import annotations

import logging
import os
import time

from PyQt6.QtCore import QSettings, QSize, Qt
from PyQt6.QtGui import QAction, QBrush, QCloseEvent, QFont, QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFrame, QGridLayout, QHeaderView,
    QHBoxLayout, QInputDialog, QLabel, QMainWindow, QMenu, QMessageBox, QPushButton,
    QSizePolicy, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .global_hotkey import ACTION_LABELS, DEFAULT_BINDINGS, GlobalHotkeyWatcher, describe_binding
from .icons import ICON_SIZE, eye_icon, pin_icon
from .map_cache import MapCache
from .map_manager import MapManagerDialog
from .route_map_view import RouteMapView
from .route_playback import PLAYBACK_SPEEDS, RoutePlayer
from .route_recorder import DEFAULT_INTERVAL_MS, RouteRecorder
from .route_store import (
    PLACEHOLDER_MAP_KEY, RouteRecord, RouteStore, fmt_speed, fmt_time, fmt_time_ms,
)
from .sound import play as play_sound
from .sound import play_error as play_error_sound
from .zones import layout_spread, ownership_text, zones_to_json

log = logging.getLogger("route_window")

# 采样间隔选项(毫秒):
# 40ms(≈25Hz)与 8111 陆战数据更新率匹配; 20ms(≈50Hz)接近 8111 请求上限
INTERVAL_CHOICES_MS = (20, 25, 30, 40, 50, 75, 100)

_ROLE_KIND = Qt.ItemDataRole.UserRole
_ROLE_ID = Qt.ItemDataRole.UserRole + 1
_ROLE_SIG = Qt.ItemDataRole.UserRole + 2      # 版本(战区布局)的坐标指纹

# 侧栏列: 0=眼睛(显示/隐藏轨迹) 1=地图/线路 2=用时
_COL_EYE = 0
_COL_NAME = 1
_COL_TIME = 2

# 眼睛列宽度(px): 轨迹条目在最里层(地图 → 模式 → 轨迹, 缩进 14×3),
# 图标 18px, 再加一点间距 —— 取小了图标会被列边界裁掉/压到名称列的文字上
EYE_COLUMN_WIDTH = 4 * 12 + 18 + 8


class RouteTesterWindow(QMainWindow):
    """跑图线路测试器主窗口。"""

    def __init__(self, store: RouteStore | None = None, parent=None,
                 map_cache: MapCache | None = None, start_recorder: bool = True):
        super().__init__(parent)
        self.setWindowTitle("跑图线路测试器 —— 战争雷霆")
        self.resize(1400, 880)

        self.store = store or RouteStore()
        # 地图图片本地缓存: 关掉游戏后仍能用缓存的底图看回放
        # (map_cache / start_recorder 可注入: 测试里指向临时目录且不启动真实 8111 轮询)
        # 必须在 _reload_tree() 之前赋值 —— 侧栏会用它判断“哪些地图只有缓存图片”
        self.map_cache = map_cache if map_cache is not None else MapCache()
        self._last_duration = 0.0
        self._last_text_at = 0.0
        self._last_map_try = 0.0      # 上次尝试用缓存底图的时间(节流)
        self._last_record: RouteRecord | None = None
        # 眼睛点亮的轨迹 id: 它们会同时显示在地图上(取代了以前 Shift 多选的做法)
        self._visible_ids: set[str] = set()
        # 选中集合(决定回放播哪几条; 用于判断“本次新加进来的是哪张地图”)
        self._sel_ids: set[str] = set()
        # “基准版本”: 双击某条轨迹后, 它的(地图键, 战区指纹)被锁为基准 ——
        # 之后多选/回放只能在同一版本内进行(见 _group_of / _on_tree_selection_changed)
        self._anchor: tuple[str, str] | None = None
        self._reselecting = False
        # “粘性状态栏文字”: 提示类信息(回放开始/结束、显示切换…)在若干秒内不被刷新覆盖
        self._sticky_text = ""
        self._sticky_until = 0.0
        # 采样间隔(毫秒, 默认 40ms ≈ 25Hz)持久化(PyQt 自带的 QSettings)
        self._settings = QSettings("WTBlackList", "RouteTester")
        try:
            self._saved_ms = int(self._settings.value("interval_ms", DEFAULT_INTERVAL_MS))
        except (TypeError, ValueError):
            self._saved_ms = int(DEFAULT_INTERVAL_MS)
        # “以速度从零变化时刻作为起始时间点”是**非默认开启**的功能: 每次启动都不勾选。
        # 不写入 QSettings —— 否则上次勾选的状态会被带到下次启动, 看起来就像默认开启。
        self._saved_on_move = False
        if self._settings.contains("start_on_move"):   # 清掉早期版本留下的值
            self._settings.remove("start_on_move")
        # 「显示战区」(小菱形 + A/B/C 字母)开关: 菜单「设置」里勾选, 记住设置
        try:
            self._show_zones = int(self._settings.value("show_zones", 1)) != 0
        except (TypeError, ValueError):
            self._show_zones = True

        self._build_ui()
        self._build_menu()
        self.map_view.set_show_zones(self._show_zones)
        self._reload_tree()
        # 后台轮询 8111
        self.recorder = RouteRecorder(self.store, parent=self, map_cache=self.map_cache)
        self.recorder.tick.connect(self._on_tick)
        self.recorder.map_changed.connect(self._on_map_changed)
        self.recorder.map_cached.connect(self._on_map_cached)
        self.recorder.start_armed.connect(self._on_start_armed)
        self.recorder.recording_started.connect(self._on_recording_started)
        self.recorder.recording_cancelled.connect(self._on_recording_cancelled)
        self.recorder.mark_added.connect(self._on_mark_added)
        self.recorder.recording_finished.connect(self._on_recording_finished)
        self.recorder.notice.connect(self._on_notice)
        if start_recorder:
            self.recorder.start()
        self.recorder.set_interval_ms(self._saved_ms)
        self.lbl_rate.setText(f"目标 {self._saved_ms} ms")

        # 轨迹回放(按倍速推进 playhead)
        self.player = RoutePlayer(parent=self)
        self.player.progress.connect(self._on_playback_progress)
        self.player.playing_changed.connect(self._on_playback_state)
        self.player.finished.connect(self._on_playback_finished)
        self.player.reached_mark.connect(self._on_playback_mark)
        try:
            saved_speed = float(self._settings.value("playback_speed", 1.0))
        except (TypeError, ValueError):
            saved_speed = 1.0
        idx_speed = self.cmb_speed.findData(saved_speed)
        if idx_speed >= 0:
            self.cmb_speed.setCurrentIndex(idx_speed)
        self.player.set_speed(saved_speed)

        # 全局快捷键(右Shift + 小键盘 1/2/3)
        self.hotkeys = GlobalHotkeyWatcher(parent=self)
        self.hotkeys.triggered.connect(self._handle_action)
        self.hotkeys.start()

        self._update_buttons(False)
        if not GlobalHotkeyWatcher.supported():
            self.lbl_status.setText("全局快捷键仅在 Windows 下可用, 请使用下方按钮操作")
        # 启动时游戏可能没开: 尝试用缓存底图显示已有轨迹
        self._restore_map_for_current_view()

    # ==================================================================== UI
    def _build_menu(self) -> None:
        """顶部菜单栏: 「地图」(管理/缓存) / 「设置」(显示开关) / 「帮助」。"""
        menu = self.menuBar().addMenu("地图(&M)")
        self.act_manage = QAction("地图管理(命名/预览/删除缓存)…", self)
        self.act_manage.triggered.connect(self._open_map_manager)
        menu.addAction(self.act_manage)
        menu.addSeparator()
        act_dir = QAction("打开地图缓存文件夹", self)
        act_dir.triggered.connect(self._open_map_dir)
        menu.addAction(act_dir)
        self.act_refresh = QAction("用缓存底图重新显示当前轨迹", self)
        self.act_refresh.triggered.connect(self._restore_map_for_current_view)
        menu.addAction(self.act_refresh)

        setting = self.menuBar().addMenu("设置(&S)")
        self.act_zones = QAction("显示战区位置(A/B/C)", self)
        self.act_zones.setCheckable(True)
        self.act_zones.setChecked(self._show_zones)
        self.act_zones.setToolTip(
            "对战局内 8111 给出的战区(capture_zone)画小菱形 + 字母。\n"
            "战局外/看缓存底图时, 画的是这张地图以前见过的战区布局。")
        self.act_zones.toggled.connect(self._on_show_zones)
        setting.addAction(self.act_zones)

        help_menu = self.menuBar().addMenu("帮助(&H)")
        help_menu.addAction(self._make_about_action())
        act_mode = QAction("关于游戏模式怎么判定", self)
        act_mode.triggered.connect(lambda: QMessageBox.information(
            self, "关于游戏模式怎么判定",
            "8111 **不提供模式名**, 但 /map_obj.json 里每个战区都是一个\n"
            "capture_zone 对象(带 A/B/C 字母与地图坐标)。\n\n"
            "所以程序按**战区数量**判定模式:\n"
            "    3 个 → 战区控制(A/B/C 互相争夺)\n"
            "    2 个 → 战区攻陷(开局双方各一个)\n"
            "    1 个 → 战区争夺(单点争夺)\n\n"
            "战区颜色就是**归属**(站在玩家视角):\n"
            "    蓝 #174DFF = 我方, 红 #fa0C00 = 敌方, 白 #ffFFFA = 中立/未被占领\n"
            "(实测: 双方重生点/机场的蓝、红对象成对出现, 旁证了这个约定)\n\n"
            "各战区坐标串作为“模式指纹”记进每条轨迹, 因此同一张图的不同模式\n"
            "会被分开归组(侧栏: 地图 → 模式 → 轨迹)。\n"
            "战区只在战局内才有数据; 见过的布局(含颜色)会存进 routes.json,\n"
            "战局结束后依旧能在地图上画出战区(归属以中立色显示)。"))
        help_menu.addAction(act_mode)

    def _make_about_action(self) -> QAction:
        act = QAction("关于地图命名", self)
        act.triggered.connect(lambda: QMessageBox.information(
            self, "关于地图命名",
            "8111 本地接口**不提供地图名**(已实测: map_info / mission / 官方页面都没有),\n"
            "所以程序用「地图图片的哈希」区分地图。\n\n"
            "进入战局时图片会自动缓存到 data/maps/, 同一张图只存一次;\n"
            "地图名需要你在「地图」→「地图管理」里自己填, 填好后左侧列表就会显示你的名字。"))
        return act

    def _on_show_zones(self, checked: bool) -> None:
        """菜单开关: 是否在地图上画战区。"""
        self._show_zones = bool(checked)
        self._settings.setValue("show_zones", 1 if checked else 0)
        self.map_view.set_show_zones(self._show_zones)
        self._set_status("已显示地图上的战区位置" if checked else "已隐藏地图上的战区位置",
                         sticky=2.0)

    def _open_map_dir(self) -> None:
        try:
            os.startfile(self.map_cache.dir)     # noqa: S606 - 仅 Windows
        except OSError as exc:
            self._set_status(f"打开缓存目录失败: {exc}", sticky=3.0)

    def _open_map_manager(self) -> None:
        """打开「地图管理」; 关掉后总是刷新侧栏(名字可能改了, 也可能删了缓存图片)。"""
        dlg = MapManagerDialog(self.store, self.map_cache, self)
        dlg.exec()
        self._reload_tree()
        if dlg.renamed:
            self._set_status(f"已保存 {dlg.renamed} 处地图名称改动(左侧列表已同步)",
                             sticky=3.0)
        else:
            self._set_status("地图管理已关闭(名称改动会立即保存)", sticky=2.0)

    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal, central)
        splitter.addWidget(self._build_sidebar())

        right = QWidget(splitter)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        self.map_view = RouteMapView(right)
        rl.addWidget(self.map_view, 1)
        rl.addWidget(self._build_info_panel(right), 0)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 1080])
        root.addWidget(splitter)
        self.setCentralWidget(central)

    # ------------------------------------------------------------------ 侧栏
    def _build_sidebar(self) -> QWidget:
        box = QWidget(self)
        box.setMinimumWidth(200)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(8, 8, 4, 8)
        layout.setSpacing(6)

        title = QLabel("地图 / 线路")
        f = QFont()
        f.setBold(True)
        title.setFont(f)
        layout.addWidget(title)

        self.tree = QTreeWidget(box)
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["", "地图 / 线路", "用时"])
        # 缩进会占用第 0 列: 二级条目内容从 ~2×缩进 处开始, 因此眼睛列必须够宽,
        # 否则图标会溢出到名称列、与文字叠在一起(Qt 不会把图标裁剪在列内)
        self.tree.setIndentation(12)
        self.tree.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        # 眼睛列固定宽(缩进 + 图标 + 间距), 名称列吃掉剩余宽度, “用时”按内容自适应
        header = self.tree.header()
        # QTreeWidget 默认让最后一列拉伸(stretchLastSection), 会顶掉下面的列宽设置
        header.setStretchLastSection(False)
        header.setSectionResizeMode(_COL_EYE, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(_COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(_COL_TIME, QHeaderView.ResizeMode.ResizeToContents)
        header.setMinimumSectionSize(24)
        self.tree.setColumnWidth(_COL_EYE, EYE_COLUMN_WIDTH)
        self.tree.setAlternatingRowColors(True)
        # 选中的条目决定「回放」播哪几条(可 Shift/Ctrl 多选);
        # 而「地图上有没有显示」完全由眼睛图标控制 —— 两者职责分开。
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_menu)
        self.tree.itemClicked.connect(self._on_tree_item_clicked)
        self.tree.itemSelectionChanged.connect(self._on_tree_selection_changed)
        # 双击轨迹行 = 把它所在的“地图·模式·版本”定为基准(之后多选只在该版本内)
        self.tree.itemDoubleClicked.connect(self._on_tree_double_clicked)
        layout.addWidget(self.tree, 1)

        hint = QLabel("点击每行前面的眼睛图标可显示/隐藏该轨迹\n"
                      "Shift/Ctrl 多选: 「回放」会播放选中的那几条\n"
                      "右键二级条目可删除")
        hint.setStyleSheet("color:#94a3b8; font-size:11px;")
        layout.addWidget(hint)
        return box

    # -------------------------------------------------------------- 信息显示区
    def _build_info_panel(self, parent=None) -> QWidget:
        panel = QFrame(parent)
        panel.setFrameShape(QFrame.Shape.StyledPanel)
        # 5 行内容(回放 / 分割线 / 状态+间隔 / 载具… / 录制按钮)的最小高度约 151,
        # 取 172 留出余量, 同时固定高度可让上方地图区域不随文字变化抖动
        panel.setFixedHeight(172)
        grid = QGridLayout(panel)
        grid.setContentsMargins(12, 8, 12, 10)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(6)

        # ---- 第 0 行: 轨迹回放(置顶, 与下方录制功能用分割线隔开)
        play_box = QHBoxLayout()
        play_box.setSpacing(8)
        self.btn_play = QPushButton("▶ 回放")
        self.btn_play.setMinimumHeight(30)
        self.btn_play.clicked.connect(self._toggle_playback)
        play_box.addWidget(self.btn_play)

        self.btn_play_mark = QPushButton("⏭ 到下一标记暂停")
        self.btn_play_mark.setMinimumHeight(30)
        self.btn_play_mark.clicked.connect(self._play_to_next_mark)
        play_box.addWidget(self.btn_play_mark)

        play_box.addWidget(QLabel("倍速:"))
        self.cmb_speed = QComboBox()
        for x in PLAYBACK_SPEEDS:
            self.cmb_speed.addItem(f"{x:g}X", x)
        self.cmb_speed.currentIndexChanged.connect(self._on_speed_changed)
        self.cmb_speed.setToolTip("回放倍速 0.5X ~ 16X")
        play_box.addWidget(self.cmb_speed)

        self.lbl_play = QLabel("回放: --")
        self.lbl_play.setStyleSheet("color:#94a3b8; font-size:11px;")
        play_box.addWidget(self.lbl_play, 1)

        # 回放期间其余功能会被禁用, 必须用它退出回放(见 _update_buttons)
        self.btn_play_stop = QPushButton("■ 结束播放")
        self.btn_play_stop.setMinimumHeight(30)
        self.btn_play_stop.setToolTip("退出回放并恢复显示眼睛点亮的轨迹(回放期间其余功能不可用)")
        self.btn_play_stop.clicked.connect(self._end_playback)
        play_box.addWidget(self.btn_play_stop)
        grid.addLayout(play_box, 0, 0, 1, 4)

        # 分割线: 用 1px 实心线(而非 Sunken QFrame —— 在浅色主题下槽线对比度太低)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background-color:#94a3b8; border:none;")
        grid.addWidget(line, 1, 0, 1, 4)

        # ---- 第 2 行: 连接状态 + 采样间隔设置
        # 两段长文字都允许被压缩(否则窄窗口下会把侧栏挤到最小宽度)
        self.lbl_status = QLabel("正在连接 8111…")
        self.lbl_status.setStyleSheet("color:#94a3b8;")
        self.lbl_status.setMinimumWidth(240)
        self.lbl_status.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Preferred)
        grid.addWidget(self.lbl_status, 2, 0, 1, 3)

        # 采样间隔设置(影响轮询与轨迹采样密度, 越小越频繁)
        rate_box = QHBoxLayout()
        rate_box.setSpacing(6)
        rate_box.addWidget(QLabel("采样间隔:"))
        self.cmb_rate = QComboBox()
        for ms in INTERVAL_CHOICES_MS:
            self.cmb_rate.addItem(f"{ms} ms  ({1000 // ms} Hz)", ms)
        idx = self.cmb_rate.findData(self._saved_ms)
        self.cmb_rate.setCurrentIndex(
            idx if idx >= 0 else self.cmb_rate.findData(int(DEFAULT_INTERVAL_MS)))
        self.cmb_rate.currentIndexChanged.connect(self._on_rate_changed)
        self.cmb_rate.setToolTip(
            "采样间隔(毫秒), 越小越频繁。\n"
            "8111 陆战数据約每 40ms 更新一次, 40ms 即与之匹配;\n"
            "20ms(≈50Hz)已接近 8111 请求上限, 实际值见右侧")
        rate_box.addWidget(self.cmb_rate)
        self.lbl_rate = QLabel("")
        self.lbl_rate.setStyleSheet("color:#64748b; font-size:11px;")
        rate_box.addWidget(self.lbl_rate)
        rate_box.addStretch(1)
        grid.addLayout(rate_box, 2, 3)

        # ---- 第 3 行: 载具 / 速度 / 计时 / 标记
        self.lbl_vehicle = self._info_field("载具", "—")
        self.lbl_speed = self._info_field("速度", "— km/h")
        self.lbl_timer = self._info_field("计时", "00:00")
        self.lbl_marks = self._info_field("标记", "0 个")
        self.lbl_timer.setStyleSheet("font-size:18px; font-weight:bold; color:#f59e0b;")

        grid.addWidget(self.lbl_vehicle, 3, 0)
        grid.addWidget(self.lbl_speed, 3, 1)
        grid.addWidget(self.lbl_timer, 3, 2)
        grid.addWidget(self.lbl_marks, 3, 3)

        # ---- 第 4 行: 录制操作按钮
        btns = QHBoxLayout()
        btns.setSpacing(10)
        self.btn_start = QPushButton("开始录制轨迹")
        self.btn_stop = QPushButton("结束录制轨迹")
        self.btn_mark = QPushButton("标记当前位置")
        self.btn_start.clicked.connect(lambda: self._handle_action("start"))
        self.btn_stop.clicked.connect(lambda: self._handle_action("stop"))
        self.btn_mark.clicked.connect(lambda: self._handle_action("mark"))
        for b in (self.btn_start, self.btn_stop, self.btn_mark):
            b.setMinimumHeight(34)
            btns.addWidget(b)
        self.btn_start.setStyleSheet("font-weight:bold;")

        # 可选: 不按“点下按钮的时刻”计时, 而以速度从 0 变为非 0 的前一时刻为起点
        # (非默认开启: 每次启动都是未勾选状态)
        self.chk_start_on_move = QCheckBox("以速度从零变化时刻作为起始时间点")
        self.chk_start_on_move.setChecked(self._saved_on_move)
        self.chk_start_on_move.setToolTip(
            "勾选后, 点「开始录制轨迹」不会立即计时:\n"
            "等载具速度从 0 变为非 0 时, 以最近一次速度为 0 的时刻作为计时起点。\n"
            "适合“点开始录制 → 切回游戏 → 再起步”的用法, 切窗口的延迟不会被算进成绩。\n"
            "注: 若开始时车已在行驶(回溯窗口内没有速度为 0 的时刻), 仍按按下时刻计时。")
        self.chk_start_on_move.stateChanged.connect(self._on_start_on_move_changed)
        btns.addWidget(self.chk_start_on_move)

        keys = "   ".join(
            f"{ACTION_LABELS[a]} = {describe_binding(DEFAULT_BINDINGS[a])}"
            for a in ("start", "mark", "stop")
        )
        tip = QLabel(keys)
        tip.setStyleSheet("color:#94a3b8; font-size:11px;")
        tip.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        btns.addWidget(tip, 1)
        grid.addLayout(btns, 4, 0, 1, 4)

        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(3, 1)
        return panel

    @staticmethod
    def _info_field(name: str, value: str) -> QLabel:
        lbl = QLabel(f"{name}: {value}")
        lbl.setStyleSheet("font-size:13px;")
        return lbl

    # ==================================================================== 侧栏内容
    def _iter_route_items(self):
        """按侧栏顺序递归遍历全部**轨迹条目**(当前是 地图 → 模式 → 版本 → 轨迹)。

        递归而不是写死层数: 以后再加/减一层也不用改遍历处。
        """
        def walk(item):
            for i in range(item.childCount()):
                ch = item.child(i)
                if ch.data(0, _ROLE_KIND) == "route":
                    yield ch
                else:
                    yield from walk(ch)

        root = self.tree.invisibleRootItem()
        yield from walk(root)

    def _reload_tree(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        alive: set[str] = set()          # 仍存在于侧栏的轨迹 id(用于修剪显示集合)
        # 四层: 地图 → 模式(由战区数量推出) → 版本(战区布局) → 轨迹
        groups = self.store.grouped_by_variant()
        for map_key, modes in groups.items():
            total = sum(len(rows) for _m, variants in modes for _s, _n, rows in variants)
            top = QTreeWidgetItem(["", self.store.display_name(map_key), f"{total} 条"])
            top.setData(0, _ROLE_KIND, "map")
            top.setData(0, _ROLE_ID, map_key)
            f = QFont()
            f.setBold(True)
            top.setFont(_COL_NAME, f)
            self.tree.addTopLevelItem(top)
            for mode_label, variants in modes:
                n_mode = sum(len(rows) for _s, _n, rows in variants)
                mid = QTreeWidgetItem(["", mode_label, f"{n_mode} 条"])
                mid.setData(0, _ROLE_KIND, "mode")
                mid.setData(0, _ROLE_ID, map_key)
                mid.setToolTip(_COL_NAME,
                               f"{self.store.display_name(map_key)} · {mode_label}\n"
                               f"模式由 8111 的战区(capture_zone)数量推出: "
                               f"3 个=战区控制, 2 个=战区攻陷, 1 个=战区争夺")
                top.addChild(mid)
                for sig, vname, routes in variants:
                    var = QTreeWidgetItem(["", vname, f"{len(routes)} 条"])
                    var.setData(0, _ROLE_KIND, "variant")
                    var.setData(0, _ROLE_ID, map_key)
                    var.setData(0, _ROLE_SIG, sig)
                    var.setToolTip(_COL_NAME, self._variant_tip(map_key, sig, vname))
                    mid.addChild(var)
                    for rec in routes:
                        child = QTreeWidgetItem(
                            ["", rec.label, fmt_time(rec.duration)])
                        child.setData(0, _ROLE_KIND, "route")
                        child.setData(0, _ROLE_ID, rec.route_id)
                        child.setIcon(_COL_EYE, eye_icon(rec.route_id in self._visible_ids))
                        child.setToolTip(_COL_EYE, "点击显示 / 隐藏该轨迹")
                        child.setToolTip(_COL_NAME,
                                         f"{rec.map_name} · {rec.mode_label} · {vname} | "
                                         f"{rec.vehicle} | 用时 {fmt_time(rec.duration)}")
                        var.addChild(child)
                        alive.add(rec.route_id)
                    var.setExpanded(True)
                mid.setExpanded(True)
            top.setExpanded(True)
        # 已缓存图片、但还没录过轨迹的地图也列出来(灰色, 用时列写“仅缓存”):
        # 否则在「地图管理」里给它们改完名字, 主界面根本看不到任何变化。
        # 顺便把它们**已见过的模式版本**也挂上(0 条) —— 还没录到轨迹时就能右键
        # 把“区域 1/2”改成自己认得的名字(大地图/小地图…)。
        for map_key in self.store.ordered_map_keys():
            if map_key in groups or not self.map_cache.has(map_key):
                continue
            top = QTreeWidgetItem(["", self.store.display_name(map_key), "仅缓存"])
            top.setData(0, _ROLE_KIND, "map")
            top.setData(0, _ROLE_ID, map_key)
            f = QFont()
            f.setBold(True)
            top.setFont(_COL_NAME, f)
            top.setForeground(_COL_NAME, QBrush(Qt.GlobalColor.gray))
            layouts = self.store.zone_layouts(map_key)
            modes = "、".join(
                f"{x.get('mode') or '?'}({self.store.layout_display_name(map_key, x.get('sig') or '')})"
                for x in layouts) or "(还没见过战区)"
            top.setToolTip(_COL_NAME,
                           "这张地图只缓存了图片, 还没录过轨迹\n"
                           f"见过的模式/版本: {modes}\n"
                           "右键可重命名地图, 或菜单「地图 → 地图管理…」")
            self.tree.addTopLevelItem(top)
            for layout in layouts:
                sig = str(layout.get("sig") or "")
                if not sig:
                    continue
                vname = self.store.layout_display_name(map_key, sig)
                var = QTreeWidgetItem(["", f"{layout.get('mode') or '?'} · {vname}", "0 条"])
                var.setData(0, _ROLE_KIND, "variant")
                var.setData(0, _ROLE_ID, map_key)
                var.setData(0, _ROLE_SIG, sig)
                var.setForeground(_COL_NAME, QBrush(Qt.GlobalColor.gray))
                var.setToolTip(_COL_NAME, self._variant_tip(map_key, sig, vname))
                top.addChild(var)
            top.setExpanded(True)
        self.tree.blockSignals(False)
        self._visible_ids &= alive       # 被删除的记录自动从“显示中”移除
        if self._anchor is not None and not any(
                (rec := self.store.get(rid)) is not None and self._group_of(rec) == self._anchor
                for rid in alive):
            self._anchor = None          # 基准那些轨迹被删光了 → 解除锁定
        self._refresh_anchor_icon()      # 图钉要跟着基准标回对应版本行
        self._refresh_map_tracks()
        self._on_tree_selection_changed()

    def _on_tree_item_clicked(self, item: QTreeWidgetItem, col: int) -> None:
        """点击第一列的**眼睛图标** → 切换该轨迹在地图上的显示/隐藏。"""
        if col != _COL_EYE or item.data(0, _ROLE_KIND) != "route":
            return
        if self._in_playback():
            # 回放绘制优先于轨迹显示, 此时改眼睛没有意义(面板上也已禁用侧栏)
            self._set_status("回放中无法修改轨迹显示, 请先点「■ 结束播放」", sticky=2.0)
            return
        route_id = item.data(0, _ROLE_ID)
        self._set_visible(item, route_id not in self._visible_ids)

    def _visible_route_ids(self) -> list[str]:
        """按侧栏顺序列出眼睛点亮的轨迹 id。"""
        return [it.data(0, _ROLE_ID) for it in self._iter_route_items()
                if it.data(0, _ROLE_ID) in self._visible_ids]

    def _refresh_map_tracks(self) -> None:
        """把“眼睛点亮的轨迹”交给地图显示(可多条, 用于对比多次跑图)。

        显示状态只有 `_visible_ids` 一个来源 —— 图标亮/灭与实际是否显示永远一致。
        额外: **刚录制的那条**若也在显示集合里, 同时交给 live 分支用速度配色画
        (与录制中一致); 绘制时视图会自动跳过重复的那份(grey 那份不画)。
        """
        recs = [r for rid in self._visible_route_ids()
                if (r := self.store.get(rid)) is not None]
        recorder = getattr(self, "recorder", None)   # 构造早期还没有 recorder
        if recorder is not None and recorder.recording:
            # 录制中 live 是正在录的那条, 不能覆盖
            self.map_view.set_visible_tracks(recs)
            return
        hl = self._last_record
        if hl is not None and hl.route_id not in self._visible_ids:
            hl = None
        self.map_view.set_visible_tracks(recs)
        self.map_view.set_live(hl)
        self._apply_zones()

    def _zone_signature_for_view(self) -> str:
        """当前视图(回放中的 / 眼睛点亮的轨迹)对应的模式指纹(取不到就空串)。

        多条轨迹时只在它们指纹一致时才返回 —— 否则无法决定画哪套战区。
        """
        recs = self.map_view.playback_records or self.map_view.visible_tracks
        sigs = {r.zone_signature for r in recs if r.zone_signature}
        return sigs.pop() if len(sigs) == 1 else ""

    def _apply_zones(self, info: dict | None = None) -> None:
        """决定地图上画哪套战区(小菱形 + A/B/C)。

        战局内且 8111 正在给同一张图 → 用**实时**战区;
        其它情况(游戏关了/在看缓存底图) → 用该地图存过的布局:
        优先匹配“正在看的轨迹的模式指纹”, 没有就取最近一次见过的。
        """
        key = self.map_view.map_key
        if not key:
            return
        recorder = getattr(self, "recorder", None)
        live = list((info or {}).get("zones") or [])
        if live and recorder is not None and recorder.map_key == key:
            self.map_view.set_zones(zones_to_json(live))
            return
        # 旧布局: 只画字母 + 中立色 —— 当时谁占着哪个战区早已过期, 不拿来误导
        layout = self.store.layout_for(key, self._zone_signature_for_view())
        self.map_view.set_zones([
            {"label": z.get("label"), "x": z.get("x"), "y": z.get("y")}
            for z in (layout or {}).get("zones") or []
        ])

    def _selection_tracks(self) -> list[RouteRecord]:
        """侧栏中当前选中的线路(可 Shift/Ctrl 多选), 按侧栏顺序。"""
        out: list[RouteRecord] = []
        for it in self.tree.selectedItems():
            if it.data(0, _ROLE_KIND) != "route":
                continue
            rec = self.store.get(it.data(0, _ROLE_ID))
            if rec is not None:
                out.append(rec)
        return out

    def _select_route_ids(self, ids) -> None:
        """在侧栏中选中指定线路(不处理信号, 由调用方决定后续动作)。"""
        want = set(ids)
        for it in self._iter_route_items():
            it.setSelected(it.data(0, _ROLE_ID) in want)

    def _on_tree_selection_changed(self) -> None:
        """选中项变化 → 回放将播放这几条; 同时**限制为同一张地图的同一版本**。

        跨地图(底图只能是一张)/跨版本(战区位置不同, 线路本来就不可比)一起回放没有意义,
        所以选中了多个版本时只保留一个:

        * 双击定过“基准”时, **基准优先** —— Ctrl 点到别的版本不会把基准换掉(锁住);
        * 没定基准时保留“本次新加进来的那个版本”。

        基准(图钉)还会**跟随当前选中所在的版本** —— 刚开始没选中任何条目时,
        第一个点到的路线就把它的“地图·模式·版本”定为基准。
        """
        if self._reselecting or self._in_playback():
            return
        recs = self._selection_tracks()
        if not recs:
            self._sel_ids = set()
            self._update_play_label()
            return                       # 清空选中时保留基准(图钉不消失)
        groups = {self._group_of(r) for r in recs}
        if len(groups) > 1:
            keep: tuple[str, str] | None = None
            if self._anchor is not None and self._anchor in groups:
                keep = self._anchor
            else:
                new_ids = {r.route_id for r in recs} - self._sel_ids
                keep = next((self._group_of(r) for r in recs if r.route_id in new_ids), None)
                if keep is None:
                    keep = self._group_of(recs[-1])
            recs = [r for r in recs if self._group_of(r) == keep]
            self._reselecting = True
            try:
                self.tree.blockSignals(True)
                self._select_route_ids([r.route_id for r in recs])
                self.tree.blockSignals(False)
            finally:
                self._reselecting = False
            self._set_status(
                "同一张地图的同一版本才能一起回放/显示, 已只保留一个版本内的选中条目",
                sticky=3.0)
        self._sel_ids = {r.route_id for r in recs}
        grp = self._group_of(recs[0])
        if grp != self._anchor:
            self._anchor = grp          # 基准跟着当前选中走
            self._refresh_anchor_icon()
        self._update_play_label()

    def _on_tree_double_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        """双击轨迹行 → 定基准 + **把它显示到地图上并切到对应地图图片**。

        双击地图/模式/版本行不做处理(保留 Qt 默认的展开/折叠)。
        """
        if item.data(0, _ROLE_KIND) != "route":
            return
        if self._in_playback():
            self._set_status("回放中无法切换基准, 请先点「■ 结束播放」", sticky=2.0)
            return
        rec = self.store.get(item.data(0, _ROLE_ID))
        if rec is None:
            return
        self._anchor = self._group_of(rec)
        self._reselecting = True
        try:
            self.tree.blockSignals(True)
            self._select_route_ids([rec.route_id])     # 基准只选中这一条
            self.tree.blockSignals(False)
        finally:
            self._reselecting = False
        self._sel_ids = {rec.route_id}
        self._refresh_anchor_icon()
        self._update_play_label()
        # 双击同时就把它显示出来(自动隐掉其它版本), 并把底图切到它的地图
        self._set_visible(item, True)
        self._set_status(
            f"基准已锁定: {self._group_name(*self._anchor)} · {rec.label}; "
            f"已切到该地图并显示这条轨迹", sticky=4.0)

    # --------------------------------------------------- 分组(地图 + 版本)
    @staticmethod
    def _group_of(rec: RouteRecord) -> tuple[str, str]:
        """一条轨迹属于哪个分组: (地图键, 战区坐标指纹) —— 版本 = 战区位置。"""
        return (rec.map_key or "", rec.zone_signature or "")

    def _group_name(self, map_key: str, signature: str) -> str:
        """分组的可读名字(地图 · 模式 · 版本)。"""
        layout = self.store.layout_info(map_key, signature) or {}
        mode = str(layout.get("mode") or "") or (
            "未知模式" if not signature else "")
        version = self.store.layout_display_name(map_key, signature)
        parts = [self.store.display_name(map_key), mode, version]
        return " · ".join(p for p in parts if p)

    def _refresh_anchor_icon(self) -> None:
        """把图钉图标标到“当前基准版本”那一行(其它版本行清掉图标)。"""
        for i in range(self.tree.topLevelItemCount()):
            self._refresh_anchor_icon_in(self.tree.topLevelItem(i))

    def _refresh_anchor_icon_in(self, item: QTreeWidgetItem) -> None:
        for i in range(item.childCount()):
            ch = item.child(i)
            if ch.data(0, _ROLE_KIND) == "variant":
                grp = (ch.data(0, _ROLE_ID), ch.data(0, _ROLE_SIG) or "")
                ch.setIcon(_COL_EYE, pin_icon() if grp == self._anchor else QIcon())
            else:
                self._refresh_anchor_icon_in(ch)

    def _update_play_label(self) -> None:
        """信息区“回放”那一栏: 显示基准版本 + 待回放条数。"""
        n = len(self._sel_ids)
        if self._anchor is not None:
            base = self._group_name(*self._anchor)
            self.lbl_play.setText(f"基准 {base} | 待回放 {n} 条" if n
                                  else f"基准 {base} | 未选轨迹")
        else:
            self.lbl_play.setText(f"待回放: {n} 条" if n else "回放: --")

    def _show_all_tracks(self, shown: bool) -> None:
        """一键显示/隐藏轨迹(**只限同一版本** —— 不同版本线路不可比)。

        从树的第一个分组开始, 只点亮该分组内的轨迹(树的顺序是最新在前,
        所以实际是“最新那个版本”); 想换版本就双击其它版本的轨迹改基准。
        """
        rows = list(self._iter_route_items())
        groups: list[tuple[str, str]] = []
        for ch in rows:
            rec = self.store.get(ch.data(0, _ROLE_ID))
            if rec is None:
                continue
            grp = self._group_of(rec)
            if grp not in groups:
                groups.append(grp)
        keep = self._anchor if (self._anchor in groups) else (groups[0] if groups else None)
        if shown:
            keep_ids = {ch.data(0, _ROLE_ID) for ch in rows
                        if keep is not None
                        and (rec := self.store.get(ch.data(0, _ROLE_ID))) is not None
                        and self._group_of(rec) == keep}
            self._visible_ids = keep_ids       # 只显示这一个版本(其它版本一并隐掉)
            self._set_status(f"已显示 {len(keep_ids)} 条轨迹(只限同一版本)", sticky=2.0)
        else:
            self._visible_ids.clear()
            self._set_status("已隐藏全部轨迹", sticky=2.0)
        self._refresh_eye_icons()
        self._refresh_map_tracks()

    def _on_tree_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        kind = item.data(0, _ROLE_KIND)
        menu = QMenu(self)
        if kind == "route":
            act_show = QAction("在地图上显示", self)
            act_show.triggered.connect(lambda: self._show_on_map(item))
            menu.addAction(act_show)
            act_hide = QAction("从地图上隐藏", self)
            act_hide.triggered.connect(lambda: self._hide_from_map(item))
            menu.addAction(act_hide)
            menu.addSeparator()
            act_del = QAction("删除该记录", self)
            act_del.triggered.connect(lambda: self._delete_route(item))
            menu.addAction(act_del)
        else:
            if kind == "variant":     # 版本(交战区域/随机点位)
                act_var = QAction("重命名此版本…", self)
                act_var.triggered.connect(
                    lambda: self._rename_variant(item.data(0, _ROLE_ID),
                                                 item.data(0, _ROLE_SIG)))
                menu.addAction(act_var)
                menu.addSeparator()
                act_show_var = QAction("显示本版本全部轨迹", self)
                act_show_var.triggered.connect(
                    lambda: self._show_tracks_of(item, True))
                menu.addAction(act_show_var)
                act_hide_var = QAction("隐藏本版本全部轨迹", self)
                act_hide_var.triggered.connect(
                    lambda: self._show_tracks_of(item, False))
                menu.addAction(act_hide_var)
                menu.addSeparator()
            else:
                act_rename = QAction("重命名地图", self)
                act_rename.triggered.connect(lambda: self._rename_map(item.data(0, _ROLE_ID)))
                menu.addAction(act_rename)
                menu.addSeparator()
                act_show_all = QAction("显示本图全部轨迹", self)
                act_show_all.triggered.connect(lambda: self._show_all_tracks(True))
                menu.addAction(act_show_all)
                act_hide_all = QAction("隐藏全部轨迹", self)
                act_hide_all.triggered.connect(lambda: self._show_all_tracks(False))
                menu.addAction(act_hide_all)
                menu.addSeparator()
            act_expand = QAction("展开/折叠", self)
            act_expand.triggered.connect(lambda: item.setExpanded(not item.isExpanded()))
            menu.addAction(act_expand)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _variant_tip(self, map_key: str, signature: str, name: str) -> str:
        """版本行的提示: 战区字母/坐标 + 跨度(辨认“大地图/小地图”)."""
        layout = self.store.layout_info(map_key, signature)
        if not layout:
            return (f"{name}\n旧记录(当时还没存战区信息), 无法归属到具体交战区域\n"
                    "右键可重命名")
        zones = layout.get("zones") or []
        where = "、".join(f"{z.get('label')}({float(z.get('x') or 0):.3f},"
                          f"{float(z.get('y') or 0):.3f})" for z in zones)
        spread = layout_spread(zones)
        lines = [
            f"{name} —— {layout.get('mode')} 的一个版本",
            f"战区 {len(zones)} 个: {where}",
            (f"跨度 {spread:.3f}(战区间最大距离, 越大越像“大地图”)" if len(zones) > 1
             else "单战区模式: 每局的战区会在这几个位置里随机"),
            f"指纹 {signature}" if signature else "没有战区信息",
            "右键可重命名(例如改成“大地图/小地图”)",
        ]
        return "\n".join(lines)

    def _show_tracks_of(self, item: QTreeWidgetItem, shown: bool) -> None:
        """把某一层(地图/模式/版本)下面的轨迹全部显示或隐藏。

        显示时同样只保留**同一版本**: 从“版本”行点就是该版本全部轨迹;
        从地图/模式行点则只取其中第一个版本(树木顺序=最新在前)。
        """
        rows = [it.data(0, _ROLE_ID) for it in self._iter_route_items_of(item)]
        if shown:
            # 这一分支里只保留一个版本(与 _show_all_tracks 同一规则)
            groups: list[tuple[str, str]] = []
            keep: tuple[str, str] | None = None
            for rid in rows:
                rec = self.store.get(rid)
                if rec is None:
                    continue
                grp = self._group_of(rec)
                if grp not in groups:
                    groups.append(grp)
            keep = groups[0] if groups else None
            ids = [rid for rid in rows
                   if (rec := self.store.get(rid)) is not None
                   and keep is not None and self._group_of(rec) == keep]
            self._visible_ids = set(ids)
            extra = "(该分支含多个版本, 只显示了最新那个)" if len(groups) > 1 else ""
        else:
            ids = rows
            for rid in ids:
                self._visible_ids.discard(rid)
            extra = ""
        self._refresh_eye_icons()
        self._refresh_map_tracks()
        verb = "已显示" if shown else "已隐藏"
        self._set_status(f"{verb}该分支的 {len(ids)} 条轨迹{extra}", sticky=2.0)

    def _iter_route_items_of(self, item: QTreeWidgetItem):
        """递归取出某层下面的全部轨迹条目。"""
        for i in range(item.childCount()):
            ch = item.child(i)
            if ch.data(0, _ROLE_KIND) == "route":
                yield ch
            else:
                yield from self._iter_route_items_of(ch)

    def _show_on_map(self, item: QTreeWidgetItem) -> None:
        """右键“在地图上显示”: 点亮该条目的眼睛(而不是进入回放)。"""
        self._set_visible(item, True)

    def _hide_from_map(self, item: QTreeWidgetItem) -> None:
        """右键“从地图上隐藏”: 熄灭该条目的眼睛。"""
        self._set_visible(item, False)

    def _set_visible(self, item: QTreeWidgetItem, shown: bool) -> None:
        """统一的“显示/隐藏”入口(眼睛点击与右键菜单都走这里)。"""
        if item.data(0, _ROLE_KIND) != "route":
            return
        route_id = item.data(0, _ROLE_ID)
        if (route_id in self._visible_ids) == shown:
            return
        rec = self.store.get(route_id)
        if shown:
            self._visible_ids.add(route_id)
            # **同一版本**才能一起显示(同一张地图不够): 版本 = 战区位置,
            # 不同交战区域的线路叠在一起毫无意义。点亮别的版本时先把它们隐掉。
            if rec is not None:
                grp = self._group_of(rec)
                others = [rid for rid in self._visible_ids
                          if rid != route_id
                          and (o := self.store.get(rid)) is not None
                          and self._group_of(o) != grp]
                if others:
                    self._visible_ids -= set(others)
                    self._set_status(
                        "同一版本(同一张图 + 同一战区布局)才能一起显示, "
                        "已隐藏其它版本的轨迹", sticky=3.0)
        else:
            self._visible_ids.discard(route_id)
        self._refresh_eye_icons()
        self._refresh_map_tracks()
        if shown:
            self._ensure_map_for(self.map_view.visible_tracks)
        what = f"{rec.vehicle} {fmt_time(rec.duration)}" if rec is not None else "该轨迹"
        self._set_status(
            f"已显示 {what}(当前共 {len(self._visible_ids)} 条)" if shown
            else f"已隐藏 {what}(当前共 {len(self._visible_ids)} 条)", sticky=2.0)
        log.info("轨迹显示切换: %s -> %s (共 %d 条)", route_id, "显示" if shown else "隐藏",
                 len(self._visible_ids))

    def _refresh_eye_icons(self) -> None:
        """按 _visible_ids 重刷所有条目的眼睛图标(不重建树, 避免调用方持有失效条目)。"""
        for ch in self._iter_route_items():
            ch.setIcon(_COL_EYE, eye_icon(ch.data(0, _ROLE_ID) in self._visible_ids))

    def _delete_route(self, item: QTreeWidgetItem) -> None:
        route_id = item.data(0, _ROLE_ID)
        rec = self.store.get(route_id)
        if rec is None:
            return
        ok = QMessageBox.question(
            self, "删除线路记录",
            f"确定删除该记录吗?\n\n{rec.map_name} | {rec.vehicle} | 用时 {fmt_time(rec.duration)}",
        )
        if ok != QMessageBox.StandardButton.Yes:
            return
        self.store.remove(route_id)
        if self._last_record is not None and self._last_record.route_id == route_id:
            self._last_record = None
        self._reload_tree()      # 重建后会自动把“已删除的轨迹”从地图上拿掉
        self._set_status("已删除线路记录", sticky=2.0)

    def _rename_map(self, map_key: str) -> None:
        current = self.store.display_name(map_key)
        name, ok = QInputDialog.getText(self, "重命名地图", "地图名称:", text=current)
        if not ok or not name.strip():
            return
        self.store.rename_map(map_key, name)
        self._reload_tree()

    def _rename_variant(self, map_key: str, signature: str) -> None:
        """给模式版本(交战区域/随机点位)命名, 例如“大地图/小地图”。

        默认名是自动编号(区域 2 / 点位 3), 留空即恢复自动编号。
        """
        current = self.store.layout_display_name(map_key, signature)
        layout = self.store.layout_info(map_key, signature) or {}
        zones = layout.get("zones") or []
        info = "、".join(f"{z.get('label')}({float(z.get('x') or 0):.3f},"
                         f"{float(z.get('y') or 0):.3f})" for z in zones)
        name, ok = QInputDialog.getText(
            self, "重命名战区版本",
            f"{self.store.display_name(map_key)} · {layout.get('mode') or '未知模式'}\n"
            f"战区: {info or '(无)'}\n\n版本名称(留空 = 恢复自动编号「{current}」):",
            text=current)
        if not ok:
            return
        self.store.rename_layout(map_key, signature, name)
        self._reload_tree()
        shown = self.store.layout_display_name(map_key, signature)
        self._set_status(f"已把该版本命名为「{shown}」", sticky=3.0)

    # ==================================================================== 动作
    def _handle_action(self, action: str) -> None:
        """按钮与全局快捷键的统一入口: 先播放提示音, 再投递录制命令。"""
        if self._in_playback():
            # 回放期间录制功能一律不响应(按钮已禁用, 这里堵的是全局快捷键)
            play_error_sound()
            self._set_status("回放中已暂停录制功能, 请先点「■ 结束播放」", sticky=2.0)
            return
        play_sound(action)
        if action == "start":
            # 选项随命令一起投递到轮询线程(避免共享状态的竞态)
            self.recorder.request_start(self.chk_start_on_move.isChecked())
        elif action == "mark":
            self.recorder.request_mark()
        elif action == "stop":
            self.recorder.request_stop()

    def _on_start_on_move_changed(self) -> None:
        """选项只对本次运行有效(不写配置, 保证每次启动都是默认关闭的)。"""
        on = self.chk_start_on_move.isChecked()
        self._set_status(
            "已开启: 开始录制后等待载具移动, 以速度从零变化的前一时刻作为计时起点"
            if on else "已关闭: 按点下开始录制的时刻计时", sticky=4.0)

    def _on_rate_changed(self) -> None:
        """采样间隔下拉框变更: 立即应用到轮询线程并记忆。"""
        ms = self.cmb_rate.currentData()
        if ms is None:
            return
        rec = getattr(self, "recorder", None)
        if rec is None:
            return
        rec.set_interval_ms(float(ms))
        self._settings.setValue("interval_ms", int(ms))
        self._saved_ms = int(ms)
        self._set_status(
            f"采样间隔已设为 {int(ms)} ms(≈{1000 // int(ms)} Hz) —— 轮询与轨迹采样同步",
            sticky=2.0)

    def _set_status(self, text: str, sticky: float = 0.0) -> None:
        """写状态栏。sticky>0 时该文字在这么多秒内不会被刷新的状态覆盖
        (否则提示类信息 80ms 后就没了)。"""
        self.lbl_status.setText(text)
        if sticky > 0:
            self._sticky_text = text
            self._sticky_until = time.monotonic() + float(sticky)

    def _in_playback(self) -> bool:
        """是否处于回放状态(已载入回放集合 → 回放绘制生效, 其它功能锁定)。"""
        # 构造早期(_reload_tree)就会调到, 那时 player 还没建
        player = getattr(self, "player", None)
        return bool(player.records) if player is not None else False

    def _update_buttons(self, recording: bool | None = None) -> None:
        """根据“录制中 / 回放中”刷新所有控件的可用状态。

        回放与录制互斥, 且**回放期间其余功能全部禁用**(眼睛、侧栏、录制按钮、
        采样间隔、快捷键入口), 只有点「■ 结束播放」才恢复 —— 否则回放绘制会盖住
        轨迹显示, 用户会以为“点了眼睛没反应”。
        """
        if recording is None:
            recording = self.recorder.recording
        armed = self.recorder.armed
        in_play = self._in_playback()
        self.btn_start.setEnabled(not recording and not in_play)
        self.btn_stop.setEnabled(recording)
        # 等待移动期间还没定下起点, 标记没有意义
        self.btn_mark.setEnabled(recording and not armed)
        self.chk_start_on_move.setEnabled(not recording and not in_play)
        self.cmb_rate.setEnabled(not recording and not in_play)
        self.tree.setEnabled(not in_play and not recording)   # 含眼睛点击与右键菜单
        # 录制期间屏蔽会改变“显示/底图”的入口, 避免与录制抢主界面
        self.act_manage.setEnabled(not recording)
        self.act_refresh.setEnabled(not recording)
        # 回放控制
        has_track = (bool(self.player.records) or self._last_record is not None
                     or bool(self.store.routes))
        self.btn_play.setEnabled(not recording and has_track)
        self.btn_play_mark.setEnabled(not recording and has_track)
        self.cmb_speed.setEnabled(not recording)
        self.btn_play_stop.setEnabled(in_play)
        self.btn_play_stop.setStyleSheet("font-weight:bold;" if in_play else "")

    # ---------------------------------------------------------------- 回放
    def _playable_tracks(self) -> list[RouteRecord]:
        """要回放的轨迹 = **侧栏当前选中的条目**(选中一条就播一条, 选中多条就播多条,
        一起从 00:00 开始, 以最长的那条作为结束)。

        没有选中任何条目时依次退回: 眼睛点亮的轨迹 → 刚录制的那条。
        """
        if self.player.records:          # 已在回放中 → 保持原来的集合
            return self.player.records
        picked = self._selection_tracks()
        if picked:
            return picked
        shown = self.map_view.visible_tracks
        if not shown and self._last_record is not None:
            shown = [self._last_record]
        return shown

    def _load_playback(self, recs) -> None:
        """载入回放(单条或列表)并复位到开头。

        - 进入回放后其它功能全部禁用(见 _update_buttons), 需点「■ 结束播放」退出;
        - 先把地图切成这批轨迹所属的那张图(游戏没开时用本地缓存)。
        """
        self._ensure_map_for(list(recs) if isinstance(recs, (list, tuple)) else [recs])
        self.player.load(recs)
        self._update_buttons()
        self._set_status(
            f"回放模式({len(self.player.records)} 条): 其余功能已暂停使用, "
            f"点「■ 结束播放」返回", sticky=3.0)

    def _toggle_playback(self) -> None:
        if self.recorder.recording:
            self._set_status("录制中无法回放, 请先结束录制", sticky=2.0)
            return
        if not self.player.records:
            recs = self._playable_tracks()
            if not recs:
                self._set_status("没有可回放的轨迹(先在侧栏选中条目, 或点亮眼睛)",
                                 sticky=2.0)
                return
            self._load_playback(recs)
        self.player.toggle()

    def _play_to_next_mark(self) -> None:
        if self.recorder.recording:
            self._set_status("录制中无法回放, 请先结束录制", sticky=2.0)
            return
        if not self.player.records:
            recs = self._playable_tracks()
            if not recs:
                self._set_status("没有可回放的轨迹(先在侧栏选中条目, 或点亮眼睛)",
                                 sticky=2.0)
                return
            self._load_playback(recs)
        self.player.play_to_next_mark()

    def _end_playback(self) -> None:
        """结束回放: 退出回放绘制、恢复眼睛点亮的轨迹, 并解锁其余功能。"""
        self.player.pause()
        self.player.load(None)            # 清空回放集合
        self.map_view.set_playback(None)
        self.lbl_play.setText("回放: --")
        self._update_buttons()
        self._refresh_map_tracks()        # 把眼睛点亮的轨迹重新画 (不透明)
        self._on_tree_selection_changed()  # 恢复“待回放: N 条”提示
        self._set_status("已结束回放, 恢复显示眼睛点亮的轨迹", sticky=2.0)

    def _on_speed_changed(self) -> None:
        x = self.cmb_speed.currentData()
        if x is None:
            return
        self.player.set_speed(float(x))
        self._settings.setValue("playback_speed", float(x))

    def _update_playback_buttons(self) -> None:
        """兼容旧调用点: 实质上重新计算全部控件状态。"""
        self._update_buttons()

    def _on_playback_progress(self, t: float) -> None:
        recs = self.player.records
        self.map_view.set_playback(recs, t)
        if recs:
            n = f"  ({len(recs)} 条)" if len(recs) > 1 else ""
            self.lbl_play.setText(
                f"回放 {fmt_time_ms(t)} / {fmt_time(self.player.duration)}{n}")

    def _on_playback_state(self, playing: bool) -> None:
        self.btn_play.setText("⏸ 暂停" if playing else "▶ 回放")
        self._update_buttons()

    def _on_playback_finished(self) -> None:
        self._set_status(
            f"回放结束({len(self.player.records)} 条轨迹已播完) —— "
            f"点「■ 结束播放」返回查看/切换轨迹", sticky=4.0)

    def _on_playback_mark(self, mk) -> None:
        self._set_status(
            f"已到标记 {fmt_time_ms(mk.t)} 并暂停 —— 点「⏭」继续到下一个标记", sticky=3.0)

    # ==================================================================== 信号
    def _on_tick(self, info: dict) -> None:
        # 地图重绘跟随刷新率(轨迹平滑); 文本按 ~12Hz 节流, 避免高频刷新浪费 CPU
        now = time.monotonic()
        if now - self._last_text_at >= 0.08:
            self._last_text_at = now
            self.lbl_vehicle.setText(f"载具: {info.get('vehicle') or '未知载具'}")
            speed = float(info.get("speed") or 0.0)
            self.lbl_speed.setText(f"速度: {fmt_speed(speed)} km/h")
            duration = (float(info.get("elapsed") or 0.0) if info.get("recording")
                        else self._last_duration)
            self.lbl_timer.setText(f"计时: {fmt_time(duration)}")
            self.lbl_marks.setText(f"标记: {int(info.get('marks') or 0)} 个")

            rate = float(info.get("rate") or 0.0)
            if rate:
                self.lbl_rate.setText(f"实际 {rate:.0f} Hz")

            if now < self._sticky_until:
                # 刚发生的事(回放开始/结束、显示切换、提示)优先显示, 不被刷掉
                self.lbl_status.setText(self._sticky_text)
            elif not info.get("connected"):
                self.lbl_status.setText("未连接到 8111 (请启动游戏)")
            elif self._in_playback():
                self.lbl_status.setText(
                    "回放模式: 其余功能已暂停使用, 点「■ 结束播放」返回")
            elif not info.get("recording"):
                if info.get("player") is None:
                    self.lbl_status.setText("已连接 8111 | 等待进入对局/试车场(未获取到玩家位置)")
                else:
                    mode = str(info.get("mode") or "")
                    own = ownership_text(info.get("zones") or [])
                    tail = " | ".join(x for x in (mode, own) if x)
                    self.lbl_status.setText(
                        f"已连接 8111 | {tail} | 可开始录制" if tail
                        else "已连接 8111 | 可开始录制")

            # 没有底图时(游戏没开/战局外)用本地缓存补上, 这样关掉游戏也能看回放
            if self.map_view.map_key is None and now - self._last_map_try >= 1.0:
                self._last_map_try = now
                self._restore_map_for_current_view()

        self.map_view.set_player(info.get("player"))
        self._apply_zones(info)
        self.map_view.update()

    def _on_map_changed(self, data: bytes) -> None:
        changed = self.map_view.set_map_bytes(data)
        if not changed:
            return
        self._apply_zones()          # 换了地图 → 换用该地图的战区布局
        if self.recorder.map_key == PLACEHOLDER_MAP_KEY:
            # 战局外 8111 只有占位图: **不要**当换图清理, 而是尽量切回“正在看的轨迹”的缓存底图
            self._restore_map_for_current_view()
            return
        self._drop_tracks_of_other_map()

    def _on_map_cached(self, key: str, created: bool) -> None:
        """地图图片已写入本地缓存(新图才提示)。"""
        if not created:
            return
        self._set_status(
            f"已缓存地图图片「{self.store.display_name(key)}」"
            f"(共 {len(self.map_cache.entries())} 张, 可在菜单「地图」→「地图管理」里命名)",
            sticky=4.0)

    # ------------------------------------------------------- 地图图片(缓存/切换)
    def _current_view_key(self) -> str | None:
        """当前在看的轨迹属于哪张地图(显示的 → 选中的 → 刚录制的)。"""
        for rec in self.map_view.visible_tracks:
            return rec.map_key
        for rec in self._selection_tracks():
            return rec.map_key
        if self._last_record is not None:
            return self._last_record.map_key
        return None

    def _show_cached_map(self, key: str) -> bool:
        """把地图切成指定地图的**本地缓存图片**(没有缓存返回 False)。"""
        if not key or key == "unknown":
            return False
        if self.map_view.map_key == key:
            return True
        data = self.map_cache.load(key)
        if data is None:
            return False
        return bool(self.map_view.set_map_bytes(data))

    def _ensure_map_for(self, recs: list[RouteRecord]) -> None:
        """让地图改成这批轨迹所属的那张图(游戏没开时就靠本地缓存)。"""
        keys = {r.map_key for r in recs if r.map_key and r.map_key != "unknown"}
        if len(keys) != 1:
            return
        key = keys.pop()
        if self.map_view.map_key == key:
            return
        if not self._show_cached_map(key):
            self._set_status(
                f"地图「{self.store.display_name(key)}」还没有缓存底图; "
                f"在游戏里进一次该地图就会自动缓存", sticky=4.0)

    def _restore_map_for_current_view(self) -> None:
        """没有底图时(游戏没开/战局外)用缓存恢复到“正在看的这张地图”。"""
        key = self._current_view_key()
        if key is None:
            entries = self.map_cache.entries()
            key = entries[0].key if entries else None    # 兜底: 最近缓存的那张
        if not key or self.map_view.map_key == key:
            return
        if self._show_cached_map(key):
            self._apply_zones()          # 切了地图 → 换用该地图的战区布局
            self._set_status(f"已用缓存底图显示「{self.store.display_name(key)}」", sticky=2.5)
        elif self.map_view.map_key is None:
            self._set_status(
                f"「{self.store.display_name(key)}」还没有缓存底图"
                f"(在游戏里进一次该地图即可缓存)", sticky=4.0)

    def _drop_tracks_of_other_map(self) -> None:
        """地图变了 → 属于其它地图的轨迹/回放全部清除(否则会画在新地图上, 产生误导)。

        以地图图片指纹(rec.map_key)判定: 同一张地图的轨迹保留,
        换图/退出战局(占位图)后旧轨迹被清掉。
        正在录制时保留实时轨迹(清掉会让轨迹直接消失), 只清回放/历史。
        """
        key = self.recorder.map_key
        recording = self.recorder.recording
        dropped = self.map_view.drop_tracks_for_other_map(key, keep_live=recording)
        if self._last_record is not None and self._last_record.map_key != key:
            self._last_record = None
            dropped.append("上次录制")
        recs = self.player.records
        if recs and any(r.map_key != key for r in recs):
            self.player.pause()
            self.player.load(None)
            self.lbl_play.setText("回放: --")
        if dropped:
            # 眼睛点亮的轨迹也要跟着换图规则走: 不属于当前地图的一律隐藏
            stale = {rid for rid in self._visible_ids
                     if (r := self.store.get(rid)) is None or r.map_key != key}
            if stale:
                self._visible_ids -= stale
                self._reload_tree()      # 重建侧栏(顺便刷新眼睛图标与地图)
        if dropped or recording:
            self._update_buttons()
        if recording:
            log.info("录制过程中地图图片发生变化 (key=%s)", key)
            self._set_status(
                "录制中地图已切换: 回放/历史轨迹已清除, 本次录制仍归属开始时的地图",
                sticky=4.0)
        elif dropped:
            log.info("切换地图(%s): 已清除 %s", key, "、".join(dict.fromkeys(dropped)))
            self._set_status(
                "地图已切换, 已清除不属于当前地图的轨迹: "
                + "、".join(dict.fromkeys(dropped)), sticky=4.0)

    def _on_recording_started(self, rec: RouteRecord) -> None:
        self._enter_recording_ui(rec, armed=False)

    def _on_start_armed(self, rec: RouteRecord) -> None:
        """已按下开始, 但在等载具移动(此时还没有计时起点)。"""
        self._enter_recording_ui(rec, armed=True)

    def _enter_recording_ui(self, rec: RouteRecord, armed: bool) -> None:
        self.player.pause()          # 录制与回放互斥
        self.player.load(None)
        self.map_view.set_playback(None)
        self.map_view.set_live(rec)
        # 录制时先**把主界面切回游戏当前地图**: 用户可能正在预览另一张地图/另一个
        # 版本的轨迹, 不切回去就会“录着这张图、看着那张图”, 两边打架。
        # 同时清掉之前预览的点亮状态(录制期间只显示正在录的这条)。
        previewed = len(self._visible_ids)
        self._visible_ids.clear()
        self._refresh_eye_icons()
        key = self.recorder.map_key
        switched = False
        if key and key != PLACEHOLDER_MAP_KEY and self.map_view.map_key != key:
            switched = self._show_cached_map(key)
        self._refresh_map_tracks()
        self._update_buttons(True)
        note = ""
        if previewed or switched:
            note = "\n已切回游戏当前地图并暂停预览(录制期间其余功能已锁定)"
        if armed:
            self.lbl_status.setText(
                f"准备录制… 地图: {rec.map_name} | 载具: {rec.vehicle} "
                f"| 等待载具移动(速度从 0 变为非 0 时开始计时){note}"
            )
        else:
            self.lbl_status.setText(
                f"录制中… 地图: {rec.map_name} | 载具: {rec.vehicle} "
                f"| 起点已标注 (按 {describe_binding(DEFAULT_BINDINGS['stop'])} 结束){note}"
            )

    def _on_recording_cancelled(self) -> None:
        """等待移动期间被结束 → 本次录制被丢弃(全程速度为 0)。"""
        self.map_view.set_live(None)
        self._update_buttons(False)
        self._set_status("已取消录制: 未检测到载具移动(速度全程为 0)", sticky=3.0)

    def _on_mark_added(self, _mk) -> None:
        self.map_view.update()

    def _on_recording_finished(self, rec: RouteRecord) -> None:
        self._last_duration = rec.duration
        self._last_record = rec
        # 刚录完的这条仍然显示在地图上 → 眼睛必须同步点亮(显示状态统一由 _visible_ids 决定)
        self._visible_ids.add(rec.route_id)
        self._update_buttons(False)
        self._reload_tree()          # 内部会 _refresh_map_tracks(): 该条用速度配色高亮
        self._set_status(
            f"录制完成: {rec.map_name} | {rec.vehicle} | 用时 {fmt_time(rec.duration)} "
            f"| 轨迹点 {len(rec.points)} | 标记 {len(rec.marks) - 1} 个", sticky=6.0)

    def _on_notice(self, text: str) -> None:
        self._set_status(text, sticky=3.0)

    # ==================================================================== 收尾
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名
        try:
            self.hotkeys.stop()
            self.player.pause()
            self.recorder.stop()
            self.store.save()
        except Exception as exc:  # noqa: BLE001 - 退出时尽力清理
            log.warning("退出清理异常: %s", exc)
        super().closeEvent(event)
