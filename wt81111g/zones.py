"""战区(`capture_zone`)与**重生位置**(`respawn_base_*`)的解析, 以及游戏模式判定。

8111 **不提供模式名**, 但 `/map_obj.json` 里每个战区都是一个
`type == "capture_zone"` 的对象, 自带字母、归属颜色与地图中心坐标(0~1 归一化):

    {"type": "capture_zone", "icon": "capture_zone", "color": "#174DFF",
     "x": 0.432927, "y": 0.556106,
     "zone_id": 0, "zone_label": "A", "zone_label_code": 65}

**重生位置**是同一份数据里的一批 `type == "respawn_base_tank"` 对象
(实测 2026-10-06: 一局 40~64 个, 双方各半)—— 游戏里"重生区域"就是这一堆候选位置,
玩家重生时从中随机分配一个; 颜色同样是站在玩家视角的归属色(蓝=我方, 红=敌方)。

实测(2026-10-06 陆战):

* **三战区局** → A/B/C 三个对象(即“战区控制”);
* **两战区局** → A/B 两个对象(即“战区攻陷”), 且两个战区**颜色不同**;
* `color` 是**站在玩家视角**的归属色, 全图对象同此约定:
  `#174DFF` 蓝 = 我方, `#fa0C00` 红 = 敌方, `#ffFFFA` 白 = 中立(未被占领)。
  (旁证: 双方的重生点/机场各 64/3 个对象, 恰好蓝红成对; `respawn_base_tank` 蓝
  的那批在己方后方, 红的那批在敌后。)

因此:

* **战区数量**决定模式(见 `MODE_NAMES`);
* **各战区中心坐标**构成稳定的“模式指纹” —— 同一张图的同一个模式, 战区位置固定,
  所以 `(地图图片指纹, 战区坐标串)` 就能把“同一张图的多种模式”区分开,
  多次跑图只要指纹相同就自动归到同一个模式组;
* **归属色**不进指纹(战局中会变: 攻陷模式就是双方各拿一个), 只用来在图上/状态栏
  显示“哪个战区是我们的”。

注意: 战区信息**只在战局内**有(战局外 `/map_obj.json` 是空数组)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# 战区数量 → 模式名(按用户口径):
#   3 个 = 战区控制(A/B/C 互相争夺)
#   2 个 = 战区攻陷(双方开局各一个, 目标是全部拿下)
#   1 个 = 战区争夺(单点争夺)
MODE_NAMES = {3: "战区控制", 2: "战区攻陷", 1: "战区争夺"}
UNKNOWN_MODE = "未知模式"

# 签名里坐标保留的小数位(4 位 ≈ 2048 图上的 0.2 像素, 足够区分战区且容忍浮点抖动)
_SIG_DIGITS = 4

# 8111 的归属色约定(站在玩家视角): 实测 2026-10-06
COLOR_OURS = "#174DFF"      # 蓝 = 我方
COLOR_ENEMY = "#fa0C00"     # 红 = 敌方
COLOR_NEUTRAL = "#ffFFFA"   # 白 = 未被占领
# 黄 = **自己** —— 也就是“摄像机挂着的那辆车”。注意: 地图对象里的 `icon` 不一定
# 同时是 `Player`(2026-10-22 实测: 同一个观战对象有时给 `icon=SAM`, 有时给
# `icon=Player`), 所以认“观战对象”要**颜色和 icon 两个线索都算**。
COLOR_SELF = "#faC81E"
COLOR_TEAM = "#67D756"      # 绿 = 队友(仅参考)

SIDE_OURS = "ours"
SIDE_ENEMY = "enemy"
SIDE_NEUTRAL = "neutral"

SIDE_LABELS = {SIDE_OURS: "我方", SIDE_ENEMY: "敌方", SIDE_NEUTRAL: "中立"}

# 重生位置: 8111 里 type/icon 以 respawn_base_ 开头(地面的是 respawn_base_tank)
SPAWN_PREFIX = "respawn_base_"
# 种类 → 中文名(只列见过的; 拿不到就用原名)
SPAWN_KIND_LABELS = {
    "tank": "地面载具",
    "fighter": "战斗机",
    "bomber": "轰炸机",
    "attack": "攻击机",
    "helicopter": "直升机",
    "ucav": "无人机",
}

# 闪烁(被占领中): 8111 的 blink 值 → 官方页面的脉冲周期
# (官方是拿它把图标在 color[] ↔ 纯黄之间插值; 本项目改成在**本阵营色**里做明暗脉冲)
BLINK_PERIODS = {1: 2.0, 2: 1.2}

_COLOR_TO_SIDE = {
    COLOR_OURS.lower(): SIDE_OURS,
    COLOR_ENEMY.lower(): SIDE_ENEMY,
    COLOR_NEUTRAL.lower(): SIDE_NEUTRAL,
}


@dataclass(frozen=True)
class Zone:
    """一个战区: 字母 + 地图归一化中心坐标 + 归属色 + 闪烁标志。

    `color` 保留 8111 原样字符串(如 `#174DFF`), `side` 是归一化后的归属
    (`ours`/`enemy`/`neutral`)。颜色**不进签名** —— 战局中归属会变。
    `blink` 是 8111 原样字段: 0 = 不闪, 1 = 普通闪(周期 2.0s),
    2 = 快闪(周期 1.2s); 实测值来自“正在被占领”的状态
    (官方页面就是拿它在归属色 ↔ 黄色之间插值)。
    """
    label: str
    x: float
    y: float
    color: str = COLOR_NEUTRAL
    side: str = SIDE_NEUTRAL
    blink: int = 0

    @property
    def side_label(self) -> str:
        return SIDE_LABELS.get(self.side, SIDE_LABELS[SIDE_NEUTRAL])

    @property
    def contested(self) -> bool:
        """是否正在被占领(8111 会闪)."""
        return self.blink > 0


def _parse_rgb(color: str) -> tuple[int, int, int] | None:
    """`#RRGGBB` / `#RGB` → (r, g, b); 解析不了返回 None。"""
    text = str(color or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return None
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return None


def side_of(color: str | None) -> str:
    """颜色 → 归属(`ours`/`enemy`/`neutral`)。

    先查实测过的精确值(`#174DFF` / `#fa0C00` / `#ffFFFA`); 查不到就**按通道**判断:
    蓝色明显占优 = 我方, 红色明显占优 = 敌方, 其余(白/黄/灰) = 中立。
    这样老版本或空战里的 `#185AFF` / `#fa3200` 也能认出来, 而玩家自己的黄色
    `#faC81E`(红绿都高)不会被误判成敌方。
    """
    text = str(color or "").strip().lower()
    exact = _COLOR_TO_SIDE.get(text)
    if exact is not None:
        return exact
    rgb = _parse_rgb(text)
    if rgb is None:
        return SIDE_NEUTRAL
    r, g, b = rgb
    if b > 120 and b >= max(r, g) * 2:
        return SIDE_OURS
    if r > 120 and r >= max(g, b) * 2:
        return SIDE_ENEMY
    return SIDE_NEUTRAL


def _color_of(obj: dict) -> str:
    """取对象的颜色字符串: 优先 `color`, 其次 `color[]` 数组(转回 #RRGGBB)。"""
    raw = obj.get("color")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    rgb = obj.get("color[]")
    if isinstance(rgb, (list, tuple)) and len(rgb) >= 3:
        try:
            r, g, b = (int(x) for x in rgb[:3])
            return f"#{r:02X}{g:02X}{b:02X}"
        except (TypeError, ValueError):
            pass
    return COLOR_NEUTRAL


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def zones_from_objects(objects) -> list[Zone]:
    """从 `/map_obj.json` 的对象列表里挑出战区, 按字母排序。"""
    out: list[Zone] = []
    for obj in objects or []:
        if not isinstance(obj, dict):
            continue
        if str(obj.get("type", "")).strip().lower() != "capture_zone":
            continue
        x, y = _to_float(obj.get("x")), _to_float(obj.get("y"))
        if x is None or y is None:
            continue
        label = str(obj.get("zone_label") or "").strip()
        if not label:      # 极少见: 没有字母时用 zone_id / 序号占位
            code = obj.get("zone_label_code")
            if isinstance(code, int) and 0 < code < 128:
                label = chr(code)
            else:
                label = str(obj.get("zone_id", len(out)))
        color = _color_of(obj)
        out.append(Zone(label=label, x=x, y=y, color=color, side=side_of(color),
                        blink=max(0, _to_int(obj.get("blink")))))
    out.sort(key=lambda z: (z.label, z.x, z.y))   # 字母序 → 签名稳定
    return out


def zone_state(zones) -> tuple:
    """战区**完整状态**(字母/坐标/归属/闪烁) —— 用来判断“该重画了”。

    与 `signature()` 的区别: 签名只看坐标(区分模式), 这里连归属也看 ——
    占领后坐标不变、只有颜色变, 只看签名就会漏掉刷新。
    """
    return tuple((z.label, round(z.x, _SIG_DIGITS), round(z.y, _SIG_DIGITS),
                  z.side, z.blink) for z in zones or [])


def ownership_text(zones) -> str:
    """归属概览, 如 `我方 B, 敌方 A(占领中)`(全中立时返回空串)。"""
    zones = list(zones or [])
    groups: dict[str, list[str]] = {}
    for z in zones:
        mark = "(占领中)" if z.contested else ""
        groups.setdefault(z.side, []).append(f"{z.label}{mark}")
    if not any(z.side != SIDE_NEUTRAL for z in zones):
        return ""
    parts = [f"{SIDE_LABELS[side]} {'/'.join(groups[side])}"
             for side in (SIDE_OURS, SIDE_ENEMY, SIDE_NEUTRAL) if groups.get(side)]
    return ", ".join(parts)


def mode_name(count: int) -> str:
    """战区数量 → 模式名(未知数量时给出可读的兜底文案)。"""
    if count in MODE_NAMES:
        return MODE_NAMES[count]
    if count <= 0:
        return UNKNOWN_MODE
    return f"{count} 战区"


def signature(zones) -> str:
    """战区坐标串(模式指纹, **不含归属色**)。空列表返回空串。"""
    return ";".join(f"{z.label}@{z.x:.{_SIG_DIGITS}f},{z.y:.{_SIG_DIGITS}f}"
                    for z in zones)


def layout_spread(zones) -> float:
    """布局跨度: 各战区中心之间的**最大距离**(0~1 地图单位)。

    用途: 同一个模式(同样的战区数量)会有多个“版本”, 典型就是**大地图/小地图**
    —— 交战区越大, 战区之间分得越开。跨度大的一般就是大地图版本。
    接受 Zone 或普通字典(持久化后的布局格式)。
    """
    pts: list[tuple[float, float]] = []
    for z in zones or []:
        try:
            x = float(z["x"] if isinstance(z, dict) else z.x)
            y = float(z["y"] if isinstance(z, dict) else z.y)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        pts.append((x, y))
    best = 0.0
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            best = max(best, math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1]))
    return best


