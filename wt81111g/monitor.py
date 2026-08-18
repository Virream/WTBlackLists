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
from .nickname_cache import NicknameCache
from .nickname_collector import parse_hudmsg_names
from .nickname_sync import fetch_shared_table
from .nickname_util import (
    battle_nickname_variants, clean_wtlive_nickname, matches,
)
from .warthunder import (
    STATUS_PERMANENT,
    STATUS_RATE_LIMIT,
    check_feed_latency,
    fetch_profile_best_effort,
)

log = logging.getLogger("monitor")


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
                 base_url: str = "http://localhost:8111",
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
        self._in_battle = False
        self._battle_sig: str | None = None
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
        # 用 mission 请求成败判定连通性, 省掉独立的 /state 探测请求(每 tick 少一次请求)
        try:
            mission = self.client.mission(raise_on_error=True)
            connected = True
        except requests.RequestException:
            mission, map_info = {}, {}
            connected = False
        if connected:
            map_info = self.client.map_info()
        if connected != self._connected:
            self._connected = connected
            self.connection_changed.emit(connected)
        if not connected:
            if self._in_battle:
                self._in_battle = False
                self.battle_ended.emit()
            return

        # 是否"在地图中(对局/试车场)":
        # 实测 map_info.json 在地图中返回完整地图几何数据(grid_size/map_max/
        # grid_zero/hud_type/map_generation 等, valid=true);
        # 退出对局/试车场回机库后变为 {"valid": false}。这是最可靠的进出场信号。
        in_map = (
            isinstance(map_info, dict)
            and map_info.get("valid") is not False
            and any(map_info.get(k) for k in (
                "grid_size", "grid_steps", "grid_zero", "map_max", "map_min",
                "hud_type", "map_generation",
            ))
        )
        # 兜底: 对局加载早期等场景 mission status=running 且 objectives 非空
        mission_running = mission.get("status") == "running" and bool(mission.get("objectives"))
        is_running = in_map or mission_running
        # 状态驱动进出场: 直接以 is_running 为准, 不再依赖 sig 变化,
        # 避免对局/试车场加载期 sig 抖动导致叠加层闪退。
        if is_running:
            if not self._in_battle:
                self._start_battle()
        elif self._in_battle:
            self._in_battle = False
            log.info("battle ended")
            self.battle_ended.emit()

        # 击杀/发言收集。仅当“已在战斗中”或“本会话曾进入过战斗(光标已初始化)”
        # 时轮询, 以便在 map_info 进出场判定延迟时, 靠击杀/发言兜底确认进入战斗,
        # 保证叠加层最迟在第一次击杀/发言时可靠弹出并立即比对。
        if self._in_battle or self._cursors_init():
            msgs = self.client.gamechat(self._gamechat_cursor)
            if msgs:
                max_id = max(int(m.get("id", -1)) for m in msgs)
                if max_id < self._gamechat_cursor:
                    # 游戏/服务器重启, ID 归零 → 从新会话起点收集(不用 -1 哨兵,
                    # 避免重复收集与 _cursors_init 短暂失效)
                    self._gamechat_cursor = max_id
            hud = self.client.hudmsg(self._evt_cursor, self._dmg_cursor)
            new_msg = bool(msgs)
            new_dmg = (
                isinstance(hud, dict)
                and any(int(d.get("id", -1)) > self._dmg_cursor
                        for d in (hud.get("damage", []) or []))
            )
            if (new_msg or new_dmg) and not self._in_battle:
                # 兜底: 有击杀/发言但进出场判定未命中 → 确认在地图中
                log.info("battle confirmed by activity (kill/chat)")
                self._start_battle()
            if msgs:
                for m in msgs:
                    mid = int(m.get("id", -1))
                    sender = (m.get("sender") or "").strip()
                    if sender:
                        self._collected.add(sender)
                    if mid > self._gamechat_cursor:
                        self._gamechat_cursor = mid
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
                    for name in parse_hudmsg_names(dmg.get("msg", "")):
                        self._collected.add(name)
            if msgs or (isinstance(hud, dict) and hud):
                self.nicknames_updated.emit(sorted(self._collected))
                self._compare()

    # ------------------------------------------------------------------
    def _start_battle(self) -> None:
        log.info("new battle detected")
        self._in_battle = True
        self._collected.clear()
        self._alerted.clear()
        self._found_nicks = []
        self.new_battle.emit()
        self.nicknames_updated.emit([])
        self.blacklist_found.emit([])
        self._start_prefetch()

    def _cursors_init(self) -> bool:
        """本会话是否曾进入过战斗(击杀/发言游标已初始化)。"""
        return self._gamechat_cursor >= 0 or self._evt_cursor >= 0 or self._dmg_cursor >= 0

    def _on_manual_check(self) -> None:
        log.info("manual check requested")
        self._start_prefetch(callback=self._compare)

    # ------------------------------------------------------------------
    # 黑名单昵称抓取策略(低频率, 避免频繁访问 live.warthunder.com 触发反爬)
    # ------------------------------------------------------------------
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
            try:
                now = time.time()
                # 先从公开仓库共享表(nickname.json)拉一次: 命中的 ID 免访问
                # WTLive/官网, 显著减少对 war thunder 站点的访问频率。
                # 拉取失败/无服务器配置 → 空表, 静默降级为纯 WTLive 抓取。
                shared: dict[str, dict] = {}
                if self.shared_repo_url:
                    try:
                        shared = fetch_shared_table(self.shared_repo_url) or {}
                    except Exception:  # noqa: BLE001
                        shared = {}
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
                        fetched_at = time.time()
                        if self._stop.is_set():
                            return  # 退出中, 不再 emit
                        with lock:
                            self._profile_cache[pid] = (nick, fetched_at)
                            updates.append((pid, nick, fetched_at, False))
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
        """把当前对局已收集昵称与黑名单昵称做比对,命中则告警。"""
        if not self._collected:
            return
        for e in self.store.snapshot():
            pid = (e.player_id or "").strip()
            key = pid or (e.nickname or "").strip()
            if not key or key in self._alerted:
                continue
            # 候选昵称(WTLive 抓取 + 手动填写), 均清洗掉平台后缀
            candidates: list[str] = []
            cached = self._profile_cache.get(pid) if pid else None
            if cached and cached[0]:
                candidates.append(clean_wtlive_nickname(cached[0]))
            manual = (e.nickname or "").strip()
            if manual:
                candidates.append(clean_wtlive_nickname(manual))
            candidates = [c for c in candidates if c]
            if not candidates:
                continue
            # 逐个对局昵称做精确 + 模糊匹配
            hit = None
            for battle in self._collected:
                # 游戏内昵称变体(去 ⋇/* 标记, 取末尾token)
                for bv in battle_nickname_variants(battle):
                    if matches(candidates, bv):
                        hit = battle
                        break
                if hit:
                    break
            if hit:
                self._alerted.add(key)
                reason = e.reason or "其他原因"
                item = (hit, reason)
                if item not in self._found_nicks:
                    self._found_nicks.append(item)
                self.blacklist_found.emit(list(self._found_nicks))
                log.warning("blacklist player in battle: nick=%s id=%s reason=%s",
                            hit, pid or "-", reason)
                self.blacklist_alert.emit(hit, pid or "-", reason)
