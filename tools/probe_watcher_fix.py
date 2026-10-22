"""诊断: 全局录制的"观战玩家死亡时刻修正"为什么对不上。

只读本软件自己的 `routes.json`(绝不碰游戏文件)。打印每局 A/B 队的

* `icon == "Player"` 的线(时间段、末点、是否 dead) —— 一条命一条;
* hudmsg 里抓到的击毁条数与时间跨度;
* 两队拟合出的时间轴偏移(`shift` / 配对条数);
* 指定关注玩家(默认 killflik / jjh67)的阵亡时刻, 以及每段能否认领。

用法::

    .venv\\Scripts\\python.exe tools\\probe_watcher_fix.py [routes.json] [昵称...]
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wt81111g import kill_timeline as kt          # noqa: E402
from wt81111g.route_store import RouteStore       # noqa: E402


def main(argv: list[str]) -> int:
    path = argv[0] if argv else os.path.join("data", "routes.json")
    names = [a for a in argv[1:] if not a.startswith("\u5206\u9694")]
    only = None
    if names and names[0].startswith("id="):
        only, names = names[0][3:], names[1:]
    names = names or ["killflik", "jjh67"]
    store = RouteStore(path)
    for g in store.globals:
        if not g.count():
            continue
        if only and g.route_id != only:
            continue
        kills = kt.merge_kills(g)
        print("=" * 78)
        print("%s  %s  duration=%.1f  watchers=%s  fixed=%s"
              % (g.route_id, g.map_name or g.map_key, g.duration,
                 g.watchers, g.fixed))
        if kills:
            print("kills: %d 条, 比赛时刻 %.1f ~ %.1f"
                  % (len(kills), kills[0]["t"], kills[-1]["t"]))
        else:
            print("kills: 0 条(没抓到 hudmsg)")
        base = kt.pooled_shift(g, kills)
        print("pooled_shift(全局) = %+.2f" % base)

        for k in kills:
            print("    kill t=%8.1f  %s" % (k["t"], k["msg"]))

        for side in ("A", "B"):
            lives = g.lives(side)
            hist = {}
            for lf in lives:
                hist[lf.icon or "-"] = hist.get(lf.icon or "-", 0) + 1
            print("  [%s] 线路 %d, icon 分布 %s"
                  % (side, len(lives),
                     sorted(hist.items(), key=lambda kv: -kv[1])[:8]))
            segs = kt.player_segments(g, side)
            shift, hits = kt.side_shift(g, side, kills, base)
            print("       Player 段 %d / 对齐锚点 %d / shift=%+.2f(配对 %d)"
                  % (len(segs), len(kt.side_ends(g, side)), shift, hits))
            if "--lines" in argv:
                for lf in sorted(lives, key=lambda x: x.points[0].t):
                    p0, p1 = lf.points[0], lf.points[-1]
                    print("      %-8s %-14s %-9s [%7.1f → %7.1f] dead=%-5s "
                          "n=%-4d (%.3f,%.3f)→(%.3f,%.3f)"
                          % (lf.life_id.split(":")[-1], lf.icon, lf.color,
                             p0.t, p1.t, lf.dead, len(lf.points),
                             p0.x, p0.y, p1.x, p1.y))
            for life, t0, t1 in segs:
                print("    %-12s icon=%-8s [%8.1f → %8.1f] dead=%s pts=%d"
                      % (life.life_id, life.icon, t0, t1, life.dead,
                         len(life.points)))
            stats = kt.player_stats(kills)
            for name in names:
                st = stats.get(name) or {}
                deaths = st.get("deaths") or []
                mapped = [round(d - shift, 1) for d in deaths]
                print("    %s: 阵亡 %d 次 %s → 换算后 %s"
                      % (name, len(deaths), [round(d, 1) for d in deaths], mapped))
                for life, t0, t1 in segs:
                    hit = kt.deaths_in(deaths, shift, t0, t1)
                    print("      段 %s [%.1f, %.1f] 命中 %s"
                          % (life.life_id, t0, t1, [round(h, 1) for h in hit]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
