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
    EMPTY_STOP, VIEW_CHANGE_GRACE, GlobalTracker, extract_units, fit_frame,
    frame_anchors, is_drone, recordable,
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

    # 敌方色 → 一律不录(死亡回放摄像机把镜头移到敌方车上时会带进来)
    # 实测两次 B 队录制都混进一条 #fa0C00 的 A 队轻坦, 与本条目 A 队同名线路
    # 逐点吻合(平均差 0.8m) —— 8111 自己标了敌方色, 按 color 过滤即可。
    red = tank(0.6, 0.6, color="#fa0C00", icon="LightTank")
    units, skipped = extract_units([
        tank(0.3, 0.3, color=BLUE),                     # 我方蓝 → 录
        tank(0.4, 0.4, color="#faC81E", icon="Player"),  # 自己黄(摄像机所在) → 录
        tank(0.5, 0.5, color="#67D756"),                 # 队友绿 → 录
        red,                                            # 敌方红 → 跳过
        {"type": "ground_model", "icon": "LightTank",    # color[] 数组形式的红
         "x": 0.7, "y": 0.7, "color[]": [250, 12, 0]},
    ])
    assert len(units) == 3, [(u.x, u.color) for u in units]
    assert units[-1].color != "#fa0C00"
    assert skipped.get(("敌方色", "LightTank")) == 2, skipped
    results.append("全局录制: 单位过滤(地面/空中全跳过/静态/超界坐标/敌方色) OK")


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


def test_timeline_note():
    """A/B 两队的时间轴对比(写日志用): 两边都从 0 开始, 只比结尾。

    用户要求的对齐机制是“第一辆载具出现 = 0 点”, 两次录制各自取到 0;
    但 8111 只给观战那一方的单位, 所以两侧“第一辆”未必是同一瞬间 ——
    结尾差就是这两个量的**合计**(分不清是某队先打光还是 0 点错开), 只能作为诊断。
    """
    rec = GlobalRecord(route_id="g9", map_key="M", map_name="地图", created_at="")
    assert rec.timeline_note() == "无线路"

    def life(side: str, t0: float, t1: float) -> UnitLife:
        lf = UnitLife(life_id=f"g9:{side}1", icon="T", obj_type="ground_model",
                      map_key="M", side=side)
        lf.points = [TrackPoint(0.2, 0.2, t0, 0.0), TrackPoint(0.3, 0.3, t1, 30.0)]
        return lf

    rec.sides["A"] = [life("A", 0.0, 500.0), life("A", 5.0, 300.0)]
    rec.sides["B"] = [life("B", 0.0, 500.2)]
    note = rec.timeline_note()
    assert "A 队 2 条 0~500.0s" in note and "B 队 1 条 0~500.2s" in note, note
    assert "结尾对齐" in note, note            # 差 0.2s ≤ 0.5s

    rec.sides["B"] = [life("B", 0.0, 503.0)]
    assert "结尾差 -3.0s" in rec.timeline_note()
    results.append("全局录制: A/B 时间轴对比(0 点 + 结尾差诊断) OK")


