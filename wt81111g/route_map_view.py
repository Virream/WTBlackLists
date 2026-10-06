"""地图画布: 显示 8111 地图图片, 绘制玩家位置、轨迹、标记与时间标注。

坐标约定: 8111 `/map_obj.json` 的 x/y 为 0~1 归一化值,
本控件把地图按原始宽高比等比缩放居中(letterbox), 再映射为像素。
"""
from __future__ import annotations

import hashlib
import logging
import math

from PyQt6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap, QPolygonF,
)
from PyQt6.QtWidgets import QWidget

from .route_store import (
    PLACEHOLDER_MAP_SHA1, RoutePoint, RouteRecord, fmt_speed, fmt_time, fmt_time_ms,
    point_at, split_points,
)
from .zones import BLINK_COLOR, BLINK_PERIODS

log = logging.getLogger("route_map_view")

# 起点/标记/终点配色
COLOR_START = QColor("#22c55e")   # 绿
COLOR_MARK = QColor("#facc15")    # 黄
COLOR_STOP = QColor("#ef4444")    # 红
# 选中的历史轨迹: **不透明**(便于多条叠在一起对比)
COLOR_HISTORY = QColor("#8fa6c8")
# 回放时**尚未播放**的轨迹段: 半透明淡色 —— 半透明只用在这里
COLOR_UNPLAYED = QColor(140, 160, 200, 120)
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
# 被占领中的战区闪烁: 与官方 8111 页面一致(在归属色 ↔ 纯黄之间脉冲)
BLINK_TICK_MS = 40

_SPEED_BUCKETS = 24  # 轨迹按速度量化的颜色档数(减少画笔切换)

# 缩放/平移: zoom=1.0 即“适配窗口”(等比缩放居中), 与未启用缩放时行为一致
MIN_ZOOM = 1.0
MAX_ZOOM = 20.0
WHEEL_STEP = 1.15        # 每格滚轮的缩放倍率
PAN_MARGIN = 40.0        # 平移时地图至少需留在视野内的像素


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


def build_speed_segments(points: list[RoutePoint], vmax: float,
                         to_px) -> dict[int, QPolygonF]:
    """把轨迹按"线段中点速度"分桶, 每段独立存为"一对点"。

    关键: 同一桶内的多段**不一定相邻**(速度波动时 A→B、C→D 同桶而 B→C 不同桶),
    因此必须用 `QPainter.drawLines(polygon)`(按点对解释为独立线段)绘制。
    若错用 `drawPolyline`, 它会把不相邻的段首尾相连, 地图上就会出现**分叉的斜线**。
    """
    buckets: dict[int, QPolygonF] = {}
    for i in range(1, len(points)):
        a, b = points[i - 1], points[i]
        idx = bucket_index((a.speed + b.speed) / 2.0, vmax)
        poly = buckets.setdefault(idx, QPolygonF())
        poly.append(to_px(a.x, a.y))
        poly.append(to_px(b.x, b.y))
    return buckets


