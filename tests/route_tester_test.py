# -*- coding: utf-8 -*-
"""跑图线路测试器 验证脚本(离屏): 存储 / 录制状态机 / 窗口与侧栏。

用法: python tests/route_tester_test.py
不依赖游戏与 8111: 用 FakeClient 模拟 8111 数据。
"""
import hashlib
import math
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QPoint, QPointF, Qt
from PyQt6.QtGui import QColor, QIcon, QImage, QPixmap
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QAbstractItemView, QApplication

import wt81111g.route_window as rw
from wt81111g.map_cache import MapCache
from wt81111g.icons import ICON_SIZE, circle_icon, eye_icon
from wt81111g.route_map_view import (
    CURSOR_DIR_LEN, PLAYER_DIR_H, PLAYER_DIR_W,
    RouteMapView, build_speed_segments, cursor_dir_points, player_dir_points,
)
from wt81111g.route_playback import PLAYBACK_SPEEDS, RoutePlayer
from wt81111g.route_recorder import RouteRecorder, _is_player_obj
from wt81111g.route_store import (
    RouteMark, RoutePoint, RouteRecord, RouteStore, fmt_speed, fmt_time, fmt_time_ms,
    point_at, pretty_vehicle, split_points,
)
from wt81111g.route_window import RouteTesterWindow
from wt81111g.zones import (
    Spawn, Zone, ground_spawns, mode_name, ownership_text, side_of, signature, spawn_summary,
    spawns_from_objects, spawns_to_json, zone_state, zones_from_objects,
)

# 测试时不播放系统提示音
rw.play_sound = lambda action: None
rw.play_error = lambda *a, **k: None

results: list[str] = []


class FakeClient:
    """模拟 8111 客户端: 可控玩家位置 / 速度 / 是否在地图中。"""

    def __init__(self):
        self.x, self.y = 0.5, 0.5
        self.speed = 0.0
        self.vehicle = "germ_leopard_2pl"
        self.in_map = True

    def indicators(self):
        return {"valid": True, "type": self.vehicle, "speed": self.speed}

    def map_objects(self):
        if not self.in_map:
            return []
        # 实测格式: 玩家靠 icon == "Player" 标识, type 为载具类别(ground_model)
        return [{"type": "ground_model", "icon": "Player",
                 "x": self.x, "y": self.y, "dx": -0.67, "dy": 0.74}]

    def map_image(self):
        return None


def png_solid(color: str, size: int = 64) -> bytes:
    """内存生成一张纯色 PNG(用做测试时的地图底图, 不依赖游戏运行)。"""
    img = QImage(size, size, QImage.Format.Format_RGB32)
    img.fill(QColor(color))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def png_pattern(size: int = 256, shift: int = 0) -> bytes:
    """内存画一张有内容的“地图”(8×8 色块棋盘放大), 用于测“同图不同分辨率”。

    同一 `shift` 的 128/256/... 版本内容一致、字节不同 —— 正好复现 8111 的行为。
    """
    src = QImage(8, 8, QImage.Format.Format_RGB32)
    for y in range(8):
        for x in range(8):
            src.setPixelColor(x, y, QColor((x * 32 + shift * 17) % 256,
                                           (y * 32 + 40) % 256,
                                           ((x + y) * 16 + shift * 60) % 256))
    big = src.scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio,
                     Qt.TransformationMode.FastTransformation)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    big.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def test_helpers():
    assert fmt_time(0) == "00:00"
    assert fmt_time(30.4) == "00:30"
    assert fmt_time(97.0) == "01:37"
    # 悬停预览用毫秒精度(含进位边界)
    assert fmt_time_ms(0) == "00:00.000"
    assert fmt_time_ms(30.123) == "00:30.123"
    assert fmt_time_ms(97.4567) == "01:37.457"
    assert fmt_time_ms(59.9996) == "01:00.000", fmt_time_ms(59.9996)
    # 速度自适应: 陆战整数不显示小数, 空战有小数则保留 1 位
    assert fmt_speed(68.0) == "68"
    assert fmt_speed(-31.0) == "-31"
    assert fmt_speed(89.072037) == "89.1"
    assert pretty_vehicle("germ_leopard_2pl") == "Leopard 2PL"
    assert pretty_vehicle("us_m1_abrams") == "M1 Abrams"
    assert pretty_vehicle("tankModels/germ_leopard_2a4_pzbtl_123") == "Leopard 2A4 Pzbtl 123"
    assert pretty_vehicle("") == "未知载具"
    results.append("fmt_time / pretty_vehicle OK")


def test_player_detect():
    """玩家识别: 实测靠 icon == "Player"(type 为载具类别)。"""
    assert _is_player_obj({"type": "ground_model", "icon": "Player"})
    assert _is_player_obj({"type": "aircraft", "icon": "player"})   # 大小写兼容
    assert _is_player_obj({"type": "player"})                       # 旧版本写法
    assert not _is_player_obj({"type": "respawn_base_tank", "icon": "respawn_base_tank"})
    assert not _is_player_obj({"type": "capture_zone", "icon": "capture_zone"})
    assert not _is_player_obj({"type": "aircraft", "icon": "Assault"})
    results.append("玩家对象识别(icon==Player) OK")


def test_store(tmpdir: str):
    path = os.path.join(tmpdir, "routes.json")
    store = RouteStore(path)
    assert store.routes == []
    assert store.display_name("abc123") == "地图 1"

    rec = store.new_route("abc123", store.display_name("abc123"), "Leopard 2Pl", 0.5, 0.5)
    assert len(rec.marks) == 1 and rec.marks[0].kind == "start"
    rec.points.append(RoutePoint(x=0.51, y=0.5, t=1.0, speed=45.0))
    rec.duration = 97.0
    store.add(rec)
    assert len(store.routes) == 1

    grouped = store.grouped()
    assert list(grouped) == ["abc123"]
    assert len(grouped["abc123"]) == 1

    store.rename_map("abc123", "波兰·西风农场")
    assert store.display_name("abc123") == "波兰·西风农场"

    again = RouteStore(path)          # 持久化往返
    assert len(again.routes) == 1
    assert again.routes[0].map_name == "波兰·西风农场"
    assert again.routes[0].duration == 97.0
    assert again.routes[0].points[0].speed == 45.0

    assert store.remove(rec.route_id) is True
    assert RouteStore(path).routes == []
    results.append("RouteStore 新增/分组/重命名/持久化/删除 OK")


def test_recorder(tmpdir: str):
    store = RouteStore(os.path.join(tmpdir, "rec_routes.json"))
    rec_w = RouteRecorder(store)      # 不启动线程, 手动 _tick
    fake = FakeClient()
    rec_w.client = fake

    events: list[tuple] = []
    rec_w.recording_started.connect(lambda r: events.append(("start", r)))
    rec_w.mark_added.connect(lambda m: events.append(("mark", m)))
    rec_w.recording_finished.connect(lambda r: events.append(("stop", r)))
    rec_w.notice.connect(lambda t: events.append(("notice", t)))

    # 未录制时标记 → 提示
    rec_w.request_mark()
    rec_w._tick()
    assert events and events[-1][0] == "notice"
    assert not rec_w.recording

    # 开始录制
    rec_w.request_start()
    rec_w._tick()
    assert rec_w.recording
    rec = events[-1][1]
    assert (rec.start_x, rec.start_y) == (0.5, 0.5)
    assert len(rec.marks) == 1 and rec.marks[0].kind == "start"
    assert rec.vehicle == "Leopard 2PL"

    # 移动 → 采样轨迹点(带速度)
    for i in range(1, 6):
        fake.x = 0.5 + i * 0.01
        fake.speed = 40.0 + i
        rec_w._sample((fake.x, fake.y), i * 0.2, fake.speed)
    assert len(rec.points) == 5, len(rec.points)
    assert rec.points[-1].speed == 45.0

    # 标记当前位置
    fake.x, fake.y = 0.55, 0.52
    rec_w.request_mark()
    rec_w._tick()
    assert events[-1][0] == "mark"
    assert len(rec.marks) == 2 and rec.marks[-1].kind == "mark"
    assert rec.marks[-1].x == 0.55

    # 结束录制 → 落盘
    rec_w.request_stop()
    rec_w._tick()
    assert not rec_w.recording
    assert events[-1][0] == "stop"
    saved = events[-1][1]
    assert saved.duration >= 0.0
    assert saved.marks[-1].kind == "stop"
    assert len(store.routes) == 1
    assert RouteStore(store.path).routes[0].route_id == saved.route_id
    results.append("RouteRecorder 开始/采样/标记/结束 + 落盘 OK")


def test_recorder_placeholder():
    """战局外(8111 只返回 "?" 占位图)应拒绝开始录制, 而不是记成一条没有地图的轨迹。"""
    from wt81111g.route_store import PLACEHOLDER_MAP_KEY

    store = RouteStore(os.path.join(tempfile.gettempdir(), "rt_ph.json"))
    rec_w = RouteRecorder(store)
    rec_w.client = FakeClient()          # 即使是“有玩家位置”的极端情况也要拦
    notices: list[str] = []
    rec_w.notice.connect(notices.append)

    rec_w._map_key = PLACEHOLDER_MAP_KEY
    rec_w.request_start()
    rec_w._tick()
    assert not rec_w.recording, "占位图状态下不应开始录制"
    assert notices and "占位图" in notices[-1], notices
    assert not store.routes

    rec_w._map_key = "abc123def456"      # 真实地图 → 可以录制
    rec_w.request_start()
    rec_w._tick()
    assert rec_w.recording
    results.append("战局外占位图拒绝录制 OK")


def test_refresh_rate():
    """采样间隔: 默认 40ms(≈25Hz, 与数据源匹配)、采样同步跟随、有范围保护。"""
    store = RouteStore(os.path.join(tempfile.gettempdir(), "rt_hz.json"))
    rec_w = RouteRecorder(store)
    assert abs(rec_w.interval - 0.040) < 1e-6, rec_w.interval
    assert abs(rec_w.target_interval_ms() - 40.0) < 1e-6
    assert abs(rec_w.target_hz() - 25.0) < 0.5, rec_w.target_hz()
    assert rec_w.sample_interval == rec_w.interval, "采样应跟随轮询间隔"
    # 25Hz 下单帧位移很小, 位移阈值必须远小于它, 否则轨迹会被拦成稀疏折线
    tank_per_frame = 0.00815 / 25.0      # 60km/h 坦克在 2048 单位地图上的单帧位移
    assert rec_w.min_move < tank_per_frame, (rec_w.min_move, tank_per_frame)

    rec_w.set_interval_ms(20)
    assert abs(rec_w.interval - 0.020) < 1e-6, rec_w.interval
    assert rec_w.sample_interval == rec_w.interval

    rec_w.set_interval_ms(1)             # 下限保护(最快 10ms)
    assert rec_w.interval >= 0.01
    rec_w.set_interval_ms(9999)          # 上限保护(最慢 200ms)
    assert rec_w.interval <= 0.2
    results.append("采样间隔(默认40ms/采样同步/范围保护) OK")


def test_track_segments():
    """轨迹按速度分桶时, 每段必须是“相邻点对”, 不得跨点相连。

    回归: 早期版本把同桶的多段接成一个 drawPolyline, 导致不相邻的段被
    首尾相连, 地图上出现“分叉”的斜线。
    """
    speeds = [10, 60, 10, 60, 10]        # 速度交替 → 同桶线段不相邻
    pts = [RoutePoint(x=0.1 * (i + 1), y=0.2 * (i + 1), t=i * 0.5, speed=sp)
           for i, sp in enumerate(speeds)]
    segs = build_speed_segments(pts, vmax=60.0, to_px=lambda x, y: QPointF(x, y))

    valid = {(pts[i - 1].x, pts[i - 1].y, pts[i].x, pts[i].y)
             for i in range(1, len(pts))}
    count = 0
    for poly in segs.values():
        assert len(poly) % 2 == 0, "必须成对存放(每 2 点 = 1 条线段)"
        for i in range(0, len(poly), 2):
            a, b = poly[i], poly[i + 1]
            key = (a.x(), a.y(), b.x(), b.y())
            assert key in valid, f"出现跨点连线(分叉): {key}"
            count += 1
    assert count == len(pts) - 1, count
    results.append("轨迹分段(每段独立/无跨点分叉) OK")


def test_point_at_and_split():
    """回放基础: 按时间插值取位置 / 按 playhead 切分轨迹(边界处插值保证连续)。"""
    pts = [RoutePoint(x=0.1 * i, y=0.05 * i, t=float(i), speed=10.0 * i) for i in range(5)]
    assert point_at([], 0.5) is None
    assert point_at(pts, -1.0).t == 0.0          # 超出范围 → 首点
    assert point_at(pts, 99.0).t == 4.0          # 超出范围 → 末点

    mid = point_at(pts, 1.5)                     # 区间中点线性插值
    assert abs(mid.x - 0.15) < 1e-9 and abs(mid.y - 0.075) < 1e-9
    assert abs(mid.speed - 15.0) < 1e-9

    head, tail = split_points(pts, 2.5)
    assert abs(head[-1].t - 2.5) < 1e-9 and abs(tail[0].t - 2.5) < 1e-9
    assert abs(head[-1].x - 0.25) < 1e-9
    assert len(head) + len(tail) == len(pts) + 2   # 两端各插入一个插值点

    h0, t0 = split_points(pts, -1.0)
    assert len(h0) == 1 and len(t0) == len(pts) + 1
    assert split_points([], 1.0) == ([], [])
    results.append("回放插值/切分 OK")


def test_playback():
    """回放控制器: 倍速推进 / 到下一标记暂停 / 播放到末尾。"""
    assert PLAYBACK_SPEEDS[0] == 0.5 and PLAYBACK_SPEEDS[-1] == 16.0
    assert len(PLAYBACK_SPEEDS) == 32, len(PLAYBACK_SPEEDS)

    store = RouteStore(os.path.join(tempfile.gettempdir(), "rt_play.json"))
    rec = store.new_route("k", "地图 1", "Leopard 2PL", 0.1, 0.1)
    rec.points = [RoutePoint(x=0.1 + 0.01 * i, y=0.1, t=float(i), speed=30.0)
                  for i in range(11)]
    rec.duration = 10.0
    rec.marks.append(RouteMark(0.13, 0.1, 3.0, "mark"))
    rec.marks.append(RouteMark(0.20, 0.1, 10.0, "stop"))

    player = RoutePlayer()
    player.load(rec)
    assert player.time == 0.0 and not player.playing

    player.set_speed(1.0)                        # 1X: 1 秒推进 1 秒
    player.start()
    assert player.playing
    player._on_tick(1.0)
    assert abs(player.time - 1.0) < 1e-6, player.time
    player.set_speed(3.0)                        # 3X: 1 秒推进 3 秒
    player._on_tick(1.0)
    assert abs(player.time - 4.0) < 1e-6, player.time

    player.pause()                               # 到下一标记暂停
    player.seek(0.0)
    hits = []
    player.reached_mark.connect(hits.append)
    player.set_speed(3.0)                        # 3X: 一次 tick(1s) 即跨过 t=3.0 的标记
    player.play_to_next_mark()
    assert player.playing and player.pause_at_mark
    player._on_tick(1.0)
    assert not player.playing, "到标记后应自动暂停"
    assert hits and abs(hits[-1].t - 3.0) < 1e-6
    assert abs(player.time - 3.0) < 1e-6, player.time   # 精确停在标记时刻而非冲过头

    done = []                                    # 播放到末尾
    player.finished.connect(lambda: done.append(True))
    player.set_speed(16.0)
    player.start()
    for _ in range(20):
        player._on_tick(1.0)
    assert done and not player.playing
    assert abs(player.time - rec.duration) < 1e-6

    player.set_speed(999)                        # 倍速范围保护
    assert player.speed <= 64.0
    player.set_speed(0)
    assert player.speed >= 0.1
    results.append("回放控制器(倍速/到标记暂停/结束) OK")


def test_map_render():
    """真正走一遍绘制路径(地图 + 轨迹 + 标记 + 时间标签)。

    这类缺陷只在真实绘制时才会暴露(例如 QRect 只接受 int, 传 float 会抛
    TypeError), 只做实例化/控件测试是抳不到的 —— 所以必须 render。
    """
    # 内存生成一张 PNG 作为地图图片(不依赖游戏运行)
    src = QImage(64, 64, QImage.Format.Format_RGB32)
    src.fill(QColor("#334455"))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    src.save(buf, "PNG")
    buf.close()

    view = RouteMapView()
    view.resize(800, 600)
    assert view.set_map_bytes(bytes(ba)) is True, "地图图片应加载成功"

    store = RouteStore(os.path.join(tempfile.gettempdir(), "rt_render.json"))
    rec = store.new_route("k", "地图 1", "Leopard 2PL", 0.4, 0.4)
    rec.points = [RoutePoint(0.4 + i * 0.01, 0.4 + i * 0.005, i * 0.5, 40.0 + i)
                  for i in range(5)]
    rec.marks.append(RouteMark(0.45, 0.42, 2.0, "mark"))
    rec.marks.append(RouteMark(0.49, 0.44, 5.0, "stop"))

    view.set_player((0.42, 0.41))
    canvas = QImage(800, 600, QImage.Format.Format_ARGB32)

    view.set_live(rec)            # 录制中: 起点圈 + 轨迹 + 标记/终点时间标签
    view.render(canvas)
    view.set_live(None)
    view.set_visible_tracks([rec])   # 眼睛点亮的轨迹: 不透明灰色折线
    view.render(canvas)
    view._hover_text = "时间 00:02.500   速度 42 km/h"   # 悬停提示(毫秒精度)
    view.render(canvas)

    view.set_playback(rec, rec.duration * 0.5)  # 回放: 已播放着色 + 灰色未播放 + 光标
    view.render(canvas)
    view.set_playback(None)

    # 无地图图片时也不应抛异常
    empty = RouteMapView()
    empty.resize(400, 300)
    empty.render(QImage(400, 300, QImage.Format.Format_ARGB32))
    results.append("地图渲染(轨迹/标记/时间标签/悬停) OK")


