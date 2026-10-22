"""跑图线路记录持久化。

8111 **不提供地图名称**(只有几何数据与地图图片), 因此用地图图片内容的
指纹(sha1 前 12 位)作为地图唯一键; 显示名默认自动编号, 支持用户重命名。

模式: 8111 也不给模式名, 但 `/map_obj.json` 里的 `capture_zone`(战区)能决定它 ——
详见 `zones.py`。同一张图会有多种模式, 所以:

* 每条轨迹记录下当时的 **战区数量 + 战区坐标串(模式指纹)+ 模式名**;
* `map_zones` 按地图保存见过的战区布局, 战局结束后依旧能在地图上画出战区;
* **同一个模式还会有多个“版本”**(实测: 战区控制/攻陷分大地图与小地图、
  战区争夺的单个战区会从 3 个位置里随机), 版本就是**战区坐标串**本身;
  `layout_names` 存用户给版本起的名字(如“小地图”), 默认自动叫“区域 2 / 点位 3”。

数据文件: <data_dir>/routes.json
**文件名叫 .json, 内容其实是 gzip 压缩的 JSON**(2026-10-22 起):
实测 3.49MB 的明文压到 0.47MB(7.4 倍), 而加载反而**更快**(80ms → 63ms)——
磁盘少读 7.4 倍, 省下的 IO 时间多于解压开销。
**不兼容旧格式**(自用软件, 数据可随时重录): 读不回来就当没有数据。
想看内容: `python -m gzip -dc data/routes.json`, 或直接用 `tools/probe_turn_radius.py`
(那个工具同时支持 gzip 和明文 —— `backups/` 里的备份是明文)。

结构:
{
  "version": 1,
  "map_names": {"<地图指纹>": "地图名"},
  "layout_names": {"<地图指纹>": {"<战区坐标串>": "版本名"}},
  "known_maps": ["<地图指纹>", ...],
  "map_zones": {"<地图指纹>": [{"sig": .., "count": .., "mode": ..,
                               "zones": [{"label": .., "x": .., "y": ..}],
                               "seen_at": ..}, ...]},
  "routes": [ {路线记录}, ... ],
  "globals": [ {全局录制条目}, ... ]
}

`globals` 是「全局录制」条目(整局所有玩家的线路, 分 A/B 两队), 与 `routes`
是两类记录: 前者挂在侧栏**存放路线的那一级**, 但不可当单条轨迹回放。
"""
from __future__ import annotations

import hashlib
import gzip
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

from .config import data_dir
from .garage import GARAGE_MAP_SHA1
from .zones import layout_spread
from .zones import mode_name as zone_mode_name
from .zones import signature as zones_signature
from .zones import spawns_to_json, zones_from_signature, zones_to_json

log = logging.getLogger("route_store")


def _read_payload(path: str) -> dict | None:
    """读数据文件: **gzip 压缩的 JSON**。

    不做旧格式兼容(用户 2026-10-22 明确要求): 本软件自用、未发布, 数据随时
    可以重录, 没必要为早期的明文/BOM 文件留分支。读取失败就当没有数据
    (宁可从零开始, 也不要把半个文件当成完整数据用)。
    """
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("读取线路数据失败: %s", exc)
        return None
    return data if isinstance(data, dict) else None


def map_fingerprint(image_bytes: bytes | None) -> str:
    """由地图图片字节生成稳定的地图指纹(图片不可用时返回 unknown)。"""
    if not image_bytes:
        return "unknown"
    return hashlib.sha1(image_bytes).hexdigest()[:12]


# 8111 在**战局外**(/map_info.json 的 valid=false)返回的不是地图,
# 而是一张固定的 128x128 灰色 “?” 占位图(PNG, 1519 字节)。
# 实测 2026-10-06:
#   Content-Type: image/png, 1519 字节, SHA1 = ab516d02191d001d809253c346e8e3f93e13f136
# 采样得到 6 种颜色, 均为灰度(64/68/72/109/115/128), 中心像素 (128,128,128)。
# 用途: 把它当作“当前没有真实地图”的信号, 不要当地图绘制, 也不要当成一次换图。
# 常量统一放在 `garage.py`(黑名单助手判“在不在车库”也用同一份实测值)。
PLACEHOLDER_MAP_SHA1 = GARAGE_MAP_SHA1
PLACEHOLDER_MAP_KEY = PLACEHOLDER_MAP_SHA1[:12]


@dataclass
class RoutePoint:
    """轨迹采样点。"""
    x: float
    y: float
    t: float          # 相对录制开始的秒数
    speed: float      # 当时速度 km/h
    # 车头朝向(归一化单位方向向量, 与 /map.img 同坐标系); 0,0 = 没记录
    # (旧版本录的轨迹没有这两个字段, 回放时不画朝向箭头)
    dx: float = 0.0
    dy: float = 0.0


@dataclass
class RouteMark:
    """标记点 / 起点 / 终点。"""
    x: float
    y: float
    t: float          # 相对录制开始的秒数
    kind: str = "mark"   # start | mark | stop


@dataclass
class RouteRecord:
    """一次完整的跑图录制。

    `zone_count` / `zone_signature` / `mode_name` 是录制开始时采集的战区信息:
    8111 不给模式名, 由战区数量推出(见 zones.MODE_NAMES), 坐标串则是稳定的
    “模式指纹”。旧记录没有这三个字段 → 归为“未知模式”。
    """
    route_id: str
    map_key: str
    map_name: str
    vehicle: str
    created_at: str
    duration: float
    start_x: float
    start_y: float
    points: list[RoutePoint] = field(default_factory=list)
    marks: list[RouteMark] = field(default_factory=list)
    zone_count: int = 0
    zone_signature: str = ""
    mode_name: str = ""

    @property
    def label(self) -> str:
        """侧栏二级条目显示的文本。

        不重复带上用时 —— 侧栏已经有专门的“用时”列了, 重复前缀会挤掉车型名。
        """
        ts = self.created_at[11:16] if len(self.created_at) >= 16 else ""
        return f"{ts}  {self.vehicle}".strip()

    @property
    def mode_label(self) -> str:
        """模式显示名(旧记录没有存过 → 按战区数量现推, 推不出就“未知模式”)。"""
        return self.mode_name or zone_mode_name(self.zone_count)


