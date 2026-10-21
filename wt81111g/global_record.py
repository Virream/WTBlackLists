"""全局录制: 从每帧 `/map_obj.json` 拼出“每辆车的线路”(多目标跟踪)。

**为什么需要跟踪**: 8111 的地图对象里**没有 id、没有玩家名**, 只有
`type`(如 `ground_model`) / `icon`(如 `MediumTank`) / `color` / `x`,`y`。
所以“同一个单位”只能靠**帧间最近邻 + 贪心匹配**推测:

* 每个已存在的线路, 在同一 `type` 的单位里找**距离最近**的那个接上;
* 没接上的单位 → 新开一条线(重生 / 刚从视野外进来);
* 一条线连续 `END_GAP` 秒都没接上 → 结束(见下面“被击毁”的口径)。

**怎么判定“被击毁”** (2026-10-21 用户口径): 不关心是哪个玩家、也不关心是哪条线 ——
只看 8111: 这条线对应的 icon **消失 = 这辆车没了 = 被击毁**(用户原话:
“在 8111 中玩家对应的 icon 消失就是死了”)。判定实现上多一道闸:
线断的那一刻场上**还有其它可录单位**才算击毁(记为 `UnitLife.dead`);
**全场一起消失**(回到车库/录制自动结束) 不算 —— 用户特意提过
“pro 能做到整局一次不死”, 那种情况不能画击毁标记。

为什么“断线”可以放心当成“被击毁”(用户 2026-10-21 补充说明):

* 玩家**一次只能操作一台载具** → 想换成固定翼(如 Su-25)必须先让地面坦克阵亡,
  所以“地面 icon 消失”不可能只是“跑去开飞机了”;
* 玩家**退出对局**后载具停在原地, 8111 过一会儿就不再上报 → 也算击毁(符合预期);
* 只有两种例外会让“坦克本体还活着而玩家不在车里”: **微型侦察无人机**与
  **核弹机**(需 2500 重生点)。无人机那条线不会断(本体仍作为 `ground_model` 上报),
  而且我们**只录地面单位**, 所以两类例外都不影响判定;
* 救回来重生的会重新出现 → 算**新的一条线**(与用户确认: 不用管谁击杀了谁)。

**已知取舍**(与用户确认过):
* 两个人贴得极近时可能互换身份(位置本来就分不出来), 概率很低, 且不影响“线路形状”;
* 不记朝向(用户要求: 全局录制忽略朝向), 只记 `(t, x, y)` + 自己算的速度。

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


# ------------------------------------------------------------ 坐标系对齐
# 8111 的 `/map_obj.json` 坐标是相对**当前观战视角那张地图**的: 观战中的玩家上了
# 固定翼(如 Su-25)时, 8111 会把地图切成空战图, 同一批地面单位的坐标整体被
# 缩放/平移, 于是帧间最近邻匹配对不上 —— 正在画的线会突然断掉、换个地方重新开始
# (2026-10-21 用户实测)。
#
# 解法: 用**静态地物当锚点**把坐标换算回“录制开始时的地面坐标系”。
# `respawn_base_tank` / `capture_zone` 在两个视角里都会上报, 是**同一批点**,
# 只是坐标整体做了相似变换(等比缩放 + 平移, 可能还有 90° 的整数倍旋转);
# 用质心 + 半径解出变换, 再用最近邻核对, 对不上就不认。
ANCHOR_TYPES = frozenset({"respawn_base_tank", "capture_zone"})
# 拟合后允许的平均残差(归一化坐标 = 地图边长的倍数); 超了就认为不是同一个坐标系
FRAME_FIT_TOL = 0.01
# 初步配对时允许的距离(初始尺度可能略有偏差, 给宽一点)
FRAME_MATCH_TOL = 0.02
# 判定“两边坐标系其实一样”的门槛: 尺度差 < 1% 且质心偏移 < 0.2%
FRAME_SAME_SCALE = 0.01
FRAME_SAME_SHIFT = 0.002
# 换视角(坐标系变化)后这么多秒内**不判“被击毁”**:
# 那段时间里单位消失更可能是“视角换了看不见了”, 不是被打死
VIEW_CHANGE_GRACE = 5.0


def frame_anchors(objects) -> list[tuple[float, float]]:
    """取这一帧的**静态锚点**(重生点 + 战区中心)的坐标, 用来判断坐标系有没有变。

    这些点在陆战图/空战图里都会上报, 而且位置固定 —— 坐标系一变, 它们的
    相对布局就会被整体缩放/平移, 正好可以反解出变换。
    """
    out: list[tuple[float, float]] = []
    for obj in objects or []:
        if not isinstance(obj, dict):
            continue
        if str(obj.get("type") or "") not in ANCHOR_TYPES:
            continue
        x, y = _f(obj.get("x")), _f(obj.get("y"))
        if x is None or y is None:
            continue
        out.append((x, y))
    return out


def _centroid(pts) -> tuple[float, float]:
    n = float(len(pts))
    return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n)


def _rms(pts, c: tuple[float, float]) -> float:
    """到质心的均方根半径(与旋转无关, 用来解缩放比)。"""
    n = float(len(pts))
    return math.sqrt(sum((p[0] - c[0]) ** 2 + (p[1] - c[1]) ** 2 for p in pts) / n)


def _nn_median(pts, cap: int = 64) -> float:
    """最近邻距离的**中位数**(抗离群点) —— 用来估初始缩放比。

    质心/bbox 那两个估计会被少数“孤零零”的点(如 3 个战区中心)拽偏;
    最近邻距离看的是点集的**密集程度**, 只要两个视角报的是同一批重生点,
    即使一边多报/少报了几个, 这个比值仍然准。
    """
    sub = list(pts)[:: max(1, len(pts) // cap)] if len(pts) > cap else list(pts)
    ds: list[float] = []
    for i, p in enumerate(sub):
        best = None
        for j, q in enumerate(sub):
            if i == j:
                continue
            d = (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2
            if best is None or d < best:
                best = d
        if best is not None:
            ds.append(math.sqrt(best))
    ds.sort()
    return ds[len(ds) // 2] if ds else 0.0


def _rot(dx: float, dy: float, k: int) -> tuple[float, float]:
    for _ in range(k % 4):
        dx, dy = -dy, dx
    return (dx, dy)


@dataclass(frozen=True)
class FrameFit:
    """一层坐标变换: `p' = scale × R_rot(p − c) + o`(相似变换)。

    * `c` = **当前帧**锚点的质心, `o` = 变换后的落点(参考帧里),
    * `rot` = 90° 的整数倍(0~3), 正常情况是 0;
    * `error` = 拟合后“当前锚点 → 参考锚点”的平均最近邻距离(归一化坐标)。
    """
    scale: float = 1.0
    rot: int = 0
    cx: float = 0.0
    cy: float = 0.0
    ox: float = 0.0
    oy: float = 0.0
    error: float = 0.0

    def apply(self, x: float, y: float) -> tuple[float, float]:
        dx, dy = _rot(x - self.cx, y - self.cy, self.rot)
        return (self.ox + dx * self.scale, self.oy + dy * self.scale)

    @property
    def same_as_reference(self) -> bool:
        """当前坐标系和参考坐标系是否其实一样(变换可忽略)。"""
        return (self.rot == 0
                and abs(self.scale - 1.0) < FRAME_SAME_SCALE
                and math.hypot(self.ox - self.cx, self.oy - self.cy) < FRAME_SAME_SHIFT)

    def describe(self) -> str:
        if self.same_as_reference:
            return "同坐标系"
        px, py = self.apply(0.0, 0.0)
        return (f"scale={self.scale:.4f} rot={self.rot * 90}° "
                f"原点→({px:+.4f},{py:+.4f}) 残差={self.error:.5f}")


def fit_frame(reference, current) -> FrameFit | None:
    """求“当前帧坐标 → 参考帧坐标”的相似变换(解不出来就返回 None)。

    做法: 质心对齐 + 均方根半径定尺度(与旋转无关) → 4 个 90° 旋转各试一次,
    每次用最近邻找配对、再对配对做**最小二乘**(只解缩放 + 平移, 旋转已定) →
    取残差最小的那个; 残差仍太大就拒绝(宁可这一帧不换算, 也不能乱画)。
    """
    if len(reference) < 4 or len(current) < 4:
        return None
    cr = _centroid(reference)
    cc = _centroid(current)
    rr = _rms(reference, cr)
    rc = _rms(current, cc)
    if rr <= 0.0 or rc <= 0.0:
        return None
    # 初始尺度**多给几个候选**: 两个视角报的锚点数量/构成可能略有差别(例如空战图
    # 少报了几个重生点), 单靠质心半径会被那几个“孤零零”的点拽偏 → 配对失败。
    # 质心半径 / bbox 长边 / bbox 短边 各算一个, 谁配上的点多就用谁。
    ext_r = (max(p[0] for p in reference) - min(p[0] for p in reference),
             max(p[1] for p in reference) - min(p[1] for p in reference))
    ext_c = (max(p[0] for p in current) - min(p[0] for p in current),
             max(p[1] for p in current) - min(p[1] for p in current))
    cands = [rr / rc]
    for a_ref, a_cur in ((ext_r[0], ext_c[0]), (ext_r[1], ext_c[1]),
                         (max(ext_r), max(ext_c)), (min(ext_r), min(ext_c))):
        if a_ref > 0.0 and a_cur > 0.0:
            cands.append(a_ref / a_cur)
    nn_r, nn_c = _nn_median(reference), _nn_median(current)
    if nn_r > 0.0 and nn_c > 0.0:
        cands.append(nn_r / nn_c)
    scale_cands = sorted({round(c, 6) for c in cands if c > 0.0})
    step = max(1, len(current) // 32)         # 只拿约 32 个点核对
    sample = list(current)[::step]
    best: FrameFit | None = None
    for k in range(4):
        # ① 只做旋转(绕当前质心) —— 之后只剩“缩放 + 平移”要解
        rotated = [_rot(p[0] - cc[0], p[1] - cc[1], k) for p in sample]
        for scale0 in scale_cands:
            pairs: list[tuple[tuple[float, float], tuple[float, float]]] = []
            for qx, qy in rotated:
                # 用这个候选尺度投到参考坐标系里找最近的锚点
                tx, ty = cr[0] + scale0 * qx, cr[1] + scale0 * qy
                nd = None
                near = None
                for rx, ry in reference:
                    d = (tx - rx) ** 2 + (ty - ry) ** 2
                    if nd is None or d < nd:
                        nd, near = d, (rx, ry)
                if near is not None and nd is not None and nd <= FRAME_MATCH_TOL ** 2:
                    pairs.append(((qx, qy), near))
            if len(pairs) < 4:
                continue
            # ② 最小二乘: p' = s·q + t
            cq = _centroid([q for q, _ in pairs])
            cm = _centroid([r for _, r in pairs])
            num = sum((q[0] - cq[0]) * (r[0] - cm[0]) + (q[1] - cq[1]) * (r[1] - cm[1])
                      for q, r in pairs)
            den = sum((q[0] - cq[0]) ** 2 + (q[1] - cq[1]) ** 2 for q, _ in pairs)
            scale = scale0 if den <= 0 else max(0.01, num / den)
            fit = FrameFit(scale=scale, rot=k, cx=cc[0], cy=cc[1],
                           ox=cm[0] - scale * cq[0], oy=cm[1] - scale * cq[1])
            # ③ 量残差(平均最近邻距离); 没配上的点会把残差拉大 → 自然淘汰
            tot = 0.0
            for px, py in sample:
                qx, qy = fit.apply(px, py)
                tot += math.sqrt(min((qx - rx) ** 2 + (qy - ry) ** 2
                                     for rx, ry in reference))
            fit = FrameFit(scale=scale, rot=k, cx=cc[0], cy=cc[1],
                           ox=fit.ox, oy=fit.oy, error=tot / len(sample))
            if best is None or fit.error < best.error:
                best = fit
    if best is None or best.error > FRAME_FIT_TOL:
        return None
    return best


class FrameAligner:
    """记住“参考坐标系”(录制开始时的地面图)并给出每一帧的换算方式。"""

    def __init__(self) -> None:
        self.reference: list[tuple[float, float]] = []
        self._cache: dict[tuple, FrameFit | None] = {}
        self.fit: FrameFit | None = None        # 当前帧用的变换(None = 还没算出来)

    @property
    def ready(self) -> bool:
        return len(self.reference) >= 4

    def set_reference(self, anchors) -> bool:
        """把这一帧的锚点当作参考坐标系(录制开始时调用一次)。"""
        pts = list(anchors or [])
        if len(pts) < 4:
            return False
        self.reference = pts
        self._cache.clear()
        self.fit = FrameFit()                   # 参考坐标系 = 恒等变换
        return True

    @staticmethod
    def signature(anchors) -> tuple:
        """锚点布局的**廉价**指纹: bbox + 质心 + 点数。

        坐标系一变(被缩放/平移)这几个数就变 → 只有变了才重新拟合,
        稳态下每帧只花几十微秒。
        """
        if not anchors:
            return ()
        xs = [p[0] for p in anchors]
        ys = [p[1] for p in anchors]
        n = float(len(anchors))
        return (len(anchors), round(min(xs), 4), round(max(xs), 4),
                round(min(ys), 4), round(max(ys), 4),
                round(sum(xs) / n, 4), round(sum(ys) / n, 4))

    def update(self, anchors) -> FrameFit | None:
        """按这一帧的锚点给出变换(参考坐标系没建立时返回 None)。"""
        if not self.ready:
            return None
        sig = self.signature(anchors)
        if not sig:
            return self.fit                 # 这帧没有锚点 → 沿用上一帧的变换
        got = self._cache.get(sig, "miss")
        if got == "miss":
            got = fit_frame(self.reference, anchors)
            self._cache[sig] = got
            if len(self._cache) > 64:       # 防御: 缓存不要无限长
                self._cache.clear()
        if got is not None:
            self.fit = got
        return self.fit


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
        # 坐标系对齐(陆战图 ↔ 空战图): 参考坐标系 = 录制开始时那一帧的静态锚点
        self.aligner = FrameAligner()
        self._view_changed_at: float | None = None      # 上一次“坐标系变了”的时刻
        self.frame_changes = 0                          # 换算过多少个不同坐标系(日志用)
        # 这一帧 /map_info.json 报的图框尺寸(米) —— **只用来交叉校验**拟合尺度:
        # 知识库(Dagor/8111)的口径是“归一化 × 图框 = 世界米”, 两个视角只是图框不同
        # (坦克图框 4096 m / 空中图框 65536 m → 比值 16), 所以拟合出的尺度应该与
        # “当前图框 ÷ 参考图框” 一致。不一致 → 要么锚点拟合错了, 要么地图信息不可信。
        self.seen_extent: tuple[float, float] | None = None

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
        self._align(t, objects, units)
        self._match(t, units)
        # 这一帧还有可录单位 → 此时断掉的线是“被击毁”; 全场空 → 是回到车库/录制结束
        self._expire(t, alive=bool(units), allow_dead=self._dead_allowed(t))
        if not units:
            if self._empty_since is None:
                self._empty_since = t
            return (t - self._empty_since) >= EMPTY_STOP
        self._empty_since = None
        return False

    # ---------------------------------------------------------- 坐标系对齐
    def _align(self, t: float, objects, units: list[Unit]) -> None:
        """把这一帧的单位坐标换算回**录制开始时的地面坐标系**。

        观战中的玩家上了固定翼时 8111 会切成空战图, 同一批地面单位的坐标整体被
        缩放/平移 —— 不换算的话, 帧间最近邻会全部脱靶, 正在画的线会突然断掉、
        换个位置重新开始(2026-10-21 用户实测)。
        """
        anchors = frame_anchors(objects)
        if not self.aligner.ready:
            if self.aligner.set_reference(anchors):
                log.info("全局录制: 坐标系基准已建立(%d 个静态锚点)", len(anchors))
            return
        before = self.aligner.fit
        fit = self.aligner.update(anchors)
        if fit is None:
            return                          # 拟合不出来 → 这一帧先不动(保持上一帧的变换)
        if before is not None and fit is not before and not fit.same_as_reference:
            self._view_changed_at = t
            self.frame_changes += 1
            log.info("全局录制: 坐标系变化(视野切到空中单位?) → %s %s",
                     fit.describe(), self._cross_check(fit))
        elif before is not None and before is not fit and fit.same_as_reference \
                and not before.same_as_reference:
            self._view_changed_at = t
            log.info("全局录制: 坐标系已恢复(%s)", fit.describe())
        if fit.same_as_reference:
            return
        for u in units:
            u.x, u.y = fit.apply(u.x, u.y)
        # 换算后再按参考坐标系判一次范围(空战图里机场 AI 防空的坐标在 2.9~3.4)
        units[:] = [u for u in units if 0.0 <= u.x <= 1.0 and 0.0 <= u.y <= 1.0]

    def _cross_check(self, fit: FrameFit) -> str:
        """交叉校验: 拟合出的尺度 vs `/map_info.json` 的图框比。

        8111/Dagor 的口径是“归一化坐标 × 图框 = 世界米”(知识库 07 章 §4.4),
        所以把两个视角的图框一除, 就该等于拟合出的尺度 —— 两个数字对得上,
        “坐标换算对不对”就是一个能一眼确认的事实。

        对不上时不报错, 只写日志(可能只是 map_info 还没跟上换图的那一瞬间)。
        """
        seen = self.seen_extent
        ref = self.map_size
        if not seen or ref[0] <= 0.0 or seen[0] <= 0.0:
            return "| map_info 图框未知, 无法交叉校验"
        expect = seen[0] / ref[0]
        diff = abs(fit.scale - expect) / expect
        if diff <= 0.02:
            verdict = "与拟合一致"
        elif diff <= 0.2:
            verdict = "偏差较大, 注意核对"
        else:
            verdict = "不一致"
        return (f"| map_info 图框 {seen[0]:.0f}/{ref[0]:.0f} = {expect:.4f}× "
                f"→ {verdict}(差 {diff * 100:.2f}%)")

    def _dead_allowed(self, t: float) -> bool:
        """现在这个时刻能不能判“被击毁”。

        刚换过视角(坐标系变了)的几秒内**不判** —— 那段时间单位消失更可能是
        “从这个视角看不见了”, 不是被打死(否则会满屏错误红叉)。
        """
        if self._view_changed_at is None:
            return True
        return (t - self._view_changed_at) > VIEW_CHANGE_GRACE

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

    def _expire(self, t: float, alive: bool = False, allow_dead: bool = True) -> None:
        """连续 `end_gap` 秒没接上的线 → 结束。

        `alive` = 这一帧场上还有别的可录单位 → 断掉的线判为**被击毁**;
        全场为空(回到车库/录制结束) 则只算“线到此为止”, 不标击毁。
        `allow_dead` = 现在能不能判击毁(刚换过视角的那几秒不能, 见 `_dead_allowed`)。
        """
        keep: list[_Live] = []
        for lv in self._live:
            if (t - lv.last_seen) > self.end_gap:
                self._close(lv, dead=alive and allow_dead)
            else:
                keep.append(lv)
        self._live = keep
