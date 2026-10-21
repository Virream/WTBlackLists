"""pygfx(WebGPU/wgpu) 渲染的地图画布 —— **继承** `RouteMapView`, 只把“画”换成 GPU。

为什么这样接:
* 软件版 `RouteMapView` 里攒了 4000 多行**状态机与交互**(眼睛开关、回放进度、命中测试、
  缩放平移、换图清理、占位图识别……), 重写一遍必然是 bug 温床;
* 所以这里 `class RouteMapGlView(RouteMapView)`, 不覆盖任何状态/交互逻辑, 只覆盖
  `paintEvent`(不再用 QPainter 画)和 `set_map_bytes`(顺带更新 GPU 纹理),
  其余全部复用 → 主程序里只要把 `RouteMapView(...)` 换成工厂函数即可, 行为一致。

实测(2026-10-21, 64 条线路 / 29630 段 / 1400×900): 渲染 1.2~2.8 ms/帧,
没有“重建整层”的尖峰。**必须** `present_method="screen"`: 默认的 bitmap present
要读回整帧再 QPainter 贴图, 实测 11.9~89 ms/帧, 比软件渲染还差。

已知取舍:
* 段长 < 线宽时 pygfx 厚线会画成“一颗颗珠子”(接头缩短), 用户已确认可接受;
* 中文字体要显式注册(`_register_cjk_font`)。
"""
from __future__ import annotations

import logging
import math
import os
import time

import numpy as np

# 必须先导入 PyQt6, rendercanvas 靠它判断用哪个 Qt 绑定
from PyQt6.QtCore import QEvent, QPointF
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication, QVBoxLayout

import pygfx as gfx
from pygfx.utils.text import font_manager as _font_manager

from . import route_map_view as rmv
from .route_map_view import RouteMapView, bucket_index, speed_color

log = logging.getLogger("route_map_gl")


def _gpu_usable() -> bool:
    """能不能安全地创建 GPU 画布。

    **必须在创建 canvas 之前判断**: 在 `QT_QPA_PLATFORM=offscreen`/`minimal` 这类没有
    真实窗口系统的平台上, wgpu 配置 surface 会在 **Rust 层直接 panic**
    (`wgpuSurfaceConfigure: Surface does not support the adapter's queue family`),
    那是 try/except 抓不住的进程级崩溃 —— 所以测试/无头环境一律走软件渲染。
    """
    if os.environ.get("WT_MAP_SOFTWARE", "") not in ("", "0"):
        return False
    app = QApplication.instance()
    if app is not None and app.platformName().lower() in ("offscreen", "minimal", "vnc"):
        return False
    return True


def create_map_view(parent=None) -> RouteMapView:
    """主程序用的工厂: 优先 GPU 画布, 不行就回退软件渲染。

    环境变量 `WT_MAP_SOFTWARE=1` 可强制走软件渲染(排查显卡/驱动/远程桌面问题时用)。
    任何异常(没装 pygfx、wgpu 找不到适配器、GL 上下文创建失败…)都回退, 保证程序能开。
    """
    if not _gpu_usable():
        log.info("GPU 画布不可用(环境变量或无头平台) → 软件渲染")
        return RouteMapView(parent)
    try:
        view = RouteMapGlView(parent)
        log.info("地图画布: GPU (pygfx/wgpu)")
        return view
    except Exception:  # noqa: BLE001
        log.exception("GPU 画布初始化失败, 回退软件渲染")
        return RouteMapView(parent)

# ---------------------------------------------------------------- 常量
BG_COLOR = "#0b1220"
CURSOR_DOT_R = rmv.PLAY_CURSOR_R
PLAYER_DIR_COLOR = QColor("#f8fafc")

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

# z 层次(归一化地图坐标, 相机看向 -z)
Z_MAP = -0.02
Z_TRACK = 0.0
Z_UNDER = 0.004
Z_PLAY = 0.008
Z_SPAWN = 0.012
Z_ZONE = 0.016
Z_MARK = 0.022
Z_PLAYER = 0.026


def _register_cjk_font() -> None:
    """把中文字体注册给 pygfx(它自带的字体管理器在 Windows 上扫不到 CJK 字体)。"""
    global _fonts_ready
    if _fonts_ready:
        return
    _fonts_ready = True
    for path in _FONT_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            _font_manager.add_font_file(path)
        except Exception:  # noqa: BLE001
            log.warning("注册字体失败: %s", path)