def test_player_heading():
    """车头朝向要跟着 dx/dy 转(渲染级: 不真画一遍看不出来)。

    玩家标记上是**蓝色 ^**, 回放光标上是**一根白线**; 做法: 纯色底图 +
    沿这两个符号逐点取样数亮像素 —— 画在哪一侧、转到哪个角度, 就只该在
    那一侧取到亮点(不会把实心点/空心圈的像素算进来)。
    """
    view = RouteMapView()
    view.resize(800, 600)
    assert view.set_map_bytes(png_solid("#334455", 64)) is True, "地图图片应加载成功"
    pos = (0.42, 0.41)
    # 几何自检: 符号够大取样才有意义
    assert PLAYER_DIR_H > 2.0 and PLAYER_DIR_W > 2.0, "^ 太小了, 看不出朝向"
    assert CURSOR_DIR_LEN > 2.0, "回放光标的朝向线太短"

    def render_now() -> QImage:
        img = QImage(800, 600, QImage.Format.Format_ARGB32)
        view.render(img)
        return img

    def count_bright(img: QImage, a: QPointF, b: QPointF,
                     t0: float = 0.0, span: float = 1.0, n_samples: int = 16) -> int:
        """沿线段 a→b 取样数亮像素(底色 #334455 和 204, ^ 与白线都是亮的)。

        t0/span 控制取哪一段 —— 两端常常挨着实心点/空心圈(它们也亮), 要避开。
        """
        n = 0
        for i in range(n_samples):
            t = t0 + span * (i + 0.5) / n_samples
            x = int(round(a.x() + (b.x() - a.x()) * t))
            y = int(round(a.y() + (b.y() - a.y()) * t))
            if 0 <= x < 800 and 0 <= y < 600:
                col = img.pixelColor(x, y)
                if col.red() + col.green() + col.blue() > 380:
                    n += 1
        return n

    def bright(angle_deg: float) -> int:
        """玩家标记的蓝色 ^: 两条边上一共取到几个亮点。"""
        c = view.map_pos(*pos)
        img = render_now()
        tip, foot1, foot2 = player_dir_points(c, math.radians(angle_deg))
        return count_bright(img, tip, foot1) + count_bright(img, tip, foot2)

    view.set_player(pos)                       # 没有朝向数据 → 只画圈和点
    assert bright(0.0) == 0 and bright(90.0) == 0 and bright(180.0) == 0, \
        "拿不到 dx/dy 时不该画出朝向符号"

    # 像素坐标 y 向下 → dx=1,dy=0 在右边(0°), dx=0,dy=1 在下边(90°)
    view.set_player(pos, (1.0, 0.0))
    right, left = bright(0.0), bright(180.0)
    assert right > 20 and left == 0, f"朝右时应只在右侧画 ^ (右 {right} / 左 {left})"

    view.set_player(pos, (0.0, 1.0))
    down, up = bright(90.0), bright(-90.0)
    assert down > 20 and up == 0, f"朝下时应只在下侧画 ^ (下 {down} / 上 {up})"

    view.set_player(pos, (-1.0, 0.0))
    assert bright(180.0) > 20 and bright(0.0) == 0, "反向时应画在左侧"

    # 斜向也要落在正确的象限里(dx=-0.67, dy=0.74 是实测样本 → 左下 132°)
    view.set_player(pos, (-0.67, 0.74))
    assert bright(132.0) > 20, "斜向朝向应落在左下方向"
    assert bright(-48.0) == 0, "反方向不该有 ^"

    view.set_player(pos, (0.0, 0.0))           # 全零向量 = 无有效朝向
    assert bright(0.0) == 0 and bright(90.0) == 0, "零向量不该画出方向"

    # --- 回放光标: 朝向是**圆点内部的一条黑色短线**(2026-10-22 用户要求) ---
    def cursor_dark(rec, head: float, angle_deg: float) -> int:
        """沿那根黑色短线取样数**暗**像素。

        判据用“既不是白点(≈750)也不是底图(≈204)”—— 短线只有 1~2px 宽, 加上亚像素
        位置, 实际像素大多是抗锯齿半透明黑(实测 247~351); 只数纯黑会全是 0。
        """
        cur = point_at(rec.points, head)
        c = view.map_pos(cur.x, cur.y)
        img = render_now()
        start, tip = cursor_dir_points(c, math.radians(angle_deg))
        n = 0
        for i in range(16):
            t = 0.2 + 0.6 * (i + 0.5) / 16
            x = int(round(start.x() + (tip.x() - start.x()) * t))
            y = int(round(start.y() + (tip.y() - start.y()) * t))
            col = img.pixelColor(x, y)
            if 100 < col.red() + col.green() + col.blue() < 600:
                n += 1
        return n

    def playback_rec(dx: float, dy: float) -> RouteRecord:
        r = RouteRecord(route_id="head", map_key="k", map_name="m", vehicle="v",
                        created_at="", duration=4.0, start_x=0.4, start_y=0.41)
        r.points = [RoutePoint(0.40, 0.41, 0.0, 30.0, dx=dx, dy=dy),
                    RoutePoint(0.60, 0.41, 4.0, 30.0, dx=dx, dy=dy)]
        return r

    view.set_player(None)
    rec_dir = playback_rec(0.0, -1.0)          # 录制时朝上
    view.set_playback(rec_dir, 2.0)
    assert cursor_dark(rec_dir, 2.0, -90.0) > 8, \
        "回放光标圆点内部应有一条指向车头的黑线"
    assert cursor_dark(rec_dir, 2.0, 90.0) == 0, "反方向不该有黑线(那里是白点)"
    view.set_playback(None)

    rec_old = playback_rec(0.0, 0.0)           # 旧轨迹: 点里没有朝向
    view.set_playback(rec_old, 2.0)
    assert cursor_dark(rec_old, 2.0, -90.0) == 0, "旧轨迹没有朝向 → 只画白点"

    # 光标**只留实心圆**(2026-10-21 用户要求): 外圈半径 9 那一圈不该有白像素
    img = render_now()
    cur = point_at(rec_old.points, 2.0)
    c = view.map_pos(cur.x, cur.y)
    ring = 0
    for deg in range(0, 360, 10):
        x = int(c.x() + 9 * math.cos(math.radians(deg)))
        y = int(c.y() + 9 * math.sin(math.radians(deg)))
        col = img.pixelColor(x, y)
        if col.red() > 150 and col.green() > 150 and col.blue() > 150:
            ring += 1
    assert ring == 0, f"回放光标不该再有外环(半径 9 上有 {ring} 个白像素)"
    core = img.pixelColor(int(c.x()), int(c.y()))
    assert core.red() > 200 and core.green() > 200 and core.blue() > 200, (
        f"光标中心应是一个实心白点, 实际 {core.name()}")
    view.set_playback(None)

    view.set_player(None)
    view.render(QImage(800, 600, QImage.Format.Format_ARGB32))   # 无玩家点也不能抛异常
    results.append("玩家车头朝向 ^(跟随 dx/dy 旋转) OK")


def test_map_hover():
    """地图悬停: **所有显示中的线路**都能读, 左下角浮层里要有车型/用时。

    修复前只查“实时轨迹 / 第一条可见轨迹”, 于是只剩刚录完那条有提示,
    其它眼睛点亮的线路鼠标移上去没反应。
    """
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QMouseEvent

    from wt81111g.route_map_view import RouteMapView

    src = QImage(64, 64, QImage.Format.Format_RGB32)
    src.fill(QColor("#334455"))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    src.save(buf, "PNG")
    buf.close()

    view = RouteMapView()
    view.resize(800, 600)
    assert view.set_map_bytes(bytes(ba))
    store = RouteStore(os.path.join(tempfile.gettempdir(), "rt_hover.json"))

    a = store.new_route("k", "地图 1", "T-72B", 0.30, 0.30)
    a.points = [RoutePoint(0.30, 0.30 + i * 0.02, i * 0.5, 10.0 + i * 3)
                for i in range(4)]
    a.duration = 1.5
    b = store.new_route("k", "地图 1", "Leopard 2PL", 0.30, 0.70)
    b.points = [RoutePoint(0.30, 0.70 + i * 0.02, i * 0.5, 50.0 + i * 3)
                for i in range(4)]
    b.duration = 1.5
    view.set_visible_tracks([a, b])

    def move_to(pt) -> None:
        c = view.map_pos(pt.x, pt.y)          # _to_px 是地图本地坐标, 鼠标事件要控件坐标
        view.mouseMoveEvent(QMouseEvent(
            QEvent.Type.MouseMove, c, c, Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))

    # ① 悬停在**后面的那条**(B): 修复前它完全没反应
    move_to(b.points[2])
    assert view._hover_text, "眼睛点亮的线路也应该能悬停读取"
    assert view._hover_rec is b
    assert "车型 Leopard 2PL" in view._hover_text and "T-72B" not in view._hover_text
    assert fmt_time_ms(b.points[2].t) in view._hover_text
    assert f"速度 {fmt_speed(b.points[2].speed)} km/h" in view._hover_text

    # ② 悬停到另一条(A) → 提示跟着换(以前永远只有一条能被读到)
    move_to(a.points[1])
    assert view._hover_rec is a
    assert "T-72B" in view._hover_text and "Leopard 2PL" not in view._hover_text

    # ③ 移到远离所有轨迹的地方(左上角) → 提示消失
    far = QPointF(4.0, 4.0)
    view.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, far, far, Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
    assert view._hover_text is None
    view.render(QImage(800, 600, QImage.Format.Format_ARGB32))   # 含白环也不应抛异常

    # ④ 被悬停的那条被隐藏 → 提示立刻消失(不留别人的数据)
    move_to(a.points[1])
    assert view._hover_text
    view.set_visible_tracks([b])
    assert view._hover_text is None, "隐藏后的线路不该还留着悬停提示"

    # ⑤ 回放模式: 悬停命中正在回放的那条
    view.set_playback(a, a.duration * 0.5)
    move_to(a.points[2])
    assert view._hover_rec is a and "T-72B" in view._hover_text
    view.set_playback(None)

    # ⑥ 平移后仍然命中正确的位置(命中测试要把鼠标坐标换算到地图本地坐标)
    view._pan_x, view._pan_y = -60.0, 35.0
    move_to(b.points[1])
    assert view._hover_rec is b, "平移后悬停仍应命中同一条线"
    assert fmt_time_ms(b.points[1].t) in view._hover_text
    view.reset_view()

    view.leaveEvent(None)
    assert view._hover_text is None
    results.append("地图悬停(任意显示中的线路 + 车型/用时) OK")


def test_map_draw_cache():
    """地图绘制缓存: 状态没变时重复重绘**不该**再重算几何 / 重建缓存层。

    背景: 5 条线 3766 个点, 状态没变时每帧都要在 Python 里重建分桶几何(30ms)
    再逐段 drawLines(10ms) —— 25Hz 刷新一次 41ms, 界面直接卡死
    (2026-10-08 用户反馈“选 5 条卡得不得了”)。修复 = 几何缓存 + 离屏缓存层。
    """
    import wt81111g.route_map_view as mv

    src = QImage(64, 64, QImage.Format.Format_RGB32)
    src.fill(QColor("#334455"))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    src.save(buf, "PNG")
    buf.close()

    view = RouteMapView()
    view.resize(800, 600)
    assert view.set_map_bytes(bytes(ba))
    store = RouteStore(os.path.join(tempfile.gettempdir(), "rt_layer.json"))
    recs = []
    for i in range(5):
        rec = store.new_route("k", "地图 1", f"载具{i}", 0.25, 0.15 + i * 0.1)
        rec.points = [RoutePoint(0.25 + j * 0.001, 0.15 + i * 0.1 + j * 0.0005,
                                 j * 0.1, 10.0 + j)
                      for j in range(300)]
        recs.append(rec)
    view.set_visible_tracks(recs)
    view.set_highlight(recs)
    view.set_player((0.3, 0.3))
    canvas = QImage(800, 600, QImage.Format.Format_ARGB32)

    seg_calls: list[int] = []
    lay_calls: list[int] = []
    real_seg, real_layer = mv.build_speed_segments, RouteMapView._make_layer
    mv.build_speed_segments = lambda *a, **k: (seg_calls.append(1), real_seg(*a, **k))[1]
    RouteMapView._make_layer = lambda self, *a, **k: (
        lay_calls.append(1), real_layer(self, *a, **k))[1]
    try:
        view.render(canvas)                     # 首帧: 建轨迹缓存层(底图每帧直接贴, 不缓存)
        n_seg, n_lay = len(seg_calls), len(lay_calls)
        assert n_seg == 5, f"5 条线各算一次分桶几何: {n_seg}"
        assert n_lay == 1, f"只有轨迹层一张: {n_lay}"

        # 玩家点在动(录制/预览时每个 tick 都重绘): 不该重算几何 / 重建层
        for i in range(4):
            view.set_player((0.3 + i * 0.001, 0.3))
            view.render(canvas)
        assert len(seg_calls) == n_seg, "重复重绘不该重算分桶几何"
        assert len(lay_calls) == n_lay, "状态没变就不该重建缓存层"

        # 轨迹仍画在图上(缓存层不是空白)
        pt0 = recs[0].points[150]
        px = view.map_pos(pt0.x, pt0.y)
        img = QImage(800, 600, QImage.Format.Format_ARGB32)
        view.render(img)
        assert img.pixelColor(int(px.x()), int(px.y())) != QColor("#334455"), "轨迹应画出来"

        # 悬停浮层是动态内容 → 不该拖累缓存层
        view._hover_text = "时间 00:01.000 | 速度 20 km/h"
        view.render(canvas)
        assert len(lay_calls) == n_lay, "悬停不该重建轨迹层"
        view._hover_text = None

        # 拖动(平移)在余量内 → **一帧都不重建**(2026-10-08: 靠偏移贴图)
        view._pan_x, view._pan_y = 60.0, 40.0
        for i in range(6):
            view._pan_x += 12.0
            view.render(canvas)
        assert len(lay_calls) == n_lay, "拖动不该重建缓存层(余量内只贴图)"
        assert len(seg_calls) == n_seg, "拖动更不该重算分桶几何"

        # 拖出余量 → **不重建**, 改成“把旧内容搬一格 + 只补新露出来的窄边”
        # (2026-10-21: 64 条线整层重建实测 79 ms, 用户感觉是“拖一下卡一下”)
        view._pan_x += 300.0                 # 余量 = 0.35×800 = 280, 300 刚好越界
        view.render(canvas)
        assert len(lay_calls) == n_lay, "拖出余量应搬图层, 不该整层重建"
        r = view._map_rect()
        mx, _my = view._margin()
        assert abs(r.left() - view._track_anchor[0]) <= mx + 1e-6, \
            "搬完图层后锚点必须重新贴近当前地图矩形(否则内容会错位)"
        px_drag = view.map_pos(recs[0].points[150].x, recs[0].points[150].y)
        img_drag = QImage(800, 600, QImage.Format.Format_ARGB32)
        view.render(img_drag)
        assert img_drag.pixelColor(int(px_drag.x()), int(px_drag.y())) != \
            QColor("#334455"), "搬图层后轨迹仍应画在正确位置"

        # 搬不动(露出来的面积太大) → 退回整层重建(把阈值调成 0 模拟)
        real_ratio = mv.SHIFT_MAX_EXPOSED
        mv.SHIFT_MAX_EXPOSED = 0.0
        try:
            view._pan_x = -168.0             # 向左拖出余量, 且被 _clamp_pan 允许
            view.render(canvas)
        finally:
            mv.SHIFT_MAX_EXPOSED = real_ratio
        assert len(lay_calls) > n_lay, "搬图层不划算时应退回整层重建"
        view.reset_view()
        view.render(canvas)
        n_lay = len(lay_calls)

        # 缩放 → **不重建**(先按比例贴旧层, 见 _layer_tracks), 停手后才重建一次;
        # 几何是归一化的, 无论如何都不该重算分桶几何
        view._zoom = 2.5
        view.render(canvas)
        assert len(lay_calls) == n_lay, "缩放过程中不该重建缓存层(先按比例贴旧层)"
        assert len(seg_calls) == n_seg, "缩放不该重算分桶几何(归一化 + 缩放画笔)"
        view._geom_at = 0.0                  # 模拟“停手超过 GEOM_SETTLE_MS”
        view._geom_settle()
        view.render(canvas)
        assert len(lay_calls) > n_lay, "停手后应重建缓存层(变清晰)"
        n_lay = len(lay_calls)
        before = len(lay_calls)

        # 隐藏线路 → 也必须重建(几何仍缓存着)
        view.set_visible_tracks(recs[:3])
        view.set_highlight(recs[:3])
        view.render(canvas)
        assert len(lay_calls) > before, "隐藏线路后必须重建缓存层"

        # 全清掉 → 轨迹应从画面上消失(缓存层被正确替换)
        view.set_visible_tracks([])
        view.set_highlight([])
        img2 = QImage(800, 600, QImage.Format.Format_ARGB32)
        view.render(img2)
        assert img2.pixelColor(int(px.x()), int(px.y())) == QColor("#334455"), "清空后不该还有轨迹"

        t0 = time.perf_counter()
        for _ in range(10):
            view.render(canvas)
        per = (time.perf_counter() - t0) / 10 * 1000
        results.append(f"地图绘制缓存(5 条线重复重绘 {per:.1f} ms/帧) OK")
    finally:
        mv.build_speed_segments = real_seg
        RouteMapView._make_layer = real_layer


def test_playback_draw_cache():
    """回放累加层: 进度推进只**补新段**, 不重建整层(否则 5 条线回放 40ms/帧)。

    只有“往回拖拽 / 缩放平移 / 换回放对象”才允许重建。
    """
    from wt81111g.route_map_view import RouteMapView

    src = QImage(64, 64, QImage.Format.Format_RGB32)
    src.fill(QColor("#334455"))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    src.save(buf, "PNG")
    buf.close()

    view = RouteMapView()
    view.resize(800, 600)
    assert view.set_map_bytes(bytes(ba))
    store = RouteStore(os.path.join(tempfile.gettempdir(), "rt_play.json"))
    rec = store.new_route("k", "地图 1", "Leopard 2PL", 0.2, 0.2)
    rec.points = [RoutePoint(0.2 + i * 0.002, 0.2 + i * 0.002, i * 0.1, 20.0 + i)
                  for i in range(200)]
    rec.duration = 20.0
    canvas = QImage(800, 600, QImage.Format.Format_ARGB32)

    lay_calls: list[int] = []
    real_layer = RouteMapView._make_layer
    RouteMapView._make_layer = lambda self, *a, **k: (
        lay_calls.append(1), real_layer(self, *a, **k))[1]
    try:
        view.set_playback([rec], 0.0)
        view.render(canvas)
        n = len(lay_calls)
        assert n == 1, f"回放累加层一张(底图每帧直接贴): {n}"

        # ① 进度一格格推进(每帧 set_playback + 重绘) → 不该重建
        for i in range(1, 11):
            view.set_playback([rec], rec.duration * i / 10)
            view.render(canvas)
        assert len(lay_calls) == n, "回放推进不该重建累加层(只补新段)"

        # ② 播放到的部分要真的画上去(playhead=0 时该位置是空的/底图色)
        pt = rec.points[-1]
        px = view.map_pos(pt.x, pt.y)
        img_end = QImage(800, 600, QImage.Format.Format_ARGB32)
        view.render(img_end)
        view.set_playback([rec], 0.0)
        img_start = QImage(800, 600, QImage.Format.Format_ARGB32)
        view.render(img_start)
        assert img_end.pixelColor(int(px.x()), int(px.y())) != \
            img_start.pixelColor(int(px.x()), int(px.y())), \
            "已播放段应该可见, 而未播放时不该画出整条灰线"

        # ③ 往回拖拽 → 必须重建(否则已播放的段留在图上擦不掉)
        view.set_playback([rec], rec.duration * 0.2)
        view.render(canvas)
        assert len(lay_calls) > n, "往回拖拽必须重建累加层"
        n2 = len(lay_calls)

        # ③b 拖动出余量 → **不重建**(搬图层), 已播放的画面仍跟着地图走
        view.set_playback([rec], rec.duration * 0.6)
        view.render(canvas)
        n_shift = len(lay_calls)
        view._pan_x += 300.0                 # 余量 = 0.35×800 = 280
        img_drag = QImage(800, 600, QImage.Format.Format_ARGB32)
        view.render(img_drag)
        assert len(lay_calls) == n_shift, "拖动出余量应搬累加层, 不该整层重建"
        # 取**已播放**范围内的点(60% 进度 ≈ 前 120 点; 未播放段现在不画)
        px_drag = view.map_pos(rec.points[50].x, rec.points[50].y)
        assert img_drag.pixelColor(int(px_drag.x()), int(px_drag.y())) != \
            QColor("#334455"), "搬图层后回放画面仍应画在正确位置"
        view._pan_x = 0.0
        view.render(canvas)
        n2 = len(lay_calls)

        # ④ 缩放 → 播放中也不重建(先按比例贴旧层), 停手后才重建一次
        view._zoom = 3.0
        view.render(canvas)
        assert len(lay_calls) == n2, "缩放过程中不该重建累加层(先按比例贴旧层)"
        view._geom_at = 0.0
        view._geom_settle()
        view.render(canvas)
        assert len(lay_calls) > n2, "停手后应重建累加层(变清晰)"
        n3 = len(lay_calls)
        assert n3 > 0

        # ⑤ 退出回放(切回预览层)再重新进入 → 累加层必须重建
        #    (否则会拿着退出前那张残留画面继续画)
        view.set_playback([], 0.0)
        view.render(canvas)
        n_exit = len(lay_calls)
        view.set_playback([rec], 0.0)
        view.render(canvas)
        assert len(lay_calls) > n_exit, "重新进入回放应重建累加层"

        t0 = time.perf_counter()
        for i in range(10):
            view.set_playback([rec], rec.duration * (i + 1) / 10)
            view.render(canvas)
        per = (time.perf_counter() - t0) / 10 * 1000
        results.append(f"回放累加层(推进不重建, {per:.1f} ms/帧) OK")
    finally:
        RouteMapView._make_layer = real_layer


