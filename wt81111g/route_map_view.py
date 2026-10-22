"""地图画布: 显示 8111 地图图片, 绘制玩家位置、轨迹、标记与时间标注。

坐标约定: 8111 `/map_obj.json` 的 x/y 为 0~1 归一化值,
本控件把地图按原始宽高比等比缩放居中(letterbox), 再映射为像素。
"""
from __future__ import annotations

import hashlib
import logging
import math
import time
from bisect import bisect_right

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen, QPixmap,
    QPolygonF,
)
from PyQt6.QtWidgets import QWidget

from .route_store import (
    PLACEHOLDER_MAP_KEY, PLACEHOLDER_MAP_SHA1, RoutePoint, RouteRecord, fmt_speed,
    fmt_time, fmt_time_ms, point_at, split_points,
)
from .zones import BLINK_PERIODS

log = logging.getLogger("route_map_view")

# 起点/标记/终点配色
COLOR_START = QColor("#22c55e")   # 绿
COLOR_MARK = QColor("#facc15")    # 黄
COLOR_STOP = QColor("#ef4444")    # 红

# 玩家标记(像素, 不随缩放变): 位置空心圆圈 + 圆心实心点 + 表示车头朝向的 ^
PLAYER_RING_R = 12.0          # 位置空心圆圈半径
PLAYER_DOT_R = 4.0            # 圆心实心点半径
# ^ 用「前进量 + 横向半宽 + 高度」描述 —— 调 BACK 就是把整个 ^ 沿车头方向平移,
# 调 W 就是把两只脚岔开, 与肉眼看到的效果一一对应(2026-10-11 用户选定的形状)
PLAYER_DIR_BACK = 4.0         # 两只脚相对圆心的前进量(越小越贴近圆心)
PLAYER_DIR_W = 5.0            # 两只脚相对中轴的横向半宽(越大越岔开)
PLAYER_DIR_H = 4.5            # 尖点比两只脚连线再往前多少
# 回放光标 = **一个实心白点**(多条一起回放时环太乱, 2026-10-21 用户要求去掉环);
# 朝向仍用一根白线表示, 线起点贴住这个点(半径 PLAY_CURSOR_R)
# 回放光标: 一个实心白点, 朝向用**点内部的一条黑色短线**表示(用户 2026-10-22:
# 原来“白点 + 伸出圆点外的白色带黑边线”看着像小蝌蚪)。
PLAY_CURSOR_R = 4.0           # 白点半径(2026-10-22: 3.0 → 4.0, 给内部短线留空间)
CURSOR_DIR_BACK = 1.2         # 朝向短线起点到圆心的距离(点内部)
CURSOR_DIR_LEN = 2.6          # 短线长度 → 终点距圆心 3.8px(仍在 4px 半径内)
CURSOR_DIR_PEN = 1.8          # 短线粗细(1.5px 时经抗锯齿后几乎没有实黑像素)
COLOR_CURSOR_DIR = QColor("#0f172a")    # 短线黑色(与地图背景同色系)
COLOR_DIR_LINE = QColor("#f8fafc")   # 回放朝向线颜色(白, 与回放光标同色)
# 选中的历史轨迹: **不透明**(便于多条叠在一起对比)
COLOR_HISTORY = QColor("#8fa6c8")
# 回放时**尚未播放**的轨迹段: 半透明淡色 —— 半透明只用在这里
COLOR_UNPLAYED = QColor(140, 160, 200, 120)
# 回放中“这个玩家已经阵亡”的线路: **半透明纯灰**(不带蓝味, 与未播放的底衬区分开)
COLOR_DEAD_ROUTE = QColor(170, 176, 188, 120)
# 阵亡位置的红叉: **细而小的两笔**, 平头端点(圆头看起来会发胖/像描边)
# 颜色按“哪一队录的”区分: A 队 = 蓝(与 8111 的我方蓝一致), B 队 = 红
COLOR_DEAD_MARK = QColor("#ff3b30")       # 默认/B 队: 红
COLOR_DEAD_MARK_A = QColor("#174DFF")     # A 队: 蓝
COLOR_DEAD_MARK_B = COLOR_DEAD_MARK       # B 队: 红(不变)
DEAD_MARK_HALF = 2.6          # X 的半臂长(px, 不随缩放变)
DEAD_MARK_PEN = 1.2           # X 的线宽(px)
COLOR_PLAYER = QColor("#38bdf8")
COLOR_BG = QColor("#0f172a")
COLOR_GRID = QColor(255, 255, 255, 18)
# 战区: 菱形 + 字母, 颜色按归属(8111 约定: 蓝=我方, 红=敌方, 白=中立)
COLOR_ZONE = QColor("#f8fafc")
ZONE_SIDE_COLORS = {
    "ours": QColor("#174DFF"),      # 蓝 = 我方(与 8111 原色一致)
    "enemy": QColor("#fa0C00"),     # 红 = 敌方
    "neutral": QColor("#f8fafc"),   # 白 = 中立/未占领(看缓存底图时也用它)
}
ZONE_RADIUS = 8.0
# 被占领中的战区闪烁: 脉冲曲线与周期照官方 8111 页面(blink=1 → 2.0s, blink=2 → 1.2s),
# 但**不掺黄** —— 在本阵营色里做“变暗 ↔ 纯色”的呼吸(蓝闪蓝, 红闪红)
BLINK_TICK_MS = 40
BLINK_DIM = 0.4           # 脉冲谷底时保留的亮度比例(0.4 = 变暗到 40%)
ZONE_DARK = QColor(15, 23, 42)    # 变暗时拉向的底色(与界面同色系)

# 重生位置: **空心方块** —— 颜色与战区同一套(蓝=我方, 红=敌方)
SPAWN_SIDE_COLORS = ZONE_SIDE_COLORS
SPAWN_HALF = 3.4          # 方块半边长(px): 一个位置一个小方块, 连成一片就是重生区域
SPAWN_PEN = 1.7


def _blend(color: QColor, rgb: tuple[int, int, int], k: float) -> QColor:
    """从 color 按 k 比例拉向 rgb(k=0 不变, k=1 完全变成 rgb)。"""
    k = max(0.0, min(1.0, float(k)))
    return QColor(int(color.red() + (rgb[0] - color.red()) * k),
                  int(color.green() + (rgb[1] - color.green()) * k),
                  int(color.blue() + (rgb[2] - color.blue()) * k))

_SPEED_BUCKETS = 24  # 轨迹按速度量化的颜色档数(减少画笔切换)

# 缩放/平移: zoom=1.0 即“适配窗口”(等比缩放居中), 与未启用缩放时行为一致
MIN_ZOOM = 1.0
MAX_ZOOM = 20.0
WHEEL_STEP = 1.15        # 每格滚轮的缩放倍率
PAN_MARGIN = 40.0        # 平移时地图至少需留在视野内的像素

# 悬停命中半径(px): 鼠标到轨迹点的距离小于它就算“悬停在这条线上”
HOVER_RADIUS = 18.0
# 缓存层四周多画的反向余量(占控件尺寸比例): 拖动地图时靠偏移贴图复用整层,
# 超出余量才重建(见 _layer_tracks / _layer_stale)
LAYER_MARGIN = 0.35
# 拖出余量后**搬图层**(而不是整层重画)时, 新露出来的那条边最多占整层的这个比例;
# 超过就说明一次跳了很远(比如 reset_view / 缩放后), 还是整层重建划算(见 _shift_layer)
SHIFT_MAX_EXPOSED = 0.5

# 缩放/改窗口后“先用旧层按比例顶着, 停手这么久再重建一次变清晰”的延迟(毫秒)。
# 重建一次实测 20~50ms(1920×1040, 20 条线 1.7 万点), 滚轮连发时每格都重建就是
# “缩放卡”的唯一原因; 按比例贴一次只要 1~2ms(2026-10-21)。
GEOM_SETTLE_MS = 200
# 轨迹线宽(px): 归一化几何 + 缩放画笔, 实际线宽 = 它们(见 _draw_route)
TRACK_PEN = 2.4          # 选中的/正在录的(速度配色)
PLAY_PEN = 2.6           # 回放中已播放的段
# 逐段描边用的端帽: **平头**。轨迹是每段独立的 `drawLines`(见 build_speed_segments),
# 而 QPainter 给每段两端各加一个半圆端帽非常贵 —— 同样 2.96 万段实测
# 圆头 **216.6 ms** vs 平头 **61.2 ms**(3.5×, 且 `setClipRect` 一点都省不掉,
# 说明卡顿在描边而不在填充)。线宽只有 2.4~2.6 px, 平头在拐弯处留的缺口不到
# 一个像素(圆/平同屏 2× 放大对比肉眼分不出), 所以逐段绘制一律用平头。2026-10-21。
SEG_CAP = Qt.PenCapStyle.FlatCap


# 全局线路的速度配色基准 = 这条线自己的最高速(**与单条轨迹完全一致**)。
# 基准涨超过这个比例就整条重算(否则桶索引/颜色全是旧的)。取 4%: 颜色差肉眼看不出来,
# 又能避免每涨 1 km/h 就重建一次。
VMAX_REBUILD_RATIO = 0.04


def _normalized(x: float, y: float) -> QPointF:
    """`build_speed_segments` 用的“坐标变换”: 原样返回归一化坐标。"""
    return QPointF(x, y)


class _LiveGeom:
    """一条全局线路的**增量**分桶几何。

    把“按线段中点速度分桶 + 每桶一个 QPolygonF”做成可增量更新的版本:
    调用方每次只喂**完整点列表**, 本对象只处理新增的那几段, 并且只把“这帧加过点”的
    桶重新构造成 `QPolygonF`(其他桶直接用缓存)。

    速度基准(= 这条线的最高速, **与单条轨迹完全一致**)一变, 桶索引就全变了,
    所以基准变化超过 `VMAX_REBUILD_RATIO` 时整条重算(调用方还得重建整个图层,
    因为旧颜色已经画进图里了)。
    """
    __slots__ = ("n", "pts", "polys", "dirty", "vmax")

    def __init__(self) -> None:
        self.n = 0                                   # 已处理的点数
        self.pts: dict[int, list[QPointF]] = {}      # 桶 -> 点对列表
        self.polys: dict[int, QPolygonF] = {}        # 桶 -> 缓存的画笔几何
        self.dirty: set[int] = set()
        self.vmax = 0.0                             # 本几何用的速度基准

    def feed(self, points, vmax: float) -> dict[int, list[QPointF]]:
        """处理新增的点。返回**本次新增**的线段(按速度桶分组), 供“只补新段”的增量绘制用。"""
        n = len(points)
        changed = (self.vmax <= 0.0
                   or abs(vmax - self.vmax) > VMAX_REBUILD_RATIO * self.vmax)
        if n < self.n or changed:                    # 基准变了/点数变少 → 从头来
            self.n, self.pts, self.polys, self.dirty = 0, {}, {}, set()
            self.vmax = vmax
        added: dict[int, list[QPointF]] = {}
        for i in range(max(1, self.n), n):
            a, b = points[i - 1], points[i]
            idx = bucket_index((a.speed + b.speed) / 2.0, vmax)
            pair = (QPointF(a.x, a.y), QPointF(b.x, b.y))
            self.pts.setdefault(idx, []).extend(pair)
            added.setdefault(idx, []).extend(pair)
            self.dirty.add(idx)
        self.n = n
        for idx in self.dirty:
            self.polys[idx] = QPolygonF(self.pts[idx])
        self.dirty.clear()
        return added


def speed_color(speed: float, vmax: float) -> QColor:
    """速度 → 颜色: 慢(蓝) → 快(红)。"""
    ratio = 0.0 if vmax <= 0 else max(0.0, min(1.0, speed / vmax))
    hue = int(210 * (1.0 - ratio))
    return QColor.fromHsv(hue, 225, 255)


def bucket_index(speed: float, vmax: float) -> int:
    """速度 → 颜色桶索引(把连续速度量化, 减少画笔切换)。"""
    if vmax <= 0:
        return 0
    ratio = max(0.0, min(1.0, speed / vmax))
    return min(_SPEED_BUCKETS - 1, int(ratio * _SPEED_BUCKETS))


