"""全局录制: 从每帧 `/map_obj.json` 拼出“每辆车的线路”(多目标跟踪)。

**为什么需要跟踪**: 8111 的地图对象里**没有 id、没有玩家名**, 只有
`type`(如 `ground_model`) / `icon`(如 `MediumTank`) / `color` / `x`,`y`。
所以“同一个单位”只能靠**帧间最近邻 + 贪心匹配**推测:

* 每个已存在的线路, 在同一 `type` 的单位里找**距离最近**的那个接上;
* 没接上的单位 → 新开一条线(重生 / 刚从视野外进来);
* 一条线连续 `END_GAP` 秒都没接上 → 结束(死亡 / 换车 / 上飞机)。

**怎么判定“被击毁”** (2026-10-21 用户口径): 不关心是哪个玩家、也不关心是哪条线 ——
只看 8111: 这条线对应的 icon **突然消失**、而那一刻场上**还有其它可录单位**,
就算它被击毁(记为 `UnitLife.dead`)。反过来, **全场一起消失**(回到车库/录制自动结束)
不算 —— 用户特意提过 “pro 能做到整局一次不死”, 那种情况不能画击毁标记。

**已知取舍**(与用户确认过):
* 同一个人中途上飞机/直升机 → 那条线断开, 落地/再重生后算**新的一条线**;
* 两个人贴得极近时可能互换身份(位置本来就分不出来), 概率很低, 且不影响“线路形状”;
* 不记朝向、不记速度(用户要求: 全局录制忽略朝向), 只记 `(t, x, y)`。

本模块**不依赖 Qt**, 便于单元测试; 8111 轮询与信号在 `route_recorder.py` 里。

关于“哪些单位要录”(用户口径):
* **地面载具全部录**(含 AI 地面目标);
* **空中单位(飞机/直升机)一律不录** —— 我们关心的是地面情况。

**为什么连侦察无人机也一起跳过**(2026-10-21 实测, 见 `docs/8111使用经验.md` §7.9.1):
在"释放无人机的车 + 无人机"同时在场的时刻抓到的两条对象是

    {"type":"aircraft",    "icon":"Fighter", "color":"#39D921", x/y ≈ 车的坐标}
    {"type":"ground_model","icon":"IFV",     "color":"#67D756", ...}

即 **侦察无人机的 `icon` 就是 `"Fighter"`, 与真正的固定翼一字不差**,
而且没有高度/id/名字 —— 只能用 `type == "aircraft"` 把它们归成一类,
**再没有任何字段能把无人机和战斗机分开**。既然分不开, 就整体不录(否则会把固定翼也画进来);
无人机本身也贴着释放者飞, 画出来只是一个小抖动, 信息量很低。

`DRONE_HINTS` 保留成一个注释级的“备选口”: 万一以后某个版本给无人机单独起了 icon 名字,
把 `recordable()` 里 `if is_air(typ): return False` 改成 `return is_drone(typ, icon)` 即可。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from .route_store import GlobalRecord, TrackPoint, UnitLife

log = logging.getLogger("global_record")

# 采样间隔(秒): 5Hz。全局录制只是“看路线怎么走”, 不需要单条录制那种 25~50Hz。
# 一局 20 分钟 × 30 辆车 × 5Hz ≈ 18 万个点 —— 再密的话 routes.json 会很大。
SAMPLE_INTERVAL = 0.2
# 帧间最近邻匹配半径(归一化坐标, 地图 = 1.0)。
# 25Hz 轮询下坦克单帧位移 < 0.001, 0.004 已经很宽松;
# 放太大反而会把“两辆并排的车”接错。
MATCH_RADIUS = 0.004
# 一条线连续多久没匹配上就算结束(秒)。要大于“短暂丢帧/闪烁”的时间
END_GAP = 1.0
# 全场一个可录单位都没有、持续多久 → 认为已经回到车库, 自动结束录制(秒)
EMPTY_STOP = 3.0
# 只在移动超过这个距离时才存点(归一化): 静止的浮点抖动不该进文件,
# 也让“一直停着的车”不会产生成千上万个点。但每条线**至少保留首尾两个点**。
MIN_MOVE = 0.0002
# 心跳: 即使一动不动, 每隔这么久也补一个点 —— 保证“停了 5 分钟”这种事实留得下来
HEARTBEAT = 15.0
# 速度上限(km/h): 帧间“跳一下”(刚被匹配上但其实是另一个单位)不该变成尖峰
MAX_SPEED_KMH = 200.0
# 拿不到 /map_info.json 时的地图边长(米)。实测陆战图 map_max-map_min = 4096。
DEFAULT_MAP_SIZE = (4096.0, 4096.0)

# 8111 的“重生保护”颜色(深蓝 `#1839A7`)—— **不再参与任何逻辑**:
# 用户 2026-10-21 决定忽略出生保护(它只是个显示状态); 而且重生时玩家会消失好几秒,
# “消失即结束”本来就会把重生前后拆成两条线, 不需要再靠颜色判断。

# 能录线路的单位类型(白名单 —— 静态地物/AI 防空/机场/航母等一概不碰)
GROUND_TYPES = frozenset({
    "ground_model", "tank", "ifv", "spg", "spaa", "armored_car", "truck",
})
AIR_TYPES = frozenset({
    "aircraft", "helicopter", "heli", "plane", "uav",
})
# 空中类型里“微型侦察无人机”的关键字 —— **目前故意不用**(见 recordable):
# 实测(2026-10-21)无人机的 icon 就是 `Fighter`, 与固定翼一字不差,
# 所以这些关键字永远命中不了任何东西。留着是给将来用的:
# 万一某版本给无人机单独起了 icon 名字, 把 recordable 里那行改回去即可。
DRONE_HINTS = ("uav", "drone", "scout", "recon")


def _norm_type(obj: dict) -> str:
    return str(obj.get("type") or "").strip()


def is_ground(typ: str) -> bool:
    return typ.lower() in GROUND_TYPES


def is_air(typ: str) -> bool:
    return typ.lower() in AIR_TYPES


def is_drone(typ: str, icon: str) -> bool:
    """`type`/`icon` 文字里带 uav/drone/scout/recon 字样?

    只在“将来能把无人机单独认出来”时才有用 —— 当前 `recordable()` **不看它**。
    """
    text = f"{typ} {icon}".lower()
    return any(h in text for h in DRONE_HINTS)


def recordable(typ: str, icon: str) -> bool:
    """这个对象要不要录线路(见模块 docstring 的“用户口径”)。

    **空中单位一律不录**(用户 2026-10-21 决定): 8111 里侦察无人机的 `icon` 就是
    `Fighter`, 与固定翼分不开, 硬录会把飞机/直升机也画进来。
    若将来能单独认出无人机, 把 `if is_air(typ):` 那行改成
    `return is_drone(typ, icon)` 即可, 其余代码不用动。
    """
    if is_air(typ):
        return False
    return is_ground(typ)


def is_protected(color: str) -> bool:
    """这一帧是否处于重生保护(深蓝)。

    **当前不参与任何逻辑** —— 用户决定忽略出生保护。保留成品函数只是为了将来要用时
    不必重新实测色值(2026-10-21 实测: 深蓝 `#1839A7`, 约 20s, 移动会缩短)。
    """
    return str(color or "").strip().lower() == "#1839a7"


def _f(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Unit:
    """帧里的一个可录单位(只有 8111 给的那点信息)。"""
    typ: str
    icon: str
    color: str
    x: float
    y: float


def extract_units(objects) -> tuple[list[Unit], dict[tuple[str, str], int]]:
    """从 `/map_obj.json` 里挑出可录单位; 同时统计“见过但没录”的 type/icon 组合。

    坐标必须落在 0~1: 空中/无人机视角会带出机场附近 10 个 AI 防空
    (`Airdefence`, 坐标 2.9~3.4), 那种东西既不是玩家也会画到图外。
    """
    units: list[Unit] = []
    skipped: dict[tuple[str, str], int] = {}
    for obj in objects or []:
        if not isinstance(obj, dict):
            continue
        typ = _norm_type(obj)
        if not typ:
            continue
        icon = str(obj.get("icon") or "").strip()
        if not recordable(typ, icon):
            key = (typ, icon)
            skipped[key] = skipped.get(key, 0) + 1
            continue
        x, y = _f(obj.get("x")), _f(obj.get("y"))
        if x is None or y is None or not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            key = (typ, icon or "-")
            skipped[key] = skipped.get(key, 0) + 1
            continue
        units.append(Unit(typ=typ, icon=icon, color=str(obj.get("color") or ""),
                          x=x, y=y))
    return units, skipped


@dataclass
class _Live:
    """正在跟踪的一条线(内部用)。"""
    life: UnitLife
    x: float
    y: float
    last_seen: float
    last_store: float
    last_x: float
    last_y: float


class GlobalTracker:
    """把逐帧的单位列表拼成 `UnitLife`。**单线程使用**(只在轮询线程里跑)。"""

    def __init__(self, record: GlobalRecord, side: str,
                 interval: float = SAMPLE_INTERVAL,
                 match_radius: float = MATCH_RADIUS,
                 end_gap: float = END_GAP,
                 map_size: tuple[float, float] = DEFAULT_MAP_SIZE):
        self.record = record
        self.side = side
        self.interval = max(0.02, float(interval))
        self.match_radius = max(0.0, float(match_radius))
        self.end_gap = max(0.1, float(end_gap))
        # 地图实际边长(米): 归一化位移 × 边长 = 实际距离 → 速度(km/h)。
        # 来自 8111 的 /map_info.json(map_max - map_min), 拿不到就用默认值。
        self.map_size = (float(map_size[0]), float(map_size[1]))
        self.lives: list[UnitLife] = []
        self._live: list[_Live] = []
        self._seq = 0
        self._empty_since: float | None = None
        self.seen_skipped: dict[tuple[str, str], int] = {}

    def set_map_size(self, width_m: float, height_m: float) -> None:
        """更新地图边长(米)。轮询线程每次拿到 /map_info 都会调一次。"""
        self.map_size = (max(1.0, float(width_m)), max(1.0, float(height_m)))

    def _speed(self, x0: float, y0: float, x1: float, y1: float, dt: float) -> float:
        """两点之间的速度(km/h)。dt<=0 时返回 0。"""
        if dt <= 0.0:
            return 0.0
        w, h = self.map_size
        dist_m = math.hypot((x1 - x0) * w, (y1 - y0) * h)
        # 上限 200 km/h: 帧间“跳一下”(例如刚被匹配上但其实是另一个单位)不该变成尖峰
        return min(MAX_SPEED_KMH, dist_m / dt * 3.6)

    # ------------------------------------------------------------------ 对外
    @property
    def active(self) -> int:
        """当前还在跟踪的线(未结束)条数。"""
        return len(self._live)

    @property
    def total(self) -> int:
        """已记录的线(含已结束的)。"""
        return len(self.lives)

    def feed(self, t: float, objects) -> bool:
        """喂一帧数据。返回 True 表示“全场已经没有可录单位够久了” → 该停止录制。"""
        units, skipped = extract_units(objects)
        for key, n in skipped.items():
            self.seen_skipped[key] = self.seen_skipped.get(key, 0) + n
        self._match(t, units)
        # 这一帧还有可录单位 → 此时断掉的线是“被击毁”; 全场空 → 是回到车库/录制结束
        self._expire(t, alive=bool(units))
        if not units:
            if self._empty_since is None:
                self._empty_since = t
            return (t - self._empty_since) >= EMPTY_STOP
        self._empty_since = None
        return False

    def finish(self) -> list[UnitLife]:
        """结束跟踪: 把还活着的线收尾(不再等 end_gap), 返回本队的全部线路。

        这里收尾的线**一律不算被击毁** —— 它们只是“录制到这里停了”
        (用户特意提过有人整局不死, 结束时全场 icon 消失不能当成击毁)。
        """
        for lv in self._live:
            self._close(lv, dead=False)
        self._live.clear()
        return self.lives

    # ------------------------------------------------------------------ 内部
    def _new_life(self, u: Unit, t: float) -> _Live:
        self._seq += 1
        life = UnitLife(
            life_id=f"{self.record.route_id}:{self.side}{self._seq}",
            icon=u.icon,
            obj_type=u.typ,
            color=u.color,
            map_key=self.record.map_key,
        )
        life.points.append(TrackPoint(x=u.x, y=u.y, t=t, speed=0.0))
        self.lives.append(life)
        return _Live(life=life, x=u.x, y=u.y, last_seen=t, last_store=t,
                     last_x=u.x, last_y=u.y)

    def _close(self, lv: _Live, dead: bool = False) -> None:
        """收尾一条线: 记下“是否被击毁”, 并补上最后一个已知位置(若与已存的点不同)。"""
        lv.life.dead = bool(dead)
        if lv.life.points:
            last = lv.life.points[-1]
            if math.hypot(lv.x - last.x, lv.y - last.y) > 1e-9:
                lv.life.points.append(TrackPoint(x=lv.x, y=lv.y, t=lv.last_seen,
                                                 speed=last.speed))

    def _match(self, t: float, units: list[Unit]) -> None:
        """帧间最近邻 + 贪心匹配(先按距离排序, 短的先配)。"""
        pairs: list[tuple[float, int, int]] = []
        for i, lv in enumerate(self._live):
            typ = lv.life.obj_type
            for j, u in enumerate(units):
                if u.typ != typ:
                    continue          # 类型不同不可能是同一个单位(地面 ↔ 空中)
                d = math.hypot(u.x - lv.x, u.y - lv.y)
                if d <= self.match_radius:
                    pairs.append((d, i, j))
        pairs.sort()
        taken_l: dict[int, int] = {}     # live 下标 -> unit 下标
        taken_u: set[int] = set()
        for _d, i, j in pairs:
            if i in taken_l or j in taken_u:
                continue
            taken_l[i] = j
            taken_u.add(j)
        for i, j in taken_l.items():
            self._touch(self._live[i], units[j], t)
        # 没接上的 → 新线; 接不上的 live 交给 _expire 按 end_gap 收尾
        for j, u in enumerate(units):
            if j in taken_u:
                continue
            self._live.append(self._new_life(u, t))

    def _touch(self, lv: _Live, u: Unit, t: float) -> None:
        """一条线接上了这一帧: 更新位置, 到点就存一个带速度的采样点。"""
        lv.x, lv.y = u.x, u.y
        lv.last_seen = t
        lv.life.color = u.color
        moved = math.hypot(u.x - lv.last_x, u.y - lv.last_y)
        if moved < MIN_MOVE:
            # 一动不动: 只保留偶尔的心跳点(“在这儿停了 5 分钟”也要留痕)
            if (t - lv.last_store) >= HEARTBEAT:
                lv.life.points.append(TrackPoint(x=u.x, y=u.y, t=t, speed=0.0))
                lv.last_store = t
            return
        if (t - lv.last_store) >= self.interval:
            speed = self._speed(lv.last_x, lv.last_y, u.x, u.y, t - lv.last_store)
            lv.life.points.append(TrackPoint(x=u.x, y=u.y, t=t, speed=speed))
            lv.last_x, lv.last_y = u.x, u.y
            lv.last_store = t

    def _expire(self, t: float, alive: bool = False) -> None:
        """连续 `end_gap` 秒没接上的线 → 结束。

        `alive` = 这一帧场上还有别的可录单位 → 断掉的线判为**被击毁**;
        全场为空(回到车库/录制结束) 则只算“线到此为止”, 不标击毁。
        """
        keep: list[_Live] = []
        for lv in self._live:
            if (t - lv.last_seen) > self.end_gap:
                self._close(lv, dead=alive)
            else:
                keep.append(lv)
        self._live = keep