def test_map_zoom_pan():
    """地图交互: 滚轮缩放(鼠标锚点不动) / 中键拖拽平移 / 双击复位。"""
    from PyQt6.QtCore import QEvent, QPoint, Qt
    from PyQt6.QtGui import QMouseEvent, QWheelEvent

    src = QImage(64, 64, QImage.Format.Format_RGB32)
    src.fill(QColor("#334455"))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    src.save(buf, "PNG")
    buf.close()

    view = RouteMapView()
    view.resize(800, 600)
    assert view.set_map_bytes(bytes(ba))
    view.reset_view()

    def norm(x: float, y: float) -> tuple[float, float]:
        r = view._map_rect()
        return ((x - r.left()) / r.width(), (y - r.top()) / r.height())

    # 基准: zoom=1 即适配窗口且居中
    r0 = view._map_rect()
    assert abs(r0.center().x() - 400) < 1 and abs(r0.center().y() - 300) < 1

    # 滚轮放大: 鼠标下的地图点必须保持不动
    anchor = QPointF(500.0, 200.0)
    n0 = norm(anchor.x(), anchor.y())
    wheel = QWheelEvent(anchor, anchor, QPoint(0, 0), QPoint(0, 120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.NoScrollPhase, False)
    view.wheelEvent(wheel)
    assert view._zoom > 1.0, view._zoom
    n1 = norm(anchor.x(), anchor.y())
    assert abs(n0[0] - n1[0]) < 0.01 and abs(n0[1] - n1[1]) < 0.01, (n0, n1)

    # 缩放上限保护
    for _ in range(60):
        view.wheelEvent(wheel)
    assert view._zoom <= 20.0 + 1e-6, view._zoom

    # 中键拖拽平移
    view.reset_view()
    press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(100, 100), QPointF(100, 100),
                        Qt.MouseButton.MiddleButton, Qt.MouseButton.MiddleButton,
                        Qt.KeyboardModifier.NoModifier)
    view.mousePressEvent(press)
    assert view._dragging
    move = QMouseEvent(QEvent.Type.MouseMove, QPointF(140, 130), QPointF(140, 130),
                       Qt.MouseButton.NoButton, Qt.MouseButton.MiddleButton,
                       Qt.KeyboardModifier.NoModifier)
    view.mouseMoveEvent(move)
    assert abs(view._pan_x - 40) < 0.01 and abs(view._pan_y - 30) < 0.01, (view._pan_x, view._pan_y)
    release = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(140, 130), QPointF(140, 130),
                          Qt.MouseButton.MiddleButton, Qt.MouseButton.NoButton,
                          Qt.KeyboardModifier.NoModifier)
    view.mouseReleaseEvent(release)
    assert not view._dragging

    # 缩放/平移后渲染仍不可抛异常
    view.render(QImage(800, 600, QImage.Format.Format_ARGB32))

    # 双击复位
    dbl = QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(200, 200), QPointF(200, 200),
                      Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                      Qt.KeyboardModifier.NoModifier)
    view.mouseDoubleClickEvent(dbl)
    assert view._zoom == 1.0 and view._pan_x == 0.0 and view._pan_y == 0.0
    results.append("地图缩放/中键平移/双击复位 OK")


def test_spawns(tmpdir: str):
    """重生位置(respawn_base_*): 解析 / 归属色 / 跟布局一起落盘 / 地图上画空心方块。

    8111 里一局有几十个 `respawn_base_tank` —— 就是“重生区域”里那堆候选位置
    (玩家重生时从中随机分配一个)。
    """
    objs = [
        {"type": "capture_zone", "icon": "capture_zone", "x": 0.5, "y": 0.5,
         "zone_label": "A", "color": "#ffFFFA"},
        # 我方(蓝) 3 个地面重生点
        {"type": "respawn_base_tank", "icon": "respawn_base_tank", "x": 0.10,
         "y": 0.10, "color": "#174DFF"},
        {"type": "respawn_base_tank", "icon": "respawn_base_tank", "x": 0.11,
         "y": 0.10, "color": "#174DFF"},
        {"type": "respawn_base_tank", "icon": "respawn_base_tank", "x": 0.12,
         "y": 0.11, "color": "#174DFF"},
        # 敌方(红) 2 个
        {"type": "respawn_base_tank", "icon": "respawn_base_tank", "x": 0.90,
         "y": 0.90, "color": "#fa0C00"},
        {"type": "respawn_base_tank", "icon": "respawn_base_tank", "x": 0.91,
         "y": 0.90, "color": "#fa0C00"},
        # 空军重生点(也解析/记录, 但地图上不画)
        {"type": "respawn_base_fighter", "icon": "respawn_base_fighter", "x": 0.50,
         "y": 0.05, "color": "#174DFF"},
        # 干扰项: 机场 / 玩家 / 战区, 都不算重生位置
        {"type": "airfield", "icon": "none", "x": 0.5, "y": 0.02, "color": "#174DFF"},
        {"type": "ground_model", "icon": "Player", "x": 0.5, "y": 0.5},
    ]
    spawns = spawns_from_objects(objs)
    assert len(spawns) == 6, [(s.kind, s.side) for s in spawns]
    assert sum(1 for s in spawns if s.kind == "tank") == 5
    assert {s.side for s in spawns if s.kind == "tank"} == {"ours", "enemy"}
    assert sum(1 for s in spawns if s.side == "ours") == 4      # 含那架战斗机
    ground = ground_spawns(spawns)
    assert len(ground) == 5 and all(s.ground for s in ground)
    assert spawn_summary(spawns) == "我方 4 · 敌方 2 (地面载具/战斗机)", spawn_summary(spawns)
    assert spawns_from_objects([]) == []
    # 归属色: 老版本/空战里的 #185AFF / #fa3200 也认得; 玩家自己的黄色不算敌方
    assert side_of("#174DFF") == "ours" and side_of("#185AFF") == "ours"
    assert side_of("#fa0C00") == "enemy" and side_of("#fa3200") == "enemy"
    assert side_of("#faC81E") == "neutral", "玩家黄色不应被当成敌方"
    assert side_of("#ffFFFA") == "neutral" and side_of(None) == "neutral"

    # 落盘: 重生位置跟着**布局**存(按战区指纹去重)
    store = RouteStore(os.path.join(tmpdir, "spawns.json"))
    zones = zones_from_objects(objs)
    sig = signature(zones)
    assert store.remember_zones("m1", zones, spawns=spawns) is True
    assert store.remember_zones("m1", zones, spawns=spawns) is False
    layout = store.layout_for("m1", sig)
    assert len(layout["spawns"]) == 6
    again = RouteStore(os.path.join(tmpdir, "spawns.json"))
    assert len(again.layout_for("m1", sig)["spawns"]) == 6       # 持久化往返
    # 同一布局、重生位置变了(同一张图的另一个版本) → 更新进去, 不新增布局
    moved = [Spawn("tank", 0.2, 0.2, side="ours"), Spawn("tank", 0.8, 0.8, side="enemy")]
    assert store.remember_zones("m1", zones, spawns=moved) is False
    assert len(store.zone_layouts("m1")) == 1
    assert [s["side"] for s in store.layout_for("m1", sig)["spawns"]] == ["ours", "enemy"]

    # 录制线程: 从同一份 map_obj.json 里抓重生位置并写进布局
    rstore = RouteStore(os.path.join(tmpdir, "spawnrec.json"))
    recorder = RouteRecorder(rstore, base_url="http://127.0.0.1:1",
                             map_cache=MapCache(os.path.join(tmpdir, "maps_spawnrec")))
    fake = FakeClient()
    fake.map_objects = lambda: objs
    recorder.client = fake
    recorder._map_key = "k1"
    recorder._tick()
    assert len(recorder.spawns) == 6
    assert len(rstore.layout_for("k1", sig)["spawns"]) == 6
    fake.map_objects = lambda: []
    recorder._tick()
    assert recorder.spawns == [] and recorder.zones == []       # 退出战局清空

    # 地图视图: 红(敌)/蓝(我)空心方块真的画上去了, 关掉开关就不画
    view = RouteMapView()
    view.resize(420, 420)                  # 必须设尺寸: render 只画控件占的那块
    view.set_map_bytes(png_solid("#303030"))
    view.set_spawns(spawns_to_json(ground))
    assert len(view.spawns) == 5

    def _has_color(img, rgb, tol: int = 40) -> bool:
        tr, tg, tb = rgb
        for y in range(img.height()):
            for x in range(img.width()):
                p = img.pixel(x, y) & 0xFFFFFF
                if (abs(((p >> 16) & 255) - tr) < tol and abs(((p >> 8) & 255) - tg) < tol
                        and abs((p & 255) - tb) < tol):
                    return True
        return False

    img = QImage(420, 420, QImage.Format.Format_ARGB32)
    view.render(img)
    assert _has_color(img, (0x17, 0x4D, 0xFF)), "应画出我方的蓝色方块"
    assert _has_color(img, (0xfa, 0x0C, 0x00)), "应画出敌方的红色方块"
    view.set_show_spawns(False)
    off = QImage(420, 420, QImage.Format.Format_ARGB32)
    view.render(off)
    assert not _has_color(off, (0x17, 0x4D, 0xFF))
    assert not _has_color(off, (0xfa, 0x0C, 0x00))
    view.set_show_spawns(True)
    view.clear()
    assert view.spawns == []

    # 窗口: 实时那份 / 存下来的那份 都要能被画上; 菜单开关能关掉
    win = make_window(tmpdir, "spawnwin.json")
    win.recorder.stop()
    win.hotkeys.stop()
    win.show()
    map_bytes = png_solid("#303030")
    key = hashlib.sha1(map_bytes).hexdigest()[:12]      # 与 view.map_key 同一算法
    win.map_view.set_map_bytes(map_bytes)
    assert win.map_view.map_key == key
    win.store.remember_zones(key, zones, spawns=spawns)
    win.recorder._map_key = key                         # 假装 8111 正在给这张图
    win._apply_zones()                                  # ① 看缓存底图: 用存下来的那份
    assert len(win.map_view.spawns) == 5, win.map_view.spawns
    win._apply_zones({"zones": zones, "spawns": spawns})   # ② 战局内: 用实时那份
    assert len(win.map_view.spawns) == 5
    assert win.act_spawns.isChecked(), "默认应勾选「显示重生位置」"
    win.act_spawns.setChecked(False)
    assert not win.map_view.show_spawns
    win.act_spawns.setChecked(True)
    win.close()
    results.append("重生位置(解析/归属色/落盘/画红蓝空心方块) OK")


def test_multi_playback(tmpdir: str):
    """多条轨迹同时回放: 从 00:00 一起播放, **以最长的那条**结束;
    “到下一标记暂停”把多条轨迹的标记放在同一条时间轴上取**之后最近**的那个。
    """
    store = RouteStore(os.path.join(tmpdir, "multi_pb.json"))

    def make(dur: float, mark_t: float, x0: float):
        rec = store.new_route("AAA", "地图 A", "T", x0, 0.2)
        rec.points = [RoutePoint(x0 + i * 0.04, 0.2 + i * 0.01, i * dur / 6.0, 30.0)
                      for i in range(7)]
        rec.duration = dur
        rec.marks.append(RouteMark(x0 + 0.08, 0.22, mark_t, "mark"))
        rec.marks.append(RouteMark(x0 + 0.24, 0.26, dur, "stop"))
        return rec

    short = make(6.0, 2.0, 0.20)        # 短的一条
    long_ = make(10.0, 3.0, 0.50)       # 长的一条(回放的结束以它为准)

    player = RoutePlayer()
    hits: list[float] = []
    player.reached_mark.connect(lambda mk: hits.append(mk.t))
    finished: list[bool] = []
    player.finished.connect(lambda: finished.append(True))

    player.load([short, long_])
    assert player.records == [short, long_]
    assert player.record is short, "record 属性仍返回第一条(兼容旧调用)"
    assert player.duration == 10.0, "总时长必须取最长的一条"
    assert player.time == 0.0, "多条一起从 00:00 开始"

    # 统一时间轴上的“下一个标记”: 2.0(短) 比 3.0(长) 更近 → 取 2.0
    assert player.next_mark_time() == 2.0
    player.play_to_next_mark()
    assert player.pause_at_mark and player.playing
    player._on_tick(dt=5.0)              # 一帧跨过 5 秒
    assert player.time == 2.0 and not player.playing, player.time
    assert hits == [2.0], hits

    # 下一标记来自**另一条**轨迹(3.0 属于长的那条)
    player.play_to_next_mark()
    player._on_tick(dt=5.0)
    assert player.time == 3.0 and hits == [2.0, 3.0], (player.time, hits)

    # 再下一个回到短的那条(6.0)
    player.play_to_next_mark()
    player._on_tick(dt=5.0)
    assert player.time == 6.0 and hits == [2.0, 3.0, 6.0], (player.time, hits)

    # 播放不会因为短的那条播完而结束, 必须等最长的那条
    player.play_to_next_mark()          # 6.0 之后最近的是长的那条的 10.0
    player._on_tick(dt=2.0)             # 6.0 → 8.0: 短的已结束, 但仍在播放
    assert player.playing and player.time == 8.0 and not finished, (player.time, finished)
    player.cancel_pause_at_mark()
    player._on_tick(dt=5.0)             # 越过 10.0 → 播放结束
    assert not player.playing and player.time == 10.0, player.time
    assert finished == [True]

    # 单条兼容: 时长就是它自己
    player.load(short)
    assert player.records == [short] and player.record is short
    assert player.duration == 6.0
    player.load(None)
    assert player.records == [] and player.record is None

    # 多条同时回放的绘制(每条各画已播放/未播放 + 各一个光标)
    view = RouteMapView()
    view.resize(600, 400)
    view.set_map_bytes(png_solid("#303030"))
    view.set_playback([short, long_], 4.0)
    assert view.playback_records == [short, long_]
    view.render(QImage(600, 400, QImage.Format.Format_ARGB32))
    view.set_playback(None)
    assert view.playback_records == []
    results.append("多轨迹同时回放(统一时间轴/最长决定结束/跨轨迹下一标记) OK")


def test_single_instance():
    """单实例锁: 同名锁第二次必须判为“已在运行”。

    用**独立的名字**: 不能去碰真实的那把锁 —— 万一用户正开着程序, 本测试会
    把它判成“已在运行”并且真的去请求置前(甚至弹出“已在运行中”提示)。
    """
    from wt81111g.route_instance import acquire_mutex, release_mutex

    name = f"WTRouteTester_Test_{os.getpid()}"
    first, h1 = acquire_mutex(name)
    second, h2 = acquire_mutex(name)
    assert first is True, "第一次取锁应成功"
    assert second is False, "第二次取同名锁应判为已在运行"
    release_mutex(h1)
    release_mutex(h2)
    results.append("单实例锁(同名第二次判为已在运行) OK")


def test_tree_expand_state(tmpdir: str):
    """侧栏: **启动默认全部折叠**, 之后重建要保住用户手动改过的展开状态。

    回归: `_reload_tree` 以前对每个分组硬编码 setExpanded(True), 于是一旦重建
    (关掉「地图管理」、录完一条轨迹、改名、删除都会走这里), 用户刚折起来的
    层级就被全部展开。
    """
    name = "win_expand.json"
    # 先造好数据再开窗口 —— 模拟"启动时库里已有轨迹"(窗口首次建侧栏就该全折叠)
    seed = RouteStore(os.path.join(tmpdir, name))
    for i, veh in enumerate(("T-72B", "M1A1")):
        rec = seed.new_route("k1", "地图 1", veh, 0.2 + i * 0.1, 0.3)
        rec.duration = 30.0 + i
        seed.add(rec)
    rec = seed.new_route("k2", "地图 2", "BMP-2", 0.5, 0.5)
    rec.duration = 20.0
    seed.add(rec)

    win = make_window(tmpdir, name)
    win.recorder.stop()
    win.hotkeys.stop()
    win.show()

    # ① 启动默认: 全部折叠(顶层的地图行本身仍然看得见)
    assert win.tree.topLevelItemCount() == 2
    assert win.tree.topLevelItem(0).text(1) == "地图 1"
    assert not win.tree.topLevelItem(0).isExpanded(), "启动时应默认折叠"
    assert not win.tree.topLevelItem(1).isExpanded(), "启动时应默认折叠"

    # ② 手动展开第一张地图 + 它的模式层 → 重建后保持展开; 第二张仍旧折叠
    top0 = win.tree.topLevelItem(0)
    top0.setExpanded(True)
    top0.child(0).setExpanded(True)
    win._reload_tree()
    top0 = win.tree.topLevelItem(0)
    assert top0.isExpanded(), "用户展开过的分组重建后应保持展开"
    assert top0.child(0).isExpanded()
    assert not win.tree.topLevelItem(1).isExpanded(), "没展开的应保持折叠"

    # ③ 再折叠回去 → 重建后仍折叠
    top0.setExpanded(False)
    win._reload_tree()
    assert not win.tree.topLevelItem(0).isExpanded(), "用户折叠过的分组重建后应保持折叠"

    # ④ 走真实路径(只把模态对话框的 exec 换成"立刻关掉"): 「地图管理」关掉后重建侧栏
    top0 = win.tree.topLevelItem(0)
    top0.setExpanded(True)
    real_exec = rw.MapManagerDialog.exec
    rw.MapManagerDialog.exec = lambda self: None
    try:
        win._open_map_manager()
    finally:
        rw.MapManagerDialog.exec = real_exec
    assert win.tree.topLevelItem(0).isExpanded(), "「地图管理」关掉后也要保持展开"

    # ⑤ 录完一条新轨迹 → 自动展开到它(否则会藏在折叠的分组里看不见)
    for it in win._iter_route_items():          # 先把所有分组折起来
        parent = it.parent()
        while parent is not None:
            parent.setExpanded(False)
            parent = parent.parent()
    rec2 = win.store.new_route("k1", "地图 1", "T-80B", 0.7, 0.7)
    rec2.duration = 11.0
    win.store.add(rec2)
    win._on_recording_finished(rec2)
    row = next(it for it in win._iter_route_items()
               if it.data(0, rw._ROLE_ID) == rec2.route_id)
    assert win.tree.visualItemRect(row).height() > 0, "刚录的这条应可见"
    parent = row.parent()
    while parent is not None:
        assert parent.isExpanded(), "录完一条应自动展开到它"
        parent = parent.parent()
    win.close()
    results.append("启动默认折叠 + 侧栏重建保持展开/折叠 + 录完自动展开到新条目 OK")


