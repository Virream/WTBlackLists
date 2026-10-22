"""录制叠加层: 录制时在屏幕角落显示进度与实际采样率。

================================================================ 安全说明(重要)
本叠加层与黑名单程序的 `overlay.py` 是**同一套做法**, 属于"外部独立窗口":

  * **不注入 DLL**, 不写游戏内存, 不钩取图形 API(D3D/OpenGL), 不碰游戏进程;
  * 不模拟按键 / 不读按键状态(那是快捷键模块的事, 与本模块无关);
  * 唯一的数据来源是游戏**官方**提供的 `http://127.0.0.1:8111` 本地 HTTP 接口
    (公开文档的遥测口), 本模块只把数字画在自己的窗口上;
  * 窗口只是一个普通透明置顶窗(`WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW`
    —— 分层/点击穿透/不进任务栏), 和 Discord、RTSS、MSI Afterburner 的覆盖层同类,
    不触发反作弊。

要让它显示在游戏画面之上, 游戏需使用 **全屏窗口(无边框)** 模式; 独占全屏下任何
外部窗口都盖不上去(这是 Windows 的合成器行为, 不是本程序的限制)。

外观(位置/背景不透明度/圆角/字号/颜色)来自 `RecordOverlaySettings`,
由主窗口菜单「设置 → 叠加层」调整, 改动实时生效。
"""
from __future__ import annotations

import ctypes
import logging
import sys
from dataclasses import dataclass

from PyQt6.QtCore import QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath
from PyQt6.QtWidgets import QApplication, QWidget

log = logging.getLogger("record_overlay")

# Win32 扩展样式(点击穿透 + 分层 + 工具窗口)
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080

# 位置锚点 → 显示名
ANCHORS = {
    "tl": "左上",
    "tr": "右上",
    "bl": "左下",
    "br": "右下",
}
DEFAULT_ANCHOR = "tr"

COLOR_BG = "#0f172a"        # 面板底色(与地图/侧栏同一套暗色)
COLOR_TITLE = "#38bdf8"     # 第一行(状态)用主题色 sky-400
COLOR_TEXT = "#e2e8f0"
PAD_X = 12                  # 面板内边距
PAD_Y = 8
LINE_GAP = 2


def _set_click_through(hwnd: int) -> None:
    """把窗口设为"点击穿透 + 分层 + 不进任务栏"(与黑名单叠加层一致)。"""
    if sys.platform.startswith("win"):
        try:
            user32 = ctypes.windll.user32
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE,
                ex | WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOOLWINDOW,
            )
        except Exception as exc:  # noqa: BLE001 - 取不到句柄也不该崩
            log.warning("set click-through failed: %s", exc)


@dataclass
class RecordOverlaySettings:
    """录制叠加层的外观设置(由主窗口从 QSettings 读写)。"""
    enabled: bool = False       # 默认关闭: 用户明确打开了才在录制时出现
    anchor: str = DEFAULT_ANCHOR
    margin: int = 24            # 距屏幕边缘的像素
    opacity: int = 62           # 背景**不**透明度 0~100(0 = 完全透明, 只剩文字)
    corner: int = 10            # 圆角半径(px)
    font_size: int = 10
    font_family: str = "Microsoft YaHei"

    def normalized(self) -> "RecordOverlaySettings":
        """把越界的值夹回合法范围(读配置/手改 json 都可能给出奇怪的值)。"""
        return RecordOverlaySettings(
            enabled=bool(self.enabled),
            anchor=self.anchor if self.anchor in ANCHORS else DEFAULT_ANCHOR,
            margin=max(0, min(400, int(self.margin))),
            opacity=max(0, min(100, int(self.opacity))),
            corner=max(0, min(60, int(self.corner))),
            font_size=max(6, min(40, int(self.font_size))),
            font_family=self.font_family or "Microsoft YaHei",
        )


