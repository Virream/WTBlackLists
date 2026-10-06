"""树形列表用的图标(眼睛 = 轨迹在地图上显示 / 隐藏)。

素材: `assets/icons/eye.png`(显示中)与 `eye_off.png`(已隐藏),
来自开源图标库 **Lucide**(https://github.com/lucide-icons/lucide, ISC 许可,
见 `assets/icons/LICENSE-lucide.txt`), 由 `tools/make_icons.py` 从官方 SVG
光栅化为 **黑色描边 + 透明底** 的 PNG。

为什么这样做:
- 颜色在运行时用 `CompositionMode_SourceIn` 合成 —— 一套黑图就能适配不同状态/主题;
- 只用 **Qt 内建的 PNG 解码器**, 不依赖 `qsvg` 这个会被 `tools/trim_qt.py` 精简掉的图片插件;
- 万一资源缺失(例如打包漏带 `assets`), 自动退化为 `QPainter` 现画的简笔眼睛,
  功能不会因为少一张图而失效。
"""
from __future__ import annotations

import logging
import os

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPalette, QPen, QPixmap
from PyQt6.QtWidgets import QApplication

from .config import resource_path

log = logging.getLogger("icons")

# 树里显示的逻辑尺寸(px); 实际位图按屏幕缩放比放大, 避免高 DPI 下发虚
ICON_SIZE = 18
_HIDDEN_ALPHA = 110    # 隐藏状态只是“变淡”(太淡会连“有图标”都看不出来)
_FALLBACK_TEXT = QColor("#334155")   # 拿不到调色板(无 QApplication)时的兜底颜色

_cache: dict[tuple[bool, int], QIcon] = {}
_logged: set[str] = set()      # 已记录“加载成功”的资源路径(只记一次)


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
