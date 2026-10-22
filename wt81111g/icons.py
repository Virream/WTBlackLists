"""树形列表用的图标(眼睛 = 轨迹在地图上显示 / 隐藏; 图钉 = 当前基准版本),
以及录制按钮上的“起点/标记/终点”小圆圈。

素材: `assets/icons/eye.png`(显示中)与 `eye_off.png`(已隐藏),
来自开源图标库 **Lucide**(https://github.com/lucide-icons/lucide, ISC 许可,
见 `assets/icons/LICENSE-lucide.txt`), 由 `tools/make_icons.py` 从官方 SVG
光栅化为 **黑色描边 + 透明底** 的 PNG。
图钉图标不依赖外部资源, 直接 `QPainter` 现画(见 `pin_icon`);
起点/标记/终点的圆圈图标同理(见 `circle_icon`) —— 颜色与大小**直接取自画地图的模块**,
保证和地图上的圈一模一样。

为什么这样做:
- 颜色在运行时用 `CompositionMode_SourceIn` 合成 —— 一套黑图就能适配不同状态/主题;
- 只用 **Qt 内建的 PNG 解码器**, 不依赖 `qsvg` 这个会被 `tools/trim_qt.py` 精简掉的图片插件;
- 万一资源缺失(例如打包漏带 `assets`), 自动退化为 `QPainter` 现画的简笔眼睛,
  功能不会因为少一张图而失效。
"""
from __future__ import annotations

import logging
import os

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QIcon, QPainter, QPalette, QPen, QPixmap, QPolygonF,
)
from PyQt6.QtWidgets import QApplication

from .config import resource_path
# 按钮图标就是地图上那三个圈的缩小版 —— 颜色从画地图的模块取, 避免两处各写一遍
from .route_map_view import COLOR_MARK, COLOR_START, COLOR_STOP

log = logging.getLogger("icons")

# 树里显示的逻辑尺寸(px); 实际位图按屏幕缩放比放大, 避免高 DPI 下发虚
ICON_SIZE = 18
_HIDDEN_ALPHA = 110    # 隐藏状态只是“变淡”(太淡会连“有图标”都看不出来)
_FALLBACK_TEXT = QColor("#334155")   # 拿不到调色板(无 QApplication)时的兜底颜色

_cache: dict[tuple[bool, int], QIcon] = {}
_pin_cache: dict[int, QIcon] = {}
_circle_cache: dict[tuple[str, int], QIcon] = {}
_warn_cache: dict[int, QIcon] = {}
_logged: set[str] = set()      # 已记录“加载成功”的资源路径(只记一次)
# 图钉的颜色取调色板的“高亮色”(通常蓝), 拿不到时用这个兜底
_FALLBACK_PIN = QColor("#2563eb")
# 黄色感叹号(提醒“这局还没指定观战玩家”)—— 固定琥珀色, 深浅主题都能看清
_WARN_AMBER = QColor("#f59e0b")
_WARN_MARK = QColor("#1f2937")

# 圆圈图标: 种类 -> (颜色, 地图上的半径, 是否带中心实心点)
# 数值与 `route_map_view` 里画标记的代码一致(半径 8/8/6, 描边 2.4, 半径比 0.3),
# 所以按钮上的圈看着就是地图上那三个圈的缩小版。
_CIRCLE_SPEC: dict[str, tuple[QColor, float, bool]] = {
    "start": (COLOR_START, 8.0, True),    # 起点: 绿圈 + 中心点
    "stop": (COLOR_STOP, 8.0, False),     # 终点: 红圈
    "mark": (COLOR_MARK, 6.0, False),     # 标记: 黄圈(地图上本来就比起终点小一圈)
}
_MAX_RADIUS = 8.0          # 地图上最大的圈半径(用来归一化大小)
_PEN_RATIO = 0.3           # 描边宽度 / 半径(与地图上的 2.4/8 相同)
_CENTER_DOT_RATIO = 2.5 / 8.0


def _pin_color() -> QColor:
    app = QApplication.instance()
    if app is None:
        return QColor(_FALLBACK_PIN)
    color = QColor(app.palette().color(QPalette.ColorRole.Highlight))
    if not color.isValid() or color.lightnessF() > 0.85:
        return QColor(_FALLBACK_PIN)
    return color


