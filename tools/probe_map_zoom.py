"""探针: 地图视图的缩放/平移范围 —— 能不能看到**图框外**的机场。

用户 2026-10-26 反馈: "校准完后我在划鼠标滚轮缩小时, 缩小的倍数不够用, 我无法看到
双方机场的情况, 此外拖动也限制了我的拖动范围没法距离陆战战场拖动太远"。

根因两条:
    * `MIN_ZOOM = 1.0` —— 1.0 就是"整张图刚好铺满窗口", 再缩不了。而 8111 的机场
      在**图框外 7~10 倍**处(实测 fighter/ucav 重生点), 不缩到 ~1/10 根本装不下;
    * `_clamp_pan()` 只允许在"图片矩形 + 窗口"那一圈里拖 —— 缩到能看见全图之后
      就走不动了。

跑法:

    cmd /c "set PYTHONIOENCODING=utf-8&& set PYTHONPATH=d:\\course\\VSCODE\\WT81111G&&
            set QT_QPA_PLATFORM=offscreen&& .venv\\Scripts\\python.exe
            tools\\probe_map_zoom.py"
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from PyQt6.QtCore import QByteArray, QBuffer, QIODevice, QPoint, QPointF, Qt   # noqa: E402
from PyQt6.QtGui import QColor, QImage, QWheelEvent                            # noqa: E402
from PyQt6.QtWidgets import QApplication                                       # noqa: E402

from wt81111g.route_map_view import MIN_ZOOM, PAN_OUTSIDE, RouteMapView        # noqa: E402


def png_bytes(size: int = 2048) -> bytes:
    img = QImage(size, size, QImage.Format.Format_RGB32)
    img.fill(QColor("#2f4f3f"))
    ba = QByteArray()          # ⚠ 必须用变量持有: QBuffer 不拥有它, 传临时对象会段错误
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def wheel(view: RouteMapView, at: QPointF, up: bool) -> None:
    ev = QWheelEvent(at, at, QPoint(0, 0), QPoint(0, 120 if up else -120),
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                     Qt.ScrollPhase.NoScrollPhase, False)
    view.wheelEvent(ev)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)   # noqa: F841
    fails: list[str] = []

    def check(label: str, cond: bool, extra: str = "") -> None:
        print(f"{'OK  ' if cond else 'FAIL'}  {label}{('  ' + extra) if extra else ''}")
        if not cond:
            fails.append(label)

    view = RouteMapView()
    view.resize(1200, 800)
    view.set_map_bytes(png_bytes(), key="probe")
    view.show()
    app.processEvents()

    center = QPointF(600.0, 400.0)
    print(f"窗口 1200×800, 底图 2048²; zoom=1 时地图显示 {view._map_size()[0]:.0f}×"
          f"{view._map_size()[1]:.0f} px")

    print("\n--- 1) 滚轮能一路缩小到 MIN_ZOOM ---")
    for _ in range(30):
        wheel(view, center, up=False)
    app.processEvents()
    dw, dh = view._map_size()
    check(f"缩到下限 {MIN_ZOOM}", abs(view._zoom - MIN_ZOOM) < 1e-9,
          f"zoom={view._zoom:.4f}")
    check("地图显示尺寸小于窗口(能看见框外)", dw < 1200 and dh < 800,
          f"={dw:.1f}×{dh:.1f} px")

    print("\n--- 2) 能拖到图框外的机场(归一化 -7 / +10) ---")
    # 屏幕 x = 地图左边缘 + u * 地图宽; 让 u 落在 [0, 窗口宽] 里就算"看得到"
    def screen_x(u: float) -> float:
        r = view._map_rect()
        return r.left() + u * r.width()

    def drag_to(u: float) -> None:
        """把归一化坐标 u 拖到窗口正中。"""
        view._pan_x = 0.0
        view._pan_y = 0.0
        view._clamp_pan()
        r = view._map_rect()
        want = 600.0 - (r.left() + u * r.width())      # 需要把 u 挪到 x=600
        view._pan_x += want
        view._clamp_pan()

    for u, who in ((-7.0, "A 方机场"), (10.0, "B 方机场"), (0.5, "战场中心")):
        drag_to(u)
        r = view._map_rect()
        x = screen_x(u)
        check(f"能把{who}(归一化 {u:+.1f})拖进窗口", 0.0 <= x <= 1200.0,
              f"屏幕 x={x:.0f}, 地图左边缘={r.left():.0f}, 宽={r.width():.0f}")

    print("\n--- 3) 平移有上限(不会无限拖)，且能一键复位 ---")
    view._pan_x = 1e6
    view._pan_y = 1e6
    view._clamp_pan()
    ax, ay = view._pan_x, view._pan_y
    check("平移被 clamp 到有限值", abs(ax) < 1e5 and abs(ay) < 1e5,
          f"pan=({ax:.0f}, {ay:.0f})")
    view._clamp_pan()                  # 再 clamp 一次结果不该变(幂等)
    check("clamp 幂等", view._pan_x == ax and view._pan_y == ay)
    view.reset_view()
    app.processEvents()
    check("双击复位立刻回到图框居中",
          view._pan_x == 0.0 and view._pan_y == 0.0
          and abs(view._zoom - 1.0) < 1e-9,
          f"zoom={view._zoom}, pan=({view._pan_x}, {view._pan_y})")

    print("\n--- 4) 放大仍然正常(上限没动) ---")
    view.reset_view()
    app.processEvents()
    check("复位回 zoom=1", abs(view._zoom - 1.0) < 1e-9, f"zoom={view._zoom}")
    for _ in range(30):
        wheel(view, center, up=True)
    app.processEvents()
    check("放大能到 20 倍", abs(view._zoom - 20.0) < 1e-9, f"zoom={view._zoom:.2f}")

    view.close()
    print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
