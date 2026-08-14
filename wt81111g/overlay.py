"""屏幕叠加层: 类似 WTRTI 的外部透明置顶窗口。

安全说明: 本叠加层是一个**独立的外部窗口**, 不注入 DLL、不读写游戏内存、
不钩取游戏图形 API, 仅读取官方 8111 遥测并以普通置顶透明窗口绘制文字,
与 Discord/RTSS/MSI Afterburner 等覆盖层属于同一类别, 不触发反作弊。
为使其显示在游戏之上, 建议游戏使用"全屏窗口(无边框)"模式。

位置/背景/字号等外观通过主界面的"叠加层设置"调整(见 settings.py)。
"""
from __future__ import annotations

import ctypes
import logging
import sys

from PyQt6.QtCore import Qt, QRect, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

from .settings import OverlaySettings

log = logging.getLogger("overlay")

# Win32 扩展样式(点击穿透)
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080


def _set_click_through(hwnd: int) -> None:
    if sys.platform.startswith("win"):
        try:
            user32 = ctypes.windll.user32
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE,
                ex | WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOOLWINDOW,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("set click-through failed: %s", exc)


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


class OverlayWindow(QWidget):
    """全屏透明置顶叠加层。外观参数来自 OverlaySettings。"""

    def __init__(self, settings: OverlaySettings):
        super().__init__()
        self.settings = settings
        self._in_battle = False
        self._enabled = True
        self._found: list[tuple[str, str]] = []  # (昵称, 原因)
        self._rotate_index = 0
        self._click_hwnd: int | None = None  # 已设置点击穿透的窗口句柄(避免重复调用)
        self._rotate_timer = QTimer(self)
        self._rotate_timer.setInterval(3000)  # 多条命中时每 3 秒轮换
        self._rotate_timer.timeout.connect(self._rotate)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        screen = self.screen() if self.screen() else None
        if screen is None:
            screen = QApplication.primaryScreen()
        geo = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        self.setGeometry(geo)
        self._max_w = int(geo.width() * 0.92)

        self._label = QLabel("", self)
        self._label.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        )
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)

        self._refresh()
        self.hide()

    # ------------------------------------------------------------------
    def set_battle(self, in_battle: bool) -> None:
        self._in_battle = in_battle
        self._refresh()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._refresh()

    def set_found(self, names: list) -> None:
        """设置命中的黑名单玩家列表, 元素可为 (昵称, 原因) 或纯昵称。"""
        items: list[tuple[str, str]] = []
        for n in names:
            if isinstance(n, (tuple, list)) and len(n) >= 2:
                items.append((str(n[0]), str(n[1] or "")))
            else:
                items.append((str(n), ""))
        self._found = items
        self._rotate_index = 0
        self._refresh()

    def apply_settings(self) -> None:
        """主界面修改叠加层设置后调用, 立即生效。"""
        self._refresh()

    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        if not self._in_battle or not self._enabled:
            self._rotate_timer.stop()
            self.hide()
            return
        if self._found:
            if len(self._found) > 1:
                self._rotate_timer.start()
            else:
                self._rotate_timer.stop()
            self._update_label()
        else:
            self._rotate_timer.stop()
            color = self.settings.font_color
            text = self.settings.text_checking or "正在确认名单中..."
            self._show_html(f'<span style="color:{color};">{_esc(text)}</span>')

    def _update_label(self) -> None:
        if not self._found:
            return
        color = self.settings.font_color
        title = self.settings.text_found or "发现肃反人员"
        nick, reason = self._found[self._rotate_index % len(self._found)]
        line = f'<span style="color:{color};">{_esc(nick)}</span>'
        if self.settings.show_reason and reason:
            line += f' <span style="color:{color};">({_esc(reason)})</span>'
        self._show_html(f'<span style="color:{color};">{_esc(title)}:</span><br>{line}')

    def _rotate(self) -> None:
        if len(self._found) > 1:
            self._rotate_index = (self._rotate_index + 1) % len(self._found)
            self._update_label()

    def _show_html(self, html: str) -> None:
        self._label.setText(html)
        self._apply_style()
        self._apply_position()
        self.show()
        self.raise_()
        if sys.platform.startswith("win"):
            hwnd = int(self.winId())
            if hwnd != self._click_hwnd:
                self._click_hwnd = hwnd
                _set_click_through(hwnd)

    def _apply_style(self) -> None:
        s = self.settings
        font = QFont(s.font_family, s.font_size)
        font.setBold(True)
        self._label.setFont(font)
        r, g, b, a = s.bg_rgba
        radius = max(0, int(s.corner_radius))
        self._label.setStyleSheet(
            f"background-color: rgba({r},{g},{b},{a});"
            f"border-radius: {radius}px; padding: 12px 26px;"
        )

    def _apply_position(self) -> None:
        geo = self.geometry()
        self._label.setMaximumWidth(self._max_w)
        self._label.adjustSize()
        w = self._label.width()
        if w > self._max_w:
            self._label.resize(self._max_w, self._label.heightForWidth(self._max_w))
            w = self._label.width()
        # 水平居中于 X%, 顶部对齐于 Y%
        x = int(geo.width() * self.settings.pos_x_pct / 100.0) - w // 2
        y = int(geo.height() * self.settings.pos_y_pct / 100.0)
        x = max(0, min(x, max(0, geo.width() - w)))
        y = max(0, min(y, max(0, geo.height() - self._label.height())))
        self._label.move(x, y)
