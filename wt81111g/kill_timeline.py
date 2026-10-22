"""用 `/hudmsg` 的击毁消息反推"摄像机挂着的那位玩家"到底什么时候死。

============================================================= 为什么需要它
8111 的 `/map_obj.json` 只给**摄像机所在那一方**的单位。录制全局 A/B 队路线时,
会刻意把视角挂在我方一位玩家身上(这样 8111 才会报这一方), 于是“摄像机挂着的那位”
的死亡时刻拿不到:

* 他阵亡后 8111 **不会**让 icon 立刻消失 —— 那条线继续上报残骸, 一直画到
  "他重生那一刻"才断(用户 2026-10-22 实测口径: 他一出新车, 上一条命的残骸 icon
  才消失);
* 如果他就此退出对局、而比赛还没结束, 残骸会一直上报到录制结束;
* **其余玩家 icon 正常消失** → "消失即击毁"的判死对他们完全有效, 不用修正。

所以每队**只有**“摄像机挂着那位”的一两条线(一条命一条)需要修正, 表现为
“阵亡时刻 = 重生时刻 / 录制结束”。认这几条线靠**颜色和 icon 两个线索**
(见 `is_player_line`): `icon` 不可靠(8111 同一辆车有时给 `Player`、有时给真实
类别), 而自己黄 `#faC81E` 一直在。

============================================================== 为什么能反推
`/hudmsg` 的 `damage` 里有本局全部击杀文本(回放模式下 `events` 是空的):

    {"id": 1374, "time": 475, "msg": "cruasane (护卫坦克 57) 击毁了 X (黄蜂-AKM)"}

`time` 是**比赛秒数**, 从服务器端开始录像就算 —— 但我们不知道它和本软件 `t=0`
(第一名玩家重生) 差多少, 而且准备阶段那个"15 秒"是假数字(游戏会在最后几秒把
时间拉长), 所以**绝对偏移不可用**(用户 2026-10-22 实测确认)。

于是分两步:

1. **对齐时间轴**: 用"icon 正常消失那些线的结束时刻 ↔ 击毁时刻"拟合出本队的
   平移量(见 `estimate_shift` / `side_shift`)。这一步必须拿**其他玩家**的数据
   当锚点 —— 观战对象的线尾是重生时刻/录制结束, 本来就不是死亡时刻。
2. **按昵称认领**: `/hudmsg` 的击毁文本里带**双方昵称**(`A (载具) 击毁了 B (载具)`),
   而用户知道录制时镜头挂在谁身上 —— 界面上点点选出来即可(见 `rank_watchers`)。

有了这两样, 每条 `icon == "Player"` 的线取"时间段内**最后一次**属于他的阵亡"即可:
每段只可能死一次(死了才会重生、才会开下一条线), 所以**不需要任何"停没停"的猜测**
(早期版本靠"线尾永久静止段"推死亡时刻, 2026-10-22 整体删除 —— 用户指出的三种
真实情形里它一次都用不上, 反而会把正确答案否掉)。

本模块**只做计算**, 不落盘; 真正截断线路在 `RouteStore.set_watchers` 里。
"""
from __future__ import annotations

import bisect
import logging

log = logging.getLogger("kill_timeline")

ZW_SPACE = "\u200b"          # 8111 的文本里中文字符之间夹着零宽空格
# 两种“有人没了”的固定文本(用户 2026-10-22 实测):
#
#   击毁了:   `击杀者 (载具) 击毁了 受害者 (载具)`   → 受害者在动词**后面**
#   已被摧毁: `玩家 (载具) 已被摧毁`                → 玩家就在动词**前面**
#
# “已被摧毁” = 弃车 / 被河流淹死 / 撞地坠毁 —— 没有击杀者, 但确实阵亡。
KILL_MARK = "击毁了"
DESTROYED_MARK = "已被摧毁"
DEATH_MARKS = (KILL_MARK, DESTROYED_MARK)
PLAYER_ICON = "player"       # 8111 给"摄像机挂着的那位玩家"用的 icon(比较时转小写)
# 自己(摄像机那辆车)的黄。`icon` 不可靠时靠它认人, 见 `is_player_line`。
SELF_COLOR = "#faC81E"
# 阵亡时刻与线路时间段比对的容差(秒): 拟合出的平移量本身有 ±1s 抖动, 而线尾
# (他重生或退出的那一刻)也可能比真实死亡晚几秒。
CAND_TOL = 3.0


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


