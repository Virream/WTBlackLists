"""全局录制: 从每帧 `/map_obj.json` 拼出“每辆车的线路”(多目标跟踪)。

**为什么需要跟踪**: 8111 的地图对象里**没有 id、没有玩家名**, 只有
`type`(如 `ground_model`) / `icon`(如 `MediumTank`) / `color` / `x`,`y`。
所以“同一个单位”只能靠**帧间最近邻 + 贪心匹配**推测:

* 每个已存在的线路, 在同一 `type` 的单位里找**距离最近**的那个接上;
* 没接上的单位 → 新开一条线(重生 / 刚从视野外进来);
* 一条线连续 `END_GAP` 秒都没接上 → 结束(死亡 / 换车 / 上飞机)。

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

`DRONE_HINTS` 保留成一个"备选口": 万一以后某个版本给无人机单独起了 icon 名字,
把关键字加进去就能立刻恢复录制。
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

# 8111 的“重生保护”颜色(深蓝): 实测 2026-10-21 回放, 约 20s, 移动会缩短
PROTECTED_COLOR = "#1839a7"

# 能录线路的单位类型(白名单 —— 静态地物/AI 防空/机场/航母等一概不碰)
GROUND_TYPES = frozenset({
    "ground_model", "tank", "ifv", "spg", "spaa", "armored_car", "truck",
})
AIR_TYPES = frozenset({
    "aircraft", "helicopter", "heli", "plane", "uav",
})
# 空中类型里**要保留**的是"微型侦察无人机" —— 但实测(2026-10-21)无人机的 icon 就是
# `Fighter`, 与固定翼完全一样, 所以这几个关键字目前**不会命中任何东西**,
# 只是留给以后万一游戏给无人机单独起了名字时直接用。
DRONE_HINTS = ("uav", "drone", "scout", "recon")


def _norm_type(obj: dict) -> str:
    return str(obj.get("type") or "").strip()


def is_ground(typ: str) -> bool:
    return typ.lower() in GROUND_TYPES


def is_air(typ: str) -> bool:
    return typ.lower() in AIR_TYPES


def is_drone(typ: str, icon: str) -> bool:
    """是不是“微型侦察无人机”(唯一的例外: 空中单位也要录)。"""
    text = f"{typ} {icon}".lower()
    return any(h in text for h in DRONE_HINTS)


def recordable(typ: str, icon: str) -> bool:
    """这个对象要不要录线路(见模块 docstring 的“用户口径”)。"""
    if is_ground(typ):
        return True
    if is_air(typ):
        return is_drone(typ, icon)
    return False


def is_protected(color: str) -> bool:
    """这一帧是否处于重生保护(深蓝)。"""
    return str(color or "").strip().lower() == PROTECTED_COLOR


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
                 end_gap: float = END_GAP):
        self.record = record
        self.side = side
        self.interval = max(0.02, float(interval))
        self.match_radius = max(0.0, float(match_radius))
        self.end_gap = max(0.1, float(end_gap))
        self.lives: list[UnitLife] = []
        self._live: list[_Live] = []
        self._seq = 0
        self._empty_since: float | None = None
        self.seen_skipped: dict[tuple[str, str], int] = {}

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
        self._expire(t)
        if not units:
            if self._empty_since is None:
                self._empty_since = t
            return (t - self._empty_since) >= EMPTY_STOP
        self._empty_since = None
        return False

    def finish(self) -> list[UnitLife]:
        """结束跟踪: 把还活着的线收尾(不再等 end_gap), 返回本队的全部线路。"""
        for lv in self._live:
            self._close(lv)
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
            protected=is_protected(u.color),
        )
        life.points.append(TrackPoint(x=u.x, y=u.y, t=t))
        self.lives.append(life)
        return _Live(life=life, x=u.x, y=u.y, last_seen=t, last_store=t,
                     last_x=u.x, last_y=u.y)

    def _close(self, lv: _Live) -> None:
        """收尾一条线: 补上最后一个已知位置(若与已存的点不同)。"""
        if lv.life.points:
            last = lv.life.points[-1]
            if math.hypot(lv.x - last.x, lv.y - last.y) > 1e-9:
                lv.life.points.append(TrackPoint(x=lv.x, y=lv.y, t=lv.last_seen))

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
        # 注意: 拆分(重生)会**替换**列表里的元素, 所以先收集、循环外再改 ——
        # 循环里直接 remove 会让后面几项的下标错位。
        replaced: dict[int, _Live] = {}
        for i, j in taken_l.items():
            new = self._touch(self._live[i], units[j], t)
            if new is not None:
                replaced[i] = new
        for i, new in replaced.items():
            self._live[i] = new
        # 没接上的 → 新线; 接不上的 live 交给 _expire 按 end_gap 收尾
        for j, u in enumerate(units):
            if j in taken_u:
                continue
            self._live.append(self._new_life(u, t))

    def _touch(self, lv: _Live, u: Unit, t: float) -> _Live | None:
        """一条线接上了这一帧: 更新位置、必要时拆线(重生)或存点。

        返回非 None 表示“这条线到此结束, 用返回值替换它”(重生: 蓝 → 深蓝)。
        """
        if is_protected(u.color) and not lv.life.protected:
            # 蓝 → 深蓝 = 重生: 上一段到此为止, 同一位置开一条新线
            self._close(lv)
            return self._new_life(u, t)
        lv.x, lv.y = u.x, u.y
        lv.last_seen = t
        lv.life.color = u.color
        moved = math.hypot(u.x - lv.last_x, u.y - lv.last_y)
        if moved < MIN_MOVE:
            # 一动不动: 只保留偶尔的心跳点(“在这儿停了 5 分钟”也要留痕)
            if (t - lv.last_store) >= HEARTBEAT:
                lv.life.points.append(TrackPoint(x=u.x, y=u.y, t=t))
                lv.last_store = t
            return None
        if (t - lv.last_store) >= self.interval:
            lv.life.points.append(TrackPoint(x=u.x, y=u.y, t=t))
            lv.last_x, lv.last_y = u.x, u.y
            lv.last_store = t
        return None

    def _expire(self, t: float) -> None:
        """连续 `end_gap` 秒没接上的线 → 结束。"""
        keep: list[_Live] = []
        for lv in self._live:
            if (t - lv.last_seen) > self.end_gap:
                self._close(lv)
            else:
                keep.append(lv)
        self._live = keep