def test_frame_align():
    """观战玩家上了固定翼 → 8111 切成空战图, 单位坐标整体被缩放/平移。

    回归(2026-10-21 用户实测): 不换算的话帧间最近邻全部脱靶, 正在画的线路会
    **突然断开、然后在别的地方重新开始**。用静态地物(respawn_base_tank /
    capture_zone)当锚点把坐标换回录制开始时的地面坐标系。
    """
    # 地锚(参考坐标系): 32 个重生点 + 3 个战区
    ground = [(0.10 + (i % 8) * 0.02, 0.15 + (i // 8) * 0.03) for i in range(32)]
    ground += [(0.35, 0.55), (0.66, 0.55), (0.50, 0.32)]
    AIR_SCALE, AIR_OFF = 0.4, (0.06, 0.10)

    def to_air(p):
        """地面坐标 → 空战图坐标(缩放 + 平移, 与实测一致)。"""
        return (p[0] * AIR_SCALE + AIR_OFF[0], p[1] * AIR_SCALE + AIR_OFF[1])

    air = [to_air(p) for p in ground]

    # ① 拟合本身: 从空战图坐标能反解出缩放/平移, 且能原样还原回来
    fit = fit_frame(ground, air)
    assert fit is not None, "应该能拟合出变换"
    # 方向是“空战图 → 地面图”, 所以尺度是 1/0.4 = 2.5
    assert abs(fit.scale - 1.0 / AIR_SCALE) < 1e-6, fit.describe()
    assert not fit.same_as_reference
    for g, a in zip(ground, air):
        back = fit.apply(a[0], a[1])
        assert abs(back[0] - g[0]) < 1e-6 and abs(back[1] - g[1]) < 1e-6, \
            f"{a} 应换算回 {g}, 实际 {back}"
    # 同一个坐标系 → 判为“同坐标系”, 不做换算
    same = fit_frame(ground, ground)
    assert same is not None and same.same_as_reference
    # 少几个锚点(某视角少报了一些重生点)也照样能拟合
    partial = fit_frame(ground, air[:-3])
    assert partial is not None and abs(partial.scale - 1.0 / AIR_SCALE) < 0.05, \
        partial.describe() if partial else "None"
    # 锚点太少 → 拒绝拟合(宁可这一帧不动, 也不乱换算)
    assert fit_frame(ground, air[:3]) is None
    assert frame_anchors([{"type": "airfield", "icon": "none", "sx": 0.1},
                          tank(0.5, 0.5)] + [
                             {"type": "respawn_base_tank", "x": 0.2, "y": 0.3}]) == [(0.2, 0.3)]

    # ② 跟踪: 前半段在地面视角, 后半段切到空战图 → **线路不能断**
    rec = GlobalRecord(route_id="g7", map_key="M", map_name="地图", created_at="")
    tr = GlobalTracker(rec, "A")
    t = 0.0

    def frame(scale_points, units):
        return units + [{"type": "respawn_base_tank", "icon": "respawn_base_tank",
                         "x": p[0], "y": p[1]} for p in scale_points]

    for i in range(20):                       # 地面视角
        tr.feed(t, frame(ground, [tank(0.30 + 0.002 * i, 0.40),
                                  tank(0.70 - 0.002 * i, 0.60, icon="IFV")]))
        t += 0.05
    for i in range(20, 40):                   # 换成空战图(坐标整体缩放/平移)
        tr.feed(t, frame(air, [tank(*to_air((0.30 + 0.002 * i, 0.40))),
                               tank(*to_air((0.70 - 0.002 * i, 0.60)), icon="IFV")]))
        t += 0.05
    assert tr.frame_changes == 1, f"应识别出一次坐标系变化, 实际 {tr.frame_changes}"
    lives = tr.finish()
    assert len(lives) == 2, f"坐标系变化不该把线路拆开, 实际 {len(lives)} 条"
    assert all(not lf.dead for lf in lives), "没人消失, 不该判击毁"
    for lf in lives:
        assert lf.points[-1].t > 1.5, "空战图那段也应该继续录到点"
        assert 0.0 <= lf.points[-1].x <= 1.0 and 0.0 <= lf.points[-1].y <= 1.0, \
            "换算后必须回到地面坐标系内"
    by_y = sorted(lives, key=lambda lf: lf.points[0].y)
    assert abs(by_y[0].points[-1].x - (0.30 + 0.002 * 39)) < 0.01, by_y[0].points[-1]
    assert abs(by_y[1].points[-1].x - (0.70 - 0.002 * 39)) < 0.01, by_y[1].points[-1]

    # ③ 换视角的那几秒**不判击毁**(单位消失更可能是“看不见了”)
    rec2 = GlobalRecord(route_id="g8", map_key="M", map_name="地图", created_at="")
    tr2 = GlobalTracker(rec2, "A")
    t = 0.0
    for i in range(20):                       # A/B/C 三辆都在
        tr2.feed(t, frame(ground, [tank(0.30 + 0.002 * i, 0.40),
                                   tank(0.50, 0.50, icon="IFV"),
                                   tank(0.70, 0.70, icon="LightTank")]))
        t += 0.05
    for i in range(20, 60):                   # 切到空战图: B 看不见了
        tr2.feed(t, frame(air, [tank(*to_air((0.30 + 0.002 * i, 0.40))),
                               tank(*to_air((0.70, 0.70)), icon="LightTank")]))
        t += 0.05
    gone = [lf for lf in tr2.lives if abs(lf.points[0].y - 0.50) < 1e-6]
    assert gone, "B 那条线应该已经结束"
    assert gone[0].dead is False, "刚换视角就消失, 不能判成被击毁"
    # 换成空战图 5 秒后 C 也消失了(视野里真的没了) → 这时才允许判击毁
    t += VIEW_CHANGE_GRACE + 1.0
    for k in range(30):
        tr2.feed(t, frame(air, [tank(*to_air((0.30, 0.40)))]))
        t += 0.05
    killed = [lf for lf in tr2.lives if abs(lf.points[0].y - 0.70) < 1e-6]
    assert killed and killed[0].dead is True, "过了宽限期再消失 → 应判为击毁"

    # ④ 交叉校验日志: 拟合尺度应该等于“当前图框 ÷ 参考图框”
    #    (真实数据是 坦克图框 4096 m ↔ 空中图框 65536 m = 16×; 这里合成数据是 1/0.4 = 2.5×)
    tr.seen_extent = (4096.0 / AIR_SCALE, 4096.0 / AIR_SCALE)
    msg = tr._cross_check(tr.aligner.fit)
    assert "与拟合一致" in msg, msg
    tr.seen_extent = (4096.0, 4096.0)          # 图框没变 → 说明 fit 不该是 2.5×
    assert "不一致" in tr._cross_check(tr.aligner.fit)
    tr.seen_extent = None                      # 拿不到 map_info 也不能炸
    assert "无法交叉校验" in tr._cross_check(tr.aligner.fit)
    results.append("全局录制: 空战图坐标换算(线路不断/换视角不误判击毁) OK")


def test_tracker_dead_flag():
    """“被击毁”判定(用户 2026-10-21 口径): 这条线的 icon 消失时, 场上**还有别的单位**。

    * 有人死了但别人还活着 → `dead=True`(回放时要变灰 + 画红叉);
    * **全场一起消失**(回到车库/录制结束) → `dead=False` —— 用户特意提过
      “pro 能做到整局一次不死”, 那种情况绝不能画红叉;
    * 手动结束录制时还活着的线 → `dead=False`。
    """
    # ① 两辆车, 其中一辆中途消失(另一辆还在) → 只有它算阵亡
    rec = GlobalRecord(route_id="g5", map_key="M", map_name="地图", created_at="")
    tr = GlobalTracker(rec, "A")
    t = 0.0
    for i in range(10):
        tr.feed(t, [tank(0.20 + 0.001 * i, 0.30),
                    tank(0.20 + 0.001 * i, 0.70, icon="IFV")])
        t += 0.05
    for i in range(10, 40):                       # 只有 y=0.30 那辆继续跑
        tr.feed(t, [tank(0.20 + 0.001 * i, 0.30)])
        t += 0.05
    lives = tr.finish()
    killed = [lf for lf in lives if abs(lf.points[0].y - 0.70) < 1e-9][0]
    alive = [lf for lf in lives if abs(lf.points[0].y - 0.30) < 1e-9][0]
    assert killed.dead is True, "中途消失(场上还有别的单位) → 应判为被击毁"
    assert alive.dead is False, "活到录制结束 → 不算被击毁"

    # ② 全场一起消失(回车库) → 谁都不算阵亡
    rec2 = GlobalRecord(route_id="g6", map_key="M", map_name="地图", created_at="")
    tr2 = GlobalTracker(rec2, "A")
    t = 0.0
    for i in range(10):
        tr2.feed(t, [tank(0.3 + 0.001 * i, 0.3), tank(0.6 + 0.001 * i, 0.6, icon="IFV")])
        t += 0.05
    for _ in range(int(EMPTY_STOP * 10) + 5):      # 所有 icon 一起消失
        t += 0.2
        if tr2.feed(t, []):
            break
    assert all(not lf.dead for lf in tr2.finish()), \
        "全场一起消失(录制结束)不能算被击毁 —— 有人整局一次不死"

    # ③ 落盘往返
    import json
    row = json.loads(json.dumps(killed.to_json()))
    back = type(killed).from_json(row)
    assert back.dead is True, row
    assert killed.side == "A" and back.side == "A", "队号(A/B)也要跟着落盘"
    assert type(alive).from_json(json.loads(json.dumps(alive.to_json()))).dead is False
    # 旧数据没有 side 字段 → 由 GlobalRecord.from_json 按所在分组补回来
    legacy = {"route_id": "g9", "map_key": "M", "sides": {
        "B": [{"id": "g9:B1", "pts": [[0.0, 0.2, 0.2, 0.0], [1.0, 0.3, 0.3, 10.0]]}]}}
    rec9 = GlobalRecord.from_json(legacy)
    assert rec9 is not None and rec9.lives("B")[0].side == "B"
    results.append("全局跟踪: 被击毁判定(icon 中途消失 / 全场消失不算 / 落盘往返) OK")


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


def test_spawn_start_check():
    """新建线路时校验起点像不像"刚重生"（用户 2026-10-22 报的 bug 的诊断）。

    场景: B 队录制里有一条线起点离最近重生点 183m、开局速度就 39km/h —— 一辆
    **已经在路上跑**的车被当成了新单位。根因是 `_new_life()` 的起点取当帧位置,
    所以任何"中途才被 8111 上报"的单位都会在路中间开一条线。
    这里验证诊断能把它抓出来(只记日志, 不改变任何行为)。
    """
    from types import SimpleNamespace

    spawns = [SimpleNamespace(x=0.30, y=0.61), SimpleNamespace(x=0.33, y=0.62)]
    rec = GlobalRecord(route_id="g9", map_key="M", map_name="地图", created_at="")
    tr = GlobalTracker(rec, "B", map_size=(4000.0, 4000.0), spawns=spawns)

    t = 0.0
    for i in range(10):                      # ① 正常重生: 从重生点出发
        tr.feed(t, [tank(0.300 + 0.001 * i, 0.610)])
        t += 0.05
    # ② 一辆"凭空出现在路中间"的车(离最近重生点 ≈183m 那种)
    for i in range(60):
        tr.feed(t, [tank(0.300 + 0.001 * 10, 0.610),      # 原来那辆停着
                    tank(0.4081 + 0.0004 * i, 0.6155, icon="LightTank")])
        t += 0.05
    lives = tr.finish()
    assert len(lives) == 2, len(lives)
    assert len(tr.suspicious) == 1, tr.suspicious
    lid, at, dist_m, icon = tr.suspicious[0]
    assert lid.endswith("B2"), lid
    assert icon == "LightTank"
    assert dist_m > 150.0, dist_m               # 实测就是 180m 量级
    assert at > 0.4, at

    # 没有重生点数据时不检查(不能凭空报警)
    tr2 = GlobalTracker(GlobalRecord(route_id="g10", map_key="M", map_name="地", created_at=""),
                        "B")
    for i in range(10):
        tr2.feed(i * 0.05, [tank(0.5 + 0.001 * i, 0.5)])
    tr2.finish()
    assert not tr2.suspicious
    results.append("全局录制: 起点不在重生点附近的线路能被抓出来(只记日志) OK")


def test_kill_timeline():
    """用 hudmsg 的击毁时刻"时间差模式"反推摄像机挂着那位玩家的死亡时刻。

    背景(用户 2026-10-22): 录制全局路线时视角挂在我方一位玩家身上, 他阵亡后
    8111 依旧持续上报(icon 不消失) → 那条线拿不到死亡时刻。其余玩家正常消失判死。
    思路: 本局所有线路的结束时刻 ≈ hudmsg 击毁时刻 + 一个**未知固定偏移**
    (绝对偏移不可用: 录像从服务器端就开始、准备阶段的 15 秒还是假数字),
    所以扫一遍偏移量取配对最多的, 对不上的击毁就是那位玩家的死亡。
    """
    from wt81111g import kill_timeline as kt

    # ---- 1) 只认"击毁了", 排除"引燃了 / 获得嘉奖", 并去掉零宽空格
    class FakeClient:
        def __init__(self, rows):
            self.rows = rows
            self.calls = []

        def hudmsg(self, a, b):
            self.calls.append((a, b))
            return {"events": [], "damage": self.rows}

    rows = [
        {"id": 10, "time": 5, "msg": "A (豹 2A4) 击毁了 B (T-64A)"},
        {"id": 11, "time": 6, "msg": "C (护卫坦克 57) 的\u200b攻击引燃了 D (黄蜂)"},
        {"id": 12, "time": 7, "msg": "E (豹 2A4) 获得嘉奖“装甲克星”"},
        {"id": 13, "time": 8, "msg": "F (T-80) 击\u200b毁\u200b了 G (M1)"},
    ]
    client = FakeClient(rows)
    assert kt.read_cursor(client) == 13
    new = kt.read_new_kills(client, 10)
    assert [r["id"] for r in new] == [13], new          # 11/12 不是击杀, 10 等于游标
    assert "\u200b" not in new[0]["msg"], new[0]["msg"]

    # ---- 2) 两队各自抓到的消息要按 id 去重
    rec = GlobalRecord(route_id="g20", map_key="M", map_name="地图", created_at="",
                       duration=100.0)
    rec.kills = [{"id": 1, "t": 5.0, "msg": "x"}, {"id": 2, "t": 6.0, "msg": "y"},
                 {"id": 1, "t": 5.0, "msg": "x"}]       # 模拟两队都抓到了 id=1
    assert [k["id"] for k in kt.merge_kills(rec)] == [1, 2]

    # ---- 3) 偏移搜索 + 未配对识别
    def life(lid, side, t1, dead, last_move=None):
        """造一条线: 末点 t1; 若给 last_move 则它最后一段不再移动。"""
        pts = [TrackPoint(0.2, 0.2, 0.0, 0.0)]
        if last_move is not None:
            pts.append(TrackPoint(0.3, 0.3, float(last_move), 30.0))
            if t1 > last_move:                       # 之后残骸不动
                pts.append(TrackPoint(0.3, 0.3, float(t1), 0.0))
        else:
            pts.append(TrackPoint(0.4, 0.4, float(t1), 30.0))
        lf = UnitLife(life_id=lid, icon="MediumTank", obj_type="ground_model",
                      map_key="M", points=pts, side=side, dead=dead)
        return lf

    rec2 = GlobalRecord(route_id="g21", map_key="M", map_name="地图", created_at="",
                        duration=100.0)
    # A 队: 两条正常阵亡(10s / 30s) + 一条"被摄像机挂着"(活到最后, 70s 后不动)
    rec2.sides["A"] = [life("A1", "A", 10.0, True),
                       life("A2", "A", 30.0, True),
                       life("A3", "A", 100.0, False, last_move=70.0)]
    # B 队: 一条正常阵亡(50s)
    rec2.sides["B"] = [life("B1", "B", 50.0, True)]
    # hudmsg: 比赛秒数比我们的 t 小 5 秒 → 偏移 +5
    rec2.kills = [{"id": 100 + i, "t": t, "msg": "x (载具) 击毁了 y (载具)"}
                  for i, t in enumerate([5.0, 25.0, 45.0, 65.0])]

    report = kt.analyze(rec2)
    assert abs(report["offset"] - 5.0) < 0.2, report["offset"]
    assert len(report["pairs"]) == 3, report["pairs"]     # 10/30/50 都能配上
    assert report["unmatched"] == [65.0], report["unmatched"]
    assert [s[0] for s in report["silent"]] == ["A3"], report["silent"]

    # ---- 4) 未配对的击毁 → 分配给"最后移动时刻"最接近的静默线路
    picked = kt.pick_death_times(report)
    assert picked == [("A3", 70.0)], picked
    text = kt.describe(report)
    assert "未配对击毁时刻 65s" in text and "A3" in text, text
    results.append("全局录制: hudmsg 击毁时刻能反推摄像机那位玩家的死亡时刻 OK")


def test_zone_layout_pick(tmpdir: str):
    """回放 AB 队线路时必须用**全局条目自己**的战区布局(可能是另一个交战区)。

    用户 2026-10-22 实测: 同一张图先录了 3 条单路(交战区 #新), 之后录 AB 队全局
    (#旧, **另一个区域**)。回放 AB 队时地图上画的是单路那套战区位置 —— 因为
    `_zone_signature_for_view()` 把 `UnitLife` 当“没有指纹”跳过, 于是
    `layout_for(key, "")` 回退到 `layouts[0]`(最后**新出现**的那套 = 单路的)。
    顺带验证: 战区归属要跟着回放时间轴推进, 而不是一张静态快照。
    """
    from wt81111g.zones import Zone
    from wt81111g.zones import signature as zone_sig

    app = QApplication.instance()
    win = make_window(tmpdir, "zones_win.json")
    win.recorder.stop()
    win.hotkeys.stop()
    win.sys_hotkeys.stop()

    def zones(points):
        return [Zone(label=label, x=x, y=y) for label, x, y in points]

    old_pts = [("A", 0.30, 0.30), ("B", 0.40, 0.40), ("C", 0.50, 0.50)]  # AB 队区域
    new_pts = [("A", 0.70, 0.70), ("B", 0.80, 0.80), ("C", 0.90, 0.90)]  # 单路区域
    old_sig, new_sig = zone_sig(zones(old_pts)), zone_sig(zones(new_pts))
    assert old_sig != new_sig, "构造失败: 两套布局指纹居然一样"

    win.store.remember_zones("MAPZ", zones(old_pts))     # 先看到的老布局
    win.store.remember_zones("MAPZ", zones(new_pts))     # 后看到的新布局 → 队首
    assert win.store.map_zones["MAPZ"][0]["sig"] == new_sig, "构造失败: 新布局应在队首"

    g = win.store.new_global("MAPZ", "地图 Z", zone_count=3,
                             zone_signature=old_sig, mode_name="战区控制")
    # 战区状态时间轴: 0s 中立 → 5s 我方占领
    g.zone_states = [
        {"t": 0.0, "zones": [{"label": "A", "x": 0.30, "y": 0.30,
                              "side": "neutral", "color": "#ffFFFA", "blink": 0}]},
        {"t": 5.0, "zones": [{"label": "A", "x": 0.30, "y": 0.30,
                              "side": "ours", "color": "#174DFF", "blink": 0}]},
    ]
    for k in range(2):
        g.sides["A"].append(UnitLife(
            life_id=f"{g.route_id}:A{k}", icon="MediumTank", obj_type="ground_model",
            map_key="MAPZ",
            points=[TrackPoint(0.3, 0.3, 0.0), TrackPoint(0.4, 0.4, 8.0)]))
    win.store.add_global(g)
    win._reload_tree()
    app.processEvents()

    win._load_playback(list(g.lives("A")))
    app.processEvents()
    assert win._in_playback()

    # 指纹必须是 AB 队那个区域, 而不是“最后新出现”的单路那套
    assert win._zone_signature_for_view() == old_sig, win._zone_signature_for_view()

    win.map_view.set_map_bytes(png_bytes(), key="MAPZ")
    win.player.seek(0.0)
    app.processEvents()
    got = win.map_view.zones
    assert got and abs(got[0]["x"] - 0.30) < 1e-6, f"战区位置应取自 AB 队布局: {got}"
    assert got[0]["side"] == "neutral", got

    win.player.seek(6.0)                    # 越过 5s 那条 → 归属变成我方
    app.processEvents()
    assert win.map_view.zones[0]["side"] == "ours", "回放应跟着时间轴推进战区归属"

    win._end_playback()
    app.processEvents()
    results.append("全局录制回放: 战区用条目自己的布局(不是最新那套) + 归属随时间轴变化 OK")


def main() -> int:
    app = QApplication([])
    tmpdir = tempfile.mkdtemp()
    test_filters()
    test_tracker_basic()
    test_tracker_two_and_death()
    test_tracker_speed()
    test_tracker_empty_stop()
    test_tracker_dead_flag()
    test_timeline_note()
    test_frame_align()
    test_store(tmpdir)
    test_map_paint()
    test_live_layer_no_rebuild()
    test_playback_timeline()
    test_spawn_start_check()
    test_kill_timeline()
    test_zone_layout_pick(tmpdir)
    test_window(tmpdir)
    test_only_global(tmpdir)
    test_buttons_and_map_lock(tmpdir)
    for line in results:
        print("[OK]", line)
    print(f"\n全部通过: {len(results)} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())