def pin_icon(size: int = ICON_SIZE) -> QIcon:
    """图钉图标: 标在“当前基准版本”那一行前面。

    直接画而不是加载 PNG: 少一个资源文件、少一道打包环节, 而且**永远不会**
    因为打包漏带 assets 而消失。
    """
    key = int(size)
    cached = _pin_cache.get(key)
    if cached is not None:
        return cached
    ratio = _device_ratio()
    px = max(8, int(round(size * ratio)))
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        color = _pin_color()
        u = px / 18.0                      # 以 18px 为设计基准
        painter.setPen(QPen(color, max(1.0, 1.5 * u)))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # 钉头(空心圆) + 中间的孔 + 下面的针
        painter.drawEllipse(QRectF(4.6 * u, 2.0 * u, 8.8 * u, 8.8 * u))
        painter.setBrush(color)
        painter.drawEllipse(QRectF(7.7 * u, 5.1 * u, 2.6 * u, 2.6 * u))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(QPolygonF([
            QPointF(8.1 * u, 10.4 * u), QPointF(9.9 * u, 10.4 * u),
            QPointF(9.0 * u, 16.0 * u),
        ]))
    finally:
        painter.end()
    pm.setDevicePixelRatio(ratio)
    icon = QIcon(pm)
    _pin_cache[key] = icon
    return icon


def warn_icon(size: int = ICON_SIZE) -> QIcon:
    """黄色感叹号: 标在“还没指定观战玩家”的全局录制条目上(点它打开修正界面)。

    与图钉一样直接 `QPainter` 现画 —— 不依赖资源文件; 颜色写死琥珀色, 深浅主题
    下都够醒目(这是“需要你来做一步”的提示, 不该跟着主题变淡)。
    """
    key = int(size)
    cached = _warn_cache.get(key)
    if cached is not None:
        return cached
    ratio = _device_ratio()
    px = max(8, int(round(size * ratio)))
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        u = px / 18.0                      # 以 18px 为设计基准
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_WARN_AMBER)
        painter.drawPolygon(QPolygonF([
            QPointF(9.0 * u, 1.4 * u), QPointF(17.4 * u, 16.4 * u),
            QPointF(0.6 * u, 16.4 * u),
        ]))
        painter.setBrush(_WARN_MARK)       # 三角形里的感叹号
        w = 1.9 * u
        painter.drawRoundedRect(
            QRectF(9.0 * u - w / 2.0, 6.4 * u, w, 5.0 * u), w / 2.0, w / 2.0)
        painter.drawEllipse(QRectF(9.0 * u - w / 2.0, 12.6 * u, w, w))
    finally:
        painter.end()
    pm.setDevicePixelRatio(ratio)
    icon = QIcon(pm)
    _warn_cache[key] = icon
    return icon


def circle_icon(kind: str, size: int = ICON_SIZE) -> QIcon:
    """“起点 / 终点 / 标记”的圆圈图标(绿圈 / 红圈 / 黄圈)。

    就是地图上那三个圈的缩小版: **颜色一致、大小按同一比例**(起点/终点半径 8、
    标记半径 6, 描边宽度都是半径的 0.3), 起点圈里那个中心实心点也照画 ——
    这样按钮和地图一看就是同一套东西。

    kind: "start"(开始录制) / "stop"(结束录制) / "mark"(标记当前位置)。
    """
    spec = _CIRCLE_SPEC.get(kind)
    if spec is None:
        return QIcon()
    color, radius, center_dot = spec
    key = (kind, int(size))
    cached = _circle_cache.get(key)
    if cached is not None:
        return cached
    ratio = _device_ratio()
    px = max(8, int(round(size * ratio)))
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        u = px / 18.0                                   # 以 18px 为设计基准
        # 最大圈留出描边的位置: 半径 7(而不是 9), 描边就不会顶到画布边
        r = max(2.0, (_MAX_RADIUS - _PEN_RATIO * _MAX_RADIUS / 2.0)
                * (radius / _MAX_RADIUS) * u)
        center = QPointF(px / 2.0, px / 2.0)
        pen = QPen(color, max(1.0, r * _PEN_RATIO))
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(center, r, r)
        if center_dot:
            dot = max(1.0, r * _CENTER_DOT_RATIO)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(color))
            painter.drawEllipse(center, dot, dot)
    finally:
        painter.end()
    pm.setDevicePixelRatio(ratio)
    icon = QIcon(pm)
    _circle_cache[key] = icon
    return icon


