"""用 `/hudmsg` 的击毁消息反推"摄像机挂着的那位玩家"到底什么时候死。

============================================================= 为什么需要它
8111 的 `/map_obj.json` 只给**摄像机所在那一方**的单位。录制全局 A/B 队路线时,
会刻意把视角挂在我方一位玩家身上(这样 8111 才会报这一方), 于是:

* 那位玩家的车**阵亡后 8111 依旧持续上报**(实测), 他的 `icon` 永远不会消失;
* 其余玩家阵亡后 `icon` 正常消失 → 现有的"消失即击毁"判死对他们完全有效。

于是每队**恰好有 1 条线拿不到死亡时刻**(就是摄像机挂的那位), 表现为"活到录制结束"。

============================================================== 为什么能反推
`/hudmsg` 的 `damage` 里有本局全部击杀文本(回放模式下 `events` 是空的):

    {"id": 1374, "time": 475, "msg": "cruasane (护卫坦克 57) 击毁了 X (黄蜂-AKM)"}

`time` 是**比赛秒数**, 从服务器端开始录像就算 —— 但我们不知道它和本软件 `t=0`
(第一名玩家重生) 差多少, 而且准备阶段那个"15 秒"是假数字(游戏会在最后几秒把
时间拉长), 所以**绝对偏移不可用**(用户 2026-10-22 实测确认)。

可用的只有"击毁事件之间的**时间差模式**":
把本局所有线路的结束时刻当成一个序列, 把所有击毁时刻当成另一个序列, 两者只差
一个未知平移 → **扫一遍平移量**, 取配对最多的那个。对不上的击毁时刻, 就是
"icon 没消失、但确实死了" 的那位(每队最多 1 条, 两队合计最多 2 条)。

本模块**只做分析、只写日志**, 不改任何 `dead` 判定 —— 先拿真实数据验证命中率。
"""
from __future__ import annotations

import logging
import math

log = logging.getLogger("kill_timeline")

ZW_SPACE = "\u200b"          # 8111 的文本里中文字符之间夹着零宽空格
KILL_MARK = "击毁了"          # 只认击杀; "引燃了" / "获得嘉奖" 不算死亡
MATCH_TOL = 2.0              # 配对容差(秒): 两队时间轴本身还差 1~2 秒
SEARCH_SPAN = 90.0           # 平移量搜索范围 ±90 秒
SEARCH_STEP = 0.1
DEAD_EPS = 1e-4              # 判定"位置没变"的阈值(归一化坐标 ≈ 0.4m)
# 静默线路"最后移动之后至少静止这么久"才可能是残骸(死亡后不动)。
# 真活到最后的玩家一直在跑, 这个差值接近 0。取 5 秒: 停着守点的车也可能静止很久,
# 所以它只用来**排除真活跃的线**, 不下"它一定死了"的结论。
STATIC_MIN = 5.0


def clean_text(text) -> str:
    """去掉零宽空格与首尾空白。"""
    return str(text or "").replace(ZW_SPACE, "").strip()


def read_cursor(client) -> int:
    """读 `/hudmsg` 当前的 damage 最大 id, 作为"录制开始前的游标"。"""
    try:
        data = client.hudmsg(0, 0)
    except Exception as exc:  # noqa: BLE001 - 抓不到就当没有, 不能影响录制
        log.warning("读取 hudmsg 游标失败: %s", exc)
        return 0
    ids = []
    for row in data.get("damage") or []:
        try:
            ids.append(int(row.get("id") or 0))
        except (TypeError, ValueError):
            continue
    return max(ids, default=0)


def read_new_kills(client, cursor: int) -> list[dict]:
    """取比 `cursor` 新的**击毁**消息: `[{id, t, msg}]`(按 id 升序)。

    只认 `击毁了` —— `引燃了`(只是着火, 不一定死) 与 `获得嘉奖` 都要排除。
    """
    try:
        data = client.hudmsg(0, int(cursor))
    except Exception as exc:  # noqa: BLE001
        log.warning("读取 hudmsg 增量失败: %s", exc)
        return []
    out: list[dict] = []
    for row in data.get("damage") or []:
        try:
            rid = int(row.get("id") or 0)
            t = float(row.get("time") or 0.0)
        except (TypeError, ValueError):
            continue
        if rid <= int(cursor):
            continue
        msg = clean_text(row.get("msg"))
        if KILL_MARK not in msg:
            continue
        out.append({"id": rid, "t": t, "msg": msg})
    out.sort(key=lambda r: r["id"])
    return out