def _rgba(c: QColor, mul: float = 1.0) -> tuple[float, float, float, float]:
    return (c.redF(), c.greenF(), c.blueF(), c.alphaF() * mul)


def _img_to_rgb(data: bytes) -> np.ndarray | None:
    """地图字节 → (h, w, 3) uint8 RGB。

    坑: `QImage.Format_RGB32` 是 0xffRRGGBB, 小端内存里字节序是 **B,G,R,A** ——
    直接取 `[:, :3]` 是 BGR, 送到 GPU 会红蓝互换(实测源 (114,115,110) 画成
    (110,115,114))。所以最后要 `[..., ::-1]`。
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


def _pos3(xy: np.ndarray, z: float) -> np.ndarray:
    out = np.empty((len(xy), 3), dtype=np.float32)
    out[:, :2] = xy
    out[:, 2] = z
    return out


class _Track:
    """一条线路的 GPU 对象: 单色底衬(可选) + 速度配色线。"""

    __slots__ = ("rec", "pos", "colors", "vmax", "under", "play", "drawn", "dead")

    def __init__(self, rec, pos, colors, vmax, under, play) -> None:
        self.rec = rec
        self.pos = pos
        self.colors = colors
        self.vmax = vmax
        self.under = under
        self.play = play
        self.drawn = 0
        self.dead = False


class RouteMapGlView(RouteMapView):
    """GPU 渲染的 RouteMapView(对外接口与软件版完全相同)。"""

    def __init__(self, parent=None, *, present_method: str = "screen",
                 offscreen: bool = False) -> None:
        super().__init__(parent)
        self._offscreen = offscreen
        self._gl_pixels: dict = {}
        self._gl_objs: list = []
        self._gl_key: tuple | None = None
        self._gl_tracks: list[_Track] = []
        self._gl_anim = 0.0
        self._fps_n = 0
        self._fps_t = time.perf_counter()
        self._fps = 0.0

        _register_cjk_font()
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
                # 鼠标/滚轮事件全落在 canvas 的内嵌控件上, 必须由它转发过来
                sub.setMouseTracking(True)
                sub.installEventFilter(self)

        self._renderer = gfx.WgpuRenderer(self._canvas)
        self._scene = gfx.Scene()
        self._scene.add(gfx.Background(None, gfx.BackgroundMaterial(BG_COLOR)))
        self._camera = gfx.OrthographicCamera(1, 1, maintain_aspect=True)
        self._camera.local.position = (0.5, 0.5, 2.0)
        self._scene.add(self._camera)
        self._tex = None
        self._plane = None
        self._hover_obj = None
        self._canvas.request_draw(self._draw)

    # -------------------------------------------------------- 覆盖渲染入口
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """软件绘制不再使用(GPU 画布自己往窗口里画)。"""

    def update(self) -> None:  # noqa: N802 - Qt 命名
        super().update()
        canvas = getattr(self, "_canvas", None)
        if canvas is not None:
            canvas.request_draw(self._draw)

    def set_map_bytes(self, data: bytes | None, key: str | None = None) -> bool:
        """复用父类的解码/占位图/换图清理逻辑, 另外更新 GPU 纹理。"""
        changed = super().set_map_bytes(data, key)
        if changed:
            self._update_texture(data)
        return changed

    def _update_texture(self, data: bytes | None) -> None:
        if not data or self._pixmap is None:      # 空数据/占位图 → 不画底图
            if self._plane is not None:
                self._plane.remove()
                self._plane = None
            self._tex = None
            self.update()
            return
        arr = _img_to_rgb(data)
        if arr is None:
            return
        if self._tex is not None and arr.shape[:2] == self._tex.data.shape[:2]:
            self._tex.data[:] = arr
            self._tex.update_full()
        else:
            if self._plane is not None:
                self._plane.remove()
            self._tex = gfx.Texture(arr, dim=2)
            self._plane = gfx.Mesh(gfx.plane_geometry(1, 1),
                                   gfx.MeshBasicMaterial(map=self._tex))
            self._plane.local.position = (0.5, 0.5, Z_MAP)
            self._scene.add(self._plane)
        self.update()

    # -------------------------------------------------------- 事件转发
    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt 命名
        """把 canvas 内嵌控件上的鼠标/滚轮事件转给**父类**去处理。

        父类的 `wheelEvent` / `mouseMoveEvent` 已经实现了以鼠标为锚点的缩放、
        中键平移与悬停命中测试 —— 这里直接 sendEvent 给 self, 逻辑一行都不用重写。
        """
        t = event.type()
        if t in (QEvent.Type.Wheel, QEvent.Type.MouseButtonPress,
                 QEvent.Type.MouseButtonRelease, QEvent.Type.MouseMove,
                 QEvent.Type.MouseButtonDblClick):
            QApplication.sendEvent(self, event)
            return True
        return super().eventFilter(obj, event)

    # -------------------------------------------------------- 相机
    def _sync_camera(self) -> None:
        r = self._map_rect()
        dw, dh = self._map_size()
        w, h = self._canvas.get_logical_size()
        if dw <= 0 or dh <= 0 or w <= 0 or h <= 0:
            return
        self._camera.show_rect(-r.left() / dw, (w - r.left()) / dw,
                              1.0 - (h - r.top()) / dh, 1.0 + r.top() / dh)

    # -------------------------------------------------------- 每帧
    def _draw(self) -> None:
        try:
            self._sync_objects()
            self._sync_camera()
            self._renderer.render(self._scene, self._camera)
        except Exception:  # noqa: BLE001
            log.exception("GPU 渲染失败")
            return
        self._fps_n += 1
        now = time.perf_counter()
        dt = now - self._fps_t
        if dt >= 0.5:
            self._fps = self._fps_n / dt
            self._fps_n = 0
            self._fps_t = now
            if not self._offscreen:
                self._canvas.set_title(f"路线地图 (GPU)  {self._fps:.0f} fps")

    @property
    def fps(self) -> float:
        return self._fps

    def snapshot(self) -> np.ndarray:
        """离屏取一帧(结构比对用; 色彩空间与屏幕不一致, 观感请看窗口)。"""
        return np.asarray(self._renderer.snapshot())

    # -------------------------------------------------------- GPU 内容同步
    def _content_key(self) -> tuple:
        """决定要不要**重建 GPU 对象**的指纹(进度/闪烁不在内, 那些每帧只改颜色)。"""

        def sig(recs) -> tuple:
            return tuple((getattr(r, "route_id", id(r)), len(r.points)) for r in recs)

        live = self._live
        zones = ()
        if self._show_zones:
            zones = tuple((z.get("label"), z.get("side"), round(float(z.get("x", 0)), 6),
                           round(float(z.get("y", 0)), 6)) for z in self._zones)
        return (sig(self._visible), sig(self._highlight),
                (getattr(live, "route_id", ""), len(live.points)) if live else None,
                sig(self._live_multi), sig(self._playback_recs),
                self._show_zones, zones, self._show_spawns, self._spawns_ver,
                self._map_digest)

    def _clear_objects(self) -> None:
        for obj in self._gl_objs:
            obj.remove()
        self._gl_objs = []
        self._gl_tracks = []
        self._hover_obj = None

    def _add(self, obj):
        self._scene.add(obj)
        self._gl_objs.append(obj)
        return obj

    def _sync_objects(self) -> None:
        key = self._content_key()
        if key != self._gl_key:
            self._gl_key = key
            self._rebuild_objects()
        self._apply_progress()
        self._apply_anim()

    def _rebuild_objects(self) -> None:
        self._clear_objects()
        if self._playback_recs:
            # 回放模式下**只**画正在回放的那几条(与软件版一致: 眼睛点亮的暂时让位)
            for rec in self._playback_recs:
                self._make_track(rec, colored=True, under=True)
        else:
            # 1) 眼睛点亮的轨迹: 单色灰蓝
            for rec in self._visible:
                self._make_track(rec, colored=False, under=False)
            # 2) 选中的 / 正在录的: 速度配色
            for rec in self._highlight:
                self._make_track(rec, colored=True, under=False)
            if self._live is not None:
                self._make_track(self._live, colored=True, under=False)
        # 3) 全局录制中“边录边画”的线路: 始终画
        for rec in self._live_multi:
            self._make_track(rec, colored=True, under=False)
        # 4) 重生方块 / 战区 / 阵亡叉 / 玩家 / 光标
        self._build_spawns()
        self._build_zones()
        self._build_dead_marks()
        self._build_player()
        self._build_cursor()
        self._build_hover()

    def _make_track(self, rec, *, colored: bool, under: bool) -> None:
        pts = getattr(rec, "points", None)
        if not pts or len(pts) < 2:
            return
        n = len(pts)
        pos = np.empty((n, 3), dtype=np.float32)
        pos[:, 0] = [p.x for p in pts]
        pos[:, 1] = [1.0 - p.y for p in pts]
        pos[:, 2] = Z_TRACK if not under else Z_PLAY
        if not colored:
            line = self._add(gfx.Line(
                gfx.Geometry(positions=pos),
                gfx.LineMaterial(thickness=2.0, thickness_space="screen",
                                 color=_rgba(rmv.COLOR_HISTORY))))
            self._gl_tracks.append(_Track(rec, pos, None, 0.0, line, None))
            return
        vmax = self._vmax(rec)
        colors = np.empty((n, 4), dtype=np.float32)
        cache: dict[int, tuple] = {}
        for i, p in enumerate(pts):
            k = bucket_index(p.speed, vmax)
            c = cache.get(k)
            if c is None:
                c = _rgba(speed_color((k + 0.5) / 24 * vmax, vmax))
                cache[k] = c
            colors[i] = c
        under_obj = None
        if under:
            under_obj = self._add(gfx.Line(
                gfx.Geometry(positions=_pos3(pos[:, :2], Z_UNDER)),
                gfx.LineMaterial(thickness=2.0, thickness_space="screen",
                                 color=_rgba(rmv.COLOR_UNPLAYED)),
                render_order=-1))
        play = self._add(gfx.Line(
            gfx.Geometry(positions=pos, colors=np.zeros_like(colors)),
            gfx.LineMaterial(thickness=rmv.PLAY_PEN, thickness_space="screen",
                             color_mode="vertex", aa=True)))
        self._gl_tracks.append(_Track(rec, pos, colors, vmax, under_obj, play))

    # ---------------------------------------------------- 图元: 重生/战区/叉
    def _build_spawns(self) -> None:
        if not self._show_spawns or not self._spawns:
            return
        dw, dh = self._map_size()
        if dw <= 0 or dh <= 0:
            return
        sx, sy = rmv.SPAWN_HALF / dw, rmv.SPAWN_HALF / dh
        pos: list[float] = []
        cols: list[float] = []
        for s in self._spawns:
            try:
                x, y = float(s["x"]), 1.0 - float(s["y"])
            except (KeyError, TypeError, ValueError):
                continue
            col = _rgba(rmv.SPAWN_SIDE_COLORS.get(str(s.get("side") or "neutral"),
                                                  rmv.SPAWN_SIDE_COLORS["neutral"]))
            for a, b in (((x - sx, y - sy), (x + sx, y - sy)),
                         ((x + sx, y - sy), (x + sx, y + sy)),
                         ((x + sx, y + sy), (x - sx, y + sy)),
                         ((x - sx, y + sy), (x - sx, y - sy))):
                pos += [a[0], a[1], Z_SPAWN, b[0], b[1], Z_SPAWN]
                cols += [*col, *col]
        if not pos:
            return
        self._add(gfx.Line(
            gfx.Geometry(positions=np.array(pos, dtype=np.float32).reshape(-1, 3),
                         colors=np.array(cols, dtype=np.float32).reshape(-1, 4)),
            gfx.LineSegmentMaterial(thickness=rmv.SPAWN_PEN, thickness_space="screen",
                                    color_mode="vertex")))

    def _build_zones(self) -> None:
        """战区: 菱形轮廓 + 中心字母。

        注意: pygfx 0.17 + wgpu 0.32 下 **Mesh 的 pipeline 会直接报 shader 校验错**
        (连一个三角形渲不出来, 实测), 所以填充图形一律用 Line/Points 画。
        """
        if not self._show_zones or not self._zones:
            return
        dw, dh = self._map_size()
        if dw <= 0 or dh <= 0:
            return
        rx, ry = rmv.ZONE_RADIUS / dw, rmv.ZONE_RADIUS / dh
        self._gl_zones = []
        for z in self._zones:
            try:
                x, y = float(z["x"]), 1.0 - float(z["y"])
            except (KeyError, TypeError, ValueError):
                continue
            side = str(z.get("side") or "neutral")
            color = QColor(rmv.ZONE_SIDE_COLORS.get(side, rmv.ZONE_SIDE_COLORS["neutral"]))
            ring = np.array([(x, y + ry, Z_ZONE), (x + rx, y, Z_ZONE),
                             (x, y - ry, Z_ZONE), (x - rx, y, Z_ZONE),
                             (x, y + ry, Z_ZONE)], dtype=np.float32)
            line = self._add(gfx.Line(
                gfx.Geometry(positions=ring),
                gfx.LineMaterial(thickness=2.0, thickness_space="screen",
                                 color=_rgba(color))))
            label = str(z.get("label") or "").strip()
            text = None
            if label:
                text = self._add(gfx.Text(
                    text=label[:2], font_size=11, screen_space=True,
                    family=_CJK_FAMILY,
                    material=gfx.TextMaterial(color="#ffffff")))
                text.local.position = (x, y, Z_ZONE + 0.002)
            self._gl_zones.append((z, line, color, text))

    def _build_dead_marks(self) -> None:
        dw, dh = self._map_size()
        if dw <= 0 or dh <= 0:
            return
        sx, sy = rmv.DEAD_MARK_HALF / dw, rmv.DEAD_MARK_HALF / dh
        pos: list[float] = []
        cols: list[float] = []
        for rec in self._playback_recs:
            if not getattr(rec, "dead", False) or not rec.points:
                continue
            side = getattr(rec, "side", "")
            col = _rgba(rmv.COLOR_DEAD_MARK_A if side == "A" else rmv.COLOR_DEAD_MARK_B)
            x, y = rec.points[-1].x, 1.0 - rec.points[-1].y
            for a, b in (((x - sx, y - sy), (x + sx, y + sy)),
                         ((x - sx, y + sy), (x + sx, y - sy))):
                pos += [a[0], a[1], Z_MARK, b[0], b[1], Z_MARK]
                cols += [*col, *col]
        if not pos:
            return
        self._add(gfx.Line(
            gfx.Geometry(positions=np.array(pos, dtype=np.float32).reshape(-1, 3),
                         colors=np.array(cols, dtype=np.float32).reshape(-1, 4)),
            gfx.LineSegmentMaterial(thickness=rmv.DEAD_MARK_PEN + 1.0,
                                    thickness_space="screen", color_mode="vertex"),
            render_order=2))

    # ---------------------------------------------------- 图元: 玩家/光标/悬停
    def _dot(self, color: QColor, radius_px: float) -> gfx.Points:
        """屏幕像素大小的实心点(用 Points: Mesh 在当前 wgpu 下不可用)。"""
        pts = gfx.Points(gfx.Geometry(positions=np.zeros((1, 3), dtype=np.float32)),
                         gfx.PointsMaterial(size=radius_px * 2.0, size_space="screen",
                                            color=_rgba(color)))
        pts.geometry.positions.data[0] = (0.0, 0.0, 0.0)
        return pts

    def _build_player(self) -> None:
        self._gl_player = None
        self._gl_player_dir = None
        if self._player is None:
            return
        self._gl_player = self._add(self._dot(rmv.COLOR_PLAYER, rmv.PLAYER_DOT_R * 1.6))
        self._gl_player.local.position = (self._player[0], 1.0 - self._player[1], Z_PLAYER)
        self._gl_player_dir = self._add(gfx.Line(
            gfx.Geometry(positions=np.zeros((2, 3), dtype=np.float32)),
            gfx.LineMaterial(thickness=2.0, thickness_space="screen",
                             color=_rgba(PLAYER_DIR_COLOR))))

    def _build_cursor(self) -> None:
        self._gl_cursor = None
        if not self._playback_recs:
            return
        self._gl_cursor = self._add(self._dot(QColor("#f8fafc"), CURSOR_DOT_R))

    def _build_hover(self) -> None:
        self._hover_obj = None
        if not self._hover_text:
            return
        self._hover_obj = self._add(gfx.Text(
            text=self._hover_text, font_size=13, screen_space=True,
            family=_CJK_FAMILY,
            material=gfx.TextMaterial(color="#e2e8f0")))
        # 贴着可见区域左下角(归一化坐标 → 按当前相机换算)
        r = self._map_rect()
        dw, dh = self._map_size()
        w, h = self._canvas.get_logical_size()
        if dw > 0 and dh > 0:
            self._hover_obj.local.position = (
                -r.left() / dw + 6.0 / dw, 1.0 - (h - r.top()) / dh + 6.0 / dh, Z_PLAYER + 0.004)

    # ---------------------------------------------------- 每帧: 进度/闪烁/位置
    def _apply_progress(self) -> None:
        """回放进度: 只改颜色切片(1~2 ms), 不重建几何。"""
        head = self._playhead
        playing = {id(r) for r in self._playback_recs}
        for tr in self._gl_tracks:
            if tr.colors is None or tr.play is None:
                continue
            pts = tr.rec.points
            end = pts[-1].t if pts else 0.0
            dead = bool(getattr(tr.rec, "dead", False)) and self._dead_dim and head >= end
            if dead != tr.dead:
                tr.dead = dead
                if tr.under is not None:
                    tr.under.material.color = _rgba(
                        rmv.COLOR_DEAD_ROUTE if dead else rmv.COLOR_UNPLAYED)
            if dead:
                if tr.drawn:
                    tr.play.geometry.colors.data[:] = 0.0
                    tr.play.geometry.colors.update_full()
                    tr.drawn = 0
                continue
            if id(tr.rec) in playing:
                k = _bisect_time(pts, head)
                if k > tr.drawn:
                    arr = tr.play.geometry.colors.data
                    arr[tr.drawn:k] = tr.colors[tr.drawn:k]
                    tr.play.geometry.colors.update_range(tr.drawn, k - tr.drawn)
                    tr.drawn = k

    def _apply_anim(self) -> None:
        """闪烁 / 玩家位置 / 回放光标(每帧都变, 但都只改 uniform 或几个点)。"""
        if self._zones and self._show_zones and getattr(self, "_gl_zones", None):
            for z, line, color, _text in self._gl_zones:
                blink = int(z.get("blink") or 0)
                if blink <= 0:
                    line.material.color = _rgba(color)
                    continue
                period = rmv.BLINK_PERIODS.get(blink, rmv.BLINK_PERIODS[1])
                t = self._blink_t % period
                bv = math.exp(-((5.0 * t - 2.0) ** 4))
                k = rmv.BLINK_DIM + (1.0 - rmv.BLINK_DIM) * bv
                c = rmv._blend(rmv.ZONE_DARK, (color.red(), color.green(), color.blue()), k)
                line.material.color = (c.redF(), c.greenF(), c.blueF(), 1.0)

        if getattr(self, "_gl_player", None) is not None and self._player is not None:
            x, y = self._player[0], 1.0 - self._player[1]
            self._gl_player.local.position = (x, y, Z_PLAYER)
            line = getattr(self, "_gl_player_dir", None)
            ang = self._dir_angle(*self._player_dir) if self._player_dir else None
            if line is not None:
                if ang is None:
                    line.visible = False
                else:
                    line.visible = True
                    dw, dh = self._map_size()
                    ca, sa = math.cos(ang), math.sin(ang)
                    # 长度是像素量, 换算成归一化单位(x/y 分别除, 角度已在像素空间算好)
                    ln = (rmv.PLAYER_DIR_BACK + rmv.PLAYER_DIR_H)
                    tip_x = x + ln * ca / dw if dw else x
                    tip_y = y + ln * sa / dh if dh else y
                    arr = line.geometry.positions.data
                    arr[0] = (x, y, Z_PLAYER)
                    arr[1] = (tip_x, tip_y, Z_PLAYER)
                    line.geometry.positions.update_full()

        if getattr(self, "_gl_cursor", None) is not None and self._playback_recs:
            pt = _point_at(self._playback_recs[0].points, self._playhead)
            if pt is None:
                self._gl_cursor.visible = False
            else:
                self._gl_cursor.visible = True
                self._gl_cursor.local.position = (pt.x, 1.0 - pt.y, Z_PLAYER)

        # 悬停浮层文本(内容随鼠标变, 就地改 Text 即可, 不必重建对象)
        if self._hover_text != getattr(self, "_gl_hover_text", None):
            self._gl_hover_text = self._hover_text
            if self._hover_obj is None:
                self._build_hover()
            elif not self._hover_text:
                self._hover_obj.remove()
                self._gl_objs = [o for o in self._gl_objs if o is not self._hover_obj]
                self._hover_obj = None
            else:
                self._hover_obj.text = self._hover_text


def _bisect_time(pts, head: float) -> int:
    """`t <= head` 的点数(与软件版同一判定)。"""
    lo, hi = 0, len(pts)
    while lo < hi:
        mid = (lo + hi) // 2
        if pts[mid].t <= head:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _point_at(pts, head: float):
    """回放到 `head` 时刻的插值位置(与软件版 point_at 同义, 这里只要位置)。"""
    if not pts:
        return None
    k = _bisect_time(pts, head)
    if k <= 0:
        return pts[0]
    if k >= len(pts):
        return pts[-1]
    a, b = pts[k - 1], pts[k]
    span = b.t - a.t
    if span <= 0:
        return b
    t = (head - a.t) / span
    return type(a)(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t,
                   head, a.speed + (b.speed - a.speed) * t)
