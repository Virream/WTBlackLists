"""诊断: 回放时哪一帧卡顿(逐帧计时, 找出尖峰对应的时刻)。

用法::

    .venv\\Scripts\\python.exe tools\\probe_playback_stutter.py [stept] [routes.json]

把最新那个全局条目的 A+B 队线路当成回放对象, 从 0 扫到结束, 每 `stept` 秒画一帧并计时;
最后打印最慢的 15 帧, 以及每条线路的“阵亡时刻”(对照尖峰是不是发生在某条线消失的瞬间)。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication            # noqa: E402


def main(argv: list[str]) -> int:
    step = float(argv[0]) if argv else 0.2
    path = (argv[1] if len(argv) > 1 else
            os.path.join("dist", "WTRouteTester", "data", "routes.json"))
    app = QApplication.instance() or QApplication([])

    from wt81111g.route_store import RouteStore

    store = RouteStore(path)
    g = max((x for x in store.globals if x.count()), key=lambda x: x.count())
    recs = [lf for side in ("A", "B") for lf in g.lives(side)]
    end = max((r.points[-1].t for r in recs if r.points), default=0.0)
    print("回放 %s: %d 条线路, 0 ~ %.1fs" % (g.route_id, len(recs), end))

    try:
        from wt81111g.route_map_gl import RouteMapGlView   # noqa: F401
        from wt81111g import route_map_gl as mod
        view = mod.RouteMapGlView(offscreen=True)
        kind = "GPU"
    except Exception as exc:                       # noqa: BLE001
        print("GPU 画布不可用(%s), 退回软件版" % exc)
        from wt81111g.route_map_view import RouteMapView
        view = RouteMapView()
        kind = "软件"
    view.resize(900, 620)
    view.set_playback(recs, 0.0)

    rebuilds = {"n": 0}
    if hasattr(view, "_rebuild_objects"):
        orig = view._rebuild_objects

        def counted():
            rebuilds["n"] += 1
            return orig()

        view._rebuild_objects = counted
    view._sync_objects() if hasattr(view, "_sync_objects") else None
    for _ in range(3):                             # 预热(建几何/纹理)
        view._draw() if hasattr(view, "_draw") else view.repaint()
    app.processEvents()
    rebuilds["n"] = 0

    frames: list[tuple[float, float, float, float, int, int]] = []
    t = 0.0
    last_zones = None
    zone_changes = 0
    while t <= end:
        # 与真实回放同一套路径: 每帧把战区状态也推到 playhead 对应的快照
        snap = g.zone_state_at(t)
        if snap and snap != last_zones:
            last_zones = snap
            zone_changes += 1
            view.set_zones(snap)
        before = rebuilds["n"]
        view.set_playback(recs, t)
        if hasattr(view, "_draw"):
            t0 = time.perf_counter()
            view._sync_objects()
            t1 = time.perf_counter()
            view._sync_camera()
            view._renderer.render(view._scene, view._camera)
            t2 = time.perf_counter()
            frames.append((t, (t1 - t0) * 1000.0, (t2 - t1) * 1000.0,
                           (t2 - t0) * 1000.0,
                           rebuilds["n"] - before, 0))
        else:
            t0 = time.perf_counter()
            view.repaint()
            t1 = time.perf_counter()
            frames.append((t, 0.0, 0.0, (t1 - t0) * 1000.0, 0, 0))
        t += step
    app.processEvents()
    print("战区快照变化 %d 次, 整层重建 %d 次" % (zone_changes, rebuilds["n"]))

    cost = [x[3] for x in frames]
    avg = sum(cost) / len(cost)
    print("%s: %d 帧, 平均 %.2f ms, 最慢 %.2f ms"
          % (kind, len(frames), avg, max(cost)))
    print("最慢的 15 帧(同步 / 渲染 / 合计 / 重建次数):")
    for t, a, b, ms, rb, _x in sorted(frames, key=lambda x: -x[3])[:15]:
        print("   t=%7.2f  %6.2f + %6.2f = %7.2f ms  重建=%d" % (t, a, b, ms, rb))
    print("各线路到点会“消失”的时刻(阵亡/结束):")
    for r in sorted(recs, key=lambda x: x.points[-1].t if x.points else 0)[:12]:
        if not r.points:
            continue
        print("   %-10s side=%s dead=%-5s 末点 t=%.2f"
              % (r.route_id.split(":")[-1], getattr(r, "side", "?"),
                 getattr(r, "dead", False), r.points[-1].t))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
