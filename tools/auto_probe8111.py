"""自动采集 + 自动分析一体脚本: 分析"进入/退出自定义对局"你都做了什么。

用法:
    python tools/auto_probe8111.py

流程(自动):
    1) 启动后自动采集(并发轮询 8111 全部端点, 每秒一帧, 单端点失败不影响整体)
    2) 采集期间你正常操作: 机库 → 进入自定义对局 → 打一会儿 → 退出自定义 → 回机库
    3) 按 Ctrl+C 停止后, 脚本【自动分析】并打印报告:
         - 阶段时间线(机库 / 进入 / 对局中 / 退出)
         - 对局中的动作(击杀/伤害记录、聊天发言、地图玩家对象数量变化)
         - 进入/退出时各端点字段差异
    4) 报告同时保存到 logs/分析_时间戳.txt

无需任何手动打标记, 阶段由程序自动识别。
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import sys
import time
from datetime import datetime

import requests

BASE = "http://localhost:8111"
INTERVAL = 1.0
TIMEOUT = 1.5
MAX_WORKERS = 8
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")

ENDPOINTS = [
    ("state", "/state", {}),
    ("mission", "/mission.json", {}),
    ("map_info", "/map_info.json", {}),
    ("map_obj", "/map_obj.json", {}),
    ("indicators", "/indicators", {}),
    ("gamechat", "/gamechat", {"lastId": 0}),
    ("hudmsg", "/hudmsg", {"lastEvt": 0, "lastDmg": 0}),
]

MAP_FIELDS = ("grid_size", "grid_steps", "grid_zero", "map_max", "map_min",
              "hud_type", "map_generation")


def in_map_of(mi) -> bool:
    return (isinstance(mi, dict) and mi.get("valid") is not False
            and any(mi.get(k) for k in MAP_FIELDS))


def _as_dict(x) -> dict:
    """端点 data 可能是字符串(ERR/文本/空串)或 None, 统一转 dict。"""
    return x if isinstance(x, dict) else {}


def _fetch(path: str, params: dict) -> tuple[int, object]:
    try:
        r = requests.get(BASE + path, params=params, timeout=TIMEOUT)
        if r.status_code != 200:
            return r.status_code, None
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text
    except Exception as exc:  # noqa: BLE001
        return -1, f"ERR {exc}"


def collect() -> dict:
    rec: dict = {}
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(_fetch, path, params): key for key, path, params in ENDPOINTS}
        for fut in cf.as_completed(futs):
            key = futs[fut]
            try:
                code, data = fut.result()
            except Exception as exc:  # noqa: BLE001
                code, data = -1, f"ERR {exc}"
            rec[key] = {"http": code, "data": data}
    return rec


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------
def capture(fname: str) -> int:
    logf = open(fname, "w", encoding="utf-8")
    last_chat = last_evt = last_dmg = 0
    last_summary: str | None = None
    n = 0
    try:
        while True:
            ts = time.time()
            iso = datetime.now().isoformat(timespec="milliseconds")
            rec = collect()
            rec["ts"] = ts
            rec["iso"] = iso

            gc = rec.get("gamechat", {}).get("data")
            gc_list = gc if isinstance(gc, list) else []
            rec["gamechat"]["new"] = [m for m in gc_list if int(m.get("id", -1)) > last_chat]
            if gc_list:
                last_chat = max(int(m.get("id", -1)) for m in gc_list)
            rec["gamechat"]["latest_id"] = last_chat

            hud = rec.get("hudmsg", {}).get("data")
            hud_d = hud if isinstance(hud, dict) else {}
            evts = hud_d.get("events") or []
            dmgs = hud_d.get("damage") or []
            rec["hudmsg"]["new_events"] = [e for e in evts if int(e.get("id", -1)) > last_evt]
            rec["hudmsg"]["new_damage"] = [d for d in dmgs if int(d.get("id", -1)) > last_dmg]
            if evts:
                last_evt = max(int(e.get("id", -1)) for e in evts)
            if dmgs:
                last_dmg = max(int(e.get("id", -1)) for e in dmgs)

            logf.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            logf.flush()

            m = _as_dict(rec.get("mission", {}).get("data"))
            mi = _as_dict(rec.get("map_info", {}).get("data"))
            mo = rec.get("map_obj", {}).get("data")
            st = _as_dict(rec.get("state", {}).get("data"))
            objs = len(mo.get("objects") or []) if isinstance(mo, dict) else "n/a"
            s = (f"in_map={in_map_of(mi)} mission.status={m.get('status')!r} "
                 f"map_obj={objs} state.valid={st.get('valid')!r}")
            n += 1
            if last_summary is not None and s != last_summary:
                print(f"[{datetime.now():%H:%M:%S}] {s}  <<< 状态变化")
            else:
                print(f"[{datetime.now():%H:%M:%S}] {s}")
            last_summary = s
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        print("\n采集结束。")
    finally:
        logf.close()
    return n


# ---------------------------------------------------------------------------
# 自动分析
# ---------------------------------------------------------------------------
def _fmt_duration(sec: float) -> str:
    return f"{sec:.1f}s"


def analyze(fname: str) -> str:
    if not os.path.exists(fname):
        return f"日志文件不存在: {fname}"
    rows = []
    for line in open(fname, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    if not rows:
        return "日志为空, 无法分析。"

    # 兼容旧日志: 缺失 ts/iso 时用帧号作为虚拟时间
    for i, r in enumerate(rows):
        if "ts" not in r:
            r["ts"] = float(i)
            r["iso"] = f"第{i}帧"

    out: list[str] = []
    out.append("=" * 60)
    out.append("自动分析报告: 你做了什么")
    out.append(f"采集区间: {rows[0].get('iso')} ~ {rows[-1].get('iso')}  共 {len(rows)} 帧")
    out.append("")

    # 1) 阶段时间线
    phases: list[dict] = []
    cur = None
    prev = None
    for r in rows:
        mi = r.get("map_info", {}).get("data") or {}
        in_map = in_map_of(mi)
        if prev is None:
            cur = {"name": "地图中" if in_map else "机库/大厅", "start": r["ts"], "start_iso": r["iso"], "in": in_map}
            prev = in_map
        elif in_map != prev:
            cur["end"] = r["ts"]
            cur["end_iso"] = r["iso"]
            phases.append(cur)
            cur = {"name": "地图中" if in_map else "机库/大厅", "start": r["ts"], "start_iso": r["iso"], "in": in_map}
            prev = in_map
    if cur:
        cur["end"] = rows[-1]["ts"]
        cur["end_iso"] = rows[-1]["iso"]
        phases.append(cur)

    out.append("【阶段时间线】")
    if len(phases) == 1:
        out.append(f"  全程: {phases[0]['name']} ({_fmt_duration(phases[0]['end'] - phases[0]['start'])})")
    else:
        for i, p in enumerate(phases):
            out.append(f"  {i + 1}. {p['name']}: {p['start_iso'][11:19]} → {p['end_iso'][11:19]} "
                       f"({_fmt_duration(p['end'] - p['start'])})")
    out.append("")

    # 2) 对局中动作: 击杀 / 聊天 / 地图对象
    in_rows = [r for r in rows if in_map_of(r.get("map_info", {}).get("data") or {})]
    if in_rows:
        out.append("【对局中收集到】")
        # 击杀 / 伤害
        dmg_seen: set[str] = set()
        dmg_msgs = []
        for r in in_rows:
            for d in r.get("hudmsg", {}).get("new_damage", []) or []:
                mid = d.get("id", "")
                if mid not in dmg_seen:
                    dmg_seen.add(mid)
                    msg = d.get("msg", "")
                    if msg:
                        dmg_msgs.append(f"    [{r['iso'][11:19]}] {msg}")
        if dmg_msgs:
            out.append("  击杀/伤害记录:")
            out.extend(dmg_msgs[:50])
        else:
            out.append("  击杀/伤害: 无")
        # 聊天
        chat_seen: set[int] = set()
        chats = []
        for r in in_rows:
            for m in r.get("gamechat", {}).get("new", []) or []:
                mid = int(m.get("id", -1))
                if mid not in chat_seen:
                    chat_seen.add(mid)
                    chats.append(f"    [{r['iso'][11:19]}] {m.get('sender', '?')}: {m.get('text', m.get('msg', ''))}")
        if chats:
            out.append("  聊天发言:")
            out.extend(chats[:50])
        # 地图对象
        peaks = []
        for r in in_rows:
            mo = r.get("map_obj", {}).get("data")
            n = len(mo.get("objects") or []) if isinstance(mo, dict) else 0
            peaks.append(n)
        if peaks:
            out.append(f"  地图玩家/载具对象数: 峰值 {max(peaks)}, 首帧 {peaks[0]}, 末帧 {peaks[-1]}")
        # 进入/退出字段差异
        out.append("")
        out.append("【进出场关键信号(对比)】")
        first_in = in_rows[0]
        last_in = in_rows[-1]
        for label, r in (("进入时刻", first_in), ("退出前一刻", last_in)):
            mi = _as_dict(r.get("map_info", {}).get("data"))
            m = _as_dict(r.get("mission", {}).get("data"))
            out.append(f"  {label} [{r['iso'][11:19]}]: "
                       f"map_info.valid={mi.get('valid')!r} 地图字段={'有' if in_map_of(mi) else '无'} "
                       f"mission.status={m.get('status')!r} objectives={m.get('objectives')!r}")
        # 退出后第一帧(机库)
        after = [r for r in rows if not in_map_of(r.get("map_info", {}).get("data") or {})
                 and r["ts"] > first_in["ts"]]
        if after:
            mi = after[0].get("map_info", {}).get("data") or {}
            out.append(f"  退出后 [{after[0]['iso'][11:19]}]: map_info = {json.dumps(mi, ensure_ascii=False)[:120]}")
        out.append("")
    else:
        out.append("【未检测到进入对局/地图】建议: 确保在自定义对局中停留并制造一些击杀/发言。")
        out.append("")

    out.append("=" * 60)
    return "\n".join(out)


def main() -> int:
    os.makedirs(LOGS_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = os.path.join(LOGS_DIR, f"auto_probe_{stamp}.jsonl")
    rname = os.path.join(LOGS_DIR, f"分析_{stamp}.txt")

    print("=" * 72)
    print("自动采集 + 自动分析  已启动")
    print("日志:", fname)
    print()
    print("请正常操作: 机库 → 进入自定义对局 → 打一会儿 → 退出自定义 → 回机库")
    print("完成后按 Ctrl+C 停止, 脚本会自动分析并输出报告")
    print("=" * 72)

    n = capture(fname)
    print(f"\n共采集 {n} 帧。正在自动分析...")
    report = analyze(fname)
    print()
    print(report)
    with open(rname, "w", encoding="utf-8") as rf:
        rf.write(report)
    print(f"\n报告已保存: {rname}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
