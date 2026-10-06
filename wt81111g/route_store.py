"""跑图线路记录持久化。

8111 **不提供地图名称**(只有几何数据与地图图片), 因此用地图图片内容的
指纹(sha1 前 12 位)作为地图唯一键; 显示名默认自动编号, 支持用户重命名。

数据文件: <data_dir>/routes.json
结构:
{
  "version": 1,
  "map_names": {"<地图指纹>": "地图名"},
  "routes": [ {路线记录}, ... ]
}
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

from .config import data_dir

log = logging.getLogger("route_store")


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
PLACEHOLDER_MAP_SHA1 = "ab516d02191d001d809253c346e8e3f93e13f136"
PLACEHOLDER_MAP_KEY = PLACEHOLDER_MAP_SHA1[:12]


@dataclass
class RoutePoint:
    """轨迹采样点。"""
    x: float
    y: float
    t: float          # 相对录制开始的秒数
    speed: float      # 当时速度 km/h


@dataclass
class RouteMark:
    """标记点 / 起点 / 终点。"""
    x: float
    y: float
    t: float          # 相对录制开始的秒数
    kind: str = "mark"   # start | mark | stop


@dataclass
class RouteRecord:
    """一次完整的跑图录制。"""
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

    @property
    def label(self) -> str:
        """侧栏二级条目显示的文本。

        不重复带上用时 —— 侧栏已经有专门的“用时”列了, 重复前缀会挤掉车型名。
        """
        ts = self.created_at[11:16] if len(self.created_at) >= 16 else ""
        return f"{ts}  {self.vehicle}".strip()


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
    return RoutePoint(x=a.x + (b.x - a.x) * k,
                      y=a.y + (b.y - a.y) * k,
                      t=t,
                      speed=a.speed + (b.speed - a.speed) * k)


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
        # 见过的地图键(含**只缓存了图片、还没录过轨迹**的地图):
        # 侧栏与「地图管理」的默认编号都基于它, 保证两边一致且稳定
        self.known_maps: list[str] = []
        self.routes: list[RouteRecord] = []
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            if not os.path.exists(self.path):
                return
            # utf-8-sig: 容忍带 BOM 的文件(用记事本/部分编辑器改过就会带 BOM)。
            # 若按 utf-8 读会直接抛异常 → 记录全丢, 下一次 save() 还会把文件覆盖掉。
            with open(self.path, encoding="utf-8-sig") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            names = data.get("map_names")
            if isinstance(names, dict):
                self.map_names = {str(k): str(v) for k, v in names.items()}
            known = data.get("known_maps")
            if isinstance(known, list):
                self.known_maps = [str(k) for k in known if str(k)]
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
                    ))
                except (KeyError, TypeError, ValueError) as exc:
                    log.warning("跳过损坏的线路记录: %s", exc)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("读取线路数据失败: %s", exc)

    def save(self) -> None:
        """原子写盘(tmp + os.replace), 强杀不残留半截文件。"""
        with self._lock:
            payload = {
                "version": 1,
                "map_names": self.map_names,
                "known_maps": self.ordered_map_keys(),
                "routes": [
                    {
                        **{k: v for k, v in asdict(r).items()
                           if k not in ("points", "marks")},
                        "points": [asdict(p) for p in r.points],
                        "marks": [asdict(m) for m in r.marks],
                    }
                    for r in self.routes
                ],
            }
            tmp = self.path + ".tmp"
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except OSError as exc:
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

    def ordered_map_keys(self) -> list[str]:
        """按首次见到顺序返回全部地图键(轨迹里的 + 只缓存了图片的), 保证顺序稳定。"""
        keys = [k for k in self.known_maps]
        for r in self.routes:
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
