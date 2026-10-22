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
from PyQt6.QtGui import QAction, QBrush, QCloseEvent, QColor, QFont, QIcon, QPalette
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame,
    QGridLayout, QHeaderView, QHBoxLayout, QInputDialog, QLabel, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSizePolicy, QSplitter, QTextBrowser, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from .global_hotkey import (
    ACTION_LABELS, DEFAULT_BINDINGS, GlobalHotkey, GlobalHotkeyWatcher, describe_binding,
    load_bindings, reset_bindings, save_bindings,
)
from .help_text import about_html
from .hotkey_dialog import HotkeyDialog
from .icons import ICON_SIZE, circle_icon, eye_icon, pin_icon
from .map_cache import MapCache
from .map_manager import MapManagerDialog
from .route_map_view import RouteMapView
from .route_playback import PLAYBACK_SPEEDS, RoutePlayer
from .route_recorder import DEFAULT_INTERVAL_MS, RouteRecorder
from .route_store import (
    GLOBAL_SIDES, GLOBAL_SIDE_LABELS, PLACEHOLDER_MAP_KEY, GlobalRecord, RouteRecord,
    RouteStore, fmt_speed, fmt_time, fmt_time_ms,
)
from .sound import play as play_sound
from .sound import play_error as play_error_sound
from .sound import settings as sound_settings
from .sound_dialog import SoundSettingsDialog
from .tree_view import RouteTreeWidget
from .zones import ground_spawns, layout_spread, ownership_text, spawns_from_json
from .zones import spawns_to_json, zones_to_json

log = logging.getLogger("route_window")


def _create_map_view(parent) -> RouteMapView:
    """建地图控件: 优先 GPU(pygfx), 不行就软件渲染。

    这里**必须惰性导入** `route_map_gl`: 它一 import 就会加载 `wgpu-native` 的 DLL,
    而无头/offscreen 平台下这会和 Qt 冲突(实测整个进程 0xC000041D 崩掉, 连测试都跑不了),
    而且普通用户也省掉几百毫秒启动时间。`create_map_view` 内部还会判平台与异常回退。
    """
    try:
        from .route_map_gl import create_map_view
    except Exception:  # noqa: BLE001
        log.exception("GPU 画布模块导入失败, 使用软件渲染")
        return RouteMapView(parent)
    return create_map_view(parent)


# 采样间隔选项(毫秒):
# 40ms(≈25Hz)与 8111 陆战数据更新率匹配; 20ms(≈50Hz)接近 8111 请求上限
INTERVAL_CHOICES_MS = (20, 25, 30, 40, 50, 75, 100)

_ROLE_KIND = Qt.ItemDataRole.UserRole
_ROLE_ID = Qt.ItemDataRole.UserRole + 1
_ROLE_SIG = Qt.ItemDataRole.UserRole + 2      # 版本(战区布局)的坐标指纹
# 分组行的稳定标识(地图/模式/版本): 侧栏重建后用它对上“原来折起来的是哪几个分组”
_ROLE_KEY = Qt.ItemDataRole.UserRole + 3

# 层级行(地图/模式/版本)只当“分组标题”: 可点击展开、可右键, 但**不可被选中** ——
# 否则多选时会把标题行也选进去(回放/基准只应以轨迹条目为单位)。
GROUP_ITEM_FLAGS = Qt.ItemFlag.ItemIsEnabled

# 侧栏列: 0=展开/收起(眼睛图标) 1=地图/模式/版本/线路 2=信息(条数或用时)
_COL_EYE = 0
_COL_NAME = 1
_COL_TIME = 2

