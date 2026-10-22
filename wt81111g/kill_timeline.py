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

能用的只有两条线索, 合起来才能算准:

1. **位置**: 他阵亡后车变成残骸不再移动 → 这条线的"永久静止段"起点就是
   "他停住不动"的那一刻(见 `tail_stop`)。实测(诺曼底 2026-10-22): "刚停车就
   被打死"的情形下这个时刻与真实死亡时刻**完全一致**(179.4s ↔ 3:28)。
   但它只是**下界**: 停稳之后还可能被打死(实测有一位停了 9.8 秒才死)。
2. **昵称**: `/hudmsg` 的击毁文本里带**双方昵称**(`A (载具) 击毁了 B (载具)`),
   而用户知道录制时镜头挂在谁身上 —— 界面上点点选出来即可(见 `rank_watchers`)。
   有了昵称就能从本局全部击毁里挑出他唯一的那次阵亡, 不必靠猜。

绝对偏移仍然不可用(回放从服务器端就开始计时、准备阶段的 15 秒还是假数字),
所以先按"线路结束时刻 ↔ 击毁时刻"拟合出这一队的平移量(见 `estimate_shift`),
再把击毁时刻换算回本软件的时间轴。

本模块**只做计算**, 不落盘; 真正截断线路在 `RouteStore.set_watchers` 里。
"""
from __future__ import annotations

import bisect
import logging

log = logging.getLogger("kill_timeline")

ZW_SPACE = "\u200b"          # 8111 的文本里中文字符之间夹着零宽空格
KILL_MARK = "击毁了"          # 只认击杀; "引燃了" / "获得嘉奖" 不算死亡
PLAYER_ICON = "player"       # 8111 给"摄像机挂着的那位玩家"用的 icon(比较时转小写)
MATCH_TOL = 2.0              # 配对容差(秒): 两队时间轴本身还差 1~2 秒
# "永久静止"判定半径(归一化坐标)。4km 的地图上 0.00025 ≈ 1m。
# 太大会把**刹车滑行**当成已经停住: 实测 B2 那条线用 1.6m 会把停车时刻
# 从 364.6s 提前到 351.6s(多算 13 秒); 太小又会被心跳点 0.45m 的抖动挡住。
STOP_RADIUS = 0.00025
# 静止段至少这么长才可能是残骸(死亡后不再动)。真活到最后的玩家一直在跑。
STATIC_MIN = 5.0
# 把昵称的阵亡时刻往下限方向放宽这么多秒再找 —— 拟合出的平移量本身有 ±1s 抖动。
CAND_TOL = 2.0


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


def raw_points(life) -> list:
    """这条线的**原始**点序列(已修正过的线返回修正前那一份)。

    修正的做法是"截断到死亡时刻 + 补一个合成点"(见 `RouteStore.set_watchers`),
    截断后 `points` 里已经没有残骸尾巴了 —— 重新指定观战玩家时必须基于原始点,
    否则算不出"他什么时候停住"。
    """
    trimmed = getattr(life, "trimmed", None)
    return list(trimmed) if trimmed else list(life.points)


def tail_stop(points, radius: float = STOP_RADIUS,
              min_tail: float = STATIC_MIN) -> tuple[float, int] | None:
    """车"停住不动"的那一刻 = 尾部永久静止段的起点。返回 `(时刻, 下标)`。

    **不能**用"最后一次位置变化": 静止时程序每 15 秒补一个心跳点, 心跳点与残骸
    之间会有 0.4~0.5m 的抖动, 那一下会被误当成"还在动"(实测 A23: 车 179.4s 就
    停了, 心跳点却把"最后移动时刻"推迟到 194.4s, 差了整整 15 秒)。

    做法: 取**最后一个点**当锚, 往前一直走到离开 `radius` 为止。只认
    "从这里到结尾都不动", 中途停一下再开走不会被误判。静止段短于 `min_tail`
    就返回 None(说明到录制结束他还在动 → 大概率没死)。

    锚必须取**线尾那一个点**, 不能用尾部几点的平均值: 坦克停车前有 1~3m 的
    刹车滑行, 用平均值会把停车时刻提前十几秒(实测 364.6s → 351.0s)。
    """
    n = len(points)
    if n < 2:
        return None
    ax, ay = points[-1].x, points[-1].y
    i = n - 1
    while i > 0:
        p = points[i - 1]
        if ((p.x - ax) ** 2 + (p.y - ay) ** 2) ** 0.5 > radius:
            break
        i -= 1
    if points[-1].t - points[i].t < min_tail:
        return None
    return points[i].t, i


def split_actor(text) -> tuple[str, str]:
    """`昵称 (载具)` → `(昵称, 载具)`。

    载具名自己还带括号(如 `(90 式战车 B 型 (富士教导团))`), 所以只按**第一个**
    `" ("` 切; 没有括号的(如 `weapons/su_r_77_1/short`、`IRIS-T SLM/发射车`)
    返回空载具 —— 那种是武器/AI, 不是玩家。
    """
    text = clean_text(text)
    i = text.find(" (")
    if i <= 0:
        return text, ""
    return text[:i].strip(), text[i + 2:].strip().rstrip(")").strip()


def player_stats(kills) -> dict[str, dict]:
    """`{昵称: {"deaths": [...], "kills": n, "vehicle": "..."}}`。

    `deaths` 是**他被打死**的比赛时刻(升序), 用来精确指定"哪一次是他的死亡"。
    打死别人的次数只用来给候选名单排序(往往和谁被摄像机挂着没关系)。
    """
    out: dict[str, dict] = {}

    def row_of(name: str, vehicle: str) -> dict:
        row = out.setdefault(name, {"deaths": [], "kills": 0, "vehicle": ""})
        if vehicle and not row["vehicle"]:
            row["vehicle"] = vehicle
        return row

    for k in kills or []:
        msg = clean_text(k.get("msg"))
        try:
            t = float(k.get("t") or 0.0)
        except (TypeError, ValueError):
            continue
        head, sep, tail = msg.partition(KILL_MARK)
        if not sep:
            continue
        killer, kvehicle = split_actor(head)
        if killer and kvehicle:
            row_of(killer, kvehicle)["kills"] += 1
        victim, vvehicle = split_actor(tail)
        if victim and vvehicle and not victim.startswith("weapons/"):
            row_of(victim, vvehicle)["deaths"].append(t)
    for row in out.values():
        row["deaths"].sort()
    return out


def estimate_shift(ends, kill_times, lo: float = -30.0, hi: float = 900.0,
                   tol: float = 1.5) -> tuple[float, int]:
    """拟合"比赛时刻 = 录制时刻 + shift"(秒), 返回 `(shift, 配对条数)`。

    `ends` 是**icon 正常消失**那些线路的结束时刻(录制时间轴), 它们约等于对应那次
    击毁的比赛时刻减去 shift。先粗扫一遍找配对最多的位置, 再在附近细扫一次。

    整局只用一个 shift 是不够的? 实测两队各自录制会差 1 秒左右(见 `side_shift`),
    所以按队分别拟合。
    """
    ks = sorted(float(k) for k in kill_times or [])
    es = sorted(float(e) for e in ends or [])
    if not ks or not es:
        return 0.0, 0

    def score(shift: float) -> tuple[int, float]:
        used: set[int] = set()
        hits, err = 0, 0.0
        for e in es:
            want = e + shift
            j = bisect.bisect_left(ks, want)
            pick = None
            for idx in (j - 1, j):
                if 0 <= idx < len(ks) and idx not in used:
                    d = abs(ks[idx] - want)
                    if d <= tol and (pick is None or d < pick[0]):
                        pick = (d, idx)
            if pick is not None:
                used.add(pick[1])
                hits += 1
                err += pick[0]
        return hits, -err

    def scan(a: float, b: float, step: float) -> tuple[float | None, int]:
        best, best_hits, best_score = None, 0, None
        n = int(round((b - a) / step))
        for i in range(n + 1):
            s = a + i * step
            sc = score(s)
            if sc[0] and (best_score is None or sc > best_score):
                best_score, best, best_hits = sc, s, sc[0]
        return best, best_hits

    coarse, _ = scan(lo, hi, 1.0)
    if coarse is None:
        return 0.0, 0                 # 一条都对不上 → 不要瞎平移(否则会算飞)
    fine, hits = scan(coarse - 1.5, coarse + 1.5, 0.05)
    if fine is None:
        return round(coarse, 2), 0
    return round(fine, 2), hits


def side_ends(record, side: str) -> list[float]:
    """这一队里**icon 正常消失**那些线路的结束时刻(录制时间轴)。

    故意排除 `icon=Player` 那条: 摄像机那位阵亡后 8111 继续上报残骸, 他的线尾是
    录制结束而不是死亡时刻 —— 拿它拟合偏移会把整队的对齐拉歪。
    """
    ends = []
    for life in record.lives(side):
        pts = raw_points(life)
        if not pts or not getattr(life, "dead", False):
            continue
        if (life.icon or "").lower() == PLAYER_ICON:
            continue
        ends.append(pts[-1].t)
    return ends


def pooled_shift(record, kills=None) -> float:
    """**两队合在一起**拟合的全局平移量(比赛时刻 = 录制时刻 + 返回值)。

    必须先把两队合起来定一个基准: 单看一队时,"线路结束"里有大量不是死亡的断点
    (单位跑出摄像机上报范围), 噪声能把偏移拟合到完全错误的 140 多秒上(实测);
    合起来则有 20~30 条真正对得上的结束时刻, 噪声盖不过去。
    """
    if kills is None:
        kills = merge_kills(record)
    ends = side_ends(record, "A") + side_ends(record, "B")
    return estimate_shift(ends, [k["t"] for k in kills], tol=1.2)[0]


def side_shift(record, side: str, kills=None, base: float | None = None
               ) -> tuple[float, int]:
    """某一队的时间轴平移量 + 配对条数。

    A/B 是两次独立观战, `t=0` 都在"看到第一辆车"那一刻, 但两次回放的实际起点
    能差 1 秒左右 —— 所以在全局基准附近 ±3 秒内再微调一次, 超出就相信全局值。
    """
    if kills is None:
        kills = merge_kills(record)
    kill_times = [k["t"] for k in kills]
    if base is None:
        base = pooled_shift(record, kills)
    shift, hits = estimate_shift(side_ends(record, side), kill_times,
                                 lo=base - 3.0, hi=base + 3.0, tol=1.2)
    if hits < 3:                      # 这一队能对上的太少 → 用全局值更稳
        return round(base, 2), hits
    return shift, hits


def player_lines(record, side: str) -> list:
    """这一队里"摄像机挂着的那位玩家"的线路(`icon` = `Player`)。每队最多 1 条。"""
    return [lf for lf in record.lives(side)
            if (lf.icon or "").lower() == PLAYER_ICON and lf.points]


def _resolve_one(record, side: str, name: str, shift: float, hits: int,
                 stats: dict, ends: list[float] | None = None) -> dict:
    """算出"这位玩家在什么时刻阵亡"(`side` 队录制时间轴上的秒数)。

    返回 dict: `ok` 成功与否; `stop` 停住时刻; `kill` 对应那次击毁的比赛时刻;
    `death` 死亡时刻(录制时间轴); `gap` 停住之后多久才死;
    `note` 一句话说明; 失败时 `note` 写清原因。
    """
    row: dict = {"ok": False, "side": side, "watcher": name, "shift": shift,
                 "hits": hits, "stop": 0.0, "kill": 0.0, "death": 0.0,
                 "gap": 0.0, "life_id": "", "note": ""}
    lines = player_lines(record, side)
    if not lines:
        row["note"] = "这一队没有观战玩家的线路(icon=Player), 无法定位"
        return row
    life = max(lines, key=lambda lf: raw_points(lf)[0].t)
    pts = raw_points(life)
    row["life_id"] = life.life_id
    t0, t_end = pts[0].t, pts[-1].t
    stop = tail_stop(pts)
    row["stop"] = stop[0] if stop else 0.0
    deaths = (stats.get(name) or {}).get("deaths") or []
    if not deaths:
        row["note"] = "本局没有这位玩家的阵亡记录"
        return row
    # 只看落在这一段观战时间里的阵亡(把比赛时刻换算到录制时间轴再比)
    window = [d - shift for d in deaths
              if t0 - CAND_TOL <= d - shift <= t_end + CAND_TOL]
    if not window:
        row["note"] = "他的阵亡不在这一段观战时间里(可能活到录制结束)"
        return row
    floor = (stop[0] if stop else t0) - CAND_TOL
    after = [d for d in window if d >= floor]
    if not after:
        # 他的阵亡全部早于“车停住不动”那一刻 —— 而他停下来就是因为被打死,
        # 所以这个人**不可能是**摄像机挂着的那位(这个信息还能帮用户排除)。
        row["note"] = "他的阵亡都早于这条线停住的时刻, 不可能是观战对象"
        return row
    death = min(after[0], t_end)
    row.update(ok=True, death=death, kill=death + shift,
               gap=death - (stop[0] if stop else t0))
    row["note"] = "阵亡于停住之后 %.1f 秒" % row["gap"]
    return row


def rank_watchers(record, side: str, kills=None, shift=None,
                  hits: int | None = None) -> list[dict]:
    """候选观战玩家(界面用), 第一位最可能。

    排序: 先看能不能判定, 再按“他的阵亡时刻离这队停住不动有多近”。注意这只是
    把可能性高的人往前提 —— 同一时刻往往好几个人在死, 真正的“最可能”也可能
    排到第三四位, 所以界面不预选, 让用户看一眼名单自己点。
    """
    if kills is None:
        kills = merge_kills(record)
    if shift is None:
        shift, got = side_shift(record, side, kills)
        hits = got if hits is None else hits
    stats = player_stats(kills)
    ends = side_ends(record, side)
    out = []
    for name, st in stats.items():
        if not st.get("vehicle") or not st.get("deaths"):
            continue            # 只打死过别人、自己没死过的, 不可能是“阵亡待修正”
        row = _resolve_one(record, side, name, shift, int(hits or 0), stats, ends)
        row["vehicle"] = st.get("vehicle") or ""
        row["deaths"] = list(st.get("deaths") or [])
        row["kills"] = int(st.get("kills") or 0)
        out.append(row)
    out.sort(key=lambda r: (not r["ok"], abs(r["gap"]) if r["ok"] else 0.0))
    return out


def resolve_deaths(record, watchers: dict, kills=None) -> dict[str, dict]:
    """`{"A": 结果, "B": 结果}` —— 两队各自"摄像机那位玩家"的死亡时刻。"""
    if kills is None:
        kills = merge_kills(record)
    stats = player_stats(kills)
    base = pooled_shift(record, kills)
    out: dict[str, dict] = {}
    for side in ("A", "B"):
        name = str((watchers or {}).get(side) or "").strip()
        if not name:
            out[side] = {"ok": False, "side": side, "watcher": "", "note": "未指定"}
            continue
        shift, hits = side_shift(record, side, kills, base)
        out[side] = _resolve_one(record, side, name, shift, hits, stats,
                                 side_ends(record, side))
    return out


def summarize(resolved: dict) -> str:
    """把 `resolve_deaths` 的结果压成一句话(界面/日志用)。"""
    parts = []
    for side in ("A", "B"):
        row = (resolved or {}).get(side) or {}
        name = row.get("watcher") or "?"
        if row.get("ok"):
            parts.append("%s 队 %s → 阵亡 %.1fs(停住后 %.1fs)"
                         % (side, name, row["death"], row["gap"]))
        else:
            parts.append("%s 队 %s → 不修正(%s)"
                         % (side, name, row.get("note") or ""))
    return " | ".join(parts)


def analyze(record, tol: float = 1.5) -> dict:
    """诊断用: 把全部线路的结束时刻与击毁时刻对齐, 报出平移量和配对数。

    返回 `{kills, shift, pairs, unmatched, player_lines}`; `shift` 的含义是
    `比赛时刻 = 录制时刻 + shift`。两队分别算(见 `side_shift`), 这里只是为了看
    一眼对齐质量, **不参与死亡时刻的判定**。
    """
    kills = merge_kills(record)
    kill_times = [k["t"] for k in kills]
    ends: list[float] = []
    players = []
    for side in ("A", "B"):
        for life in record.lives(side):
            pts = raw_points(life)
            if not pts or not getattr(life, "dead", False):
                continue
            if (life.icon or "").lower() == PLAYER_ICON:
                players.append((side, life.life_id))
                continue
            ends.append(pts[-1].t)
    shift, _hits = estimate_shift(ends, kill_times, tol=tol)
    pairs = [(k, k + shift) for k in kill_times
             if any(abs(e - (k + shift)) <= tol for e in ends)]
    unmatched = [k for k in kill_times if k not in {p[0] for p in pairs}]
    return {"kills": kills, "shift": shift, "pairs": pairs,
            "unmatched": unmatched, "player_lines": players}


def describe(report: dict) -> str:
    """把 `analyze` 的结果压成一段便于看日志的文字。"""
    kills = report.get("kills") or []
    if not kills:
        return "hudmsg 里没有本局的击毁消息(抓取失败或这局没人阵亡)"
    parts = [
        "击毁 %d 条(含对空, 只用于对齐) / 与线路结束配对 %d 条 (比赛 = 录制 %+.1fs)"
        % (len(kills), len(report.get("pairs") or []), report.get("shift") or 0.0),
        "未配对击毁 %d 条(含对空击杀, 不作为候选)"
        % len(report.get("unmatched") or []),
    ]
    players = report.get("player_lines") or []
    if players:
        parts.append("待指定的观战玩家线路: %s"
                     % ", ".join("%s 队 %s" % (s, lid.split(":")[-1])
                                 for s, lid in players))
    return " | ".join(parts)