def player_dir_points(center: QPointF, ang: float) -> tuple[QPointF, QPointF, QPointF]:
    """玩家标记那个 ^ 的三个点(尖点, 脚 1, 脚 2); ang = 车头方向(弧度)。

    用「前进量 + 横向半宽 + 高度」描述(见 PLAYER_DIR_* 常量)而不是两个半径 ——
    这样"把整个 ^ 沿车头方向平移"就是改 BACK, "把两只脚岔开"就是改 W,
    和眼睛看到的效果一一对应。测试也直接用它取点, 不必重复算几何。
    """
    ca, sa = math.cos(ang), math.sin(ang)

    def pt(fwd: float, lat: float) -> QPointF:
        return QPointF(center.x() + fwd * ca - lat * sa,
                       center.y() + fwd * sa + lat * ca)

    tip = pt(PLAYER_DIR_BACK + PLAYER_DIR_H, 0.0)
    return (tip, pt(PLAYER_DIR_BACK, -PLAYER_DIR_W), pt(PLAYER_DIR_BACK, PLAYER_DIR_W))


def cursor_dir_points(center: QPointF, ang: float, back: float = CURSOR_DIR_BACK,
                      length: float = CURSOR_DIR_LEN) -> tuple[QPointF, QPointF]:
    """回放光标那根白线的两个端点(起点, 尖端); ang = 录制时的车头方向(弧度)。"""
    ca, sa = math.cos(ang), math.sin(ang)

    def pt(fwd: float) -> QPointF:
        return QPointF(center.x() + fwd * ca, center.y() + fwd * sa)

    return (pt(back), pt(back + length))


def build_speed_segments(points: list[RoutePoint], vmax: float,
                         to_px) -> dict[int, QPolygonF]:
    """把轨迹按"线段中点速度"分桶, 每段独立存为"一对点"。

    关键: 同一桶内的多段**不一定相邻**(速度波动时 A→B、C→D 同桶而 B→C 不同桶),
    因此必须用 `QPainter.drawLines(polygon)`(按点对解释为独立线段)绘制。
    若错用 `drawPolyline`, 它会把不相邻的段首尾相连, 地图上就会出现**分叉的斜线**。

    实现注意: 先往普通 list 里攒点、最后一次性构造 QPolygonF ——
    比反复 `QPolygonF.append()` 快 2~3 倍(每次 append 都是一次 Python→C++ 调用,
    5 条线 3766 个点原本要 30ms)。
    """
    tmp: dict[int, list[QPointF]] = {}
    for i in range(1, len(points)):
        a, b = points[i - 1], points[i]
        idx = bucket_index((a.speed + b.speed) / 2.0, vmax)
        tmp.setdefault(idx, []).extend((to_px(a.x, a.y), to_px(b.x, b.y)))
    return {idx: QPolygonF(pts) for idx, pts in tmp.items()}


