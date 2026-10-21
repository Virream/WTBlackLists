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

from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication

import wt81111g.route_window as rw
from wt81111g.global_record import (
    EMPTY_STOP, GlobalTracker, extract_units, is_drone, recordable,
)
from wt81111g.map_cache import MapCache
from wt81111g.route_store import (
    GLOBAL_SIDES, GlobalRecord, RoutePoint, RouteRecord, RouteStore, TrackPoint, UnitLife,
)

rw.play_sound = lambda action: None
rw.play_error = lambda *a, **k: None

results: list[str] = []

PROT = "#1839A7"     # 重生保护(深蓝) —— 现已不参与逻辑, 仅留作实测记录
BLUE = "#174DFF"     # 我方
YELLOW = "#faC81E"   # 自己


def tank(x, y, color=BLUE, icon="MediumTank", typ="ground_model"):
    return {"type": typ, "icon": icon, "color": color, "x": x, "y": y}


def png_bytes(color: str = "#334455", size: int = 256) -> bytes:
    """内存生成一张纯色 PNG 当作地图底图(不依赖游戏)。"""
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
    from PyQt6.QtGui import QColor
    img = QImage(size, size, QImage.Format.Format_RGB32)
    img.fill(QColor(color))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


# ---------------------------------------------------------------- 单位过滤
def test_filters():
    # 地面载具: 录
    assert recordable("ground_model", "MediumTank")
    assert recordable("ground_model", "IFV")
    # 固定翼 / 直升机: 不录
    assert not recordable("aircraft", "Assault")
    assert not recordable("aircraft", "Fighter")
    assert not recordable("helicopter", "AttackHelicopter")
    # 侦察无人机也**不录**(2026-10-21 决定): 它的 icon 就是 Fighter, 与固定翼分不开
    assert not recordable("aircraft", "Fighter")
    assert not recordable("uav", "uav")
    # 但“认无人机”的辅助函数还在(将来要是能单独认出来就用它)
    assert is_drone("aircraft", "ReconDrone")
    assert is_drone("uav", "scout")
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
    results.append("全局录制: 单位过滤(地面/空中全跳过/静态/超界坐标) OK")


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


