# -*- coding: utf-8 -*-
"""试车场探测: 高频轮询 8111 的 /state 与 /indicators,
检查陆战坦克模式下是否有 RPM(转速)/speed(速度)/throttle(油门)/gear(挡位)等字段。

用法:
  .venv\\Scripts\\python.exe tools\\probe_ground.py

步骤: 进试车场 → 按 W 加速(自动换挡) → 观察转速/速度是否变化 → Ctrl+C 停止。
"""
import json
import time
import urllib.request

BASE = "http://localhost:8111"
INTERVAL = 0.2  # 采样间隔(秒)


def fetch(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=1.0) as r:
            text = r.read().decode("utf-8", "replace").strip()
            if not text:
                return None
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    except Exception as exc:  # noqa: BLE001
        return {"__error__": str(exc)}


def key_fields(d):
    """提取关键字段(valid/type/含 rpm/speed/throttle/gear 的字段)。"""
    out = {}
    if not isinstance(d, dict):
        return out
    for k, v in d.items():
        kl = k.lower()
        if ("valid" == kl or "type" == kl or "rpm" in kl or "speed" in kl
                or "throttle" in kl or "gear" in kl or "kmh" in kl
                or "m/s" in kl or "vario" in kl):
            out[k] = v
    return out


def main() -> int:
    print(f"8111 探测开始({BASE}), 采样间隔 {INTERVAL}s。")
    print("请进入试车场, 按 W 加速观察换挡过程。Ctrl+C 停止。\n")
    # 首次打印完整 JSON(确认字段结构)
    for path in ("/state", "/indicators", "/mission.json", "/map_info.json"):
        d = fetch(path)
        print(f"--- 首次 {path} ---")
        print(json.dumps(d, ensure_ascii=False, indent=2)[:2000])
        print()
    print("=== 开始高频采样(仅关键字段) ===\n")
    try:
        while True:
            ts = time.strftime("%H:%M:%S")
            row = [f"[{ts}]"]
            for path in ("/state", "/indicators"):
                d = fetch(path)
                if isinstance(d, dict) and d.get("__error__"):
                    row.append(f"{path}:ERR")
                    continue
                kf = key_fields(d)
                line = " ".join(f"{k}={v}" for k, v in kf.items())
                row.append(f"{path} {{ {line} }}")
            print("  ".join(row))
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    main()