def zones_from_signature(sig: str) -> list[Zone]:
    """指纹(`A@0.4329,0.5561;B@x,y`) → Zone 列表。

    指纹里就带着字母与坐标, 所以旧数据(轨迹有指纹、但 `map_zones` 没存过布局)
    也能把战区位置还原出来。
    """
    out: list[Zone] = []
    for part in (sig or "").split(";"):
        label, at, coords = part.partition("@")
        xs, comma, ys = coords.partition(",")
        if not at or not comma:
            continue
        x, y = _to_float(xs), _to_float(ys)
        if x is None or y is None:
            continue
        out.append(Zone(label=label.strip() or "?", x=x, y=y))
    return out


def zones_to_json(zones) -> list[dict]:
    """Zone 列表 → 可绘制的普通字典列表(带颜色/归属/闪烁)。"""
    return [{"label": z.label, "x": round(z.x, _SIG_DIGITS), "y": round(z.y, _SIG_DIGITS),
             "color": z.color, "side": z.side, "blink": z.blink}
            for z in zones]


def zones_from_json(data) -> list[Zone]:
    """持久化数据 → Zone 列表(容忍损坏条目)。"""
    out: list[Zone] = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        x, y = _to_float(item.get("x")), _to_float(item.get("y"))
        if x is None or y is None:
            continue
        color = str(item.get("color") or COLOR_NEUTRAL)
        out.append(Zone(label=str(item.get("label") or "?"), x=x, y=y,
                        color=color, side=side_of(color),
                        blink=max(0, _to_int(item.get("blink")))))
    return out


