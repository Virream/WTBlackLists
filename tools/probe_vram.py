"""测量“地图画布”会不会涨显存(只读本机计数, 不碰游戏文件)。

用真实的 `routes.json` 里的线路当素材, 把 `RouteMapGlView` 跑起来:

    A. 基线(载入地图 + 全部线路 + 画几帧)
    B1~B3. 三轮同样的压力: 缩放 300 次 + 回放进度 300 次
    E. 换图 30 次
    F. 删掉画布

每轮结束读一次**本进程**的 GPU 专用/共享显存。判断标准:

* 三轮之间基本不涨 → 渲染路径不泄漏(临时对象被 GC 掉了);
* 每轮线性上涨 → 泄漏, 得定位到具体对象。

用法::

    .venv\\Scripts\\python.exe tools\\probe_vram.py [迭代数] [routes.json]
"""
from __future__ import annotations

import gc
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication            # noqa: E402


def gpu_mb() -> tuple[str, str]:
    """本进程的 GPU (专用, 共享) 显存 MB。取不到就返回 `?`。"""
    ps = (
        "$s=(Get-Counter '\\GPU Process Memory(*)\\Dedicated Usage',"
        "'\\GPU Process Memory(*)\\Shared Usage').CounterSamples "
        "| Where-Object {{ $_.InstanceName -like 'pid_{pid}_*' }};"
        "$d=($s|Where-Object Path -like '*dedicated*'|Measure-Object CookedValue -Sum).Sum;"
        "$h=($s|Where-Object Path -like '*shared*'|Measure-Object CookedValue -Sum).Sum;"
        "'{{0}} {{1}}' -f [int]($d/1MB), [int]($h/1MB)"
    ).format(pid=os.getpid())
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=40)
        parts = (out.stdout or "").strip().split()
        if len(parts) >= 2:
            return parts[0], parts[1]
    except Exception:                              # noqa: BLE001
        pass
    return "?", "?"


def gpu_line(tag: str, view=None) -> None:
    ded, shared = gpu_mb()
    extra = ""
    if view is not None:
        extra = ("  场景对象 %d / 轨迹 %d / 图标 %d / _gl_objs %d"
                 % (len(view._scene.children), len(view._gl_tracks),
                    len(view._gl_icons), len(view._gl_objs)))
    print("%-28s 专用 %5s MB  共享 %5s MB%s" % (tag, ded, shared, extra),
          flush=True)


def main(argv: list[str]) -> int:
    iters = int(argv[0]) if argv else 300
    path = (argv[1] if len(argv) > 1 else
            os.path.join("dist", "WTRouteTester", "data", "routes.json"))
    app = QApplication.instance() or QApplication([])

    from wt81111g.map_cache import MapCache
    from wt81111g.route_map_gl import RouteMapGlView
    from wt81111g.route_store import RouteStore

    cache = MapCache(os.path.join(os.path.dirname(path), "maps"))
    data = None
    for key in cache.keys():
        data = cache.load(key)
        if data:
            break

    recs = []
    try:
        store = RouteStore(path)
        for g in store.globals:
            recs.extend(g.lives("A") + g.lives("B"))
    except Exception as exc:                        # noqa: BLE001
        print("!! 读 routes.json 失败: %s" % exc)
    recs.sort(key=lambda lf: -len(lf.points))
    recs = recs[:40]
    print("素材: %d 条线路, 最长 %d 点; 地图 %s"
          % (len(recs), len(recs[0].points) if recs else 0,
             "已载入" if data else "缺失"), flush=True)

    view = RouteMapGlView(offscreen=True)
    view.resize(1200, 800)
    if data:
        view.set_map_bytes(data, key="probe")
    view._visible = list(recs)
    view._highlight = list(recs)
    view._playback_recs = list(recs)
    for _ in range(5):
        view._draw()
    app.processEvents()
    gpu_line("A 基线", view)

    for round_no in range(1, 4):
        for i in range(iters):
            view.zoom = 1.0 + (i % 7) * 0.35
            view._draw()
        for i in range(iters):
            view._playhead = (i * 7.0) % 400.0
            view._draw()
        app.processEvents()
        gc.collect()
        gpu_line("B%d 缩放+回放 %d 次" % (round_no, iters * 2), view)

    if data:
        for i in range(30):
            view.set_map_bytes(data, key="probe%d" % (i % 2))
            view._draw()
        app.processEvents()
        gc.collect()
        gpu_line("E 换图 30 次", view)

    del view
    gc.collect()
    app.processEvents()
    gpu_line("F 删掉画布后")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
