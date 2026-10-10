"""对局监控: 轮询 8111,收集昵称,比对黑名单,发出告警。"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import queue
import random
import threading
import time

import requests

from PyQt6.QtCore import QObject, pyqtSignal

from .api8111 import WT8111
from .blacklist import BlacklistStore
from .config import WT_BASE_URL
from .garage import GARAGE_CONFIRM_TICKS, PROBE_BYTES, in_map_by_image
from .nickname_cache import NicknameCache
from .nickname_collector import parse_hudmsg_names
from .nickname_sync import fetch_shared_table
from .nickname_util import (
    BattleNickIndex, clean_wtlive_nickname,
)
from .warthunder import (
    STATUS_PERMANENT,
    STATUS_RATE_LIMIT,
    check_feed_latency,
    fetch_profile_best_effort,
)

log = logging.getLogger("monitor")

# 刚收到击杀/发言后, 这么多秒内不把“没有真地图”当成“已回车库”:
# 击杀消息说明人还在对局里, 不该清空本局收集(8111 偶发拿不到图片)。
ACTIVITY_GRACE = 5.0

# 探测 /map.img 的超时: 进对局初期这张图可能要好几秒才回应(几百 KB~几 MB),
# 超时短了会被误判成“不在对局”。
MAP_PROBE_TIMEOUT = 2.0

# 连续多少次请求都失败才认定“8111 断开连接”: 换局那几十秒游戏很忙, 单次超时不算断开。
CONNECT_FAIL_TICKS = 3

# 已经在对局里时, 每多少个 tick 才去探一次 /map.img: 真地图几百 KB~几 MB,
# 每秒完整取一遍既费带宽又让游戏的 8111 变忙(实测换地图那一下 mission/hudmsg 会慢到 2 秒)。
# 出局时仍然每 tick 都探 —— 要尽快发现“进局”。
MAP_PROBE_EVERY = 2


class MonitorWorker(QObject):
    """后台线程轮询 8111 并维护“当前对局昵称”,与黑名单比对。"""

    connection_changed = pyqtSignal(bool)
    new_battle = pyqtSignal()
    battle_ended = pyqtSignal()
    nicknames_updated = pyqtSignal(list)     # 已收集昵称列表
    blacklist_alert = pyqtSignal(str, str, str)  # 昵称, 玩家ID, 原因
    blacklist_found = pyqtSignal(list)       # 本局已发现的黑名单昵称(供叠加层显示)
    profiles_updated = pyqtSignal(dict)      # {player_id: nickname}
    feed_status = pyqtSignal(str, str)       # (level, text) WT Live 连通状态
    feed_requested = pyqtSignal()            # 用户手动点击"检测"
    wtlive_count = pyqtSignal(int)           # 本次会话 WT Live 访问次数
    cache_updated = pyqtSignal()             # 昵称缓存数据库发生变更
    prefetch_progress = pyqtSignal(int, int)  # 正在抓取昵称 (已完成, 总数)
    manual_requested = pyqtSignal()
    nickname_manual_needed = pyqtSignal(str, str)  # (player_id, 当前昵称) 需要交互式浏览器兜底

    def __init__(self, store: BlacklistStore,
                 nickname_cache: NicknameCache | None = None,
                 base_url: str = WT_BASE_URL,
                 poll_interval: float = 1.0,
                 shared_repo_url: str | None = None):
        super().__init__()
        self.store = store
        self.nickname_cache = nickname_cache or NicknameCache()
        self.poll_interval = poll_interval
        # 公开仓库共享表(nickname.json)地址: 自动刷新优先查此表, 命中则免访问 WTLive/官网
        self.shared_repo_url = shared_repo_url
        self.client = WT8111(base_url)
        self.manual_requested.connect(self._on_manual_check)
        self.feed_requested.connect(self._on_feed_check)

        self._stop = threading.Event()
        self._connected = False
        self._conn_fails = 0      # mission 连续失败次数(单次超时不算断开)
        self._msg_fails = 0       # hudmsg/gamechat 连续失败次数(失败不算“没有新消息”)
        self._tick_no = 0         # tick 计数(控制探地图图片的频率)
        self._in_battle = False
        self._garage_ticks = 0   # 连续多少个 tick 只看到车库占位图
        self._last_activity = 0.0  # 最近一次收到击杀/发言的时间
        self._msg_primed = False   # 是否已建立过消息基线(用来“吸收”上一局的旧消息)
        self._cmp_sig: tuple | None = None  # 上次比对的输入签名(避免重复算)        self._battle_sig: str | None = None
        self._gamechat_cursor = -1
        self._evt_cursor = -1
        self._dmg_cursor = -1
        self._collected: set[str] = set()
        self._alerted: set[str] = set()
        self._found_nicks: list[tuple[str, str]] = []  # (昵称, 原因)
        self._profile_cache: dict[str, tuple[str | None, float]] = {}
        self._manual_asked: set[str] = set()  # 已提示过交互兜底的玩家ID(避免反复弹窗)
        # 用独立缓存数据库初始化内存缓存(删除/重加条目后缓存依然有效)
        for _pid, _nick, _ts, _inv in self.nickname_cache.items():
            if _pid and _nick:
                self._profile_cache[_pid] = (_nick, _ts)
        self._prefetch_running = False
        self.auto_update = True  # 是否自动更新24h过期的昵称(缓存窗口'自动更新'控制, 默认开启)
        self._wtlive_count = 0
        self._count_lock = threading.Lock()
        # 跟踪后台 daemon 线程(prefetch/feed_check), 退出时 join 避免对象销毁后仍 emit
        self._bg_threads: list[threading.Thread] = []
        self._bg_lock = threading.Lock()

    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("monitor started")
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                log.exception("monitor tick error: %s", exc)
            self._stop.wait(self.poll_interval)
        log.info("monitor stopped")

    # ------------------------------------------------------------------
    def _tick(self) -> None:
        # 排查用: 记下各阶段耗时(取消息 / 探地图), 慢的时候直接写进日志
        t_tick = time.perf_counter()
        # 用 mission 请求成败判定连通性, 省掉独立的 /state 探测请求(每 tick 少一次请求)。
        # ⚠️ 单次超时**不算**断开(实测换局那几十秒 8111 很忙, 请求偶发要 1~2 秒):
        # 连续 CONNECT_FAIL_TICKS 次都失败才认定断开, 否则只跳过本轮、不动任何状态。
        try:
            mission = self.client.mission(raise_on_error=True)
            self._conn_fails = 0
            connected = True
        except requests.RequestException as exc:
            self._conn_fails += 1
            mission = {}
            connected = self._connected
            if self._conn_fails < CONNECT_FAIL_TICKS:
                log.info("mission 没有回答(%s, 连续第 %d 次) → 本轮跳过, 不改状态",
                         type(exc).__name__, self._conn_fails)
                return
        if connected != self._connected:
            self._connected = connected
            self.connection_changed.emit(connected)
        if not connected:
            if self._in_battle:
                self._end_battle("8111 断开连接")
            self._garage_ticks = 0
            return

        # 击杀/发言收集(先取消息, 再探地图): 地图图片在刚进对局时可能要好几秒才回应,
        # 放在前面会拖慢击杀消息的上报。
        # ⚠️ 不在对局(或本会话第一次轮询)时只“**吸收**”消息: 推进游标、不收集、不匹配。
        # 原因: 8111 的 /hudmsg 是**滚动窗口**, 刚进新对局时里面还留着上一局的消息;
        # 消息 id 跨局只增不重置, 所以只能用“游标之后的新 id”判定“本局新增的”。
        # 这样: 软件启动前就在窗口里的 ID、以及出局期间收到的消息, 都不可能参与本局判断。
        msgs: list = []
        hud: dict = {}
        try:
            msgs = self.client.gamechat(self._gamechat_cursor, raise_on_error=True)
            hud = self.client.hudmsg(self._evt_cursor, self._dmg_cursor, raise_on_error=True)
            self._msg_fails = 0
        except requests.RequestException as exc:
            # 请求没成功 ≠ 没有新消息: 消息还在 8111 的窗口里, 保留游标下次再取
            # (以前这里直接当空数组, 换局那几十秒会被当成“没消息”, 白白等 8111 缓过来)
            self._msg_fails += 1
            log.info("hudmsg/gamechat 没有回答(%s, 连续第 %d 次) → 本轮不收集, 保留游标",
                     type(exc).__name__, self._msg_fails)
            return
        new_msg = bool(msgs)
        t_msg = time.perf_counter()

        # 是否“在地图里(对局/试车场)” —— 以 8111 的地图图片为准(见 garage.py):
        #   ① 固定的 “?” 占位图(128×128, 1519 字节 PNG) / 不是图片 → 在车库
        #   ② 真地图(≥1024²) → 在地图里
        #   ③ **没有回答**(超时/连接错) → 状态未知 → 沿用上一次判定, 不当成“回车库”
        #      (实测: 进对局初期 /map.img 要好几秒才回应, 当成“不在对局”就会在已经开打
        #       的头几十秒里一直不判进局 —— 用户反馈“打死 3 辆靶车才检测到”)
        # 为什么不看 map_info/mission: 那两个是**状态**字段, 退出对局回机库后会残留
        # 一会儿(实测 mission.objectives 会短暂保留上一局的内容), 叠加层就会在机库里
        # 多显示一段时间的“正在确认名单中…”。
        # 探地图图片: 出局时每 tick 都探; 已经在局内时每 MAP_PROBE_EVERY 个 tick 探一次。
        # 完整取图而不是“读满 16KB 就断”: 中途断开会把连接弄脏, 后续请求会卡到超时。
        # 没探的那一轮(in_map=None)什么样都不改 —— 特别是不能把“回车库”计数清零。
        self._tick_no += 1
        in_map: bool | None
        probe_ms = -1.0
        if (not self._in_battle) or (self._tick_no % MAP_PROBE_EVERY == 0):
            _t0 = time.perf_counter()
            try:
                probe = self.client.map_image(timeout=MAP_PROBE_TIMEOUT,
                                              raise_on_error=True)
                in_map = in_map_by_image(probe)
            except requests.RequestException as exc:
                in_map = None                  # 未知 → 不改状态
                log.info("map.img 没有回答(%s) → 沿用上一次判定", type(exc).__name__)
            probe_ms = (time.perf_counter() - _t0) * 1000
        else:
            in_map = None                      # 本轮不探, 不改状态
        now = time.time()
        # 状态驱动进出场: 直接以 in_map 为准, 不再依赖 sig 变化,
        # 避免对局/试车场加载期 sig 抖动导致叠加层闪退。
        if in_map is None:
            pass
        elif in_map:
            self._garage_ticks = 0
            if not self._in_battle:
                self._start_battle()
        elif self._in_battle:
            if now - self._last_activity < ACTIVITY_GRACE:
                # 刚刚还有击杀/发言 → 肯定还在对局里(地图图片一时拿不到, 或试车场
                # 给的不是真地图): 先不判离开, 免得把本局收集清掉、叠加层闪一下。
                self._garage_ticks = 0
            else:
                # 没有真地图: 连续 GARAGE_CONFIRM_TICKS 个 tick 才认定回到车库,
                # 图片一时拿不到(8111 偶发超时)不该立刻把叠加层撤掉。
                self._garage_ticks += 1
                if self._garage_ticks >= GARAGE_CONFIRM_TICKS:
                    self._end_battle("8111 没有地图图片 / 只给车库占位图")
        if probe_ms > 300:
            log.info("map.img 探测用了 %.0f ms → in_map=%s", probe_ms, in_map)
        new_dmg = (
            isinstance(hud, dict)
            and any(int(d.get("id", -1)) > self._dmg_cursor
                    for d in (hud.get("damage", []) or []))
        )
        absorbing = (not self._in_battle) or (not self._msg_primed)
        if absorbing and not self._msg_primed:
            # 本会话第一次轮询: 8111 窗口里可能有上一局的旧消息, 这里只建立基线
            log.info("建立消息基线: 吸收窗口里的旧消息(本次 %d 条击杀/伤害)",
                     len(hud.get("damage") or []) if isinstance(hud, dict) else 0)
        if absorbing and self._msg_primed and (new_msg or new_dmg):
            # 必须有的兜底: 新对局开始时**地图图片要等加载画面结束才出现**
            # (2026-10-10 实测: 地图图片比任务开始晚 ~25 秒), 而加载期已经在打靶车 ——
            # 不判进局的话, 进局后打掉的第一辆永远报不出来(用户实测: IT-1/T-62/T-55A
            # 都在地图出现前打掉, 只有地图出来之后的才报 = “进局后半分钟才检测到”)。
            # 只认“游标之后的新消息”, 所以启动基线里的旧消息依然不参与判断。
            log.info("battle confirmed by activity (kill/chat)")
            self._start_battle()
            absorbing = False
        before = len(self._collected)
        new_names: list[str] = []
        if not absorbing:
            if msgs:
                for m in msgs:
                    sender = (m.get("sender") or "").strip()
                    if sender and sender not in self._collected:
                        new_names.append(sender)
                        self._collected.add(sender)
            if isinstance(hud, dict):
                for dmg in hud.get("damage", []) or []:
                    for name in parse_hudmsg_names(dmg.get("msg", "")):
                        if name not in self._collected:
                            new_names.append(name)
                        self._collected.add(name)
        # 推进游标(吸收模式下也推进: 这样旧消息不会在下一局被当成新的)
        if msgs:
            max_id = max(int(m.get("id", -1)) for m in msgs)
            if max_id < self._gamechat_cursor:
                # 游戏/服务器重启, ID 归零 → 以新会话为准
                self._gamechat_cursor = max_id
            else:
                self._gamechat_cursor = max(self._gamechat_cursor, max_id)
        if isinstance(hud, dict):
            # 推进事件游标, 避免每次以 lastEvt=-1 全量拉取历史 events
            for ev in hud.get("events", []) or []:
                eid = int(ev.get("id", -1))
                if eid > self._evt_cursor:
                    self._evt_cursor = eid
            for dmg in hud.get("damage", []) or []:
                did = int(dmg.get("id", -1))
                if did > self._dmg_cursor:
                    self._dmg_cursor = did
        if not absorbing:
            if new_msg or new_dmg:
                self._last_activity = now
                # 排查用: 记下这次到底收到多少条/解析出谁(游标值一并记, 便于定位漏检)
                n_dmg = len(hud.get("damage") or []) if isinstance(hud, dict) else 0
                log.info("hudmsg: 游标 lastDmg=%s, 返回 %d 条, 解析出 %d 个新昵称 %s "
                         "[取消息 %.0fms 本局共 %d 个]",
                         self._dmg_cursor, n_dmg, len(new_names), new_names[:20] or "-",
                         (t_msg - t_tick) * 1000, len(self._collected))
        if not absorbing:
            if msgs or (isinstance(hud, dict) and hud):
                self.nicknames_updated.emit(sorted(self._collected))
                self._compare()
        self._msg_primed = True
        dt_ms = (time.perf_counter() - t_tick) * 1000
        if dt_ms > 1200:
            log.info("本轮 tick 耗时 %.0f ms(取消息 %.0f ms, 探图 %s ms)",
                     dt_ms, (t_msg - t_tick) * 1000,
                     "%0.f" % probe_ms if probe_ms >= 0 else "未探")

    # ------------------------------------------------------------------
    def _start_battle(self) -> None:
        log.info("new battle detected")
        self._in_battle = True
        self._garage_ticks = 0
        self._last_activity = 0.0
        self._cmp_sig = None
        self._collected.clear()
        self._alerted.clear()
        self._found_nicks = []
        self.new_battle.emit()
        self.nicknames_updated.emit([])
        self.blacklist_found.emit([])
        # 本局只认“进局之后新收到的消息”(id > 游标): 进局前/上一局留在 8111 窗口里的
        # 消息已经在游标之前了, 不会参与本局判断 —— 不做任何“回捞”猜测。
        self._start_prefetch()

    def _end_battle(self, reason: str) -> None:
        """离开对局(回到车库/断开连接): 清空当前对局的收集, 叠加层收起。

        回到车库时列表里的昵称全是上一局的残留(实测退出对局后这些数据不会自动清),
        所以一并清掉 —— 下一局开始时 `_start_battle` 再从头收集。
        """
        self._in_battle = False
        self._garage_ticks = 0
        self._last_activity = 0.0
        self._cmp_sig = None
        self._collected.clear()
        self._alerted.clear()
        self._found_nicks = []
        log.info("battle ended (%s)", reason)
        self.battle_ended.emit()
        self.nicknames_updated.emit([])
        self.blacklist_found.emit([])

    def _cursors_init(self) -> bool:
        """本会话是否曾进入过战斗(击杀/发言游标已初始化)。"""
        return self._gamechat_cursor >= 0 or self._evt_cursor >= 0 or self._dmg_cursor >= 0

    def _on_manual_check(self) -> None:
        log.info("manual check requested")
        self._start_prefetch(callback=self._compare)

    # ------------------------------------------------------------------
    # 黑名单昵称抓取策略(低频率, 避免频繁访问 live.warthunder.com 触发反爬)
    # ------------------------------------------------------------------
    ACTIVITY_GRACE = 5.0              # 刚有击杀/发言时, 先不判“已回车库”的宽限(秒)
    PROFILE_FETCH_TTL = 24 * 3600     # 正常昵称缓存有效期: 24 小时
    PROFILE_INVALID_TTL = 7 * 24 * 3600  # 无效ID(404)缓存有效期: 7 天, 避免反复重试坏ID
    PROFILE_FETCH_RETRY = 10 * 60     # 临时抓取失败后的重试冷却: 10 分钟
    PREFETCH_WORKERS = 3             # 预抓取并发数(缩短识别延迟, 总访问量不变)
    PREFETCH_INTER_TASK = (0.2, 0.6)  # 并发下单个任务请求前的错开间隔(秒)
    BATCH_DEADLINE = 30.0             # 整批抓取的最长耗时(秒), 超时中止下次再来
    CONSEC_FAIL_LIMIT = 3             # 连续临时失败达到该次数 → 判定 WT Live 不可达, 中止整批

    # ------------------------------------------------------------------
    # 本次会话 WT Live 访问计数(关闭软件即清空, 不持久化)
    # ------------------------------------------------------------------
    def _bump_wtlive_count(self) -> None:
        with self._count_lock:
            self._wtlive_count += 1
        self.wtlive_count.emit(self._wtlive_count)

    def _needs_fetch(self, player_id: str, now: float) -> bool:
        """判断某ID当前是否真正需要访问 WT Live 抓取昵称。"""
        cached = self._profile_cache.get(player_id)
        if cached is None:
            return True
        nick, fetched_at = cached
        rec = self.nickname_cache.get(player_id)
        invalid = bool(rec and rec.get("invalid"))
        if invalid:
            # 无效ID: 7 天内不反复重试
            return not (now - fetched_at < self.PROFILE_INVALID_TTL)
        if nick:
            return not (now - fetched_at < self.PROFILE_FETCH_TTL)
        # 临时失败(网络/5xx等): 冷却期内不重试
        return not (now - fetched_at < self.PROFILE_FETCH_RETRY)

    def _start_prefetch(self, callback=None) -> None:
        """后台抓取黑名单玩家昵称(仅抓缺失/过期的), 完成后可选执行 callback。"""
        if not self.auto_update:
            return  # 未勾选"自动更新": 不自动抓取, 需手动点刷新昵称
        if self._prefetch_running:
            if callback is not None:
                threading.Thread(target=callback, daemon=True).start()
            return
        self._prefetch_running = True

        def work() -> None:
            t_work = time.perf_counter()
            log.info("prefetch: 开始(名单 %d 条)", len(self.store.snapshot()))
            try:
                now = time.time()
                # 先从公开仓库共享表(nickname.json)拉一次: 命中的 ID 免访问
                # WTLive/官网, 显著减少对 war thunder 站点的访问频率。
                # 拉取失败/无服务器配置 → 空表, 静默降级为纯 WTLive 抓取。
                shared: dict[str, dict] = {}
                if self.shared_repo_url:
                    t_shared = time.perf_counter()
                    try:
                        shared = fetch_shared_table(self.shared_repo_url) or {}
                    except Exception:  # noqa: BLE001
                        shared = {}
                    log.info("prefetch: 共享表 %d 条(耗时 %.0f ms)", len(shared),
                             (time.perf_counter() - t_shared) * 1000)
                    if shared:
                        log.info("prefetch: 共享表命中 %d 条, 减少 WTLive 访问", len(shared))
                # 只处理真正需要抓取的 ID(缓存未命中/过期)
                pending: list[str] = []
                for e in self.store.snapshot():
                    pid = (e.player_id or "").strip()
                    if pid and self._needs_fetch(pid, now):
                        pending.append(pid)
                total = len(pending)
                result: dict[str, str] = {}
                updates: list[tuple[str, str, float, bool]] = []
                consec_fail = 0
                start = time.time()
                done = 0
                aborted = False
                rate_limited = False
                lock = threading.Lock()
                workq: "queue.Queue[str]" = queue.Queue()
                for pid in pending:
                    workq.put(pid)

                def fetch_one(pid: str) -> None:
                    nonlocal consec_fail, done, aborted, rate_limited
                    with lock:
                        if aborted:
                            return  # 已被中止(限流/连续失败/超时), 不处理新任务
                    # 优先查共享表: 命中则直接采用, 免访问 WTLive/官网(不计数不 sleep)
                    snick = str((shared.get(pid) or {}).get("nickname") or "").strip()
                    if snick:
                        nick = clean_wtlive_nickname(snick) or snick
                        # 写缓存保留共享表原始 ts: 避免 collect_pending 判为
                        # "本地更新者"而把共享表已有的昵称重复上传
                        rec = shared.get(pid) or {}
                        cache_ts = float(rec.get("ts") or 0) or time.time()
                        fetched_at = time.time()  # 内存判断用 now(24h 内不重抓)
                        if self._stop.is_set():
                            return  # 退出中, 不再 emit
                        with lock:
                            self._profile_cache[pid] = (nick, fetched_at)
                            updates.append((pid, nick, cache_ts, False))
                            result[pid] = nick
                        done += 1
                        self.prefetch_progress.emit(done, total)
                        return
                    time.sleep(random.uniform(*self.PREFETCH_INTER_TASK))
                    with lock:
                        if aborted:
                            return  # 等待期间已被中止(限流/连续失败/超时), 不发起请求
                    nick, status = fetch_profile_best_effort(pid)
                    fetched_at = time.time()
                    self._bump_wtlive_count()  # 实际访问了一次 WT Live 玩家页
                    if self._stop.is_set():
                        return  # 退出中, 不再 emit(避免对象销毁后崩溃)
                    with lock:
                        if nick:
                            self._profile_cache[pid] = (nick, fetched_at)
                            updates.append((pid, nick, fetched_at, False))
                            result[pid] = nick
                            consec_fail = 0
                        else:
                            # 区分永久失败(404 无效ID)与临时失败(网络错误/5xx/403)
                            invalid = status == STATUS_PERMANENT
                            self._profile_cache[pid] = (None, fetched_at)
                            updates.append((pid, "", fetched_at, invalid))
                            if status == STATUS_RATE_LIMIT:
                                rate_limited = True
                                log.warning("rate limited (429), aborting prefetch")
                                self.feed_status.emit(
                                    "warn", "WT Live 限流(429), 已停止本次抓取"
                                )
                                aborted = True
                            elif status == 0 or status >= 500 or status == 403:
                                consec_fail += 1
                                if consec_fail >= self.CONSEC_FAIL_LIMIT:
                                    log.warning("too many consecutive failures, aborting")
                                    self.feed_status.emit(
                                        "bad", "WT Live 不可达, 建议开启加速器"
                                    )
                                    aborted = True
                            elif invalid and pid not in self._manual_asked:
                                # WTLive 与官网都 404 → 提示用户用浏览器手动兜底
                                self._manual_asked.add(pid)
                                nick_manual = next(
                                    (e.nickname for e in self.store.snapshot()
                                     if (e.player_id or "").strip() == pid), ""
                                )
                                self.nickname_manual_needed.emit(pid, nick_manual)
                        done += 1
                        self.prefetch_progress.emit(done, total)

                def worker_loop() -> None:
                    """动态派发: 每完成一个任务, 先检查中止标志再取下一个。"""
                    nonlocal aborted
                    while True:
                        if self._stop.is_set():
                            return
                        with lock:
                            if aborted:
                                return  # 限流/连续失败/超时 → 不再取新任务
                            if time.time() - start > self.BATCH_DEADLINE:
                                aborted = True
                                return
                        try:
                            pid = workq.get_nowait()
                        except queue.Empty:
                            return
                        try:
                            fetch_one(pid)
                        except Exception:  # noqa: BLE001
                            pass

                n_workers = min(self.PREFETCH_WORKERS, len(pending)) if pending else 0
                if n_workers:
                    with cf.ThreadPoolExecutor(max_workers=n_workers) as ex:
                        futs = [ex.submit(worker_loop) for _ in range(n_workers)]
                        for f in futs:
                            try:
                                f.result()
                            except Exception:  # noqa: BLE001
                                pass

                # 批量统一写入缓存数据库 + 保存 + 通知刷新窗口
                if updates:
                    for pid, nick, ts, invalid in updates:
                        self.nickname_cache.set(pid, nick, ts,
                                                invalid=invalid, save=False)
                    self.nickname_cache.save()
                    self.cache_updated.emit()

                if result:
                    self.profiles_updated.emit(result)
                log.info("prefetch: 结束(待抓 %d 个 ID, 更新 %d 条, 耗时 %.0f ms)",
                         total, len(updates), (time.perf_counter() - t_work) * 1000)
                if callback is not None:
                    callback()
            finally:
                self._prefetch_running = False

        t = threading.Thread(target=work, daemon=True)
        self._track_bg(t)
        t.start()

    def _track_bg(self, t: threading.Thread) -> None:
        with self._bg_lock:
            self._bg_threads.append(t)

    def join_background(self, timeout: float = 2.0) -> None:
        """退出前等待后台 daemon 线程结束, 避免其仍 emit 已销毁对象而崩溃。"""
        with self._bg_lock:
            threads = list(self._bg_threads)
        for t in threads:
            t.join(timeout)
        with self._bg_lock:
            self._bg_threads = [t for t in threads if t.is_alive()]

    # ------------------------------------------------------------------
    # WT Live 连通性检测(仅由用户点击"检测"触发一次)
    # ------------------------------------------------------------------
    def _on_feed_check(self) -> None:
        """用户点击"检测"后, 一次性测试 WT Live 连通性。"""
        log.info("feed check requested")
        self.feed_status.emit("warn", "检测中…")

        def work() -> None:
            ok, sec = check_feed_latency()
            if self._stop.is_set():
                return  # 退出中, 不再 emit
            self._bump_wtlive_count()  # 访问了一次 WT Live 首页
            if not ok:
                level, text = "bad", "不可达"
            elif sec < 3.0:
                level, text = "good", f"{sec * 1000:.0f}ms"
            elif sec < 8.0:
                level, text = "warn", f"{sec * 1000:.0f}ms"
            else:
                level, text = "warn", f"{sec * 1000:.0f}ms"
            log.info("feed latency: %s %s", level, text)
            self.feed_status.emit(level, text)

        t = threading.Thread(target=work, daemon=True)
        self._track_bg(t)
        t.start()

    def _compare(self) -> None:
        """把当前对局已收集昵称与黑名单昵称做比对,命中则告警。

        性能: 先算一个轻量签名(本局收集数量 + 名单指纹), 没变化就直接返回;
        对局昵称的**变体与候选项全部预先算好**并小写 —— 否则每个条目都要重算一遍,
        实测 300 条名单 × 201 个昵称时从 ~100ms 降到 ~2ms(之前这段会卡住界面)。
        """
        if not self._collected:
            return
        t_cmp = time.perf_counter()
        entries = self.store.snapshot()
        sig = (len(self._collected),
               len(entries),
               sum(len(e.player_id or "") + len(e.nickname or "") for e in entries))
        if sig == self._cmp_sig:
            return
        self._cmp_sig = sig
        index = BattleNickIndex(sorted(self._collected))
        n_cand = 0          # 名单里“有昵称候选”的条数(排查“为什么没命中”)
        n_hits = 0
        miss: list[str] = []    # 候选示例(未命中时打印几个)
        for e in entries:
            pid = (e.player_id or "").strip()
            key = pid or (e.nickname or "").strip()
            if not key or key in self._alerted:
                continue
            # 候选昵称(WTLive 抓取 + 手动填写), 均清洗掉平台后缀
            cands: list[str] = []
            cached = self._profile_cache.get(pid) if pid else None
            if cached and cached[0]:
                cands.append(clean_wtlive_nickname(cached[0]))
            manual = (e.nickname or "").strip()
            if manual:
                cands.append(clean_wtlive_nickname(manual))
            if cands:
                n_cand += 1
            hit = index.match(cands)
            if hit is None:
                alt = index.match_ignoring_case(cands)
                if alt:
                    # 只有大小写不同 → 游戏里是不同玩家, 不算命中; 记一笔便于核对名单
                    log.info("昵称仅大小写不同, 按不同玩家处理: 名单候选 %s vs 对局 %s",
                             cands, alt)
            if hit:
                n_hits += 1
                self._alerted.add(key)
                reason = e.reason or "其他原因"
                item = (hit, reason)
                if item not in self._found_nicks:
                    self._found_nicks.append(item)
                self.blacklist_found.emit(list(self._found_nicks))
                log.warning("blacklist player in battle: nick=%s id=%s reason=%s",
                            hit, pid or "-", reason)
                self.blacklist_alert.emit(hit, pid or "-", reason)
            elif cands and len(miss) < 5:
                miss.append(f"{pid or '-'}:{cands}")
        # 排查用: 每次比对都记一笔输入规模与耗时, 未命中时附带候选样例
        log.info("比对: 本局 %d 个昵称 vs 名单 %d 条(有候选 %d 条) → 命中 %d, 耗时 %.1f ms",
                 len(self._collected), len(entries), n_cand, n_hits,
                 (time.perf_counter() - t_cmp) * 1000)
        if n_hits == 0:
            log.info("比对未命中: 本局昵称=%s 名单候选样例=%s",
                     sorted(self._collected)[:20], miss or "-")
