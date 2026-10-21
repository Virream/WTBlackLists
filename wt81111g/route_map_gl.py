"""pygfx(WebGPU/wgpu) 版地图画布 —— 高性能替代 `route_map_view.RouteMapView`。

**最小可用版本**(2026-10-21): 底图 + 多队线路(速度配色) + 回放进度着色 + 阵亡变灰/X +
缩放平移 + 命中测试用的坐标换算。悬停浮层、战区闪烁、重生方块、时间标签等图元后续补。

为什么另起一个模块而不是直接改 `route_map_view.py`:
* 那一份是**软件渲染**(QPainter), 功能齐全、有像素级回归测试, 留着当回退路径;
* 这一份只依赖 pygfx/wgpu, 对外接口尽量与它一致, 由 `route_window` 决定用哪一份。

实测(spike, 64 条线路 / 29630 段, 1400×900):
* 渲染 1.2~2.8 ms/帧(`present_method="screen"` 原生 present), 无“重建整层”尖峰;
* **必须** `present_method="screen"`: 默认的 bitmap present 要读回整帧再 QPainter 贴图,
  实测 11.9~89 ms/帧, 比软件渲染还慢;
* 段长 < 线宽时 pygfx 厚线会画成“一颗颗珠子”(接头缩短所致)。按用户要求**接受**这种
  观感(每个采样点一颗珠子), 因此这里不做 LOD 抽稀;
* 中文文字要显式注册系统字体(见 `_register_cjk_font`)。
"""
from __future__ import annotations

import logging
import math
import os
import time

import numpy as np

from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QVBoxLayout, QWidget

import pygfx as gfx
from pygfx.utils.text import font_manager as _font_manager

from .route_map_view import (COLOR_DEAD_MARK_A, COLOR_DEAD_MARK_B, COLOR_DEAD_ROUTE,
                             MIN_ZOOM, MAX_ZOOM, WHEEL_STEP, PAN_MARGIN,
                             PLAY_PEN, bucket_index, speed_color)

log = logging.getLogger("route_map_gl")

# 画布底色(与软件渲染一致)
COLOR_BG = "#0b1220"
# 未播放部分的底衬颜色
COLOR_UNPLAYED = QColor(148, 163, 184, 90)
# 阵亡标记笔宽 / 半径(屏幕像素)
DEAD_MARK_PEN = 2.2

_CJK_FAMILY = "Microsoft YaHei"
_FONT_CANDIDATES = (
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "fonts", "SourceHanSansSC-Regular.otf"),
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhl.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
)
_fonts_ready = False


def _register_cjk_font() -> None:
    """把系统里的中文字体注册给 pygfx(它自带的字体管理器在 Windows 上扫不到 CJK 字体)。

    优先用程序自带的 `data/fonts/*.otf`(将来打包一份思源黑体), 找不到才用系统字体。
    没注册成功时 `gfx.Text` 会打印 “Cannot render chars …”, 但线路绘制不受影响。
    """
    global _fonts_ready
    if _fonts_ready:
        return
    _fonts_ready = True
    got = False
    for path in _FONT_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            _font_manager.add_font_file(path)
            got = True
        except Exception:  # noqa: BLE001
            log.warning("注册字体失败: %s", path)
    if not got:
        log.warning("没找到可用的中文字体, pygfx 文字可能显示为空白")


def _rgba(c: QColor, mul: float = 1.0) -> tuple[float, float, float, float]:
    """QColor → pygfx 用的 (r,g,b,a) 浮点四元组。"""
    return (c.redF(), c.greenF(), c.blueF(), c.alphaF() * mul)