# ====================================================================== 全局录制
# 「全局录制」= 一局回放里**所有玩家**的线路, 分 A / B 两方各录一次
# (回放的 `/map_obj.json` 只给观战那一方的单位, 所以要录两遍才拼出整局)。
#
# 与单条线路录制的区别:
# * **不记朝向**(用户要求), 只记 x/y/t —— 点用 TrackPoint, 比 RoutePoint 省一半内存;
# * 一条全局记录下面是**很多条**线路: 每个单位"从出现(重生)到消失(死亡/换车)"算一条,
#   按 A/B 两队分组, 组内按开始时间**正序**(越靠前越早);
# * 8111 的 `/map_obj.json` **没有 id、没有玩家名**, 所以"同一个单位"是录制器按
#   "上一帧位置最近的那个同类对象"推测出来的(见 route_recorder 里的匹配逻辑),
#   同一个人中途上飞机/换车会被拆成两条线 —— 这点与用户确认过, 可以接受。
GLOBAL_SIDES = ("A", "B")
GLOBAL_SIDE_LABELS = {"A": "A 队", "B": "B 队"}


@dataclass(slots=True)
class TrackPoint:
    """全局录制的轨迹点: 坐标 + 时间 + **速度**(km/h)。

    `dx`/`dy` 恒为 0(全局录制不记朝向), 但**字段必须有**: `point_at()` 在
    “早于首点/晚于末点”时会把原始点直接返回, 回放光标随即读 `.dx/.dy` ——
    缺了就会每帧抛 AttributeError(2026-10-21 实测: 整张地图画不出来)。
    字段名与 `RoutePoint` 对齐, 速度配色/playhead 切分/光标那套可以直接复用。
    """
    x: float
    y: float
    t: float
    speed: float = 0.0
    dx: float = 0.0
    dy: float = 0.0


@dataclass
class UnitLife:
    """全局录制里的一条线路 = 某个单位从出现(重生)到消失(死亡/换车)的一段轨迹。

    `life_id` 形如 `<全局条目 id>:<序号>` —— 侧栏的眼睛图标/地图缓存都拿它当键;
    为了能被回放与高亮那套代码直接使用, 额外暴露了 `route_id` / `marks` /
    `map_key` / `duration` 这几个与 `RouteRecord` 同名的属性。
    `icon` / `obj_type` 是 8111 原样的字段(如 `MediumTank` / `ground_model`),
    没有玩家名可用(8111 不给), 所以显示时就用它们当“名字”。
    """
    life_id: str
    icon: str = ""
    obj_type: str = ""
    color: str = ""             # 8111 的颜色(仅参考, 不参与分组)
    map_key: str = ""           # 所属地图(回放时用来判断底图对不对)
    points: list[TrackPoint] = field(default_factory=list)
    # 这条线是在哪一队录到的(`A` / `B`): 回放时阵亡标记的颜色靠它区分
    # (A 队 = 蓝, B 队 = 红)。旧数据没这个字段 → 读取时按 life_id 里的队号补。
    side: str = ""
    # 这条线的单位是**被击毁**结束的(而不是录制结束时还活着)。
    # 用户口径(2026-10-21): “在 8111 中玩家对应的 icon 消失就是死了” ——
    # 一次只能开一台载具, 想换固定翼必先阵亡; 退局后载具停在原地、
    # 8111 过一会儿不再上报, 也算击毁。实现上只看“线断那一刻场上还有没有别人”。
    dead: bool = False

    @property
    def route_id(self) -> str:
        """与 `RouteRecord.route_id` 同名 —— 地图缓存/回放/高亮都按它当键。"""
        return self.life_id

    @property
    def marks(self) -> list:
        """全局线路没有标记点 ⇒ 回放时“到下一标记暂停”会直接播到底。"""
        return []

    # ---- 下面两个只为了让绘制/悬停那套代码把它当成普通轨迹(鸭子类型)
    @property
    def vehicle(self) -> str:
        return self.icon or self.obj_type or "未知单位"

    @property
    def created_at(self) -> str:
        return ""                  # 全局线路没有独立的录制时刻(它属于整局)

    @property
    def t0(self) -> float:
        return self.points[0].t if self.points else 0.0

    @property
    def t1(self) -> float:
        return self.points[-1].t if self.points else 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.t1 - self.t0)

    @property
    def label(self) -> str:
        name = self.icon or self.obj_type or "未知单位"
        return f"{fmt_time(self.t0)}  {name}"

    def to_json(self) -> dict:
        # 点写成 [t, x, y, speed] 的紧凑数组: 整局几十辆车 → 几十万个点,
        # 用 {"x":..,"y":..,"t":..} 的字典形式会让 routes.json 膨胀 3 倍以上
        return {
            "id": self.life_id,
            "icon": self.icon,
            "type": self.obj_type,
            "color": self.color,
            "map_key": self.map_key,
            "side": self.side,
            "dead": 1 if self.dead else 0,
            "pts": [[round(p.t, 2), round(p.x, 5), round(p.y, 5), round(p.speed, 1)]
                    for p in self.points],
        }

    @classmethod
    def from_json(cls, data: dict) -> UnitLife | None:
        try:
            life_id = str(data["id"])
        except (KeyError, TypeError):
            return None
        pts: list[TrackPoint] = []
        for row in data.get("pts") or []:
            try:
                t, x, y = float(row[0]), float(row[1]), float(row[2])
            except (IndexError, TypeError, ValueError):
                continue
            speed = 0.0
            if len(row) > 3:
                try:
                    speed = float(row[3])
                except (TypeError, ValueError):
                    speed = 0.0
            pts.append(TrackPoint(x=x, y=y, t=t, speed=speed))
        return cls(
            life_id=life_id,
            icon=str(data.get("icon") or ""),
            obj_type=str(data.get("type") or ""),
            color=str(data.get("color") or ""),
            map_key=str(data.get("map_key") or ""),
            points=pts,
            side=str(data.get("side") or ""),
            dead=bool(data.get("dead")),
        )