class RouteMapView(QWidget):
    """地图显示与轨迹绘制控件。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 300)
        self.setMouseTracking(True)
        self.setAutoFillBackground(False)

        self._pixmap: QPixmap | None = None
        self._map_digest: str | None = None
        # 地图**身份**(可能由缓存层归并过: 同一张地图的不同分辨率共用一个键),
        # 与字节指纹 _map_digest 分开 —— 后者只用来判断“图变了没有”
        self._map_key: str | None = None
        self._live: RouteRecord | None = None
        # 侧栏里“眼睛点亮的”历史轨迹(可多条): 全部以不透明灰色轨迹画在地图上
        self._visible: list[RouteRecord] = []
        # **高亮层**: 侧栏当前选中的那几条 → 用速度配色画(可多条),
        # 与“眼睛点亮”无关 —— 选了就应该看得到
        self._highlight: list[RouteRecord] = []
        # 全局录制“边录边画”的线路(与单条录制的 _live 同一套画法: 速度配色)
        self._live_multi: list = []
        # 逐条增量累加的分桶几何: route_id -> _LiveGeom(见 _draw_live_multi)
        self._live_seg: dict[str, _LiveGeom] = {}
        self._player: tuple[float, float] | None = None
        # 车头朝向(归一化单位方向向量 dx/dy, 与 /map.img 同坐标系); None = 只画点不画方向
        self._player_dir: tuple[float, float] | None = None
        # 悬停提示(左下角浮层): 命中的轨迹 + 轨迹上的那个点 + 逐行文本
        self._hover_text: str | None = None
        self._hover_rec: RouteRecord | None = None
        self._hover_pt: RoutePoint | None = None
        self._status = "等待 8111 地图数据…"
        # 视图变换: zoom 相对“适配窗口”, pan 为相对视图中心的像素偏移
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._dragging = False
        self._drag_last = QPointF()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        # 回放状态: 非空时进入回放绘制模式(按 playhead 切分进度, 可多条同时播放)
        self._playback_recs: list[RouteRecord] = []
        self._playhead = 0.0
        # 回放选项(侧栏两个勾选框, 默认都开): 阵亡后线路变半透明纯灰 / 阵亡处画红叉
        self._dead_dim = True
        self._dead_mark = True
        # 战区(A/B/C): [{"label": "A", "x": .., "y": .., "side": .., "blink": 0/1/2}]
        self._zones: list[dict] = []
        self._show_zones = True
        # 重生位置: [{"kind": "tank", "x": .., "y": .., "side": "ours"/"enemy"}]
        self._spawns: list[dict] = []
        self._spawns_ver = 0        # 内容版本号(变化 → 轨迹层/方块路径缓存失效)
        self._spawn_key: tuple | None = None
        self._spawn_cache: dict[str, QPainterPath] = {}
        self._show_spawns = True
        # 离屏缓存层: 轨迹/重生方块(半透明)。每帧重画几千个线段太贵(见 _seg_cache 说明),
        # 而这部分内容在“只动玩家点”“战区闪烁”“拖动地图”时并没有变 —— 缓存后每帧只剩两次贴图。
        # (底图不缓存: 缩放贴图本身只要 ~0.5ms, 还省掉了一张窗口大小的位图)
        self._track_layer: QImage | None = None
        self._track_key: tuple | None = None
        self._track_anchor: tuple[float, float] | None = None
        # 建层时的几何指纹与地图尺寸: 缩放/改窗口后据此**按比例贴旧层**(见 _layer_tracks)
        self._track_geom: tuple | None = None
        self._track_size: tuple[float, float] = (0.0, 0.0)
        self._track_margin: tuple[float, float] | None = None
        self._spare_img: QImage | None = None       # 回收的层图像(省掉每次重建 20MB 的分配)
        self._geom_at = 0.0                         # 最后一次几何变化的时刻
        self._geom_seen: tuple | None = None        # 上一帧看到的几何(避免每帧刷新时间戳)
        self._geom_timer: QTimer | None = None
        self._paint_dpr = 0.0                       # 当前画笔设备的缩放比(建层用)
        self._play_layer: QImage | None = None     # 回放累加层(见 _layer_playback)
        self._play_key: tuple | None = None
        self._play_anchor: tuple[float, float] | None = None
        self._play_geom: tuple | None = None
        self._play_size: tuple[float, float] = (0.0, 0.0)
        self._play_margin: tuple[float, float] | None = None
        self._play_drawn: dict[str, int] = {}       # route_id -> 已画进累加层的点数
        # route_id -> 已播放前缀的分桶几何(和实时线路同一个增量缓存类)。
        # 搬图层/重建时要**整条重画一遍**, 没这个缓存就每次都在 Python 里重建
        # 2.9 万段的分桶(实测一次 40 ms, 抵消了搬图层的全部收益)。
        self._play_seg: dict[str, _LiveGeom] = {}
        # 全局录制“边录边画”的累加层(见 _layer_live)
        self._live_layer: QImage | None = None
        self._live_ids: tuple | None = None
        self._live_anchor: tuple[float, float] | None = None
        self._live_size: tuple[float, float] = (0.0, 0.0)
        self._live_seen_size: tuple[float, float] = (0.0, 0.0)
        self._live_margin: tuple[float, float] | None = None
        self._live_zoom_at = 0.0
        self._live_timer: QTimer | None = None
        # 闪烁动画(只在有战区 blink>0 时跑, 不给静止画面白耗 CPU)
        self._blink_t = 0.0
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(BLINK_TICK_MS)
        self._blink_timer.timeout.connect(self._on_blink_tick)
        # 绘制缓存: 分桶几何 / 灰色折线 / 峰值速度 —— **必须缓存**, 否则每帧都要在 Python 里
        # 重新构造几千个 QPointF: 5 条线 3766 个点一次重绘 41ms, 25Hz 刷新直接把界面拖垮
        # (2026-10-08 用户反馈“选 5 条卡得不得了”查到的原因; 分桶几何一项就 30ms)
        self._geom: tuple | None = None            # 当前几何指纹(缩放/平移/尺寸/底图)
        self._seg_cache: dict[str, tuple] = {}     # route_id -> (点数, {桶: QPolygonF})
        self._line_cache: dict[str, tuple] = {}    # route_id -> (点数, QPolygonF)
        self._vmax_cache: dict[str, tuple] = {}    # route_id -> (点数, 峰值速度)
        self._times_cache: dict[str, tuple] = {}   # route_id -> (点数, [t...])
        self._bbox_cache: dict[str, tuple] = {}    # route_id -> (点数, 包围盒) 视野裁剪用

    def _on_blink_tick(self) -> None:
        """推进闪烁相位并重画。"""
        self._blink_t += BLINK_TICK_MS / 1000.0
        self.update()

    def _sync_blink_timer(self) -> None:
        """按当前战区状态开关闪烁定时器。"""
        need = self._show_zones and any(int(z.get("blink") or 0) > 0
                                        for z in self._zones)
        if need and not self._blink_timer.isActive():
            self._blink_t = 0.0
            self._blink_timer.start()
        elif not need and self._blink_timer.isActive():
            self._blink_timer.stop()

    def set_zones(self, zones: list[dict] | None) -> None:
        """设置要绘制的战区(位置为地图归一化坐标)。

        比较包含归属色与 blink: 占领后坐标不变、只有颜色变, 也要刷新。
        """
        new = list(zones or [])
        if new == self._zones:
            return
        self._zones = new
        self._sync_blink_timer()
        self.update()

    def set_show_zones(self, show: bool) -> None:
        """是否把战区画在地图上(菜单开关, 持久化在窗口侧)。"""
        show = bool(show)
        if show == self._show_zones:
            return
        self._show_zones = show
        self._sync_blink_timer()
        self.update()

    @property
    def show_zones(self) -> bool:
        return self._show_zones

    @property
    def zones(self) -> list[dict]:
        return list(self._zones)

    def set_spawns(self, spawns: list[dict] | None) -> None:
        """设置要绘制的**重生位置**(地图归一化坐标)。"""
        new = list(spawns or [])
        if new == self._spawns:
            return
        self._spawns = new
        self._spawns_ver += 1
        self.update()

    def set_show_spawns(self, show: bool) -> None:
        """是否把重生位置画在地图上(菜单开关, 持久化在窗口侧)。"""
        show = bool(show)
        if show == self._show_spawns:
            return
        self._show_spawns = show
        self.update()

    @property
    def show_spawns(self) -> bool:
        return self._show_spawns

    @property
    def spawns(self) -> list[dict]:
        return list(self._spawns)

    # ------------------------------------------------------------------ 数据
    def set_map_bytes(self, data: bytes | None, key: str | None = None) -> bool:
        """更新地图图片; 返回地图是否发生变化(用于判断是否切换了地图)。

        战局外 8111 返回的是固定的 “?” 占位图(见 PLACEHOLDER_MAP_SHA1),
        它不被当作地图绘制, 只显示提示文字; 但同样返回 True(相当于“地图没了/换了”),
        以便上层清理不属于当前地图的轨迹。

        `key` 是**地图身份**: 同一张地图的 1080/2048 分辨率字节不同, 但身份应该一样,
        由缓存层归并后传进来(见 MapCache.save); 不传则用字节哈希。
        """
        if not data:
            if self._pixmap is None:
                self._status = "等待 8111 地图数据…(需进入对局/试车场)"
            return False
        digest = hashlib.sha1(data).hexdigest()
        if digest == self._map_digest:
            return False
        self._map_digest = digest
        self._map_key = key or digest[:12]
        if digest == PLACEHOLDER_MAP_SHA1:
            # 战局外的 “?” 占位图: 不是地图, 不绘制
            self._pixmap = None
            self._map_key = PLACEHOLDER_MAP_KEY
            self._status = "尚未进入对局/试车场(8111 当前只提供占位图)"
            self.reset_view()
            self.update()
            log.info("8111 返回占位图(战局外), 不作为地图显示")
            return True
        pm = QPixmap()
        if not pm.loadFromData(data):
            log.warning("地图图片解码失败 (%d 字节)", len(data))
            return False
        self._pixmap = pm
        self._status = ""
        self.reset_view()   # 换了地图则视图重置(旧缩放/平移对新地图无意义)
        self.update()
        return True

    @property
    def map_digest(self) -> str | None:
        return self._map_digest

    @property
    def map_key(self) -> str | None:
        """当前地图的身份键(与 routes.json 的 map_key / 缓存文件名一致)。"""
        return self._map_key

    @property
    def live_record(self) -> RouteRecord | None:
        return self._live

    @property
    def visible_tracks(self) -> list[RouteRecord]:
        """侧栏中“眼睛点亮”的全部轨迹。"""
        return list(self._visible)

    @property
    def playback_record(self) -> RouteRecord | None:
        """回放中的第一条轨迹(单条时就是它)。"""
        return self._playback_recs[0] if self._playback_recs else None

    @property
    def playback_records(self) -> list[RouteRecord]:
        return list(self._playback_recs)

    def drop_tracks_for_other_map(self, map_key: str,
                                  keep_live: bool = False) -> list[str]:
        """清除不属于 map_key 的轨迹(换图/退出战局时调用)。

        录制中轨迹的 map_key 就是开始录制那一刻的地图, 因此换图后它会被清掉。
        keep_live=True 时保留实时轨迹(正在录制中, 清掉会让轨迹直接消失)。
        返回被清除的项目名(用于提示文案)。
        """
        dropped: list[str] = []
        if not keep_live and self._live is not None and self._live.map_key != map_key:
            self._live = None
            dropped.append("实时轨迹")
        keep = [r for r in self._visible if r.map_key == map_key]
        if len(keep) != len(self._visible):
            self._visible = keep
            dropped.append("显示的轨迹")
        if not keep_live and self._live_multi:
            keep_lm = [r for r in self._live_multi if getattr(r, "map_key", "") == map_key]
            if len(keep_lm) != len(self._live_multi):
                self._live_multi = keep_lm
                dropped.append("实时轨迹")
        if self._playback_recs and any(r.map_key != map_key for r in self._playback_recs):
            self._playback_recs = []
            self._playhead = 0.0
            dropped.append("回放")
        keep_hi = [r for r in self._highlight if r.map_key == map_key]
        if len(keep_hi) != len(self._highlight):
            self._highlight = keep_hi
            dropped.append("高亮")
        if dropped:
            self.update()
        return dropped

    def set_live(self, rec: RouteRecord | None) -> None:
        self._live = rec
        self.update()

    def set_live_multi(self, recs: list | None) -> None:
        """全局录制期间“边录边画”的线路(一次几十条, 都用速度配色)。

        它们**不进缓存层**: 录制时几乎每帧都有新点, 层指纹一直在变, 那样每帧都要把
        全部轨迹重画一遍 —— 实测把 25Hz 轮询拖到 8Hz。改成每帧直接画, 并且分桶几何
        **增量累积**(见 `_draw_live_multi`)。
        """
        self._live_multi = list(recs or [])
        ids = {getattr(r, "route_id", "") for r in self._live_multi}
        for rid in [k for k in self._live_seg if k not in ids]:
            self._live_seg.pop(rid, None)      # 录制已结束/被清掉 → 丢掉它的缓存
        if not self._live_multi:               # 录完了 → 释放累加层(省一张窗口大小的图)
            self._recycle(self._live_layer)
            self._live_layer = None
            self._live_ids = None
            self._live_seg.clear()
        self.update()

    @property
    def live_multi(self) -> list:
        return list(self._live_multi)

    def set_visible_tracks(self, recs: list[RouteRecord] | None) -> None:
        """设置要显示的历史轨迹(可多条, 按给定顺序绘制)。"""
        self._visible = list(recs or [])
        self._drop_hover_if_gone()
        self.update()

    def set_highlight(self, recs: list[RouteRecord] | None) -> None:
        """设置高亮轨迹(侧栏选中的那几条) —— 用速度配色画在最上面。"""
        self._highlight = list(recs or [])
        self._drop_hover_if_gone()
        self.update()

    @property
    def highlight_records(self) -> list[RouteRecord]:
        return list(self._highlight)

    def set_playback(self, recs, playhead: float = 0.0) -> None:
        """进入/退出回放绘制模式(可多条同时回放)。

        传单条记录或列表都可以; 已播放部分按速度着色, 未播放部分半透明,
        每条轨迹各画一个回放光标。

        注意: 回放进度信号**每帧都会调它** —— 因此只有“换了回放对象”才丢弃累加层,
        否则每帧重建累加层就等于每帧重画一遍全部轨迹(白优化)。
        """
        if recs is None:
            new: list[RouteRecord] = []
        elif isinstance(recs, (list, tuple)):
            new = [r for r in recs if r is not None]
        else:
            new = [recs]
        if [r.route_id for r in new] != [r.route_id for r in self._playback_recs]:
            self._recycle(self._play_layer)
            self._play_layer = None      # 换了回放对象 → 累加层作废(下次重画)
            self._play_drawn = {}
            self._play_seg = {}
        self._playback_recs = new
        self._playhead = playhead
        self._drop_hover_if_gone()
        self.update()

    def set_player(self, pos: tuple[float, float] | None,
                   direction: tuple[float, float] | None = None) -> None:
        """更新玩家标记: 位置(归一化坐标) + 车头朝向(归一化单位方向向量, 可为 None)。"""
        self._player = pos
        self._player_dir = direction

    def clear(self) -> None:
        self._live = None
        self._live_multi = []
        self._live_seg.clear()
        self._live_layer = None
        self._live_ids = None
        self._live_size = (0.0, 0.0)
        self._visible = []
        self._highlight = []
        self._player = None
        self._player_dir = None
        self._reset_hover()
        self._playback_recs = []
        self._playhead = 0.0
        self._zones = []
        self._spawns = []
        self._spawns_ver += 1
        self._drop_layers()
        self._seg_cache.clear()
        self._line_cache.clear()
        self._vmax_cache.clear()
        self._times_cache.clear()
        self._bbox_cache.clear()
        self.update()

    # ------------------------------------------------------------------ 几何
    def _fit_scale(self) -> float:
        """zoom=1.0 时的缩放比(等比缩放到刚好放进控件)。"""
        pw, ph = self._pixmap.width(), self._pixmap.height()
        if pw <= 0 or ph <= 0:
            return 0.0
        return min(self.width() / pw, self.height() / ph)

    def _map_size(self) -> tuple[float, float]:
        """当前缩放下的地图显示尺寸(像素)。"""
        if self._pixmap is None:
            return (0.0, 0.0)
        s = self._fit_scale() * self._zoom
        return (self._pixmap.width() * s, self._pixmap.height() * s)

    def _map_rect(self) -> QRectF:
        """地图在控件中的显示矩形(含缩放与平移)。"""
        dw, dh = self._map_size()
        if dw <= 0 or dh <= 0:
            return QRectF()
        cx = self.width() / 2.0 + self._pan_x
        cy = self.height() / 2.0 + self._pan_y
        return QRectF(cx - dw / 2.0, cy - dh / 2.0, dw, dh)

    def _clamp_pan(self) -> None:
        """限制平移幅度, 避免把地图整个拖出视野(至少保留 PAN_MARGIN 像素可见)。"""
        dw, dh = self._map_size()
        if dw <= 0 or dh <= 0:
            return
        max_x = max(0.0, (dw + self.width()) / 2.0 - PAN_MARGIN)
        max_y = max(0.0, (dh + self.height()) / 2.0 - PAN_MARGIN)
        self._pan_x = max(-max_x, min(max_x, self._pan_x))
        self._pan_y = max(-max_y, min(max_y, self._pan_y))

    def reset_view(self) -> None:
        """恢复为适配窗口(双击地图/切换地图时调用)。"""
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self.update()

    def _to_px(self, x: float, y: float) -> QPointF:
        """归一化坐标 → **地图本地像素**(以地图左上角为原点, **不含平移**)。

        故意不含平移: 平移只是“把整张图挪一下”, 用 `QPainter.translate` 平移画笔即可
        —— 这样拖动地图时缓存的分桶几何/折线**完全不用重算**
        (2026-10-08: 以前平移算进几何指纹, 拖一下就要在 Python 里重算 30ms)。
        需要控件坐标时用 `map_pos()`。
        """
        w, h = self._map_size()
        return QPointF(x * w, y * h)

    def map_pos(self, x: float, y: float) -> QPointF:
        """归一化坐标 → **控件像素坐标**(含缩放与平移); 交互命中测试与测试用。"""
        r = self._map_rect()
        q = self._to_px(x, y)
        return QPointF(r.left() + q.x(), r.top() + q.y())

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # 绘制异常不允许拖垮程序(打包版无控制台, 崩溃极难排查)
        try:
            self._paint()
        except Exception:  # noqa: BLE001
            log.exception("地图绘制失败")

    def _paint(self) -> None:
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # 地图图片缩放用**平滑(双线性)**滤波: 放大后边缘不再有台阶/马赛克。
            # 实测开销可忽略(整帧 1.72 → 1.49 ms) —— 目标只有可见区域那一小块。
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            p.fillRect(self.rect(), COLOR_BG)
            r = self._map_rect()
            if self._pixmap is None or r.isEmpty():
                p.setPen(QColor("#64748b"))
                p.setFont(QFont("Microsoft YaHei", 11))
                p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._status)
                return

            # 底图用**控件坐标**直接画(与平移无关, 也保持与旧实现逐像素一致)
            self._draw_base(p)
            # 其余内容都画在“地图本地坐标”里(见 _to_px) → 平移只动画笔, 不改几何缓存
            self._paint_dpr = p.device().devicePixelRatio() or 1.0
            p.translate(r.left(), r.top())
            # 战区会闪烁 → 不进缓存层, 每帧现画(几个菱形, 便宜); 位置在轨迹之下
            self._draw_zones(p)
            if self._playback_recs:
                # 回放: 未播放底衬 + 已播放着色段都在**累加层**里, 每帧只补新段;
                # 标记与光标每帧现画(几个圈, 便宜)
                labels = len(self._playback_recs) <= 1
                got = self._layer_playback()
                if got is not None:
                    layer, pscale = got
                    self._blit_layer(p, layer, self._play_anchor, pscale, self._play_margin)
                    for rec in self._playback_recs:
                        self._draw_play_marks(p, rec, self._playhead, labels=labels)
                        self._draw_dead_mark(p, rec, self._playhead)   # 阵亡的红叉
                        if not self._dead_now(rec, self._playhead):
                            self._draw_play_cursor(p, rec, self._playhead)
                        #   阵亡后没有“当前位置”, 不再画那颗白点(又正好压在 X 上)
                else:
                    for rec in self._playback_recs:
                        self._draw_playback(p, rec, self._playhead, labels=labels)
            else:
                got = self._layer_tracks()
                if got is not None:
                    layer, tscale = got
                    self._blit_layer(p, layer, self._track_anchor, tscale,
                                     self._track_margin)
                else:
                    self._draw_tracks(p)
            # 全局录制“边录边画”: 走累加层(每帧只补新段; 缩放时先缩放贴图)
            if self._live_multi:
                got = self._layer_live()
                if got is not None:
                    layer_live, live_scale = got
                    self._blit_layer(p, layer_live, self._live_anchor, live_scale,
                                     self._live_margin)
            self._draw_player(p)

            if self._hover_text:
                self._draw_hover_mark(p)
            p.translate(-r.left(), -r.top())
            if self._hover_text:
                self._draw_hover(p)               # 左下角浮层用控件坐标
        finally:
            p.end()

    # ------------------------------------------------------------ 绘制缓存层
    def _draw_base(self, p: QPainter) -> None:
        """底图: 地图图片 + 边框(用控件坐标画, 每帧一次 —— 缩放贴图只要零点几毫秒)。

        注意: 它**不使用**“地图本地坐标 + 平移画笔”那套(见 _paint), 因为直接用整张
        地图矩形贴图既最简单, 也与旧实现逐像素一致。
        """
        r = self._map_rect()
        if r.isEmpty():
            return
        p.drawPixmap(r, self._pixmap, QRectF(self._pixmap.rect()))
        p.setPen(QPen(COLOR_GRID, 1))
        p.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))

    def _draw_tracks(self, p: QPainter) -> None:
        """轨迹层内容: 重生位置 + 轨迹(含时间标签)。战区和玩家位置不在这里画。"""
        # 战区/重生位置画在轨迹下面(它们是背景信息, 不该盖住线路)
        if self._show_spawns:
            self._draw_spawns(p)
        # 显示中的轨迹: 灰色普通层 + 高亮层(选中的/正在录的)
        hi = list(self._highlight)
        if self._live is not None and all(r is not self._live for r in hi):
            hi.append(self._live)
        hi_ids = {id(r) for r in hi}
        total = len(self._visible) + len(hi)
        labels = total <= 1      # 多条时时间标签会完全重叠、无法阅读
        for rec in self._visible:
            if id(rec) in hi_ids:
                continue        # 同一条不重复画(下面用速度配色画它)
            self._draw_route(p, rec, history=True, vmax=self._vmax(rec), labels=labels)
        for rec in hi:
            self._draw_route(p, rec, history=False, vmax=self._vmax(rec), labels=labels)

    def _draw_live_segments(self, p: QPainter, only_new: bool) -> None:
        """把全局线路画到画笔 `p` 上。

        `only_new=True` 时只画“这帧新增的段”(增量层用), 否则画全部(建层时用)。
        坐标是归一化的, 靠 `_scaled()` 把画笔缩放成“地图本地像素”。
        """
        if not self._scaled(p):
            return
        s = self._px_scale()
        try:
            for rec in self._live_multi:
                geom = self._live_seg.get(rec.route_id)
                if geom is None:
                    geom = _LiveGeom()
                    self._live_seg[rec.route_id] = geom
                vmax = self._vmax(rec)
                added = geom.feed(rec.points, vmax)
                buckets = added if only_new else geom.polys
                for idx, poly in buckets.items():
                    speed = (idx + 0.5) / _SPEED_BUCKETS * vmax
                    p.setPen(QPen(speed_color(speed, vmax), TRACK_PEN / s,
                                  Qt.PenStyle.SolidLine, SEG_CAP,
                                  Qt.PenJoinStyle.RoundJoin))
                    p.drawLines(QPolygonF(poly) if only_new else poly)
        finally:
            p.restore()

    def _draw_live_layer(self, lp: QPainter) -> None:
        """建层时: 把全部已有段画一遍(之后每帧只补新段)。"""
        self._draw_live_segments(lp, only_new=False)

    def _build_live_layer(self, ids: tuple) -> QImage | None:
        """从头重建实时层(缩放停下后 / 录制重开时)。"""
        self._live_ids = ids
        self._live_size = self._map_size()
        self._live_anchor = (self._map_rect().left(), self._map_rect().top())
        self._live_margin = self._margin()
        self._live_layer = self._make_layer(self._draw_live_layer, self._live_anchor)
        return self._live_layer

    def _shift_live_layer(self) -> bool:
        """拖出余量时**搬**实时层而不是重画; 不划算返回 False。

        顺便修了一个隐患: 以前这里根本不看锚点, 录制中拖动地图会让已经录出来的
        实时线路**跟着错位**(层还是贴在旧位置)。2026-10-21。
        """
        layer, anchor = self._live_layer, self._live_anchor
        if layer is None or anchor is None:
            return False
        got = self._shift_layer(layer, anchor,
                                self._live_margin or self._margin(),
                                self._draw_live_layer)
        if got is None:
            return False
        self._recycle(layer)
        self._live_layer, self._live_anchor = got
        return True

    def _schedule_live_rebuild(self) -> None:
        """缩放过程中不重建(一次重建实测 270 ms), 停手 200ms 后再重建。

        注意**不能在已经计时时再 start()** —— 录制时每 40ms 就 paint 一次,
        重新计时会让它永远不触发, 于是实时线路一直卡在“缩放后的旧图”上不再长
        (2026-10-21 用户实测: 缩放+拖动后不再实时显示).
        """
        if self._live_timer is None:
            self._live_timer = QTimer(self)
            self._live_timer.setSingleShot(True)
            self._live_timer.timeout.connect(self._rebuild_live_now)
        if not self._live_timer.isActive():
            self._live_timer.start(200)

    def _rebuild_live_now(self) -> None:
        if not self._live_multi:
            return
        if time.monotonic() - self._live_zoom_at < 0.25:
            # 还在缩放/改窗口 → 再等一轮, 别每 200ms 重建一次(每次都是 200ms 的卡)
            self._live_timer.start(200)
            return
        self._live_layer = None
        self._live_ids = None
        self.update()

    def _layer_live(self) -> tuple[QImage, tuple[float, float]] | None:
        """全局录制的**累加层**: 返回 (图层, 缩放比), 由调用方负责贴图。

        三条实测过的坑(别改回去):

        1. 放进静态轨迹层 → 每帧都有新点 → 层指纹每帧变 → 每帧把全部轨迹重画一遍,
           25Hz 轮询被拖到 8Hz;
        2. 每帧直接画全部线路 → **240 ms/帧**(4.8 万个抗锯齿线段);
        3. 因此做成累加层: 每帧只补“这 0.04 秒新增的几段”, 稳态 **3.5 ms/帧**。

        缩放时几何全变, 重建一次要 **270 ms**(40 条线 × 1200 点) → 缩放过程中先按比例
        把旧图层贴上去(立即有反馈), 停手 200ms 后再重建。
        """
        ids = tuple(getattr(r, "route_id", "") for r in self._live_multi)
        size = self._map_size()
        if self._live_layer is None or self._live_ids != ids:
            layer = self._build_live_layer(ids)
            return (layer, (1.0, 1.0)) if layer is not None else None
        if size != self._live_size and self._live_size[0] > 0 and self._live_size[1] > 0:
            # 缩放/改窗口 → 旧图层按比例贴(即时反馈), 稍后重建
            if size != self._live_seen_size:
                self._live_seen_size = size
                self._live_zoom_at = time.monotonic()   # 记下“最后一次几何变化”
            self._schedule_live_rebuild()
            return (self._live_layer,
                    (size[0] / self._live_size[0], size[1] / self._live_size[1]))
        # 拖出定位余量 → 把旧内容搬一格(与轨迹层/回放层同一套, 见 _shift_layer)
        if self._layer_stale(self._live_anchor, (1.0, 1.0)):
            if self._shift_live_layer():
                return self._live_layer, (1.0, 1.0)
            self._live_layer = None
            self._live_ids = None
            layer = self._build_live_layer(ids)
            return (layer, (1.0, 1.0)) if layer is not None else None
        # 稳态: 只补新段
        # 但速度基准(=最高速)涨了的话, 桶索引/颜色全都变 → 整个层重建
        for rec in self._live_multi:
            geom = self._live_seg.get(rec.route_id)
            if geom is None or geom.vmax <= 0.0:
                continue
            vmax = self._vmax(rec)
            if abs(vmax - geom.vmax) > VMAX_REBUILD_RATIO * geom.vmax:
                self._live_layer = None
                self._live_ids = None
                layer = self._build_live_layer(ids)
                return (layer, (1.0, 1.0)) if layer is not None else None
        mx, my = self._margin()
        p = QPainter(self._live_layer)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.translate(self._live_anchor[0] + mx, self._live_anchor[1] + my)
            self._draw_live_segments(p, only_new=True)
        except Exception:  # noqa: BLE001
            log.exception("补画全局线路失败")
        finally:
            p.end()
        return self._live_layer, (1.0, 1.0)

    def _margin(self) -> tuple[float, float]:
        """缓存层四周的余量(像素): 拖动地图时靠偏移贴图复用, 不必重建。"""
        return (self.width() * LAYER_MARGIN, self.height() * LAYER_MARGIN)

    def _recycle(self, img: QImage | None) -> None:
        """回收不再显示的层图像: 下次建层直接复用(省掉 20MB 级的分配 + 页错误)。"""
        if img is not None and not img.isNull() and self._spare_img is None:
            self._spare_img = img

    def _take_spare(self, w: int, h: int, dpr: float) -> QImage | None:
        """取一张尺寸正好合适的回收图像(尺寸不同就丢掉, 不为此额外占内存)。"""
        img, self._spare_img = self._spare_img, None
        if img is None or img.isNull() or img.width() != w or img.height() != h:
            return None
        img.setDevicePixelRatio(dpr)
        return img

    def _make_layer(self, draw, anchor: tuple[float, float]) -> QImage | None:
        """把一次绘制结果渲进离屏图像(拿不到就返回 None, 上层退回直接绘制)。

        * 用 **QImage** 而不是 QPixmap: 同样是 ARGB32_Premultiplied, 但往 QPixmap
          上画在本机要慢一倍(实测 5 条线 21ms vs 12ms)。
        * 图像比控件大一圈(`_margin`), 且用 `anchor`(=当时地图矩形左上角)定位 ——
          之后平移范围在余量内就能直接**偏移贴图**, 一帧都不重画。
        """
        mx, my = self._margin()
        dpr = self._paint_dpr or 1.0     # 与画笔设备一致 → 贴图时 1:1, 不会被重采样
        w = int(round((self.width() + 2 * mx) * dpr))
        h = int(round((self.height() + 2 * my) * dpr))
        img = self._take_spare(w, h, dpr)
        if img is None:
            img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
            if img.isNull():
                return None
            img.setDevicePixelRatio(dpr)
        img.fill(Qt.GlobalColor.transparent)
        lp = QPainter(img)
        try:
            lp.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            lp.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            # 层覆盖“控件坐标 [-余量, 尺寸+余量]”这一块: 地图本地坐标 + 锚点 + 余量 = 层像素
            lp.translate(anchor[0] + mx, anchor[1] + my)
            draw(lp)
        except Exception:  # noqa: BLE001
            log.exception("绘制缓存层失败")
            return None
        finally:
            lp.end()
        return img

    def _blit_layer(self, p: QPainter, layer: QImage, anchor: tuple[float, float],
                    scale: tuple[float, float] = (1.0, 1.0),
                    margin: tuple[float, float] | None = None) -> None:
        """把缓存层贴到当前视图(画笔已平移到地图本地坐标)。

        显式给出目标矩形: 层图带 devicePixelRatio(高 DPI 屏上 1.5/2.0),
        用 `drawImage(QPointF, img)` 不一定会按逻辑尺寸贴(实测 150% 缩放下会偏)。

        `scale` 是“缩放过程中先用旧层顶着”的比例(见 `_layer_tracks`): 图层里的
        归一化内容必须落在 **q×scale** 处, 所以目标矩形要按 `scale` 缩放 ——
        不能简单 `p.scale()` 后再贴(那样会多出 `(余量+锚点)×(scale-1)` 的偏移,
        1920 宽下实测能偏几十像素)。

        `margin` 必须是**建层时**的余量(窗口改大小后 `_margin()` 会变), 否则偏移也错。
        """
        mx, my = margin if margin is not None else self._margin()
        dpr = layer.devicePixelRatio() or 1.0
        sx, sy = scale
        p.drawImage(QRectF((-mx - anchor[0]) * sx, (-my - anchor[1]) * sy,
                           layer.width() / dpr * sx, layer.height() / dpr * sy),
                    layer, QRectF(0, 0, layer.width(), layer.height()))

    def _shift_layer(self, layer: QImage, anchor: tuple[float, float],
                     margin: tuple[float, float],
                     draw) -> tuple[QImage, tuple[float, float]] | None:
        """把缓存层的内容整体搬一格, 只重画新露出来的那条边 —— 不整层重画。

        拖动地图时图层锚点会越漂越远, 以前一越出余量就整层重画(64 条线 29630 段
        实测 **79 ms**, 用户感觉就是“同时播放 AB 两队时拖一下卡一下”)。改成三步:

        1. 新锚点直接取**当前地图矩形**(= 建层时的位置), 于是只有一侧那条 ~余量宽
           的边要重画(约占整层 20%);
        2. 旧图内容按整数像素偏移贴进一张同样大小的新图(整层一次 `drawImage`,
           省掉全部矢量栅格化);
        3. 用 `setClipRect` 只把新露出来的那条边重画一遍(所有绘制函数都是幂等的)。

        锚点必须挪到“偏移 0”而不是“刚好贴住余量边界”: 贴边界的话再拖一格就又越界,
        40 px 一步的拖动会变成**每帧搬一次图**(实测 60 帧搬 20 次, 比整层重建还慢)。

        位移取整到**设备像素**(小数会让整层被重采样糊掉), 代价是图层与底图最多
        差半像素。要重画的面积超过 `SHIFT_MAX_EXPOSED` 时返回 None —— 说明一次跳了
        很远, 让调用方整层重建。

        返回 (新图层, 新锚点)。
        """
        dpr = layer.devicePixelRatio() or 1.0
        dw, dh = layer.width(), layer.height()
        if dw <= 0 or dh <= 0:
            return None
        lw, lh = dw / dpr, dh / dpr
        r = self._map_rect()
        mx, my = margin

        def step(cur: float, anc: float, lim: float) -> float:
            """锚点该跟着走多少。

            直接把锚点挪到**当前地图矩形**上(= 建层时的位置, 偏移 0), 而不是
            “刚好贴住余量边界”: 贴着边界的话再拖一格就又越界, 40 px 一步的拖动会
            变成**每帧搬一次图**(实测 60 帧里搬 20 次, 比整层重建还慢)。
            """
            d = cur - anc
            return d if abs(d) > lim else 0.0

        dx = round(step(r.left(), anchor[0], mx) * dpr) / dpr
        dy = round(step(r.top(), anchor[1], my) * dpr) / dpr
        if dx == 0.0 and dy == 0.0:
            return None
        sx, sy = int(round(dx * dpr)), int(round(dy * dpr))     # 设备像素(整数)
        new_anchor = (anchor[0] + dx, anchor[1] + dy)
        # 旧内容搬走后, 新图上没内容的区域(左右两条 + 上下两条, 实际最多各一条)
        strips: list[QRectF] = []
        if sx > 0:
            strips.append(QRectF(0.0, 0.0, sx / dpr, lh))
        elif sx < 0:
            strips.append(QRectF(lw + sx / dpr, 0.0, -sx / dpr, lh))
        x0, x1 = max(0, min(dw, sx)), max(0, min(dw, sx + dw))
        if x1 > x0:
            if sy > 0:
                strips.append(QRectF(x0 / dpr, 0.0, (x1 - x0) / dpr, sy / dpr))
            elif sy < 0:
                strips.append(QRectF(x0 / dpr, lh + sy / dpr,
                                     (x1 - x0) / dpr, -sy / dpr))
        if sum(s.width() * s.height() for s in strips) > SHIFT_MAX_EXPOSED * lw * lh:
            return None                     # 露出来的太多 → 整层重建更划算
        img = self._take_spare(dw, dh, dpr)
        if img is None:
            img = QImage(dw, dh, QImage.Format.Format_ARGB32_Premultiplied)
            if img.isNull():
                return None
            img.setDevicePixelRatio(dpr)
        img.fill(Qt.GlobalColor.transparent)
        lp = QPainter(img)
        try:
            lp.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            lp.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            lp.drawImage(QPointF(dx, dy), layer)            # 整数偏移 → 1:1 拷贝
            for strip in strips:
                lp.save()
                lp.setClipRect(strip)
                lp.translate(new_anchor[0] + mx, new_anchor[1] + my)
                draw(lp)
                lp.restore()
        except Exception:  # noqa: BLE001
            log.exception("平移缓存层失败")
            return None
        finally:
            lp.end()
        return img, new_anchor

    def _layer_stale(self, anchor: tuple[float, float] | None,
                     scale: tuple[float, float] = (1.0, 1.0)) -> bool:
        """层的定位锚点离当前位置太远(超出余量)→ 必须重新渲染。

        按比例贴图时**覆盖范围也会按比例变大**, 所以允许的偏移量要乘上缩放比 ——
        否则放大过程中锚点一漂就重建(一次 20~50ms), 滚轮又变成一顿一顿的。
        """
        if anchor is None:
            return True
        mx, my = self._margin()
        mx *= max(1.0, scale[0])
        my *= max(1.0, scale[1])
        r = self._map_rect()
        return (abs(r.left() - anchor[0]) > mx or abs(r.top() - anchor[1]) > my)

    def _tracks_key(self) -> tuple:
        """轨迹层的内容指纹(点数/标记数/回放进度/重生方块都在内)。

        **不含几何**(缩放/窗口尺寸)与平移 —— 几何变化走“先缩放贴旧层”那条路
        (见 `_layer_tracks`), 只有内容真的变了才值得在 Python 里重画一遍。
        """
        live = self._live

        def sig(recs) -> tuple:
            return tuple((r.route_id, len(r.points), len(r.marks)) for r in recs)

        return (sig(self._visible), sig(self._highlight),
                (live.route_id, len(live.points), len(live.marks)) if live else None,
                sig(self._playback_recs),
                round(self._playhead, 3) if self._playback_recs else None,
                self._show_spawns, self._spawns_ver)

    def _build_track_layer(self) -> None:
        """(重)建轨迹层, 并记下建层时的几何/尺寸(之后缩放要按比例贴)。"""
        self._track_anchor = (self._map_rect().left(), self._map_rect().top())
        self._track_margin = self._margin()
        self._recycle(self._track_layer)
        self._track_layer = self._make_layer(self._draw_tracks, self._track_anchor)
        self._track_key = self._tracks_key()
        self._track_geom = self._geom_key()
        self._track_size = self._map_size()

    def _shift_track_layer(self) -> bool:
        """拖出余量时**搬**轨迹层而不是重画; 不划算返回 False(调用方整层重建)。"""
        layer, anchor = self._track_layer, self._track_anchor
        if layer is None or anchor is None:
            return False
        got = self._shift_layer(layer, anchor,
                                self._track_margin or self._margin(), self._draw_tracks)
        if got is None:
            return False
        self._recycle(layer)
        self._track_layer, self._track_anchor = got
        return True

    def _layer_tracks(self) -> tuple[QImage, tuple[float, float]] | None:
        """轨迹层(半透明, 带余量): 返回 (图层, 缩放比)。

        三种情况:

        1. 没有层 / **内容变了**(眼睛开关、选中、重生位置…) → 立刻重建;
        2. 拖出余量 → 先把旧内容**搬**一格并只补新露出来的窄边(`_shift_layer`),
           搬不动(一眼跳很远)才整层重建;
        3. 只是**几何变了**(滚轮缩放、改窗口大小) → 先把旧层按比例贴上顶住
           (1~2ms), 停手 200ms 后再重建一次变清晰。

        第 3 条是 2026-10-21 的卡顿修正: 重建一次实测 20~50ms(1920×1040, 20 条线
        1.7 万点), 而滚轮连发 6 格就要重建 6 次 → 一段滚轮要 250~500ms, 手感就是
        “缩放卡”。旧层按比例贴的画质会临时略糊/线宽略变, 停手 200ms 后自动复原。
        第 2 条是 2026-10-21 晚的拖动卡顿修正: 64 条线整层重建 79ms, 而“搬一格”
        只有越界的那几像素要重画。
        """
        key = self._tracks_key()
        geom = self._geom_key()
        size = self._map_size()
        scale = self._scale_vs(self._track_size, size)
        stale = self._layer_stale(self._track_anchor, scale)
        if self._track_layer is None or self._track_key != key or stale:
            # 只有“锚点漂了”这一条时, 搬图层就够(内容与几何都没变, 且是 1:1 贴)
            if not (stale and self._track_layer is not None and self._track_key == key
                    and scale == (1.0, 1.0) and geom == self._track_geom
                    and self._shift_track_layer()):
                self._build_track_layer()
            if self._track_layer is None:
                return None
            return (self._track_layer, (1.0, 1.0))
        if geom != self._track_geom:
            self._note_geom_change(geom)
            return (self._track_layer, scale)
        return (self._track_layer, (1.0, 1.0))

    @staticmethod
    def _scale_vs(built: tuple[float, float],
                  now: tuple[float, float]) -> tuple[float, float]:
        """当前地图尺寸 / 建层时的地图尺寸(无法判断时当作 1)。"""
        if built[0] <= 0 or built[1] <= 0 or now[0] <= 0 or now[1] <= 0:
            return (1.0, 1.0)
        return (now[0] / built[0], now[1] / built[1])

    # ------------------------------------------- 缩放/改窗口后的延迟重建
    def _note_geom_change(self, geom: tuple) -> None:
        """记下“几何这次真的变了”, 并安排停手后重建。

        注意这里必须**只在几何再一次变化时**才刷新时间戳: `_layer_tracks` /
        `_layer_playback` 是“当前几何 != 建层时几何”就调进来, 而重建之前这个条件
        **每帧都成立** —— 若每帧都刷新时间戳, `_geom_settle` 就会一直以为“刚刚还在
        缩放”而无限推迟, 画面便永远卡在按比例贴的旧图上(用户实测: 播放全局线路时
        着色段画到一半就不再增长, 重新缩放一下才又出现)。2026-10-21。
        """
        if geom == self._geom_seen:
            return                          # 几何没再变 → 不要刷新时间戳
        self._geom_seen = geom
        self._geom_at = time.monotonic()
        if self._geom_timer is None:
            self._geom_timer = QTimer(self)
            self._geom_timer.setSingleShot(True)
            self._geom_timer.timeout.connect(self._geom_settle)
        if not self._geom_timer.isActive():
            self._geom_timer.start(GEOM_SETTLE_MS)

    def _geom_settle(self) -> None:
        """停手 GEOM_SETTLE_MS 之后: 丢掉按比例顶着的旧层, 下次 paint 重建(变清晰)。"""
        if time.monotonic() - self._geom_at < GEOM_SETTLE_MS / 1000.0:
            self._geom_timer.start(GEOM_SETTLE_MS)     # 还在缩放 → 再等一轮
            return
        geom = self._geom_key()
        dirty = False
        if self._track_layer is not None and self._track_geom != geom:
            self._recycle(self._track_layer)
            self._track_layer = None
            self._track_key = None
            dirty = True
        if self._play_layer is not None and self._play_geom != geom:
            self._recycle(self._play_layer)
            self._play_layer = None
            self._play_key = None
            dirty = True
        if dirty:
            self.update()

    def _drop_layers(self) -> None:
        self._recycle(self._track_layer)
        self._track_layer = None
        self._track_key = None
        self._track_anchor = None
        self._track_geom = None
        self._track_margin = None
        self._recycle(self._play_layer)
        self._play_layer = None
        self._play_key = None
        self._play_anchor = None
        self._play_geom = None
        self._play_margin = None
        self._play_drawn = {}
        self._play_seg = {}

    def _play_layer_key(self) -> tuple:
        """回放累加层的内容指纹(**不含几何**: 几何变化走“先缩放贴旧层”)。

        `dead` 里是**这一刻已经阵亡的线路** —— 越过阵亡时刻时整条线要从“速度配色”
        换成“半透明纯灰”(或者直接清除, 看“变暗”开关), 而累加层里已经画上去的彩色
        像素无法擦掉, 所以让**阵亡**(不是“阵亡且要变暗”)进指纹: 每个玩家只会在
        阵亡那一刻触发一次重建(与回拖重建同量级)。
        """
        return (tuple(r0.route_id for r0 in self._playback_recs),
                self._show_spawns, self._spawns_ver,
                tuple(r.route_id for r in self._playback_recs
                      if self._dead_now(r, self._playhead)))

    def _draw_play_static(self, p: QPainter) -> None:
        """回放累加层的初始内容: 重生位置 + 已播放前缀(+ 阵亡后要变暗的整条灰)。

        2026-10-22 改动(用户要求): 回放**不再画未播放的全长底衬** —— 以前一按播放
        就看得到所有线路的整条灰影。现在:

        * 未阵亡: 只画已播放的彩色前缀(未播放部分完全不可见);
        * 已阵亡且勾了“被击毁后路线变暗”: 整条画成半透明纯灰;
        * 已阵亡但没勾: 什么都不画(等于直接清除)。
        """
        if self._show_spawns:
            self._draw_spawns(p)
        for rec in self._playback_recs:
            if not self._dead_now(rec, self._playhead):
                k, _mid = self._played_prefix(rec, self._playhead)
                self._draw_play_prefix(p, rec, k)
            elif self._dead_dim:
                self._draw_play_underlay(p, rec, COLOR_DEAD_ROUTE)

    def _play_prefix_geom(self, rec, k: int) -> _LiveGeom:
        """该线路“已播放前缀”的分桶几何(增量缓存, 供重复绘制用)。"""
        geom = self._play_seg.get(rec.route_id)
        if geom is None:
            geom = _LiveGeom()
            self._play_seg[rec.route_id] = geom
        pts = rec.points
        geom.feed(pts if k >= len(pts) else pts[:k], self._vmax(rec))
        return geom

    def _draw_play_prefix(self, p: QPainter, rec, k: int) -> None:
        """把前 k 个点的“已播放”着色段画上去(**用缓存的分桶几何**)。

        与 `_extend_play` 的区别: 那个只补 [done, k) 的增量(逐帧快), 这个画全部
        已播放段(重建/搬图层时要整条重画)。两者共用 `_play_seg` 里的增量缓存,
        所以重画一条 64 线路的回放层不再需要每次在 Python 里重新分桶。
        """
        if k < 2 or not self._scaled(p):
            return
        geom = self._play_prefix_geom(rec, k)
        vmax = self._vmax(rec)
        s = self._px_scale()
        try:
            for idx, poly in geom.polys.items():
                speed = (idx + 0.5) / _SPEED_BUCKETS * vmax
                p.setPen(QPen(speed_color(speed, vmax), PLAY_PEN / s,
                              Qt.PenStyle.SolidLine, SEG_CAP,
                              Qt.PenJoinStyle.RoundJoin))
                p.drawLines(poly)
        finally:
            p.restore()

    def _rebuild_play_layer(self, key: tuple, geom: tuple,
                            size: tuple[float, float]) -> None:
        """从头重建回放累加层(换回放对象 / 往回拖拽 / 重建不划算时)。"""
        self._play_drawn = {}
        self._play_anchor = (self._map_rect().left(), self._map_rect().top())
        self._play_margin = self._margin()
        self._recycle(self._play_layer)
        self._play_layer = self._make_layer(self._draw_play_static, self._play_anchor)
        self._play_key = key
        self._play_geom = geom
        self._play_size = size
        if self._play_layer is not None:
            for rec in self._playback_recs:          # 记下已经画到哪
                self._play_drawn[rec.route_id], _ = self._played_prefix(rec, self._playhead)

    def _shift_play_layer(self) -> bool:
        """拖出余量时**搬**累加层而不是重画; 不划算返回 False。

        已画进去的进度(`_play_drawn`)不受影响 —— 搬的是像素, 数据空间没有变,
        新露出来的窄边按**当前**进度补画(差几像素宽, 看不出来)。
        """
        layer, anchor = self._play_layer, self._play_anchor
        if layer is None or anchor is None:
            return False
        got = self._shift_layer(layer, anchor,
                                self._play_margin or self._margin(),
                                self._draw_play_static)
        if got is None:
            return False
        self._recycle(layer)
        self._play_layer, self._play_anchor = got
        return True

    def _layer_playback(self) -> tuple[QImage, tuple[float, float]] | None:
        """回放**累加层**(半透明): 未播放底衬 + 已播放的着色段。返回 (图层, 缩放比)。

        回放时进度每帧都在推进, 而“已播放”是只增不减的前缀 —— 于是把画好的部分
        **留在图像里**, 每帧只补新播放到的几段。否则每帧都要重画全部已播放线段
        (5 条线要 35ms/帧, 30fps 下必卡)。
        往回拖拽 / 定位超出余量 / 换回放对象 → 重建;
        缩放/改窗口 → 先按比例贴旧层, 停手 200ms 再重建(同 `_layer_tracks`)。
        """
        key = self._play_layer_key()
        geom = self._geom_key()
        size = self._map_size()
        scale = self._scale_vs(self._play_size, size)
        back = any(self._played_prefix(r, self._playhead)[0]
                   < self._play_drawn.get(r.route_id, 0) for r in self._playback_recs)
        stale = self._layer_stale(self._play_anchor, scale)
        broken = self._play_layer is None or self._play_key != key or back
        if broken or stale:
            # 只有“锚点漂了”这一条时, 搬图层就够(拖动时不再整层重画, 见 _shift_layer)
            if not (stale and not broken and scale == (1.0, 1.0)
                    and geom == self._play_geom and self._shift_play_layer()):
                self._rebuild_play_layer(key, geom, size)
            if self._play_layer is None:
                return None
        elif geom != self._play_geom:
            # 只是几何变了 → 旧层按比例顶着(播放中滚轮缩放不再一顿一顿)
            self._note_geom_change(geom)
            return (self._play_layer, scale)
        mx, my = self._margin()
        p = QPainter(self._play_layer)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.translate(self._play_anchor[0] + mx, self._play_anchor[1] + my)
            for rec in self._playback_recs:
                if self._dead_now(rec, self._playhead):
                    continue            # 已阵亡: 整条线在重建时已按“灰/不画”处理完毕
                k, _mid = self._played_prefix(rec, self._playhead)
                done = self._play_drawn.get(rec.route_id, 0)
                if k > done:
                    self._extend_play(p, rec, done, k)
                    self._play_drawn[rec.route_id] = k
        finally:
            p.end()
        return (self._play_layer, (1.0, 1.0))

    def _geom_key(self) -> tuple:
        """**像素坐标**的几何指纹: 缩放/窗口尺寸/底图变了, 缓存像素的东西就失效。

        只有需要真实像素的东西靠它(战区/重生方块的路径);
        轨迹的分桶几何是**归一化**的, 不靠它 —— 缩放拖动都不用重算。
        不含平移: 平移只动画笔(`QPainter.translate`)。
        """
        return (round(self._zoom, 6), self.width(), self.height(), self._map_digest)

    def _px_scale(self) -> float:
        """归一化坐标 → 像素的缩放比 = 地图在屏幕上的边长(宽高均值, 用于线宽补偿)。"""
        w, h = self._map_size()
        return (w + h) / 2.0

    def _scaled(self, p: QPainter) -> bool:
        """进入“归一化坐标 × 地图边长”的坐标系(用于画轨迹折线)。

        返回 False 表示地图尺寸无效(调用方直接跳过)。调用方负责 `p.restore()`。
        """
        w, h = self._map_size()
        if w <= 0 or h <= 0:
            return False
        p.save()
        p.scale(w, h)
        return True

    def _vmax(self, rec: RouteRecord) -> float:
        """该轨迹的峰值速度(速度配色的基准), 按记录 + 点数缓存。

        单条轨迹与全局线路**用同一个规则**(每条线自己的最高速), 所以两边
        “同样的速度 → 同样的颜色”。全局线路边录边长, 基准上涨时调用方会重建图层。
        """
        n = len(rec.points)
        got = self._vmax_cache.get(rec.route_id)
        if got is not None and got[0] == n:
            return got[1]
        if got is not None and got[0] < n:
            # 增量: 只看新来的点(单条录制时点数也在涨)
            value = max(got[1], max((p.speed for p in rec.points[got[0]:]), default=0.0))
            self._vmax_cache[rec.route_id] = (n, value)
            return value
        value = max(max((pt.speed for pt in rec.points), default=0.0), 1.0)
        self._vmax_cache[rec.route_id] = (n, value)
        return value

    def _speed_segments(self, rec: RouteRecord, vmax: float) -> dict[int, QPolygonF]:
        """按速度分桶的轨迹几何(**归一化坐标**, 缓存; 见 `build_speed_segments`)。

        归一化的好处: 缩放 / 改窗口大小 / 拖动都**不用重算几何** —— 画的时候
        用 `p.scale(s, s)` 缩放画笔、并把线宽设为 `宽度 / s` 即可
        (否则每次滚轮缩放都要在 Python 里重算 3766 个点)。
        """
        n = len(rec.points)
        got = self._seg_cache.get(rec.route_id)
        if got is None or got[0] != n:
            got = (n, build_speed_segments(rec.points, vmax, _normalized))
            self._seg_cache[rec.route_id] = got
        return got[1]

    def _px_line(self, rec: RouteRecord) -> QPolygonF:
        """整条轨迹的归一化折线(同样缓存): 灰色历史轨迹与回放未播放段共用。

        注意: 只取几何, 颜色/线宽由调用方自己设 —— 回放里它是半透明淡色的底衬。
        """
        n = len(rec.points)
        got = self._line_cache.get(rec.route_id)
        if got is None or got[0] != n:
            got = (n, QPolygonF([QPointF(pt.x, pt.y) for pt in rec.points]))
            self._line_cache[rec.route_id] = got
        return got[1]

    # 旧名(灰色历史轨迹就是这条折线)
    _gray_line = _px_line

    def _times(self, rec: RouteRecord) -> list[float]:
        """轨迹点的时间列表(缓存; 回放每帧要用它二分找已播放前缀)。"""
        n = len(rec.points)
        got = self._times_cache.get(rec.route_id)
        if got is None or got[0] != n:
            got = (n, [pt.t for pt in rec.points])
            self._times_cache[rec.route_id] = got
        return got[1]

    def _bbox(self, rec: RouteRecord) -> tuple[float, float, float, float]:
        """轨迹的归一化包围盒 (x0, y0, x1, y1), 按点数缓存。"""
        n = len(rec.points)
        got = self._bbox_cache.get(rec.route_id)
        if got is not None and got[0] == n:
            return got[1]
        pts = rec.points
        if pts:
            box = (min(p.x for p in pts), min(p.y for p in pts),
                   max(p.x for p in pts), max(p.y for p in pts))
        else:
            box = (0.0, 0.0, 0.0, 0.0)
        self._bbox_cache[rec.route_id] = (n, box)
        return box

    def _in_view(self, rec: RouteRecord) -> bool:
        """这条轨迹是否与当前可见区域相交(**视野裁剪**)。

        放大后地图上大部分线路都在视野外, 但 QPainter 自己的 clip 并不会“提前拒绝”
        —— 实测 zoom 1x/8x 重建一层的耗时几乎一样(22~47ms)。于是这里先用包围盒
        判一下: 看不见的线一条段也不用栅格化(2026-10-21)。
        """
        w, h = self._map_size()
        if w <= 0 or h <= 0:
            return True
        r = self._map_rect()
        pad = 8.0 / w, 8.0 / h            # 留点余量(线宽/圆头)
        x0, y0, x1, y1 = self._bbox(rec)
        vx0 = (-r.left()) / w - pad[0]
        vy0 = (-r.top()) / h - pad[1]
        vx1 = (self.width() - r.left()) / w + pad[0]
        vy1 = (self.height() - r.top()) / h + pad[1]
        return not (x1 < vx0 or x0 > vx1 or y1 < vy0 or y0 > vy1)

    def _played_prefix(self, rec: RouteRecord, head: float
                       ) -> tuple[int, RoutePoint | None]:
        """已播放的**稳定前缀**点数 k 与插值点 mid(回放每帧调用)。

        k = `t <= head` 的采样点个数; mid = head 时刻的插值位置(每帧都在动,
        所以它那一段每帧现画, 前 k 个点的分桶几何则进增量缓存)。
        """
        k = bisect_right(self._times(rec), head)
        return k, point_at(rec.points, head)

    def _extend_play(self, p: QPainter, rec: RouteRecord, done: int, k: int) -> None:
        """把 [done, k) 这些**已播放**的段按速度配色画上去(回放每帧只补新段)。"""
        pts = rec.points
        vmax = self._vmax(rec)
        buckets: dict[int, list[QPointF]] = {}
        for i in range(max(1, done), k):
            a, b = pts[i - 1], pts[i]
            idx = bucket_index((a.speed + b.speed) / 2.0, vmax)
            buckets.setdefault(idx, []).extend((QPointF(a.x, a.y), QPointF(b.x, b.y)))
        if not buckets or not self._scaled(p):
            return
        s = self._px_scale()
        try:
            for idx, pts_px in buckets.items():
                speed = (idx + 0.5) / _SPEED_BUCKETS * vmax
                p.setPen(QPen(speed_color(speed, vmax), PLAY_PEN / s,
                              Qt.PenStyle.SolidLine, SEG_CAP,
                              Qt.PenJoinStyle.RoundJoin))
                p.drawLines(QPolygonF(pts_px))
        finally:
            p.restore()

    def _draw_play_underlay(self, p: QPainter, rec: RouteRecord,
                            color: QColor | None = None) -> None:
        """未播放部分的底衬: 整条轨迹的半透明描边。

        画的是**整条**(缓存的一条折线, 每帧/每次只贴一次): 已播放部分紧接着会用
        不透明的速度配色盖在上面, 效果与“只画未播放段”一致, 但省掉了每帧构造
        未播放段多边形的开销。

        `color` 不为空时用它 —— 阵亡线路整条改成半透明纯灰走的就是这里。
        """
        if len(rec.points) < 2 or not self._scaled(p):
            return
        try:
            p.setPen(QPen(color or COLOR_UNPLAYED, 2.0 / self._px_scale()))
            p.drawPolyline(self._px_line(rec))
        finally:
            p.restore()

    @staticmethod
    def _is_dead(rec) -> bool:
        """这条线是不是“单位被击毁”结束的(见 global_record 的判定口径)。

        单个轨迹(RouteRecord)没有这个字段 → `getattr` 兜底成 False。
        """
        return bool(getattr(rec, "dead", False)) and bool(rec.points)

    def _dead_now(self, rec, head: float) -> bool:
        """回放到此刻为止, 这条线的玩家是否已经阵亡(已过掉线时刻)。"""
        return self._is_dead(rec) and head >= rec.points[-1].t

    def _dead_dimmed(self, rec, head: float) -> bool:
        """阵亡后整条线路要不要变半透明纯灰(开关 `被击毁后路线变暗`)。"""
        return self._dead_dim and self._dead_now(rec, head)

    def set_dead_style(self, dim: bool | None = None, mark: bool | None = None) -> None:
        """回放选项: 阵亡后线路变暗 / 阵亡位置画红叉(两个开关都默认开)。"""
        changed = False
        if dim is not None and bool(dim) != self._dead_dim:
            self._dead_dim = bool(dim)
            changed = True
        if mark is not None:
            self._dead_mark = bool(mark)
        if changed:
            # “变暗”会用另一套颜色画整条线 → 累加层里的旧像素没用了
            self._recycle(self._play_layer)
            self._play_layer = None
            self._play_key = None
            # 关掉“变暗”后阵亡线路已经不在画面上 → 它身上的悬停浮层也要清掉
            self._drop_hover_if_gone()
        self.update()

    @property
    def dead_dim(self) -> bool:
        return self._dead_dim

    @property
    def dead_mark(self) -> bool:
        return self._dead_mark

    def _dead_mark_color(self, rec) -> QColor:
        """阵亡标记的颜色: A 队蓝 / B 队红(普通轨迹没有队号 → 红)。"""
        return COLOR_DEAD_MARK_A if getattr(rec, "side", "") == "A" else COLOR_DEAD_MARK_B

    def _draw_dead_mark(self, p: QPainter, rec, head: float) -> None:
        """阵亡位置的叉: 画在线路末端(最后一个采样点), 大小与回放光标白点相当。

        颜色按队区分(A 队蓝 / B 队红, 见 `_dead_mark_color`);
        只在“回放已经越过阵亡时刻”后才出现 —— 否则等于提前剧透。
        """
        if not self._dead_mark or not self._dead_now(rec, head):
            return
        last = rec.points[-1]
        c = self._to_px(last.x, last.y)
        h = DEAD_MARK_HALF
        p.setPen(QPen(self._dead_mark_color(rec), DEAD_MARK_PEN, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.FlatCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(c.x() - h, c.y() - h), QPointF(c.x() + h, c.y() + h))
        p.drawLine(QPointF(c.x() - h, c.y() + h), QPointF(c.x() + h, c.y() - h))

    def _draw_play_marks(self, p: QPainter, rec: RouteRecord, head: float,
                         labels: bool = True) -> None:
        """回放的标记: 已到达的正常显示, 未到达的淡化(每帧现画, 只有几个圈)。"""
        for mk in rec.marks:
            color = QColor({"start": COLOR_START, "stop": COLOR_STOP}.get(mk.kind, COLOR_MARK))
            if mk.t > head:
                color.setAlpha(80)
            radius = 8.0 if mk.kind in ("start", "stop") else 6.0
            center = self._to_px(mk.x, mk.y)
            p.setPen(QPen(color, 2.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(center, radius, radius)
            if mk.kind == "start":
                p.setBrush(QBrush(color))
                p.drawEllipse(center, 2.5, 2.5)
            if labels:
                self._draw_time_label(p, center, fmt_time(mk.t), color)

    def _draw_play_cursor(self, p: QPainter, rec: RouteRecord, head: float) -> None:
        """回放光标: **只有一个实心白点**(位置由时间插值得到, 移动才平滑)。

        以前是“一个环套一个小实心圆”, 多条轨迹一起回放时十几个环挤在一起太乱
        (2026-10-21 用户要求) —— 现在只保留实心圆, 朝向仍用那根白线表示。
        """
        cur = point_at(rec.points, head)
        if cur is None:
            return
        c = self._to_px(cur.x, cur.y)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor("#f8fafc")))
        p.drawEllipse(c, PLAY_CURSOR_R, PLAY_CURSOR_R)
        # 录制时存过朝向就一起画出来(旧轨迹的点没有朝向 → 只画那个点)
        self._draw_cursor_dir(p, c, cur.dx, cur.dy)

    def _draw_playback(self, p: QPainter, rec: RouteRecord, head: float,
                       labels: bool = True) -> None:
        """回放绘制(**兜底路径**, 缓存层建不出来时才会走到这里)。

        多条同时回放时: 每条各自按自己的 playhead 切分(时间轴相同),
        因此较短的那条会自然地“跑完停在终点”。
        """
        if self._dead_now(rec, head):
            if self._dead_dim:                      # 勾了“变暗”才留整条灰
                self._draw_play_underlay(p, rec, COLOR_DEAD_ROUTE)
        else:
            k, _mid = self._played_prefix(rec, head)
            self._extend_play(p, rec, 0, k)
        self._draw_play_marks(p, rec, head, labels=labels)
        self._draw_dead_mark(p, rec, head)
        if not self._dead_now(rec, head):
            self._draw_play_cursor(p, rec, head)

    def _draw_route(self, p: QPainter, rec: RouteRecord, history: bool, vmax: float,
                    labels: bool = True) -> None:
        pts = rec.points
        if len(pts) >= 2 and self._in_view(rec) and self._scaled(p):
            s = self._px_scale()
            try:
                if history:
                    # 未选中的历史轨迹: 不透明灰蓝(半透明只用于回放中未播放的段)
                    p.setPen(QPen(COLOR_HISTORY, 2.0 / s,
                                  Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                  Qt.PenJoinStyle.RoundJoin))
                    p.drawPolyline(self._px_line(rec))
                else:
                    # 按速度分桶着色; 每段独立绘制(见 build_speed_segments 说明)
                    for idx, poly in self._speed_segments(rec, vmax).items():
                        speed = (idx + 0.5) / _SPEED_BUCKETS * vmax
                        p.setPen(QPen(speed_color(speed, vmax), TRACK_PEN / s,
                                      Qt.PenStyle.SolidLine, SEG_CAP,
                                      Qt.PenJoinStyle.RoundJoin))
                        p.drawLines(poly)
            finally:
                p.restore()

        for mk in rec.marks:
            # 起点圈对**所有**轨迹都要画: 以前只画实时轨迹, 导致同一张图上
            # “眼睛点亮”的轨迹没有起点圈(看起来像其中一个起点丢了)
            color = {"start": COLOR_START, "stop": COLOR_STOP}.get(mk.kind, COLOR_MARK)
            radius = 8.0 if mk.kind in ("start", "stop") else 6.0
            center = self._to_px(mk.x, mk.y)
            p.setPen(QPen(color, 2.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(center, radius, radius)
            if mk.kind == "start":
                p.setBrush(QBrush(color))
                p.drawEllipse(center, 2.5, 2.5)
            if labels:
                self._draw_time_label(p, center, fmt_time(mk.t), color)

    def _draw_time_label(self, p: QPainter, center: QPointF, text: str, color: QColor) -> None:
        """在圆旁绘制时间数字(白字 + 深色描边, 保证在地图上可读)。"""
        font = QFont("Consolas", 10)
        font.setBold(True)
        p.setFont(font)
        fm = QFontMetrics(font)
        tw, th = fm.horizontalAdvance(text), fm.height()
        x, y = center.x() + 11, center.y() - th / 2.0
        box = QRectF(x - 3, y, tw + 6, th)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(15, 23, 42, 200))
        p.drawRoundedRect(box, 3, 3)
        p.setPen(QPen(color, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(box, 3, 3)
        p.setPen(QPen(QColor("#f8fafc")))
        # 注意: QRect 只接受 int, 坐标必须用 QRectF(否则运行期 TypeError)
        p.drawText(QRectF(x, y, tw, th),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)

    def _draw_spawns(self, p: QPainter) -> None:
        """画重生位置: **空心方块** —— 红 = 敌方, 蓝 = 我方(与战区同一套归属色)。

        8111 里一批 `respawn_base_tank` 就是游戏“重生区域”里那一堆候选位置(实测
        一局 40~64 个), 所以是一个位置一个小方块 —— 连成一片方块就是那个区域。
        方块内部**不填充**: 免得盖住底图与轨迹。

        一局有 128 个方块: 逐个 `drawRect` 要 ~3ms(每帧), 因此按归属色**合成
        一个 QPainterPath** 一次描出来(垫底深色描边也合成一条)。
        """
        if not self._show_spawns or not self._spawns:
            return
        dark = QColor(15, 23, 42, 200)      # 深色垫底描边(浅色底图上也能看清)
        paths = self._spawn_paths()
        if not paths:
            return
        p.setBrush(Qt.BrushStyle.NoBrush)      # 空心
        p.setPen(QPen(dark, SPAWN_PEN + 1.4))
        for path in paths.values():
            p.drawPath(path)
        for side, path in paths.items():
            color = SPAWN_SIDE_COLORS.get(side, SPAWN_SIDE_COLORS["neutral"])
            p.setPen(QPen(color, SPAWN_PEN))
            p.drawPath(path)

    def _spawn_paths(self) -> dict[str, QPainterPath]:
        """重生方块按归属色分堆的路径 —— 缓存(只跟缩放/尺寸/方块内容有关)。

        一局 128 个方块: 逐个 drawRect 要 ~3ms, 合成路径后每帧只剩两次 drawPath;
        再缓存住路径, 拖动/闪烁时连合成都不用做。
        """
        key = (self._geom_key(), self._spawns_ver)
        if self._spawn_key == key:
            return self._spawn_cache
        paths: dict[str, QPainterPath] = {}    # 按归属分堆(QColor 不可作字典键)
        for s in self._spawns:
            try:
                x, y = float(s["x"]), float(s["y"])
            except (KeyError, TypeError, ValueError):
                continue
            side = str(s.get("side") or "neutral")
            c = self._to_px(x, y)
            path = paths.setdefault(side, QPainterPath())
            path.addRect(QRectF(c.x() - SPAWN_HALF, c.y() - SPAWN_HALF,
                                SPAWN_HALF * 2, SPAWN_HALF * 2))
        self._spawn_key = key
        self._spawn_cache = paths
        return paths

    def _draw_zones(self, p: QPainter) -> None:
        """画战区: 小菱形 + 中心字母(A/B/C, 由 8111 的 zone_label 提供)。

        形状用菱形而不是圆圈: 图上圆圈已经表示起点/标记/终点, 菱形不会混淆。
        颜色按归属: 蓝 = 我方, 红 = 敌方, 白 = 中立(见 zones.SIDE_*)。
        被占领中(blink)时在本阵营色里脉冲: 变暗 ↔ 纯色(蓝闪蓝、红闪红, 不掺黄)。
        """
        if not self._show_zones or not self._zones:
            return
        font = QFont("Consolas", 9)
        font.setBold(True)
        for z in self._zones:
            try:
                x, y = float(z["x"]), float(z["y"])
            except (KeyError, TypeError, ValueError):
                continue
            side = str(z.get("side") or "neutral")
            fill = QColor(ZONE_SIDE_COLORS.get(side, ZONE_SIDE_COLORS["neutral"]))
            label_color = QColor("#0f172a") if side == "neutral" else QColor("#ffffff")
            blink = int(z.get("blink") or 0)
            if blink > 0:
                # 官方: bv = exp(-(5*t - 2)^4), t 在一个周期内 0→period
                period = BLINK_PERIODS.get(blink, BLINK_PERIODS[1])
                t = self._blink_t % period
                bv = math.exp(-((5.0 * t - 2.0) ** 4))
                # 谷底 = 变暗的阵营色, 峰值 = 纯阵营色(色相始终不变)
                k = BLINK_DIM + (1.0 - BLINK_DIM) * bv
                fill = _blend(ZONE_DARK, (fill.red(), fill.green(), fill.blue()), k)
            c = self._to_px(x, y)
            r = ZONE_RADIUS
            diamond = QPolygonF([
                QPointF(c.x(), c.y() - r), QPointF(c.x() + r, c.y()),
                QPointF(c.x(), c.y() + r), QPointF(c.x() - r, c.y()),
            ])
            p.setBrush(QBrush(fill))
            p.setPen(QPen(QColor(15, 23, 42, 210), 3.4))   # 深色描边(浅底图上也能看清)
            p.drawPolygon(diamond)
            p.setPen(QPen(COLOR_ZONE, 1.6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPolygon(diamond)
            label = str(z.get("label") or "").strip()
            if label:
                p.setFont(font)
                p.setPen(QPen(label_color))
                p.drawText(QRectF(c.x() - r, c.y() - r, r * 2, r * 2),
                           int(Qt.AlignmentFlag.AlignCenter), label[:2])

    def _draw_player(self, p: QPainter) -> None:
        if self._player is None:
            return
        c = self._to_px(*self._player)
        p.setPen(QPen(QColor(56, 189, 248, 90), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(c, PLAYER_RING_R, PLAYER_RING_R)
        p.setPen(QPen(COLOR_PLAYER, 2))
        p.setBrush(QBrush(COLOR_PLAYER))
        p.drawEllipse(c, PLAYER_DOT_R, PLAYER_DOT_R)
        if self._player_dir is not None:
            self._draw_player_dir(p, c, self._player_dir[0], self._player_dir[1])

    def _dir_angle(self, dx: float, dy: float) -> float | None:
        """朝向的像素角度; 地图尺寸未就绪或向量全零时返回 None。

        dx/dy 是 map_obj.json 给的**归一化单位方向向量**(与 /map.img 同坐标系)——
        地图显示尺寸可能不是正方形(等比缩放后的 w/h 不等), 所以按 w/h 换算成像素
        方向再求角, 否则朝向会被拉歪。
        """
        w, h = self._map_size()
        if w <= 0.0 or h <= 0.0:
            return None
        if dx == 0.0 and dy == 0.0:
            return None            # 全零 = 无有效朝向(旧轨迹 / 没取到)
        return math.atan2(dy * h, dx * w)

    def _draw_player_dir(self, p: QPainter, c: QPointF, dx: float, dy: float) -> None:
        """玩家标记上的**蓝色 ^**(车头朝向); 形状见 PLAYER_DIR_* 常量。"""
        ang = self._dir_angle(dx, dy)
        if ang is None:
            return
        tip, foot1, foot2 = player_dir_points(c, ang)
        # 先描深色底再画亮色: 深/浅底图上都看得清(与 _draw_hover_mark 同一套办法)
        for color, width in ((QColor(15, 23, 42, 220), 4.0), (COLOR_PLAYER, 2.0)):
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(color, width, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawLine(tip, foot1)
            p.drawLine(tip, foot2)

    def _draw_cursor_dir(self, p: QPainter, c: QPointF, dx: float, dy: float) -> None:
        """回放光标**圆点内部**的朝向短线(黑色)。

        用户 2026-10-22: 原来是“白点 + 伸出圆点外的白色带黑边线”, 看着像小蝌蚪;
        现在把点放大一圈(半径 4), 朝向改成点在内部的一条黑色短线。
        用**平头**端帽: 圆头会在起点端婳进圆心, 把白点中心也染黑。
        """
        ang = self._dir_angle(dx, dy)
        if ang is None:
            return
        start, tip = cursor_dir_points(c, ang)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(COLOR_CURSOR_DIR, CURSOR_DIR_PEN,
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawLine(start, tip)

    def _draw_hover_mark(self, p: QPainter) -> None:
        """在命中的那个轨迹点上画一圈白环。

        多条轨迹叠在一起时, 只靠文字看不出“读的是哪条线” —— 白环指出具体位置。
        """
        if self._hover_pt is None or self._hover_rec is None:
            return
        if not any(r is self._hover_rec for r in self._drawn_records()):
            return                                   # 命中的线已被隐藏/删除
        c = self._to_px(self._hover_pt.x, self._hover_pt.y)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(15, 23, 42, 200), 4.0))
        p.drawEllipse(c, 5.0, 5.0)
        p.setPen(QPen(QColor("#f8fafc"), 1.8))
        p.drawEllipse(c, 5.0, 5.0)

    def _draw_hover(self, p: QPainter) -> None:
        """左下角悬停浮层(多行文本, 宽度超出时贴右边)。"""
        font = QFont("Microsoft YaHei", 9)
        p.setFont(font)
        fm = QFontMetrics(font)
        lines = [ln for ln in (self._hover_text or "").split("\n")]
        if not lines or not any(lines):
            return
        th = fm.height()
        tw = max(fm.horizontalAdvance(ln) for ln in lines)
        box_w = min(float(tw + 14), max(80.0, float(self.width() - 20)))
        box_h = float(th * len(lines) + 8)
        x, y = 10.0, float(self.height()) - box_h - 12.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(15, 23, 42, 215))
        p.drawRoundedRect(QRectF(x, y, box_w, box_h), 4, 4)
        p.setPen(QPen(QColor("#e2e8f0")))
        for i, ln in enumerate(lines):
            p.drawText(QRect(int(x) + 7, int(y) + 4 + i * th, int(box_w) - 14, th),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, ln)

    # ------------------------------------------------------------------ 交互
    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """滚轮缩放: 以鼠标位置为锚点(该点的地图坐标保持不动)。"""
        if self._pixmap is None:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = WHEEL_STEP if delta > 0 else 1.0 / WHEEL_STEP
        new_zoom = max(MIN_ZOOM, min(MAX_ZOOM, self._zoom * factor))
        if abs(new_zoom - self._zoom) < 1e-6:
            return
        pos = event.position()
        r = self._map_rect()
        # 鼠标处的归一化地图坐标(缩放前后保持一致)
        nx = (pos.x() - r.left()) / r.width()
        ny = (pos.y() - r.top()) / r.height()
        self._zoom = new_zoom
        r2 = self._map_rect()
        # 把该点重新对齐到鼠标下 → 修正平移
        self._pan_x += pos.x() - (r2.left() + nx * r2.width())
        self._pan_y += pos.y() - (r2.top() + ny * r2.height())
        self._clamp_pan()
        self.update()
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.MiddleButton and self._pixmap is not None:
            self._dragging = True
            self._drag_last = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.MiddleButton and self._dragging:
            self._dragging = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """双击复位为适配窗口。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.reset_view()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._dragging:
            pos = event.position()
            self._pan_x += pos.x() - self._drag_last.x()
            self._pan_y += pos.y() - self._drag_last.y()
            self._drag_last = pos
            self._clamp_pan()
            self.update()
            return
        r = self._map_rect()
        if r.isEmpty() or not self._hover_candidates():
            self._set_hover(None, None)
            return
        hit = self._hit_test(event.position().x(), event.position().y())
        self._set_hover(*(hit or (None, None)))

    # ------------------------------------------------------------------ 悬停
    def _drawn_records(self) -> list[RouteRecord]:
        """当前**真的画在地图上**的轨迹 —— 悬停只该命中看得见的线。

        回放中画的是被回放的那几条; 否则是“眼睛点亮的” + 高亮层 + 正在录的那条。
        """
        if self._playback_recs:
            return list(self._playback_recs)
        out: list[RouteRecord] = []
        seen: set[int] = set()
        for rec in [*self._visible, *self._highlight, self._live]:
            if rec is None or id(rec) in seen:
                continue
            seen.add(id(rec))
            out.append(rec)
        return out

    def _hover_candidates(self) -> list[RouteRecord]:
        """**此刻真的画在地图上**的轨迹 —— 悬停只该命中看得见的线。

        2026-10-22: 回放中阵亡的线路如果没勾“被击毁后路线变暗”, 它其实已经被
        清除不画了(见 `_draw_play_static`), 自然也不该还能被悬停选中; 勾了变暗
        则整条灰线仍在地上, 可以悬停查看。
        """
        out: list[RouteRecord] = []
        for rec in self._drawn_records():
            if not rec.points:
                continue
            if (self._playback_recs and not self._dead_dim
                    and self._dead_now(rec, self._playhead)):
                continue            # 已阵亡且选择“不留灰” → 画面上没有它
            out.append(rec)
        return out

    def _hit_test(self, mx: float, my: float
                  ) -> tuple[RouteRecord, RoutePoint] | None:
        """在**所有显示中的轨迹**里找离鼠标最近的采样点(半径见 HOVER_RADIUS)。

        以前只看实时轨迹 / 第一条可见轨迹 —— 于是只剩“刚录完那条”有提示,
        其它眼睛点亮的线路移上去没反应(2026-10-07 修)。

        **两级搜索**: 只做单级抽样(`n // 400` 个点)在超长线路上会变稀 ——
        一局 24 分钟 = 25Hz × 1440s = 36000 个点, step = 90, 相邻点间距 0.26px 时
        抽样间距已 ≈ 24px **超过命中半径 18px**, 会出现“鼠标在线上却没提示”以及
        “提示的点差半米”(用户 2026-10-22 提出)。所以先用粗点定位, 再在命中粗点
        前后各 step 个点内精查一遍 —— 结果与全量扫描一致(实测误差 0.000000px),
        计算量仍只有 400 + 2·step。

        另外这里直接用 `pt.x * w`, 不再逐点调 `_to_px()`: 后者每次都会重算一遍
        地图尺寸并在 C++ 里构造 QPointF(64 条回放实测 62ms/次 → 现在几毫秒级)。
        """
        r = self._map_rect()
        w, h = self._map_size()
        if w <= 0 or h <= 0:
            return None
        mx, my = mx - r.left(), my - r.top()
        best: tuple[RouteRecord, RoutePoint] | None = None
        best_d2 = HOVER_RADIUS ** 2
        for rec in self._hover_candidates():
            pts = rec.points
            n = len(pts)
            if self._playback_recs:
                # 回放中只允许命中**已播放**的点(玩家还没走到的路段不该能被选中)
                n = min(n, self._played_prefix(rec, self._playhead)[0])
            if n <= 0:
                continue
            step = 1 if n <= 400 else n // 400
            # 粗扫(step == 1 时就是全量)
            k = -1
            k_d2 = best_d2
            for i in range(0, n, step):
                pt = pts[i]
                d2 = (pt.x * w - mx) ** 2 + (pt.y * h - my) ** 2
                if d2 < k_d2:
                    k_d2, k = d2, i
            if k < 0:
                continue
            if step > 1:                      # 邻域精查: 粗点前后各 step 个点
                for i in range(max(0, k - step), min(n, k + step + 1)):
                    pt = pts[i]
                    d2 = (pt.x * w - mx) ** 2 + (pt.y * h - my) ** 2
                    if d2 < k_d2:
                        k_d2, k = d2, i
            if k_d2 < best_d2:
                best_d2, best = k_d2, (rec, pts[k])
        return best

    def _hover_text_for(self, rec: RouteRecord, pt: RoutePoint) -> str:
        """悬停浮层文本。

        第一行 = **这是哪条线路**(录制时刻 + 车型 + 用时; 与侧栏条目一一对应),
        第二行 = 鼠标所在位置的用时与速度(毫秒精度, 便于对比微小时差)。
        """
        if rec is self._live:
            head = f"录制中 | 车型 {getattr(rec, 'vehicle', '?')}"
        else:
            created = getattr(rec, "created_at", "") or ""
            ts = created[11:16] if len(created) >= 16 else ""
            head = " | ".join(x for x in (ts, f"车型 {getattr(rec, 'vehicle', '?')}",
                                          f"用时 {fmt_time(rec.duration)}") if x)
        return (f"{head}\n"
                f"时间 {fmt_time_ms(pt.t)} | 速度 {fmt_speed(pt.speed)} km/h")

    def _set_hover(self, rec: RouteRecord | None, pt: RoutePoint | None) -> None:
        """更新悬停状态(文本 + 命中点), 有变化才重画。"""
        text = None if rec is None or pt is None else self._hover_text_for(rec, pt)
        if (text == self._hover_text and rec is self._hover_rec
                and pt is self._hover_pt):
            return
        self._hover_text = text
        self._hover_rec = rec
        self._hover_pt = pt
        self.update()

    def _reset_hover(self) -> None:
        self._hover_text = None
        self._hover_rec = None
        self._hover_pt = None

    def _drop_hover_if_gone(self) -> None:
        """命中的那条线被隐藏/换掉、或回放头回退到命中点之前时, 悬停提示不该还留着。

        * 用 `_hover_candidates()` 而不是 `_drawn_records()`: 回放中阵亡且没勾“变暗”的
          线路已经从画面上清除, 停在它上面的浮层也得跟着消失(用户 2026-10-22);
        * 回放只能命中已播放的点 → 往回拖之后那个点可能又变成“未行进”, 也要清掉。
        """
        if self._hover_rec is None:
            return
        alive = any(r is self._hover_rec for r in self._hover_candidates())
        if alive and self._playback_recs and self._hover_pt is not None:
            alive = self._hover_pt.t <= self._playhead
        if not alive:
            self._reset_hover()

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._set_hover(None, None)
