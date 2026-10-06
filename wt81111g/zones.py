"""战区(`capture_zone`)解析与游戏模式判定。

8111 **不提供模式名**, 但 `/map_obj.json` 里每个战区都是一个
`type == "capture_zone"` 的对象, 自带字母与地图中心坐标(0~1 归一化):

    {"type": "capture_zone", "icon": "capture_zone",
     "x": 0.432927, "y": 0.556106,
     "zone_id": 0, "zone_label": "A", "zone_label_code": 65}

实测(2026-10-06 陆战, 三战区局): 返回 A/B/C 三个对象, 坐标互不相同。
因此:

* **战区数量**决定模式(见 `MODE_NAMES`);
* **各战区中心坐标**构成稳定的"模式指纹" —— 同一张图的同一个模式, 战区位置固定,
  所以 `(地图图片指纹, 战区坐标串)` 就能把"同一张图的多种模式"区分开,
  多次跑图只要指纹相同就自动归到同一个模式组。

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


@dataclass(frozen=True)
class Zone:
    """一个战区: 字母 + 地图归一化中心坐标。"""
    label: str
    x: float
    y: float


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
        out.append(Zone(label=label, x=x, y=y))
    out.sort(key=lambda z: (z.label, z.x, z.y))   # 字母序 → 签名稳定
    return out


def mode_name(count: int) -> str:
    """战区数量 → 模式名(未知数量时给出可读的兜底文案)。"""
    if count in MODE_NAMES:
        return MODE_NAMES[count]
    if count <= 0:
        return UNKNOWN_MODE
    return f"{count} 战区"


def signature(zones) -> str:
    """战区坐标串(模式指纹)。空列表返回空串。"""
    return ";".join(f"{z.label}@{z.x:.{_SIG_DIGITS}f},{z.y:.{_SIG_DIGITS}f}"
                    for z in zones)


def zones_to_json(zones) -> list[dict]:
    """Zone 列表 → 可持久化的普通字典列表。"""
    return [{"label": z.label, "x": round(z.x, _SIG_DIGITS), "y": round(z.y, _SIG_DIGITS)}
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
        out.append(Zone(label=str(item.get("label") or "?"), x=x, y=y))
    return out