def read_damage(client, cursor: int) -> list[dict]:
    """取比 `cursor` 新的**全部** damage 消息: `[{id, t, msg}]`(按 id 升序)。

    不只看击毁: 8111 的 `damage` 里还混着“引燃了 / 获得嘉奖”这类条目, 以及
    **弃车、被淹死、坠毁**这类“不是被别人打死”的阵亡(用户 2026-10-22 实测: 它们
    同样是一行“<昵称> (载具) 已被摧毁”)。先原样取回来, 由调用方按
    `DEATH_MARKS` 分捡。
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
        out.append({"id": rid, "t": t, "msg": clean_text(row.get("msg"))})
    out.sort(key=lambda r: r["id"])
    return out


def is_death_msg(msg) -> bool:
    """这条消息是不是“有人阵亡”(击毁了 / 已被摧毁)。

    `引燃了`(只是着火, 不一定死) 与 `获得嘉奖` 都不算。
    """
    text = clean_text(msg)
    return any(mark in text for mark in DEATH_MARKS)


def read_new_deaths(client, cursor: int) -> list[dict]:
    """取比 `cursor` 新的**阵亡**消息: `[{id, t, msg}]`(按 id 升序)。"""
    return [r for r in read_damage(client, cursor) if is_death_msg(r["msg"])]


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
    截断后 `points` 里已经没有残骸尾巴了 —— 改选另一位观战玩家时必须基于原始点,
    否则这条线的时间段(起点/终点)会被上一次的修正吃掉, 判断会跟着错。
    """
    trimmed = getattr(life, "trimmed", None)
    return list(trimmed) if trimmed else list(life.points)


