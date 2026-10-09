"""跑图录制状态机 + 8111 轮询(后台线程)。

数据来源:
- 载具型号 / 速度 : GET /indicators  (type / speed)
- 玩家坐标        : GET /map_obj.json (type == "player" 的 x/y, 0~1 归一化)
- 战区坐标/模式   : GET /map_obj.json (type == "capture_zone", 见 zones.py)
- 地图图片        : GET /map.img      (节流请求, 内容变化才上报)

录制命令(开始/标记/结束)由 GUI 线程投递到队列, 在轮询线程内消费,
保证录制状态只被单一线程修改, 无需复杂加锁。
"""
from __future__ import annotations

import hashlib
import logging
import math
import queue
import threading
import time
from collections import deque

from PyQt6.QtCore import QObject, pyqtSignal

from .api8111 import WT8111
from .config import WT_BASE_URL
from .map_cache import MapCache
from .route_store import (
    PLACEHOLDER_MAP_KEY, RouteMark, RoutePoint, RouteRecord, RouteStore, pretty_vehicle,
)
from .zones import Zone, mode_name as zone_mode_name, signature as zones_signature
from .zones import (Spawn, spawn_state, spawn_summary, spawns_from_objects, zone_state,
                    zones_from_objects)

log = logging.getLogger("route_recorder")

# 轮询/采样间隔: 默认 40ms(≈25Hz)。
# 实测 8111 陆战数据本身約每 40ms 更新一次(rpm 变化间隔 p50=40ms),
# 再快只能重复采到同一批数据(46Hz 采样时每个数据点被重复采约 1.85 次)。
DEFAULT_INTERVAL_MS = 40.0
DEFAULT_INTERVAL = DEFAULT_INTERVAL_MS / 1000.0
DEFAULT_HZ = 1.0 / DEFAULT_INTERVAL
_MIN_INTERVAL = 0.01          # 最快 100Hz(10ms)
_MAX_INTERVAL = 0.2           # 最慢 5Hz(200ms)
# 位移阈值: 仅用于滤掉静止时的浮点抖动。
# 注意: 40Hz 下单帧位移很小(坦克 60km/h 约 0.0002 归一化/帧), 阈值必须远小于它,
# 否则会被误拦导致采样稀疏、轨迹发折。
DEFAULT_MIN_MOVE = 0.00001

# “以速度从零变化时刻作为起始时间点”选项的回溯窗口(秒)。
# 触发时从最近采样向前找“最后一个速度为 0 的时刻”, 该时刻即计时起点;
# 窗口取 1.5s: 足以覆盖“点击录制的同时车刚开始动”(命令到达与速度变化相差几十 ms),
# 又不会让起点被拉到很久以前。
START_LOOKBACK = 1.5


