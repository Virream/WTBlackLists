# -*- coding: utf-8 -*-
"""全局录制(整局所有玩家的线路)的针对性用例。

只覆盖本次新增的逻辑:
* `global_record.GlobalTracker` 的多目标跟踪(配对 / 结束 / 重生拆线 / 过滤);
* `RouteStore` 里全局条目的落盘、A/B 标记唯一性;
* 主窗口: 条目挂在“存放路线的那一级”的末尾、A/B 两队分组、眼睛开关、
  删除需要确认; 以及“全局录制时锁定地面底图”。

用法: python tests/global_record_test.py
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtWidgets import QApplication

import wt81111g.route_window as rw
from wt81111g.global_record import (
    EMPTY_STOP, GlobalTracker, extract_units, is_drone, recordable,
)
from wt81111g.map_cache import MapCache
from wt81111g.route_store import (
    GLOBAL_SIDES, GlobalRecord, RoutePoint, RouteStore, TrackPoint, UnitLife,
)

rw.play_sound = lambda action: None
rw.play_error = lambda *a, **k: None

results: list[str] = []

PROT = "#1839A7"     # 重生保护(深蓝)
BLUE = "#174DFF"     # 我方
YELLOW = "#faC81E"   # 自己


def tank(x, y, color=BLUE, icon="MediumTank", typ="ground_model"):
    return {"type": typ, "icon": icon, "color": color, "x": x, "y": y}


# ---------------------------------------------------------------- 单位过滤
def test_filters():
    # 地面载具: 录
    assert recordable("ground_model", "MediumTank")
    assert recordable("ground_model", "IFV")
    # 固定翼 / 直升机: 不录
    assert not recordable("aircraft", "Assault")
    assert not recordable("aircraft", "Fighter")
    assert not recordable("helicopter", "AttackHelicopter")
    # 微型侦察无人机(唯一要录的空中单位)
    assert recordable("aircraft", "uav")
    assert recordable("uav", "scout")
    assert is_drone("aircraft", "ReconDrone")
    # 静态地物 / AI 防空 / 机场: 一律不录
    for typ in ("capture_zone", "respawn_base_tank", "airfield", "Airdefence",
                "bombing_point", "respawn_base_ucav"):
        assert not recordable(typ, typ), typ

    # 坐标必须落在 0~1: 无人机视角会带出坐标 2.9~3.4 的 AI 防空
    objects = [tank(0.5, 0.5), {"type": "Airdefence", "icon": "Airdefence",
                                "x": 3.1, "y": 2.9},
               tank(1.4, 0.2)]        # 超界的“地面车”也不要
    units, skipped = extract_units(objects)
    assert len(units) == 1 and units[0].x == 0.5, [u.x for u in units]
    assert ("Airdefence", "Airdefence") in skipped
    results.append("全局录制: 单位过滤(地面/空中/无人机/静态/超界坐标) OK")


# ---------------------------------------------------------------- 跟踪
def test_tracker_basic():
    rec = GlobalRecord(route_id="g1", map_key="M", map_name="地图", created_at="")
    tr = GlobalTracker(rec, "A")
    # 一辆车从 (0.2,0.2) 直线开到 (0.22,0.2): 每帧 0.05s、每帧 0.001
    # (真实坦克 25Hz 下约 0.0002/帧, 0.001 已经算开得很快了)
    t = 0.0
    for i in range(21):
        tr.feed(t, [tank(0.2 + 0.001 * i, 0.2)])
        t += 0.05
    assert tr.total == 1, f"同一条线应始终只有 1 条, 实际 {tr.total}"
    assert tr.active == 1
    lives = tr.finish()
    assert len(lives) == 1
    pts = lives[0].points
    assert pts[0].x == 0.2 and abs(pts[-1].x - 0.22) < 1e-6, (pts[0].x, pts[-1].x)
    # 5Hz 采样: 1 秒左右 → 点数量远少于帧数(每帧都存就说明抽稀没生效)
    assert len(pts) <= 12, f"采样过密: {len(pts)} 个点"
    assert pts == sorted(pts, key=lambda p: p.t), "点必须按时间递增"
    results.append("全局跟踪: 单辆车连续移动 → 一条线, 5Hz 抽稀 OK")


def test_tracker_two_and_death():
    rec = GlobalRecord(route_id="g2", map_key="M", map_name="地图", created_at="")
    tr = GlobalTracker(rec, "B")
    t = 0.0
    for i in range(10):                        # 两辆车并排前进
        tr.feed(t, [tank(0.20 + 0.001 * i, 0.30),
                    tank(0.20 + 0.001 * i, 0.70, color=YELLOW, icon="IFV")])
        t += 0.05
    assert tr.total == 2, tr.total
    # 其中一辆死了(不再出现), 另一辆继续
    for i in range(10, 40):
        tr.feed(t, [tank(0.20 + 0.001 * i, 0.30)])
        t += 0.05
    lives = tr.finish()
    assert len(lives) == 2, f"消失的那辆不该复活, 实际 {len(lives)} 条"
    dead = [l for l in lives if abs(l.points[0].y - 0.70) < 1e-9][0]
    alive = [l for l in lives if abs(l.points[0].y - 0.30) < 1e-9][0]
    assert dead.t1 < alive.t1, "死掉的那条线应先结束"
    results.append("全局跟踪: 并排两辆不串线 + 消失即结束 OK")


def test_tracker_respawn():
    """蓝 → 深蓝 = 重生: 同一位置要拆成两条线(上一段到此为止)。"""
    rec = GlobalRecord(route_id="g3", map_key="M", map_name="地图", created_at="")
    tr = GlobalTracker(rec, "A")
    t = 0.0
    for i in range(10):
        tr.feed(t, [tank(0.20 + 0.001 * i, 0.5)])
        t += 0.05
    for i in range(10):                        # 重生: 原地变深蓝
        tr.feed(t, [tank(0.209 + 0.0005 * i, 0.5, color=PROT)])
        t += 0.05
    lives = tr.finish()
    assert len(lives) == 2, f"重生应拆成两条线, 实际 {len(lives)}"
    assert not lives[0].protected, "前一段不是重生保护状态"
    assert lives[1].protected, "后一段出生时处于重生保护"
    assert lives[0].t1 <= lives[1].t0 + 1e-9
    results.append("全局跟踪: 深蓝重生保护 → 拆成两条线并标记 OK")


def test_tracker_empty_stop():
    """全场没有可录单位超过 EMTPY_STOP 秒 → 该停止录制(回到车库)。"""
    rec = GlobalRecord(route_id="g4", map_key="M", map_name="地图", created_at="")
    tr = GlobalTracker(rec, "A")
    tr.feed(0.0, [tank(0.3, 0.3)])
    assert not tr.feed(1.0, [])
    assert not tr.feed(1.0 + EMPTY_STOP * 0.5, [])
    assert tr.feed(1.0 + EMPTY_STOP + 0.1, []), "空场超过阈值应返回 True"
    results.append("全局跟踪: 全场无单位超时 → 自动停止 OK")


# ---------------------------------------------------------------- 存储
def test_store(tmpdir: str):
    path = os.path.join(tmpdir, "globals.json")
    store = RouteStore(path)
    g = store.new_global("MAP1", "地图 1", zone_count=3, zone_signature="sig",
                         mode_name="战区控制")
    assert g.label and len(g.label) >= 5, g.label            # 名字 = 录制时间
    life = UnitLife(life_id=f"{g.route_id}:A1", icon="MediumTank",
                    obj_type="ground_model", protected=True,
                    points=[TrackPoint(0.1, 0.2, 0.0), TrackPoint(0.3, 0.4, 3.5)])
    g.sides["A"].append(life)
    store.add_global(g)
    assert store.get_global(g.route_id) is g

    # 重新读盘: 图元/时间/保护标志都要在
    again = RouteStore(path)
    got = again.get_global(g.route_id)
    assert got is not None and got.count() == 1, "全局条目没有落盘/读回"
    l2 = got.lives("A")[0]
    assert l2.protected and l2.icon == "MediumTank"
    assert len(l2.points) == 2 and abs(l2.points[1].t - 3.5) < 1e-9
    assert abs(l2.points[1].x - 0.3) < 1e-9

    # 标记: 同一队只能指向一条记录(打新的会自动清掉旧的)
    g2 = again.new_global("MAP1", "地图 1")
    again.add_global(g2)
    again.set_mark(g.route_id, "A", True)
    assert again.marked_global("A") is not None
    again.set_mark(g2.route_id, "A", True)
    assert again.marked_global("A").route_id == g2.route_id
    assert "A" not in again.get_global(g.route_id).marks, "旧标记应被清掉"
    # B 与 A 互不影响, 同一条可以同时标 A 和 B
    again.set_mark(g.route_id, "B", True)
    assert set(again.get_global(g.route_id).marks) == {"B"}
    again.set_mark(g.route_id, "A", True)
    assert again.get_global(g.route_id).marks == ["A", "B"], "标记应保持 A,B 顺序"
    again.clear_marks(g.route_id)
    assert again.get_global(g.route_id).marks == []

    # 分组: 按 (地图, 战区指纹) 归组
    grp = again.globals_by_group()
    assert ("MAP1", "sig") in grp, list(grp)
    assert ("MAP1", "") in grp, list(grp)

    # 删除
    assert again.remove_global(g2.route_id)
    assert again.get_global(g2.route_id) is None
    results.append("全局录制存储: 落盘/读回/标记唯一性/删除/分组 OK")


# ---------------------------------------------------------------- 窗口
def make_window(tmpdir: str, name: str) -> rw.RouteTesterWindow:
    store = RouteStore(os.path.join(tmpdir, name))
    cache = MapCache(os.path.join(tmpdir, "maps_" + name))
    return rw.RouteTesterWindow(store, map_cache=cache, start_recorder=False)


def _global_items(win):
    out = []

    def walk(item):
        for i in range(item.childCount()):
            ch = item.child(i)
            if ch.data(0, rw._ROLE_KIND) == "global":
                out.append(ch)
            walk(ch)

    walk(win.tree.invisibleRootItem())
    return out


def _children_kind(item, kind):
    return [item.child(i) for i in range(item.childCount())
            if item.child(i).data(0, rw._ROLE_KIND) == kind]


def test_window(tmpdir: str):
    app = QApplication.instance()
    win = make_window(tmpdir, "globals_win.json")
    win.recorder.stop()
    win.hotkeys.stop()
    win.sys_hotkeys.stop()
    win.show()
    app.processEvents()

    # 一张地图 + 一个版本(带一个普通版本分组) + 一条全局录制
    win.store.new_global("MAPX", "地图 X")            # 只登记地图
    win.store.remember_zones("MAPX", [])
    rec = win.store.new_route("MAPX", "地图 X", "T", 0.3, 0.3)
    rec.zone_count = 3
    rec.mode_name = "战区控制"
    rec.zone_signature = "A@0.1,0.1;B@0.5,0.5;C@0.9,0.9"
    rec.points = [RoutePoint(0.3, 0.3, 0.0, 0.0), RoutePoint(0.4, 0.4, 5.0, 40.0)]
    win.store.add(rec)

    g = win.store.new_global("MAPX", "地图 X", zone_count=3,
                             zone_signature=rec.zone_signature, mode_name="战区控制")
    for side, ys in (("A", (0.2, 0.4)), ("B", (0.6, 0.8))):
        for k, y in enumerate(ys):
            g.sides[side].append(UnitLife(
                life_id=f"{g.route_id}:{side}{k}", icon="MediumTank",
                obj_type="ground_model",
                points=[TrackPoint(0.2, y, 0.0), TrackPoint(0.3, y, 1.0)]))
    win.store.add_global(g)
    win._reload_tree()
    app.processEvents()

    # 1) 全局条目挂在“存放路线的那一级”的**末尾**, 与单条路线同级
    items = _global_items(win)
    assert len(items) == 1, items
    variant = items[0].parent()
    assert variant.data(0, rw._ROLE_KIND) == "variant"
    kinds = [variant.child(i).data(0, rw._ROLE_KIND)
             for i in range(variant.childCount())]
    assert kinds[-1] == "global", f"全局条目应在末尾: {kinds}"
    assert "route" in kinds, kinds
    assert items[0].text(rw._COL_NAME).startswith("全局"), items[0].text(rw._COL_NAME)
    # 眼睛列要够宽: 最深的一层是“全局 → 队伍 → 线路”(缩进 5 层), 否则图标会被裁掉
    from wt81111g.icons import ICON_SIZE
    assert rw.EYE_COLUMN_WIDTH >= 5 * win.tree.indentation() + ICON_SIZE, (
        f"眼睛列太窄: {rw.EYE_COLUMN_WIDTH}")

    # 2) 展开后分 A / B 两队, 组内按时间正序
    sides = _children_kind(items[0], "gside")
    assert [s.text(rw._COL_NAME) for s in sides] == ["A 队", "B 队"]
    assert len(_children_kind(sides[0], "glife")) == 2
    assert len(_children_kind(sides[1], "glife")) == 2

    # 3) 眼睛: 点全局条目 → 全部线路交给地图; 再点一次 → 清空
    win._set_lives_visible(win._life_ids_of(items[0]), True)
    app.processEvents()
    assert len(win._visible_lives) == 4
    assert len(win.map_view.global_tracks) == 4, len(win.map_view.global_tracks)
    colors = {t.color.name() for t in win.map_view.global_tracks}
    assert len(colors) == 2, f"A/B 队应分两种颜色: {colors}"
    win._set_lives_visible(win._life_ids_of(items[0]), False)
    app.processEvents()
    assert not win.map_view.global_tracks

    # 4) 标记: 打上 A 后, 条目文字带 [A], 且 marked_global 能找到它
    win.store.set_mark(g.route_id, "A", True)
    win._reload_tree()
    app.processEvents()
    items = _global_items(win)
    assert "[A]" in items[0].text(rw._COL_NAME), items[0].text(rw._COL_NAME)
    assert win.store.marked_global("A").route_id == g.route_id

    # 5) 删除要确认: 选“否”不删, 选“是”才删
    import PyQt6.QtWidgets as qw
    orig = qw.QMessageBox.question
    try:
        qw.QMessageBox.question = staticmethod(
            lambda *a, **k: qw.QMessageBox.StandardButton.No)
        win._delete_global(g.route_id)
        assert win.store.get_global(g.route_id) is not None, "选“否”不应删除"
        qw.QMessageBox.question = staticmethod(
            lambda *a, **k: qw.QMessageBox.StandardButton.Yes)
        win._delete_global(g.route_id)
        assert win.store.get_global(g.route_id) is None, "选“是”应删除"
    finally:
        qw.QMessageBox.question = orig
    app.processEvents()
    assert not _global_items(win)
    results.append("全局录制侧栏: 末尾同级别条目 + A/B 分组 + 眼睛开关 + 标记 + 删除确认 OK")


def test_buttons_and_map_lock(tmpdir: str):
    """按钮分区: 单条录制 3 键 + 全局录制 3 键; 全局录制期间锁定地面底图。"""
    app = QApplication.instance()
    win = make_window(tmpdir, "globals_btn.json")
    win.recorder.stop()
    win.hotkeys.stop()
    win.sys_hotkeys.stop()
    win.show()
    app.processEvents()
    assert win.btn_rec_a.text() == "A 队录制"
    assert win.btn_rec_b.text() == "B 队录制"
    assert win.btn_rec_stop.text() == "停止 A/B 队录制"
    # 平时: 全局录制两个按钮可用, 停止不可用
    assert win.btn_rec_a.isEnabled() and not win.btn_rec_stop.isEnabled()
    # 模拟“正在全局录制”: 录音按钮禁用、停止可用、单条录制按钮也禁用
    win.recorder._global = GlobalRecord(route_id="x", map_key="M",
                                        map_name="地图", created_at="")
    win.recorder._global_side = "A"
    win.recorder._global_waiting = True
    win._update_buttons()
    assert not win.btn_rec_a.isEnabled() and not win.btn_rec_b.isEnabled()
    assert win.btn_rec_stop.isEnabled()
    assert not win.btn_start.isEnabled() and not win.chk_start_on_move.isEnabled()
    assert not win.tree.isEnabled(), "录制期间侧栏也应锁定"
    win.recorder._global = None
    win.recorder._global_waiting = False
    win._update_buttons()
    assert win.btn_rec_a.isEnabled() and not win.btn_rec_stop.isEnabled()
    results.append("全局录制按钮: 分区/互斥启用状态 OK")


def test_map_paint():
    """全局线路真的画到地图上了: A/B 队两种颜色、线宽比单条轨迹细。"""
    from PyQt6.QtGui import QImage
    from wt81111g.route_map_view import COLOR_TEAM_A, COLOR_TEAM_B, GlobalTrack, RouteMapView

    def png(size=256):
        from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
        from PyQt6.QtGui import QColor
        img = QImage(size, size, QImage.Format.Format_RGB32)
        img.fill(QColor("#303030"))
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        buf.close()
        return bytes(ba)

    view = RouteMapView()
    view.resize(400, 400)
    view.set_map_bytes(png(), key="M")
    view.set_global_tracks([
        GlobalTrack(points=[TrackPoint(0.2, 0.3, 0.0), TrackPoint(0.8, 0.3, 1.0)],
                    color=COLOR_TEAM_A),
        GlobalTrack(points=[TrackPoint(0.2, 0.7, 0.0), TrackPoint(0.8, 0.7, 1.0)],
                    color=COLOR_TEAM_B),
    ])
    view.show()
    QApplication.instance().processEvents()
    canvas = QImage(view.size(), QImage.Format.Format_ARGB32)
    view.render(canvas)

    def count(color, tol=60):
        # 抗锯齿会让线条两侧混色, 所以按“通道距离”判近似色, 不用精确相等
        want = (color.red(), color.green(), color.blue())
        n = 0
        for y in range(canvas.height()):
            for x in range(canvas.width()):
                c = canvas.pixelColor(x, y)
                if (abs(c.red() - want[0]) <= tol and abs(c.green() - want[1]) <= tol
                        and abs(c.blue() - want[2]) <= tol):
                    n += 1
        return n

    na, nb = count(COLOR_TEAM_A), count(COLOR_TEAM_B)
    assert na > 50 and nb > 50, (na, nb)
    view.set_global_tracks(None)
    QApplication.instance().processEvents()
    canvas2 = QImage(view.size(), QImage.Format.Format_ARGB32)
    view.render(canvas2)
    assert count(COLOR_TEAM_A) > 0            # 上一帧的画布不受影响
    canvas = canvas2
    assert count(COLOR_TEAM_A) == 0, "清空后不该还留着全局线路"
    results.append("地图: 全局线路按 A/B 队分色绘制 + 可清空 OK")


def test_only_global(tmpdir: str):
    """一张地图**只有全局录制**、没有单条路线时, 侧栏也要能正常挂出条目。"""
    app = QApplication.instance()
    win = make_window(tmpdir, "globals_only.json")
    win.recorder.stop()
    win.hotkeys.stop()
    win.sys_hotkeys.stop()
    win.show()
    app.processEvents()
    win.store.rename_map("MAPONLY", "只有全局")
    g = win.store.new_global("MAPONLY", "只有全局", zone_count=2,
                             zone_signature="A@0.2,0.2;B@0.8,0.8", mode_name="战区攻陷")
    g.sides["A"].append(UnitLife(life_id=f"{g.route_id}:A1", icon="IFV",
                                 points=[TrackPoint(0.2, 0.2, 0.0),
                                         TrackPoint(0.3, 0.25, 2.0)]))
    win.store.add_global(g)
    win._reload_tree()
    app.processEvents()
    top = win.tree.topLevelItem(0)
    assert top.text(rw._COL_NAME) == "只有全局", top.text(rw._COL_NAME)
    mid = top.child(0)
    assert mid.data(0, rw._ROLE_KIND) == "mode"
    assert mid.text(rw._COL_NAME) == "战区攻陷", mid.text(rw._COL_NAME)
    var = mid.child(0)
    assert var.data(0, rw._ROLE_KIND) == "variant"
    items = _children_kind(var, "global")
    assert len(items) == 1, "只有全局录制的地图也要挂出条目"
    # 信息列: “0 条 · 1 局”
    assert "1 局" in top.text(rw._COL_TIME), top.text(rw._COL_TIME)
    results.append("全局录制: 只有全局条目的地图也能正常挂在侧栏 OK")


def main() -> int:
    app = QApplication([])
    tmpdir = tempfile.mkdtemp()
    test_filters()
    test_tracker_basic()
    test_tracker_two_and_death()
    test_tracker_respawn()
    test_tracker_empty_stop()
    test_store(tmpdir)
    test_map_paint()
    test_window(tmpdir)
    test_only_global(tmpdir)
    test_buttons_and_map_lock(tmpdir)
    for line in results:
        print("[OK]", line)
    print(f"\n全部通过: {len(results)} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
