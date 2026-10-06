"""战区(`capture_zone`)解析与游戏模式判定。

8111 **不提供模式名**, 但 `/map_obj.json` 里每个战区都是一个
`type == "capture_zone"` 的对象, 自带字母、归属颜色与地图中心坐标(0~1 归一化):

    {"type": "capture_zone", "icon": "capture_zone", "color": "#174DFF",
     "x": 0.432927, "y": 0.556106,
     "zone_id": 0, "zone_label": "A", "zone_label_code": 65}

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

SIDE_OURS = "ours"
SIDE_ENEMY = "enemy"
SIDE_NEUTRAL = "neutral"

SIDE_LABELS = {SIDE_OURS: "我方", SIDE_ENEMY: "敌方", SIDE_NEUTRAL: "中立"}

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


def side_of(color: str | None) -> str:
    """颜色 → 归属(`ours`/`enemy`/`neutral`); 未知颜色当**中立**处理。"""
    return _COLOR_TO_SIDE.get(str(color or "").strip().lower(), SIDE_NEUTRAL)


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