@dataclass
class GlobalRecord:
    """一次“全局录制”条目(侧栏里挂在**存放路线的那一级**, 与单条路线同级)。

    名称直接用录制时间(`label`); 下面分 A / B 两队, 每队若干条 `UnitLife`;
    组内按开始时间正序(用户要求: 越靠前时间越早)。
    `marks` 是右键打上的“下次录到哪一队”(可同时有 A 和 B) ——
    **没有任何条目带标记**时, 点「A/B 队录制」会新建一个全局条目。
    """
    route_id: str
    map_key: str
    map_name: str
    created_at: str
    zone_count: int = 0
    zone_signature: str = ""
    mode_name: str = ""
    duration: float = 0.0
    marks: list[str] = field(default_factory=list)
    sides: dict[str, list[UnitLife]] = field(default_factory=lambda: {"A": [], "B": []})
    # 战区状态**时间轴**: [{"t": 秒, "zones": [{label,x,y,side,color,blink}]}]
    # 只在“归属变了或开始/结束被占领”时记一条(见 `RouteRecorder._record_zone_state`),
    # 回放时按时间取最近一条 → 战区能重现当时的蓝/红/占领中, 而不是一张静态快照。
    # 旧数据没有这个字段 → 回放退回静态布局(取 `layout_for`)。
    zone_states: list[dict] = field(default_factory=list)

    def zone_state_at(self, t: float) -> list[dict] | None:
        """取回放时刻 `t` 的战区快照(没有时间轴返回 None)。

        用时间轴里 `<= t` 的最后一条 —— 战区状态是“阶毯式”变化(只在不连续时刻变),
        用“最近的前一条”才是当时真实的样子; 取最近的一条(不管是前是后)会在
        占领发生**之前**就先把颜色改了。
        """
        states = self.zone_states
        if not states:
            return None
        lo, hi, best = 0, len(states) - 1, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if float(states[mid].get("t") or 0.0) <= t:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        if best < 0:
            best = 0                     # 早于第一条 → 用第一条(录制开始那一刻)
        zones = states[best].get("zones")
        return list(zones) if isinstance(zones, list) else None

    @property
    def label(self) -> str:
        """侧栏显示名 = 录制时间(如 `10-21 21:03`)。"""
        ts = self.created_at or ""
        if len(ts) >= 16:
            return f"{ts[5:10]} {ts[11:16]}"
        return ts or "全局录制"

    @property
    def mode_label(self) -> str:
        return self.mode_name or zone_mode_name(self.zone_count)

    @property
    def mark_label(self) -> str:
        """标记的显示文字(没有标记返回空串)。"""
        return " ".join(s for s in GLOBAL_SIDES if s in self.marks)

    def lives(self, side: str) -> list[UnitLife]:
        return self.sides.get(side) or []

    def count(self) -> int:
        return sum(len(v) for v in self.sides.values())

    def timeline_note(self) -> str:
        """A/B 两队的**时间轴对比**(写日志/诊断用)。

        两队都是“抓到第一辆载具的那一刻 = 0 点”, 所以两边都从 0 开始 ——
        能拿来比的只有**结尾**:

        * 结尾相同(±0.5s) → 两边的可录单位消失于同一时刻(通常就是战斗结束),
          说明两次录制的 0 点也对得上;
        * 结尾差得多 → 两种可能, 8111 的数据**分不出来**:
          ① 某一队先被打光/退场(跟对齐无关),
          ② 两次录制的 0 点真的错开了(两侧“第一辆车”不是同一瞬间重生)。

        没有更好锚点的原因: 8111 不提供战斗时钟(`/hudmsg` 的 `time` 实测不可靠),
        而 `/map_obj.json` 只给**观战那一方**的单位 —— 两侧看到的第一个重生时刻
        本来就可能差 1~2 秒。
        """
        spans = []
        for side in GLOBAL_SIDES:
            lives = [lf for lf in self.lives(side) if lf.points]
            if lives:
                spans.append((side, len(lives),
                              max(lf.points[-1].t for lf in lives)))
        if not spans:
            return "无线路"
        parts = [f"{side} 队 {n} 条 0~{last:.1f}s" for side, n, last in spans]
        if len(spans) == 2:
            diff = spans[0][2] - spans[1][2]
            parts.append("结尾对齐" if abs(diff) <= 0.5 else f"结尾差 {diff:+.1f}s")
        return " | ".join(parts)

    def to_json(self) -> dict:
        return {
            "route_id": self.route_id,
            "map_key": self.map_key,
            "map_name": self.map_name,
            "created_at": self.created_at,
            "zone_count": self.zone_count,
            "zone_signature": self.zone_signature,
            "mode_name": self.mode_name,
            "duration": self.duration,
            "marks": list(self.marks),
            "zone_states": self.zone_states,
            "sides": {side: [life.to_json() for life in self.lives(side)]
                      for side in GLOBAL_SIDES},
        }

    @classmethod
    def from_json(cls, data: dict) -> GlobalRecord | None:
        try:
            rec = cls(
                route_id=str(data["route_id"]),
                map_key=str(data.get("map_key", "unknown")),
                map_name=str(data.get("map_name", "未知地图")),
                created_at=str(data.get("created_at", "")),
                zone_count=int(data.get("zone_count") or 0),
                zone_signature=str(data.get("zone_signature") or ""),
                mode_name=str(data.get("mode_name") or ""),
                duration=float(data.get("duration") or 0.0),
                marks=[str(m) for m in (data.get("marks") or [])
                       if str(m) in GLOBAL_SIDES],
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("跳过损坏的全局录制记录: %s", exc)
            return None
        for row in data.get("zone_states") or []:
            if not isinstance(row, dict):
                continue
            zones = row.get("zones")
            if not isinstance(zones, list) or not zones:
                continue
            try:
                t = float(row.get("t") or 0.0)
            except (TypeError, ValueError):
                continue
            rec.zone_states.append({"t": t, "zones": [z for z in zones
                                                       if isinstance(z, dict)]})
        for side, rows in (data.get("sides") or {}).items():
            if side not in GLOBAL_SIDES or not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                life = UnitLife.from_json(row)
                if life is not None:
                    # 早期版本没存 map_key → 用条目自己的(否则切图后会被当成“别的地图”)
                    if not life.map_key:
                        life.map_key = rec.map_key
                    if life.side not in GLOBAL_SIDES:
                        life.side = side          # 早期版本没存 side → 用所在组的队号
                    rec.sides[side].append(life)
        _infer_legacy_deaths(rec, data)
        return rec


# 旧数据推断“被击毁”的门槛(秒): 线路结束得比**整局末尾**早这么多, 才认为是中途阵亡。
# 活到最后的那些线会在录制结束前 1~2 秒内陆续收尾(END_GAP), 用 10 秒足够安全。
LEGACY_DEAD_MARGIN = 10.0


def _infer_legacy_deaths(rec: GlobalRecord, data: dict) -> None:
    """给**旧版本录的**全局条目补上 `dead` 标记(它们当年没存这个信息)。

    判定: 某条线在整局末尾之前 `LEGACY_DEAD_MARGIN` 秒以上就结束了 → 当作中途阵亡。
    只在“整份数据里没有任何一条线带 `dead` 字段”时推断 —— 新录的数据总会写
    `dead: 0/1`, 所以不会把“这一局真的没人死”误判成旧数据。
    """
    rows: list[dict] = []
    for side_rows in (data.get("sides") or {}).values():
        if isinstance(side_rows, list):
            rows.extend(r for r in side_rows if isinstance(r, dict))
    if not rows or any("dead" in r for r in rows):
        return                      # 新数据(或空) → 不推断
    lives = [lf for side in GLOBAL_SIDES for lf in rec.lives(side) if lf.points]
    if not lives:
        return
    last_t = max(lf.t1 for lf in lives)
    for lf in lives:
        lf.dead = lf.t1 < last_t - LEGACY_DEAD_MARGIN


def fmt_time(seconds: float) -> str:
    """秒 -> MM:SS。"""
    s = max(0, int(round(seconds)))
    return f"{s // 60:02d}:{s % 60:02d}"


def fmt_time_ms(seconds: float) -> str:
    """秒 -> MM:SS.mmm(毫秒精度, 用于轨迹悬停预览)。"""
    total_ms = max(0, int(round(float(seconds) * 1000)))
    m, rem = divmod(total_ms, 60_000)
    sec, ms = divmod(rem, 1000)
    return f"{m:02d}:{sec:02d}.{ms:03d}"


def fmt_speed(value: float) -> str:
    """速度(km/h)显示: 陆战 8111 返回整数(无小数), 空战有小数。

    实测: 陆战 4000+ 样本无一小数; 空战最多 6 位小数。
    这里自适应 —— 整数不显示小数点, 有小数才保留 1 位。
    """
    v = float(value)
    return f"{v:.0f}" if abs(v - round(v)) < 1e-6 else f"{v:.1f}"


def point_at(points: list[RoutePoint], t: float) -> RoutePoint | None:
    """取 t 时刻的轨迹位置(线性插值)。

    回放光标需要平滑移动, 不能在采样点之间跳 —— 因此按时间插值。
    t 超出范围时返回首/末点; 空列表返回 None。
    """
    if not points:
        return None
    if t <= points[0].t:
        return points[0]
    if t >= points[-1].t:
        return points[-1]
    lo, hi = 0, len(points) - 1
    while hi - lo > 1:                      # 二分找到区间
        mid = (lo + hi) // 2
        if points[mid].t <= t:
            lo = mid
        else:
            hi = mid
    a, b = points[lo], points[hi]
    span = b.t - a.t
    k = 0.0 if span <= 0 else (t - a.t) / span
    # 朝向取**更近那一端**的采样值: 单位向量做线性插值在掉头时会退化成零向量
    # (方向丢失), 而采样间隔只有几十 ms, 取端点肉眼看不出跳变。
    src = a if k < 0.5 else b
    return RoutePoint(x=a.x + (b.x - a.x) * k,
                      y=a.y + (b.y - a.y) * k,
                      t=t,
                      speed=a.speed + (b.speed - a.speed) * k,
                      dx=src.dx, dy=src.dy)


def split_points(points: list[RoutePoint],
                 t: float) -> tuple[list[RoutePoint], list[RoutePoint]]:
    """按时刻 t 把轨迹切成 (已播放, 未播放), 边界插入插值点以保证连续。"""
    if not points:
        return ([], [])
    head = [p for p in points if p.t <= t]
    tail = [p for p in points if p.t > t]
    mid = point_at(points, t)
    if mid is not None:
        if not head or head[-1].t < t:
            head.append(mid)
        if tail and tail[0].t > t:
            tail.insert(0, mid)
    return (head, tail)


# 8111 载具内部名的常见国家前缀(显示时去掉, 更接近游戏内名称)
_NATION_PREFIXES = (
    "germ_", "ussr_", "us_", "uk_", "jp_", "cn_", "fr_", "it_",
    "sw_", "il_", "rus_", "usa_",
)

# 载具类别前缀(实测 indicators.type 形如 tankModels/germ_leopard_2a4_pzbtl_123)
_MODELS_PREFIX = re.compile(r"^[A-Za-z]+Models/")


def pretty_vehicle(key: str) -> str:
    """把 8111 的载具内部名转为较友好的显示名。

    例: tankModels/germ_leopard_2a4_pzbtl_123 -> Leopard 2A4 Pzbtl 123;
        germ_leopard_2pl -> Leopard 2PL; us_m1_abrams -> M1 Abrams。
    无法解析时原样返回, 避免信息丢失。
    """
    if not key:
        return "未知载具"
    k = _MODELS_PREFIX.sub("", key.strip())
    for p in _NATION_PREFIXES:
        if k.startswith(p):
            k = k[len(p):]
            break
    words = [w for w in k.split("_") if w]
    if not words:
        return key

    def _fmt(w: str) -> str:
        if w.isdigit():
            return w
        if any(ch.isdigit() for ch in w):   # 数字+字母混合段(如 72b)整体大写
            return w.upper()
        return w.capitalize()

    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        # 数字段 + 紧邻的短后缀(如 2 + pl)合并: "2PL"
        if (w.isdigit() and i + 1 < len(words)
                and words[i + 1].isalpha() and len(words[i + 1]) <= 3):
            out.append(w + words[i + 1].upper())
            i += 2
            continue
        out.append(_fmt(w))
        i += 1
    return " ".join(out)


class RouteStore:
    """线路记录读写(原子写, 线程安全)。"""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(data_dir(), "routes.json")
        self._lock = threading.RLock()
        self.map_names: dict[str, str] = {}
        # 用户给“模式版本(战区布局)”起的名字: {地图键: {战区坐标串: 名字}}
        self.layout_names: dict[str, dict[str, str]] = {}
        # 见过的地图键(含**只缓存了图片、还没录过轨迹**的地图):
        # 侧栏与「地图管理」的默认编号都基于它, 保证两边一致且稳定
        self.known_maps: list[str] = []
        # 见过的战区布局: {地图键: [布局, ...]}(新的在前, 每种模式一条),
        # 存下来才能在战局结束后依旧把 A/B/C 画在地图上
        self.map_zones: dict[str, list[dict]] = {}
        self.routes: list[RouteRecord] = []
        # 全局录制条目(整局所有玩家的线路, 分 A/B 两队)
        self.globals: list[GlobalRecord] = []
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        data = _read_payload(self.path)
        if not isinstance(data, dict):
            return
        try:
            names = data.get("map_names")
            if isinstance(names, dict):
                self.map_names = {str(k): str(v) for k, v in names.items()}
            known = data.get("known_maps")
            if isinstance(known, list):
                self.known_maps = [str(k) for k in known if str(k)]
            lnames = data.get("layout_names")
            if isinstance(lnames, dict):
                for key, names in lnames.items():
                    if isinstance(names, dict):
                        self.layout_names[str(key)] = {
                            str(s): str(v) for s, v in names.items() if str(v).strip()
                        }
            mz = data.get("map_zones")
            if isinstance(mz, dict):
                for key, layouts in mz.items():
                    if isinstance(layouts, list):
                        self.map_zones[str(key)] = [x for x in layouts if isinstance(x, dict)]
            for item in data.get("routes") or []:
                try:
                    pts = [RoutePoint(**p) for p in item.get("points") or []]
                    mks = [RouteMark(**m) for m in item.get("marks") or []]
                    self.routes.append(RouteRecord(
                        route_id=str(item["route_id"]),
                        map_key=str(item.get("map_key", "unknown")),
                        map_name=str(item.get("map_name", "未知地图")),
                        vehicle=str(item.get("vehicle", "")),
                        created_at=str(item.get("created_at", "")),
                        duration=float(item.get("duration", 0.0)),
                        start_x=float(item.get("start_x", 0.0)),
                        start_y=float(item.get("start_y", 0.0)),
                        points=pts, marks=mks,
                        # 旧版本的记录没有这三个字段 → 默认 0/空 → 归为“未知模式”
                        zone_count=int(item.get("zone_count") or 0),
                        zone_signature=str(item.get("zone_signature") or ""),
                        mode_name=str(item.get("mode_name") or ""),
                    ))
                except (KeyError, TypeError, ValueError) as exc:
                    log.warning("跳过损坏的线路记录: %s", exc)
            for item in data.get("globals") or []:
                if isinstance(item, dict):
                    rec = GlobalRecord.from_json(item)
                    if rec is not None:
                        self.globals.append(rec)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("读取线路数据失败: %s", exc)
        self._recover_layouts()

    def _recover_layouts(self) -> bool:
        """轨迹里有战区指纹、但 `map_zones` 没存过对应布局 → 从指纹还原出来。

        指纹本身就是 `字母@x,y;...`, 所以能反推战区位置; 这样四层树(地图→模式→版本)
        与地图上画战区对**旧数据**也成立(旧版本的 exe 没存过布局)。
        不在这里写盘 —— 下次正常保存时自然写回去。
        """
        known: dict[str, set[str]] = {k: {str(x.get("sig") or "") for x in v}
                                      for k, v in self.map_zones.items()}
        changed = False
        for rec in [*self.routes, *self.globals]:
            sig = rec.zone_signature
            if not sig or not rec.map_key or sig in known.get(rec.map_key, set()):
                continue
            zones = zones_from_signature(sig)
            if not zones:
                continue
            self.map_zones.setdefault(rec.map_key, []).append({
                "sig": sig,
                "count": len(zones),
                "mode": zone_mode_name(len(zones)),
                "zones": zones_to_json(zones),
                "seen_at": "",
            })
            known.setdefault(rec.map_key, set()).add(sig)
            changed = True
        if changed:
            log.info("从轨迹指纹补回了缺失的战区布局")
        return changed

    def save(self) -> None:
        """原子写盘(tmp + os.replace), 强杀不残留半截文件。

        写的是**紧凑 JSON + gzip**(不带缩进): 缩进本身占文件体积的 ~40%, 而
        gzip 又能再压到 14% —— 合起来 3.49MB → 0.47MB。压缩后加载反而更快:
        磁盘少读 7.4 倍, 省下的时间多于解压(实测 80ms → 63ms)。
        """
        with self._lock:
            payload = {
                "version": 1,
                "map_names": self.map_names,
                "layout_names": self.layout_names,
                "known_maps": self.ordered_map_keys(),
                "map_zones": self.map_zones,
                "routes": [
                    {
                        **{k: v for k, v in asdict(r).items()
                           if k not in ("points", "marks")},
                        "points": [asdict(p) for p in r.points],
                        "marks": [asdict(m) for m in r.marks],
                    }
                    for r in self.routes
                ],
                "globals": [g.to_json() for g in self.globals],
            }
            tmp = self.path + ".tmp"
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                blob = gzip.compress(text.encode("utf-8"), compresslevel=6)
                with open(tmp, "wb") as f:
                    f.write(blob)
                os.replace(tmp, self.path)
            except (OSError, ValueError) as exc:
                log.error("保存线路数据失败: %s", exc)

    # ------------------------------------------------------------------
    def new_route(self, map_key: str, map_name: str, vehicle: str,
                  start_x: float, start_y: float) -> RouteRecord:
        """创建(尚未落盘)一条新线路记录。"""
        with self._lock:
            if map_key and map_key not in self.known_maps:
                self.known_maps.append(map_key)   # 新地图登记, 保证默认编号稳定
        rec = RouteRecord(
            route_id=uuid.uuid4().hex[:12],
            map_key=map_key,
            map_name=map_name,
            vehicle=vehicle,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            duration=0.0,
            start_x=start_x,
            start_y=start_y,
        )
        rec.marks.append(RouteMark(x=start_x, y=start_y, t=0.0, kind="start"))
        return rec

    def add(self, rec: RouteRecord) -> None:
        with self._lock:
            self.routes.append(rec)
        if rec.zone_signature:
            # 这条记录带着战区指纹, 但可能没存过对应布局(旧版 exe/手工数据)
            # → 立刻从指纹补上, 保证侧栏四层树与地图画战区都能正常工作
            self._recover_layouts()
        self.save()

    def remove(self, route_id: str) -> bool:
        with self._lock:
            before = len(self.routes)
            self.routes = [r for r in self.routes if r.route_id != route_id]
            changed = len(self.routes) != before
        if changed:
            self.save()
        return changed

    def get(self, route_id: str) -> RouteRecord | None:
        for r in self.routes:
            if r.route_id == route_id:
                return r
        return None

    # ------------------------------------------------------------ 全局录制条目
    def new_global(self, map_key: str, map_name: str, zone_count: int = 0,
                   zone_signature: str = "", mode_name: str = "") -> GlobalRecord:
        """创建一个(尚未落盘的)全局录制条目 —— 名字就是录制时间。"""
        with self._lock:
            if map_key and map_key not in self.known_maps:
                self.known_maps.append(map_key)
        return GlobalRecord(
            route_id=uuid.uuid4().hex[:12],
            map_key=map_key,
            map_name=map_name,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            zone_count=zone_count,
            zone_signature=zone_signature,
            mode_name=mode_name,
        )

    def add_global(self, rec: GlobalRecord) -> None:
        with self._lock:
            self.globals.append(rec)
        if rec.zone_signature:
            self._recover_layouts()
        self.save()

    def remove_global(self, gid: str) -> bool:
        with self._lock:
            before = len(self.globals)
            self.globals = [g for g in self.globals if g.route_id != gid]
            changed = len(self.globals) != before
        if changed:
            self.save()
        return changed

    def get_global(self, gid: str) -> GlobalRecord | None:
        for g in self.globals:
            if g.route_id == gid:
                return g
        return None

    def marked_global(self, side: str) -> GlobalRecord | None:
        """找出被标记为“下次录到 `side` 队”的条目(同一队最多一条, 见 set_mark)。"""
        for g in self.globals:
            if side in g.marks:
                return g
        return None

    def set_mark(self, gid: str, side: str, on: bool) -> None:
        """给条目打/清标记。**同一队只会有一条**记录带标记 —— 打新标记时清掉旧条目上的。"""
        if side not in GLOBAL_SIDES:
            return
        with self._lock:
            for g in self.globals:
                if g.route_id == gid:
                    continue
                if side in g.marks:
                    g.marks.remove(side)     # 一队只能指向一个条目, 否则不知道该录到哪
            target = self.get_global(gid)
            if target is not None:
                if on and side not in target.marks:
                    target.marks.append(side)
                elif not on and side in target.marks:
                    target.marks.remove(side)
                target.marks.sort(key=GLOBAL_SIDES.index)
        self.save()

    def clear_marks(self, gid: str) -> None:
        """清掉某个条目的全部标记(右键「清除标记」)。"""
        with self._lock:
            target = self.get_global(gid)
            if target is not None:
                target.marks.clear()
        self.save()

    def globals_by_group(self) -> dict[tuple[str, str], list[GlobalRecord]]:
        """{(地图键, 战区指纹): [全局录制条目...]} —— 新的在前(按录制时间倒序)。"""
        out: dict[tuple[str, str], list[GlobalRecord]] = {}
        for g in self.globals:
            out.setdefault((g.map_key or "", g.zone_signature or ""), []).append(g)
        for rows in out.values():
            rows.sort(key=lambda g: g.created_at, reverse=True)
        return out

    # ------------------------------------------------------------------
    def display_name(self, map_key: str) -> str:
        """地图显示名: 用户命名优先, 否则按“首次见到”的顺序自动编号。"""
        if map_key in self.map_names and self.map_names[map_key].strip():
            return self.map_names[map_key]
        keys = self.ordered_map_keys()
        if map_key in keys:
            return f"地图 {keys.index(map_key) + 1}"
        return f"地图 {len(keys) + 1}"

    def name_of(self, map_key: str) -> str:
        """用户给这张地图起的名字(没起过则为空串)。"""
        return (self.map_names.get(map_key) or "").strip()

    def remember_map(self, map_key: str) -> None:
        """登记一张见过(缓存过图片)的地图, 使它也有稳定的默认编号。"""
        if not map_key or map_key in ("unknown", PLACEHOLDER_MAP_KEY):
            return
        with self._lock:
            if map_key in self.known_maps:
                return
            self.known_maps.append(map_key)
        self.save()

    def rename_map(self, map_key: str, name: str) -> None:
        with self._lock:
            self.map_names[map_key] = name.strip() or self.map_names.get(map_key, "")
            for r in self.routes:
                if r.map_key == map_key:
                    r.map_name = self.map_names[map_key]
        self.save()

    def merge_map_keys(self, keep: str, drop: str) -> bool:
        """把 `drop` 这张地图的全部数据并到 `keep` 上, 并删掉 `drop` 条目。

        用在“同一张地图被 8111 以不同分辨率发过来”时(见 MapCache.save):
        两个键其实是同一张图, 侧栏/地图管理里就应该只有一个地图。
        会先把 routes.json 备份成 .bak —— 这是会改用户数据的操作。
        """
        if not keep or not drop or keep == drop:
            return False
        moved = [r for r in self.routes if r.map_key == drop]
        moved_g = [g for g in self.globals if g.map_key == drop]
        has_data = bool(moved) or bool(moved_g) or drop in self.known_maps \
            or bool(self.name_of(drop)) \
            or bool(self.map_zones.get(drop)) or bool(self.layout_names.get(drop))
        if not has_data:
            return False
        try:
            if os.path.exists(self.path):
                shutil.copyfile(self.path, self.path + ".bak")
        except OSError as exc:  # noqa: BLE001
            log.warning("备份线路数据失败: %s", exc)
        with self._lock:
            keep_name = self.display_name(keep)      # drop 的轨迹改用 keep 的显示名
            for rec in moved:
                rec.map_key = keep
                rec.map_name = keep_name
            for g in moved_g:
                g.map_key = keep
                g.map_name = keep_name
            # 见过的地图: drop 的位置换成 keep(保持“首次见到”的顺序), 去掉重复
            self.known_maps = [keep if k == drop else k for k in self.known_maps]
            seen: set[str] = set()
            self.known_maps = [k for k in self.known_maps
                               if not (k in seen or seen.add(k))]
            if keep not in self.known_maps:
                self.known_maps.append(keep)
            # 名字: keep 没名字就用 drop 的
            if not self.name_of(keep) and self.name_of(drop):
                self.map_names[keep] = self.map_names[drop]
            self.map_names.pop(drop, None)
            # 战区布局: 按指纹合并(同一布局只留一份, 取较新的 seen_at)
            merged: dict[str, dict] = {}
            for layout in (self.map_zones.get(keep) or []) + (self.map_zones.get(drop) or []):
                sig = str(layout.get("sig") or "")
                old = merged.get(sig)
                if old is None or str(layout.get("seen_at") or "") > str(old.get("seen_at") or ""):
                    merged[sig] = layout
            if merged:
                self.map_zones[keep] = sorted(
                    merged.values(), key=lambda x: str(x.get("seen_at") or ""),
                    reverse=True)[:12]
            self.map_zones.pop(drop, None)
            # 用户给“区域/点位”起的名字
            names = self.layout_names.pop(drop, None)
            if names:
                tgt = self.layout_names.setdefault(keep, {})
                for sig, name in names.items():
                    tgt.setdefault(sig, name)
            if not self.layout_names.get(keep):
                self.layout_names.pop(keep, None)
        self.save()
        log.info("已合并地图条目: %s ← %s (%d 条轨迹)", keep, drop, len(moved))
        return True

    def ordered_map_keys(self) -> list[str]:
        """按首次见到顺序返回全部地图键(轨迹里的 + 全局录制的 + 只缓存了图片的)。"""
        keys = [k for k in self.known_maps]
        for r in [*self.routes, *self.globals]:
            if r.map_key not in keys:
                keys.append(r.map_key)
        return keys

    def grouped(self) -> dict[str, list[RouteRecord]]:
        """{地图键: [线路...]}(组内按时间倒序, 最新的在前)。

        只包含**有记录**的地图 —— 只缓存了图片、还没录过轨迹的地图不占侧栏一行
        (那类地图在菜单「地图管理」里看/命名)。
        """
        out: dict[str, list[RouteRecord]] = {}
        for key in self.ordered_map_keys():
            rows = sorted(
                (r for r in self.routes if r.map_key == key),
                key=lambda r: r.created_at, reverse=True,
            )
            if rows:
                out[key] = rows
        return out

    # ------------------------------------------------------------------ 战区/模式
    def remember_zones(self, map_key: str, zones, spawns=None) -> bool:
        """记下某地图的战区布局 + **重生位置**(按坐标串去重)。返回是否是新布局。

        必须按指纹去重: 同一张图的**不同模式**战区数量/位置不同, 它们是不同的布局,
        而同一模式的多次对局指纹相同(只刷新 seen_at)。

        重生位置(`spawns`)跟着布局一起存 —— 它同样随模式/版本变(同一张图的
        “区域1/区域2”双方重生位置不同), 所以同一个布局的多次对局只更新一份,
        内容真的变了才写盘(战局内每帧都会调到这里)。
        """
        if not map_key or not zones:
            return False
        sig = zones_signature(zones)
        if not sig:
            return False
        spawn_json = spawns_to_json(spawns or [])
        with self._lock:
            layouts = self.map_zones.setdefault(map_key, [])
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            is_new = True
            for item in layouts:
                if item.get("sig") == sig:
                    is_new = False
                    item["seen_at"] = now
                    # 重生位置没变就不写盘(否则每帧一次存盘太浪费)
                    if spawn_json and item.get("spawns") != spawn_json:
                        item["spawns"] = spawn_json
                    break
            if is_new:
                layouts.insert(0, {
                    "sig": sig,
                    "count": len(zones),
                    "mode": zone_mode_name(len(zones)),
                    # 存位置 + 当次归属色(供以后录像解析参考); **不存 blink** —— 那只是
                    # “当时正在被占”的瞬时状态, 存下来会让旧布局一直闪
                    "zones": [{k: v for k, v in z.items() if k != "blink"}
                              for z in zones_to_json(zones)],
                    "spawns": spawn_json,      # 重生位置(地图上画空心方块的就是它们)
                    "seen_at": now,
                })
                del layouts[12:]                 # 单张地图最多留 12 种布局
        self.save()
        return is_new

    # ------------------------------------------------------------------ 版本(战区布局)
    def _layout_auto_names(self, map_key: str) -> list[tuple[str, str]]:
        """[(坐标串, 自动名)] —— 按模式分组编号, 顺序稳定(同一个地图每次算出来一样)。

        * 多战区模式: 按**跨度**降序 → `区域 1` 就是交战区最大的那个(通常是大地图);
        * 单战区模式: 按坐标排序 → `点位 1/2/3`(那 3 个随机位置)。
        """
        by_count: dict[int, list[dict]] = {}
        for layout in self.map_zones.get(map_key) or []:
            by_count.setdefault(int(layout.get("count") or 0), []).append(layout)
        out: list[tuple[str, str]] = []
        for count in sorted(by_count, reverse=True):      # 3 战区 → 2 → 1, 与侧栏一致
            group = by_count[count]
            if count <= 1:
                def _xy(item: dict) -> tuple[float, float]:
                    zones = item.get("zones") or [{}]
                    return (float(zones[0].get("x") or 0.0), float(zones[0].get("y") or 0.0))
                group.sort(key=lambda item: (*_xy(item), str(item.get("sig"))))
                prefix = "点位"
            else:
                group.sort(key=lambda item: (-layout_spread(item.get("zones")),
                                             str(item.get("sig"))))
                prefix = "区域"
            for i, layout in enumerate(group, start=1):
                out.append((str(layout.get("sig") or ""), f"{prefix} {i}"))
        return out

    def layout_display_name(self, map_key: str, signature: str) -> str:
        """版本显示名: 用户命名优先, 否则自动编号(区域 N / 点位 N)。"""
        if not signature:
            return "未知位置"
        user = (self.layout_names.get(map_key) or {}).get(signature, "").strip()
        if user:
            return user
        for sig, auto in self._layout_auto_names(map_key):
            if sig == signature:
                return auto
        return "未知位置"

    def name_of_layout(self, map_key: str, signature: str) -> str:
        """用户给这个版本起的名字(没起过则为空串)。"""
        return ((self.layout_names.get(map_key) or {}).get(signature) or "").strip()

    def rename_layout(self, map_key: str, signature: str, name: str) -> None:
        """给“模式版本(战区布局)”命名; 空名字 = 恢复自动编号。"""
        if not map_key or not signature:
            return
        with self._lock:
            names = self.layout_names.setdefault(map_key, {})
            name = (name or "").strip()
            if name:
                names[signature] = name
            else:
                names.pop(signature, None)
                if not names:
                    self.layout_names.pop(map_key, None)
        self.save()

    def layout_info(self, map_key: str, signature: str) -> dict | None:
        """取某个版本的布局(含战区列表/跨度), 供提示文字用。"""
        for layout in self.map_zones.get(map_key) or []:
            if str(layout.get("sig") or "") == signature:
                return layout
        return None

    def zone_layouts(self, map_key: str) -> list[dict]:
        """某地图已见过的战区布局(新的在前)。"""
        return list(self.map_zones.get(map_key) or [])

    def layout_for(self, map_key: str, signature: str = "") -> dict | None:
        """取某地图的战区布局: 优先精确匹配指纹, 否则用**最近见过**的那张。

        2026-10-22 修正: 回退原先取 `layouts[0]`, 但那只是“最后**新出现**的布局” ——
        `remember_zones` 只在新布局插入队首, 老布局被再次看到时仅刷新 `seen_at`,
        于是用户先录一张单路的“区域 1”再录 AB 队的“区域 2”(区域 2 历史上见过),
        回放 AB 队时却拿到“区域 1”的战区位置。改为按 `seen_at` 取最新的。
        """
        layouts = self.map_zones.get(map_key) or []
        if signature:
            for item in layouts:
                if item.get("sig") == signature:
                    return item
        if not layouts:
            return None
        # seen_at 是 `%Y-%m-%dT%H:%M:%S` 字串, 字典序即时间序
        return max(layouts, key=lambda it: str(it.get("seen_at") or ""))

    def grouped_by_mode(self) -> dict[str, list[tuple[str, list[RouteRecord]]]]:
        """{地图键: [(模式名, [轨迹...]), ...]} —— 侧栏三层树用。

        同一张图的不同模式分开成组(模式按组内最新一条的时间倒序),
        这样“同一张图跑不同模式的线路”不会混在一起。
        """
        out: dict[str, list[tuple[str, list[RouteRecord]]]] = {}
        for key, rows in self.grouped().items():
            buckets: dict[str, list[RouteRecord]] = {}
            order: list[str] = []
            for rec in rows:            # rows 已按时间倒序 → 最近跑的模式排前面
                name = rec.mode_label
                if name not in buckets:
                    buckets[name] = []
                    order.append(name)
                buckets[name].append(rec)
            out[key] = [(name, buckets[name]) for name in order]
        return out

    def grouped_by_variant(self) -> dict[str, list[tuple[str, list[tuple[str, str, list[RouteRecord]]]]]]:
        """{地图键: [(模式名, [(战区坐标串, 版本名, [轨迹...]), ...]), ...]}

        侧栏四层树(地图 → 模式 → 版本 → 轨迹)用。版本就是**战区坐标串**:
        同一个模式下的不同交战区域(大地图/小地图)或不同随机点位会自然分开。
        """
        out: dict[str, list[tuple[str, list[tuple[str, str, list[RouteRecord]]]]]] = {}
        for key, modes in self.grouped_by_mode().items():
            rows: list[tuple[str, list[tuple[str, str, list[RouteRecord]]]]] = []
            for mode_label, recs in modes:
                buckets: dict[str, list[RouteRecord]] = {}
                order: list[str] = []
                for rec in recs:        # recs 已按时间倒序 → 最近跑的版本排前面
                    sig = rec.zone_signature or ""
                    if sig not in buckets:
                        buckets[sig] = []
                        order.append(sig)
                    buckets[sig].append(rec)
                rows.append((mode_label,
                             [(sig, self.layout_display_name(key, sig), buckets[sig])
                              for sig in order]))
            out[key] = rows
        return out