class RouteMapView(QWidget):
    """地图显示与轨迹绘制控件。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 300)
        self.setMouseTracking(True)
        self.setAutoFillBackground(False)

        self._pixmap: QPixmap | None = None
        self._map_digest: str | None = None
        self._live: RouteRecord | None = None
        # 侧栏里“眼睛点亮的”历史轨迹(可多条): 全部以不透明灰色轨迹画在地图上
        self._visible: list[RouteRecord] = []
        self._player: tuple[float, float] | None = None
        self._hover_text: str | None = None
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
        # 战区(A/B/C): [{"label": "A", "x": .., "y": .., "side": .., "blink": 0/1/2}]
        self._zones: list[dict] = []
        self._show_zones = True
        # 闪烁动画(只在有战区 blink>0 时跑, 不给静止画面白耗 CPU)
        self._blink_t = 0.0
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(BLINK_TICK_MS)
        self._blink_timer.timeout.connect(self._on_blink_tick)

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

    # ------------------------------------------------------------------ 数据
    def set_map_bytes(self, data: bytes | None) -> bool:
        """更新地图图片; 返回地图是否发生变化(用于判断是否切换了地图)。

        战局外 8111 返回的是固定的 “?” 占位图(见 PLACEHOLDER_MAP_SHA1),
        它不被当作地图绘制, 只显示提示文字; 但同样返回 True(相当于“地图没了/换了”),
        以便上层清理不属于当前地图的轨迹。
        """
        if not data:
            if self._pixmap is None:
                self._status = "等待 8111 地图数据…(需进入对局/试车场)"
            return False
        digest = hashlib.sha1(data).hexdigest()
        if digest == self._map_digest:
            return False
        self._map_digest = digest
        if digest == PLACEHOLDER_MAP_SHA1:
            # 战局外的 “?” 占位图: 不是地图, 不绘制
            self._pixmap = None
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
        """当前地图的键(图片 sha1 前 12 位, 与 routes.json 的 map_key 一致)。"""
        return self._map_digest[:12] if self._map_digest else None

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
        if self._playback_recs and any(r.map_key != map_key for r in self._playback_recs):
            self._playback_recs = []
            self._playhead = 0.0
            dropped.append("回放")
        if dropped:
            self.update()
        return dropped

    def set_live(self, rec: RouteRecord | None) -> None:
        self._live = rec

    def set_visible_tracks(self, recs: list[RouteRecord] | None) -> None:
        """设置要显示的历史轨迹(可多条, 按给定顺序绘制)。"""
        self._visible = list(recs or [])
        self.update()

    def set_playback(self, recs, playhead: float = 0.0) -> None:
        """进入/退出回放绘制模式(可多条同时回放)。

        传单条记录或列表都可以; 已播放部分按速度着色, 未播放部分半透明,
        每条轨迹各画一个回放光标。
        """
        if recs is None:
            self._playback_recs = []
        elif isinstance(recs, (list, tuple)):
            self._playback_recs = [r for r in recs if r is not None]
        else:
            self._playback_recs = [recs]
        self._playhead = playhead
        self.update()

    def set_player(self, pos: tuple[float, float] | None) -> None:
        self._player = pos

    def clear(self) -> None:
        self._live = None
        self._visible = []
        self._player = None
        self._hover_text = None
        self._playback_recs = []
        self._playhead = 0.0
        self._zones = []
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
        r = self._map_rect()
        return QPointF(r.left() + x * r.width(), r.top() + y * r.height())

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
            p.fillRect(self.rect(), COLOR_BG)
            r = self._map_rect()
            if self._pixmap is None or r.isEmpty():
                p.setPen(QColor("#64748b"))
                p.setFont(QFont("Microsoft YaHei", 11))
                p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._status)
                return

            p.drawPixmap(r, self._pixmap, QRectF(self._pixmap.rect()))
            p.setPen(QPen(COLOR_GRID, 1))
            p.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))

            # 战区画在轨迹下面(它是背景信息, 不该盖住线路)
            self._draw_zones(p)

            if self._playback_recs:
                # 多条同时回放时不画时间标签(会完全重叠、无法阅读)
                labels = len(self._playback_recs) <= 1
                for rec in self._playback_recs:
                    self._draw_playback(p, rec, self._playhead, labels=labels)
            else:
                # 显示中的轨迹: 眼睛点亮的 + 刚录制的那条(live, 用速度配色)
                total = len(self._visible) + (0 if self._live is None else 1)
                labels = total <= 1      # 多条时时间标签会完全重叠、无法阅读
                for rec in self._visible:
                    if rec is self._live:
                        continue        # 同一条不重复画(下面用速度配色画它)
                    self._draw_route(p, rec, history=True, vmax=self._vmax(rec),
                                     labels=labels)
                if self._live is not None:
                    self._draw_route(p, self._live, history=False,
                                     vmax=self._vmax(self._live), labels=labels)
            self._draw_player(p)

            if self._hover_text:
                self._draw_hover(p)
        finally:
            p.end()

    @staticmethod
    def _vmax(rec: RouteRecord) -> float:
        peak = max((pt.speed for pt in rec.points), default=0.0)
        return max(peak, 1.0)

    def _draw_route(self, p: QPainter, rec: RouteRecord, history: bool, vmax: float,
                    labels: bool = True) -> None:
        pts = rec.points
        if len(pts) >= 2:
            if history:
                # 选中的历史轨迹: 不透明(半透明只用于回放中未播放的段)
                p.setPen(QPen(COLOR_HISTORY, 2.0,
                              Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                              Qt.PenJoinStyle.RoundJoin))
                path = QPolygonF([self._to_px(pt.x, pt.y) for pt in pts])
                p.drawPolyline(path)
            else:
                # 按速度分桶着色; 每段独立绘制(见 build_speed_segments 说明)
                for idx, poly in build_speed_segments(pts, vmax, self._to_px).items():
                    speed = (idx + 0.5) / _SPEED_BUCKETS * vmax
                    p.setPen(QPen(speed_color(speed, vmax), 2.4,
                                  Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                  Qt.PenJoinStyle.RoundJoin))
                    p.drawLines(poly)

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

    def _draw_playback(self, p: QPainter, rec: RouteRecord, head: float,
                       labels: bool = True) -> None:
        """回放绘制: 已播放部分按速度着色, 未播放部分半透明, 并画回放光标。

        多条同时回放时: 每条各自按自己的 playhead 切分(时间轴相同),
        因此较短的那条会自然地“跑完停在终点”。
        """
        vmax = self._vmax(rec)
        played, rest = split_points(rec.points, head)

        # 未播放部分: 半透明淡色(**唯一**使用半透明的地方), 便于一眼看出进度
        if len(rest) >= 2:
            p.setPen(QPen(COLOR_UNPLAYED, 2.0))
            p.drawPolyline(QPolygonF([self._to_px(pt.x, pt.y) for pt in rest]))

        # 已播放部分: 按速度着色(与录制时一致)
        if len(played) >= 2:
            for idx, poly in build_speed_segments(played, vmax, self._to_px).items():
                speed = (idx + 0.5) / _SPEED_BUCKETS * vmax
                p.setPen(QPen(speed_color(speed, vmax), 2.6,
                              Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                              Qt.PenJoinStyle.RoundJoin))
                p.drawLines(poly)

        # 标记: 已到达的正常显示, 未到达的淡化
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

        # 回放光标: 白色圆环 + 中心点(位置由时间插值得到, 移动才平滑)
        cur = point_at(rec.points, head)
        if cur is not None:
            c = self._to_px(cur.x, cur.y)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(15, 23, 42, 180), 4.5))
            p.drawEllipse(c, 8.0, 8.0)
            p.setPen(QPen(QColor("#f8fafc"), 2.2))
            p.drawEllipse(c, 8.0, 8.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor("#f8fafc")))
            p.drawEllipse(c, 3.0, 3.0)

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

    def _draw_zones(self, p: QPainter) -> None:
        """画战区: 小菱形 + 中心字母(A/B/C, 由 8111 的 zone_label 提供)。

        形状用菱形而不是圆圈: 图上圆圈已经表示起点/标记/终点, 菱形不会混淆。
        颜色按归属: 蓝 = 我方, 红 = 敌方, 白 = 中立(见 zones.SIDE_*)。
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
                # 正在被占领: 与官方页面一致 —— 在归属色↔纯黄之间脉冲
                # (官方: bv = exp(-(5*t - 2)^4), t 在一个周期内 0→period)
                period = BLINK_PERIODS.get(blink, BLINK_PERIODS[1])
                t = self._blink_t % period
                bv = math.exp(-((5.0 * t - 2.0) ** 4))
                r, g, b = BLINK_COLOR
                fill = QColor(int(fill.red() + (r - fill.red()) * bv),
                              int(fill.green() + (g - fill.green()) * bv),
                              int(fill.blue() + (b - fill.blue()) * bv))
                label_color = QColor("#0f172a")
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
        p.drawEllipse(c, 12, 12)
        p.setPen(QPen(COLOR_PLAYER, 2))
        p.setBrush(QBrush(COLOR_PLAYER))
        p.drawEllipse(c, 4, 4)

    def _draw_hover(self, p: QPainter) -> None:
        font = QFont("Microsoft YaHei", 9)
        p.setFont(font)
        fm = QFontMetrics(font)
        text = self._hover_text or ""
        tw, th = fm.horizontalAdvance(text), fm.height()
        x, y = 10, self.height() - th - 12
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(15, 23, 42, 215))
        p.drawRoundedRect(QRectF(x, y, tw + 14, th + 8), 4, 4)
        p.setPen(QPen(QColor("#e2e8f0")))
        p.drawText(QRect(int(x) + 7, y, tw, th + 8),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

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
        rec = self._live or (self._visible[0] if self._visible else None)
        if rec is None or not rec.points:
            if self._hover_text:
                self._hover_text = None
                self.update()
            return
        r = self._map_rect()
        if r.isEmpty():
            return
        mx, my = event.position().x(), event.position().y()
        best: RoutePoint | None = None
        best_d2 = 18.0 ** 2  # 命中半径 18px
        for pt in rec.points:
            px = self._to_px(pt.x, pt.y)
            d2 = (px.x() - mx) ** 2 + (px.y() - my) ** 2
            if d2 < best_d2:
                best_d2, best = d2, pt
        new_text = None
        if best is not None:
            # 悬停预览用毫秒精度(轨迹点本身就是高精度采样, 便于对比微小时差)
            new_text = f"时间 {fmt_time_ms(best.t)}   速度 {fmt_speed(best.speed)} km/h"
        if new_text != self._hover_text:
            self._hover_text = new_text
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._hover_text:
            self._hover_text = None
            self.update()