def test_window(tmpdir: str):
    win = make_window(tmpdir, "win_routes.json")
    win.recorder.stop()               # 关闭真实 8111 轮询
    win.hotkeys.stop()
    win.show()
    assert win.tree.topLevelItemCount() == 0

    rec = win.store.new_route("k1", "地图 1", "Leopard 2Pl", 0.2, 0.3)
    rec.duration = 97.0
    win.store.add(rec)
    win._reload_tree()

    assert win.tree.topLevelItemCount() == 1
    top = win.tree.topLevelItem(0)
    # 列: 0=眼睛 1=地图/线路 2=用时
    assert top.text(1) == "地图 1" and top.text(2) == "1 条"
    rows = routes_of(top)                      # 地图 → 模式 → 轨迹
    assert len(rows) == 1
    child = rows[0]
    assert child.text(1) == rec.label and child.text(2) == "01:37"

    win._load_playback([rec])         # 载入回放 → 进入回放模式并锁定其余功能
    assert win.btn_play_stop.isEnabled() and not win.tree.isEnabled(), \
        "回放期间应锁定其余功能(包括侧栏/眼睛)"

    win._end_playback()               # 结束播放后恢复
    assert not win.btn_play_stop.isEnabled() and win.tree.isEnabled()
    assert win.map_view.playback_records == []

    win._update_buttons(True)         # 按钮状态随录制切换
    assert not win.btn_start.isEnabled()
    assert win.btn_stop.isEnabled() and win.btn_mark.isEnabled()
    # 三个录制按钮都带图标(绿圈/红圈/黄圈, 与地图上的标记同色)
    for btn, kind in ((win.btn_start, "start"), (win.btn_stop, "stop"),
                      (win.btn_mark, "mark")):
        assert not btn.icon().isNull(), f"{btn.text()} 应有图标"
        assert not circle_icon(kind).isNull()
    win._update_buttons(False)
    assert win.btn_start.isEnabled() and not win.btn_stop.isEnabled()

    win._handle_action("mark")        # 动作入口不抛异常(音效已打桩)
    win.close()
    results.append("RouteTesterWindow 侧栏/按钮/回放 OK")


def test_map_switch(tmpdir: str):
    """换图/退出战局/录制中换图的轨迹清理规则。"""
    import hashlib

    from wt81111g.route_store import PLACEHOLDER_MAP_SHA1

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ph_path = os.path.join(root, "docs", "assets", "8111_map_placeholder.png")
    with open(ph_path, "rb") as fh:
        ph_bytes = fh.read()
    # 常量必须与真实占位图一致(占位图是实测下来存档的, 容易因 8111 改版而失效)
    assert hashlib.sha1(ph_bytes).hexdigest() == PLACEHOLDER_MAP_SHA1

    def png(color: str) -> bytes:
        img = QImage(64, 64, QImage.Format.Format_RGB32)
        img.fill(QColor(color))
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        buf.close()
        return bytes(ba)

    view = RouteMapView()
    view.resize(600, 480)
    map_a, map_b = png("#334455"), png("#554433")
    assert view.set_map_bytes(map_a) is True
    assert view.set_map_bytes(map_a) is False, "同一张地图不应重复触发换图"

    store = RouteStore(os.path.join(tmpdir, "switch.json"))
    rec = store.new_route("AAA", "地图 A", "Leopard 2PL", 0.3, 0.3)
    rec.points = [RoutePoint(0.3 + i * 0.02, 0.3, i * 0.5, 30.0) for i in range(4)]
    rec.duration = 2.0
    view.set_live(rec)
    view.set_visible_tracks([rec])
    view.set_playback(rec, 1.0)

    # 同一张地图: 什么都不清
    assert view.drop_tracks_for_other_map("AAA") == []
    assert view.live_record is rec and view.visible_tracks == [rec]
    assert view.playback_record is rec

    # 换了地图: 旧地图的实时/显示中/回放全部清掉
    dropped = view.drop_tracks_for_other_map("BBB")
    assert len(dropped) == 3, dropped
    assert view.live_record is None and view.visible_tracks == []
    assert view.playback_record is None and view._playhead == 0.0

    # 录制中换图: 保留实时轨迹(否则轨迹会在录制中途直接消失)
    view.set_live(rec)
    dropped = view.drop_tracks_for_other_map("BBB", keep_live=True)
    assert dropped == [] and view.live_record is rec
    view.set_live(None)

    # 战局外: 8111 返回 “?” 占位图, 不应当地图绘制, 但算作一次变化
    assert view.set_map_bytes(ph_bytes) is True
    assert view._pixmap is None, "占位图不应被当作地图显示"
    assert view.map_digest == PLACEHOLDER_MAP_SHA1
    assert view._status, "应显示提示文字"
    view.render(QImage(600, 480, QImage.Format.Format_ARGB32))   # 占位状态也要能绘制

    # 从占位图回到真实地图
    assert view.set_map_bytes(map_b) is True
    assert view._pixmap is not None and view.map_digest != PLACEHOLDER_MAP_SHA1

    # 窗口层: 换图时清理挂载在旧地图上的轨迹/回放/上次录制
    win = make_window(tmpdir, "switch_win.json")
    win.recorder.stop()
    win.hotkeys.stop()
    win.store.add(rec)
    win._reload_tree()
    old = win.store.new_route("AAA", "地图 A", "Leopard 2PL", 0.3, 0.3)
    old.duration = 2.0
    win._last_record = old
    win._load_playback(old)
    assert win.player.record is old
    win.recorder._map_key = "AAA"
    win._drop_tracks_of_other_map()            # 同一张地图 → 保留
    assert win.player.record is old and win._last_record is old
    win.recorder._map_key = "BBB"
    win._drop_tracks_of_other_map()            # 换图 → 清除
    assert win._last_record is None
    assert win.player.record is None
    assert win.map_view.playback_record is None
    assert "清除" in win.lbl_status.text(), win.lbl_status.text()
    win.close()
    results.append("换图轨迹清理(同图保留/换图清除/占位图) OK")


def test_start_on_move(tmpdir: str):
    """勾选“以速度从零变化时刻作为起始时间点”: 先等待移动, 再把计时起点回溯到起步前一帧。

    _recent 是“最近采样缓冲”, 这里手工灌入带精确时间戳的样本来验证回溯结果
    (不能靠 _tick 的实时时钟, 那样起点与按下时刻只差零点几毫秒, 测不出区别)。
    """
    store = RouteStore(os.path.join(tmpdir, "onmove.json"))
    rec_w = RouteRecorder(store)
    rec_w.client = FakeClient()
    armed: list = []
    started: list = []
    cancelled: list = []
    notices: list[str] = []
    rec_w.start_armed.connect(armed.append)
    rec_w.recording_started.connect(started.append)
    rec_w.recording_cancelled.connect(lambda: cancelled.append(True))
    rec_w.notice.connect(notices.append)

    # 1) 速度为 0 → 只进入“等待移动”, 不建立计时起点
    rec_w.client.speed = 0.0
    rec_w.request_start(True)
    rec_w._tick()
    assert rec_w.recording and rec_w.armed, (rec_w.recording, rec_w.armed)
    assert len(armed) == 1 and not started, (armed, started)
    assert rec_w._t0 == 0.0, "等待期间不应计时"
    rec_w.request_mark()                     # 等待中标记应被拒绝
    rec_w._tick()
    assert "等待载具移动" in notices[-1], notices
    assert len(armed[0].marks) == 1

    # 2) 回溯起点: 速度为 0 的最后时刻 = 起步前一帧
    rec = armed[0]
    rec_w._recent.clear()
    rec_w._recent.extend([
        (97.0, 0.400, 0.400, 0.0, None),           # 更早的静止
        (98.0, 0.401, 0.400, 0.0, None),
        (98.5, 0.402, 0.400, 0.0, None),           # ★ 最后一个速度为 0 的时刻 → 计时起点
        (98.9, 0.410, 0.402, 4.0, (0.0, -1.0)),    # 已经起步的帧(必须补成轨迹点)
    ])
    rec_w._armed_at = 99.2                    # 按下“开始录制”的时刻
    rec_w._arm_check(7.0)                     # 本帧速度 7 km/h → 触发
    assert not rec_w.armed and len(started) == 1, (rec_w.armed, started)
    assert rec_w._t0 == 98.5, rec_w._t0       # 起点=静止的最后一帧, 不是 _armed_at
    assert rec.start_x == 0.402 and rec.start_y == 0.400
    assert rec.marks[0].kind == "start" and rec.marks[0].t == 0.0
    assert rec.marks[0].x == 0.402 and rec.marks[0].y == 0.400
    assert len(rec.points) == 1, rec.points   # 起点之后的位移帧被补录
    assert abs(rec.points[0].t - 0.4) < 1e-9 and rec.points[0].speed == 4.0
    assert (rec.points[0].dx, rec.points[0].dy) == (0.0, -1.0), "补录的点也要带朝向"
    assert rec_w._last_pt_t == rec.points[0].t

    # 3) 起步后照常采样/结束, 用时从回溯出的起点算起
    rec_w._sample((0.42, 0.41), 3.0, 30.0)
    assert len(rec.points) == 2
    rec_w.request_stop()
    rec_w._tick()
    assert not rec_w.recording and not rec_w.armed
    assert store.routes and store.routes[-1].route_id == rec.route_id
    assert store.routes[-1].duration > 3.0

    # 4) 按下时车已在行驶(回溯窗口内没有速度为 0) → 退化为按按下时刻计时, 并给出提示
    started.clear()
    notices.clear()
    rec_w.request_start(True)
    rec_w._tick()
    assert rec_w.armed
    rec_w._recent.clear()
    rec_w._recent.extend([(200.0, 0.5, 0.5, 40.0, (1.0, 0.0)),
                          (200.1, 0.51, 0.5, 42.0, (1.0, 0.0))])
    rec_w._armed_at = 200.2
    rec_w._arm_check(45.0)
    assert len(started) == 1 and rec_w._t0 == 200.2, rec_w._t0
    assert "没有速度为 0" in notices[-1], notices
    assert rec_w._recent or True

    # 5) 等待移动期间按“结束录制” → 本次录制作废(不入库)
    rec_w.request_stop()                 # 先正常结束第 4 步那次录制(应入库)
    rec_w._tick()
    before = len(store.routes)
    rec_w.request_start(True)
    rec_w._tick()
    assert rec_w.armed
    rec_w.request_stop()
    rec_w._tick()
    assert not rec_w.recording and not rec_w.armed
    assert cancelled and len(store.routes) == before, (cancelled, len(store.routes))
    assert "已取消" in notices[-1], notices
    results.append("以速度从零变化时刻为起点(等待移动/回溯起点/取消) OK")


def test_eye_visibility_opacity(tmpdir: str):
    """侧栏眼睛图标控制轨迹显示/隐藏; **轨迹一律不透明**。

    透明度用像素级验证: 同一条轨迹分别画在深/浅两种底图上取色 ——
    不透明 → 两次取色完全相同(颜色不随底图变); 半透明 → 取色随底图变。

    2026-10-22: 回放不再画“未播放的全长底衬”(否则一按播放就能看到整条灰影),
    所以 playhead=0 时该位置应该就是底图色。
    """
    def png(color: str) -> bytes:
        img = QImage(64, 64, QImage.Format.Format_RGB32)
        img.fill(QColor(color))
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        buf.close()
        return bytes(ba)

    store = RouteStore(os.path.join(tmpdir, "opacity.json"))
    rec = store.new_route("AAA", "地图 A", "Leopard 2PL", 0.2, 0.5)
    rec.points = [RoutePoint(0.2 + k * 0.05, 0.5, k * 0.5, 30.0) for k in range(13)]
    rec.duration = 6.0
    rec.marks.append(RouteMark(0.8, 0.5, 6.0, "stop"))

    view = RouteMapView()
    view.resize(600, 400)          # 64×64 底图 → 6.25 倍, 地图矩形落在整数像素上

    def track_color(mode: str, bg: str) -> tuple[int, int, int]:
        view.set_map_bytes(png(bg))
        view.set_live(None)
        view.set_playback(None)
        view.set_visible_tracks([])
        if mode == "visible":
            view.set_visible_tracks([rec])
        elif mode == "playback":
            view.set_playback(rec, 0.0)      # playhead=0 → 还没播放任何段
        else:
            view.set_live(rec)
        canvas = QImage(view.width(), view.height(), QImage.Format.Format_ARGB32)
        view.render(canvas)
        r = view._map_rect()
        c = canvas.pixelColor(int(r.left() + 0.5 * r.width()),
                              int(r.top() + 0.5 * r.height()))
        return (c.red(), c.green(), c.blue())

    dark, light = "#282828", "#c8c8c8"
    assert track_color("visible", dark) == track_color("visible", light), \
        "眼睛点亮的轨迹必须不透明(颜色不应随底图变化)"
    assert track_color("live", dark) == track_color("live", light), \
        "录制中的轨迹必须不透明"
    # 回放未播放段不画 → 取到的就是底图色(两种底图各自不同)
    assert track_color("playback", dark) == (0x28, 0x28, 0x28), \
        "回放不该提前把未播放的轨迹画出来(会看到整条灰影)"
    assert track_color("playback", light) == (0xC8, 0xC8, 0xC8)
    # 不透明 ≠ 半透明: 同底图下两种画法颜色必须不同
    assert track_color("visible", dark) != track_color("playback", dark)

    # 两条轨迹同时显示时, **两条的起点圈都要画**(用户报过“其中一条起点圈丢失”)
    other = store.new_route("AAA", "地图 A", "T", 0.7, 0.7)
    other.points = [RoutePoint(0.7 + k * 0.03, 0.7 + k * 0.02, k * 0.5, 30.0) for k in range(5)]
    other.duration = 3.0
    other.marks.append(RouteMark(0.82, 0.78, 3.0, "stop"))
    view.set_map_bytes(png_solid("#101010"))
    view.set_live(None)
    view.set_playback(None)
    view.set_visible_tracks([rec, other])
    canvas = QImage(view.width(), view.height(), QImage.Format.Format_ARGB32)
    view.render(canvas)
    r = view._map_rect()

    def has_start_circle(nx: float, ny: float) -> bool:
        cx, cy = r.left() + nx * r.width(), r.top() + ny * r.height()
        for dy in range(-6, 7):
            for dx in range(-6, 7):
                c = canvas.pixelColor(int(cx) + dx, int(cy) + dy)
                # 起点色 #22c55e
                if abs(c.red() - 34) < 45 and abs(c.green() - 197) < 45 and abs(c.blue() - 94) < 45:
                    return True
        return False

    assert has_start_circle(0.2, 0.5), "第 1 条轨迹的起点圈丢失"
    assert has_start_circle(0.7, 0.7), "第 2 条轨迹的起点圈丢失"

    # ---------------------------------------------------------------- 眼睛图标
    assert eye_icon(True).cacheKey() != eye_icon(False).cacheKey(), \
        "显示/隐藏必须是两个不同的图标"
    # 图标资源真实存在(来自开源图标库), 而不是退化的简笔画
    for name in ("eye.png", "eye_off.png"):
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "assets", "icons", name)
        assert os.path.exists(p), f"缺少图标资源 {p}"
    pm_on, pm_off = eye_icon(True).pixmap(24, 24), eye_icon(False).pixmap(24, 24)
    assert not pm_on.isNull() and not pm_off.isNull()
    assert pm_on.toImage() != pm_off.toImage(), "两种状态的图标画面必须不同"

    # 窗口层: 点眼睛 → 显示/隐藏(取代以前的 Shift 多选)
    app = QApplication.instance()
    win = make_window(tmpdir, "eye_win.json")
    win.recorder.stop()
    win.hotkeys.stop()
    win.show()
    app.processEvents()
    r1 = win.store.new_route("AAA", "地图 A", "T", 0.2, 0.2)
    r1.duration = 10.0
    r2 = win.store.new_route("AAA", "地图 A", "T", 0.3, 0.3)
    r2.duration = 20.0
    win.store.add(r1)
    win.store.add(r2)
    win._reload_tree()
    assert win.map_view.visible_tracks == [], "默认不显示任何轨迹(眼睛都没点亮)"
    assert win.tree.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection, \
        "回放按“选中条目”决定播哪几条 → 需要 Shift/Ctrl 多选"

    top = win.tree.topLevelItem(0)
    rows = routes_of(top)
    assert len(rows) == 2
    # 模式层不应有眼睛图标(只有轨迹条目才有)
    assert top.child(0).icon(0).isNull()
    # 地图节点没有眼睛图标
    assert top.icon(0).isNull(), "一级(地图)条目不应有眼睛图标"
    # **选中条目 ≠ 显示轨迹**: 选中只影响“回放播哪几条”
    rows[0].setSelected(True)
    app.processEvents()
    assert win.map_view.visible_tracks == [], "选中条目不应把轨迹显示到地图上"
    rows[0].setSelected(False)
    app.processEvents()
    assert [r.route_id for r in win._playable_tracks()] == [], "无选中/无显示时没有可回放内容"
    icon_before = rows[0].icon(0).cacheKey()

    win._on_tree_item_clicked(rows[0], 0)      # 点第 1 条的眼睛
    assert [r.route_id for r in win.map_view.visible_tracks] == [r1.route_id]
    assert rows[0].icon(0).cacheKey() != icon_before, "图标应切换为“显示中”"
    icon_shown = rows[0].icon(0).cacheKey()

    win._on_tree_item_clicked(rows[1], 0)      # 点第 2 条的眼睛 → 两条同时显示
    assert {r.route_id for r in win.map_view.visible_tracks} == {r1.route_id, r2.route_id}

    # 点名字列(不是眼睛列)不改变显示状态
    win._on_tree_item_clicked(rows[0], 1)
    assert len(win.map_view.visible_tracks) == 2

    win._on_tree_item_clicked(rows[0], 0)      # 再点一次 → 隐藏
    assert [r.route_id for r in win.map_view.visible_tracks] == [r2.route_id]
    assert rows[0].icon(0).cacheKey() != icon_shown

    # 侧栏重建(结束录制/重命名地图时会发生)不能丢掉“显示中”的状态
    win._reload_tree()
    assert [r.route_id for r in win.map_view.visible_tracks] == [r2.route_id], \
        "重建侧栏后眼睛状态应保持"
    assert routes_of(win.tree.topLevelItem(0))[0].icon(0).cacheKey() != icon_shown

    # 一键显示/隐藏全部
    win._show_all_tracks(True)
    assert len(win.map_view.visible_tracks) == 2
    win._show_all_tracks(False)
    assert win.map_view.visible_tracks == []

    # 高亮层跟着“选中”走(可多条): 不再永远是“最后录制的那条”
    win._last_record = r2
    win._show_all_tracks(True)
    win.tree.clearSelection()
    win._on_tree_selection_changed()
    assert [r.route_id for r in win.map_view.highlight_records] == [r2.route_id], \
        "没有选中时退回“刚录制的那条”"
    rows2 = list(win._iter_route_items())

    def click_row(row, ctrl):
        QTest.mouseClick(win.tree.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ControlModifier if ctrl
                         else Qt.KeyboardModifier.NoModifier,
                         win.tree.visualItemRect(row).center())
        app.processEvents()

    click_row(rows2[0], ctrl=False)                       # 单选第一条
    assert [r.route_id for r in win.map_view.highlight_records] == [r1.route_id]
    click_row(rows2[1], ctrl=True)                        # 再 Ctrl 多选 → 两条都高亮
    assert {r.route_id for r in win.map_view.highlight_records} == \
        {r1.route_id, r2.route_id}
    # 选中不影响“眼睛点亮的显示集合”(那份仍是灰色普通层)
    assert {r.route_id for r in win.map_view.visible_tracks} == {r1.route_id, r2.route_id}

    # 删除记录后, 显示集合里自动移除
    win.tree.clearSelection()
    win._on_tree_selection_changed()
    win._show_all_tracks(True)
    gone = r1.route_id
    win.store.remove(gone)
    win._reload_tree()
    assert all(r.route_id != gone for r in win.map_view.visible_tracks)
    assert gone not in win._visible_ids
    win.close()
    results.append("眼睛图标显示/隐藏轨迹 + 不透明/半透明规则 OK")