# --------------------------------------------------------------------- 重生位置
@dataclass(frozen=True)
class Spawn:
    """一个重生**候选位置**: 种类 + 归属 + 地图归一化坐标。

    8111 里地面重生点是一批 `respawn_base_tank` 对象 —— 游戏里"重生区域"就是
    这一堆位置, 玩家重生时从中随机分配一个, 所以它们**成片出现**而不是一个点。
    坐标随模式/版本变(同一张图的"区域1/区域2"重生位置不同), 所以跟着布局一起存。
    """
    kind: str
    x: float
    y: float
    color: str = COLOR_NEUTRAL
    side: str = SIDE_NEUTRAL

    @property
    def side_label(self) -> str:
        return SIDE_LABELS.get(self.side, SIDE_LABELS[SIDE_NEUTRAL])

    @property
    def kind_label(self) -> str:
        return SPAWN_KIND_LABELS.get(self.kind, self.kind or "重生点")

    @property
    def ground(self) -> bool:
        """是不是地面载具的重生点(地图上画红/蓝空心方块的就是这批)。"""
        return self.kind == "tank"


def _spawn_kind(obj: dict) -> str:
    """从 type/icon 里取重生点种类(去掉 `respawn_base_` 前缀); 不是重生点返回空串。"""
    for field in ("type", "icon"):
        raw = str(obj.get(field) or "").strip().lower()
        if raw.startswith(SPAWN_PREFIX):
            return raw[len(SPAWN_PREFIX):] or "?"
    return ""


