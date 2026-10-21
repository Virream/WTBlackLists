# -*- coding: utf-8 -*-
"""诊断: 观战视角在“地面单位 ↔ 空中单位”之间切换时, 8111 的坐标系到底怎么变的。

**为什么需要这个脚本**(2026-10-21): 全局录制时如果观战中的玩家上了固定翼, 8111 会把
地图切成空战图, `/map_obj.json` 里同一批地面单位的坐标整体被缩放/平移 —— 录下来的
线路就会突然断开、在别的地方重新开始。程序里已经按“用静态地物当锚点反解相似变换”
修了(见 `wt81111g/global_record.py` 的 `fit_frame`), 但那套参数是照着**实测数据**
定的, 所以这里把实测数据打印出来, 用来核对:

* 两个视角下 `respawn_base_tank` / `capture_zone` 的锚点数量与包围盒;
* 由锚点反解出的变换(scale / rot / 原点映射 / 残差);
* `/map_info.json` 的 `map_min` / `map_max` 有没有跟着变(速度尺度会不会歪)。

**用法**:
1. 进一局陆战回放(或让别人观战), 把视角放在一辆**地面载具**上, 先让它稳定几秒;
2. 保持运行, 期间**暂停回放**、把观战视角切到一架**空中载具**、再切回地面;
3. 把终端里每一段 `坐标系变化` 的输出贴回来。

只读 8111(官方接口), 不碰游戏文件。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wt81111g.api8111 import WT8111
from wt81111g.global_record import fit_frame, frame_anchors

POLL_MS = 200          # 采样间隔(毫秒)
SHOW_EVERY_S = 2.0     # 即使没变化, 也隔这么久打一行心跳


def bbox(points) -> str:
    if not points:
        return "-"
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (f"x[{min(xs):.4f},{max(xs):.4f}] y[{min(ys):.4f},{max(ys):.4f}] "
            f"质心=({sum(xs) / len(xs):.4f},{sum(ys) / len(ys):.4f})")


def player_desc(objects) -> str:
    """当前观战的那台载具(`icon == "Player"`)。"""
    for obj in objects or []:
        if not isinstance(obj, dict):
            continue
        if str(obj.get("icon") or "") == "Player":
            return (f"Player: type={obj.get('type')} icon={obj.get('icon')} "
                    f"x={obj.get('x')} y={obj.get('y')}")
    return "Player: (无)"


def main() -> int:
    client = WT8111()
    print("=" * 78)
    print("8111 坐标系诊断 —— 请在地面载具 / 空中载具之间切换观战视角")
    print("=" * 78)
    reference = None            # 参考锚点(第一次拿到的)
    last_sig = None
    last_print = 0.0
    map_desc = ""
    while True:
        try:
            objects = client.map_objects()
        except Exception as exc:                     # noqa: BLE001 - 诊断脚本
            print(f"[8111] 取 /map_obj.json 失败: {exc}")
            time.sleep(1.0)
            continue
        try:
            info = client.map_info() or {}
            map_desc = f"map_min={info.get('map_min')} map_max={info.get('map_max')}"
        except Exception:                            # noqa: BLE001
            map_desc = "map_info 取不到"
        anchors = frame_anchors(objects)
        sig = (len(anchors), bbox(anchors))
        now = time.monotonic()
        if reference is None and len(anchors) >= 4:
            reference = anchors
            print(f"\n[基准] 锚点 {len(anchors)} 个  {bbox(anchors)}")
            print(f"       {map_desc}")
            print(f"       {player_desc(objects)}")
        elif reference is not None and sig != last_sig:
            fit = fit_frame(reference, anchors) if len(anchors) >= 4 else None
            print(f"\n[变化] 锚点 {len(anchors)} 个  {bbox(anchors)}")
            print(f"       {map_desc}")
            print(f"       {player_desc(objects)}")
            if fit is None:
                print("       拟合: 解不出来(锚点太少或对不上) → 程序会暂时不换算")
            else:
                print(f"       拟合: {fit.describe()}"
                      f"{'  → 同一个坐标系, 不需要换算' if fit.same_as_reference else ''}")
                if not fit.same_as_reference:
                    gx, gy = anchors[0]
                    bx, by = fit.apply(gx, gy)
                    print(f"       校验: 当前帧锚点 ({gx:.4f},{gy:.4f}) "
                          f"→ 参考坐标系 ({bx:.4f},{by:.4f})")
        elif now - last_print > SHOW_EVERY_S:
            print(f"[等待] 锚点 {len(anchors)} 个  {player_desc(objects)}")
        last_sig = sig
        last_print = now
        time.sleep(POLL_MS / 1000.0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已停止。")