def test_store_bom(tmpdir: str):
    """带 BOM 的 routes.json 也要能读(否则会被当成损坏 → 记录全丢, 下次 save 还会覆盖掉)。"""
    path = os.path.join(tmpdir, "bom_routes.json")
    store = RouteStore(path)
    rec = store.new_route("AAA", "地图 A", "T", 0.2, 0.2)
    rec.duration = 5.0
    rec.points = [RoutePoint(0.2, 0.2, 0.0, 0.0), RoutePoint(0.3, 0.3, 5.0, 30.0)]
    store.add(rec)
    raw = open(path, "rb").read()
    open(path, "wb").write(b"\xef\xbb\xbf" + raw)      # 手工加 UTF-8 BOM
    again = RouteStore(path)
    assert len(again.routes) == 1, again.routes
    assert again.routes[0].route_id == rec.route_id
    assert again.routes[0].points[-1].speed == 30.0
    results.append("带 BOM 的 routes.json 可正常读取 OK")


def test_playback_lock(tmpdir: str):
    """回放期间锁定其余功能, 必须点「■ 结束播放」才恢复。

    这是用户报的 bug: 回放结束后回放绘制仍然盖着地图, 点眼睛看起来“没反应”。
    """
    app = QApplication.instance()
    win = make_window(tmpdir, "lock.json")
    win.recorder.stop()
    win.hotkeys.stop()
    win.show()
    app.processEvents()

    recs = []
    for i in range(2):
        r = win.store.new_route("AAA", "地图 A", "T", 0.2 + i * 0.3, 0.2)
        r.points = [RoutePoint(0.2 + i * 0.3 + k * 0.02, 0.2 + k * 0.01, k * 0.5, 30.0)
                    for k in range(6)]
        r.duration = 4.0
        r.marks.append(RouteMark(0.2 + i * 0.3 + 0.1, 0.25, 4.0, "stop"))
        win.store.add(r)
        recs.append(r)
    win._reload_tree()
    win._show_all_tracks(True)
    assert len(win.map_view.visible_tracks) == 2

    # 开始回放 → 其余功能全部禁用
    win._toggle_playback()
    assert win.player.playing and len(win.player.records) == 2
    assert win.tree.isEnabled() is False, "回放中侧栏(眼睛)应禁用"
    assert win.btn_start.isEnabled() is False
    assert win.chk_start_on_move.isEnabled() is False
    assert win.cmb_rate.isEnabled() is False
    assert win.btn_play_stop.isEnabled() is True

    # 回放中直接调用眼睛点击入口也应被拒绝(防止绕过 UI 禁用)
    top = win.tree.topLevelItem(0)
    before = set(win._visible_ids)
    win._on_tree_item_clicked(routes_of(top)[0], 0)
    assert set(win._visible_ids) == before, "回放中不应改变轨迹显示"
    assert "结束播放" in win.lbl_status.text(), win.lbl_status.text()

    # 全局快捷键(开始录制等)同样被拦下, 不会投递到录制器
    win._handle_action("start")
    assert win.recorder.recording is False, "回放中快捷键不应开始录制"
    assert "结束播放" in win.lbl_status.text(), win.lbl_status.text()

    # 播完 → 仍然锁定(直到用户点结束播放)
    win._on_playback_finished()
    assert win.tree.isEnabled() is False
    assert "结束播放" in win.lbl_status.text(), win.lbl_status.text()

    # 结束播放 → 解锁, 并恢复显示眼睛点亮的轨迹
    win._end_playback()
    assert not win.player.records and not win.player.playing
    assert win.map_view.playback_records == []
    assert len(win.map_view.visible_tracks) == 2, "退出回放后应恢复显示点亮的轨迹"
    assert win.tree.isEnabled() and win.btn_start.isEnabled()
    assert win.chk_start_on_move.isEnabled() and win.cmb_rate.isEnabled()
    assert win.btn_play_stop.isEnabled() is False

    # 解锁后眼睛又能用了
    win._on_tree_item_clicked(routes_of(top)[0], 0)
    assert len(win.map_view.visible_tracks) == 1
    win.close()
    results.append("回放锁定其余功能 + 结束播放解锁 OK")


def test_sound(tmpdir: str):
    """提示音: 默认用 Windows 自带音效, 音量靠重采样(不动系统音量), 设置可持久化。"""
    from PyQt6.QtCore import QSettings

    from wt81111g import sound as snd

    # 默认文件: 开始/结束/标记 分别对应 Speech On / Speech Off / Windows Balloon
    if sys.platform == "win32":
        for action, name in (("start", "speech on.wav"), ("stop", "speech off.wav"),
                             ("mark", "windows balloon.wav")):
            path = snd.settings().path_for(action)
            assert path and os.path.basename(path).lower() == name, \
                f"{action} 默认音效应为 {name}, 实际 {path!r}"

    src = snd.settings().path_for("start")
    if src:
        # 音量: 缩放成临时文件, 同一音量命中缓存(不重复算)
        first = snd._scaled_wav(src, 30)
        second = snd._scaled_wav(src, 30)
        assert first and first == second, "音量缩放结果应缓存复用"
        assert os.path.getsize(first) > 1000
        other = snd._scaled_wav(src, 60)
        assert other and other != first, "不同音量应是不同文件"
        # 非 16 位 PCM(拿本文件冒充): 放弃缩放, 按原音量播
        assert snd._scaled_wav(__file__, 30) == ""
        # 音量为 0 = 静音不播(不阻塞)
        t0 = time.perf_counter()
        snd._play_blocking(src, 0)
        assert time.perf_counter() - t0 < 0.05, "音量 0 应直接返回"

    # 设置持久化(用临时 ini, 不污染用户注册表)
    ini = os.path.join(tmpdir, "snd.ini")
    conf = snd.SoundSettings(QSettings(ini, QSettings.Format.IniFormat))
    assert conf.enabled is True and conf.volume == snd.DEFAULT_VOLUME
    conf.set_enabled(False)
    conf.set_volume(35)
    conf.set_file("mark", os.path.abspath(__file__))     # 存在但非 wav: 只测存取
    again = snd.SoundSettings(QSettings(ini, QSettings.Format.IniFormat))
    assert again.enabled is False and again.volume == 35
    assert "route_tester_test.py" in again.label_for("mark")
    again.set_file("mark", "")
    assert again.custom_file("mark") == ""
    assert "默认" in again.label_for("mark")
    # 自定义文件被删掉后自动回退到默认
    conf.set_file("mark", os.path.join(tmpdir, "不存在.wav"))
    assert conf.custom_file("mark") == "", "文件不存在应回退默认"
    assert conf.volume == 35 and conf.enabled is False
    results.append("提示音(默认音效/音量缩放/设置持久化) OK")


