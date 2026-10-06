# -*- coding: utf-8 -*-
"""8111 空战模式高频采样: 实时读取飞行速度/转速, 并统计真实响应耗时(rtt)与数据更新频率。

用法:
  .venv\\Scripts\\python.exe tools\\probe_air_speed.py [interval_ms] [输出CSV文件名]

Ctrl+C 停止后导出 CSV(默认 speed_log_tank.csv):
  time_s, rtt_ms, speed, gear, gear_neutral, rpm, vario, altitude
"""
import csv
import json
import os
import statistics
import sys
import time
import urllib.request

BASE_URL = "http://127.0.0.1:8111"
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTERVAL = float(sys.argv[1]) / 1000.0 if len(sys.argv) > 1 else 0.010  # 默认 10ms
OUT_NAME = sys.argv[2] if len(sys.argv) > 2 else "speed_log_tank.csv"
SEG_MIN = 45.0  # 判定一轮"极速→变负"的最低速度(km/h)


def fetch():
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(BASE_URL + "/indicators", timeout=1.0) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return d, time.perf_counter() - t0
    except Exception:  # noqa: BLE001
        return None, time.perf_counter() - t0


def main() -> int:
    print(f"8111 空战高频采样开始 (间隔 {INTERVAL * 1000:.0f}ms, Ctrl+C 停止)")
    print("速度每次变化都会打印, 每秒打印一次汇总。\n")
    t0 = time.monotonic()
    out = os.path.join(PROJECT, OUT_NAME)
    rtts = []
    f = open(out, "w", newline="", encoding="utf-8-sig")
    w = csv.writer(f)
    w.writerow(["time_s", "rtt_ms", "speed", "gear", "gear_neutral", "rpm", "vario", "altitude", "segment"])
    prev_speed = None
    prev_rpm = None
    prev_gear = None
    gear_changes = []  # (t, gear) 挡位每次变化
    changes = []      # (t, speed) 速度每次变化
    rpm_changes = []  # (t, rpm) 转速每次变化
    last_print = 0.0
    n = 0
    segment = 0        # 当前轮次号
    saw_high = False   # 是否已见极速(>= SEG_MIN)
    seg_start = None   # 本轮起点时刻
    seg_peak = None    # 本轮极速 (t, speed)
    try:
        while True:
            d, rtt = fetch()
            t = time.monotonic() - t0
            if d:
                speed = d.get("speed")
                rpm = d.get("rpm_hour") or d.get("rpm")
                gear = d.get("gear")
                gneutral = d.get("gear_neutral")
                # 轮次检测: 速度 >= SEG_MIN 升至峰值, 再降至 <0 = 一轮"极速→变负"完成
                if speed is not None:
                    if speed >= SEG_MIN:
                        if not saw_high:
                            saw_high = True
                            seg_start = t
                            seg_peak = (t, speed)
                        elif seg_peak and speed > seg_peak[1]:
                            seg_peak = (t, speed)
                    if saw_high and speed < 0:
                        segment += 1
                        pk_t, pk_s = seg_peak or (t, speed)
                        dur = t - (seg_start or t)
                        print(f"  >> 第{segment}轮完成: 极速 {pk_s:.0f}km/h @{pk_t:.1f}s "
                              f"→ 速度变负 @{t:.2f}s = {dur:.2f}s")
                        saw_high = False
                        seg_start = None
                        seg_peak = None
                w.writerow([round(t, 3), round(rtt * 1000, 1), speed, gear, gneutral,
                            rpm, d.get("vario"), d.get("altitude_min"), segment])
                f.flush()
                n += 1
                rtts.append(rtt * 1000)
                now = time.strftime('%H:%M:%S')
                if gear is not None and gear != prev_gear:
                    ui = (gear - gneutral) if gneutral is not None else None
                    print(f"[{now}|{t:6.3f}s] 挡位→{gear}(UI {ui}) 速度={speed} (rtt {rtt * 1000:.0f}ms)")
                    gear_changes.append((t, gear))
                    prev_gear = gear
                if speed is not None and speed != prev_speed:
                    if prev_speed is not None and changes:
                        dt = (t - changes[-1][0]) * 1000
                        print(f"[{now}|{t:6.3f}s] 速度 {prev_speed:7.2f}→{speed:7.2f} "
                              f"(变化间隔 {dt:.0f}ms, rtt {rtt * 1000:.0f}ms)")
                    changes.append((t, speed))
                    prev_speed = speed
                if rpm is not None and rpm != prev_rpm:
                    if prev_rpm is not None and rpm_changes:
                        dt = (t - rpm_changes[-1][0]) * 1000
                        print(f"[{now}|{t:6.3f}s] 转速 {prev_rpm:.0f}→{rpm:.0f} "
                              f"(变化间隔 {dt:.0f}ms, rtt {rtt * 1000:.0f}ms)")
                    rpm_changes.append((t, rpm))
                    prev_rpm = rpm
                if t - last_print >= 1.0:
                    if rtts:
                        p95 = sorted(rtts)[max(int(len(rtts) * 0.95) - 1, 0)]
                        print(f"[{now}|{t:6.1f}s] speed={speed} rpm={rpm} | "
                              f"样本={n} 请求率≈{n / t:.0f}Hz rtt p50={statistics.median(rtts):.0f}ms p95={p95:.0f}ms")
                    last_print = t
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        f.close()

    if rtts:
        p95 = sorted(rtts)[max(int(len(rtts) * 0.95) - 1, 0)]
        print(f"\n=== 汇总 ===")
        print(f"样本数 {n}, 时长 {t:.1f}s, 请求率 {n / t:.1f} Hz")
        print(f"响应耗时 rtt: min {min(rtts):.0f}ms / p50 {statistics.median(rtts):.0f}ms "
              f"/ p95 {p95:.0f}ms / max {max(rtts):.0f}ms")
        if len(changes) >= 2:
            dts = [b[0] - a[0] for a, b in zip(changes[:-1], changes[1:])]
            print(f"速度值变化 {len(changes)} 次, 间隔: p50 {statistics.median(dts) * 1000:.0f}ms "
                  f"/ min {min(dts) * 1000:.0f}ms / max {max(dts) * 1000:.0f}ms")
        if len(rpm_changes) >= 2:
            dts = [b[0] - a[0] for a, b in zip(rpm_changes[:-1], rpm_changes[1:])]
            print(f"转速值变化 {len(rpm_changes)} 次, 间隔: p50 {statistics.median(dts) * 1000:.0f}ms "
                  f"/ min {min(dts) * 1000:.0f}ms / max {max(dts) * 1000:.0f}ms")
        if len(gear_changes) >= 2:
            dts = [b[0] - a[0] for a, b in zip(gear_changes[:-1], gear_changes[1:])]
            print(f"挡位变化 {len(gear_changes)} 次, 间隔: p50 {statistics.median(dts) * 1000:.0f}ms "
                  f"/ min {min(dts) * 1000:.0f}ms / max {max(dts) * 1000:.0f}ms")
    print(f"\nCSV -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
