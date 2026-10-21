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
import pylinalg as la
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
BG_COLOR = rmv.COLOR_BG.name() if isinstance(rmv.COLOR_BG, QColor) else "#0f172a"
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

# z 层次(归一化地图坐标, 相机看向 -z)。
#
# **注意: z 不参与排序**。2D 叠加的顺序完全由 `render_order` 决定(见 `_add`),
# 因为 pygfx 的 `depth_compare` 默认是严格的 "<", 两个对象放在同一 z 上时后画的
# 那个会被**整个丢掉**(实测: 回放时未播放的灰底衬把彩色的已播放段全盖掉,
# 画面上只剩一片灰线)。z 值现在只用于保持层次语义、便于排查。
Z_MAP = -0.02
Z_TRACK = 0.0
Z_UNDER = 0.004
Z_PLAY = 0.008
Z_SPAWN = 0.012
Z_ZONE = 0.016
Z_MARK = 0.022
Z_PLAYER = 0.026

# ---------------------------------------------------------------- 绘制顺序
# pygfx 的排序键是 `(render_queue, group_order, render_order, 距离)`,
# **render_queue 在 render_order 之前**。所以要让 `render_order` 成为唯一的排序键,
# 必须把所有制材的 render_queue 统一(并关掉深度测试)。
# 下面这些常量与软件版 `_paint()` 的调用顺序一一对应 —— 改这里就等于改叠放层次。
RENDER_QUEUE = 3000
O_MAP = -100
O_BORDER = -90
O_ZONE = -80          # 战区填充(在轨迹**之下**, 与软件版一致)
O_ZONE_EDGE = -79
O_ZONE_LABEL = -78
O_SPAWN = -70         # 重生方块(在轨迹之下)
O_UNDER = 10          # 回放未播放底衬
O_TRACK = 20          # 轨迹本体
O_MARK = 30           # 起/终点圆环
O_MARK_DOT = 31       # 起点的实心点
O_LABEL_BOX = 32      # 时间标签的深色底框
O_LABEL_EDGE = 33     # 时间标签的彩色边框
O_LABEL_TEXT = 34     # 时间标签文字
O_DEAD = 40           # 阵亡叉
O_CURSOR_DOT = 50     # 回放光标白点
O_CURSOR_DIR = 51     # 光标朝向线
O_PLAYER_RING = 60
O_PLAYER_DOT = 61
O_PLAYER_DIR = 62
O_HOVER_RING = 70
O_HOVER_BOX = 80
O_HOVER_TEXT = 81
O_STATUS = 90


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


def _fill_segs(dst: np.ndarray, xy: np.ndarray, dst_seg: int, count: int, z: float) -> None:
    """把 xy[0..count] 的相邻点写成“段对”, 写入 dst 里从第 dst_seg 段开始。

    `LineSegmentMaterial` 要的是 **(2N,3) 成对顶点**, 不是 strip。用段对而不是 strip
    是必需的选择: strip 的厚线会给每个拐点生成 miter 接头, 而接头长度 = 线宽/sin(θ/2),
    录制时车停着不动、相邻采样点几乎重合(θ接近180°) → 接头长度爆炸, 画面上每个
    采样点都会刺出一根白线(用户 2026-10-21 截图)。段与段独立就没有接头。
    """
    if count <= 0:
        return
    i = 2 * dst_seg
    j = i + 2 * count
    dst[i:j:2, 0] = xy[:count, 0]
    dst[i:j:2, 1] = xy[:count, 1]
    dst[i + 1:j:2, 0] = xy[1:count + 1, 0]
    dst[i + 1:j:2, 1] = xy[1:count + 1, 1]
    dst[i:j, 2] = z


def _seg_colors(pts, i0: int, i1: int, vmax: float) -> np.ndarray:
    """第 [i0, i1) 段的颜色(按段中点速度), 返回 (2*(i1-i0), 4)。"""
    out = np.empty((2 * (i1 - i0), 4), dtype=np.float32)
    cache: dict[int, tuple] = {}
    for j, i in enumerate(range(i0, i1)):
        a, b = pts[i], pts[i + 1]
        k = bucket_index((a.speed + b.speed) * 0.5, vmax)
        c = cache.get(k)
        if c is None:
            c = _rgba(speed_color((k + 0.5) / 24 * vmax, vmax))
            cache[k] = c
        out[2 * j] = c
        out[2 * j + 1] = c
    return out