def test_eye_click(tmpdir: str):
    """点“展开/收起”列里的眼睛图标必须真能隐藏/显示轨迹。

    回归: 点眼睛时该行会被选中, 而高亮层曾经把“选中的轨迹”画回地图 ——
    于是看起来就像“点了眼睛没反应”。
    """
    win = make_window(tmpdir, "win_eyeclick.json")
    win.recorder.stop()
    win.hotkeys.stop()
    win.show()
    app = QApplication.instance()
    rec = win.store.new_route("k1", "地图 1", "T-72B", 0.2, 0.3)
    rec.duration = 42.0
    win.store.add(rec)
    win._reload_tree()
    row = list(win._iter_route_items())[0]
    win._set_visible(row, True)
    assert len(win.map_view.visible_tracks) == 1

    def click_eye() -> None:
        rect = win.tree.visualItemRect(row)
        QTest.mouseClick(win.tree.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier,
                         QPoint(rect.left() + ICON_SIZE // 2, rect.center().y()))
        app.processEvents()

    click_eye()                                    # 隐藏
    assert rec.route_id not in win._visible_ids, "点眼睛应熄灭"
    assert all(r.route_id != rec.route_id for r in win.map_view.visible_tracks)
    assert all(r.route_id != rec.route_id for r in win.map_view.highlight_records), \
        "高亮层不应把眼睛已关闭的选中轨迹画回地图"

    click_eye()                                    # 再点亮
    assert rec.route_id in win._visible_ids, "再点一下应重新点亮"
    assert [r.route_id for r in win.map_view.visible_tracks] == [rec.route_id]
    win.close()
    results.append("点眼睛图标能真隐藏/显示轨迹(选中时不复活) OK")


def test_eye_icon_no_overlap(tmpdir: str):
    """眼睛图标必须完整落在自己的列里, 不能溢到名称列上压住文字。

    缩进占的是第 0 列: 二级条目的图标从 ~2×缩进 处开始画, 而 Qt 不会把图标
    裁剪在列内 —— 列宽不够时图标就会压在名称列的文字上(用户实际报过)。
    """
    app = QApplication.instance()
    # 把图标临时换成纯红方块, 便于用像素精确量出图标横向范围
    marker = QPixmap(ICON_SIZE, ICON_SIZE)
    marker.fill(QColor("#ff0000"))
    original = rw.eye_icon
    rw.eye_icon = lambda shown, size=ICON_SIZE: QIcon(marker)
    try:
        win = make_window(tmpdir, "overlap.json")
        win.recorder.stop()
        win.hotkeys.stop()
        win.show()
        app.processEvents()
        rec = win.store.new_route("AAA", "地图 A", "Leopard 2A4 Pzbtl 123", 0.3, 0.3)
        rec.duration = 44.0
        win.store.add(rec)
        win._reload_tree()
        app.processEvents()

        row = win.tree.visualItemRect(routes_of(win.tree.topLevelItem(0))[0])
        vp = win.tree.viewport()
        canvas = QImage(vp.width(), vp.height(), QImage.Format.Format_ARGB32)
        vp.render(canvas)
        col1 = win.tree.header().sectionViewportPosition(1)     # 名称列起始 x

        def is_icon(x: int, y: int) -> bool:
            c = canvas.pixelColor(x, y)
            return c.red() > 180 and c.green() < 80 and c.blue() < 80

        # 扫**整行**找图标色像素: 只能扫 x<col1 的话, 溢出到名称列的图标就检不出来了
        icon_x = [x for x in range(vp.width()) for y in range(row.top(), row.bottom() + 1)
                  if is_icon(x, y)]
        assert icon_x, "眼睛图标没画出来"
        assert max(icon_x) < col1, (
            f"图标溢出了眼睛列(图标到 x={max(icon_x)}, 列边界 x={col1}) → 会压住文字")
        # 列宽不够时 Qt 也可能把图标裁掉 —— 两种情况都要拦住
        assert max(icon_x) - min(icon_x) + 1 >= ICON_SIZE - 2, (
            f"眼睛图标被裁掉了(可见宽度 {max(icon_x) - min(icon_x) + 1}px, 应为 {ICON_SIZE}px)")

        bg = canvas.pixelColor(vp.width() - 2, 2)

        def is_text(x: int, y: int) -> bool:
            c = canvas.pixelColor(x, y)
            return (abs(c.red() - bg.red()) + abs(c.green() - bg.green())
                    + abs(c.blue() - bg.blue())) > 60

        text_x = [x for x in range(col1, vp.width())
                  for y in range(row.top(), row.bottom() + 1) if is_text(x, y)]
        assert text_x, "名称列没有文字"
        assert min(text_x) >= max(icon_x) + 4, (
            f"图标与文字距离过近(图标至 x={max(icon_x)}, 文字起于 x={min(text_x)})")
        win.close()
    finally:
        rw.eye_icon = original
    results.append("眼睛图标不溢出列/不压文字 OK")


def make_window(tmpdir: str, name: str) -> RouteTesterWindow:
    """测试用窗口: **不启动 8111 轮询**, 地图缓存指向临时目录。

    否则测试会真的去访问本机 8111(游戏可能在跑)并把地图图片写进真实 data/maps,
    既让断言变脆(会多出没录过轨迹的地图), 也污染用户数据。
    """
    store = RouteStore(os.path.join(tmpdir, name))
    cache = MapCache(os.path.join(tmpdir, "maps_" + name))
    return RouteTesterWindow(store, map_cache=cache, start_recorder=False)


def routes_of(top) -> list:
    """取某个节点下的全部**轨迹条目**(侧栏: 地图 → 模式 → 版本 → 轨迹)。"""
    out = []
    for i in range(top.childCount()):
        ch = top.child(i)
        if ch.data(0, rw._ROLE_KIND) == "route":
            out.append(ch)
        else:
            out.extend(routes_of(ch))
    return out


def test_recording_end_eye(tmpdir: str):
    """录制结束后: 轨迹仍显示在地图上 → 眼睛必须同时点亮(显示状态统一由 _visible_ids 决定)。

    以前录完只调 set_live() 画轨迹, 眼睛还是“闭”的, 图标与实际显示不一致。
    """
    app = QApplication.instance()
    win = make_window(tmpdir, "recend.json")
    win.recorder.stop()
    win.hotkeys.stop()
    win.show()
    app.processEvents()
    rec = win.store.new_route("AAA", "地图 A", "T", 0.3, 0.3)
    rec.points = [RoutePoint(0.3, 0.3, 0.0, 0.0), RoutePoint(0.4, 0.4, 5.0, 40.0)]
    rec.duration = 5.0
    win.store.add(rec)

    win._on_recording_finished(rec)          # 模拟“结束录制”信号
    app.processEvents()
    assert rec.route_id in win._visible_ids, "刚录完的轨迹正在显示, 眼睛必须点亮"
    child = routes_of(win.tree.topLevelItem(0))[0]
    assert child.icon(0).cacheKey() == eye_icon(True).cacheKey(), "眼睛图标应显示为“显示中”"
    assert [r.route_id for r in win.map_view.highlight_records] == [rec.route_id], \
        "刚录完的那条用速度配色高亮"
    assert rec.route_id in [r.route_id for r in win.map_view.visible_tracks], \
        "显示集合里必须有它(实际由高亮层用速度配色绘制, 不重复画灰色那份)"

    # 熄灭眼睛 → 彻底从地图上消失(高亮层也一起退掉)
    win._on_tree_item_clicked(child, 0)
    assert win.map_view.highlight_records == [], "眼睛关掉后不应再高亮显示"
    assert win.map_view.visible_tracks == []

    # 再点亮 → 回到普通(不透明灰)显示
    win._on_tree_item_clicked(child, 0)
    assert [r.route_id for r in win.map_view.visible_tracks] == [rec.route_id]
    assert [r.route_id for r in win.map_view.highlight_records] == [rec.route_id], \
        "仍是刚录完的那条 → 继续用速度配色"
    win.close()
    results.append("录制结束→眼睛点亮且与实际显示一致 OK")


def test_playback_source_selection(tmpdir: str):
    """回放播哪几条 = 侧栏**选中的条目**(选一条播一条, 选多条播多条)。

    没有选中时才依次退回: 眼睛点亮的轨迹 → 刚录制的那条。
    """
    app = QApplication.instance()
    win = make_window(tmpdir, "src.json")
    win.recorder.stop()
    win.hotkeys.stop()
    win.show()
    app.processEvents()
    recs = []
    for i in range(3):
        r = win.store.new_route("AAA", "地图 A", "T", 0.2 + i * 0.2, 0.2)
        r.points = [RoutePoint(0.2 + i * 0.2, 0.2, 0.0, 0.0),
                    RoutePoint(0.3 + i * 0.2, 0.3, 4.0 + i, 30.0)]
        r.duration = 4.0 + i
        win.store.add(r)
        recs.append(r)
    win._reload_tree()
    app.processEvents()
    top = win.tree.topLevelItem(0)
    rows = routes_of(top)
    assert len(rows) == 3

    # 没有任何选中、也没点亮眼睛 → 无内容可播
    assert win._playable_tracks() == []

    # 选中一条 → 只播这一条
    rows[1].setSelected(True)
    QTest.qWait(20)      # 离屏下选择模型偶尔要转一两轮事件循环才同步
    assert [r.route_id for r in win._playable_tracks()] == [recs[1].route_id]

    # 选中两条 → 播这两条(按侧栏顺序)
    rows[2].setSelected(True)
    QTest.qWait(20)
    assert [r.route_id for r in win._playable_tracks()] == \
        [recs[1].route_id, recs[2].route_id]
    win._toggle_playback()
    assert [r.route_id for r in win.player.records] == [recs[1].route_id, recs[2].route_id]
    assert len(win.player.records) == 2 and win.player.playing
    win._end_playback()

    # 没选中时退回“眼睛点亮的轨迹”
    for row in rows:
        row.setSelected(False)
    app.processEvents()
    assert win._playable_tracks() == []
    win._on_tree_item_clicked(rows[0], 0)
    assert [r.route_id for r in win._playable_tracks()] == [recs[0].route_id]

    # 也没点亮时退回“刚录制的那条”
    win._on_tree_item_clicked(rows[0], 0)
    win._last_record = recs[2]
    assert [r.route_id for r in win._playable_tracks()] == [recs[2].route_id]
    win.close()
    results.append("回放来源=选中条目(退回眼睛/上次录制) OK")


def test_map_cache(tmpdir: str):
    """地图图片本地缓存: 同一张图不重复存、占位图不入缓存、可读可删。"""
    from wt81111g.map_cache import MapCache, map_key
    from wt81111g.route_store import PLACEHOLDER_MAP_SHA1

    cache = MapCache(os.path.join(tmpdir, "mcache"))
    data = png_solid("#123456")
    key, created = cache.save(data)
    assert created is True and key == map_key(data) == hashlib.sha1(data).hexdigest()[:12]
    assert cache.has(key) and cache.load(key) == data
    assert os.path.exists(cache.path_of(key))

    # 哈希相同 → 不重复保存
    key2, created2 = cache.save(data)
    assert key2 == key and created2 is False and len(cache.keys()) == 1

    # 另一张图 → 新增一条
    other = png_solid("#654321")
    key3, created3 = cache.save(other)
    assert created3 is True and len(cache.keys()) == 2 and key3 != key

    # 战局外的 "?" 占位图不是地图, 不入缓存
    with open(os.path.join(ROOT, "docs", "assets", "8111_map_placeholder.png"), "rb") as fh:
        ph = fh.read()
    _k, created_ph = cache.save(ph)
    assert created_ph is False
    assert not cache.has(PLACEHOLDER_MAP_SHA1[:12])

    entries = cache.entries()
    assert len(entries) == 2 and all(e.size > 0 for e in entries)
    assert cache.total_bytes() > 0
    assert entries[0].size_text and entries[0].saved_at_text

    assert cache.remove(key) is True and not cache.has(key)
    assert len(cache.keys()) == 1
    assert cache.load("不存在的键") is None
    results.append("地图图片缓存(去重/占位图不入缓存/删除) OK")


def test_map_resolution_alias(tmpdir: str):
    """8111 会把同一张地图发成不同分辨率: 不能当成两张地图(2026-10-09 修的 bug)。

    实测: 同一张图的 1024²/2048² 两份, 缩到 64×64 灰度后的平均差只有 0.41,
    而**最接近的两张不同地图**是 11.97 → 阈值取 4 有足够余量。
    """
    from wt81111g.map_cache import (
        SAME_IMAGE_HAMMING, SAME_IMAGE_TOL, MapCache, gray_distance, image_info,
        looks_same, map_key,
    )

    cache = MapCache(os.path.join(tmpdir, "alias"))
    big, small = png_pattern(256), png_pattern(128)
    other = png_pattern(256, shift=3)
    assert map_key(big) != map_key(small), "分辨率不同 → 字节哈希必然不同"

    # 三道闸的判定(纯函数, 不用图片就能精确测)。实测数字:
    # 同图(0.25~1.0 倍率 × JPEG q60~95, 135 份) 灰度 ≤0.60 / 汉明 ≤372;
    # 不同图 灰度 ≥11.97(整图) / ≥2.91(同一张大地图的不同区域), 汉明 ≥1776
    assert looks_same(0.4, 241) is True                 # 典型同图(缓存里只有这一张)
    assert looks_same(0.4, 241, 2.4) is True            # 次近的刚好压线(2×阈值)
    assert looks_same(0.4, 241, 2.3) is False, "两张都很像 → 宁可不认"
    assert looks_same(SAME_IMAGE_TOL + 0.1, 10) is False
    assert looks_same(0.4, SAME_IMAGE_HAMMING + 1) is False
    # 最危险的一种: 同一张大地图的两个不同区域(灰度很像, 纹理不同) → 必须被汉明闸拦下
    assert looks_same(0.8, 1700) is False, "灰度再像, 结构不一样也不能认"
    assert looks_same(2.91, 1776) is False
    assert looks_same(11.97, 1813) is False, "实测最像的两张不同地图不能被认成同一张"

    info_big, info_small, info_other = image_info(big), image_info(small), image_info(other)
    assert (info_big.width, info_small.width) == (256, 128)
    d_same = gray_distance(info_big.gray, info_small.gray)
    d_other = gray_distance(info_big.gray, info_other.gray)
    assert d_same < SAME_IMAGE_TOL < d_other, (d_same, d_other)

    # 同一张图的另一种分辨率 → 归到已有键, 不新建地图
    key_big, created = cache.save(big)
    assert created is True and key_big == map_key(big)
    key_small, created2 = cache.save(small)
    assert created2 is False and key_small == key_big
    assert cache.keys() == [key_big], cache.keys()

    # 反过来(先小后大): 同样归到一个键, 但缓存图升级为更清晰的那份
    cache2 = MapCache(os.path.join(tmpdir, "alias2"))
    k_small, _ = cache2.save(small)
    cache2.save(big)
    assert cache2.keys() == [k_small], cache2.keys()
    kept = image_info(cache2.load(k_small))
    assert kept is not None and kept.width == 256, "应保留分辨率更高的那份"

    # 真正不同的地图 → 仍然各占一个键
    key_other, created3 = cache2.save(other)
    assert created3 is True and key_other not in (k_small,) and len(cache2.keys()) == 2
    results.append("地图缓存: 同图不同分辨率归并到同一个键 OK")


def test_map_merge_old_duplicates(tmpdir: str):
    """老版本留下的“同图两个键”在启动时合并: 轨迹/名字/底图都并到保留的那个键。"""
    from wt81111g.map_cache import MapCache, map_key

    cache = MapCache(os.path.join(tmpdir, "merge"))
    big, small = png_pattern(256), png_pattern(128)
    key_big, key_small = map_key(big), map_key(small)
    for key, blob in ((key_big, big), (key_small, small)):      # 老版本写下的两份
        with open(os.path.join(cache.dir, f"{key}.png"), "wb") as fh:
            fh.write(blob)

    store = RouteStore(os.path.join(tmpdir, "merged.json"))
    rec1 = store.new_route(key_big, "地图 A", "T-72B", 0.2, 0.2)
    rec1.duration = 5.0
    rec2 = store.new_route(key_small, "地图 A", "T-72B", 0.3, 0.3)
    rec2.duration = 6.0
    store.add(rec1)
    store.add(rec2)
    store.rename_map(key_big, "货运港口")          # 名字在“大图”那个键上
    # 再放一张**长得有点像但确实是另一张**的图: 不该被并进来
    near = png_pattern(256, shift=1)
    key_near, _ = cache.save(near)
    assert key_near not in (key_big, key_small), "相近但不同的图应另占一个键"
    assert len(store.grouped()) == 2, "合并前是两张地图"

    merged = cache.merge_duplicates(store)
    assert merged == [(key_small, key_big)], merged  # 有名字/有记录的那个留下
    assert {r.map_key for r in store.routes} == {key_big}, "轨迹应并到保留的键上"
    assert store.display_name(key_big) == "货运港口"
    assert len(store.grouped()) == 1, "合并后侧栏只剩一张地图"
    assert cache.has(key_big) and not cache.has(key_small), "多余的那份图片应删掉"
    assert cache.has(key_near), "相近但不同的图不能被删"
    assert image_info_ok(cache, key_big, 256), "保留的应是更清晰的那份"
    assert os.path.exists(store.path + ".bak"), "改用户数据前应留一份备份"
    # 再跑一次: 已经没有重复了
    assert cache.merge_duplicates(store) == []
    results.append("地图缓存: 启动时合并历史重复条目 OK")


def test_map_smooth_zoom(tmpdir: str):
    """滚轮放大用的是**平滑(双线性)**缩放; 关掉它画面应明显不同(台阶/马赛克)。

    实测开销可忽略(整帧 1.72 → 1.49 ms), 所以默认开着 —— 这条用例防止以后被误删。
    """
    from PyQt6.QtGui import QPainter

    view = RouteMapView()
    view.resize(400, 400)
    assert view.set_map_bytes(png_pattern(256))
    view._zoom = 6.0                      # 放大到能明显看出滤波差别
    canvas = QImage(400, 400, QImage.Format.Format_ARGB32)
    view.render(canvas)
    pixels = [canvas.pixel(x, y) for y in range(0, 400, 2) for x in range(0, 400, 2)]

    real_base = RouteMapView._draw_base

    def nearest(self, p):                 # 强制最近邻(旧行为)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        return real_base(self, p)

    RouteMapView._draw_base = nearest
    try:
        other = QImage(400, 400, QImage.Format.Format_ARGB32)
        view.render(other)
    finally:
        RouteMapView._draw_base = real_base
    pixels2 = [other.pixel(x, y) for y in range(0, 400, 2) for x in range(0, 400, 2)]
    diff = sum(1 for a, b in zip(pixels, pixels2) if a != b)
    assert diff > 50, f"生产路径应该是平滑缩放(与最近邻应有明显差别), 实测差异 {diff}"
    results.append(f"地图缩放用平滑滤波(与最近邻差 {diff} 个采样点) OK")


def test_map_sharpest(tmpdir: str):
    """8111 发来低分辨率的地图时, 用**手上更清晰的那份**显示(放大看细节差别很大)。"""
    from wt81111g.map_cache import MapCache

    cache = MapCache(os.path.join(tmpdir, "sharp"))
    big, small = png_pattern(256), png_pattern(128)
    key, _ = cache.save(big)
    assert cache.sharpest(key, small) == big, "缓存里更清晰 → 用缓存那份"
    assert cache.sharpest(key, png_pattern(512)) != big, "手上这份更清晰 → 用它自己"
    assert cache.sharpest("不存在的键", small) == small
    assert cache.sharpest(key, None) is None

    # 窗口层: 收到 128 的图时, 底图应该仍是缓存里的 256 那份
    app = QApplication.instance()
    win = make_window(tmpdir, "sharpwin.json")
    win.recorder.stop()
    win.hotkeys.stop()
    key, _ = win.map_cache.save(png_pattern(256))       # 存进**这个窗口**的缓存
    win.recorder._map_key = key
    win._on_map_changed(png_pattern(128))
    app.processEvents()
    assert win.map_view._pixmap is not None
    assert win.map_view._pixmap.width() == 256, win.map_view._pixmap.width()
    assert win.map_view.map_key == key
    win.close()
    results.append("地图清晰度优先(用缓存里更清晰的那份) OK")


def image_info_ok(cache, key: str, width: int) -> bool:
    """缓存里这个键的图片宽度是不是 width(小工具, 只给上面的测试用)。"""
    from wt81111g.map_cache import image_info
    blob = cache.load(key)
    info = image_info(blob) if blob else None
    return info is not None and info.width == width


def test_cached_map_switch(tmpdir: str):
    """关掉游戏也能看回放: 显示/回放时把地图切成对应缓存图; 且不能跨地图混用。"""
    from wt81111g.map_cache import map_key

    app = QApplication.instance()
    win = make_window(tmpdir, "cachemap.json")
    win.recorder.stop()
    win.hotkeys.stop()
    win.show()
    app.processEvents()

    img_a, img_b = png_solid("#204030"), png_solid("#402030")
    key_a, key_b = map_key(img_a), map_key(img_b)
    # 造出两张“已缓存地图”: 文件名就是地图键(与录制时写入的一致)
    for key, blob in ((key_a, img_a), (key_b, img_b)):
        with open(os.path.join(win.map_cache.dir, f"{key}.png"), "wb") as fh:
            fh.write(blob)
    assert win.map_cache.has(key_a) and win.map_cache.has(key_b)

    rec_a = win.store.new_route(key_a, "地图 A", "T", 0.2, 0.2)
    rec_a.points = [RoutePoint(0.2, 0.2, 0.0, 0.0), RoutePoint(0.3, 0.3, 4.0, 30.0)]
    rec_a.duration = 4.0
    rec_b = win.store.new_route(key_b, "地图 B", "T", 0.5, 0.5)
    rec_b.points = [RoutePoint(0.5, 0.5, 0.0, 0.0), RoutePoint(0.6, 0.6, 6.0, 30.0)]
    rec_b.duration = 6.0
    win.store.add(rec_a)
    win.store.add(rec_b)
    win._reload_tree()
    app.processEvents()
    assert win.map_view.map_key is None, "启动时还没有底图"

    # 两张地图各有自己的分组(侧栏按地图分组)
    assert win.tree.topLevelItemCount() == 2
    child_a = routes_of(win.tree.topLevelItem(0))[0]
    child_b = routes_of(win.tree.topLevelItem(1))[0]

    # 1) 显示 A 的轨迹 → 底图自动切成 A 的缓存图
    win._on_tree_item_clicked(child_a, 0)
    assert win.map_view.map_key == key_a, "显示轨迹时应把地图切到对应图片(用缓存)"

    # 2) 再显示 B 的轨迹 → 不能跨地图混显: A 的眼睛自动熄灭, 底图换成 B
    win._on_tree_item_clicked(child_b, 0)
    assert win.map_view.map_key == key_b
    assert win._visible_ids == {rec_b.route_id}, "同一张地图才能一起显示"
    assert child_a.icon(0).cacheKey() != child_b.icon(0).cacheKey()

    # 3) 选中也不允许跨地图/跨版本: 先选 A 再 Ctrl 选 B → 保留基准(A) 并提示
    child_a.setSelected(True)
    app.processEvents()
    child_b.setSelected(True)          # 相当于 Ctrl 点(在已选中基础上追加)
    app.processEvents()
    picked = [r.route_id for r in win._selection_tracks()]
    assert picked == [rec_a.route_id], picked
    assert "同一版本" in win.lbl_status.text(), win.lbl_status.text()
    # 想真正换到 B: 直接点它(清空后选中) —— 基准会跟着换过去
    win.tree.clearSelection()
    app.processEvents()
    child_b.setSelected(True)
    app.processEvents()
    assert [r.route_id for r in win._selection_tracks()] == [rec_b.route_id]
    assert win._anchor == win._group_of(rec_b)
    assert [r.route_id for r in win._playable_tracks()] == [rec_b.route_id]

    # 4) 回放前会把底图切到选中轨迹的那张图
    win.map_view.set_map_bytes(None)
    win.map_view._pixmap = None
    win.map_view._map_digest = None
    win._load_playback([rec_b])
    assert win.map_view.map_key == key_b
    win._end_playback()

    # 5) 没有底图时用缓存恢复(模拟“关掉游戏后重新打开程序”)
    win.map_view._pixmap = None
    win.map_view._map_digest = None
    win._restore_map_for_current_view()
    assert win.map_view.map_key in (key_a, key_b), win.map_view.map_key
    win.close()
    results.append("缓存底图自动切图 + 不跨地图混用 OK")


def test_map_manager(tmpdir: str):
    """地图管理: 缓存列表 + 命名 → 侧栏与轨迹记录都显示新名字; 菜单栏入口存在。"""
    from wt81111g.map_cache import map_key
    from wt81111g.map_manager import MapManagerDialog

    app = QApplication.instance()
    win = make_window(tmpdir, "manager.json")
    win.recorder.stop()
    win.hotkeys.stop()
    win.show()
    app.processEvents()

    menus = [a.text() for a in win.menuBar().actions()]
    assert any("地图" in m for m in menus), menus

    blob = png_solid("#336655")
    key = map_key(blob)
    with open(os.path.join(win.map_cache.dir, f"{key}.png"), "wb") as fh:
        fh.write(blob)
    rec = win.store.new_route(key, "地图 1", "T", 0.2, 0.2)
    rec.duration = 3.0
    win.store.add(rec)
    win._reload_tree()
    app.processEvents()
    assert win.tree.topLevelItem(0).text(1) == win.store.display_name(key)   # 1 = 名称列

    dlg = MapManagerDialog(win.store, win.map_cache, win)
    assert dlg.table.rowCount() == 1, "地图管理应列出缓存的地图"
    assert dlg.table.item(0, 0).data(Qt.ItemDataRole.UserRole) == key
    assert dlg.table.item(0, 3).text() == "1"        # 该图的轨迹数
    # 表格里一律不许就地编辑(以前双击图片会在图片格里弹出输入框)
    assert dlg.table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert not (dlg.table.model().flags(dlg.table.model().index(0, 0))
                & Qt.ItemFlag.ItemIsEditable)
    assert not (dlg.table.model().flags(dlg.table.model().index(0, 1))
                & Qt.ItemFlag.ItemIsEditable)
    # 改名走顶部固定的输入框: 回车即保存(立即写盘)
    dlg.table.setCurrentCell(0, 0)
    app.processEvents()
    assert dlg.edit_name.isEnabled()
    dlg.edit_name.setText("诺曼底")
    dlg.edit_name.returnPressed.emit()
    app.processEvents()
    assert win.store.name_of(key) == "诺曼底"
    win._reload_tree()
    app.processEvents()
    assert win.tree.topLevelItem(0).text(1) == "诺曼底"
    assert win.store.get(rec.route_id).map_name == "诺曼底"
    assert "诺曼底" in win.store.display_name(key)
    # 重建 store 后名字应仍在(落盘)
    again = RouteStore(os.path.join(tmpdir, "manager.json"))
    assert again.name_of(key) == "诺曼底"
    assert key in again.known_maps
    dlg.close()
    win.close()
    results.append("地图管理(缓存列表/命名/落盘/菜单栏) OK")


def test_zones_and_modes(tmpdir: str):
    """战区 → 模式: 解析 / 指纹去重 / 录制时写进记录 / 侧栏三层树。

    8111 不给模式名, 只能靠 /map_obj.json 里 capture_zone 的**数量**判定;
    样本数据用实测格式(三战区局: A/B/C + 各自中心坐标)。
    """
    tri = [
        {"type": "capture_zone", "icon": "capture_zone", "x": 0.4329, "y": 0.5561,
         "zone_id": 0, "zone_label": "A", "zone_label_code": 65},
        {"type": "ground_model", "icon": "Player", "x": 0.5, "y": 0.5},
        {"type": "capture_zone", "icon": "capture_zone", "x": 0.5899, "y": 0.4277,
         "zone_id": 2, "zone_label": "C", "zone_label_code": 67},
        {"type": "capture_zone", "icon": "capture_zone", "x": 0.499, "y": 0.4985,
         "zone_id": 1, "zone_label": "B", "zone_label_code": 66},
    ]
    zones = zones_from_objects(tri)
    assert [z.label for z in zones] == ["A", "B", "C"]      # 顺序乱了也要按字母排好
    assert [z.x for z in zones] == [0.4329, 0.499, 0.5899]
    assert mode_name(3) == "战区控制" and mode_name(2) == "战区攻陷"
    assert mode_name(1) == "战区争夺" and mode_name(0) == "未知模式"
    sig3 = signature(zones)
    assert sig3 == "A@0.4329,0.5561;B@0.4990,0.4985;C@0.5899,0.4277", sig3
    assert zones_from_objects([]) == []
    # 没带颜色的样本 → 全部当“中立”
    assert {z.side for z in zones} == {"neutral"}
    assert ownership_text(zones) == ""
    # 归属色(8111 站在玩家视角): 实测两战区局 = 红(敌方)+蓝(我方)
    duo = [
        {"type": "capture_zone", "icon": "capture_zone", "x": 0.4727, "y": 0.5456,
         "zone_id": 0, "zone_label": "A", "color": "#fa0C00"},
        {"type": "capture_zone", "icon": "capture_zone", "x": 0.5137, "y": 0.4705,
         "zone_id": 1, "zone_label": "B", "color[]": [23, 77, 255]},
    ]
    duoz = zones_from_objects(duo)
    assert [z.side for z in duoz] == ["enemy", "ours"], [z.side for z in duoz]
    assert duoz[1].color == "#174DFF", duoz[1].color   # color[] 数组也能还原
    assert duoz[0].side_label == "敌方" and duoz[1].side_label == "我方"
    assert mode_name(len(duoz)) == "战区攻陷"
    assert ownership_text(duoz) == "我方 B, 敌方 A", ownership_text(duoz)
    # 颜色**不进指纹**: 攻陷模式会换手, 但模式还是同一个
    flipped = [Zone("A", 0.4727, 0.5456, "#174DFF", "ours"),
               Zone("B", 0.5137, 0.4705, "#fa0C00", "enemy")]
    assert signature(flipped) == signature(duoz)
    # 闪烁(被占领中): blink 1/2 → contested; zone_state 能看出归属/闪烁变化
    blink = zones_from_objects([
        {"type": "capture_zone", "icon": "capture_zone", "x": 0.5, "y": 0.5,
         "zone_label": "A", "color": "#174DFF", "blink": 2},
    ])
    assert blink[0].contested and blink[0].side == "ours"
    plain = zones_from_objects([
        {"type": "capture_zone", "icon": "capture_zone", "x": 0.5, "y": 0.5,
         "zone_label": "A", "color": "#174DFF"},
    ])
    assert not plain[0].contested
    assert signature(blink) == signature(plain)          # 指纹只看坐标
    assert zone_state(blink) != zone_state(plain)        # 完整状态看得出差别
    enemy = [Zone("A", 0.5, 0.5, "#fa0C00", "enemy")]
    assert zone_state(enemy) != zone_state(plain)        # 占领(颜色变)也要刷新
    assert ownership_text(blink) == "我方 A(占领中)", ownership_text(blink)
    # 旧记录没存过模式 → 未知模式
    bare = RouteRecord("r", "k", "m", "v", "2026-10-06T21:00:00", 1.0, 0.0, 0.0)
    assert bare.mode_label == "未知模式"

    # 战区布局按地图落盘, 同指纹(同模式)只留一条
    store = RouteStore(os.path.join(tmpdir, "zones.json"))
    assert store.remember_zones("m1", zones) is True
    assert store.remember_zones("m1", zones) is False
    assert [x["mode"] for x in store.zone_layouts("m1")] == ["战区控制"]
    assert store.layout_for("m1", sig3)["count"] == 3
    assert store.layout_for("m1", "X@0,0")["mode"] == "战区控制"   # 匹配不到就用最新的
    again = RouteStore(os.path.join(tmpdir, "zones.json"))
    assert len(again.zone_layouts("m1")) == 1                     # 持久化往返

    # 录制线程从同一份 /map_obj.json 里抓战区, 并把模式写进记录
    rstore = RouteStore(os.path.join(tmpdir, "zonerec.json"))
    recorder = RouteRecorder(rstore, base_url="http://127.0.0.1:1",
                             map_cache=MapCache(os.path.join(tmpdir, "maps_zonerec")))
    fake = FakeClient()
    fake.map_objects = lambda: tri
    recorder.client = fake
    recorder._map_key = "m1"
    recorder._tick()
    assert recorder.mode_name == "战区控制"
    assert recorder.zones_signature == sig3
    assert [x["mode"] for x in rstore.zone_layouts("m1")] == ["战区控制"]
    recorder.request_start(False)
    recorder._tick()
    live = recorder._rec
    assert live is not None
    assert (live.zone_count, live.mode_name) == (3, "战区控制")
    recorder.request_stop()
    recorder._tick()
    assert live in rstore.routes and live.mode_label == "战区控制"

    # 占领后**坐标不变、只有归属色变** → 必须刷新(以前只在坐标变时刷新, 图标就不变色)
    assert recorder.zones[0].side == "neutral"
    fake.map_objects = lambda: [dict(o, color="#174DFF") if o.get("type") == "capture_zone"
                               and o.get("zone_label") == "A" else o for o in tri]
    recorder._tick()
    assert [z.side for z in recorder.zones][0] == "ours", recorder.zones
    assert len(rstore.zone_layouts("m1")) == 1, "归属变化不能当成新布局重复落盘"
    # 战区被敌人占走 → 同样要刷新成红色
    fake.map_objects = lambda: [dict(o, color="#fa0C00") if o.get("type") == "capture_zone"
                               and o.get("zone_label") == "A" else o for o in tri]
    recorder._tick()
    assert recorder.zones[0].side == "enemy"
    # 退出战局 → 战区清空(再进图能重新上报)
    fake.map_objects = lambda: []
    recorder._tick()
    assert recorder.zones == [] and recorder.zones_signature == ""

    # 侧栏四层树: 地图 → 模式 → 版本(战区布局) → 轨迹
    win = make_window(tmpdir, "zonetree.json")
    win.recorder.stop()
    win.hotkeys.stop()
    win.show()
    app = QApplication.instance()
    app.processEvents()
    # 记录里带战区指纹(但 map_zones 没存过布局) → 从指纹还原出布局, 版本也能自动编号
    # 造 3 个版本: 攻陷/sig1 两条(用于同版本多选), 争夺/sig2、sig3 各一条
    plan = [("战区攻陷", "A@0.10,0.10;B@0.20,0.20"),
            ("战区攻陷", "A@0.10,0.10;B@0.20,0.20"),
            ("战区争夺", "A@0.50,0.50"),
            ("战区争夺", "A@0.70,0.70")]
    for i, (mode, sig) in enumerate(plan):
        rec = win.store.new_route("m2", "地图 2", "T-90M", 0.3, 0.3)
        rec.duration = 10.0
        rec.zone_count, rec.zone_signature, rec.mode_name = 2, sig, mode
        # 时间戳**固定**(与 plan 反序 → 攻陷最新 → 侧栏里排前面)。
        # 不能用 new_route 默认的“当前秒”: 造数据跨过整秒边界时, 侧栏按时间倒序排,
        # 模式的先后会翻转 → 下面“前两条同版本”就会偶发失败(2026-10-08 修)
        rec.created_at = f"2026-01-01T10:{len(plan) - i:02d}:00"
        win.store.add(rec)
    assert len(win.store.zone_layouts("m2")) == 3, "应从轨迹指纹补回 3 个版本布局"
    win._reload_tree()
    app.processEvents()
    assert win.tree.topLevelItemCount() == 1
    top = win.tree.topLevelItem(0)
    assert top.text(2) == "4 条" and top.childCount() == 2      # 2 个模式
    assert {top.child(i).text(1) for i in range(top.childCount())} == {"战区攻陷", "战区争夺"}
    by_mode = {top.child(i).text(1): top.child(i) for i in range(top.childCount())}
    # 2 战区模式 → 单版本“区域 1”(里面 2 条); 1 战区模式 → 两个随机点位“点位 1/2”
    assert [by_mode["战区攻陷"].child(i).text(1)
            for i in range(by_mode["战区攻陷"].childCount())] == ["区域 1"]
    assert by_mode["战区攻陷"].child(0).childCount() == 2
    spots = [by_mode["战区争夺"].child(i).text(1)
             for i in range(by_mode["战区争夺"].childCount())]
    assert sorted(spots) == ["点位 1", "点位 2"], spots
    assert all(by_mode["战区争夺"].child(i).childCount() == 1
               for i in range(by_mode["战区争夺"].childCount()))
    assert len(list(win._iter_route_items())) == 4        # 四层下仍能遍历到轨迹
    rows = list(win._iter_route_items())
    assert win._group_of(win.store.get(rows[0].data(0, rw._ROLE_ID))) == \
        win._group_of(win.store.get(rows[1].data(0, rw._ROLE_ID)))       # 前两条同版本
    assert win._group_of(win.store.get(rows[2].data(0, rw._ROLE_ID))) != \
        win._group_of(win.store.get(rows[0].data(0, rw._ROLE_ID)))

    # 层级行(地图/模式/版本)是分组标题: **不能被选中**(多选时不能把它们也选进去)
    top_node = win.tree.topLevelItem(0)
    mode_node = top_node.child(0)
    var_node = mode_node.child(0)
    for node in (top_node, mode_node, var_node):
        assert not (node.flags() & Qt.ItemFlag.ItemIsSelectable), node.text(1)
        assert node.flags() & Qt.ItemFlag.ItemIsEnabled, "仍要能展开/折叠、能右键"
    assert rows[0].flags() & Qt.ItemFlag.ItemIsSelectable
    win.tree.clearSelection()
    app.processEvents()
    top_node.setSelected(True)
    mode_node.setSelected(True)
    var_node.setSelected(True)
    app.processEvents()
    assert win.tree.selectedItems() == [], "层级行不能被选中"
    assert win._sel_ids == set()

    # 显示只限**同一版本**: 点亮其它版本的轨迹时, 原来的自动隐掉
    win._set_visible(rows[0], True)
    assert len(win._visible_ids) == 1
    win._set_visible(rows[3], True)          # rows[3] 属于另一个版本
    assert len(win._visible_ids) == 1, "不同版本不能共同显示"
    assert rows[3].data(0, rw._ROLE_ID) in win._visible_ids
    # 一键显示也只作用在一个版本上(树木顺序里第一个 = 攻陷/区域 1, 共 2 条)
    win._show_all_tracks(True)
    assert len(win._visible_ids) == 2
    win._show_all_tracks(False)
    assert win._visible_ids == set()

    # 双击=定基准: 之后多选锁死在该版本内
    # (必须用真实的 Ctrl+左键 —— QTreeWidgetItem.setSelected 是“清空后选中”语义,
    #  与用户按住 Ctrl 多选的行为不同, 用它测不出这个拦截)
    win._on_tree_double_clicked(rows[0], 1)
    assert win._sel_ids == {rows[0].data(0, rw._ROLE_ID)}
    assert win._anchor is not None and "基准" in win.lbl_play.text()

    def ctrl_click(row):
        QTest.mouseClick(win.tree.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.ControlModifier,
                         win.tree.visualItemRect(row).center())
        app.processEvents()

    ctrl_click(rows[2])                      # 想去选另一个版本 → 被拦下
    assert win._sel_ids == {rows[0].data(0, rw._ROLE_ID)}, win._sel_ids
    assert not rows[2].isSelected()
    ctrl_click(rows[1])                      # rows[1] 与基准同版本 → 允许
    assert len(win._sel_ids) == 2
    # 双击另一版本的轨迹 → 基准可以切换, 且只选中被双击的那一条
    win._on_tree_double_clicked(rows[2], 1)
    assert win._sel_ids == {rows[2].data(0, rw._ROLE_ID)}
    assert win._anchor == win._group_of(win.store.get(rows[2].data(0, rw._ROLE_ID)))
    # 双击还会把它显示到地图上(并切到它那张图的缓存底图)
    assert rows[2].data(0, rw._ROLE_ID) in win._visible_ids
    assert any(r.route_id == rows[2].data(0, rw._ROLE_ID)
               for r in win.map_view.visible_tracks)

    # 图钉: 只标在“基准版本”那一行, 换基准时跟着走
    top_now = win.tree.topLevelItem(0)
    mode_rows = {top_now.child(i).text(1): top_now.child(i)
                 for i in range(top_now.childCount())}
    variant_rows = [mode_rows[m].child(i) for m in mode_rows
                    for i in range(mode_rows[m].childCount())]
    pinned = [it for it in variant_rows if not it.icon(0).isNull()]
    assert len(pinned) == 1, [it.text(1) for it in pinned]
    base_sig = win.store.get(rows[2].data(0, rw._ROLE_ID)).zone_signature
    assert pinned[0].data(0, rw._ROLE_SIG) == base_sig, pinned[0].text(1)

    # 没选中任何条目时: 第一个点到的路线就把它的版本定为基准
    win.tree.clearSelection()
    app.processEvents()
    win._anchor = None                  # 模拟“刚打开软件, 还没定过基准”
    win._refresh_anchor_icon()
    ctrl_click(rows[0])
    assert win._anchor == win._group_of(win.store.get(rows[0].data(0, rw._ROLE_ID)))
    ctrl_click(rows[2])                 # 另一个版本 → 被拦下(基准仍是 rows[0] 的版本)
    assert win._anchor == win._group_of(win.store.get(rows[0].data(0, rw._ROLE_ID)))
    assert not rows[2].isSelected()

    # 录制期间: 侧栏与“会改显示/底图”的菜单入口都要锁住, 并清掉之前的预览
    live_rec = win.store.get(rows[0].data(0, rw._ROLE_ID))
    win._visible_ids = {rows[2].data(0, rw._ROLE_ID)}
    win._enter_recording_ui(live_rec, armed=False)
    assert not win.tree.isEnabled(), "录制中侧栏应禁用(防止触发预览)"
    assert not win.act_manage.isEnabled() and not win.act_refresh.isEnabled()
    assert win._visible_ids == set(), "录制开始时应清掉之前预览的轨迹"
    win._update_buttons(False)
    assert win.tree.isEnabled() and win.act_manage.isEnabled()

    # 右键重命名版本 → 树里立刻换成新名字
    win.store.rename_layout("m2", "A@0.10,0.10;B@0.20,0.20", "大地图")
    win._reload_tree()
    app.processEvents()
    names = [win.tree.topLevelItem(0).child(0).child(i).text(1)
             for i in range(win.tree.topLevelItem(0).child(0).childCount())]
    assert names == ["大地图"], names
    assert win.store.layout_display_name("m2", "A@0.10,0.10;B@0.20,0.20") == "大地图"
    # 名字落盘(重建 store 仍在)
    again_store = RouteStore(os.path.join(tmpdir, "zonetree.json"))
    assert again_store.layout_display_name("m2", "A@0.10,0.10;B@0.20,0.20") == "大地图"

    # 地图上的战区: 归属色变化要刷新, blink>0 时跑闪烁定时器
    view = win.map_view
    view.set_show_zones(True)
    view.set_zones([{"label": "A", "x": 0.4, "y": 0.4, "side": "neutral"}])
    assert not view._blink_timer.isActive()
    view.set_zones([{"label": "A", "x": 0.4, "y": 0.4, "side": "ours"}])
    assert view.zones[0]["side"] == "ours"               # 同坐标但归属变了 → 要生效
    assert not view._blink_timer.isActive()
    view.set_zones([{"label": "A", "x": 0.4, "y": 0.4, "side": "ours", "blink": 2}])
    assert view._blink_timer.isActive(), "被占领中应闪烁"
    view.set_zones([{"label": "A", "x": 0.4, "y": 0.4, "side": "ours", "blink": 0}])
    assert not view._blink_timer.isActive()
    # 关掉“显示战区”时不应空转
    view.set_zones([{"label": "A", "x": 0.4, "y": 0.4, "side": "ours", "blink": 1}])
    assert view._blink_timer.isActive()
    view.set_show_zones(False)
    assert not view._blink_timer.isActive()
    view.set_zones([])
    win.close()
    results.append("战区解析/模式判定/录制写入/三层树 OK")


def test_global_hotkey():
    """全局快捷键(轮询通道): 单键 / 带修饰键 两种绑定, 与其它按键无关, 拖长也不漏按。

    用假的 user32.GetAsyncKeyState 复现:
    1. 默认的单键 F5(同时还按着 W 前进) —— 不需要任何修饰键;
    2. 右Shift + 小键盘1 这种老式组合(仍支持, 能区分左右);
    3. **定时器被游戏挤占**: 一次轮询之间键就已经按完松开 —— 靠最低位
       (“自上次查询以来按过”)发现, 这正是“游戏里按了没反应”的修复要点。
    """
    from PyQt6.QtCore import QSettings

    from wt81111g.global_hotkey import (
        DEFAULT_BINDINGS, VK_KEYS, VK_MODIFIERS, VK_NUMPADS, GlobalHotkeyWatcher,
        describe_binding, load_bindings, reset_bindings, save_bindings,
    )

    class FakeUser32:
        """按下集合 + “自上次查询以来按过”集合(最低位只报一次, 与真实 API 一致)。"""

        def __init__(self):
            self.down: set[int] = set()
            self.pressed: set[int] = set()

        def GetAsyncKeyState(self, vk: int) -> int:
            raw = (0x8000 if vk in self.down else 0) | (0x0001 if vk in self.pressed else 0)
            self.pressed.discard(vk)
            return raw

    # 默认绑定: F5/F6/F7 单键, 描述文本可读
    assert DEFAULT_BINDINGS == {"start": ("", "f5"), "stop": ("", "f6"),
                                "mark": ("", "f7")}
    assert describe_binding(("", "f5")) == "F5"
    assert describe_binding(("rshift", "num1")) == "右Shift + 小键盘1"

    rshift = VK_MODIFIERS["rshift"]
    f5 = VK_KEYS["f5"]
    f6 = VK_KEYS["f6"]
    num1_nl_on, num1_nl_off = VK_NUMPADS["num1"]
    vk_w = 0x57                                     # W: 与本程序无关的按键

    # ① 默认绑定(单键 F5/F6/F7): 按着 W 打 F5 照样触发
    fake = FakeUser32()
    watcher = GlobalHotkeyWatcher()
    watcher._user32 = fake
    got: list[str] = []
    watcher.triggered.connect(got.append)
    fake.down = {vk_w, f5}
    fake.pressed = {f5}
    watcher._poll()
    assert got == ["start"], f"按着 W 时 F5 也应触发: {got}"
    watcher._poll()                              # 2) 按住不放: 只触发一次
    watcher._poll()
    assert got == ["start"], f"按住不放不应重复触发: {got}"
    fake.down = {vk_w}
    fake.pressed = set()
    watcher._poll()                              # 3) 松开 → 重新武装
    assert got == ["start"], f"松开后不应额外触发: {got}"
    fake.down = {vk_w, f5}
    fake.pressed = {f5}
    watcher._poll()
    assert got == ["start", "start"], f"松开再按应再次触发: {got}"
    # 4) 定时器被拖长: 两次轮询之间键已按完松开(低位兜底)
    got.clear()
    fake.__init__()
    fake.pressed = {f5}
    watcher._poll()
    assert got == ["start"], f"轮询被拖长时不应漏按: {got}"
    # 5) 另一个动作(F6)不会串到 start
    got.clear()
    fake.__init__()
    fake.down = {f6}
    fake.pressed = {f6}
    watcher._poll()
    assert got == ["stop"], f"F6 应触发 stop: {got}"

    # ② 老式组合仍然支持: 右Shift + 小键盘1(小键盘两种键码都认), 且区分左右
    for num1 in (num1_nl_on, num1_nl_off):
        got.clear()
        fake.__init__()
        combo = {"start": ("rshift", "num1")}
        watcher.set_bindings(combo)
        fake.down = {vk_w, rshift, num1}
        fake.pressed = {rshift, num1}
        watcher._poll()
        assert got == ["start"], f"右Shift+小键盘1 应触发: {got}"
        # 只按小键盘1 / 只按右Shift: 都不触发
        for downs, presses in (({vk_w, num1}, {num1}), ({rshift}, {rshift})):
            got.clear()
            fake.__init__()
            watcher.set_bindings(combo)
            fake.down = set(downs)
            fake.pressed = set(presses)
            watcher._poll()
            assert got == [], f"缺少修饰键/主键时不应触发: {got}"

    # ③ 绑定存取(QSettings 往返 + 无效值回退默认)
    ini = os.path.join(tempfile.gettempdir(), f"hk_{os.getpid()}.ini")
    conf = QSettings(ini, QSettings.Format.IniFormat)
    assert load_bindings(conf) == DEFAULT_BINDINGS              # 未设过 → 默认
    save_bindings(conf, {"start": ("rshift", "num3")})
    assert load_bindings(conf)["start"] == ("rshift", "num3")
    assert load_bindings(conf)["stop"] == ("", "f6")
    conf.setValue("hotkey_start", "乱写的")
    assert load_bindings(conf)["start"] == ("", "f5"), "无效值应回退默认"
    reset_bindings(conf)
    assert load_bindings(conf) == DEFAULT_BINDINGS
    results.append("全局快捷键(默认 F5/F6/F7 + 组合键 + 绑定存取) OK")


def test_zoom_reuses_layer(tmpdir: str):
    """滚轮缩放**不许每一格都重建轨迹层**; 停手后才重建一次变清晰。

    回归(2026-10-21 用户反馈“不播放时缩放也卡”): 以前 `_tracks_key()` 里含几何指纹,
    于是每滚动一格都要把所有轨迹重新栅格化一遍 —— 实测 1920×1040 / 20 条线
    (1.7 万点)重建一次 20~50ms, 滚轮连发 6 格就是 250~500ms, 手感就是“卡”。
    现在改成: 几何变了就先按比例贴旧层(1~2ms), 停手 GEOM_SETTLE_MS 后再重建。
    """
    from PyQt6.QtGui import QWheelEvent

    store = RouteStore(os.path.join(tmpdir, "zoomlayer.json"))
    recs = []
    for k in range(3):
        rec = store.new_route("AAA", "地图 A", "T", 0.1, 0.2 + k * 0.2)
        rec.points = [RoutePoint(0.1 + i * 0.05, 0.2 + k * 0.2 + 0.02 * (i % 2),
                                 i * 0.5, 30.0) for i in range(16)]
        rec.duration = 7.5
        recs.append(rec)
    other = store.new_route("AAA", "地图 A", "T", 0.5, 0.5)
    other.points = [RoutePoint(0.5, 0.5, 0.0, 10.0), RoutePoint(0.6, 0.6, 1.0, 20.0)]
    other.duration = 1.0

    view = RouteMapView()
    view.resize(600, 400)
    view.set_map_bytes(png_solid("#303030"))
    view.set_visible_tracks(recs)
    canvas = QImage(600, 400, QImage.Format.Format_ARGB32)
    view.render(canvas)
    layer = view._track_layer
    assert layer is not None
    assert view._track_geom == view._geom_key(), "建层时应记下几何指纹"
    built_size = view._track_size
    spare = view._spare_img

    # 连续滚 3 格: 层必须一直是同一张图(不重建), 但要按比例贴
    for _ in range(3):
        pos = QPointF(300.0, 200.0)
        view.wheelEvent(QWheelEvent(pos, QPointF(pos.x(), pos.y()), QPoint(0, 0),
                                    QPoint(0, 120), Qt.MouseButton.NoButton,
                                    Qt.KeyboardModifier.NoModifier,
                                    Qt.ScrollPhase.NoScrollPhase, False))
        got = view._layer_tracks()
        assert got is not None
        assert got[0] is layer, "缩放过程中不应重建轨迹层(每格重建一次就是卡顿的来源)"
        assert got[1] != (1.0, 1.0), "应按比例贴旧层"
        view.render(canvas)
    assert view._track_layer is layer
    assert view._track_geom != view._geom_key(), "几何已变, 应与建层时不同"
    assert view._track_size == built_size, "贴图期间仍按建层时的尺寸算比例"

    # 停手 → 延迟重建一次变清晰(把“最后一次几何变化”往前推, 免得测试要等 200ms)
    view._geom_at = 0.0
    view._geom_settle()
    assert view._track_layer is None, "停手后应丢掉按比例顶着的旧层"
    assert view._spare_img is not None, "旧层图像应被回收(省掉每次 20MB 级的分配)"
    view.render(canvas)
    assert view._track_layer is not None
    assert view._spare_img is None, "重建时应把回收的那张图用掉(不再新分配)"
    assert view._track_geom == view._geom_key(), "重建后的层应对应当前几何"
    assert view._track_size == view._map_size()

    # 内容变了(眼睛开关) → 必须立刻重建, 不能等
    view.set_visible_tracks([*recs, other])
    view.render(canvas)
    assert view._track_size == view._map_size(), "内容变化应立即重建"
    results.append("滚轮缩放复用轨迹层(按比例贴/停手重建/图像回收) OK")


def test_zoom_scaled_blit_offset(tmpdir: str):
    """按比例贴旧层时内容必须落在 **q×scale** 处, 不能多出偏移。

    回归: 以前用 `p.scale()` 后再贴图层, 会多出 `(余量+锚点)×(scale-1)` 的偏移
    —— 1920 宽、余量 0.35 时放大 2 倍能偏两百多像素。
    """
    store = RouteStore(os.path.join(tmpdir, "zoomoff.json"))
    rec = store.new_route("AAA", "地图 A", "T", 0.05, 0.5)
    # 一条横线, 正好压在地图竖直中线 y=0.5 上
    rec.points = [RoutePoint(0.05 + i * 0.05, 0.5, i * 0.2, 30.0) for i in range(19)]
    rec.duration = 3.6

    view = RouteMapView()
    view.resize(600, 400)
    view.set_map_bytes(png_solid("#202020"))
    view.set_visible_tracks([rec])
    canvas = QImage(600, 400, QImage.Format.Format_ARGB32)
    view.render(canvas)                         # 建层(zoom=1)

    def color_at(nx: float, ny: float) -> tuple[int, int, int]:
        c = view.map_pos(nx, ny)
        px = canvas.pixelColor(int(round(c.x())), int(round(c.y())))
        return (px.red(), px.green(), px.blue())

    def is_line(nx: float, ny: float) -> bool:
        r, g, b = color_at(nx, ny)
        return abs(r - 0x20) + abs(g - 0x20) + abs(b - 0x20) > 60

    assert is_line(0.5, 0.5), "缩放前: 中线处应有轨迹"
    assert not is_line(0.5, 0.45), "缩放前: 0.45 处不应有轨迹"

    # 放大 2 倍(几何变了 → 走“按比例贴旧层”这条路径, 不重建)
    view._zoom = 2.0
    view._clamp_pan()
    assert view._layer_tracks()[1] != (1.0, 1.0)
    canvas.fill(0)
    view.render(canvas)
    assert is_line(0.5, 0.5), "按比例贴图时轨迹必须仍落在 y=0.5(否则就是偏移 bug)"
    r, g, b = color_at(0.5, 0.42)
    assert abs(r - 0x20) + abs(g - 0x20) + abs(b - 0x20) < 40, \
        f"0.42 处不该有轨迹(实际 {r:#04x},{g:#04x},{b:#04x})"
    # 轨迹层确实是被贴上的那张(没重建)
    assert view._track_geom != view._geom_key()

    # 改窗口大小 → 同样“按比例贴”, 并且必须用**建层时**的余量算偏移
    # (余量 = 控件尺寸×LAYER_MARGIN, 改大小后变了; 用错就会整体错位)
    view._geom_at = 0.0
    view._geom_settle()
    view.render(canvas)                       # 先重建一次(此时余量属于 600×400)
    assert view._track_margin == view._margin()
    built_margin = view._track_margin
    view.resize(500, 360)
    canvas = QImage(500, 360, QImage.Format.Format_ARGB32)
    assert view._layer_tracks()[1] != (1.0, 1.0), "改窗口应走按比例贴"
    view.render(canvas)
    assert view._track_margin == built_margin
    assert is_line(0.5, 0.5), "改窗口大小后轨迹必须仍在 y=0.5(余量要用建层时那份)"
    assert not is_line(0.5, 0.4), "改窗口大小后轨迹不该整体偏移"
    results.append("按比例贴旧层不产生偏移(q×scale) OK")


def test_playback_zoom_resumes(tmpdir: str):
    """回放中缩放之后, 必须能自己恢复“继续累加着色段”(不能永久卡在按比例贴的旧图)。

    回归(2026-10-21 用户反馈“播放全局线路时颜色只显示一部分就不显示了, 只有我重新
    缩放时才显示”): `_note_geom_change()` 每帧都被调用(因为“当前几何 != 建层几何”
    在重建之前每帧都成立), 它会刷新 `_geom_at` —— 于是 `_geom_settle()` 永远看到
    “刚刚还在缩放”, 无限推迟重建, 画面停在旧图层上不再长; 而再缩放一次可能触发
    锚点超余量的立即重建, 于是“一缩放就又有了”。
    """
    from PyQt6.QtGui import QWheelEvent

    store = RouteStore(os.path.join(tmpdir, "playzoom.json"))
    recs = []
    for k in range(3):
        rec = store.new_route("AAA", "地图 A", "T", 0.1, 0.2 + k * 0.25)
        rec.points = [RoutePoint(0.1 + i * 0.045, 0.2 + k * 0.25 + 0.01 * (i % 3),
                                 i * 2.0, 20.0 + i) for i in range(20)]
        rec.duration = 38.0
        recs.append(rec)

    view = RouteMapView()
    view.resize(700, 500)
    view.set_map_bytes(png_solid("#202020"))
    canvas = QImage(700, 500, QImage.Format.Format_ARGB32)
    view.set_playback(recs, 0.0)
    view.render(canvas)
    assert view._play_geom == view._geom_key()

    # 缩放一次 → 进入“按比例贴旧层”
    pos = QPointF(350.0, 250.0)
    view.wheelEvent(QWheelEvent(pos, QPointF(pos.x(), pos.y()), QPoint(0, 0),
                                QPoint(0, 120), Qt.MouseButton.NoButton,
                                Qt.KeyboardModifier.NoModifier,
                                Qt.ScrollPhase.NoScrollPhase, False))
    view.set_playback(recs, 10.0)
    view.render(canvas)
    assert view._play_geom != view._geom_key(), "刚缩放完应是按比例贴(还没重建)"

    # 之后回放每帧都会渲染(几何“不再变化”但依旧 != 建层几何):
    # 必须能在 GEOM_SETTLE_MS 之后重建一次 —— 不能每帧把时间戳顶回去
    for i in range(8):
        QTest.qWait(50)
        view.set_playback(recs, 10.0 + i)
        view.render(canvas)
    assert view._play_geom == view._geom_key(), \
        "缩放停手超过 200ms 必须重建(否则着色段永远停在旧画面上)"

    # 重建之后继续推进 → 已画点数必须继续增长
    before = sum(view._play_drawn.values())
    view.set_playback(recs, 20.0)
    view.render(canvas)
    assert sum(view._play_drawn.values()) > before, "恢复后着色段应继续累加"

    # 轨迹层同理(p_chart 不在回放时)
    view.set_playback(None)
    view.set_visible_tracks(recs)
    view.render(canvas)
    pos = QPointF(350.0, 250.0)
    view.wheelEvent(QWheelEvent(pos, QPointF(pos.x(), pos.y()), QPoint(0, 0),
                                QPoint(0, 120), Qt.MouseButton.NoButton,
                                Qt.KeyboardModifier.NoModifier,
                                Qt.ScrollPhase.NoScrollPhase, False))
    for _ in range(8):
        QTest.qWait(50)
        view.render(canvas)
    assert view._track_geom == view._geom_key(), "轨迹层同样必须停手后重建"
    results.append("回放中缩放后自动恢复(延迟重建不会被每帧刷新时间戳顶掉) OK")


def test_dead_style(tmpdir: str):
    """回放的两个“被击毁”选项: 阵亡后线路变半透明纯灰 + 末端红叉。

    判定完全在录制端(见 global_record.GlobalTracker), 这里只测绘制:
    * 越过阵亡时刻前: 速度配色 + 白点光标, **没有**红叉;
    * 越过之后: 整条线变灰(饱和度接近 0)、光标消失、末端出现红叉;
    * 两个开关都能单独关掉(关掉“变暗”仍保留红叉)。
    """
    from wt81111g.route_store import TrackPoint, UnitLife

    life = UnitLife(life_id="L1", icon="MediumTank", obj_type="ground_model",
                    map_key="AAA", dead=True)
    life.points = [TrackPoint(0.10 + i * 0.05, 0.5, float(i), 40.0) for i in range(10)]
    plain = UnitLife(life_id="L2", icon="IFV", obj_type="ground_model",
                     map_key="AAA", dead=False)
    plain.points = [TrackPoint(0.10 + i * 0.05, 0.7, float(i), 40.0) for i in range(10)]

    view = RouteMapView()
    view.resize(640, 480)
    view.set_map_bytes(png_solid("#303030"))
    canvas = QImage(640, 480, QImage.Format.Format_ARGB32)

    def shot(head: float) -> QImage:
        view.set_playback([life], head)
        view._play_layer = None          # 每次都从干净状态画, 避免累加层干扰取色
        c = QImage(640, 480, QImage.Format.Format_ARGB32)
        view.render(c)
        return c

    def at(img: QImage, nx: float, ny: float) -> tuple[int, int, int]:
        q = view.map_pos(nx, ny)
        c = img.pixelColor(int(round(q.x())), int(round(q.y())))
        return (c.red(), c.green(), c.blue())

    def sat(rgb) -> int:
        return max(rgb) - min(rgb)

    def red_pixels(img: QImage, nx: float, ny: float, half: int = 8) -> int:
        q = view.map_pos(nx, ny)
        cx, cy = int(round(q.x())), int(round(q.y()))
        n = 0
        for dy in range(-half, half + 1):
            for dx in range(-half, half + 1):
                c = img.pixelColor(cx + dx, cy + dy)
                if c.red() > 170 and c.green() < 110 and c.blue() < 110:
                    n += 1
        return n

    def blue_pixels(img: QImage, nx: float, ny: float, half: int = 8) -> int:
        """8111 蓝 `#174DFF` 的像素个数(A 队的阵亡标记)。"""
        q = view.map_pos(nx, ny)
        cx, cy = int(round(q.x())), int(round(q.y()))
        n = 0
        for dy in range(-half, half + 1):
            for dx in range(-half, half + 1):
                c = img.pixelColor(cx + dx, cy + dy)
                if c.blue() > 170 and c.red() < 120 and c.green() < 130:
                    n += 1
        return n

    # ① 阵亡之前(t=3 < 9): 彩色 + 光标, 没有红叉
    assert view._dead_now(life, 3.0) is False
    before = shot(3.0)
    assert sat(at(before, 0.20, 0.5)) > 60, "阵亡前应按速度着色(高饱和度)"
    assert red_pixels(before, 0.55, 0.5) == 0, "还没到阵亡时刻, 不该提前画红叉"

    # ② 越过阵亡时刻(t=12 > 9): 整条灰、无光标、末端红叉
    assert view._dead_now(life, 12.0) is True
    after = shot(12.0)
    rgb = at(after, 0.20, 0.5)
    assert sat(rgb) < 30, f"阵亡后线路应是半透明纯灰(饱和度接近 0), 实际 {rgb}"
    n_mark = red_pixels(after, 0.55, 0.5)
    assert n_mark > 3, "阵亡位置应有红叉"
    # 红叉大小: 与回放光标白点(半径 3)差不多 —— 直径不超过 9px
    q = view.map_pos(0.55, 0.5)
    cx, cy = int(round(q.x())), int(round(q.y()))
    xs = [x for x in range(cx - 12, cx + 13) for y in range(cy - 12, cy + 13)
          if (lambda c: c.red() > 170 and c.green() < 110 and c.blue() < 110)(
              after.pixelColor(x, y))]
    assert xs and max(xs) - min(xs) + 1 <= 9, f"红叉太大了: {max(xs) - min(xs) + 1}px"

    # ③ 关掉“变暗”: 整条线路清除(既不画灰也不画彩), 红叉仍在
    #    (速度配色本身就可能是红的, 所以红叉一律用“开关前后的差值”来断言)
    view.set_dead_style(dim=False)
    after2 = shot(12.0)
    assert sat(at(after2, 0.20, 0.5)) < 30, \
        f"关掉变暗后阵亡线路应被清除(取到底图色), 实际 {at(after2, 0.20, 0.5)}"
    view.set_dead_style(dim=False, mark=False)
    after2b = shot(12.0)
    assert red_pixels(after2, 0.55, 0.5) > red_pixels(after2b, 0.55, 0.5) + 3, \
        "“变暗”关掉不影响红叉"

    # ④ 只开“标记”/只开“变暗”都不影响另一个
    view.set_dead_style(dim=True, mark=False)
    after3 = shot(12.0)
    assert sat(at(after3, 0.20, 0.5)) < 30, "“标记”关掉不影响“变暗”"
    view.set_dead_style(dim=True, mark=True)

    # ⑤ 没有阵亡信息的普通轨迹: 永远不变灰、不画叉
    view.set_playback([plain], 20.0)
    assert view._dead_now(plain, 20.0) is False
    with_x = QImage(640, 480, QImage.Format.Format_ARGB32)
    view._play_layer = None
    view.render(with_x)
    view.set_dead_style(mark=False)
    without_x = QImage(640, 480, QImage.Format.Format_ARGB32)
    view._play_layer = None
    view.render(without_x)
    view.set_dead_style(mark=True)
    assert red_pixels(with_x, 0.55, 0.7) == red_pixels(without_x, 0.55, 0.7), \
        "没有阵亡信息的轨迹不该画红叉"
    assert sat(at(with_x, 0.20, 0.7)) > 60, "普通轨迹到末尾仍是速度配色"

    # ⑤b 标记颜色按队区分: **A 队蓝、B 队红**
    life.side = "A"
    img_a = QImage(640, 480, QImage.Format.Format_ARGB32)
    view.set_playback([life], 12.0)
    view._play_layer = None
    view.render(img_a)
    assert blue_pixels(img_a, 0.55, 0.5) > 3, "A 队的阵亡标记应是蓝色"
    assert red_pixels(img_a, 0.55, 0.5) == 0, "A 队不该再有红叉"
    life.side = "B"
    img_b = QImage(640, 480, QImage.Format.Format_ARGB32)
    view.set_playback([life], 12.0)
    view._play_layer = None
    view.render(img_b)
    assert red_pixels(img_b, 0.55, 0.5) > 3, "B 队的阵亡标记仍是红色"
    assert blue_pixels(img_b, 0.55, 0.5) == 0
    life.side = ""                     # 没有队号(普通轨迹) → 红色兜底
    img_none = QImage(640, 480, QImage.Format.Format_ARGB32)
    view.set_playback([life], 12.0)
    view._play_layer = None
    view.render(img_none)
    assert red_pixels(img_none, 0.55, 0.5) > 3

    view.set_playback(None)

    # ⑥ 窗口层: 两个勾选框**默认都勾上**, 一改就作用到地图并记住设置
    from PyQt6.QtCore import QSettings
    conf = QSettings("WTBlackList", "RouteTester")
    keep = (conf.value("dead_dim"), conf.value("dead_mark"))
    try:
        conf.remove("dead_dim")
        conf.remove("dead_mark")
        win = make_window(tmpdir, "deadwin.json")
        win.recorder.stop()
        win.hotkeys.stop()
        win.show()
        assert win.chk_dead_dim.isChecked(), "「被击毁后路线变暗」应默认勾选"
        assert win.chk_dead_mark.isChecked(), "「被击毁标记」应默认勾选"
        assert win.map_view.dead_dim is True and win.map_view.dead_mark is True
        win.chk_dead_dim.setChecked(False)
        assert win.map_view.dead_dim is False, "取消勾选应立刻作用到地图"
        win.chk_dead_mark.setChecked(False)
        assert win.map_view.dead_mark is False
        assert int(conf.value("dead_dim")) == 0 and int(conf.value("dead_mark")) == 0, \
            "两个选项都要记住"
        win.close()
    finally:
        for key, val in zip(("dead_dim", "dead_mark"), keep):
            if val is None:
                conf.remove(key)
            else:
                conf.setValue(key, val)
    results.append("回放阵亡样式(变灰/红叉/两个开关/无阵亡信息不变灰) OK")


def main() -> int:
    app = QApplication([])
    tmpdir = tempfile.mkdtemp()
    test_helpers()
    test_player_detect()
    test_store(tmpdir)
    test_store_bom(tmpdir)
    test_recorder(tmpdir)
    test_recorder_placeholder()
    test_start_on_move(tmpdir)
    test_refresh_rate()
    test_global_hotkey()
    test_sound(tmpdir)
    test_single_instance()
    test_track_segments()
    test_point_at_and_split()
    test_playback()
    test_zones_and_modes(tmpdir)
    test_spawns(tmpdir)
    test_multi_playback(tmpdir)
    test_map_render()
    test_player_heading()
    test_map_hover()
    test_map_draw_cache()
    test_playback_draw_cache()
    test_map_zoom_pan()
    test_map_switch(tmpdir)
    test_eye_visibility_opacity(tmpdir)
    test_playback_lock(tmpdir)
    test_recording_end_eye(tmpdir)
    test_playback_source_selection(tmpdir)
    test_map_cache(tmpdir)
    test_map_resolution_alias(tmpdir)
    test_map_merge_old_duplicates(tmpdir)
    test_map_smooth_zoom(tmpdir)
    test_zoom_reuses_layer(tmpdir)
    test_zoom_scaled_blit_offset(tmpdir)
    test_playback_zoom_resumes(tmpdir)
    test_dead_style(tmpdir)
    test_map_sharpest(tmpdir)
    test_cached_map_switch(tmpdir)
    test_map_manager(tmpdir)
    test_eye_icon_no_overlap(tmpdir)
    test_eye_click(tmpdir)
    test_tree_expand_state(tmpdir)
    test_window(tmpdir)
    for line in results:
        print("[OK]", line)
    print(f"\n全部通过: {len(results)} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())

