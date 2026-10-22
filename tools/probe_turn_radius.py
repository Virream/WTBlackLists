# -*- coding: utf-8 -*-
"""转弯半径相关数据的**真实录制**质量分析(开发者工具, 不参与打包)。

用途: 每次新录一批轨迹后, 想知道"这批数据够不够干净、半径能信到什么程度"就跑它。
只读项目自己的 routes.json, **不碰任何游戏文件**。

它检查四件事:
  1. 采样间隔 dt —— 决定半径窗口该取多少秒(以及有没有掉帧的大间隔);
  2. `/indicators` 的 speed 与"位置差分速度"是否一致 —— 若加速时比值 >1、减速时 <1,
     说明速度有滞后(切屏/后台会让 8111 变慢);比值恒为某个数(如 0.98)则只是
     地图边长常数略偏, 不是噪声;
  3. 朝向抖动(二阶差分角)—— 转弯半径误差的主来源;
  4. 朝向角变化率 ω_dir 与位移航向变化率 ω_pos 的差 —— 两个独立信源的交叉验证。

用法:
    python tools/probe_turn_radius.py                  # 默认读打包目录里的 routes.json
    python tools/probe_turn_radius.py <routes.json>    # 指定数据文件
    python tools/probe_turn_radius.py data/routes.json # 源码运行时数据在仓库根 data/

输出解读:
    * dt 中位数应接近你设的采样间隔(如 40ms/80ms);出现 1s 以上的 max 说明掉过帧;
    * 速度比值 p50 在 0.95~1.05、且"加速/减速"两列差不多 → 速度没有滞后, 可信;
    * 朝向抖动 p90 超过 2~3° → 半径会跟着抖, 该考虑加大窗口;
    * 只有带 dx/dy 的轨迹才能算半径(老数据/全局录制没有朝向, 会被跳过)。
"""
import gzip
import io
import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT = os.path.join(ROOT, "dist", "WTRouteTester", "data", "routes.json")

# 各图地图边长(米), 只用于把归一化位移换算成米来核对速度量级。
# 取自 /map_info.json 的 map_max - map_min(录制日志里的“速度标定”行也记了这个值)。
# 没列到的地图按 4000m 估算 —— 比值会整体偏移一个常数, 不影响“有没有滞后”的判断。
MAP_M = {
    "c981952c2f95": 4000.0,      # 富尔达
    "bd2fe7194004": 4000.0,      # 布雷斯劳
    "372ce29b0767": 4000.0,      # 货运港口
    "a562a195841b": 4000.0,      # 第二次阿拉曼战役
    "fd837a9e6fff": 2000.0,      # 试车场
}
DEFAULT_MAP_M = 4000.0


def pct(vals, p):
    """分位数(输入不必有序)。"""
    if not vals:
        return float("nan")
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def unwrap_delta(a0, a1):
    """两个角度之间的最短角差(处理 ±π 跨越)。"""
    d = a1 - a0
    while d > math.pi:
        d -= 2 * math.pi
    while d < -math.pi:
        d += 2 * math.pi
    return d