class _Track:
    """一条线路的 GPU 对象(段对几何) + 已填段数。

    容量(`cap`)预留一段余量: 录制时点数一直在涨, 有了预留就能只写新段 +
    `update_range`, 不必每帧重建整个几何(重建 64 条线路实测 150 ms 级)。
    """

    __slots__ = ("rec", "pos", "colors", "vmax", "under", "play", "nseg", "cap",
                 "drawn", "dead")

    def __init__(self, rec, pos, colors, vmax, under, play, nseg, cap) -> None:
        self.rec = rec
        self.pos = pos
        self.colors = colors
        self.vmax = vmax
        self.under = under
        self.play = play
        self.nseg = nseg
        self.cap = cap
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
        # 关掉默认的 2x 超采样(SSAA): `thickness_space="screen"` 的线宽按**内部像素**
        # 算, 开着 SSAA 时内部是逻辑尺寸的 2 倍, 于是 2.6 px 的线看起来只有 1.3 px
        # (实测对比图里 GPU 的线与阵亡叉都比软件版细一半)。关掉后线宽准确,
        # 每帧像素量也降到 1/4。线本身的抗锯齿用 `aa=True` 就够。
        self._renderer.pixel_scale = 1
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
            # 底图也是 2D 叠加的一层(必须关深度测试, 否则它会和别的层抢 z)
            self._plane.material.depth_test = False
            self._plane.material.render_queue = RENDER_QUEUE
            self._plane.render_order = O_MAP
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
        """把 zoom/pan 换算成相机的可见矩形(归一化地图坐标, y 朝上)。

        **必须用控件尺寸**(`self.width()/height()`), 不能用 canvas 的逻辑尺寸:
        父类 `_map_rect()` / `map_pos()` 都是按控件尺寸算的, 两边不一致就会错位
        (实测: 离屏画布 1400x900 + 控件 640x480 时画面整体偏移)。
        """
        r = self._map_rect()
        dw, dh = self._map_size()
        w, h = float(self.width()), float(self.height())
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
            return tuple(getattr(r, "route_id", id(r)) for r in recs)

        live = self._live
        zones = ()
        if self._show_zones:
            zones = tuple((z.get("label"), z.get("side"), round(float(z.get("x", 0)), 6),
                           round(float(z.get("y", 0)), 6)) for z in self._zones)
        return (sig(self._visible), sig(self._highlight),
                getattr(live, "route_id", "") if live else None,
                sig(self._live_multi), sig(self._playback_recs),
                self._show_zones, zones, self._show_spawns, self._spawns_ver,
                self._map_digest, self._hover_text, self._status)

    def _clear_objects(self) -> None:
        for obj in self._gl_objs:
            obj.remove()
        self._gl_objs = []
        self._gl_tracks = []
        self._hover_obj = None

    def _add(self, obj, order: float = 0.0):
        """加入场景, 并把整个场景统一成“画家算法”的 2D 叠加。

        为什么要这么做(源码依据 `renderers/wgpu/engine/renderer.py`):

        1. 排序键是 `(render_queue, group_order, render_order, 距离)` ——
           **render_queue 比 render_order 优先**, 所以不同 render_queue 的对象
           无论怎么设 render_order 都不能按我们的意思叠;
        2. `Material.depth_compare` 默认是严格的 `"<"`, 同一 z 上后画的对象会被
           深度测试**整个丢掉**。之前未播放底衬和已播放彩色段共用 `Z_PLAY`,
           结果灰底衬把彩线全盖了(用户 2026-10-22 截图)。

        因此: 关掉深度测试 + 统一 render_queue, 让 `render_order` 成为唯一排序键,
        与 QPainter 的调用顺序一一对应。
        """
        mat = getattr(obj, "material", None)
        if mat is not None:
            mat.depth_test = False
            mat.render_queue = RENDER_QUEUE
        obj.render_order = order
        self._scene.add(obj)
        self._gl_objs.append(obj)
        return obj

    def _sync_objects(self) -> None:
        key = self._content_key()
        if key != self._gl_key:
            self._gl_key = key
            self._rebuild_objects()
        elif getattr(self, "_gl_tracks", None):
            self._extend_tracks()          # 录制中点涨了 → 只补新段
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
        # 4) 重生方块 / 战区 / 起终点标记 / 阵亡叉 / 玩家 / 光标
        self._build_spawns()
        self._build_zones()
        self._build_marks()
        self._build_dead_marks()
        self._build_player()
        self._build_cursor()
        self._build_border()
        self._build_hover()
        self._build_status()

    def _make_track(self, rec, *, colored: bool, under: bool) -> None:
        pts = getattr(rec, "points", None)
        if not pts or len(pts) < 2:
            return
        n = len(pts)
        segs = n - 1
        cap = segs + max(64, segs // 4)          # 预留容量(增量录制用)
        xy = np.empty((n, 2), dtype=np.float32)
        xy[:, 0] = [p.x for p in pts]
        xy[:, 1] = [1.0 - p.y for p in pts]
        z = Z_PLAY if under else Z_TRACK
        pos = np.empty((cap * 2, 3), dtype=np.float32)
        _fill_segs(pos, xy, 0, segs, z)
        pos[segs * 2:] = (xy[-1, 0], xy[-1, 1], z)   # 未用段: 零长, 不会画出来
        if not colored:
            line = self._add(gfx.Line(
                gfx.Geometry(positions=pos),
                gfx.LineSegmentMaterial(thickness=2.0, thickness_space="screen",
                                        color=_rgba(rmv.COLOR_HISTORY))), O_TRACK)
            self._gl_tracks.append(_Track(rec, pos, None, 0.0, line, None, segs, cap))
            return
        vmax = self._vmax(rec)
        colors = np.zeros((cap * 2, 4), dtype=np.float32)
        colors[:segs * 2] = _seg_colors(pts, 0, segs, vmax)
        under_obj = None
        if under:
            under_obj = self._add(gfx.Line(
                gfx.Geometry(positions=pos),
                gfx.LineSegmentMaterial(thickness=2.0, thickness_space="screen",
                                        color=_rgba(rmv.COLOR_UNPLAYED))), O_UNDER)
        play = self._add(gfx.Line(
            # 回放(under=True)的“已播放”从空开始, 由进度逐段填色;
            # 录制/预览的线路本身就整条可见, 直接带着颜色开始
            gfx.Geometry(positions=pos,
                         colors=np.zeros_like(colors) if under else colors.copy()),
            gfx.LineSegmentMaterial(thickness=rmv.PLAY_PEN, thickness_space="screen",
                                    color_mode="vertex", aa=True,
                                    # 显式 blend: 顶点 alpha 都是 1 时 auto 会把它
                                    # 当成不透明材质, 排到另一个 render_queue 去
                                    alpha_mode="blend")), O_TRACK)
        self._gl_tracks.append(_Track(rec, pos, colors, vmax, under_obj, play, segs, cap))

    def _extend_tracks(self) -> None:
        """录制中点数在涨 → 只把**新段**写进去并 update_range(不重建几何)。

        容量不够或速度基准涨太多(颜色桶全变)时把 `_gl_key` 置空, 下一帧整层重建。
        """
        for tr in self._gl_tracks:
            pts = tr.rec.points
            n = len(pts)
            if n < 2:
                continue
            if abs(self._vmax(tr.rec) - tr.vmax) > rmv.VMAX_REBUILD_RATIO * max(tr.vmax, 1e-6):
                self._gl_key = None       # 基准变了, 颜色全要重算
                return
            segs = n - 1
            if segs <= tr.nseg:
                continue
            if segs > tr.cap:
                self._gl_key = None
                return
            i0, i1 = tr.nseg, segs
            xy = np.empty(((i1 - i0) + 1, 2), dtype=np.float32)
            for j, i in enumerate(range(i0, i1 + 1)):
                p = pts[i]
                xy[j, 0] = p.x
                xy[j, 1] = 1.0 - p.y
            _fill_segs(tr.pos, xy, i0, i1 - i0, tr.pos[0, 2])
            tr.play.geometry.positions.update_range(2 * i0, 2 * (i1 - i0))
            if tr.colors is not None:
                cols = _seg_colors(pts, i0, i1, tr.vmax)
                tr.colors[2 * i0:2 * i1] = cols
                if tr.under is None and not tr.dead:
                    tr.play.geometry.colors.data[2 * i0:2 * i1] = cols
                    tr.play.geometry.colors.update_range(2 * i0, 2 * (i1 - i0))
                    tr.drawn = max(tr.drawn, i1)
            tr.nseg = i1

    # ---------------------------------------------------- 图元: 重生/战区/叉
    def _build_spawns(self) -> None:
        """重生方块: 深色垫底(粗) + 归属色(细), 与软件版一致。"""
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
        arr = np.array(pos, dtype=np.float32).reshape(-1, 3)
        # 深色垫底: 比方块粗 1.4px(软件版 SPAWN_PEN + 1.4), 浅色底图上也能看清
        self._add(gfx.Line(
            gfx.Geometry(positions=arr.copy()),
            gfx.LineSegmentMaterial(thickness=rmv.SPAWN_PEN + 1.4,
                                    thickness_space="screen",
                                    color=_rgba(QColor(15, 23, 42, 200)))), O_SPAWN)
        self._add(gfx.Line(
            gfx.Geometry(positions=arr,
                         colors=np.array(cols, dtype=np.float32).reshape(-1, 4)),
            gfx.LineSegmentMaterial(thickness=rmv.SPAWN_PEN, thickness_space="screen",
                                    color_mode="vertex")), O_SPAWN)

    def _build_zones(self) -> None:
        """战区: 菱形**填充** + 深色 3.4px 描边 + 白色 1.6px 描边 + 中心字母。

        与软件版 `_draw_zones` 逐项对应(颜色/线宽/字体都一样)。
        填充用**自造 Geometry**: 菱形的四个顶点是屏幕上的上右下左, 而归一化坐标的
        宽高不等比, 所以不能用“正方形转 45°”(那会把菱形画歪)。
        注意 `indices` 必须是 **(N,3)** 形状, 传一维数组会报 shader 校验错误。
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
            label_color = "#0f172a" if side == "neutral" else "#ffffff"
            quad = np.array([(x, y + ry, Z_ZONE), (x + rx, y, Z_ZONE),
                             (x, y - ry, Z_ZONE), (x - rx, y, Z_ZONE)], dtype=np.float32)
            fill = self._add(gfx.Mesh(
                gfx.Geometry(positions=quad,
                             indices=np.array([[0, 1, 2], [0, 2, 3]], np.uint32)),
                gfx.MeshBasicMaterial(color=_rgba(color), side="both")), O_ZONE)
            ring = np.array([(x, y + ry, Z_ZONE), (x + rx, y, Z_ZONE),
                             (x, y - ry, Z_ZONE), (x - rx, y, Z_ZONE),
                             (x, y + ry, Z_ZONE)], dtype=np.float32)
            self._add(gfx.Line(  # 深色描边(浅色底图上也看得清)
                gfx.Geometry(positions=ring.copy()),
                gfx.LineMaterial(thickness=3.4, thickness_space="screen",
                                 color=_rgba(QColor(15, 23, 42, 210)))), O_ZONE_EDGE)
            self._add(gfx.Line(  # 白色描边(COLOR_ZONE)
                gfx.Geometry(positions=ring),
                gfx.LineMaterial(thickness=1.6, thickness_space="screen",
                                 color=_rgba(rmv.COLOR_ZONE))), O_ZONE_EDGE)
            label = str(z.get("label") or "").strip()
            text = None
            if label:                        # 软件版: Consolas 9 bold, 区域中心
                text = self._add(gfx.Text(
                    text=label[:2], font_size=9, screen_space=True,
                    family="Consolas", anchor="middle-center",
                    material=gfx.TextMaterial(color=label_color)), O_ZONE_LABEL)
                text.local.position = (x, y, Z_ZONE + 0.002)
            self._gl_zones.append((z, fill, color, text))

    def _build_marks(self) -> None:
        """轨迹上的起/终点标记: 圆环(8px) + 起点实心点(2.5px) + 时间数字。

        对应软件版 `_draw_route` 尾部的 marks 循环与 `_draw_play_marks`:
        所有可见轨迹都画, 回放时未到达的标记 alpha=80。
        """
        self._gl_marks = None
        self._gl_mark_info = []
        dw, dh = self._map_size()
        if dw <= 0 or dh <= 0:
            return
        recs = self._drawn_records()
        if not recs:
            return
        # 多条轨迹同时显示时时间标签会完全重叠(软件版同一规则:
        #   labels = len(visible) + len(highlight ∪ live) <= 1)
        if self._playback_recs:
            labels = len(self._playback_recs) <= 1
        else:
            hi = list(self._highlight)
            if self._live is not None and all(r is not self._live for r in hi):
                hi.append(self._live)
            labels = len(self._visible) + len(hi) <= 1
        pos: list[float] = []
        cols: list[float] = []
        info: list[tuple] = []
        dots: list[tuple[float, float, float]] = []
        dot_cols: list[float] = []
        for rec in recs:
            for mk in getattr(rec, "marks", None) or []:
                color = QColor({"start": rmv.COLOR_START,
                                "stop": rmv.COLOR_STOP}.get(mk.kind, rmv.COLOR_MARK))
                radius = 8.0 if mk.kind in ("start", "stop") else 6.0
                x, y = float(mk.x), 1.0 - float(mk.y)
                ring = self._ring(x, y, radius / dw, radius / dh, Z_MARK)
                v0 = len(cols)
                for i in range(len(ring) - 1):
                    a, b = ring[i], ring[i + 1]
                    pos += [float(a[0]), float(a[1]), Z_MARK,
                            float(b[0]), float(b[1]), Z_MARK]
                rgba = _rgba(color)
                cols += list(rgba) * (len(ring) - 1) * 2
                info.append((float(mk.t), v0, len(cols), rgba))
                if mk.kind == "start":
                    dots.append((x, y, Z_MARK))
                    dot_cols += list(rgba)
                if labels:
                    self._add_time_label(x, y, mk.t, color, dw, dh)
        self._gl_mark_info = info
        if not pos:
            return
        self._mark_line = self._add(gfx.Line(
            gfx.Geometry(positions=np.array(pos, dtype=np.float32).reshape(-1, 3),
                         colors=np.array(cols, dtype=np.float32).reshape(-1, 4)),
            gfx.LineSegmentMaterial(thickness=2.4, thickness_space="screen",
                                    color_mode="vertex", alpha_mode="blend")), O_MARK)
        if dots:
            # 软件版: 半径 2.5 的实心圆 + 2.4px 同色描边 → 视觉直径 7.4
            self._add(gfx.Points(
                gfx.Geometry(positions=np.array(dots, dtype=np.float32),
                             colors=np.array(dot_cols, dtype=np.float32).reshape(-1, 4)),
                gfx.PointsMaterial(size=7.4, size_space="screen",
                                   color_mode="vertex")), O_MARK_DOT)

    def _add_time_label(self, x: float, y: float, t: float, color: QColor,
                        dw: float, dh: float) -> None:
        """时间标签: 深色底框 + 彩色 1px 边框 + 白色 Consolas 文字。

        与软件版 `_draw_time_label` 同几何: 框在圆环右侧(距中心 11px), 高 16px,
        文字左对齐、垂直居中。宽度用 Consolas 10pt 的近似字宽估(数字等宽)。
        """
        text = rmv.fmt_time(t)
        tw = 6.3 * len(text)
        th = 16.0
        box_w = tw + 6.0
        cx = x + (11.0 + box_w / 2.0 - 3.0) / dw
        cy = y
        w, h = box_w / dw, th / dh
        box = self._add(gfx.Mesh(
            gfx.Geometry(positions=np.array(
                [(cx - w / 2, cy - h / 2, Z_MARK), (cx + w / 2, cy - h / 2, Z_MARK),
                 (cx + w / 2, cy + h / 2, Z_MARK), (cx - w / 2, cy + h / 2, Z_MARK)],
                dtype=np.float32),
                indices=np.array([[0, 1, 2], [0, 2, 3]], np.uint32)),
            gfx.MeshBasicMaterial(color=_rgba(QColor(15, 23, 42, 200)), side="both")),
            O_LABEL_BOX)
        edge = np.array([(cx - w / 2, cy - h / 2, Z_MARK), (cx + w / 2, cy - h / 2, Z_MARK),
                         (cx + w / 2, cy + h / 2, Z_MARK), (cx - w / 2, cy + h / 2, Z_MARK),
                         (cx - w / 2, cy - h / 2, Z_MARK)], dtype=np.float32)
        segs = np.empty((8, 3), dtype=np.float32)
        segs[0::2] = edge[:-1]
        segs[1::2] = edge[1:]
        self._add(gfx.Line(
            gfx.Geometry(positions=segs),
            gfx.LineSegmentMaterial(thickness=1.0, thickness_space="screen",
                                    color=_rgba(color), alpha_mode="blend")), O_LABEL_EDGE)
        t_obj = self._add(gfx.Text(
            text=text, font_size=10, screen_space=True, family="Consolas",
            anchor="middle-center",
            material=gfx.TextMaterial(color="#f8fafc")), O_LABEL_TEXT)
        t_obj.local.position = (cx, cy, Z_MARK)

    def _build_dead_marks(self) -> None:
        """阵亡叉: 画在线路末端, **只有回放越过阵亡时刻后才显示**。

        软件版是在 `_draw_dead_mark` 里逐帧判 `_dead_now()` 的 —— 所以这里也把
        “什么时候显示”交给 `_apply_anim`(按 alpha 开关), 不能建完就全部显示:
        否则一回放就把所有阵亡位置提前剧透出来(用户 2026-10-22 截图: 右侧多出
        一大片红叉)。
        """
        self._gl_dead = None
        self._gl_dead_info = []
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
            v0 = len(cols)
            for a, b in (((x - sx, y - sy), (x + sx, y + sy)),
                         ((x - sx, y + sy), (x + sx, y - sy))):
                pos += [a[0], a[1], Z_MARK, b[0], b[1], Z_MARK]
                cols += [*col, *col]
            self._gl_dead_info.append((rec, v0, len(cols)))
        if not pos:
            return
        self._gl_dead = self._add(gfx.Line(
            gfx.Geometry(positions=np.array(pos, dtype=np.float32).reshape(-1, 3),
                         colors=np.array(cols, dtype=np.float32).reshape(-1, 4)),
            gfx.LineSegmentMaterial(thickness=rmv.DEAD_MARK_PEN,
                                    thickness_space="screen", color_mode="vertex",
                                    alpha_mode="blend")), O_DEAD)

    # ---------------------------------------------------- 图元: 玩家/光标/悬停
    def _dot(self, color: QColor, radius_px: float) -> gfx.Points:
        """屏幕像素大小的实心点(用 Points: Mesh 在当前 wgpu 下不可用)。"""
        pts = gfx.Points(gfx.Geometry(positions=np.zeros((1, 3), dtype=np.float32)),
                         gfx.PointsMaterial(size=radius_px * 2.0, size_space="screen",
                                            color=_rgba(color)))
        pts.geometry.positions.data[0] = (0.0, 0.0, 0.0)
        return pts

    @staticmethod
    def _ring(x: float, y: float, rx: float, ry: float, z: float,
              seg: int = 16) -> np.ndarray:
        """圆环折线顶点(闭合)。"""
        a = np.linspace(0.0, 2.0 * math.pi, seg + 1, dtype=np.float32)
        out = np.empty((seg + 1, 3), dtype=np.float32)
        out[:, 0] = x + np.cos(a) * rx
        out[:, 1] = y + np.sin(a) * ry
        out[:, 2] = z
        return out

    def _build_player(self) -> None:
        """玩家标记: 空心圈(半透明蓝) + 实心点 + **蓝色 ^**(带深色垫底), 与软件版一致。"""
        self._gl_player = self._gl_player_ring = None
        self._gl_player_dir = self._gl_player_dir_dark = None
        if self._player is None:
            return
        dw, dh = self._map_size()
        if dw <= 0 or dh <= 0:
            return
        x, y = self._player[0], 1.0 - self._player[1]
        self._gl_player_ring = self._add(gfx.Line(
            gfx.Geometry(positions=self._ring(x, y, rmv.PLAYER_RING_R / dw,
                                              rmv.PLAYER_RING_R / dh, Z_PLAYER)),
            gfx.LineMaterial(thickness=1.0, thickness_space="screen",
                             color=_rgba(QColor(56, 189, 248, 90)))), O_PLAYER_RING)
        # 软件版: 半径 4 的实心圆 + 2px 同色描边 → 视觉直径 10
        self._gl_player = self._add(self._dot(rmv.COLOR_PLAYER, 5.0), O_PLAYER_DOT)
        self._gl_player.local.position = (x, y, Z_PLAYER)
        # ^ 是两条线段(4 个顶点): 深色垫底 + 蓝色
        self._gl_player_dir_dark = self._add(gfx.Line(
            gfx.Geometry(positions=np.zeros((4, 3), dtype=np.float32)),
            gfx.LineSegmentMaterial(thickness=4.0, thickness_space="screen",
                                    color=_rgba(QColor(15, 23, 42, 220)))), O_PLAYER_DIR)
        self._gl_player_dir = self._add(gfx.Line(
            gfx.Geometry(positions=np.zeros((4, 3), dtype=np.float32)),
            gfx.LineSegmentMaterial(thickness=2.0, thickness_space="screen",
                                    color=_rgba(rmv.COLOR_PLAYER))), O_PLAYER_DIR)

    def _build_cursor(self) -> None:
        """回放光标: 每条线路一个白点 + 白色朝向线(带深色垫底), 与软件版一致。"""
        self._gl_cursor = self._gl_cursor_dir = self._gl_cursor_dir_dark = None
        n = len(self._playback_recs)
        if not n:
            return
        self._gl_cursor = self._add(gfx.Points(
            gfx.Geometry(positions=np.zeros((n, 3), dtype=np.float32)),
            gfx.PointsMaterial(size=CURSOR_DOT_R * 2.0, size_space="screen",
                               color=_rgba(QColor("#f8fafc")))), O_CURSOR_DOT)
        self._gl_cursor_dir_dark = self._add(gfx.Line(
            gfx.Geometry(positions=np.zeros((2 * n, 3), dtype=np.float32)),
            gfx.LineSegmentMaterial(thickness=4.2, thickness_space="screen",
                                    color=_rgba(QColor(15, 23, 42, 210)))), O_CURSOR_DIR)
        self._gl_cursor_dir = self._add(gfx.Line(
            gfx.Geometry(positions=np.zeros((2 * n, 3), dtype=np.float32)),
            gfx.LineSegmentMaterial(thickness=2.0, thickness_space="screen",
                                    color=_rgba(rmv.COLOR_DIR_LINE))), O_CURSOR_DIR)

    def _build_border(self) -> None:
        """地图边框(与软件版 `_draw_base` 末尾那条细框一致)。"""
        xy = np.array([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.0, 0.0)],
                      dtype=np.float32)
        pos = np.empty((len(xy), 3), dtype=np.float32)
        pos[:, :2] = xy
        pos[:, 2] = Z_MAP + 0.001
        self._add(gfx.Line(gfx.Geometry(positions=pos),
                           gfx.LineMaterial(thickness=1.0, thickness_space="screen",
                                            color=_rgba(rmv.COLOR_GRID))), O_BORDER)

    def _build_hover(self) -> None:
        """悬停: 命中点上的白环 + 左下角深色浮层(背景框 + 多行文字)。

        对应软件版的 `_draw_hover_mark`(深色 4.0px 底环 + 白色 1.8px 环, 半径 5)
        与 `_draw_hover`(左下角 alpha=215 的深色框, 内边距 7/4)。
        """
        self._hover_obj = None
        dw, dh = self._map_size()
        if dw <= 0 or dh <= 0:
            return
        # 1) 命中点白环(线条已被隐藏/删除时不画, 与软件版一致)
        pt, rec = self._hover_pt, self._hover_rec
        if pt is not None and rec is not None and any(
                r is rec for r in self._drawn_records()):
            ring = self._ring(float(pt.x), 1.0 - float(pt.y),
                              5.0 / dw, 5.0 / dh, Z_PLAYER + 0.006)
            segs = np.empty((2 * (len(ring) - 1), 3), dtype=np.float32)
            segs[0::2] = ring[:-1]
            segs[1::2] = ring[1:]
            for thick, col in ((4.0, QColor(15, 23, 42, 200)), (1.8, QColor("#f8fafc"))):
                self._add(gfx.Line(
                    gfx.Geometry(positions=segs.copy()),
                    gfx.LineSegmentMaterial(thickness=thick, thickness_space="screen",
                                            color=_rgba(col), alpha_mode="blend")),
                    O_HOVER_RING)
        text = self._hover_text
        if not text:
            return
        # 2) 左下角浮层: 与软件版同尺寸(字号 9, 行高~17, 内边距 7/4)
        lines = text.split("\n")
        th = 17.0
        tw = 0.0
        for ln in lines:
            tw = max(tw, sum(9.0 if ord(ch) > 127 else 5.6 for ch in ln))
        box_w = min(tw + 14.0, max(80.0, float(self.width() - 20.0)))
        box_h = th * len(lines) + 8.0
        r = self._map_rect()
        x, y = 10.0, float(self.height()) - box_h - 12.0
        # 世界坐标 y 是“向上”的(轨迹用 1-y), 所以控件像素要翻一下
        nx = (x - r.left()) / dw
        ny = 1.0 - (y - r.top()) / dh
        w, h = box_w / dw, box_h / dh
        z = Z_PLAYER + 0.008
        box = self._add(gfx.Mesh(
            gfx.plane_geometry(w, h),
            gfx.MeshBasicMaterial(color=_rgba(QColor(15, 23, 42, 215)), side="both")),
            O_HOVER_BOX)
        box.local.position = (nx + w / 2.0, ny - h / 2.0, z)
        # 软件版: 文字左对齐、垂直居中, 内边距左 7 / 上 4;
        # 行高要与 QFontMetrics.height() 一致(9pt ≈ 17px), pygfx 默认 1.2×字号 只有 10.8px
        self._hover_obj = self._add(gfx.Text(
            text="\n".join(lines), font_size=9, screen_space=True,
            family=_CJK_FAMILY, anchor="top-left", line_height=17.0 / 9.0,
            material=gfx.TextMaterial(color="#e2e8f0")), O_HOVER_TEXT)
        self._hover_obj.local.position = (nx + 7.0 / dw, ny - 4.0 / dh, z + 0.002)

    def _build_status(self) -> None:
        """无地图时居中显示提示文字(软件版 `_paint` 里的 `_status`)。"""
        self._gl_status = None
        if self._pixmap is not None or not self._status:
            return
        self._gl_status = self._add(gfx.Text(
            text=self._status, font_size=11, screen_space=True,
            family=_CJK_FAMILY, anchor="middle-center",
            material=gfx.TextMaterial(color=_rgba(QColor("#64748b")))), O_STATUS)
        self._gl_status.local.position = (0.5, 0.5, 0.0)

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
                kseg = max(0, _bisect_time(pts, head) - 1)
                if kseg > tr.drawn:
                    arr = tr.play.geometry.colors.data
                    arr[2 * tr.drawn:2 * kseg] = tr.colors[2 * tr.drawn:2 * kseg]
                    tr.play.geometry.colors.update_range(2 * tr.drawn,
                                                        2 * (kseg - tr.drawn))
                    tr.drawn = kseg

    def _apply_anim(self) -> None:
        """闪烁 / 玩家位置 / 回放光标(每帧都变, 但都只改 uniform 或几个点)。"""
        if self._gl_marks is not None and self._gl_mark_info and self._playback_recs:
            # 回放: 未到达的标记淡到 alpha=80(与软件版 _draw_play_marks 一致)
            arr = self._mark_line.geometry.colors.data
            head = self._playhead
            for t, v0, v1, rgba in self._gl_mark_info:
                a = 80.0 / 255.0 if t > head else rgba[3]
                arr[v0:v1] = (rgba[0], rgba[1], rgba[2], a)
            self._mark_line.geometry.colors.update_full()

        # 阵亡叉: 只在“回放已经越过阵亡时刻”后显示(软件版逐帧判 _dead_now)
        if getattr(self, "_gl_dead", None) is not None and self._gl_dead_info:
            arr = self._gl_dead.geometry.colors.data
            for rec, v0, v1 in self._gl_dead_info:
                show = self._dead_mark and self._dead_now(rec, self._playhead)
                arr[v0:v1, 3] = 1.0 if show else 0.0
            self._gl_dead.geometry.colors.update_full()
        if self._zones and self._show_zones and getattr(self, "_gl_zones", None):
            for z, fill, color, _text in self._gl_zones:
                blink = int(z.get("blink") or 0)
                if blink <= 0:
                    fill.material.color = _rgba(color)
                    continue
                period = rmv.BLINK_PERIODS.get(blink, rmv.BLINK_PERIODS[1])
                t = self._blink_t % period
                bv = math.exp(-((5.0 * t - 2.0) ** 4))
                k = rmv.BLINK_DIM + (1.0 - rmv.BLINK_DIM) * bv
                c = rmv._blend(rmv.ZONE_DARK, (color.red(), color.green(), color.blue()), k)
                fill.material.color = (c.redF(), c.greenF(), c.blueF(), 1.0)

        if getattr(self, "_gl_player", None) is not None and self._player is not None:
            x, y = self._player[0], 1.0 - self._player[1]
            self._gl_player.local.position = (x, y, Z_PLAYER)
            dw, dh = self._map_size()
            if getattr(self, "_gl_player_ring", None) is not None and dw > 0 and dh > 0:
                self._gl_player_ring.geometry.positions.data[:] = self._ring(
                    x, y, rmv.PLAYER_RING_R / dw, rmv.PLAYER_RING_R / dh, Z_PLAYER)
                self._gl_player_ring.geometry.positions.update_full()
            ang = self._dir_angle(*self._player_dir) if self._player_dir else None
            dirs = [getattr(self, "_gl_player_dir_dark", None),
                    getattr(self, "_gl_player_dir", None)]
            if ang is None or not dw or not dh:
                for line in dirs:
                    if line is not None:
                        line.visible = False
            else:
                # ^ 的几何与软件版 `player_dir_points` 一致: 尖点在前, 两只脚在后
                ca, sa = math.cos(ang), math.sin(ang)

                def pt(fwd: float, lat: float) -> tuple[float, float, float]:
                    dx_px = fwd * ca - lat * sa
                    dy_px = fwd * sa + lat * ca
                    return (x + dx_px / dw, y - dy_px / dh, Z_PLAYER)

                tip = pt(rmv.PLAYER_DIR_BACK + rmv.PLAYER_DIR_H, 0.0)
                f1 = pt(rmv.PLAYER_DIR_BACK, -rmv.PLAYER_DIR_W)
                f2 = pt(rmv.PLAYER_DIR_BACK, rmv.PLAYER_DIR_W)
                for line in dirs:
                    if line is None:
                        continue
                    line.visible = True
                    arr = line.geometry.positions.data
                    arr[0], arr[1] = tip, f1
                    arr[2], arr[3] = tip, f2
                    line.geometry.positions.update_full()

        if getattr(self, "_gl_cursor", None) is not None and self._playback_recs:
            dw, dh = self._map_size()
            parr = self._gl_cursor.geometry.positions.data
            darr = self._gl_cursor_dir.geometry.positions.data if self._gl_cursor_dir else None
            ddarr = (self._gl_cursor_dir_dark.geometry.positions.data
                     if self._gl_cursor_dir_dark else None)
            far = -1.0e6
            for i, rec in enumerate(self._playback_recs):
                cur = rmv.point_at(rec.points, self._playhead) if rec.points else None
                visible = cur is not None and not (
                    self._dead_mark and self._dead_now(rec, self._playhead))
                if not visible or not dw or not dh:
                    parr[i] = (far, far, 0.0)
                    if darr is not None:
                        darr[2 * i] = darr[2 * i + 1] = (far, far, 0.0)
                    if ddarr is not None:
                        ddarr[2 * i] = ddarr[2 * i + 1] = (far, far, 0.0)
                    continue
                x, y = cur.x, 1.0 - cur.y
                if not np.isfinite(x) or not np.isfinite(y):
                    x = y = far
                parr[i] = (x, y, Z_PLAYER)
                ang = self._dir_angle(cur.dx, cur.dy)
                if ang is None:
                    if darr is not None:
                        darr[2 * i] = darr[2 * i + 1] = (far, far, 0.0)
                    if ddarr is not None:
                        ddarr[2 * i] = ddarr[2 * i + 1] = (far, far, 0.0)
                    continue
                ca, sa = math.cos(ang), math.sin(ang)
                inset = (4.2 - 2.0) / 2.0
                # 深色垫底两端各内缩半个描边宽(否则线头会露出黑头, 与白点接不上)
                for arr, shrink in ((ddarr, inset), (darr, 0.0)):
                    if arr is None:
                        continue
                    b0 = rmv.CURSOR_DIR_BACK + shrink
                    b1 = rmv.CURSOR_DIR_BACK + rmv.CURSOR_DIR_LEN - shrink
                    arr[2 * i] = (x + b0 * ca / dw, y - b0 * sa / dh, Z_PLAYER)
                    arr[2 * i + 1] = (x + b1 * ca / dw, y - b1 * sa / dh, Z_PLAYER)
            self._gl_cursor.geometry.positions.update_full()
            if self._gl_cursor_dir is not None:
                self._gl_cursor_dir.geometry.positions.update_full()
            if self._gl_cursor_dir_dark is not None:
                self._gl_cursor_dir_dark.geometry.positions.update_full()

        # 悬停浮层的尺寸/内容都随文本变, 而且文本已进 `_content_key`
        # → 由 `_rebuild_objects` 统一重建(这里不再做就地改字的特例)


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
