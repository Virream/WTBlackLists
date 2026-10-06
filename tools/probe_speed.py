# -*- coding: utf-8 -*-
"""20ms 全量车速采样: 记录每 20ms 的 (时间, 速度, 挡位, 转速), 供生成速度-时间折线图。

用法:
  .venv\\Scripts\\python.exe tools\\probe_speed.py

Ctrl+C 停止后导出 CSV(speed_log.csv): time_s, speed_kmh, gear, rpm
并自动定位每次"极速→速度变负"的耗时(按速度从峰值回落到负值的连续段)。
"""
import csv
import json
import os
import sys
import time
import urllib.request

BASE_URL = "http://localhost:8111"
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTERVAL = 0.02  # 20ms


def fetch():
    try:
        with urllib.request.urlopen(BASE_URL + "/indicators", timeout=1.0) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        if not isinstance(d, dict) or d.get("valid") is False:
            return None
        return d
    except Exception:  # noqa: BLE001
        return None


def detect_peak_to_negative(samples):
    """粗定位每次"极速→速度变负"耗时: 速度从峰值(≥40)持续降到负值。"""
    # 找出速度峰值段与速度变负段, 按时间先后配对
    peaks = []   # (t, speed) 速度 >= 40 的峰值
    negs = []    # (t, speed) 速度 < 0 的起点
    cur_peak = None
    for t, speed, _g, _r in samples:
        if speed is None:
            continue
        if speed >= 40:
            if cur_peak is None or speed > cur_peak[1]:
                cur_peak = (t, speed)
        else:
            if cur_peak is not None:
                peaks.append(cur_peak)
                cur_peak = None
        if speed < 0:
            if not negs or t - negs[-1][0] > 1.0:
                negs.append((t, speed))
    # 配对: 每个 neg 找它之前最近的 peak
    out = []
    for nt, ns in negs:
        cand = [p for p in peaks if p[0] < nt]
        if cand:
            pt, ps = max(cand, key=lambda x: x[0])
            out.append((pt, ps, nt, ns, round(nt - pt, 2)))
    return out


def main() -> int:
    print("20ms 全量车速采样开始(每0.5s显示一次当前速度; 挡位变化会单独标出)。")
    print("请按你的实验流程操作, 测完 Ctrl+C 停止。\n")
    t0 = time.monotonic()
    samples = []
    prev_gear = None
    last_print = 0.0
    out = os.path.join(PROJECT, "speed_log.csv")
    # 实时落盘: 每次采样立即写 CSV 并 flush, 强停(kill)也不丢数据
    f = open(out, "w", newline="", encoding="utf-8-sig")
    w = csv.writer(f)
    w.writerow(["time_s", "speed_kmh", "gear", "rpm"])
    try:
        while True:
            d = fetch()
            t = time.monotonic() - t0
            if d:
                gear = d.get("gear")
                speed = d.get("speed")
                rpm = d.get("rpm")
                row = (round(t, 3), speed, gear, rpm)
                samples.append(row)
                w.writerow(row)
                f.flush()
                if gear is not None and gear != prev_gear:
                    neutral = d.get("gear_neutral")
                    ui = (gear - neutral) if neutral is not None else None
                    print(f"[{time.strftime('%H:%M:%S')} | {t:6.2f}s] 挡位→{gear}(UI {ui}) "
                          f"速度={speed} rpm={rpm}")
                    prev_gear = gear
                if t - last_print >= 0.5:
                    print(f"[{time.strftime('%H:%M:%S')} | {t:6.2f}s] 速度={speed} km/h 挡位={gear}")
                    last_print = t
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        f.close()
    print(f"\n已实时落盘 {len(samples)} 个采样点 -> {out}")

    segs = detect_peak_to_negative(samples)
    if segs:
        print("\n=== 极速→速度变负 段(自动检测) ===")
        for pt, ps, nt, ns, dur in segs:
            print(f"  极速 {ps}km/h @{pt:.1f}s → 速度变负 {ns}km/h @{nt:.1f}s = {dur}s")
    else:
        print("\n(未检测到完整 极速→速度变负 段)")
    return 0


if __name__ == "__main__":
    main()