def test_tracker_speed():
    """速度用“帧间位移 × 地图边长”算出来(8111 只给摄像机的速度)。

    地图 4096m 宽, 每 0.05s 走 0.001 归一化 = 4.096m → 81.92 m/s ≈ 294 km/h,
    但这个值会被 MAX_SPEED_KMH 截断 —— 所以另用慢速再验一遍。
    """
    rec = GlobalRecord(route_id="g3", map_key="M", map_name="地图", created_at="")
    tr = GlobalTracker(rec, "A", map_size=(4096.0, 4096.0))
    t = 0.0
    # 每帧 0.05s 走 0.00005 归一化 → 0.2048 m / 0.05s = 4.096 m/s ≈ 14.7 km/h
    for i in range(41):
        tr.feed(t, [tank(0.20 + 0.00005 * i, 0.5)])
        t += 0.05
    lives = tr.finish()
    assert len(lives) == 1, f"应只有 1 条线, 实际 {len(lives)}"
    speeds = [p.speed for p in lives[0].points[1:]]
    assert speeds, "没有采样点"
    avg = sum(speeds) / len(speeds)
    assert 10.0 < avg < 20.0, f"速度应约 14.7 km/h, 实际 {avg:.1f}"
    # 地图边长翻倍 → 速度也翻倍
    tr2 = GlobalTracker(rec, "A", map_size=(8192.0, 8192.0))
    t = 0.0
    for i in range(41):
        tr2.feed(t, [tank(0.20 + 0.00005 * i, 0.5)])
        t += 0.05
    sp2 = [p.speed for p in tr2.finish()[0].points[1:]]
    assert 20.0 < sum(sp2) / len(sp2) < 40.0, "地图边长翻倍, 速度也应翻倍"
    results.append("全局跟踪: 速度 = 位移 × 地图边长(缩放到 km/h) OK")


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
                    obj_type="ground_model", map_key="MAP1",
                    points=[TrackPoint(0.1, 0.2, 0.0, 0.0),
                            TrackPoint(0.3, 0.4, 3.5, 42.5)])
    g.sides["A"].append(life)
    store.add_global(g)
    assert store.get_global(g.route_id) is g
    assert life.route_id == life.life_id, "全局线路要能被当成 RouteRecord 用"
    assert life.marks == [] and life.map_key == "MAP1"

    # 重新读盘: 坐标/时间/**速度**都要在
    again = RouteStore(path)
    got = again.get_global(g.route_id)
    assert got is not None and got.count() == 1, "全局条目没有落盘/读回"
    l2 = got.lives("A")[0]
    assert l2.icon == "MediumTank" and l2.map_key == "MAP1"
    assert len(l2.points) == 2 and abs(l2.points[1].t - 3.5) < 1e-9
    assert abs(l2.points[1].x - 0.3) < 1e-9
    assert abs(l2.points[1].speed - 42.5) < 1e-9, "速度没存住"

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

    # 3) 眼睛: 点全局条目 → 4 条线进入地图的“显示中”集合(与单条轨迹同一套)
    win._set_lives_visible(win._life_ids_of(items[0]), True)
    app.processEvents()
    assert len(win._visible_lives) == 4
    assert len(win.map_view.visible_tracks) == 4, len(win.map_view.visible_tracks)
    win._set_lives_visible(win._life_ids_of(items[0]), False)
    app.processEvents()
    assert not win.map_view.visible_tracks

    # 3.5) 回放: 选中全局条目 → 播它下面的全部线路; 只选几条 → 只播那几条
    # (先把“录制完成”走一遍: 录完自动点亮, 且眼睛图标与地图显示保持一致)
    win._on_global_finished(g, "A")
    app.processEvents()
    assert len(win._visible_lives) == 2, "录完应只点亮刚录的 A 队"
    assert len(win.map_view.visible_tracks) == 2, "录完路线应该还在地图上(不能自己藏起来)"
    lit = [it for it in win._iter_life_items()
           if it.data(0, rw._ROLE_ID) in win._visible_lives]
    assert len(lit) == 2, "眼睛图标必须与显示状态联动"
    win._set_lives_visible([l.life_id for s in GLOBAL_SIDES for l in g.lives(s)], True)
    items = _global_items(win)
    win.tree.clearSelection()
    items[0].setSelected(True)
    app.processEvents()
    picked = win._playable_tracks()
    assert len(picked) == 4, [p.route_id for p in picked]
    win._load_playback(picked)
    app.processEvents()
    assert len(win.player.records) == 4 and win._in_playback()
    assert win.map_view.playback_records, "回放对象没交给地图"
    # 真渲染一帧: 必须有底图, 否则 _paint 会直接返回、根本走不到回放光标那段
    win.map_view.set_map_bytes(png_bytes(), key="MAPX")
    app.processEvents()
    img = QImage(win.map_view.size(), QImage.Format.Format_ARGB32)
    for t in (0.0, 0.5, 999.0):    # 0 点/中间/超出末尾(前两者会直接返回原始 TrackPoint)
        win.player.seek(t)
        app.processEvents()
        win.map_view.render(img)
    win._end_playback()
    app.processEvents()
    life_items = list(win._iter_life_items())
    win.tree.clearSelection()
    life_items[0].setSelected(True)
    app.processEvents()
    picked = win._playable_tracks()
    assert len(picked) == 1, f"只选一条就该只播一条, 实际 {len(picked)}"
    assert picked[0].life_id == life_items[0].data(0, rw._ROLE_ID)

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
    """全局线路用**速度配色**画(与单条轨迹同一套), 且实时预览也走这条路径。"""
    from PyQt6.QtGui import QImage
    from wt81111g.route_map_view import (
        _SPEED_BUCKETS, RouteMapView, bucket_index, speed_color,
    )

    view = RouteMapView()
    view.resize(400, 400)
    view.set_map_bytes(png_bytes(), key="M")
    # 一条速度 0 → 50 → 100 的线: 两段的桶中点分别是 25 与 75
    life = UnitLife(
        life_id="L1", icon="MediumTank", obj_type="ground_model", map_key="M",
        points=[TrackPoint(0.2, 0.3, 0.0, 0.0),
                TrackPoint(0.5, 0.5, 1.0, 50.0),
                TrackPoint(0.8, 0.7, 2.0, 100.0)])

    def canvas_of():
        view.show()
        QApplication.instance().processEvents()
        img = QImage(view.size(), QImage.Format.Format_ARGB32)
        view.render(img)
        return img

    def count(img, color, tol=70):
        want = (color.red(), color.green(), color.blue())
        n = 0
        for y in range(img.height()):
            for x in range(img.width()):
                c = img.pixelColor(x, y)
                if (abs(c.red() - want[0]) <= tol and abs(c.green() - want[1]) <= tol
                        and abs(c.blue() - want[2]) <= tol):
                    n += 1
        return n

    # 配色基准必须与单条轨迹**完全一致**: 同样的速度 → 同样的颜色
    rec = RouteRecord(route_id="R1", map_key="M", map_name="M", vehicle="T",
                      created_at="", duration=2.0, start_x=0.2, start_y=0.3,
                      points=[RoutePoint(p.x, p.y, p.t, p.speed) for p in life.points])
    assert view._vmax(rec) == view._vmax(life), (
        f"速度基准不一致: 单条 {view._vmax(rec)} vs 全局 {view._vmax(life)}")
    for speed in (10.0, 37.5, 80.0):
        a = bucket_index(speed, view._vmax(rec))
        b = bucket_index(speed, view._vmax(life))
        assert a == b, f"{speed} km/h 在两边的颜色档不同: {a} vs {b}"

    # 选中层(与单条轨迹一样): 按速度分档上色
    view.set_highlight([life])
    idx = bucket_index(25.0, 100.0)
    want = speed_color((idx + 0.5) / _SPEED_BUCKETS * 100.0, 100.0)
    assert count(canvas_of(), want) > 20, "选中/高亮的全局线路没有按速度配色画出来"

    # 录制中的实时预览走同一条路径
    view.set_highlight(None)
    view.set_live_multi([life])
    img = canvas_of()
    assert count(img, want) > 20, "全局录制时没有边录边画"
    view.set_live_multi(None)
    assert not count(canvas_of(), want), "清空后不该还留着全局线路"
    results.append("地图: 全局线路按速度配色绘制 + 录制中实时预览 OK")


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