def _img_to_rgb(data: bytes) -> np.ndarray | None:
    """地图原始字节(jpg/png) → (h, w, 3) uint8 数组, 通道顺序 **RGB**。

    坑(2026-10-21): `QImage.Format_RGB32` 是 0xffRRGGBB, 在**小端**内存里字节顺序是
    **B,G,R,A** —— 直接取 `[:, :3]` 得到的是 BGR, 传给 pygfx 就成了红蓝互换, 底图颜色
    明显不对(实测源 (110,115,114) 画出来是 (114,115,110))。所以最后要 `[..., ::-1]`。
    """
    img = QImage.fromData(data)
    if img.isNull():
        return None
    img = img.convertToFormat(QImage.Format.Format_RGB32)
    w, h = img.width(), img.height()
    buf = img.constBits()
    buf.setsize(img.sizeInBytes())
    bgra = np.frombuffer(buf, np.uint8).reshape(h, img.bytesPerLine() // 4, 4)[:, :w, :3]
    return np.ascontiguousarray(bgra[..., ::-1])


class _GlRoute:
    """一条线路在 GPU 上的两个对象: 半透明底衬 + 已播放的着色段。"""

    __slots__ = ("rec", "pos", "colors", "vmax", "under", "play", "drawn", "dead")

    def __init__(self, rec, pos: np.ndarray, colors: np.ndarray, vmax: float,
                 under, play) -> None:
        self.rec = rec
        self.pos = pos
        self.colors = colors
        self.vmax = vmax
        self.under = under
        self.play = play
        self.drawn = 0
        self.dead = False


class RouteMapGlView(QWidget):
    """pygfx 版地图控件(最小集)。

    `offscreen=True` 时用离屏画布(测试/截图用, 不弹窗)。
    """

    def __init__(self, parent=None, *, offscreen: bool = False,
                 present_method: str = "screen") -> None:
        super().__init__(parent)
        self.setMinimumSize(420, 300)
        _register_cjk_font()

        self._offscreen = offscreen
        if offscreen:
            from rendercanvas.offscreen import RenderCanvas
            self._canvas = RenderCanvas(size=(1400, 900), pixel_ratio=1)
        else:
            from rendercanvas.qt import RenderCanvas
            self._canvas = RenderCanvas(size=(900, 620), title="路线地图 (GPU)",
                                        present_method=present_method)
            sub = getattr(self._canvas, "_subwidget", None)
            lay = QVBoxLayout(self)
            lay.setContentsMargins(0, 0, 0, 0)
            if sub is not None:
                lay.addWidget(sub)
                # 鼠标/滚轮事件都落在 subwidget 上, 必须由它转发(见 eventFilter 说明)
                sub.setMouseTracking(True)
                sub.installEventFilter(self)

        self._renderer = gfx.WgpuRenderer(self._canvas)
        self._scene = gfx.Scene()
        self._scene.add(gfx.Background(None, gfx.BackgroundMaterial(COLOR_BG)))
        self._camera = gfx.OrthographicCamera(1, 1, maintain_aspect=True)
        self._camera.local.position = (0.5, 0.5, 2.0)
        self._scene.add(self._camera)

        self._plane = None            # 底图 Mesh
        self._tex = None
        self._tex_size = (0, 0)
        self._routes: list[_GlRoute] = []
        self._dead_line = None        # 阵亡 X 的 Line(所有叉合成一条)
        self._dead_key = None         # 阵亡叉内容指纹(避免每帧重建)

        # 视图状态(与软件渲染同一套语义: zoom 相对“适配窗口”, pan 为像素偏移)
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._dragging = False
        self._drag_last = QPointF()

        # 回放
        self._playhead = 0.0
        self._playing = False
        self._dead_dim = True
        self._dead_mark = True

        # FPS 统计
        self._fps_n = 0
        self._fps_t = time.perf_counter()
        self._fps = 0.0
        self._title_t = 0.0

        self._canvas.request_draw(self._draw)

    # ------------------------------------------------------------ 对外接口
    @property
    def fps(self) -> float:
        """最近一秒的平均帧率(演示窗口标题里用)。"""
        return self._fps

    def set_map_bytes(self, data: bytes) -> bool:
        """设置底图(jpg/png 原始字节)。"""
        arr = _img_to_rgb(data)
        if arr is None:
            return False
        h, w = arr.shape[:2]
        if self._tex is not None and (w, h) == self._tex_size:
            self._tex.data[:] = arr
            self._tex.update_range((0, 0, 0), self._tex.size)
        else:
            old = self._plane
            self._tex = gfx.Texture(arr, dim=2)
            self._tex_size = (w, h)
            mat = gfx.MeshBasicMaterial(map=self._tex)
            self._plane = gfx.Mesh(gfx.plane_geometry(1, 1), mat)
            self._plane.local.position = (0.5, 0.5, -0.02)
            self._scene.add(self._plane)
            if old is not None:
                old.remove()
        self._tex_aspect = w / max(1, h)
        self.reset_view()
        return True

    def set_routes(self, recs) -> None:
        """整体替换要画的线路(每条记录画成一条带速度着色的折线)。"""
        for r in self._routes:
            r.under.remove()
            r.play.remove()
        self._routes = [x for x in (self._make_route(rec) for rec in recs) if x is not None]
        self._apply_playhead(self._playhead, force=True)
        self._refresh_dead_line()
        self.update()

    def set_playback(self, recs=None, playhead: float = 0.0) -> None:
        """设置回放进度(只改颜色数组, 不重建几何)。"""
        self._playhead = float(playhead)
        self._playing = bool(recs)
        self._apply_playhead(self._playhead)
        self._refresh_dead_line()
        self.update()

    def set_dead_style(self, dim: bool | None = None, mark: bool | None = None) -> None:
        if dim is not None:
            self._dead_dim = bool(dim)
        if mark is not None:
            self._dead_mark = bool(mark)
        self._apply_playhead(self._playhead, force=True)
        self._refresh_dead_line()
        self.update()

    def reset_view(self) -> None:
        self._zoom = 1.0
        self._pan_x = 0.0
        self._pan_y = 0.0
        self.update()

    def map_pos(self, x: float, y: float) -> QPointF:
        """归一化坐标 → 控件像素坐标(命中测试用, 与软件渲染一致)。"""
        r = self._map_rect()
        dw, dh = self._map_size()
        return QPointF(r.left() + x * dw, r.top() + y * dh)

    def snapshot(self, srgb: bool = False) -> np.ndarray:
        """离屏模式下取一帧(测试用)。

        注意 `renderer.snapshot()` 读回的色彩空间与屏幕不一致(实测屏幕上是 (114,115,110),
        读回却是 (50,50,50)), 所以它只能拿来比对结构/像素位置, **不能**用来判断观感 ——
        要“眼睛看到的样子”就用 `grab()`(走 Qt 控件抓图)。
        """
        arr = np.asarray(self._renderer.snapshot())
        return _encode_srgb(arr) if srgb else arr

    def grab(self) -> "QImage | None":
        """抓一张“屏幕上看到的样子”(非离屏模式, 会强制重绘一次)。"""
        sub = getattr(self._canvas, "_subwidget", None)
        if sub is None:
            return None
        self._draw()
        return sub.grab().toImage()

    def update(self) -> None:  # noqa: N802 - Qt 命名
        super().update()
        self._canvas.request_draw(self._draw)

    # ------------------------------------------------------------ 视图几何
    def _map_size(self) -> tuple[float, float]:
        if self._tex is None:
            return (0.0, 0.0)
        w, h = self._canvas.get_logical_size()
        s = min(w / self._tex_size[0], h / self._tex_size[1]) * self._zoom
        return (self._tex_size[0] * s, self._tex_size[1] * s)

    def _map_rect(self) -> QRectF:
        dw, dh = self._map_size()
        w, h = self._canvas.get_logical_size()
        if dw <= 0 or dh <= 0:
            return QRectF()
        cx = w / 2.0 + self._pan_x
        cy = h / 2.0 + self._pan_y
        return QRectF(cx - dw / 2.0, cy - dh / 2.0, dw, dh)

    def _clamp_pan(self) -> None:
        dw, dh = self._map_size()
        w, h = self._canvas.get_logical_size()
        if dw <= 0 or dh <= 0:
            return
        max_x = max(0.0, (dw + w) / 2.0 - PAN_MARGIN)
        max_y = max(0.0, (dh + h) / 2.0 - PAN_MARGIN)
        self._pan_x = max(-max_x, min(max_x, self._pan_x))
        self._pan_y = max(-max_y, min(max_y, self._pan_y))

    def _sync_camera(self) -> None:
        """把 zoom/pan 换算成相机的可见矩形(归一化地图坐标, y 朝上)。"""
        r = self._map_rect()
        dw, dh = self._map_size()
        w, h = self._canvas.get_logical_size()
        if dw <= 0 or dh <= 0:
            return
        x0 = -r.left() / dw
        x1 = (w - r.left()) / dw
        # 控件 y 是从上往下, pygfx 是从下往上 → 翻转
        y_lo = 1.0 - (h - r.top()) / dh
        y_hi = 1.0 + r.top() / dh
        self._camera.show_rect(x0, x1, y_lo, y_hi)

    # ------------------------------------------------------------ 线路几何
    def _make_route(self, rec) -> _GlRoute | None:
        pts = getattr(rec, "points", None)
        if not pts or len(pts) < 2:
            return None
        n = len(pts)
        pos = np.empty((n, 3), dtype=np.float32)
        pos[:, 0] = [p.x for p in pts]
        pos[:, 1] = [1.0 - p.y for p in pts]
        pos[:, 2] = 0.0
        vmax = max(max(p.speed for p in pts), 1.0)
        colors = np.empty((n, 4), dtype=np.float32)
        cache: dict[int, tuple] = {}
        for i, p in enumerate(pts):
            k = bucket_index(p.speed, vmax)
            c = cache.get(k)
            if c is None:
                c = _rgba(speed_color((k + 0.5) / 24 * vmax, vmax))
                cache[k] = c
            colors[i] = c
        # 底衣(半透明灰, 细)在下, 已播放线(速度配色, 粗)在上。
        # 两者都是半透明物体, 只靠 z 值排序不可靠(同 z 时顺序不确定, 实测灰底衣会
        # 盖掉彩线 → 整条线看起来发白); 所以显式给 render_order, 并把 play 抬 z=0.01。
        pos_play = pos.copy()
        pos_play[:, 2] = 0.01
        under = gfx.Line(
            gfx.Geometry(positions=pos),
            gfx.LineMaterial(thickness=2.0, thickness_space="screen",
                             color=_rgba(COLOR_UNPLAYED)),
            render_order=-1)
        play_colors = np.zeros_like(colors)
        play = gfx.Line(
            gfx.Geometry(positions=pos_play, colors=play_colors),
            gfx.LineMaterial(thickness=PLAY_PEN, thickness_space="screen",
                             color_mode="vertex", aa=True),
            render_order=0)
        self._scene.add(under)
        self._scene.add(play)
        return _GlRoute(rec, pos, colors, vmax, under, play)

    def _apply_playhead(self, head: float, force: bool = False) -> None:
        """按进度改“已播放”那部分的颜色(只动变化的切片, 每帧 1~2 ms)。"""
        for r in self._routes:
            pts = r.rec.points
            end = pts[-1].t if pts else 0.0
            dead = bool(getattr(r.rec, "dead", False))
            dim = dead and self._dead_dim and head >= end
            if dim != r.dead or force:
                r.dead = dim
                # 阵亡: 底衬整条改成半透明纯灰, 已播放层全部透明
                r.under.material.color = _rgba(COLOR_DEAD_ROUTE if dim else COLOR_UNPLAYED)
            if dim:
                if force or r.drawn:
                    r.play.geometry.colors.data[:] = 0.0
                    r.play.geometry.colors.update_range(0, len(r.colors))
                    r.drawn = 0
                continue
            k = _bisect_time(pts, head)
            if force:
                arr = r.play.geometry.colors.data
                arr[:k] = r.colors[:k]
                arr[k:] = 0.0
                r.play.geometry.colors.update_range(0, len(arr))
                r.drawn = k
                continue
            if k > r.drawn:
                arr = r.play.geometry.colors.data
                arr[r.drawn:k] = r.colors[r.drawn:k]
                r.play.geometry.colors.update_range(r.drawn, k - r.drawn)
                r.drawn = k

    def _refresh_dead_line(self) -> None:
        """阵亡 X: 所有叉合成**一条** Line(每叉两段, 用逐顶点颜色区分 A 蓝 / B 红)。

        回放时这个函数每帧都会被调 —— 所以先算一个“内容指纹”(哪些叉 + 叉的大小),
        没变就直接返回, 不能每帧重建 Line(否则每帧都在建 GPU 对象)。
        """
        dw, dh = self._map_size()
        if dw <= 0 or dh <= 0:
            return
        size = 5.0
        dead_now: list = []
        for r in self._routes:
            if not self._dead_mark or not bool(getattr(r.rec, "dead", False)):
                continue
            pts = r.rec.points
            if pts and self._playhead >= pts[-1].t:
                dead_now.append(r)
        key = (self._dead_mark, round(dw, 2), round(dh, 2),
               tuple(r.rec.route_id for r in dead_now))
        if key == self._dead_key:
            return
        self._dead_key = key

        pos: list[float] = []
        cols: list[float] = []
        sx = size / dw
        sy = size / dh
        for r in dead_now:
            side = getattr(r.rec, "side", "")
            col = _rgba(COLOR_DEAD_MARK_A if side == "A" else COLOR_DEAD_MARK_B)
            x, y = r.rec.points[-1].x, 1.0 - r.rec.points[-1].y
            for a, b in (((x - sx, y - sy), (x + sx, y + sy)),
                         ((x - sx, y + sy), (x + sx, y - sy))):
                pos += [a[0], a[1], 0.0, b[0], b[1], 0.0]
                cols += [*col, *col]
        if self._dead_line is not None:
            self._dead_line.remove()
            self._dead_line = None
        if not pos:
            return
        self._dead_line = gfx.Line(
            gfx.Geometry(positions=np.array(pos, dtype=np.float32).reshape(-1, 3),
                         colors=np.array(cols, dtype=np.float32).reshape(-1, 4)),
            gfx.LineSegmentMaterial(thickness=DEAD_MARK_PEN, thickness_space="screen",
                                    color_mode="vertex"),
            render_order=1)
        self._scene.add(self._dead_line)

    # ------------------------------------------------------------ 帧循环
    def _draw(self) -> None:
        self._sync_camera()
        self._renderer.render(self._scene, self._camera)
        self._fps_n += 1
        now = time.perf_counter()
        dt = now - self._fps_t
        if dt >= 0.5:
            self._fps = self._fps_n / dt
            self._fps_n = 0
            self._fps_t = now
            if not self._offscreen:
                self._canvas.set_title(f"路线地图 (GPU)  {self._fps:.0f} fps")

    # ------------------------------------------------------------ 交互
    # 关键: 鼠标/滚轮事件都发生在 canvas 内嵌的 `_subwidget`(QRenderWidget)上,
    # **不会**传给我们这个外层 QWidget —— 2026-10-21 用户实测“缩放和拖拽没反应”。
    # 所以在 subwidget 上装 Qt 事件过滤器, 直接截它的 Wheel/Mouse* 事件。
    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt 命名
        t = event.type()
        if t == QEvent.Type.Wheel:
            self._zoom_at(event.angleDelta().y() / 120.0, event.position())
            return True
        if t == QEvent.Type.MouseButtonDblClick:
            self.reset_view()
            return True
        if t == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.MiddleButton:
                self._dragging = True
                self._drag_last = event.position()
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                return True
            return False
        if t == QEvent.Type.MouseButtonRelease:
            if event.button() == Qt.MouseButton.MiddleButton:
                self._dragging = False
                self.setCursor(Qt.CursorShape.ArrowCursor)
                return True
            return False
        if t == QEvent.Type.MouseMove and self._dragging:
            self._pan_by(event.position())
            return True
        return super().eventFilter(obj, event)

    def _zoom_at(self, steps: float, pos: QPointF) -> None:
        """以 `pos`(控件坐标)为中心缩放 `steps` 格(鼠标下那个点保持不动)。"""
        if not steps:
            return
        before = self._map_pos_at(pos)
        self._zoom = max(MIN_ZOOM, min(MAX_ZOOM, self._zoom * (WHEEL_STEP ** steps)))
        self._clamp_pan()
        after = self._map_pos_at(pos)
        self._pan_x += after.x() - before.x()
        self._pan_y += after.y() - before.y()
        self._clamp_pan()
        self.update()

    def _pan_by(self, pos: QPointF) -> None:
        """跟着鼠标平移(delta 由上一次位置算)。"""
        self._pan_x += pos.x() - self._drag_last.x()
        self._pan_y += pos.y() - self._drag_last.y()
        self._drag_last = pos
        self._clamp_pan()
        self.update()

    def _map_pos_at(self, pos: QPointF) -> QPointF:
        """控件坐标 → 归一化地图坐标。"""
        r = self._map_rect()
        dw, dh = self._map_size()
        if dw <= 0 or dh <= 0:
            return QPointF()
        return QPointF((pos.x() - r.left()) / dw, (pos.y() - r.top()) / dh)

    # 下面四个是“本控件自己拿到事件”时的入口(不带内嵌 canvas 时/测试用)。
    def wheelEvent(self, event) -> None:  # noqa: N802
        self._zoom_at(event.angleDelta().y() / 120.0, event.position())

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton:
            self._dragging = True
            self._drag_last = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton:
            self._dragging = False
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self._pan_by(event.position())

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.reset_view()


def _bisect_time(pts, head: float) -> int:
    """`t <= head` 的点数(与软件渲染同一个判定, 供进度着色用)。"""
    lo, hi = 0, len(pts)
    while lo < hi:
        mid = (lo + hi) // 2
        if pts[mid].t <= head:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _encode_srgb(arr: np.ndarray) -> np.ndarray:
    """线性 → sRGB(截图用: 让存出来的图与屏幕观感一致)。"""
    out = arr.astype(np.float32) / 255.0
    rgb = out[..., :3]
    lo = rgb * 12.92
    hi = 1.055 * np.power(np.clip(rgb, 0.0, None), 1.0 / 2.4) - 0.055
    out[..., :3] = np.where(rgb <= 0.0031308, lo, hi)
    res = np.empty_like(arr)
    res[..., :3] = np.clip(out[..., :3] * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)
    if arr.shape[-1] > 3:
        res[..., 3] = arr[..., 3]
    return res