def deaths_in(deaths, shift: float, t0: float, t1: float,
              tol: float = CAND_TOL) -> list[float]:
    """他的阵亡(比赛时刻)里, 换算到录制时间轴后落在 `[t0-tol, t1+tol]` 的那些。"""
    out = []
    for d in deaths or []:
        try:
            t = float(d) - shift
        except (TypeError, ValueError):
            continue
        if t0 - tol <= t <= t1 + tol:
            out.append(t)
    out.sort()
    return out


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

    `deaths` 是**他死了**的比赛时刻(升序), 用来精确指定“哪一次是他的死亡”。
    两种文本都算阵亡(用户 2026-10-22 实测格式):

    * `击杀者 (载具) 击毁了 他 (载具)` —— 受害者在动词**后面**;
    * `他 (载具) 已被摧毁` —— 弃车 / 被淹死 / 坠毁, **没有击杀者**, 他就在动词前面。

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
        if DESTROYED_MARK in msg:
            victim, vvehicle = split_actor(msg.partition(DESTROYED_MARK)[0])
            if victim and vvehicle and not victim.startswith("weapons/"):
                row_of(victim, vvehicle)["deaths"].append(t)
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

    故意排除观战对象那几条(见 `is_player_line`): 他阵亡后 8111 继续上报残骸,
    线尾是重生/录制结束而不是死亡时刻 —— 拿它拟合偏移会把整队的对齐拉歪。
    """
    ends = []
    for life in record.lives(side):
        pts = raw_points(life)
        if not pts or not getattr(life, "dead", False):
            continue
        if is_player_line(life):
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


def is_player_line(life) -> bool:
    """这条线是不是“摄像机挂着的那辆车”。

    三条线索任一命中就算(2026-10-22 用户重录诺曼底时实测, 只认 `icon` 会漏):

    1. `watched` —— 录制时记的持久标记(任何一帧 icon=Player 或颜色是自己黄);
    2. `color == #faC81E` —— 旧数据没有 `watched` 字段时的兵底 (实测一直准确);
    3. `icon == "Player"` —— 8111 文档口径, 但不可靠(同一辆车会变成 `SAM` 等)。
    """
    if bool(getattr(life, "watched", False)):
        return True
    if (life.icon or "").strip().lower() == PLAYER_ICON:
        return True
    return (life.color or "").strip().lower() == SELF_COLOR.lower()


def player_lines(record, side: str) -> list:
    """这一队里“摄像机挂着的那位玩家”的线路。

    **一条命一条**: 他一出新车, 上一条命的残骸 icon 就消失、那条线正常结束,
    新车的线则从重生点重新开始。所以同一队可能有多条(他换过几次车就有几条)。
    """
    return [lf for lf in record.lives(side) if lf.points and is_player_line(lf)]


def player_segments(record, side: str) -> list[tuple]:
    """这一队的 Player 线按起点排好 → `[(line, t0, t1)]`。

    `t0` = 这一命被 8111 首次上报的时刻, `t1` = 这条线的最后一个点。
    他阵亡后车身不再动, 线会一直延伸到"他重生那一刻"(残骸尾巴, 比真实死亡
    晚几秒); 最后一条线如果他是退出对局/活到结束, 尾巴能拖到录制结束。

    这些时间段**互不重叠**、且一段只可能有一次他的阵亡 —— 这就是"按昵称认领
    阵亡"的全部依据, 不需要任何静止/停车推断。
    """
    out = []
    for life in sorted(player_lines(record, side),
                       key=lambda lf: raw_points(lf)[0].t):
        pts = raw_points(life)
        out.append((life, pts[0].t, pts[-1].t))
    return out


def resolve_side(record, side: str, name: str, shift: float, hits: int,
                 stats: dict) -> dict:
    """算出 `side` 队"摄像机那位玩家"**每一段**线路的死亡时刻。

    返回 dict: `ok`(至少修正了一段); `segments` 段数; `death` 最后一段的阵亡时刻;
    `fixes` 逐段结果(`life_id`/`t0`/`t_end`/`deaths`/`death`/`tail`/`ok`/`note`);
    `shift`/`hits` 是这一队的时间轴对齐信息; `note` 一句话说明。
    """
    segs = player_segments(record, side)
    row: dict = {"ok": False, "side": side, "watcher": name, "shift": shift,
                 "hits": hits, "segments": len(segs), "fixes": [],
                 "death": 0.0, "note": ""}
    if not segs:
        row["note"] = "这一队没有观战玩家的线路(icon=Player), 不用修正"
        return row
    deaths = (stats.get(name) or {}).get("deaths") or []
    if not deaths:
        row["note"] = "本局没有这位玩家的阵亡记录"
        return row
    for life, t0, t1 in segs:
        hit = deaths_in(deaths, shift, t0, t1)
        fix = {"life_id": life.life_id, "t0": t0, "t_end": t1,
               "deaths": list(hit), "ok": False, "death": 0.0, "kill": 0.0,
               "tail": 0.0, "note": ""}
        if hit:
            death = hit[-1]         # 段内最后一次(正常一段只有一次)
            fix.update(ok=True, death=death, kill=death + shift,
                       tail=max(0.0, t1 - death))
            fix["note"] = ("阵亡 %.1fs, 这条线之后还上报了 %.1fs 残骸"
                           % (death, fix["tail"]))
        else:
            fix["note"] = "这一段没有他的阵亡 → 他在这段活到结束"
        row["fixes"].append(fix)
    fixed = [f for f in row["fixes"] if f["ok"]]
    if fixed:
        row["ok"] = True
        row["death"] = fixed[-1]["death"]
        row["note"] = "修正 %d/%d 段" % (len(fixed), len(segs))
    else:
        row["note"] = "他的阵亡都不在这些线的时间段内 → 不修正(他活到录制结束)"
    return row


def rank_watchers(record, side: str, kills=None, shift=None,
                  hits: int | None = None) -> list[dict]:
    """候选观战玩家(界面用), 第一位最可能。

    排序依据是**时间段命中数**: 观战对象每死一次才会重生、才会开下一条 Player 线,
    所以"他的阵亡刚好落在这些段里"是最硬的信号(早期那套"离停车时刻多近"的排序
    2026-10-22 已删除)。但同一时刻往往好几个人在死, 打平很常见, 所以界面
    **不预选**, 让用户扫一眼名单自己点。
    """
    if kills is None:
        kills = merge_kills(record)
    if shift is None:
        shift, got = side_shift(record, side, kills)
        hits = got if hits is None else hits
    stats = player_stats(kills)
    out = []
    for name, st in stats.items():
        if not st.get("vehicle") or not st.get("deaths"):
            continue            # 只打死过别人、自己没死过的, 不可能是“阵亡待修正”
        row = resolve_side(record, side, name, shift, int(hits or 0), stats)
        row["vehicle"] = st.get("vehicle") or ""
        row["deaths"] = list(st.get("deaths") or [])
        row["kills"] = int(st.get("kills") or 0)
        row["fixed_count"] = sum(1 for f in row["fixes"] if f["ok"])
        out.append(row)
    out.sort(key=lambda r: (-r["fixed_count"],
                            len(r["deaths"]) - r["fixed_count"],
                            -len(r["deaths"])))
    return out


def resolve_deaths(record, watchers: dict, kills=None) -> dict[str, dict]:
    """`{"A": 结果, "B": 结果}` —— 两队各自"摄像机那位玩家"逐段线路的死亡时刻。"""
    if kills is None:
        kills = merge_kills(record)
    stats = player_stats(kills)
    base = pooled_shift(record, kills)
    out: dict[str, dict] = {}
    for side in ("A", "B"):
        name = str((watchers or {}).get(side) or "").strip()
        if not name:
            out[side] = {"ok": False, "side": side, "watcher": "",
                         "note": "未指定", "segments": 0, "fixes": [],
                         "death": 0.0, "shift": 0.0, "hits": 0}
            continue
        shift, hits = side_shift(record, side, kills, base)
        out[side] = resolve_side(record, side, name, shift, hits, stats)
    return out


def summarize(resolved: dict) -> str:
    """把 `resolve_deaths` 的结果压成一句话(日志用)。"""
    parts = []
    for side in ("A", "B"):
        row = (resolved or {}).get(side) or {}
        name = row.get("watcher") or "?"
        fixed = [f for f in row.get("fixes") or [] if f.get("ok")]
        if row.get("ok") and fixed:
            parts.append("%s 队 %s → 修正 %d/%d 段, 阵亡 %s"
                         % (side, name, len(fixed), row.get("segments") or 0,
                            "、".join("%.1fs" % f["death"] for f in fixed)))
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
            if is_player_line(life):
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