def merge_kills(record) -> list[dict]:
    """合并 A/B 两次录制各自抓到的消息(两次回放会抓到同一批事件)。

    **按 `(time, msg)` 去重, 不能按 `id`** —— 实测诺曼底那局(2026-10-22):
    两次回放各自**重新编号**, 同一事件在两次录制里的 id 完全不同(A 队 1531~1581、
    B 队 1623~1795), 73 条里有 36 组是重复的; 只按 id 去重会让同一条击杀出现两遍,
    把"配对数"虚增, 偏移也可能被带偏。同一时刻真有两起击杀时 msg 必然不同,
    所以 `(时间, 文本)` 是安全的去重键。
    """
    seen: dict[tuple[float, str], dict] = {}
    for row in getattr(record, "kills", None) or []:
        try:
            rid = int(row.get("id") or 0)
            t = float(row.get("t") or 0.0)
        except (TypeError, ValueError):
            continue
        msg = str(row.get("msg") or "")
        key = (round(t, 1), msg)
        if key in seen:
            continue
        seen[key] = {"id": rid, "t": t, "msg": msg}
    return sorted(seen.values(), key=lambda r: r["t"])


def last_move_time(points) -> float | None:
    """最后一次"位置还在变"的时刻 —— 之后车就没再动过了。

    摄像机挂着的那辆车阵亡后会变成残骸(位置固定), 所以"最后移动时刻"就是死亡
    时刻的上界。活到最后的玩家如果一直在跑, 这个值就等于最后一点的时间。
    """
    if not points:
        return None
    last = None
    for a, b in zip(points, points[1:]):
        if abs(a.x - b.x) > DEAD_EPS or abs(a.y - b.y) > DEAD_EPS:
            last = b.t
    return last if last is not None else points[0].t


def _pair(kills: list[float], ends: list[float], offset: float,
          tol: float) -> list[tuple[float, float]]:
    """把 `kills` 平移 offset 后与 `ends` 做贪心一对一配对。"""
    used: set[int] = set()
    pairs: list[tuple[float, float]] = []
    for k in kills:
        want = k + offset
        best = None
        for i, e in enumerate(ends):
            if i in used:
                continue
            d = abs(e - want)
            if d <= tol and (best is None or d < best[0]):
                best = (d, i)
        if best is not None:
            used.add(best[1])
            pairs.append((k, ends[best[1]]))
    return pairs


def analyze(record, tol: float = MATCH_TOL,
            span: float = SEARCH_SPAN, step: float = SEARCH_STEP) -> dict:
    """对齐"击毁时刻"与"线路结束时刻", 找出对不上的那些击毁。

    返回 dict:
      * `kills`    : 本局击毁消息(已合并去重), `[{id, t, msg}]`
      * `offset`   : 最佳平移量(秒), 即 `end ≈ kill.time + offset`
      * `pairs`    : 配对成功的 `[(kill_time, end_time)]`
      * `unmatched`: 没配上的击毁时刻列表(候选"摄像机那位玩家的死亡时刻")
      * `silent`   : 活到最后(dead=0 且结束得很晚)的线路 `[(life_id, t1, 最后移动时刻)]`
    """
    kills = merge_kills(record)
    kill_times = [k["t"] for k in kills]

    ends: list[float] = []
    ends_owner: list[tuple[str, str]] = []      # (side, life_id) 与 ends 同序
    silent: list[tuple[str, float, float | None]] = []
    for side in ("A", "B"):
        for life in record.lives(side):
            pts = life.points
            if not pts:
                continue
            t1 = pts[-1].t
            if getattr(life, "dead", False):
                ends.append(t1)
                ends_owner.append((side, life.life_id))
            elif t1 >= (record.duration or 0.0) - 10.0:
                # 活到最后的: 这正是"摄像机挂着那位"的形状
                silent.append((life.life_id, t1, last_move_time(pts)))
    ends.sort()

    best_offset, best_pairs = 0.0, []
    if kill_times and ends:
        # 静默线路(活到最后的那些)的"最后移动时刻"也是锚点: 摄像机挂着的那位玩家
        # 阵亡后残骸不再移动, 所以**死亡时刻 ≈ 最后移动时刻**。把"未配对的击毁"能
        # 否与这些锚点吻合也算进评分 —— 否则偏移会有歧义(实测: 同样的配对数下,
        # -15s 与 +5s 都能成立, 只有把静默线路算进来才能定下唯一解)。
        anchors = [lm for _lid, _t1, lm in silent if lm is not None]
        steps = int(round(span / step))
        best_score: tuple[int, float] | None = None
        for i in range(-steps, steps + 1):
            off = i * step
            pairs = _pair(kill_times, ends, off, tol)
            if not pairs:
                continue
            err = sum(abs(e - (k + off)) for k, e in pairs)
            matched = {round(k, 3) for k, _e in pairs}
            spare = [k for k in kill_times if round(k, 3) not in matched]
            hits = sum(1 for k in spare
                       if any(abs((k + off) - lm) <= tol for lm in anchors))
            score = (len(pairs) + hits, -err)
            if best_score is None or score > best_score:
                best_score, best_offset, best_pairs = score, off, pairs

    matched_kill = {round(k, 3) for k, _e in best_pairs}
    unmatched = [k for k in kill_times if round(k, 3) not in matched_kill]

    return {
        "kills": kills,
        "offset": best_offset,
        "pairs": best_pairs,
        "unmatched": unmatched,
        "silent": silent,
    }