class _Panel(QWidget):  # noqa: N801 - 内部类
    """叠加层面板本身: 圆角背景 + 若干行文字(第一行用主题色)。"""

    def __init__(self, parent: QWidget, settings: RecordOverlaySettings):
        super().__init__(parent)
        self.settings = settings
        self._lines: list[tuple[str, str]] = []      # [(文本, 颜色)]
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._recalc()

    # ------------------------------------------------------------------ 内容
    def set_lines(self, lines: list[tuple[str, str]]) -> None:
        if lines == self._lines:
            return                       # 内容没变 → 不重排也不重绘
        self._lines = lines
        self._recalc()
        self.update()

    def _font(self) -> QFont:
        return QFont(self.settings.font_family, self.settings.font_size)

    def _recalc(self) -> None:
        """按当前文字量出面板尺寸(文字宽 + 内边距, 行数 × 行高)。"""
        fm = QFontMetrics(self._font())
        text_w = max((fm.horizontalAdvance(t) for t, _c in self._lines), default=0)
        line_h = fm.height() + LINE_GAP
        w = text_w + PAD_X * 2
        h = line_h * len(self._lines) + PAD_Y * 2 if self._lines else 0
        self.resize(max(1, w), max(1, h))
        self._line_h = line_h

    def apply_settings(self) -> None:
        self._recalc()
        self.update()

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, _ev) -> None:  # noqa: N802 - Qt 命名
        if not self._lines:
            return
        p = QPainter(self)
        self.draw(p)
        p.end()

    def draw(self, p: QPainter) -> None:
        """把面板画到任意 painter 上。

        单独抽出来是为了**测试能验证到真实结果**: `QWidget.render()` 会先填一层
        不透明窗口背景, 读不到半透明的 alpha; 这里直接画到透明底的 QImage 上,
        与实机看到的合成过程一致(实机是由 `WA_TranslucentBackground` 的顶层窗口合成)。
        """
        if not self._lines:
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        s = self.settings
        # 背景: 底色 + 用户设定的不透明度(0 = 只剩文字)
        bg = QColor(COLOR_BG)
        bg.setAlpha(round(255 * s.opacity / 100.0))
        path = QPainterPath()
        r = float(s.corner)
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), r, r)
        p.fillPath(path, bg)
        # 文字(第一行加粗 + 主题色)
        font = self._font()
        p.setFont(font)
        fm = QFontMetrics(font)
        align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        y = float(PAD_Y)
        for i, (text, color) in enumerate(self._lines):
            p.setPen(QColor(color or COLOR_TEXT))
            if i == 0:
                bold = QFont(font)
                bold.setBold(True)
                p.setFont(bold)
            p.drawText(QRectF(PAD_X, y, self.width() - PAD_X * 2, fm.height()),
                       align, text)
            if i == 0:
                p.setFont(font)          # 其余行用常规字重
            y += self._line_h


class RecordOverlay(QWidget):
    """全屏透明置顶的录制叠加层, 内部面板按锚点 + 边距定位。

    用法(主窗口):
        ov = RecordOverlay(settings)
        ov.set_lines([("● 录制中 01:23.4", COLOR_TITLE), ("采样 12.6/s", COLOR_TEXT)])
        ov.set_active(True)      # 录制开始 → 显示; 结束 → 隐藏
    """

    def __init__(self, settings: RecordOverlaySettings, parent=None):
        super().__init__(None)                   # 顶层窗口(不要父窗口, 否则会被主窗裁掉)
        self.settings = settings.normalized()
        self._active = False
        self._click_hwnd: int | None = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        geo = self._screen_geometry()
        self.setGeometry(geo)
        self._panel = _Panel(self, self.settings)
        self._reposition()
        self.hide()

    # ------------------------------------------------------------------ 屏幕/位置
    @staticmethod
    def _screen_geometry() -> QRect:
        screen = QApplication.primaryScreen()
        return screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)

    def _reposition(self) -> None:
        """按锚点 + 边距摆放面板(小屏/大屏都按真实可用区域算)。"""
        geo = self.geometry()
        m = self.settings.margin
        w, h = self._panel.width(), self._panel.height()
        anchor = self.settings.anchor
        x = geo.width() - w - m if anchor in ("tr", "br") else m
        y = geo.height() - h - m if anchor in ("bl", "br") else m
        x = max(0, min(x, max(0, geo.width() - w)))
        y = max(0, min(y, max(0, geo.height() - h)))
        self._panel.move(x, y)

    # ------------------------------------------------------------------ 内容/开关
    def set_lines(self, lines: list[tuple[str, str]]) -> None:
        self._panel.set_lines(lines)
        self._reposition()

    def set_active(self, active: bool) -> None:
        """录制中 → True(且设置里启用了才真的显示); 否则 False → 隐藏。"""
        self._active = bool(active)
        if self._active and self.settings.enabled:
            self._show_overlay()
        else:
            self._hide_overlay()

    def set_enabled(self, enabled: bool) -> None:
        self.settings.enabled = bool(enabled)
        self.set_active(self._active)

    def apply_settings(self, settings: RecordOverlaySettings | None = None) -> None:
        """设置改动后调用: 换外观 + 重新量尺寸 + 重新定位(立即生效)。"""
        if settings is not None:
            self.settings = settings.normalized()
        self._panel.settings = self.settings
        self._panel.apply_settings()
        self._reposition()
        self.set_active(self._active)

    # ------------------------------------------------------------------ 显示
    def _show_overlay(self) -> None:
        geo = self._screen_geometry()
        if self.geometry() != geo:
            self.setGeometry(geo)        # 分辨率/显示器变更后跟着走
            self._reposition()
        self.show()
        self.raise_()
        if sys.platform.startswith("win"):
            hwnd = int(self.winId())
            if hwnd != self._click_hwnd:
                self._click_hwnd = hwnd
                _set_click_through(hwnd)

    def _hide_overlay(self) -> None:
        self.hide()