def test_live_layer_no_rebuild():
    """录制中的“边录边画”**不能每帧重建整层**(曾经把 25Hz 拖到 8Hz、缩放卡成幻灯片)。

    断言两件事:
    * 新增点 → 只在旧图层上补几段, 层对象还是同一个(不重建);
    * 缩放 → 先按比例贴旧图(返回缩放比 ≠ 1), 稍后才重建。
    """
    from wt81111g.route_map_view import RouteMapView

    view = RouteMapView()
    view.resize(600, 500)
    view.set_map_bytes(png_bytes(), key="M")
    life = UnitLife(life_id="L1", icon="IFV", obj_type="ground_model", map_key="M",
                    points=[TrackPoint(0.2 + 0.01 * i, 0.3 + 0.005 * i, i * 0.2, 30.0)
                            for i in range(20)])
    view.set_live_multi([life])
    view.show()
    app = QApplication.instance()
    app.processEvents()
    canvas = QImage(view.size(), QImage.Format.Format_ARGB32)
    view.render(canvas)
    layer = view._live_layer
    assert layer is not None, "没有建出实时层"

    # 新增点 → 只补段, 层对象不变(速度基准没变)
    for i in range(20):
        life.points.append(TrackPoint(0.2 + 0.01 * i, 0.31 + 0.005 * i,
                                      4.0 + i * 0.2, 30.0))
    view.render(canvas)
    assert view._live_layer is layer, "新点不该导致整层重建"
    got = view._layer_live()
    assert got is not None and got[1] == (1.0, 1.0)

    # 缩放 → 先缩放贴旧图(立即反馈), 不重建
    view._zoom *= 1.25
    view.update()
    view.render(canvas)
    got = view._layer_live()
    assert got is not None and got[0] is layer, "缩放时不该立刻重建"
    assert abs(got[1][0] - 1.25) < 1e-6, f"应按缩放比贴图, 实际 {got[1]}"

    # 停手后重建(定时器触发)
    view._live_zoom_at = 0.0          # 模拟“早就停手了”
    view._rebuild_live_now()
    app.processEvents()
    view.render(canvas)
    assert view._live_layer is not layer, "停手后应重建一次以恢复清晰度"

    # 重建之后,"边录边画”必须能继续长(曾经: 定时器被每帧重置 → 永远不重建 → 线路卡住)
    layer2 = view._live_layer
    for i in range(10):
        life.points.append(TrackPoint(0.2 + 0.01 * i, 0.32 + 0.005 * i,
                                      6.0 + i * 0.2, 30.0))
    view.render(canvas)
    assert view._live_layer is layer2, "重建后新增点不该再触发重建"
    n_before = view._live_seg[life.route_id].n
    for i in range(10):
        life.points.append(TrackPoint(0.2 + 0.01 * i, 0.33 + 0.005 * i,
                                      8.0 + i * 0.2, 30.0))
    view.render(canvas)
    assert view._live_seg[life.route_id].n > n_before, "缩放后线路不再增长(实时显示卡住)"

    # 速度基准(这条线的最高速)涨了 → 桶索引/颜色全变 → 必须整层重建
    before_vmax = view._live_seg[life.route_id].vmax
    life.points.append(TrackPoint(0.55, 0.55, 30.0, before_vmax * 2))
    view.render(canvas)
    assert view._live_seg[life.route_id].vmax > before_vmax, "速度基准应跟着最高速走"
    results.append("全局录制: 边录边画不重建整层 + 缩放先缩放贴图 + 缩放后仍能增长 OK")


