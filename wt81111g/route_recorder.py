"""跑图录制状态机 + 8111 轮询(后台线程)。

数据来源:
- 载具型号 / 速度 : GET /indicators  (type / speed)
- 玩家坐标/朝向   : GET /map_obj.json (玩家对象的 x/y 归一化坐标 + dx/dy 单位方向向量)
- 战区坐标/模式   : GET /map_obj.json (type == "capture_zone", 见 zones.py)
- 地图图片        : GET /map.img      (节流请求, 内容变化才上报)

录制命令(开始/标记/结束)由 GUI 线程投递到队列, 在轮询线程内消费,
保证录制状态只被单一线程修改, 无需复杂加锁。

两类录制(互斥):

* **单条线路**(`start`/`mark`/`stop`): 只录玩家自己, 起点/标记/终点 + 朝向;
* **全局录制**(`global_start`/`global_stop`): 录**全场所有玩家**的线路,
  A / B 两队各录一次(回放只给观战那一方的单位)。逻辑在 `global_record.py`,
  这里只负责“什么时候开始/结束”:
  - 点下按钮时通常还在**等待部署**, 8111 没有任何玩家数据 → 先挂着,
    直到**第一次出现可录单位**才算 0 点;
  - 到 demo 末尾回到车库时自动停止(8111 换成占位图 / 全场单位消失);
  - 期间**锁定地面底图**, 不会因为有人上飞机而切成空战地图。
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
from .global_record import DEFAULT_MAP_SIZE, GlobalTracker, extract_units
from .map_cache import MapCache
from .route_store import (
    GLOBAL_SIDES, PLACEHOLDER_MAP_KEY, PLACEHOLDER_MAP_SHA1, GlobalRecord, RouteMark,
    RoutePoint, RouteRecord, RouteStore, pretty_vehicle,
)
from .zones import Zone, mode_name as zone_mode_name, signature as zones_signature
from .zones import (Spawn, spawn_state, spawn_summary, spawns_from_objects, zone_state,
                    zones_from_objects, zones_to_json)

log = logging.getLogger("route_recorder")

# 全局录制里战区状态时间轴的条数上限(正常一局只有几十条; 异常时也不写爆数据)
MAX_ZONE_STATES = 4000

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


def _player_dir(obj: dict) -> tuple[float, float] | None:
    """玩家车头朝向: map_obj.json 里的 dx/dy 单位方向向量(**与 /map.img 同坐标系**)。

    拿不到或全零时返回 None —— 那种情况只能画点, 画不出朝向。
    """
    dx, dy = _to_float(obj.get("dx")), _to_float(obj.get("dy"))
    if dx is None or dy is None or (dx == 0.0 and dy == 0.0):
        return None
    return (dx, dy)


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
    # ---- 全局录制(整局所有玩家的线路, 分 A/B 两队)
    global_armed = pyqtSignal(object, str)     # (GlobalRecord, side) 已建条目, 等第一名玩家重生
    global_started = pyqtSignal(object, str)   # (GlobalRecord, side) 第一名玩家出现, 已定下 0 点
    global_finished = pyqtSignal(object, str)  # (GlobalRecord, side)
    global_cancelled = pyqtSignal()            # 一条线都没录到

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
        # 最近采样环形缓冲(时间, x, y, 速度, 朝向): 用于回溯“最后一个速度为 0 的时刻”
        self._recent: deque[tuple[float, float, float, float, tuple | None]] = deque(maxlen=400)
        # 当前车头朝向(归一化单位方向向量); None = 本帧没拿到
        self._dir: tuple[float, float] | None = None

        # ---- 全局录制状态(同样只由轮询线程修改)
        self._global: GlobalRecord | None = None   # 正在录的条目
        self._global_side = ""                      # "A" / "B"
        self._global_created = False                # 条目是不是这次新建的(取消时要删掉)
        self._global_prev_lives: list = []           # 重录前的旧线路(取消时恢复)
        self._tracker: GlobalTracker | None = None
        self._global_waiting = False                # 还没等到第一名玩家重生
        self._global_t0 = 0.0
        self._map_lock = False                      # 全局录制期间锁定地面底图
        # 地图实际边长(米, 来自 /map_info.json): 归一化位移 × 边长 → 速度(km/h)
        self._map_size: tuple[float, float] = DEFAULT_MAP_SIZE
        # 这一帧 /map_info.json 报的图框尺寸(**总是**记下来, 包括“当前坐标系与参考不同、
        # 不采用”的情况) —— 全局录制的坐标系拟合尺度要拿它交叉校验
        self._map_size_seen: tuple[float, float] | None = None
        self._map_info_at = 0.0
        # 速度标定(只写日志): (单调时刻, x, y) 与上次记录的时机
        self._cal_prev: tuple[float, float, float] | None = None
        self._cal_at = 0.0

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
        # 全局录制: 已记进时间轴的战区状态(字母/坐标/归属/是否被占)
        self._zone_state_key: tuple | None = None
        # 最近 2 秒内的**实际采样时刻**: 叠加层要显示“每秒真的写入了几个轨迹点”
        # (它和 8111 轮询频率不是一回事 —— 采样还要过“采样间隔 + 最小位移”两道闸)
        self._pt_times: deque[float] = deque()

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
                    if self._map_lock:
                        # 全局录制期间**锁定地面底图**: 玩家上飞机时 8111 会发空战地图,
                        # 那种图的坐标系和地面图完全不是一回事, 绝不能跟着换。
                        # 只有回到车库(占位图)才算“本局结束”, 顺便自动停止录制。
                        if digest == PLACEHOLDER_MAP_SHA1:
                            log.info("全局录制: 8111 回到占位图(疑已退回车库), 自动停止")
                            self._cmds.put("global_stop")
                        else:
                            log.info("全局录制中地图图片变化(很可能是空战地图), 已忽略")
                        continue
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

    def request_global_start(self, side: str, target: GlobalRecord | None = None) -> None:
        """请求开始全局录制。

        `target` 为侧栏里被标记过的条目(要追加/重录到它), None = 新建一个条目。
        """
        self._cmds.put(("global_start", (str(side), target)))

    def request_global_stop(self) -> None:
        self._cmds.put("global_stop")

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
    def global_recording(self) -> bool:
        """是否正在(或正准备)做全局录制。"""
        return self._global is not None

    @property
    def global_waiting(self) -> bool:
        """全局录制已开始但还在等第一名玩家重生(此时 8111 没有玩家数据)。"""
        return self._global is not None and self._global_waiting

    @property
    def global_side(self) -> str:
        return self._global_side

    @property
    def global_record(self) -> GlobalRecord | None:
        return self._global

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
        player_obj = self._find_player_obj(objects)
        player = self._find_player(objects, ind)
        # 车头朝向(给地图上的 ^ 用, 也随轨迹点存下来供回放); 与坐标来自同一条玩家对象
        direction = _player_dir(player_obj) if player_obj is not None else None
        self._dir = direction
        self._update_zones(objects)

        # 全局录制: 顺手标定“位移推算的速度”和 8111 直报的速度(只写日志)
        if self._global is not None and player is not None:
            self._calibrate(now, player, _to_float(ind.get("speed")) if ind else None)

        # 最近采样缓冲(回溯起点用); 按时间从头部淘汰, 只留 START_LOOKBACK 秒
        if player is not None:
            self._recent.append((now, player[0], player[1], speed, direction))
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

        # 全局录制: 每帧把**全场**的单位喂给跟踪器(与单条录制互斥)
        global_elapsed = 0.0
        if self._global is not None:
            # 地图实际边长(算速度用)不必每帧取
            if now - self._map_info_at >= 2.0:
                self._map_info_at = now
                self._update_map_size()
            global_elapsed = self._global_feed(now, objects)

        self.tick.emit({
            "connected": connected,
            "in_map": player is not None,
            "vehicle": pretty_vehicle(vehicle) if vehicle else "未知载具",
            "vehicle_key": vehicle,
            "speed": speed,
            "player": player,
            "dir": direction,          # 车头朝向(归一化单位方向向量) 或 None
            "recording": self._rec is not None,
            "armed": self._armed,
            "elapsed": elapsed,
            "marks": len(self._rec.marks) - 1 if self._rec is not None else 0,
            "rate": self._current_rate(),
            "points": len(self._rec.points) if self._rec is not None else 0,
            "point_rate": self.point_rate(),
            "zones": self._zones,
            "spawns": self._spawns,
            "mode": self.mode_name,
            # 全局录制状态(None = 没在录)
            "global": ({
                "side": self._global_side,
                "waiting": self._global_waiting,
                "elapsed": global_elapsed,
                "lives": self._tracker.total if self._tracker else 0,
                "active": self._tracker.active if self._tracker else 0,
            } if self._global is not None else None),
            # 实时线路(录制期间给地图“边录边画”用; 列表是快照, 元素引用共享)
            "global_lives": (list(self._tracker.lives) if self._tracker else None),
        })

    def _current_rate(self) -> float:
        """最近窗口内的实际轮询频率(Hz)。"""
        if len(self._rate_window) < 2:
            return 0.0
        span = self._rate_window[-1] - self._rate_window[0]
        return (len(self._rate_window) - 1) / span if span > 0 else 0.0

    def _find_player_obj(self, objects: list) -> dict | None:
        """map_obj.json 里代表玩家自己的那条对象(坐标不全的不算)。"""
        for obj in objects or []:
            if not isinstance(obj, dict) or not _is_player_obj(obj):
                continue
            if _to_float(obj.get("x")) is not None and _to_float(obj.get("y")) is not None:
                return obj
        return None

    def _find_player(self, objects: list, ind: dict) -> tuple[float, float] | None:
        """从 map_obj.json 找玩家自身坐标; 兼底读 indicators 的 map_x/map_y。"""
        obj = self._find_player_obj(objects)
        if obj is not None:
            return (_to_float(obj.get("x")), _to_float(obj.get("y")))
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
            elif name == "global_start":
                side, target = arg
                self._do_global_start(side, target)
            elif name == "global_stop":
                self._do_global_stop()

    def _calibrate(self, now: float, player: tuple[float, float],
                   speed_kmh: float | None) -> None:
        """把“帧间位移推算的速度”与 `/indicators` 直报的速度放在一起记一行日志。

        **为什么需要它**: `/indicators` 只给**摄像机所在载具**的速度(而且无人机/空战视角
        直接是 `valid:false`), 其它玩家的速度只能用“归一化位移 × 地图边长(米)”算;
        而边长取自 `/map_info.json` 的 `map_max - map_min`。下面这一行就是验证那个尺度:
        两个数字接近 → 换算正确; 差一个固定倍数 → 说明边长取错了(改 `_map_size` 即可)。

        2 秒取一次位移、最多 5 秒记一行, 不给日志添负担。
        """
        prev = self._cal_prev
        if prev is None or (now - prev[0]) < 2.0:
            return
        self._cal_prev = (now, player[0], player[1])
        dt = now - prev[0]
        if dt <= 0.0 or speed_kmh is None or (now - self._cal_at) < 5.0:
            return
        self._cal_at = now
        w, h = self._map_size
        dist_m = math.hypot((player[0] - prev[1]) * w, (player[1] - prev[2]) * h)
        log.info("速度标定: 8111=%.1f km/h | 位移推算=%.1f km/h | 地图=%.0f×%.0f m",
                 speed_kmh, dist_m / dt * 3.6, w, h)

    # ------------------------------------------------------------ 全局录制
    def _update_map_size(self) -> None:
        """从 `/map_info.json` 取地图实际边长(米)。

        `map_min` / `map_max` 是**世界坐标(米)**(官方例子是 ±32768; 实测陆战图
        `0..4096`)。归一化坐标 × 边长 = 实际距离, 才能把帧间位移换算成 km/h。
        拿不到或数值不合法时保持上一次的值(首次为 4096 m)。
        """
        info = self.client.map_info()
        try:
            lo = [float(v) for v in (info.get("map_min") or [])]
            hi = [float(v) for v in (info.get("map_max") or [])]
        except (TypeError, ValueError):
            return
        if len(lo) < 2 or len(hi) < 2:
            return
        w, h = hi[0] - lo[0], hi[1] - lo[1]
        if w <= 0 or h <= 0:
            return
        tracker = self._tracker
        self._map_size_seen = (w, h)
        if tracker is not None:
            tracker.seen_extent = (w, h)      # 仅用于日志里的交叉校验
            fit = tracker.aligner.fit
            if fit is not None and not fit.same_as_reference:
                # 观战中的玩家上了飞机: 8111 切成空战图, 连 map_min/map_max 都变了。
                # 但单位坐标已经被换算回**地面坐标系**(见 FrameAligner), 所以速度必须
                # 继续按地面图的边长算 —— 这里直接不动。
                log.debug("全局录制: 当前是换算过的坐标系, 忽略 map_info 的边长更新")
                return
        self._map_size = (w, h)
        if tracker is not None:
            tracker.set_map_size(w, h)

    def _do_global_start(self, side: str, target: GlobalRecord | None) -> None:
        """开始全局录制。

        用户点击时正处在**开局等待部署**阶段(8111 还没有任何玩家数据),
        所以这里只是建条目 + 进入“等待第一名玩家重生”, 真正的 0 点由
        `_global_feed` 在第一次看到可录单位时定下。

        * `target` 为 None → 新建条目(名字用当前时间), 并在条目上打上该队标记;
        * `target` 非 None → 追加/重录到已有条目(调用方已确认过“重录要清空”);
          旧线路**先留着**, 直到新录制成功才整体替换, 中途取消不会丢数据。
        """
        if self._global is not None:
            self.notice.emit("已在全局录制中, 请先停止当前录制")
            return
        if self._rec is not None:
            self.notice.emit("正在录制单条轨迹, 请先结束当前录制")
            return
        if self._map_key == PLACEHOLDER_MAP_KEY:
            self.notice.emit("尚未进入对局/回放(8111 仅返回占位图), 无法开始全局录制")
            return
        created = target is None
        if created:
            rec = self.store.new_global(
                map_key=self._map_key,
                map_name=self.store.display_name(self._map_key),
                zone_count=len(self._zones),
                zone_signature=self._zones_sig,
                mode_name=self.mode_name,
            )
        else:
            rec = target
            if not rec.zone_signature:      # 条目建立时可能还没拿到战区
                rec.zone_signature = self._zones_sig
                rec.zone_count = len(self._zones)
                rec.mode_name = self.mode_name
        if side not in rec.marks:
            rec.marks.append(side)
            rec.marks.sort(key=GLOBAL_SIDES.index)
        self._global = rec
        self._global_side = side
        self._global_created = created
        self._global_prev_lives = list(rec.lives(side))
        self._tracker = GlobalTracker(rec, side, map_size=self._map_size)
        self._global_waiting = True
        self._global_t0 = 0.0
        self._zone_state_key = None      # 新录制 → 时间轴从头记(重录时清空旧时间轴)
        if created:
            rec.zone_states = []
        # 锁定地面底图: 录制期间玩家可能上飞机, 8111 会改发空战地图
        self._map_lock = True
        self._map_info_at = 0.0          # 下一次 tick 立刻取一次地图边长
        if created:
            self.store.add_global(rec)
        else:
            self.store.save()
        log.info("全局录制已就绪: %s %s 队 (条目 %s)", rec.label, side, rec.route_id)
        self.global_armed.emit(rec, side)
        self.notice.emit(
            f"已就绪: 等待第一名玩家重生后开始计时({side} 队) —— "
            f"回放请保持 1× 且不要暂停, 否则 A/B 时间轴会对不上")

    def _global_feed(self, now: float, objects: list) -> float:
        """每帧喂给跟踪器; 返回已录制的秒数(还没开始时为 0)。

        0 点 = **第一次看到可录单位**的时刻(用户要求: 等待部署期间不算时间)。
        """
        rec = self._global
        tracker = self._tracker
        if rec is None or tracker is None:
            return 0.0
        if self._global_waiting:
            units, _skipped = extract_units(objects)
            if not units:
                return 0.0            # 还在等待部署: 8111 没有任何玩家数据
            self._global_waiting = False
            self._global_t0 = now
            log.info("全局录制开始计时: %s %s 队", rec.label, self._global_side)
            self.global_started.emit(rec, self._global_side)
        elapsed = now - self._global_t0
        if tracker.feed(elapsed, objects):
            log.info("全局录制: 全场已无可录单位超过阈值, 自动停止")
            self._do_global_stop()
            return max(0.0, elapsed)
        rec.duration = elapsed
        self._record_zone_state(rec, elapsed)
        # 点录制时可能还没拿到战区(进图瞬间) → 一旦知道就把模式/布局补进条目,
        # 否则它会被归到“未知位置”那个版本组里
        if not rec.zone_signature and self._zones_sig:
            rec.zone_signature = self._zones_sig
            rec.zone_count = len(self._zones)
            rec.mode_name = self.mode_name
        return elapsed

    def _record_zone_state(self, rec, elapsed: float) -> None:
        """把战区状态的**变化**记进 `rec.zone_states` 时间轴(回放用)。

        用户 2026-10-22 要求: 回放 AB 队录像时战区要能重现当时的归属(蓝/红)
        与“正在被占领”, 而不是一张与录像无关的静态布局。8111 每帧都会给归属色,
        但直接每帧存一条会让 routes.json 膨胀得没法看, 所以只在**下面三件事之一
        变了**才记一条:

        * 归属变了(我方 ↔ 敌方 ↔ 中立);
        * 开始/结束被占领(`blink > 0` 的布尔位变了);
        * 战区集合本身变了(换布局、数量变)。

        `blink` 在 1 / 2 之间跳变属于“同一个占领过程”, 不当成变化 —— 否则一个
        占领会造成每秒好几条记录。回放时取 `<= playhead` 的最后一条, 于是整段
        占领期间都保持脉冲动画, 与实际观感一致。
        """
        zones = self._zones
        if not zones:
            return
        key = tuple((z.label, round(z.x, 5), round(z.y, 5), z.side,
                     1 if z.blink else 0) for z in zones)
        if key == self._zone_state_key:
            return
        self._zone_state_key = key
        # 兜底上限: 正常一局只有几十条; 真遇到异常(比如坐标系抖动)也不要写爆
        if len(rec.zone_states) >= MAX_ZONE_STATES:
            return
        rec.zone_states.append({
            "t": round(elapsed, 2),
            "zones": zones_to_json(zones),
        })

    def _do_global_stop(self) -> None:
        """结束全局录制并落盘。一条线都没录到 → 撤销(新建的删掉/重录的恢复原状)。"""
        rec = self._global
        if rec is None:
            self.notice.emit("当前没有正在进行的全局录制")
            return
        side = self._global_side
        lives = self._tracker.finish() if self._tracker is not None else []
        skipped = dict(self._tracker.seen_skipped) if self._tracker is not None else {}
        duration = max(0.0, rec.duration or 0.0)
        self._global = None
        self._tracker = None
        self._global_waiting = False
        self._zone_state_key = None
        self._map_lock = False
        self._map_digest = None          # 解锁后重新判断一次当前地图(可能已回到车库)
        if skipped:
            top = sorted(skipped.items(), key=lambda kv: -kv[1])[:8]
            log.info("全局录制: 未录制(%s) 共 %d 种, 数量前 8: %s",
                     "非地面单位/静态对象", len(skipped),
                     ", ".join(f"{t}/{i or '-'}×{n}" for (t, i), n in top))
        if not lives:
            if self._global_created:
                self.store.remove_global(rec.route_id)
            else:
                rec.sides[side] = self._global_prev_lives
                self.store.save()
            self._global_prev_lives = []
            log.info("全局录制已取消(没有录到任何线路)")
            self.global_cancelled.emit()
            self.notice.emit("已取消全局录制: 没有录到任何线路")
            return
        rec.sides[side] = lives          # 重录 = 整队替换
        rec.duration = duration
        self.store.save()
        self._global_prev_lives = []
        log.info("全局录制完成: %s %s 队 %d 条线路, 用时 %.1fs",
                 rec.label, side, len(lives), duration)
        # 两队都录过时, 顺手把时间轴对比写进日志(见 GlobalRecord.timeline_note)
        log.info("全局录制 时间轴: %s", rec.timeline_note())
        self.global_finished.emit(rec, side)

    def _do_start(self, now: float, player: tuple[float, float] | None, vehicle: str,
                  start_on_move: bool = False) -> None:
        if self._global is not None:
            self.notice.emit("正在全局录制中, 请先停止全局录制")
            return
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
        self._pt_times.clear()          # 新一次录制 → 点数率窗口从头算
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
            t0, sx, sy, _v, d0 = samples[origin_i]   # 元组顺序: (时刻, x, y, 速度, 朝向)
            extra = [RoutePoint(x=s[1], y=s[2], t=s[0] - t0, speed=s[3],
                                dx=(s[4] or (0.0, 0.0))[0], dy=(s[4] or (0.0, 0.0))[1])
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
            dx, dy = self._dir_xy()
            rec.points.append(RoutePoint(x=player[0], y=player[1],
                                         t=rec.duration, speed=speed, dx=dx, dy=dy))
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
        dx, dy = self._dir_xy()
        rec.points.append(RoutePoint(x=player[0], y=player[1], t=elapsed, speed=speed,
                                     dx=dx, dy=dy))
        self._last_pt_t = elapsed
        self._last_xy = player
        # 维护“实际采样时刻”窗口(只留最近 2 秒), 供 `point_rate()` 算真实点数率
        self._pt_times.append(elapsed)
        cutoff = elapsed - 2.0
        while self._pt_times and self._pt_times[0] < cutoff:
            self._pt_times.popleft()

    def point_rate(self) -> float:
        """最近 2 秒**真的写入了多少个轨迹点**(点/秒)。

        与 `_current_rate()`(8111 轮询频率)不同: 采样还要过“采样间隔 + 最小位移”
        两道闸, 所以实际点数率会**低于**轮询频率 —— 低速或停着不动时尤其明显
        (车不动 → 位移不够 → 一个点也不写)。叠加层把它和轮询 Hz 并排显示,
        就是为了让人一眼看出“8111 在给数据, 但轨迹没在长”。
        """
        q = self._pt_times
        if len(q) < 2:
            return 0.0
        span = q[-1] - q[0]
        return (len(q) - 1) / span if span > 0 else 0.0

    def _dir_xy(self) -> tuple[float, float]:
        """当前车头朝向的 (dx, dy); 没拿到时为 (0, 0)(= 不画朝向箭头)。"""
        d = self._dir
        return d if d is not None else (0.0, 0.0)
