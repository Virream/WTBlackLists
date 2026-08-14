"""8111 全端点并发探针(增强版)。

用途: 在"机库 → 自定义对局 → 退出"过程中完整记录 8111 各端点返回,
用于分析自定义对局中能获取到什么信息(地图、玩家对象、击杀、聊天等)。

用法:
    python tools/probe8111.py

操作:
    - 启动后可在任意时刻按【Enter】(可先输入说明文字再回车)打一个时间标记,
      便于把日志分段为: 机库基线 / 进入自定义 / 自定义中 / 退出自定义。
    - 协议建议: 机库5秒 → 进自定义打一段时间 → 退出自定义回机库 → Ctrl+C 停止。

日志: logs/probe8111_时间戳.jsonl(每帧一行, 完整 JSON; 标记写入 frame_markers)。
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import sys
import threading
import time
from datetime import datetime

import requests

BASE = "http://localhost:8111"
INTERVAL = 1.0           # 轮询间隔(秒)
TIMEOUT = 1.5            # 单端点超时(秒)
MAX_WORKERS = 8          # 并发抓取, 避免某个端点慢拖累整轮
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")

# (key, path, 请求参数)
ENDPOINTS = [
    ("state", "/state", {}),
    ("mission", "/mission.json", {}),
    ("map_info", "/map_info.json", {}),
    ("map_obj", "/map_obj.json", {}),
    ("indicators", "/indicators", {}),
    ("gamechat", "/gamechat", {"lastId": 0}),
    ("hudmsg", "/hudmsg", {"lastEvt": 0, "lastDmg": 0}),
]

_markers: list[tuple[float, str]] = []


def _fetch(path: str, params: dict) -> tuple[int, object]:
    try:
        r = requests.get(BASE + path, params=params, timeout=TIMEOUT)
        if r.status_code != 200:
            return r.status_code, None
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text
    except requests.RequestException as exc:
        return -1, f"ERR {exc}"


def collect() -> dict:
    """并发抓取全部端点。"""
    rec: dict = {}
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(_fetch, path, params): key for key, path, params in ENDPOINTS}
        for fut in cf.as_completed(futs):
            key = futs[fut]
            code, data = fut.result()
            rec[key] = {"http": code, "data": data}
    return rec


def _in_map(mi) -> bool:
    return (
        isinstance(mi, dict)
        and mi.get("valid") is not False
        and any(mi.get(k) for k in (
            "grid_size", "grid_steps", "grid_zero", "map_max", "map_min",
            "hud_type", "map_generation",
        ))
    )


def summarize(rec: dict) -> str:
    m = rec.get("mission", {}).get("data") if isinstance(rec.get("mission", {}).get("data"), dict) else {}
    mi = rec.get("map_info", {}).get("data") if isinstance(rec.get("map_info", {}).get("data"), dict) else {}
    mo = rec.get("map_obj", {}).get("data")
    st = rec.get("state", {}).get("data") if isinstance(rec.get("state", {}).get("data"), dict) else {}
    objs = len(mo.get("objects") or []) if isinstance(mo, dict) else "n/a"
    m_obj = (m.get("objectives") or []) if isinstance(m.get("objectives"), list) else None
    return (
        f"in_map={_in_map(mi)} mission.status={m.get('status')!r} "
        f"objectives={len(m_obj) if m_obj is not None else 'null'} "
        f"map_obj={objs} state.valid={st.get('valid')!r}"
    )


def _marker_loop() -> None:
    """后台线程: 等待用户按 Enter / 输入说明, 打时间标记。"""
    while True:
        try:
            text = input().strip()
        except (EOFError, OSError):
            break
        _markers.append((time.time(), text or "marker"))
        print(f"\n  >>> 标记已打: {datetime.now():%H:%M:%S}  {text or 'marker'}\n")


def main() -> int:
    os.makedirs(LOGS_DIR, exist_ok=True)
    fname = os.path.join(LOGS_DIR, f"probe8111_{datetime.now():%Y%m%d_%H%M%S}.jsonl")
    logf = open(fname, "w", encoding="utf-8")

    print("=" * 72)
    print("8111 全端点并发探针(增强版)  已启动")
    print("日志文件:", fname)
    print()
    print("操作说明:")
    print("  随时按【Enter】(或先输入说明文字再回车)可打一个时间标记, 方便分段")
    print("  建议协议: 机库5秒 → 进入自定义 → 打一段时间 → 退出自定义 → 停")
    print("  完成后按 Ctrl+C 停止, 把上面的日志文件路径发给助手")
    print("=" * 72)

    threading.Thread(target=_marker_loop, daemon=True).start()

    last_chat, last_evt, last_dmg = 0, 0, 0
    last_summary: str | None = None
    try:
        while True:
            ts = time.time()
            iso = datetime.now().isoformat(timespec="milliseconds")
            rec = collect()
            rec["ts"] = ts
            rec["iso"] = iso

            # 增量: gamechat / hudmsg 只记录"新增"部分
            gc = rec.get("gamechat", {}).get("data")
            gc_list = gc if isinstance(gc, list) else []
            new_chat = [m for m in gc_list if int(m.get("id", -1)) > last_chat]
            if gc_list:
                last_chat = max(int(m.get("id", -1)) for m in gc_list)
            rec["gamechat"]["new"] = new_chat
            rec["gamechat"]["latest_id"] = last_chat

            hud = rec.get("hudmsg", {}).get("data")
            hud_d = hud if isinstance(hud, dict) else {}
            evts = hud_d.get("events") or []
            dmgs = hud_d.get("damage") or []
            new_evts = [e for e in evts if int(e.get("id", -1)) > last_evt]
            new_dmgs = [d for d in dmgs if int(d.get("id", -1)) > last_dmg]
            if evts:
                last_evt = max(int(e.get("id", -1)) for e in evts)
            if dmgs:
                last_dmg = max(int(e.get("id", -1)) for e in dmgs)
            rec["hudmsg"]["new_events"] = new_evts
            rec["hudmsg"]["new_damage"] = new_dmgs
            rec["hudmsg"]["latest_evt"] = last_evt
            rec["hudmsg"]["latest_dmg"] = last_dmg

            # 本帧时刻打下的标记(写入日志便于分段)
            if _markers:
                rec["frame_markers"] = [x for (t, x) in _markers]

            logf.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            logf.flush()

            s = summarize(rec)
            marker_txt = ("  <-- 标记: " + "; ".join(rec.get("frame_markers", []))) if rec.get("frame_markers") else ""
            if last_summary is not None and s != last_summary:
                print(f"[{datetime.now():%H:%M:%S}] {s}  <<< 状态变化{marker_txt}")
            else:
                print(f"[{datetime.now():%H:%M:%S}] {s}{marker_txt}")
            last_summary = s

            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        print("\n已停止。日志已保存到:", fname)
        print("请把该文件路径发给助手进行分析。")
    finally:
        logf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