def spawns_from_objects(objects) -> list[Spawn]:
    """从 `/map_obj.json` 的对象列表里挑出重生位置, 排序后返回(顺序稳定)。"""
    out: list[Spawn] = []
    for obj in objects or []:
        if not isinstance(obj, dict):
            continue
        kind = _spawn_kind(obj)
        if not kind:
            continue
        x, y = _to_float(obj.get("x")), _to_float(obj.get("y"))
        if x is None or y is None:
            continue
        color = _color_of(obj)
        out.append(Spawn(kind=kind, x=x, y=y, color=color, side=side_of(color)))
    out.sort(key=lambda s: (s.side, s.kind, round(s.x, _SIG_DIGITS), round(s.y, _SIG_DIGITS)))
    return out


def ground_spawns(spawns) -> list[Spawn]:
    """只取**地面载具**的重生位置(地图上画方块的就是它们)。"""
    return [s for s in spawns or [] if s.ground]


def spawn_state(spawns) -> tuple:
    """重生点完整状态(归属/种类/坐标) —— 用来判断“该重画了/该存盘了”。"""
    return tuple((s.side, s.kind, round(s.x, _SIG_DIGITS), round(s.y, _SIG_DIGITS))
                 for s in spawns or [])


def spawns_to_json(spawns) -> list[dict]:
    """Spawn 列表 → 可绘制的普通字典列表(位置/归属/种类)。"""
    return [{"kind": s.kind, "x": round(s.x, _SIG_DIGITS), "y": round(s.y, _SIG_DIGITS),
             "side": s.side} for s in spawns]


def spawns_from_json(data) -> list[Spawn]:
    """持久化数据 → Spawn 列表(容忍损坏条目)。"""
    out: list[Spawn] = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        x, y = _to_float(item.get("x")), _to_float(item.get("y"))
        if x is None or y is None:
            continue
        side = str(item.get("side") or SIDE_NEUTRAL)
        color = {SIDE_OURS: COLOR_OURS, SIDE_ENEMY: COLOR_ENEMY}.get(side, COLOR_NEUTRAL)
        out.append(Spawn(kind=str(item.get("kind") or "tank"), x=x, y=y,
                         color=color, side=side))
    return out


def spawn_summary(spawns) -> str:
    """概览文案, 如 `我方 12 · 敌方 12 (地面载具)`; 空列表返回空串。"""
    spawns = list(spawns or [])
    if not spawns:
        return ""
    parts = [f"{SIDE_LABELS[side]} {sum(1 for s in spawns if s.side == side)}"
             for side in (SIDE_OURS, SIDE_ENEMY, SIDE_NEUTRAL)
             if any(s.side == side for s in spawns)]
    kinds = "/".join(sorted({s.kind_label for s in spawns}))
    return f"{' · '.join(parts)} ({kinds})"