# 眼睛列宽度(px): 最深的一条是全局录制里的线路(地图 → 模式 → 版本 → 全局 → 队伍 → 线路,
# 缩进 12×5), 图标 18px, 再加一点间距 —— 取小了图标会被列边界裁掉/压到名称列的文字上
EYE_COLUMN_WIDTH = 6 * 12 + 18 + 8


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
        # 同一张地图可能被 8111 以不同分辨率发过来(1024²/2048²/4096²) —— 老版本会按
        # 字节哈希把它们当成两张地图。启动时把这类重复条目合并掉(见 _merge_duplicate_maps)
        self._merged_maps = self._merge_duplicate_maps()
        self._last_duration = 0.0
        self._last_text_at = 0.0
        self._last_map_try = 0.0      # 上次尝试用缓存底图的时间(节流)
        self._last_record: RouteRecord | None = None
        # 眼睛点亮的轨迹 id: 它们会同时显示在地图上(取代了以前 Shift 多选的做法)
        self._visible_ids: set[str] = set()
        # 全局录制里“眼睛点亮”的线路 id(`<条目 id>:<队><序号>`), 与 _visible_ids 分开存:
        # 两类记录不是一回事(前者能回放, 后者只是折线), 混在一起容易误判。
        self._visible_lives: set[str] = set()
        # 最近一次完成的全局录制条目(用于状态提示/自动展开)
        self._last_global: GlobalRecord | None = None
        # 选中集合(决定回放播哪几条; 用于判断“本次新加进来的是哪张地图”)
        self._sel_ids: set[str] = set()
        self._sel_groups: set[tuple[str, str]] = set()
        # “基准版本”: 双击某条轨迹后, 它的(地图键, 战区指纹)被锁为基准 ——
        # 之后多选/回放只能在同一版本内进行(见 _group_of / _on_tree_selection_changed)
        self._anchor: tuple[str, str] | None = None
        self._reselecting = False
        # 侧栏是否已经建过一次: 首次构建(= 刚启动)默认**全部折叠**, 之后的重建
        # 才按用户手动展开/折叠过的状态恢复(见 _restore_folded)
        self._tree_built = False
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
        # 回放显示选项: “被击毁后路线变暗” / “被击毁标记” —— **默认都开**, 并记住设置
        try:
            self._saved_dead_dim = int(self._settings.value("dead_dim", 1)) != 0
        except (TypeError, ValueError):
            self._saved_dead_dim = True
        try:
            self._saved_dead_mark = int(self._settings.value("dead_mark", 1)) != 0
        except (TypeError, ValueError):
            self._saved_dead_mark = True
        # 「显示战区」(小菱形 + A/B/C 字母)开关: 菜单「设置」里勾选, 记住设置
        try:
            self._show_zones = int(self._settings.value("show_zones", 1)) != 0
        except (TypeError, ValueError):
            self._show_zones = True
        # 「显示重生位置」(红/蓝空心方块)开关: 同上
        try:
            self._show_spawns = int(self._settings.value("show_spawns", 1)) != 0
        except (TypeError, ValueError):
            self._show_spawns = True

        # 快捷键设置必须先读(菜单里的「同时使用按键轮询」勾选状态要用), 监控对象在后面再建
        self._hotkey_bindings = load_bindings(self._settings)
        self._last_hotkey_at: dict[str, float] = {}
        try:
            self._poll_enabled = int(self._settings.value("hotkey_poll", 1)) != 0
        except (TypeError, ValueError):
            self._poll_enabled = True

        self._build_ui()
        self._build_menu()
        self.map_view.set_show_zones(self._show_zones)
        self.map_view.set_show_spawns(self._show_spawns)
        self.map_view.set_dead_style(dim=self._saved_dead_dim, mark=self._saved_dead_mark)
        self._reload_tree()
        if self._merged_maps:
            names = []
            for _drop, keep in self._merged_maps:
                name = self.store.display_name(keep)
                if name not in names:
                    names.append(name)
            self._set_status(
                f"已把同一张地图的重复条目(不同分辨率)合并为: {'、'.join(names)}"
                f"(原数据已备份为 routes.json.bak)", sticky=10.0)
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
        self.recorder.global_armed.connect(self._on_global_armed)
        self.recorder.global_started.connect(self._on_global_started)
        self.recorder.global_finished.connect(self._on_global_finished)
        self.recorder.global_cancelled.connect(self._on_global_cancelled)
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

        # 全局快捷键: **两套通道**同时开着 —— 系统热键(RegisterHotKey)+ 轮询
        # (GetAsyncKeyState); 任意一套命中就触发, 同一动作 0.3s 内去重。
        # 为什么两套: 用户反馈游戏里右Shift+小键盘按了没反应(疑似键盘多键冲突/被挡),
        # 系统热键不靠定时器也不读按键状态, 最可靠; 轮询则能区分左右 Shift 等精确组合。
        self.hotkeys = GlobalHotkeyWatcher(self._hotkey_bindings, parent=self)
        self.hotkeys.triggered.connect(self._hotkey_action)
        if self._poll_enabled:
            self.hotkeys.start()
        self.sys_hotkeys = GlobalHotkey(self._hotkey_bindings, parent=self)
        self.sys_hotkeys.triggered.connect(self._hotkey_action)
        self.sys_hotkeys.start()

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

        self.act_spawns = QAction("显示重生位置(红/蓝方块)", self)
        self.act_spawns.setCheckable(True)
        self.act_spawns.setChecked(self._show_spawns)
        self.act_spawns.setToolTip(
            "对战局内 8111 给出的重生点(respawn_base_tank)画空心方块:\n"
            "红 = 敌方, 蓝 = 我方。一个位置一个小方块 —— 连成一片的就是“重生区域”\n"
            "(游戏里重生位置就在这个区域里随机分配)。\n"
            "战局外/看缓存底图时, 画的是这张地图这个版本以前记下的重生位置。")
        self.act_spawns.toggled.connect(self._on_show_spawns)
        setting.addAction(self.act_spawns)

        hk_menu = self.menuBar().addMenu("快捷键(&K)")
        act_hk_set = QAction("快捷键设置(按键 / 修饰键)…", self)
        act_hk_set.setToolTip("给“开始录制 / 结束录制 / 标记位置”分别指定按键, 改动立即生效。")
        act_hk_set.triggered.connect(self._open_hotkey_settings)
        hk_menu.addAction(act_hk_set)
        self.act_poll = QAction("同时使用按键轮询(可区分左右 Shift)", self)
        self.act_poll.setCheckable(True)
        self.act_poll.setChecked(self._poll_enabled)
        self.act_poll.setToolTip(
            "主通道是**系统热键**(RegisterHotKey): 由 Windows 直接把按键事件通知本程序,\n"
            "本程序既不读按键状态也不挂钩子。\n"
            "这一项是额外的兜底通道(GetAsyncKeyState 轮询), 好处是能区分“右Shift”这种精确组合,\n"
            "而且能兼容某些系统热键注册失败的键。\n"
            "如果你不想让程序去查询按键状态, 可以关掉它(只留系统热键)。\n"
            "两条通道都只是“接收按键通知”: 不注入按键、不挂钩子、不碰游戏进程。")
        self.act_poll.toggled.connect(self._on_poll_toggled)
        hk_menu.addAction(self.act_poll)
        hk_menu.addSeparator()
        act_hk_reset = QAction("恢复默认快捷键(F5 / F6 / F7)", self)
        act_hk_reset.triggered.connect(self._reset_hotkeys)
        hk_menu.addAction(act_hk_reset)

        snd_menu = self.menuBar().addMenu("音效(&A)")
        self.act_sound_on = QAction("启用提示音", self)
        self.act_sound_on.setCheckable(True)
        self.act_sound_on.setChecked(sound_settings().enabled)
        self.act_sound_on.toggled.connect(self._on_sound_toggled)
        snd_menu.addAction(self.act_sound_on)
        act_sound_set = QAction("音效设置(音量 / 自定义音效文件)…", self)
        act_sound_set.triggered.connect(self._open_sound_settings)
        snd_menu.addAction(act_sound_set)

        help_menu = self.menuBar().addMenu("帮助(&H)")
        act_about = QAction("关于 / 功能说明(快捷键、界面用法)…", self)
        act_about.triggered.connect(self._show_about)
        help_menu.addAction(act_about)

    def _show_about(self) -> None:
        """菜单「帮助 → 关于」: 全部说明都收在这里。

        界面上不再放常驻提示文字(侧栏底部、信息区那几行已删除) ——
        那些文字只在第一次用的时候有用, 之后纯粹是占地方。
        """
        dlg = QDialog(self)
        dlg.setWindowTitle("关于 / 功能说明 —— 跑图线路测试器")
        dlg.resize(780, 660)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(10, 10, 10, 10)
        view = QTextBrowser(dlg)
        view.setOpenExternalLinks(True)
        view.setHtml(about_html(self._hotkey_bindings))
        layout.addWidget(view, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dlg)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        layout.addWidget(buttons)
        dlg.exec()

    def _on_show_zones(self, checked: bool) -> None:
        """菜单开关: 是否在地图上画战区。"""
        self._show_zones = bool(checked)
        self._settings.setValue("show_zones", 1 if checked else 0)
        self.map_view.set_show_zones(self._show_zones)
        self._set_status("已显示地图上的战区位置" if checked else "已隐藏地图上的战区位置",
                         sticky=2.0)

    def _on_sound_toggled(self, on: bool) -> None:
        """菜单开关: 提示音总开关(立即落盘)。"""
        sound_settings().set_enabled(bool(on))
        self._set_status("已启用提示音" if on else "已关闭提示音(所有音效都不再播放)",
                         sticky=2.0)
        if on:
            play_sound("mark")          # 打开时响一声, 便于确认音量

    def _on_show_spawns(self, checked: bool) -> None:
        """菜单开关: 是否在地图上画重生位置(红/蓝空心方块)。"""
        self._show_spawns = bool(checked)
        self._settings.setValue("show_spawns", 1 if checked else 0)
        self.map_view.set_show_spawns(self._show_spawns)
        self._set_status("已显示地图上的重生位置(红=敌方, 蓝=我方)" if checked
                         else "已隐藏地图上的重生位置", sticky=2.0)

    def _open_sound_settings(self) -> None:
        """菜单「音效 → 音效设置」: 音量 / 自定义音效文件(改动立即生效)。"""
        dlg = SoundSettingsDialog(sound_settings(), self)
        dlg.exec()
        self.act_sound_on.setChecked(sound_settings().enabled)   # 对话框里可能改过开关
        self._set_status("音效设置已更新", sticky=2.0)

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

        self.map_view = _create_map_view(right)
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

        self.tree = RouteTreeWidget(box)
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["展开/收起", "地图 / 线路", "信息"])
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
        # 选中行不够显眼 → 把选中底色调**深**(系统默认的浅蓝高亮在两种主题下都偏淡)。
        # 用 palette 而不是 QSS: QSS 的 `::item` 会接管单元格绘制, 容易连带改掉
        # 交替行/图标/文字顏色; 只换 Highlight 两个颜色, 其余绘制保持系统原生。
        pal = self.tree.palette()
        # Active / Inactive 两组都要设: 窗口失焦时 Qt 用的是 Inactive.Highlight,
        # 只设 Active 的话切出去再回来会看到浅灰高亮(实测取样仍是 #f3f3f3)。
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            pal.setColor(group, QPalette.ColorRole.Highlight, QColor("#1d4ed8"))
            pal.setColor(group, QPalette.ColorRole.HighlightedText, QColor("#f8fafc"))
        self.tree.setPalette(pal)
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

        # ---- 回放显示选项(两个都默认勾选)
        # “被击毁”判定只看 8111: 线路对应的 icon 突然消失、且当时场上还有别的单位
        # → 算阵亡; 全场一起消失(回到车库/录制结束) 不算(有人整局不死)。
        self.chk_dead_dim = QCheckBox("被击毁后路线变暗")
        self.chk_dead_dim.setChecked(self._saved_dead_dim)
        self.chk_dead_dim.setToolTip(
            "回放时, 线路对应的玩家被击毁之后, 这条线路整条变成半透明纯灰\n"
            "(不再按速度着色), 一眼能看出哪些人已经阵亡。")
        self.chk_dead_dim.stateChanged.connect(self._on_dead_style_changed)
        play_box.addWidget(self.chk_dead_dim)

        self.chk_dead_mark = QCheckBox("被击毁标记")
        self.chk_dead_mark.setChecked(self._saved_dead_mark)
        self.chk_dead_mark.setToolTip(
            "在被击毁的位置(线路末端)画一个红色 X, 大小与回放光标白点相当。\n"
            "回放越过阵亡时刻后才出现。")
        self.chk_dead_mark.stateChanged.connect(self._on_dead_style_changed)
        play_box.addWidget(self.chk_dead_mark)

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
        # 图标 = 地图上对应的那个圈(绿色起点圈 / 红色终点圈 / 黄色标记圈),
        # 颜色与相对大小都跟地图一致, 一眼能对上(见 icons.circle_icon)
        for btn, kind in ((self.btn_start, "start"), (self.btn_stop, "stop"),
                          (self.btn_mark, "mark")):
            btn.setIcon(circle_icon(kind))
            btn.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
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
        btns.addStretch(1)

        # ---- 竖线分隔: 左边是“单条线路录制”, 右边是“全局录制”(整局所有玩家的线路)
        sep = QFrame()
        sep.setFixedWidth(1)
        sep.setStyleSheet("background-color:#94a3b8; border:none;")
        sep.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        btns.addSpacing(10)
        btns.addWidget(sep)
        btns.addSpacing(10)

        self.btn_rec_a = QPushButton("A 队录制")
        self.btn_rec_b = QPushButton("B 队录制")
        self.btn_rec_stop = QPushButton("停止 A/B 队录制")
        self.btn_rec_a.setToolTip(
            "全局录制(整局): 把**所有玩家**的线路录到这个条目的 A 队里。\n"
            "点之前请先回到**刚开局的等待部署**阶段 —— 此时 8111 还没有玩家数据,\n"
            "软件会挂着等第一名玩家重生, 那一刻才算 0 秒。\n"
            "⚠ 时间按**真实时钟**计: A/B 两次录制请都从同一个位置开始、保持 1× 播放、\n"
            "不要暂停, 否则两队的时间轴会对不上(回放时会不同步)。\n"
            "回到车库(demo 播完)或点「停止 A/B 队录制」时结束。\n"
            "规则: 侧栏里**有 A 队标记**的全局条目 → 录到它; 没有任何标记 → 新建一条。")
        self.btn_rec_b.setToolTip("与「A 队录制」相同, 录到 B 队(回放里换成观战另一方再录一次)。")
        self.btn_rec_stop.setToolTip("结束当前全局录制并存盘(没有录到线路则丢弃本条)。")
        self.btn_rec_a.clicked.connect(lambda: self._start_global("A"))
        self.btn_rec_b.clicked.connect(lambda: self._start_global("B"))
        self.btn_rec_stop.clicked.connect(self._stop_global)
        for b in (self.btn_rec_a, self.btn_rec_b, self.btn_rec_stop):
            b.setMinimumHeight(34)
            btns.addWidget(b)
        self.btn_rec_a.setStyleSheet("font-weight:bold;")
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

    def _iter_life_items(self):
        """递归遍历全部**全局录制里的线路条目**(全局条目 → A/B 队 → 线路)。"""
        def walk(item):
            for i in range(item.childCount()):
                ch = item.child(i)
                if ch.data(0, _ROLE_KIND) == "glife":
                    yield ch
                else:
                    yield from walk(ch)

        root = self.tree.invisibleRootItem()
        yield from walk(root)

    def _life_ids_of(self, item: QTreeWidgetItem) -> list[str]:
        """某个全局条目 / 队伍行下面的全部线路 id。"""
        if item.data(0, _ROLE_KIND) == "glife":
            return [item.data(0, _ROLE_ID)]
        out: list[str] = []

        def walk(node: QTreeWidgetItem) -> None:
            for i in range(node.childCount()):
                ch = node.child(i)
                if ch.data(0, _ROLE_KIND) == "glife":
                    out.append(ch.data(0, _ROLE_ID))
                else:
                    walk(ch)

        walk(item)
        return out

    def _life_map_keys(self) -> dict[str, str]:
        """{线路 id: 所属地图键} —— 换图时用它判断哪些全局线路已经不属于当前地图。"""
        return {life.life_id: g.map_key
                for g in self.store.globals for side in GLOBAL_SIDES
                for life in g.lives(side)}

    def _tree_variants(self):
        """把「单条路线」与「全局录制条目」合并成统一三层结构(地图 → 模式 → 版本)。

        返回 `[(地图键, [(模式名, [(战区指纹, 版本名, [路线...], [全局条目...])])])]`。
        全局条目按 `(地图键, 战区指纹)` 归到对应版本下; 如果某个版本**只有**全局条目
        (还没录过单条路线), 也会单独补一组出来, 否则它就没地方可放了。
        """
        routes = self.store.grouped_by_variant()
        glob = self.store.globals_by_group()
        out: list[tuple[str, list[tuple[str, list[tuple[str, str, list, list]]]]]] = []
        for key in self.store.ordered_map_keys():
            modes: list[tuple[str, list[tuple[str, str, list, list]]]] = []
            used: set[str] = set()
            for mode_label, variants in routes.get(key, []):
                rows: list[tuple[str, str, list, list]] = []
                for sig, vname, recs in variants:
                    rows.append((sig, vname, list(recs), glob.get((key, sig), [])))
                    used.add(sig)
                modes.append((mode_label, rows))
            for (mk, sig), items in glob.items():
                if mk != key or sig in used:
                    continue
                vname = self.store.layout_display_name(key, sig)
                mode_label = items[0].mode_label if items else ""
                modes.append((mode_label, [(sig, vname, [], list(items))]))
            if modes:
                out.append((key, modes))
        return out

    @staticmethod
    def _count_text(n_route: int, n_global: int) -> str:
        """分组行的“信息”列: 单条路线条数 + 全局录制局数(没有全局录制就不写)。"""
        return f"{n_route} 条 · {n_global} 局" if n_global else f"{n_route} 条"

    def _add_global_item(self, parent: QTreeWidgetItem, g: GlobalRecord) -> None:
        """在版本行下挂一个「全局录制」条目: 条目 → A/B 队 → 每辆车的线路。"""
        mark = f"  [{g.mark_label}]" if g.mark_label else ""
        item = QTreeWidgetItem(["", f"全局 {g.label}{mark}", f"{g.count()} 条"])
        item.setData(0, _ROLE_KIND, "global")
        item.setData(0, _ROLE_ID, g.route_id)
        item.setData(0, _ROLE_KEY, f"global|{g.route_id}")
        item.setIcon(_COL_EYE, eye_icon(any(life.life_id in self._visible_lives
                                            for side in GLOBAL_SIDES
                                            for life in g.lives(side))))
        item.setToolTip(_COL_EYE, "点击显示 / 隐藏整局的全部线路")
        item.setToolTip(_COL_NAME,
                        f"全局录制 · {g.map_name} · {g.mode_label}\n"
                        f"录制时间 {g.created_at.replace('T', ' ')}\n"
                        f"用时 {fmt_time(g.duration)} · "
                        f"A 队 {len(g.lives('A'))} 条 / B 队 {len(g.lives('B'))} 条\n"
                        f"标记: {g.mark_label or '(无 — 点「A/B 队录制」会新建一个条目)'}\n"
                        f"右键可标记为 A/B 队录制目标、清除标记或删除")
        parent.addChild(item)
        for side in GLOBAL_SIDES:
            lives = g.lives(side)
            sitem = QTreeWidgetItem(["", GLOBAL_SIDE_LABELS[side], f"{len(lives)} 条"])
            sitem.setData(0, _ROLE_KIND, "gside")
            sitem.setData(0, _ROLE_ID, g.route_id)
            sitem.setData(0, _ROLE_SIG, side)
            sitem.setData(0, _ROLE_KEY, f"gside|{g.route_id}|{side}")
            sitem.setFlags(GROUP_ITEM_FLAGS)
            sitem.setIcon(_COL_EYE, eye_icon(any(l.life_id in self._visible_lives
                                                for l in lives)))
            sitem.setToolTip(_COL_EYE, "点击显示 / 隐藏这一队的全部线路")
            sitem.setToolTip(_COL_NAME,
                             "回放的 /map_obj.json 只给**观战那一方**的单位,\n"
                             "所以要分两次录制: 一次观战 A 方, 一次观战 B 方。")
            item.addChild(sitem)
            # 组内按开始时间**正序**(越靠前时间越早) —— 与单条路线(倒序)刚好相反
            for life in sorted(lives, key=lambda x: (x.t0, x.life_id)):
                litem = QTreeWidgetItem(["", life.label, fmt_time(life.duration)])
                litem.setData(0, _ROLE_KIND, "glife")
                litem.setData(0, _ROLE_ID, life.life_id)
                litem.setData(0, _ROLE_SIG, side)
                litem.setData(0, _ROLE_KEY, g.route_id)
                litem.setIcon(_COL_EYE, eye_icon(life.life_id in self._visible_lives))
                litem.setToolTip(_COL_EYE, "点击显示 / 隐藏这条线路")
                litem.setToolTip(_COL_NAME,
                                 f"{side} 队 · {life.icon or life.obj_type}\n"
                                 f"出现 {fmt_time(life.t0)} → 消失 {fmt_time(life.t1)}\n"
                                 f"点数 {len(life.points)} · 最高速 "
                                 f"{fmt_speed(max((p.speed for p in life.points), default=0.0))} km/h\n"
                                 f"色标 {life.color or '-'}(8111 原始颜色, 仅参考)")
                sitem.addChild(litem)

    def _reload_tree(self) -> None:
        # 重建前先记住哪些分组是折叠的: 「地图管理」关掉、录完一条轨迹、改名或删除
        # 之后都会走这里重建侧栏, 否则用户刚折起来的分组会被全部重新展开。
        folded = self._folded_keys()
        first_build = not getattr(self, "_tree_built", False)   # 刚启动的那一次
        self.tree.blockSignals(True)
        self.tree.clear()
        alive: set[str] = set()          # 仍存在于侧栏的轨迹 id(用于修剪显示集合)
        alive_lives: set[str] = set()    # 仍存在于侧栏的全局线路 id
        # 四层: 地图 → 模式(由战区数量推出) → 版本(战区布局) → 轨迹/全局条目
        variants_all = self._tree_variants()
        for map_key, modes in variants_all:
            n_route = sum(len(rows) for _m, vs in modes for _s, _n, rows, _g in vs)
            n_glob = sum(len(g) for _m, vs in modes for _s, _n, _r, g in vs)
            top = QTreeWidgetItem(["", self.store.display_name(map_key),
                                   self._count_text(n_route, n_glob)])
            top.setData(0, _ROLE_KIND, "map")
            top.setData(0, _ROLE_ID, map_key)
            top.setData(0, _ROLE_KEY, f"map|{map_key}")
            top.setFlags(GROUP_ITEM_FLAGS)
            f = QFont()
            f.setBold(True)
            top.setFont(_COL_NAME, f)
            self.tree.addTopLevelItem(top)
            for mode_label, vs in modes:
                n_mode = sum(len(rows) for _s, _n, rows, _g in vs)
                g_mode = sum(len(g) for _s, _n, _r, g in vs)
                mid = QTreeWidgetItem(["", mode_label,
                                       self._count_text(n_mode, g_mode)])
                mid.setData(0, _ROLE_KIND, "mode")
                mid.setData(0, _ROLE_ID, map_key)
                mid.setData(0, _ROLE_KEY, f"mode|{map_key}|{mode_label}")
                mid.setFlags(GROUP_ITEM_FLAGS)
                mid.setToolTip(_COL_NAME,
                               f"{self.store.display_name(map_key)} · {mode_label}\n"
                               f"模式由 8111 的战区(capture_zone)数量推出: "
                               f"3 个=战区控制, 2 个=战区攻陷, 1 个=战区争夺")
                top.addChild(mid)
                for sig, vname, routes, globs in vs:
                    var = QTreeWidgetItem(["", vname,
                                           self._count_text(len(routes), len(globs))])
                    var.setData(0, _ROLE_KIND, "variant")
                    var.setData(0, _ROLE_ID, map_key)
                    var.setData(0, _ROLE_SIG, sig)
                    var.setData(0, _ROLE_KEY, f"variant|{map_key}|{sig}")
                    var.setFlags(GROUP_ITEM_FLAGS)
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
                    # 全局录制条目排在单条路线之后(用户要求: 加在这一级的末尾)
                    for g in globs:
                        self._add_global_item(var, g)
                        alive_lives.update(
                            life.life_id for side in GLOBAL_SIDES
                            for life in g.lives(side))
        # 展开/折叠统一放在重建末尾恢复(见 _restore_folded)
        # 已缓存图片、但还没录过轨迹的地图也列出来(灰色, 用时列写“仅缓存”):
        # 否则在「地图管理」里给它们改完名字, 主界面根本看不到任何变化。
        # 顺便把它们**已见过的模式版本**也挂上(0 条) —— 还没录到轨迹时就能右键
        # 把“区域 1/2”改成自己认得的名字(大地图/小地图…)。
        listed = {key for key, _modes in variants_all}
        for map_key in self.store.ordered_map_keys():
            if map_key in listed or not self.map_cache.has(map_key):
                continue
            top = QTreeWidgetItem(["", self.store.display_name(map_key), "仅缓存"])
            top.setData(0, _ROLE_KIND, "map")
            top.setData(0, _ROLE_ID, map_key)
            top.setData(0, _ROLE_KEY, f"map|{map_key}")
            top.setFlags(GROUP_ITEM_FLAGS)
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
                var.setData(0, _ROLE_KEY, f"variant|{map_key}|{sig}")
                var.setFlags(GROUP_ITEM_FLAGS)
                var.setForeground(_COL_NAME, QBrush(Qt.GlobalColor.gray))
                var.setToolTip(_COL_NAME, self._variant_tip(map_key, sig, vname))
                top.addChild(var)
        self._restore_folded(folded, collapse_all=first_build)
        self._tree_built = True
        self.tree.blockSignals(False)
        self._visible_ids &= alive       # 被删除的记录自动从“显示中”移除
        self._visible_lives &= alive_lives
        if self._anchor is not None and not any(
                (rec := self.store.get(rid)) is not None and self._group_of(rec) == self._anchor
                for rid in alive):
            self._anchor = None          # 基准那些轨迹被删光了 → 解除锁定
        self._refresh_anchor_icon()      # 图钉要跟着基准标回对应版本行
        self._refresh_map_tracks()
        self._on_tree_selection_changed()

    def _folded_keys(self) -> set[str]:
        """当前被**折叠**的分组标识(`_ROLE_KEY`); 用于侧栏重建后恢复展开状态。"""
        folded: set[str] = set()

        def walk(item: QTreeWidgetItem) -> None:
            key = item.data(0, _ROLE_KEY)
            if key and not item.isExpanded():
                folded.add(key)
            for i in range(item.childCount()):
                walk(item.child(i))

        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            walk(root.child(i))
        return folded

    def _restore_folded(self, folded: set[str], collapse_all: bool = False) -> None:
        """恢复展开状态。

        * `collapse_all=True`(刚启动的那一次): **全部折叠** —— 打开软件先看到一排地图/线路,
          想看哪张再自己展开;
        * 之后的重建(关掉「地图管理」、录完一条、改名、删除): 用户折起来的保持折叠,
          用户展开过的保持展开; **新出现的**分组(新地图/新模式/新版本)默认展开。
        """
        def walk(item: QTreeWidgetItem) -> None:
            key = item.data(0, _ROLE_KEY)
            if key:
                item.setExpanded(False if collapse_all else key not in folded)
            for i in range(item.childCount()):
                walk(item.child(i))

        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            walk(root.child(i))

    def _expand_item(self, node_id: str) -> None:
        """把某个条目(单条轨迹 / 全局录制条目)的各级分组全部展开。

        启动时分组是折叠的, 新录的这条会藏在里面 —— 不展开的话侧栏看起来几乎没变化。
        """
        found: list[QTreeWidgetItem] = []

        def walk(item: QTreeWidgetItem) -> None:
            for i in range(item.childCount()):
                ch = item.child(i)
                if (ch.data(0, _ROLE_ID) == node_id
                        and ch.data(0, _ROLE_KIND) in ("route", "global")):
                    found.append(ch)
                    return
                walk(ch)

        walk(self.tree.invisibleRootItem())
        node = found[0] if found else None
        while node is not None:
            node.setExpanded(True)
            node = node.parent()

    def _expand_to_route(self, route_id: str) -> None:
        """兼容旧调用点: 展开到某条单条轨迹。"""
        self._expand_item(route_id)

    def _on_tree_item_clicked(self, item: QTreeWidgetItem, col: int) -> None:
        """点击第一列的**眼睛图标** → 切换该条目在地图上的显示/隐藏。"""
        if col != _COL_EYE:
            return
        kind = item.data(0, _ROLE_KIND)
        if kind == "route":
            if self._in_playback():
                # 回放绘制优先于轨迹显示, 此时改眼睛没有意义(面板上也已禁用侧栏)
                self._set_status("回放中无法修改轨迹显示, 请先点「■ 结束播放」", sticky=2.0)
                return
            route_id = item.data(0, _ROLE_ID)
            self._set_visible(item, route_id not in self._visible_ids)
        elif kind in ("glife", "gside", "global"):
            if self._in_playback():
                self._set_status("回放中无法修改轨迹显示, 请先点「■ 结束播放」", sticky=2.0)
                return
            ids = self._life_ids_of(item)
            label = item.text(_COL_NAME)
            # 判断“这一层当前亮不亮”只算真实存在的线路(残留 id 会让点一下全灭)
            live_ids = [gid for gid in ids if self._life_by_id(gid) is not None]
            shown = not any(gid in self._visible_lives for gid in live_ids)
            self._set_lives_visible(ids, shown, what=f"「{label}」")

    def _set_lives_visible(self, ids: list[str], shown: bool, what: str = "") -> None:
        """统一控制“全局录制里的线路”在地图上的显示/隐藏。

        只点亮本条的线路时**不清掉别人**: 全局线路之间不冲突(它们同属一张地图,
        本来就是整局所有玩家的轨迹, 叠在一起才有意义)。

        **只认真正存在的线路**: `_visible_lives` 里万一残留了已重录/删除的 id,
        会让“点一下眼睛”的判断(any in ...)全错 —— 所以先过滤。
        """
        existing = {gid for gid in ids if self._life_by_id(gid) is not None}
        if not existing:
            return
        if shown:
            self._visible_lives.update(existing)
        else:
            self._visible_lives -= existing
        self._refresh_eye_icons()
        self._refresh_map_tracks()
        if shown:
            # 点在缓存底图上看不到线路时, 自动切到这条全局录制所属的地图
            for g in self.store.globals:
                if any(life.life_id in self._visible_lives for side in GLOBAL_SIDES
                       for life in g.lives(side)):
                    self._ensure_map_for([g])
                    break
        tail = f" {what}" if what else ""
        self._set_status(
            f"{'已显示' if shown else '已隐藏'}{tail} {len(existing)} 条全局线路"
            f"(当前共 {len(self._visible_lives)} 条)", sticky=2.5)


    def _visible_route_ids(self) -> list[str]:
        """按侧栏顺序列出眼睛点亮的轨迹 id。"""
        return [it.data(0, _ROLE_ID) for it in self._iter_route_items()
                if it.data(0, _ROLE_ID) in self._visible_ids]

    def _merge_duplicate_maps(self) -> list[tuple[str, str]]:
        """把“同一张地图被发成不同分辨率”的重复条目合并成一张地图。

        8111 同一张地图会以 1024²/2048²/4096² 不同分辨率发出(实测), 老版本按字节
        哈希当地图身份 → 同一张图被当成两张(2026-10-09 用户反馈)。
        `MapCache.merge_duplicates` 会按“缩到 64×64 灰度后的平均差”认出来,
        并把轨迹/名字/战区布局并到保留的那个键上(会先备份 routes.json)。
        """
        try:
            merged = self.map_cache.merge_duplicates(self.store)
        except Exception:  # noqa: BLE001 - 合并失败不应该影响启动
            log.exception("合并重复地图失败")
            return []
        if merged:
            log.info("启动时合并了 %d 个重复地图条目", len(merged))
        return merged

    def _refresh_map_tracks(self) -> None:
        """把“眼睛点亮的轨迹”交给地图显示, 并把**选中的那几条**标为高亮。

        两层绘制:
        * 普通层 = `_visible_ids`(眼睛点亮) → 不透明灰色;
        * **高亮层 = 侧栏当前选中的那几条里、眼睛也点亮了的** → 速度配色, 可多条;
          没选中任何条目时才退回“刚录制的那条”。

        高亮层**不会**把眼睛没点亮的轨迹画出来: 点眼睛时该行会先被选中,
        若高亮层又把它画回去, 就会看起来像“点了眼睛没反应”(2026-10-07 修的)。
        """
        # 重录/删除后可能残留已经不存在的线路 id → 先剪掉（否则眼睛与显示会错位）
        alive_lives = {life.life_id for g in self.store.globals
                       for side in GLOBAL_SIDES for life in g.lives(side)}
        if not self._visible_lives <= alive_lives:
            self._visible_lives &= alive_lives
            self._refresh_eye_icons()
        recs = [r for rid in self._visible_route_ids()
                if (r := self.store.get(rid)) is not None]
        # 眼睛点亮的全局线路也当成普通轨迹画(灰色层)
        recs += self._visible_life_objects()
        picked = self._selection_playables()
        if picked:
            highlight = [r for r in picked if self._is_shown(r)]
        else:
            # 没选中时退回“刚录制的那条”(只在它还显示着的时候高亮)
            hl = self._last_record
            highlight = [hl] if (hl is not None and hl.route_id in self._visible_ids) else []
        recorder = getattr(self, "recorder", None)   # 构造早期还没有 recorder
        if recorder is not None and recorder.recording:
            # 录制中 live 是正在录的那条, 不能被选中/高亮覆盖
            self.map_view.set_visible_tracks(recs)
            self.map_view.set_highlight([])
            return
        self.map_view.set_visible_tracks(recs)
        self.map_view.set_highlight(highlight)
        self.map_view.set_live(None)
        self._apply_zones()

    def _is_shown(self, rec) -> bool:
        """这条轨迹/线路当前是不是“眼睛点亮”状态。"""
        rid = getattr(rec, "route_id", "")
        return rid in self._visible_ids or rid in self._visible_lives

    def _visible_life_objects(self) -> list:
        """眼睛点亮的全局线路(按 `_visible_lives` 里的 id 取出来)。"""
        if not self._visible_lives:
            return []
        out: list = []
        for g in self.store.globals:
            for side in GLOBAL_SIDES:
                for life in g.lives(side):
                    if life.life_id in self._visible_lives:
                        out.append(life)
        return out


    def _life_owner(self, life) -> GlobalRecord | None:
        """全局线路(`UnitLife`)所属的全局条目(找不到返回 None)。"""
        lid = getattr(life, "life_id", "")
        if not lid:
            return None
        for g in self.store.globals:
            for side in GLOBAL_SIDES:
                if any(lf.life_id == lid for lf in g.lives(side)):
                    return g
        return None

    def _zone_signature_for_view(self) -> str:
        """当前视图(回放中的 / 眼睛点亮的轨迹)对应的模式指纹(取不到就空串)。

        多条轨迹时只在它们指纹一致时才返回 —— 否则无法决定画哪套战区。

        2026-10-22 修: 全局线路(`UnitLife`)**自己没有** `zone_signature`,
        以前这里直接把它当空串跳过 → `layout_for(key, "")` 退化成 `layouts[0]`,
        于是回放 AB 队录制的线路时画的是**单条路线录制时**那套战区位置。
        现在对全局线路反查它所属的 `GlobalRecord.zone_signature`。
        """
        recs = self.map_view.playback_records or self.map_view.visible_tracks
        sigs = {s for r in recs if (s := getattr(r, "zone_signature", ""))}
        if len(sigs) == 1:
            return sigs.pop()
        if sigs:
            return ""                    # 多条轨迹模式不一致 → 无法决定
        gsigs: set[str] = set()          # 全是全局线路 → 问它们所属的条目
        for r in recs:
            owner = self._life_owner(r)
            if owner is not None and owner.zone_signature:
                gsigs.add(owner.zone_signature)
        return gsigs.pop() if len(gsigs) == 1 else ""

    def _playback_global(self) -> GlobalRecord | None:
        """回放中若含全局线路, 返回它们所属的全局条目(用于取战区时间轴)。"""
        for r in self.map_view.playback_records:
            owner = self._life_owner(r)
            if owner is not None:
                return owner
        return None

    def _apply_zones(self, info: dict | None = None) -> None:
        """决定地图上画哪套战区(小菱形 + A/B/C)。

        优先级:
        1. **回放全局录制** → 用录像里记的战区状态时间轴(归属变化、占领中都能重现);
           这是 2026-10-22 用户要求的: 回放 AB 队线路时不该显示当前战局/别的录像的战区;
        2. 战局内且 8111 正在给同一张图 → 用**实时**战区;
        3. 其它(游戏关了/在看缓存底图) → 用该地图存过的布局:
           优先匹配“正在看的轨迹的模式指纹”, 没有就取最近一次见过的。
        """
        key = self.map_view.map_key
        if not key:
            return
        recorder = getattr(self, "recorder", None)
        live = list((info or {}).get("zones") or [])
        live_spawns = list((info or {}).get("spawns") or [])
        layout = self.store.layout_for(key, self._zone_signature_for_view())
        # 1) 回放全局录制: 按 playhead 取当时的战区快照
        if self._in_playback():
            g = self._playback_global()
            snap = g.zone_state_at(self.player.time) if g is not None else None
            if snap:
                self.map_view.set_zones(snap)
                self.map_view.set_spawns(spawns_to_json(ground_spawns(
                    spawns_from_json((layout or {}).get("spawns")))))
                return
        if live and recorder is not None and recorder.map_key == key:
            self.map_view.set_zones(zones_to_json(live))
            # 只画**地面**重生位置(空军的 fighter/bomber/ucav 也记了, 但地图上不画)
            self.map_view.set_spawns(spawns_to_json(ground_spawns(live_spawns)))
            return
        # 旧布局: 位置 + 当时存下的归属色(用户 2026-10-22 要求把归属显示回来;
        # `blink` 不存也不画 —— 那只是那一刻"正在被占领"的瞬时状态)
        self.map_view.set_zones([
            {"label": z.get("label"), "x": z.get("x"), "y": z.get("y"),
             "color": z.get("color"), "side": z.get("side")}
            for z in (layout or {}).get("zones") or []
        ])
        # 重生位置: 存过就画(同一张图不同版本存的是各自的那份)
        self.map_view.set_spawns(spawns_to_json(ground_spawns(
            spawns_from_json((layout or {}).get("spawns")))))

    def _selection_tracks(self) -> list[RouteRecord]:
        """侧栏中当前选中的线路(可 Shift/Ctrl 多选), 按侧栏顺序。"""
        out: list[RouteRecord] = []
        for it in self._iter_route_items():
            if not self._is_item_selected(it):
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

    def _life_by_id(self, life_id: str):
        """按 life_id 找全局线路(找不到返回 None)。"""
        for g in self.store.globals:
            for side in GLOBAL_SIDES:
                for life in g.lives(side):
                    if life.life_id == life_id:
                        return life
        return None

    def _selection_lives(self) -> list:
        """侧栏中选中的**全局线路**(按侧栏顺序)。

        选中整个全局条目 / A·B 队行时, 把它们下面的全部线路都算进来;
        只选中几条线路时就只算那几条 —— 与用户要的“选了就播选中的”一致。
        按**树的顺序**取(不能用 `selectedItems()` 的顺序, 那是选择模型的顺序)。

        选中与否以 `selectionModel` 为准(见 `_is_item_selected` 的说明): 只看
        `QTreeWidgetItem.isSelected()` 会把“父项折叠时只有内部标志”的条目也算进来,
        于是明明取消掉的其它地图的线路还在选中集合里。
        """
        def hit(item: QTreeWidgetItem) -> bool:
            node: QTreeWidgetItem | None = item
            while node is not None:
                if (self._is_item_selected(node)
                        and node.data(0, _ROLE_KIND) in ("glife", "gside", "global")):
                    return True
                node = node.parent()
            return False

        out: list = []
        for it in self._iter_life_items():
            if not hit(it):
                continue
            life = self._life_by_id(it.data(0, _ROLE_ID))
            if life is not None:
                out.append(life)
        return out

    def _iter_all_items(self):
        """递归遍历侧栏里的**全部**条目(不限可见性)。"""
        def walk(item):
            for i in range(item.childCount()):
                ch = item.child(i)
                yield ch
                yield from walk(ch)

        yield from walk(self.tree.invisibleRootItem())

    def _is_item_selected(self, item: QTreeWidgetItem) -> bool:
        """该条目此刻是否真的处于选中状态(index 级判断)。

        不能用 `QTreeWidgetItem.isSelected()`: 父项折叠时它可能只是 item 自身的
        标志位, 并不在 selectionModel 里(实测同一行 isSelected()=True 但
        `selectedIndexes()` 里没有它) —— 那样子项会被误认为“选中”。
        也不能用 `_ROLE_KEY` 建集合: 同一个全局条目下的所有线路条目共用同一个 KEY。
        """
        model = self.tree.selectionModel()
        if model is None:
            return False
        idx = self.tree.indexFromItem(item, 0)
        return idx.isValid() and model.isSelected(idx)

    def _selection_playables(self) -> list:
        """回放/高亮用: 选中的单条轨迹 + 选中的全局线路(侧栏顺序)。"""
        return self._selection_tracks() + self._selection_lives()

    def _playable_group(self, r) -> tuple[str, str]:
        """一条**可播放线路**属于哪个分组 (地图, 战区指纹)。

        单条轨迹自带 `zone_signature`; 全局线路(`UnitLife`)只有 map_key,
        它的版本信息在**所属全局录制**上 —— 不这样取的话, 同一张地图不同版本的
        全局条目会被当成同一组。
        """
        if isinstance(r, RouteRecord):
            return self._group_of(r)
        lid = getattr(r, "life_id", "")
        if lid:
            for g in self.store.globals:
                if any(l.life_id == lid for side in GLOBAL_SIDES for l in g.lives(side)):
                    return (g.map_key or "", g.zone_signature or "")
        return (getattr(r, "map_key", "") or "", "")

    def _item_groups(self, item: QTreeWidgetItem) -> set[tuple[str, str]]:
        """某个侧栏条目(任意层级)涉及哪些分组 —— 取消越界选中时按它判定。"""
        groups: set[tuple[str, str]] = set()
        for lid in self._life_ids_of(item):
            life = self._life_by_id(lid)
            if life is not None:
                groups.add(self._playable_group(life))
        if not groups:
            rec = self.store.get(item.data(0, _ROLE_ID))
            if rec is not None:
                groups.add(self._playable_group(rec))
        return groups

    def _on_tree_selection_changed(self) -> None:
        """选中项变化 → 回放将播放这几条; 同时**限制为同一张地图的同一版本**。

        跨地图(底图只能是一张)/跨版本(战区位置不同, 线路本来就不可比)一起回放没有意义,
        所以选中了多个版本时只保留一个:

        * 双击定过“基准”时, **基准优先** —— Ctrl 点到别的版本不会把基准换掉(锁住);
        * 没定基准时保留“本次新加进来的那个版本”。

        基准(图钉)还会**跟随当前选中所在的版本** —— 刚开始没选中任何条目时,
        第一个点到的路线就把它的“地图·模式·版本”定为基准。

        判定用 `_selection_playables()`(单条轨迹 + 全局线路)而不是只看单条轨迹:
        以前用 `_selection_tracks()`, 而它只认 `kind == "route"` 的条目 —— 选中
        全局条目 / A·B 队行 / 全局线路时列表是空的, 跨地图限制完全不生效
        (用户 2026-10-22: 选中 AB 队所有路线后还能 Ctrl 点别的地图的路线)。
        """
        if self._reselecting or self._in_playback():
            return
        playables = self._selection_playables()
        if not playables:
            self._sel_ids = set()
            self._sel_groups = set()
            self._update_play_label()
            self._refresh_map_tracks()   # 选中全局线路时也要把高亮刷出来
            return                       # 清空选中时保留基准(图钉不消失)
        groups = {self._playable_group(r) for r in playables}
        if len(groups) > 1:
            keep: tuple[str, str] | None = None
            if self._anchor is not None and self._anchor in groups:
                keep = self._anchor
            else:
                new_groups = groups - self._sel_groups
                keep = next(iter(new_groups), None) or self._playable_group(playables[-1])
            self._reselecting = True
            try:
                self.tree.blockSignals(True)
                # 按**条目**取消选中(它可能是全局条目/队伍行, 没法用 route_id 选回去)。
                # 两套都清: selectionModel(真正生效的选中) + item 自身标志位 ——
                # 折叠组里的条目可能只改了自身标志, 不在 selectionModel 里
                # (`selectedItems()` 还会过滤隐藏项, 所以也不能用它)。
                model = self.tree.selectionModel()
                seen: set[int] = set()
                for idx in list(model.selectedIndexes()):
                    it = self.tree.itemFromIndex(idx)
                    if it is None or id(it) in seen:
                        continue
                    seen.add(id(it))
                    if self._item_groups(it) - {keep}:
                        it.setSelected(False)
                for it in self._iter_all_items():
                    if it.isSelected() and self._item_groups(it) - {keep}:
                        it.setSelected(False)
                self.tree.blockSignals(False)
            finally:
                self._reselecting = False
            playables = [r for r in playables if self._playable_group(r) == keep]
            groups = {keep}
            self._set_status(
                "同一张地图的同一版本才能一起回放/显示, 已只保留一个版本内的选中条目",
                sticky=3.0)
        self._sel_groups = groups
        self._sel_ids = {getattr(r, "route_id", "") for r in playables}
        grp = self._playable_group(playables[0])
        if grp != self._anchor:
            self._anchor = grp          # 基准跟着当前选中走
            self._refresh_anchor_icon()
        self._update_play_label()
        self._refresh_map_tracks()      # 高亮层 = 选中的那几条 → 选中变了要重画

    def _on_tree_double_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        """双击轨迹行 → 定基准 + **把它显示到地图上并切到对应地图图片**。

        双击地图/模式/版本行不做处理(保留 Qt 默认的展开/折叠)。
        """
        if item.data(0, _ROLE_KIND) != "route":
            if item.data(0, _ROLE_KIND) in ("global", "gside", "glife"):
                # 双击全局条目/队伍/线路 = 显示它并切到对应地图(不能回放, 只能看)
                if self._in_playback():
                    self._set_status("回放中无法切换显示, 请先点「■ 结束播放」", sticky=2.0)
                    return
                self._set_lives_visible(self._life_ids_of(item), True)
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
        return (getattr(rec, "map_key", "") or "",
                getattr(rec, "zone_signature", "") or "")

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
        """信息区“回放”那一栏: 显示基准版本 + 待回放条数(含选中的全局线路)。"""
        n = len(self._selection_playables())
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
        if kind in ("global", "gside", "glife"):
            self._fill_global_menu(menu, item, kind)
        elif kind == "route":
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

    # --------------------------------------------------- 全局录制的右键菜单
    def _fill_global_menu(self, menu: QMenu, item: QTreeWidgetItem, kind: str) -> None:
        """全局条目 / A·B 队行 / 单条线路的右键菜单。

        标记只在“全局条目”这一层打: 它表示**下一次点「A/B 队录制」录到哪一条**上。
        没有任何条目标记时, 点录制按钮会新建一个全局条目。
        """
        ids = self._life_ids_of(item)
        act_show = QAction("在地图上显示", self)
        act_show.triggered.connect(lambda: self._set_lives_visible(ids, True))
        menu.addAction(act_show)
        act_hide = QAction("从地图上隐藏", self)
        act_hide.triggered.connect(lambda: self._set_lives_visible(ids, False))
        menu.addAction(act_hide)
        if kind != "glife":
            gid = item.data(0, _ROLE_ID)
            g = self.store.get_global(gid)
            menu.addSeparator()
            for side in GLOBAL_SIDES:
                act = QAction(f"标记为 {GLOBAL_SIDE_LABELS[side]}录制目标", self)
                act.setCheckable(True)
                act.setChecked(g is not None and side in g.marks)
                act.setToolTip(
                    f"打上标记后, 点「{side} 队录制」会录到这一条(而不是新建一条)。\n"
                    f"同一队只会有一条记录带标记 —— 打到别处时旧的会自动清掉。")
                act.toggled.connect(
                    lambda checked, gid=gid, s=side: self._toggle_mark(gid, s, checked))
                menu.addAction(act)
            act_clear = QAction("清除标记", self)
            act_clear.setEnabled(g is not None and bool(g.marks))
            act_clear.setToolTip("清除后, 点「A/B 队录制」会新建一个全局录制条目")
            act_clear.triggered.connect(lambda: self._clear_mark(item.data(0, _ROLE_ID)))
            menu.addAction(act_clear)
        menu.addSeparator()
        act_expand = QAction("展开/折叠", self)
        act_expand.triggered.connect(lambda: item.setExpanded(not item.isExpanded()))
        menu.addAction(act_expand)
        if kind == "global":
            menu.addSeparator()
            act_del = QAction("删除该全局录制…", self)
            act_del.triggered.connect(
                lambda: self._delete_global(item.data(0, _ROLE_ID)))
            menu.addAction(act_del)

    def _toggle_mark(self, gid: str, side: str, on: bool) -> None:
        """打/清“下次录到这一条”的标记。"""
        g = self.store.get_global(gid)
        if g is None:
            return
        self.store.set_mark(gid, side, on)
        self._reload_tree()
        self._set_status(
            f"已把「{g.label}」标记为 {side} 队录制目标"
            f"(点「{side} 队录制」就录到这一条)" if on
            else f"已取消「{g.label}」的 {side} 队标记", sticky=4.0)

    def _clear_mark(self, gid: str) -> None:
        g = self.store.get_global(gid)
        if g is None:
            return
        self.store.clear_marks(gid)
        self._reload_tree()
        self._set_status(
            f"已清除「{g.label}」的标记 —— 下次点「A/B 队录制」会新建一个全局条目",
            sticky=4.0)

    def _delete_global(self, gid: str) -> None:
        g = self.store.get_global(gid)
        if g is None:
            return
        ok = QMessageBox.question(
            self, "删除全局录制",
            f"确定删除这条全局录制吗?\n\n"
            f"{g.label} · {g.map_name} · {g.mode_label}\n"
            f"A 队 {len(g.lives('A'))} 条 / B 队 {len(g.lives('B'))} 条线路\n\n"
            f"删除后无法恢复(单条线路录制的轨迹不受影响)。")
        if ok != QMessageBox.StandardButton.Yes:
            return
        self.store.remove_global(gid)
        if self._last_global is not None and self._last_global.route_id == gid:
            self._last_global = None
        self._reload_tree()
        self._set_status("已删除全局录制条目", sticky=3.0)

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
        """按 _visible_ids / _visible_lives 重刷所有眼睛图标(不重建树)。

        单条轨迹看自己的 id; 全局线路看 `_visible_lives`; 全局条目行与 A/B 队行
        是**汇总**: 只要下面有任意一条线亮着, 就点亮。
        """
        def walk(item: QTreeWidgetItem) -> None:
            kind = item.data(0, _ROLE_KIND)
            if kind == "route":
                item.setIcon(_COL_EYE,
                             eye_icon(item.data(0, _ROLE_ID) in self._visible_ids))
            elif kind == "glife":
                item.setIcon(_COL_EYE,
                             eye_icon(item.data(0, _ROLE_ID) in self._visible_lives))
            elif kind in ("global", "gside"):
                ids = self._life_ids_of(item)
                item.setIcon(_COL_EYE,
                             eye_icon(any(i in self._visible_lives for i in ids)))
            for i in range(item.childCount()):
                walk(item.child(i))

        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            walk(root.child(i))

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

    # ------------------------------------------------------------- 全局录制
    def _start_global(self, side: str) -> None:
        """点「A 队录制 / B 队录制」。

        目标条目 = 侧栏里被标记为这一队的全局条目; 没有标记 → 新建一条。
        如果目标条目这一队**已经录过**, 先问“是否清空重录”(用户要求的确认)。
        """
        if self._in_playback():
            play_error_sound()
            self._set_status("回放中已暂停录制功能, 请先点「■ 结束播放」", sticky=2.0)
            return
        if self.recorder.global_recording:
            self._set_status(
                f"已在全局录制中({self.recorder.global_side} 队), 请先点「停止 A/B 队录制」",
                sticky=2.5)
            return
        if self.recorder.recording:
            self._set_status("正在录制单条轨迹, 请先结束当前录制", sticky=2.5)
            return
        target = self.store.marked_global(side)
        if target is not None and target.lives(side):
            n = len(target.lives(side))
            ok = QMessageBox.question(
                self, f"{side} 队录制",
                f"「{target.label}」的 {side} 队已经录制了 {n} 条玩家线路。\n\n"
                f"要清空这 {n} 条, 重新录制本条目 {side} 队的所有玩家路线吗?\n"
                f"(选“否”则取消, 原有录制保持不变)")
            if ok != QMessageBox.StandardButton.Yes:
                self._set_status("已取消: 保留原有的全局录制", sticky=3.0)
                return
        play_sound("start")
        self.recorder.request_global_start(side, target)

    def _stop_global(self) -> None:
        if not self.recorder.global_recording:
            self._set_status("当前没有正在进行的全局录制", sticky=2.0)
            return
        play_sound("stop")
        self.recorder.request_global_stop()

    def _on_global_armed(self, rec: GlobalRecord, side: str) -> None:
        """已建好条目, 正在等第一名玩家重生(只有此时可以点停止/取消)。"""
        self.player.pause()          # 全局录制与回放互斥
        self.player.load(None)
        self.map_view.set_playback(None)
        self._visible_ids.clear()
        self._visible_lives.clear()
        self._reload_tree()
        self._update_buttons()
        # 把底图切回**游戏当前地图**(用户可能正在预览另一张图的轨迹)
        key = self.recorder.map_key
        if key and key != PLACEHOLDER_MAP_KEY and self.map_view.map_key != key:
            self._show_cached_map(key)
        self._set_status(
            f"全局录制已就绪({side} 队 → 「{rec.label}」): 等待第一名玩家重生后开始计时…\n"
            f"现在请让游戏进入开局/回放, 点「停止 A/B 队录制」可取消", sticky=10.0)

    def _on_global_started(self, rec: GlobalRecord, side: str) -> None:
        """第一次看到可录单位 → 0 点已定。"""
        self._set_status(
            f"{side} 队全局录制中: 已捕获第一名玩家, 计时从 0 开始"
            f"(地图: {rec.map_name})", sticky=6.0)

    def _on_global_finished(self, rec: GlobalRecord, side: str) -> None:
        self._last_global = rec
        self._last_duration = rec.duration
        self._update_buttons()
        # 刚录完的这一队自动点亮, 否则侧栏看起来“什么都没发生”
        self._visible_lives = {life.life_id for life in rec.lives(side)}
        self._reload_tree()
        self._expand_item(rec.route_id)
        self._ensure_map_for([rec])
        self._set_status(
            f"全局录制完成: {rec.label} · {side} 队 {len(rec.lives(side))} 条线路"
            f" | 用时 {fmt_time(rec.duration)}", sticky=8.0)

    def _on_global_cancelled(self) -> None:
        self._update_buttons()
        self._reload_tree()
        self._set_status("已取消全局录制(没有录到任何线路, 条目已丢弃)", sticky=4.0)

    # ==================================================================== 动作
    def _hotkey_action(self, action: str) -> None:
        """两套全局快捷键通道的统一入口(先去掉重复命中, 再走按钮那条路)。"""
        now = time.monotonic()
        if now - self._last_hotkey_at.get(action, 0.0) < 0.3:
            return                    # 系统热键与轮询可能同时命中 → 只算一次
        self._last_hotkey_at[action] = now
        self._handle_action(action)

    def apply_hotkey(self, action: str, binding: tuple[str, str]) -> None:
        """改某个动作的快捷键: 两套通道立即生效并存盘(设置对话框调用)。"""
        self._hotkey_bindings[action] = (binding[0] or "", binding[1])
        self.hotkeys.set_bindings(self._hotkey_bindings)
        self.sys_hotkeys.apply(self._hotkey_bindings)
        save_bindings(self._settings, {action: self._hotkey_bindings[action]})
        self._update_buttons()
        self._set_status(f"{ACTION_LABELS[action]} 的快捷键已改为 "
                         f"{describe_binding(self._hotkey_bindings[action])}", sticky=3.0)

    def _on_poll_toggled(self, on: bool) -> None:
        """菜单开关: 是否同时用 GetAsyncKeyState 轮询(系统热键通道始终在跑)。"""
        self._poll_enabled = bool(on)
        self._settings.setValue("hotkey_poll", 1 if on else 0)
        if on:
            self.hotkeys.start()
        else:
            self.hotkeys.stop()
        self._set_status("已启用按键轮询(可区分左右 Shift)" if on
                         else "已关闭按键轮询, 只使用系统热键(不读按键状态)", sticky=3.0)

    def _reset_hotkeys(self) -> None:
        """菜单: 把三个快捷键恢复默认(F5 开始 / F6 结束 / F7 标记)。"""
        for action, binding in DEFAULT_BINDINGS.items():
            self._hotkey_bindings[action] = binding
        self.hotkeys.set_bindings(self._hotkey_bindings)
        self.sys_hotkeys.apply(self._hotkey_bindings)
        reset_bindings(self._settings)
        self._set_status("快捷键已恢复默认: F5 开始 / F6 结束 / F7 标记", sticky=4.0)

    def _open_hotkey_settings(self) -> None:
        """菜单「快捷键 → 快捷键设置」: 改按键/修饰键, 改动立即生效。"""
        dlg = HotkeyDialog(self, self)
        dlg.exec()
        self._set_status("快捷键: " + " · ".join(
            f"{ACTION_LABELS[a]}={describe_binding(b)}"
            for a, b in self._hotkey_bindings.items()), sticky=4.0)
        self._update_buttons()

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

    def _on_dead_style_changed(self) -> None:
        """回放的两个“被击毁”显示选项: 应用到地图并记住设置。"""
        dim = self.chk_dead_dim.isChecked()
        mark = self.chk_dead_mark.isChecked()
        self._saved_dead_dim, self._saved_dead_mark = dim, mark
        self._settings.setValue("dead_dim", 1 if dim else 0)
        self._settings.setValue("dead_mark", 1 if mark else 0)
        self.map_view.set_dead_style(dim=dim, mark=mark)
        if dim or mark:
            self._set_status(
                f"回放显示: 阵亡变暗{'开' if dim else '关'} · "
                f"阵亡标记{'开' if mark else '关'}", sticky=3.0)
        else:
            self._set_status("回放显示: 不区分阵亡", sticky=3.0)

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
        g_rec = self.recorder.global_recording
        busy = recording or g_rec          # 两种录制都算“占用主界面”
        self.btn_start.setEnabled(not busy and not in_play)
        self.btn_stop.setEnabled(recording)
        # 等待移动期间还没定下起点, 标记没有意义
        self.btn_mark.setEnabled(recording and not armed)
        self.chk_start_on_move.setEnabled(not busy and not in_play)
        self.cmb_rate.setEnabled(not busy and not in_play)
        self.tree.setEnabled(not in_play and not busy)   # 含眼睛点击与右键菜单
        # 全局录制按钮: 自己录制中时两个“录制”按钮禁用, 只留“停止”
        self.btn_rec_a.setEnabled(not busy and not in_play)
        self.btn_rec_b.setEnabled(not busy and not in_play)
        self.btn_rec_stop.setEnabled(g_rec)
        self.btn_rec_stop.setStyleSheet("font-weight:bold;" if g_rec else "")
        # 录制期间屏蔽会改变“显示/底图”的入口, 避免与录制抢主界面
        self.act_manage.setEnabled(not busy)
        self.act_refresh.setEnabled(not busy)
        # 回放控制
        has_track = (bool(self.player.records) or self._last_record is not None
                     or bool(self.store.routes)
                     or any(g.count() for g in self.store.globals))
        self.btn_play.setEnabled(not busy and has_track)
        self.btn_play_mark.setEnabled(not busy and has_track)
        self.cmb_speed.setEnabled(not busy)
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
        picked = self._selection_playables()
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
        # 战区要跟着这次回放的**对象**换: 全局线路用条目自己的布局/时间轴,
        # 单条轨迹用它录的那套(见 `_apply_zones`) —— 否则会留着上一批线路的战区
        self._apply_zones()
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

    def _sync_playback_zones(self, t: float) -> None:
        """回放全局录制时, 把战区状态跟着时间轴一起推进。

        用户 2026-10-22 要求: 回放 AB 队线路时要能看到当时战区被谁占着、
        哪个正在被占领 —— 而不是一张与录像无关的静态布局(甚至是**另一个区域**
        的布局: 全局录像和单条路线可能根本不在同一片交战区)。
        `set_zones` 自己会去重, 所以每帧调用只是几次列表比较。
        """
        g = self._playback_global()
        if g is None:
            return
        snap = g.zone_state_at(t)
        if snap:
            self.map_view.set_zones(snap)

    def _on_playback_progress(self, t: float) -> None:
        recs = self.player.records
        self.map_view.set_playback(recs, t)
        self._sync_playback_zones(t)
        if recs:
            n = f"  ({len(recs)} 条)" if len(recs) > 1 else ""
            self.lbl_play.setText(
                f"回放 {fmt_time_ms(t)} / {fmt_time(self.player.end)}{n}")

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

            ginfo = info.get("global")
            if ginfo:
                # 全局录制期间状态栏只显示录制进度(比“可开始录制”重要得多)
                side = str(ginfo.get("side") or "?")
                elapsed = float(ginfo.get("elapsed") or 0.0)
                lives = int(ginfo.get("lives") or 0)
                self.lbl_timer.setText(f"计时: {fmt_time(elapsed)}")
                self.lbl_marks.setText(f"线路: {lives} 条")
                if ginfo.get("waiting"):
                    self.lbl_status.setText(
                        f"全局录制已就绪({side} 队): 等待第一名玩家重生后开始计时…"
                        f" | 点「停止 A/B 队录制」可取消")
                else:
                    self.lbl_status.setText(
                        f"{side} 队全局录制中 {fmt_time(elapsed)} | 线路 {lives} 条"
                        f"({int(ginfo.get('active') or 0)} 辆进行中) | "
                        f"回到车库自动结束")
            elif now < self._sticky_until:
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

        self.map_view.set_player(info.get("player"), info.get("dir"))
        # 全局录制: 边录边画(新点一多就重画一次模型层, 见 _tracks_key)
        lives = info.get("global_lives")
        if lives:
            self.map_view.set_live_multi(lives)
        elif self.map_view.live_multi:
            self.map_view.set_live_multi(None)   # 录完了 → 交给侧栏眼睛控制显示
        self._apply_zones(info)
        self.map_view.update()

    def _on_map_changed(self, data: bytes) -> None:
        # 用录制线程已经解析好的地图身份(同一张地图的不同分辨率会归到同一个键)
        key = self.recorder.map_key
        # 8111 偶尔只发低分辨率的那份(1024²); 手上有更清晰的同一张图就用它显示
        data = self.map_cache.sharpest(key, data)
        changed = self.map_view.set_map_bytes(data, key=key)
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
        if self._visible_lives:
            keys = self._life_map_keys()
            for lid in self._visible_lives:
                if lid in keys:
                    return keys[lid]
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
        return bool(self.map_view.set_map_bytes(data, key=key))

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
        # 全局线路同理: 只保留属于当前地图的那些
        life_keys = self._life_map_keys()
        stale_lives = {lid for lid in self._visible_lives
                       if life_keys.get(lid) != key}
        if stale_lives:
            self._visible_lives -= stale_lives
            self._reload_tree()
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
        previewed = len(self._visible_ids) + len(self._visible_lives)
        self._visible_ids.clear()
        self._visible_lives.clear()
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
                f"| 起点已标注 (按 "
                f"{describe_binding(self._hotkey_bindings.get('stop'))} 结束){note}"
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
        self._expand_to_route(rec.route_id)   # 分组可能是折叠的 → 展开到刚录的这条
        self._set_status(
            f"录制完成: {rec.map_name} | {rec.vehicle} | 用时 {fmt_time(rec.duration)} "
            f"| 轨迹点 {len(rec.points)} | 标记 {len(rec.marks) - 1} 个", sticky=6.0)

    def _on_notice(self, text: str) -> None:
        self._set_status(text, sticky=3.0)

    # ==================================================================== 收尾
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名
        try:
            self.hotkeys.stop()
            self.sys_hotkeys.stop()
            self.player.pause()
            self.recorder.stop()
            self.store.save()
        except Exception as exc:  # noqa: BLE001 - 退出时尽力清理
            log.warning("退出清理异常: %s", exc)
        super().closeEvent(event)