def describe(report: dict) -> str:
    """把分析结果压成一段便于看日志的文字。"""
    kills = report.get("kills") or []
    pairs = report.get("pairs") or []
    unmatched = report.get("unmatched") or []
    silent = report.get("silent") or []
    if not kills:
        return "hudmsg 里没有本局的击毁消息(抓取失败或这局没人阵亡)"
    parts = [
        "击毁 %d 条(含对空, 只用于对齐) / 与线路结束配对 %d 条 (偏移 %+.1fs)"
        % (len(kills), len(pairs), report.get("offset") or 0.0),
        "未配对击毁 %d 条(含对空击杀, 不作为候选)" % len(unmatched),
    ]
    if silent:
        parts.append("活到最后的线路 %d 条: %s"
                     % (len(silent),
                        ", ".join("%s(末 %.0fs, 最后移动 %.0fs, 静止 %.0fs %s)"
                                  % (lid.split(":")[-1], t1, lm or 0.0,
                                     (t1 - (lm or t1)),
                                     "可判定" if (lm is not None
                                                and (t1 - lm) >= STATIC_MIN)
                                     else "在动")
                                  for lid, t1, lm in silent[:6])))
    return " | ".join(parts)


def pick_death_times(report: dict, tol: float = MATCH_TOL) -> list[tuple[str, float]]:
    """给"死后的残骸"型静默线路找死亡时刻。

    **不从"未配对击毁"里挑** —— `hudmsg` 含**对空击杀**(玩家打飞机、飞机互咬),
    而我们只录地面单位, 未配对的数量会远超"每队 1 条", 拿它当候选会被污染。
    改为: 对每条静默线路, 直接用它的"最后移动时刻"去 hudmsg 里找映射后最近的击毁。

    只认"`t1 - 最后移动时刻 >= STATIC_MIN`"的线 —— 那是**死后残骸不再移动**的特征;
    真活到最后的玩家一直在动, 这个差值接近 0, 不会被误改。
    """
    offset = report.get("offset") or 0.0
    kills = [k["t"] for k in (report.get("kills") or [])]
    used: set[float] = set()
    out: list[tuple[str, float]] = []
    for lid, t1, lm in report.get("silent") or []:
        if lm is None or (t1 - lm) < STATIC_MIN:
            continue                      # 一直在动 → 真活着, 不动它
        want = lm - offset                # end ≈ kill + offset → kill = end - offset
        best = None
        for t in kills:
            if t in used:
                continue                  # 一条击毁只能对应一条线路
            d = abs(t - want)
            if d <= tol and (best is None or d < best[0]):
                best = (d, t)
        if best is not None:
            used.add(best[1])
            out.append((lid, lm))
    return out
