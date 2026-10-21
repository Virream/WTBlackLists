"""pygfx 版地图画布的最小演示(2026-10-21)。

用途: 拿真实录制数据看 GPU 画布的效果与帧率, 还不改正式界面。
* 鼠标滚轮缩放、中键拖拽平移、双击复位;
* 播放/暂停按 60Hz 推进进度, 看“边播放边拖”的手感;
* 窗口标题实时显示 fps。

用法:
    .venv\\Scripts\\python.exe tools\\route_map_gl_demo.py            # 最后一条全局录制
    .venv\\Scripts\\python.exe tools\\route_map_gl_demo.py --index 0
    .venv\\Scripts\\python.exe tools\\route_map_gl_demo.py --snapshot out.png   # 离屏截图
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QApplication, QCheckBox, QHBoxLayout, QLabel,
                             QPushButton, QSlider, QVBoxLayout, QWidget)

from wt81111g.route_map_gl import RouteMapGlView

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "dist", "WTRouteTester", "data")


def load_case(index: int | None):
    """取一条全局录制: 返回 (地图字节, 线路列表, 总时长)。"""
    from wt81111g.route_store import RouteStore

    store = RouteStore(os.path.join(DATA, "routes.json"))
    if not store.globals:
        raise SystemExit("routes.json 里没有全局录制")
    g = store.globals[-1] if index is None else store.globals[index]
    lives = g.lives("A") + g.lives("B")
    if not lives:
        raise SystemExit("这条全局录制没有线路")
    key = lives[0].map_key
    with open(os.path.join(DATA, "maps", key + ".jpg"), "rb") as f:
        img = f.read()
    total = max((lf.points[-1].t for lf in lives if lf.points), default=0.0)
    return img, lives, total


class Demo(QWidget):
    def __init__(self, img: bytes, lives, total: float) -> None:
        super().__init__()
        self.setWindowTitle("pygfx 地图画布 演示")
        self.resize(1100, 760)
        self._total = total
        self._lives = lives
        self._head = 0.0
        self._playing = True

        self.view = RouteMapGlView(self)
        self.view.set_map_bytes(img)
        self.view.set_playback(lives, 0.0)

        self.lbl = QLabel("0.0 / %.1f s" % total)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, int(total * 100))
        self.slider.valueChanged.connect(self._on_slider)
        self.btn = QPushButton("暂停")
        self.btn.clicked.connect(self._toggle)
        self.chk_dim = QCheckBox("被击毁后路线变暗")
        self.chk_dim.setChecked(True)
        self.chk_dim.toggled.connect(
            lambda v: self.view.set_dead_style(dim=v))
        self.chk_mark = QCheckBox("被击毁标记")
        self.chk_mark.setChecked(True)
        self.chk_mark.toggled.connect(
            lambda v: self.view.set_dead_style(mark=v))

        bar = QHBoxLayout()
        bar.addWidget(self.btn)
        bar.addWidget(self.slider, 1)
        bar.addWidget(self.lbl)
        bar.addWidget(self.chk_dim)
        bar.addWidget(self.chk_mark)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.view, 1)
        lay.addLayout(bar)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

    def _toggle(self) -> None:
        self._playing = not self._playing
        self.btn.setText("暂停" if self._playing else "播放")

    def _on_slider(self, v: int) -> None:
        self._head = v / 100.0
        self.view.set_playback(self._lives, self._head)
        self.lbl.setText("%.1f / %.1f s" % (self._head, self._total))

    def _tick(self) -> None:
        if not self._playing:
            return
        self._head += 0.016
        if self._head > self._total:
            self._head = 0.0
        self.slider.blockSignals(True)
        self.slider.setValue(int(self._head * 100))
        self.slider.blockSignals(False)
        self.view.set_playback(self._lives, self._head)
        self.lbl.setText("%.1f / %.1f s" % (self._head, self._total))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=None, help="第几条全局录制(默认最后一条)")
    ap.add_argument("--snapshot", default="", help="离屏渲染到这文件(png)后退出")
    ap.add_argument("--at", type=float, default=None, help="截图时的进度(秒), 默认一半")
    args = ap.parse_args()

    img, lives, total = load_case(args.index)
    print(f"线路 {len(lives)} 条 / 总时长 {total:.1f}s")

    app = QApplication.instance() or QApplication(sys.argv[:1])

    if args.snapshot:
        # 抓“屏幕上看到的样子”: 用 bitmap present 的 Qt 画布 + 控件抓图
        # (离屏读回的色彩空间与屏幕不一致, 见 route_map_gl.snapshot 的说明)
        view = RouteMapGlView(None, present_method="bitmap")
        view.resize(1100, 760)
        view.show()
        for _ in range(6):
            app.processEvents()
        view.set_map_bytes(img)
        view.set_playback(lives, total * 0.5 if args.at is None else args.at)
        for _ in range(6):
            app.processEvents()
        if hasattr(view._canvas, "force_draw"):
            view._canvas.force_draw()
        app.processEvents()
        print("画布逻辑尺寸", view._canvas.get_logical_size())
        qi = view.grab()
        if qi is None:
            print("抓图失败")
            return 1
        qi.save(args.snapshot)
        print("已存", args.snapshot, qi.width(), qi.height())
        return 0

    w = Demo(img, lives, total)
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