def load_routes(path: str) -> dict:
    """读数据文件(2026-10-22 起是 gzip 压缩的 JSON, 老文件是明文, 都支持)。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:2] == b"\x1f\x8b":            # gzip 魔数
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8-sig"))


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    if not os.path.exists(path):
        print("找不到数据文件: %s" % path)
        print("用法: python tools/probe_turn_radius.py [routes.json]")
        return 2
    data = load_routes(path)
    routes = [r for r in data.get("routes") or [] if r.get("points")]
    print("=== 轨迹清单 (%s) ===" % path)
    for r in routes:
        pts = r["points"]
        ndir = sum(1 for p in pts if p.get("dx") or p.get("dy"))
        print("  %-14s %-22s n=%-5d dur=%6.1fs 有朝向=%3d%% %s"
              % (r["map_key"], (r.get("vehicle") or "?")[:22], len(pts),
                 r.get("duration") or 0.0, round(100.0 * ndir / len(pts)),
                 (r.get("created_at") or "")[:19]))
    withdir = [r for r in routes
               if sum(1 for p in r["points"] if p.get("dx") or p.get("dy"))
               > len(r["points"]) * 0.5]
    print("\n有朝向数据的轨迹: %d 条" % len(withdir))
    if not withdir:
        print("!! 没有可用朝向的轨迹(老数据或全局录制, 都算不了转弯半径)")
        return 0

    print("\n=== 1. 采样间隔 dt ===")
    for r in withdir:
        pts = r["points"]
        dts = [b["t"] - a["t"] for a, b in zip(pts, pts[1:])]
        print("  %-14s %-22s n=%-5d dt p5=%.4f p50=%.4f p95=%.4f max=%.4f"
              % (r["map_key"], (r.get("vehicle") or "?")[:22], len(pts),
                 pct(dts, .05), pct(dts, .50), pct(dts, .95), max(dts)))

    print("\n=== 2. 速度: 位移差分 vs 8111 speed ===")
    print("   (≈1 说明一致; 加速时 >1 且减速时 <1 就说明 speed 滞后)")
    for r in withdir:
        M = MAP_M.get(r["map_key"], DEFAULT_MAP_M)
        pts = r["points"]
        ratios, acc = [], []
        for i in range(1, len(pts) - 1):
            a, b = pts[i - 1], pts[i + 1]
            dt = b["t"] - a["t"]
            if dt <= 0 or b["speed"] < 5:
                continue
            d = math.hypot((b["x"] - a["x"]) * M, (b["y"] - a["y"]) * M)
            ratios.append((d / dt * 3.6) / b["speed"])
            acc.append(b["speed"] - a["speed"])
        if not ratios:
            continue
        up = [x for x, ac in zip(ratios, acc) if ac > 0.5]
        dn = [x for x, ac in zip(ratios, acc) if ac < -0.5]
        print("  %-14s %-22s 比值 p10=%.3f p50=%.3f p90=%.3f | 加速 %.3f / 减速 %.3f"
              % (r["map_key"], (r.get("vehicle") or "?")[:22],
                 pct(ratios, .10), pct(ratios, .50), pct(ratios, .90),
                 pct(up, .5) if up else float("nan"),
                 pct(dn, .5) if dn else float("nan")))

    print("\n=== 3. 朝向抖动(二阶差分角) ===")
    for r in withdir:
        pts = r["points"]
        devs = []
        for i in range(1, len(pts) - 1):
            a0, a1, a2 = (math.atan2(p["dy"], p["dx"])
                          for p in (pts[i - 1], pts[i], pts[i + 1]))
            devs.append(abs(math.degrees(unwrap_delta(a1, a2) - unwrap_delta(a0, a1))))
        print("  %-14s %-22s p50=%.3f° p90=%.3f° p99=%.3f°"
              % (r["map_key"], (r.get("vehicle") or "?")[:22],
                 pct(devs, .5), pct(devs, .9), pct(devs, .99)))

    print("\n=== 4. 交叉验证: 朝向角变化率 ω_dir vs 位移航向变化率 ω_pos ===")
    print("   (转弯时两者应接近; 差得多说明其中一路有噪声/滞后)")
    for r in withdir:
        pts = r["points"]
        M = MAP_M.get(r["map_key"], DEFAULT_MAP_M)
        diffs = []
        for i in range(2, len(pts) - 2):
            p0, p1, p2 = pts[i - 2], pts[i], pts[i + 2]
            dt = p2["t"] - p0["t"]
            if dt <= 0:
                continue
            w_dir = unwrap_delta(math.atan2(p0["dy"], p0["dx"]),
                                 math.atan2(p2["dy"], p2["dx"])) / dt
            h0 = math.atan2((p1["y"] - p0["y"]) * M, (p1["x"] - p0["x"]) * M)
            h1 = math.atan2((p2["y"] - p1["y"]) * M, (p2["x"] - p1["x"]) * M)
            w_pos = unwrap_delta(h0, h1) / dt
            if abs(w_dir) < 0.02 and abs(w_pos) < 0.02:
                continue                    # 直线段没意义
            diffs.append(abs(w_dir - w_pos))
        if diffs:
            print("  %-14s %-22s |Δω| p50=%.3f p90=%.3f rad/s (转弯样本 %d)"
                  % (r["map_key"], (r.get("vehicle") or "?")[:22],
                     pct(diffs, .5), pct(diffs, .9), len(diffs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