def test_playback_timeline():
    """全局线路的时间戳是**整局时刻**(可能从 02:00 才开始), 回放必须也按绝对时刻走。

    曾经: 回放器从 0 开始推进, 而点的 t 是 120 → `bisect` 认为“还没到第一个点”,
    计时器在动、线路一动不动(2026-10-21 用户实测)。
    """
    from wt81111g.route_playback import RoutePlayer
    from wt81111g.route_store import point_at

    def life(lid, t0, n=31):
        return UnitLife(life_id=lid, icon="IFV", obj_type="ground_model", map_key="M",
                        points=[TrackPoint(0.2 + 0.01 * i, 0.3, t0 + i, 40.0)
                                for i in range(n)])

    player = RoutePlayer(tick_ms=10)
    single = life("L1", 120.0)          # 02:00 出现, 02:30 消失
    player.load([single])
    assert player.span == (120.0, 150.0), player.span
    assert player.time == 120.0, "应复位到时间轴起点(不是 0)"
    assert player.duration == 30.0 and player.end == 150.0
    for t, want_x in ((120.0, 0.20), (130.0, 0.30), (145.0, 0.45)):
        player.seek(t)
        assert player.time == t
        assert abs(point_at(single.points, t).x - want_x) < 1e-9, (
            f"t={t} 光标位置不对")

    # 多条一起播: 时间轴 = 最早开始 → 最晚结束(按真实时刻一起播)
    a, b = life("A", 100.0, 21), life("B", 200.0, 11)
    player.load([a, b])
    assert player.span == (100.0, 210.0), player.span
    assert player.time == 100.0
    # 单条线路(点从 0 开始)行为不变
    rec = RouteRecord(route_id="R", map_key="M", map_name="M", vehicle="T",
                      created_at="", duration=5.0, start_x=0.3, start_y=0.3,
                      points=[RoutePoint(0.3, 0.3, 0.0, 0.0),
                              RoutePoint(0.4, 0.4, 5.0, 40.0)])
    player.load([rec])
    assert player.span == (0.0, 5.0) and player.time == 0.0
    results.append("全局录制回放: 时间轴按绝对时刻(02:00 开始的线路也会动) OK")


def main() -> int:
    app = QApplication([])
    tmpdir = tempfile.mkdtemp()
    test_filters()
    test_tracker_basic()
    test_tracker_two_and_death()
    test_tracker_speed()
    test_tracker_empty_stop()
    test_store(tmpdir)
    test_map_paint()
    test_live_layer_no_rebuild()
    test_playback_timeline()
    test_window(tmpdir)
    test_only_global(tmpdir)
    test_buttons_and_map_lock(tmpdir)
    for line in results:
        print("[OK]", line)
    print(f"\n全部通过: {len(results)} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