def _device_ratio() -> float:
    app = QApplication.instance()
    screen = app.primaryScreen() if app is not None else None
    return float(screen.devicePixelRatio()) if screen is not None else 1.0


def _state_colors() -> tuple[QColor, QColor]:
    """按当前调色板取 (显示中, 已隐藏) 的图标颜色。

    必须跟随调色板: 系统深色主题下控件底色是 #2d2d2d, 写死的深色图标在那里
    几乎看不见(浅色主题下则相反)。做法是统一用“文字色 + 降低不透明度”。
    """
    app = QApplication.instance()
    text = (_FALLBACK_TEXT if app is None
            else app.palette().color(QPalette.ColorRole.Text))
    shown = QColor(text)
    shown.setAlpha(255)
    hidden = QColor(text)
    hidden.setAlpha(_HIDDEN_ALPHA)
    return shown, hidden


def _base_pixmap(shown: bool) -> QPixmap | None:
    name = "eye.png" if shown else "eye_off.png"
    path = resource_path(os.path.join("assets", "icons", name))
    if not os.path.exists(path):
        log.warning("图标资源缺失: %s (将使用内置简笔图标)", path)
        return None
    pm = QPixmap(path)
    if pm.isNull():
        log.warning("图标资源损坏: %s", path)
        return None
    if path not in _logged:       # 首成功加载记一条 INFO: 便于确认打包后资源找得到
        _logged.add(path)
        log.info("图标已加载: %s", path)
    return pm


def _tinted(src: QPixmap, color: QColor, size: int) -> QPixmap:
    """把黑描边图标按目标尺寸缩放并用 color 着色(保持描边内外的透明度)。"""
    ratio = _device_ratio()
    px = max(8, int(round(size * ratio)))
    out = QPixmap(px, px)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(
            0, 0,
            src.scaled(px, px, Qt.AspectRatioMode.KeepAspectRatio,
                       Qt.TransformationMode.SmoothTransformation))
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(out.rect(), color)
    finally:
        painter.end()
    out.setDevicePixelRatio(ratio)
    return out


def _drawn_fallback(shown: bool, color: QColor, size: int) -> QPixmap:
    """资源缺失时的简笔眼睛(眼睛/眼睛加斜杠), 纯 QPainter 绘制。"""
    ratio = _device_ratio()
    px = max(8, int(round(size * ratio)))
    out = QPixmap(px, px)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        u = px / 18.0                      # 以 18px 为设计基准等比放大
        pen = QPen(color, max(1.0, 1.6 * u))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(1.5 * u, 5.0 * u, 15.0 * u, 8.0 * u))
        painter.setBrush(color)
        painter.drawEllipse(QRectF(6.9 * u, 7.0 * u, 4.2 * u, 4.2 * u))
        if not shown:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawLine(QRectF(2.5 * u, 2.5 * u, 13.0 * u, 13.0 * u).topLeft(),
                             QRectF(2.5 * u, 2.5 * u, 13.0 * u, 13.0 * u).bottomRight())
    finally:
        painter.end()
    out.setDevicePixelRatio(ratio)
    return out


def eye_icon(shown: bool, size: int = ICON_SIZE) -> QIcon:
    """返回“显示中/已隐藏”对应的眼睛图标(按状态/尺寸/主题缓存)。"""
    on, off = _state_colors()
    color = on if shown else off
    key = (bool(shown), int(size), color.rgba())
    cached = _cache.get(key)
    if cached is not None:
        return cached
    base = _base_pixmap(shown)
    pm = _tinted(base, color, size) if base is not None else _drawn_fallback(shown, color, size)
    icon = QIcon(pm)
    _cache[key] = icon
    return icon