# 玩家自身在 map_obj.json 中的标识::
# 实测(自定义房间/试车场)玩家对象 type 为载具类别(如 ground_model / aircraft),
# 真正可靠的是 icon == "Player"; type == "player" 为部分版本的写法, 一并兼容。
def _is_player_obj(obj: dict) -> bool:
    icon = str(obj.get("icon", "")).strip().lower()
    typ = str(obj.get("type", "")).strip().lower()
    return icon == "player" or typ == "player"


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class RouteRecorder(QObject):
    """轮询 8111 并在录制时采样轨迹。"""

    tick = pyqtSignal(dict)              # 每轮状态快照
    map_changed = pyqtSignal(bytes)      # 地图图片变化(原始字节)
    map_cached = pyqtSignal(str, bool)   # (地图键, 是否新建缓存文件)
    recording_started = pyqtSignal(object)   # RouteRecord
    start_armed = pyqtSignal(object)         # RouteRecord(已开始录制但等待载具移动)
    recording_cancelled = pyqtSignal()       # 等待移动却被取消(全程速度为 0)
    mark_added = pyqtSignal(object)          # RouteMark
    recording_finished = pyqtSignal(object)  # RouteRecord
    notice = pyqtSignal(str)                 # 面向用户的提示

    def __init__(self, store: RouteStore, base_url: str = WT_BASE_URL,
                 interval: float = DEFAULT_INTERVAL, map_interval: float = 2.0,
                 sample_interval: float | None = None,
                 min_move: float = DEFAULT_MIN_MOVE, indicators_every: int = 4,
                 timeout: float = 0.5, map_cache: MapCache | None = None, parent=None):
        super().__init__(parent)
        self.store = store
        self.map_cache = map_cache if map_cache is not None else MapCache()
        # 超时取 0.5s: 8111 正常 rtt 仅 ~10-20ms, 短超时能让偶发卡顿快速跳过
        self.client = WT8111(base_url, timeout=timeout)
        self._interval_lock = threading.Lock()
        self.interval = max(_MIN_INTERVAL, min(_MAX_INTERVAL, float(interval)))
        self.map_interval = max(0.5, float(map_interval))
        # 采样间隔跟随刷新率(默认与轮询同频), 保证轨迹点足够密
        self.sample_interval = (self.interval if sample_interval is None
                                else max(0.01, float(sample_interval)))
        self.min_move = max(0.0, float(min_move))
        # /indicators 降频倍数: 载具/速度变化慢, 不必每帧取(坐标 /map_obj 每帧取)。
        # 每轮请求数从 2 降到 ~1.25, 是达到 40Hz 的关键(实测单请求 ~20ms)。
        self.indicators_every = max(1, int(indicators_every))
        self._frame = 0
        self._ind_cache: dict = {}
        self._rate_window: deque[float] = deque(maxlen=40)

        self._stop = threading.Event()
        self._cmds: queue.Queue[str] = queue.Queue()
        self._thread: threading.Thread | None = None

        # 录制状态(仅轮询线程访问)
        self._rec: RouteRecord | None = None
        self._t0 = 0.0
        self._last_pt_t = 0.0
        self._last_xy: tuple[float, float] | None = None
        # 等待载具移动状态(勾选“以速度从零变化时刻作为起点”时)
        self._armed = False
        self._armed_at = 0.0
        # 最近采样环形缓冲(时间, x, y, 速度): 用于回溯“最后一个速度为 0 的时刻”
        self._recent: deque[tuple[float, float, float, float]] = deque(maxlen=400)

        # 地图缓存
        self._map_digest: str | None = None
        self._map_key = "unknown"
        self._map_thread: threading.Thread | None = None

        # 当前战局的战区(轮询线程写入, GUI 只读引用): 坐标是模式指纹的来源
        self._zones: list[Zone] = []
        self._zones_sig = ""          # 坐标指纹(= 模式)
        self._zones_state: tuple = ()   # 完整状态(含归属/闪烁) —— 变了就刷新界面
        self._spawns: list[Spawn] = []  # 重生位置(respawn_base_*)
        self._spawns_state: tuple = ()
        self._spawns_saved: tuple = ()  # 已落盘的那份重生点状态

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="route-recorder", daemon=True)
        self._thread.start()
        # 地图图片(约 230KB)放独立线程: 若在轮询线程里下载, 每 2 秒会卡住一整帧
        self._map_thread = threading.Thread(target=self._map_loop, name="route-map", daemon=True)
        self._map_thread.start()

    def stop(self) -> None:
        self._stop.set()
        for t in (self._thread, self._map_thread):
            if t is not None and t.is_alive():
                t.join(timeout=2.0)
        self._thread = None
        self._map_thread = None

    def _map_loop(self) -> None:
        """独立线程: 定期取地图图片, 仅在内容变化时上报(不阻塞轨迹轮询)。

        每次发现**新地图**就把图片写到本地缓存(`data/maps/<哈希>.<ext>`),
        这样关掉游戏后依旧能用缓存底图看回放; 同一张图(哈希相同)不会重复保存。
        """
        while not self._stop.is_set():
            data = self.client.map_image()
            if data:
                digest = hashlib.sha1(data).hexdigest()
                if digest != self._map_digest:
                    self._map_digest = digest
                    try:
                        # 注意: 用缓存返回的键, 而不是字节哈希 —— 同一张地图的
                        # 不同分辨率会归到同一个键上(见 MapCache.save)
                        key, created = self.map_cache.save(data)
                        self._map_key = key
                        if key != PLACEHOLDER_MAP_KEY:
                            self.store.remember_map(key)
                        self.map_cached.emit(key, created)
                    except Exception as exc:  # noqa: BLE001 - 缓存失败不应影响显示
                        log.warning("缓存地图图片失败: %s", exc)
                        self._map_key = digest[:12]
                    self.map_changed.emit(data)
                    log.info("地图已更新 (key=%s)", self._map_key)
            self._stop.wait(self.map_interval)

    def request_start(self, start_on_move: bool = False) -> None:
        """请求开始录制。

        start_on_move=True(界面勾选“以速度从零变化时刻作为起始时间点”)时,
        不是立即计时, 而是等速度首次不为 0 后再回溯起点 —— 见 _arm_check。
        选项随命令一起投递, 避免与轮询线程共享状态的竞态。
        """
        self._cmds.put(("start", bool(start_on_move)))

    def request_mark(self) -> None:
        self._cmds.put("mark")

    def request_stop(self) -> None:
        self._cmds.put("stop")

    def set_interval_ms(self, ms: float) -> None:
        """按毫秒设置轮询间隔(GUI 线程调用), 采样间隔同步跟随。"""
        interval = max(_MIN_INTERVAL, min(_MAX_INTERVAL, float(ms) / 1000.0))
        with self._interval_lock:
            self.interval = interval
            self.sample_interval = interval
        log.info("采样间隔已设为 %.0f ms (%.0f Hz)", interval * 1000, 1.0 / interval)

    def target_interval_ms(self) -> float:
        return self.interval * 1000.0

    def target_hz(self) -> float:
        return 1.0 / self.interval if self.interval > 0 else 0.0

    @property
    def recording(self) -> bool:
        return self._rec is not None

    @property
    def armed(self) -> bool:
        """已开始录制但还在等载具移动(尚未定下计时起点)。"""
        return self._armed

    @property
    def map_key(self) -> str:
        """当前地图指纹(地图线程更新, 战局外为占位图指纹)。"""
        return self._map_key

    @property
    def zones(self) -> list[Zone]:
        """当前战局的战区(GUI 线程读: 列表引用整体替换, 无需加锁)。"""
        return self._zones

    @property
    def zones_signature(self) -> str:
        """当前战区指纹(空串 = 还不知道/战局外)。"""
        return self._zones_sig

    @property
    def spawns(self) -> list[Spawn]:
        """当前战局解析到的重生位置(双方、地面/空中都在里面)。"""
        return list(self._spawns)

    @property
    def mode_name(self) -> str:
        """由战区数量推出的模式名(战区未知时为空串)。"""
        return zone_mode_name(len(self._zones)) if self._zones else ""

    # ------------------------------------------------------------------ 轮询
    def run(self) -> None:
        log.info("route recorder started (%.0f Hz)", self.target_hz())
        next_at = time.monotonic()
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 - 单次异常不应终止线程
                log.exception("route recorder tick error: %s", exc)
            # 目标周期调度(而非“固定 sleep”), 否则请求耗时会被叠加, 实际频率远低于设定值
            with self._interval_lock:
                interval = self.interval
            next_at += interval
            delay = next_at - time.monotonic()
            if delay <= 0.0:        # 跟不上(请求耗时过长)则重置, 避免累积滞后
                next_at = time.monotonic()
                delay = 0.0
            self._stop.wait(delay)
        log.info("route recorder stopped")

    def _tick(self) -> None:
        now = time.monotonic()
        self._rate_window.append(now)
        self._frame += 1
        # /indicators 降频(默认每 4 帧): 载具名/速度变化慢, 不必每帧请求;
        # 拿不到数据时(断开)会逐帧重试, 保证能快速恢复
        if self._frame % self.indicators_every == 1 or not self._ind_cache:
            self._ind_cache = self.client.indicators()
        ind = self._ind_cache
        connected = bool(ind)
        vehicle = str(ind.get("type") or "") if ind else ""
        speed = _to_float(ind.get("speed")) or 0.0

        # 玩家坐标: 每帧请求(轨迹平滑的关键)
        # 顺带从同一份数据里取战区 —— 不额外增加请求
        objects = self.client.map_objects()
        player = self._find_player(objects, ind)
        self._update_zones(objects)

        # 最近采样缓冲(回溯起点用); 按时间从头部淘汰, 只留 START_LOOKBACK 秒
        if player is not None:
            self._recent.append((now, player[0], player[1], speed))
            cutoff = now - START_LOOKBACK
            while self._recent and self._recent[0][0] < cutoff:
                self._recent.popleft()

        self._drain_commands(now, player, vehicle, speed)

        # 录制采样
        elapsed = 0.0
        if self._rec is not None:
            if self._armed:
                self._arm_check(speed)
            if self._rec is not None and not self._armed:
                elapsed = now - self._t0
                self._sample(player, elapsed, speed)

        self.tick.emit({
            "connected": connected,
            "in_map": player is not None,
            "vehicle": pretty_vehicle(vehicle) if vehicle else "未知载具",
            "vehicle_key": vehicle,
            "speed": speed,
            "player": player,
            "recording": self._rec is not None,
            "armed": self._armed,
            "elapsed": elapsed,
            "marks": len(self._rec.marks) - 1 if self._rec is not None else 0,
            "rate": self._current_rate(),
            "zones": self._zones,
            "spawns": self._spawns,
            "mode": self.mode_name,
        })

    def _current_rate(self) -> float:
        """最近窗口内的实际轮询频率(Hz)。"""
        if len(self._rate_window) < 2:
            return 0.0
        span = self._rate_window[-1] - self._rate_window[0]
        return (len(self._rate_window) - 1) / span if span > 0 else 0.0

    def _find_player(self, objects: list, ind: dict) -> tuple[float, float] | None:
        """从 map_obj.json 找玩家自身坐标; 兼底读 indicators 的 map_x/map_y。"""
        for obj in objects or []:
            if not isinstance(obj, dict) or not _is_player_obj(obj):
                continue
            x, y = _to_float(obj.get("x")), _to_float(obj.get("y"))
            if x is not None and y is not None:
                return (x, y)
        if ind:
            x, y = _to_float(ind.get("map_x")), _to_float(ind.get("map_y"))
            if x is not None and y is not None:
                return (x, y)
        return None

    def _update_zones(self, objects: list) -> None:
        """从地图对象里提取战区与**重生位置**; **归属/闪烁变化也要上报**。

        8111 每帧都给战区的当前归属色与 blink 标志, 但坐标不变 —— 只在“坐标变化”时
        刷新的话, 占领后图标颜色会永远停在第一次见到的样子(用户报的 bug)。
        所以: 用 `zone_state()`(字母/坐标/归属/闪烁)判断是否需要刷新;
        而落盘的“布局”仍然只按**坐标指纹**去重(归属是反复变的, 不能当新模式)。

        重生位置(`respawn_base_*`)也一样: 一局里成片出现且固定不变, 但**同一张图的
        不同版本(区域1/区域2)位置不同**, 所以跟布局一起存(见 store.remember_zones)。
        """
        zones = zones_from_objects(objects)
        spawns = spawns_from_objects(objects)
        zstate = zone_state(zones)
        sstate = spawn_state(spawns)
        if zstate == self._zones_state and sstate == self._spawns_state:
            return                       # 什么都没变 → 不刷新界面也不存盘
        self._zones = zones
        self._zones_state = zstate
        self._spawns = spawns
        self._spawns_state = sstate
        if not zones:
            self._zones_sig = ""       # 退出战局: 指纹清空, 下次进图重新上报
            return
        sig = zones_signature(zones)
        new_layout = sig != self._zones_sig
        self._zones_sig = sig
        log.info("战区已刷新: %s %s", self.mode_name,
                 ", ".join(f"{z.label}={z.side_label}{'*' if z.contested else ''}"
                           for z in zones))
        if spawns:
            log.info("重生位置已刷新: %s", spawn_summary(spawns))
        # 录制开始时可能还没拿到战区(进图瞬间) → 指纹变化时补写进当前录制
        if self._rec is not None and (new_layout or not self._rec.zone_signature):
            self._stamp_record(self._rec)
        # 新布局、或同一布局但重生位置变了 → 存一份(带重生点)
        if (new_layout or sstate != self._spawns_saved) \
                and self._map_key != PLACEHOLDER_MAP_KEY:
            try:
                self.store.remember_zones(self._map_key, zones, spawns=spawns)
                self._spawns_saved = sstate
            except Exception as exc:  # noqa: BLE001 - 存盘失败不应影响录制
                log.warning("保存战区/重生位置失败: %s", exc)

    def _stamp_record(self, rec: RouteRecord) -> None:
        """把当前战局的战区信息写进记录(模式名/数量/指纹)。"""
        rec.zone_count = len(self._zones)
        rec.zone_signature = self._zones_sig
        rec.mode_name = self.mode_name

    # ------------------------------------------------------------------ 录制
    def _drain_commands(self, now: float, player: tuple[float, float] | None,
                        vehicle: str, speed: float) -> None:
        while True:
            try:
                cmd = self._cmds.get_nowait()
            except queue.Empty:
                return
            name, arg = cmd if isinstance(cmd, tuple) else (cmd, None)
            if name == "start":
                self._do_start(now, player, vehicle, start_on_move=bool(arg))
            elif name == "mark":
                self._do_mark(player)
            elif name == "stop":
                self._do_stop(player, speed)

    def _do_start(self, now: float, player: tuple[float, float] | None, vehicle: str,
                  start_on_move: bool = False) -> None:
        if self._rec is not None:
            self.notice.emit("已在录制中, 请先结束当前录制")
            return
        if self._map_key == PLACEHOLDER_MAP_KEY:
            # 战局外 8111 只给 "?" 占位图, 此时录下的轨迹没有地图可依托
            self.notice.emit("尚未进入对局/试车场(8111 仅返回占位图), 无法开始录制")
            return
        if player is None:
            self.notice.emit("无法获取玩家位置, 请进入对局/试车场后再开始录制")
            return
        rec = self.store.new_route(
            map_key=self._map_key,
            map_name=self.store.display_name(self._map_key),
            vehicle=pretty_vehicle(vehicle),
            start_x=player[0], start_y=player[1],
        )
        self._stamp_record(rec)          # 模式 = 当前战局的战区布局(可能稍后才拿到)
        self._rec = rec
        if start_on_move:
            # 等速度不再为 0 再回溯定起点(见 _arm_check); 此时不开始计时
            self._armed = True
            self._armed_at = now
            log.info("已准备录制: 地图=%s 载具=%s, 等待载具移动", rec.map_name, rec.vehicle)
            self.start_armed.emit(rec)
            return
        self._begin_recording(now, player[0], player[1])
        log.info("开始录制: 地图=%s 载具=%s 起点=(%.4f, %.4f)",
                 rec.map_name, rec.vehicle, player[0], player[1])

    def _begin_recording(self, t0: float, sx: float, sy: float,
                         extra: list | None = None) -> None:
        """定下计时起点(位置 + 起点标记)并发出 recording_started。

        t0 为单调时钟时刻: 常规模式为按下的那一刻,
        “以速度从零变化时刻为起点”模式为**回溯出的速度为 0 的最后时刻**。
        """
        rec = self._rec
        assert rec is not None
        self._t0 = t0
        self._armed = False
        rec.start_x, rec.start_y = sx, sy
        for mk in rec.marks:     # new_route 已插入起点标记, 这里把它挪到回溯出的位置
            if mk.kind == "start":
                mk.x, mk.y, mk.t = sx, sy, 0.0
                break
        self._last_pt_t = 0.0
        self._last_xy = (sx, sy)
        for pt in extra or []:   # 起点之后已有的位移点(最多回溯窗口内的几帧)
            rec.points.append(pt)
            self._last_pt_t = pt.t
            self._last_xy = (pt.x, pt.y)
        self.recording_started.emit(rec)

    def _arm_check(self, speed: float) -> None:
        """等待载具移动: 速度为 0 时继续等; 首次不为 0 时回溯计时起点。

        起点取**最近一次速度为 0 的时刻**。例如“点录制 → 切回游戏 → 开始走”:
        等待期间采到的都是 0, 最后一个 0 就在真正起步的上一帧,
        因此计时起点≈实际起步时刻, 不会把切窗口的延迟算进去。
        """
        if speed <= 0.0:
            return
        rec = self._rec
        if rec is None:
            self._armed = False
            return
        samples = list(self._recent)
        origin_i = -1
        for i in range(len(samples) - 1, -1, -1):
            if samples[i][3] <= 0.0:
                origin_i = i
                break
        if origin_i < 0:
            # 回溯窗口内一直是运动中(例如点击时车已在行驶) → 退化为“按下的那一刻”
            t0 = self._armed_at
            sx, sy = (samples[0][1], samples[0][2]) if samples else (rec.start_x, rec.start_y)
            self.notice.emit("回溯窗口内没有速度为 0 的时刻, 已按开始录制的时刻计时")
            log.info("回溯起点失败(一直运动中), 改用按下时刻")
            extra: list[RoutePoint] = []
        else:
            t0, sx, sy, _v = samples[origin_i]      # 元组顺序: (时刻, x, y, 速度)
            extra = [RoutePoint(x=s[1], y=s[2], t=s[0] - t0, speed=s[3])
                     for s in samples[origin_i + 1:]]
            log.info("回溯起点: 速度非 0(%.0f km/h) → 起点位于 %.2fs 前",
                     speed, self._armed_at - t0)
        rec.duration = 0.0
        self._begin_recording(t0, sx, sy, extra)

    def _do_mark(self, player: tuple[float, float] | None) -> None:
        if self._rec is None:
            self.notice.emit("请先开始录制轨迹, 再标记当前位置")
            return
        if self._armed:
            self.notice.emit("还在等待载具移动(尚未定下计时起点), 无法标记")
            return
        if player is None:
            self.notice.emit("无法获取玩家位置, 标记失败")
            return
        t = time.monotonic() - self._t0
        mk = RouteMark(x=player[0], y=player[1], t=t, kind="mark")
        self._rec.marks.append(mk)
        log.info("标记 #%d @%.2fs", len(self._rec.marks) - 1, t)
        self.mark_added.emit(mk)

    def _do_stop(self, player: tuple[float, float] | None, speed: float) -> None:
        if self._rec is None:
            self.notice.emit("当前没有正在录制的轨迹")
            return
        if self._armed:
            # 等待移动期间结束 → 当作取消: 全程速度为 0, 没有任何可回放的内容
            self._armed = False
            self._rec = None
            log.info("录制已取消(等待移动期间结束)")
            self.recording_cancelled.emit()
            self.notice.emit("已取消: 未检测到载具移动(全程速度为 0)")
            return
        rec = self._rec
        rec.duration = time.monotonic() - self._t0
        if player is not None:
            rec.points.append(RoutePoint(x=player[0], y=player[1],
                                         t=rec.duration, speed=speed))
            rec.marks.append(RouteMark(x=player[0], y=player[1],
                                       t=rec.duration, kind="stop"))
        else:
            last = rec.points[-1] if rec.points else None
            x = last.x if last else rec.start_x
            y = last.y if last else rec.start_y
            rec.marks.append(RouteMark(x=x, y=y, t=rec.duration, kind="stop"))
        self._rec = None
        self.store.add(rec)
        log.info("结束录制: 用时 %.2fs, 轨迹点 %d, 标记 %d",
                 rec.duration, len(rec.points), len(rec.marks))
        self.recording_finished.emit(rec)

    def _sample(self, player: tuple[float, float] | None,
                elapsed: float, speed: float) -> None:
        """按时间间隔 + 最小位移抽稀, 记录轨迹点与速度。"""
        rec = self._rec
        if rec is None or player is None:
            return
        if elapsed - self._last_pt_t < self.sample_interval:
            return
        if self._last_xy is not None:
            dist = math.hypot(player[0] - self._last_xy[0], player[1] - self._last_xy[1])
            if dist < self.min_move:
                return
        rec.points.append(RoutePoint(x=player[0], y=player[1], t=elapsed, speed=speed))
        self._last_pt_t = elapsed
        self._last_xy = player
