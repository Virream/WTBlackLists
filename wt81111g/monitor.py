"""对局监控: 轮询 8111,收集昵称,比对黑名单,发出告警。"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import queue
import random
import threading
import time

from PyQt6.QtCore import QObject, pyqtSignal

from .api8111 import WT8111
from .blacklist import BlacklistStore
from .garage import GARAGE_CONFIRM_TICKS, PROBE_BYTES, in_map_by_image
from .nickname_cache import NicknameCache
from .nickname_collector import parse_hudmsg_names
from .warthunder import (
    STATUS_PERMANENT,
    STATUS_RATE_LIMIT,
    check_feed_latency,
    fetch_profile_with_status,
)

log = logging.getLogger("monitor")

# 刚收到击杀/发言后, 这么多秒内不把“没有真地图”当成“已回车库”:
# 击杀消息说明人还在对局里, 不该清空本局收集(8111 偶发拿不到图片)。
ACTIVITY_GRACE = 5.0


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

    def __init__(self, store: BlacklistStore,
                 nickname_cache: NicknameCache | None = None,
                 base_url: str = "http://localhost:8111",
                 poll_interval: float = 1.0):
        super().__init__()
        self.store = store
        self.nickname_cache = nickname_cache or NicknameCache()
        self.poll_interval = poll_interval
        self.client = WT8111(base_url)
        self.manual_requested.connect(self._on_manual_check)
        self.feed_requested.connect(self._on_feed_check)

        self._stop = threading.Event()
        self._connected = False
        self._in_battle = False
        self._garage_ticks = 0   # 连续多少个 tick 都没看到真地图
        self._last_activity = 0.0  # 最近一次收到击杀/发言的时间
        self._msg_primed = False   # 是否已建立过消息基线(用来“吸收”上一局的旧消息)
        self._cmp_sig: tuple | None = None  # 上次比对的输入签名(避免重复算)
        self._battle_sig: str | None = None
        self._gamechat_cursor = -1
        self._evt_cursor = -1
        self._dmg_cursor = -1
        self._collected: set[str] = set()
        self._alerted: set[str] = set()
        self._found_nicks: list[tuple[str, str]] = []  # (昵称, 原因)
        self._profile_cache: dict[str, tuple[str | None, float]] = {}
        # 用独立缓存数据库初始化内存缓存(删除/重加条目后缓存依然有效)
        for _pid, _nick, _ts, _inv in self.nickname_cache.items():
            if _pid and _nick:
                self._profile_cache[_pid] = (_nick, _ts)
        self._prefetch_running = False
        self._wtlive_count = 0
        self._count_lock = threading.Lock()

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
        connected = self.client.connected()
        if connected != self._connected:
            self._connected = connected
            self.connection_changed.emit(connected)
        if not connected:
            if self._in_battle:
                self._end_battle("8111 断开连接")
            self._garage_ticks = 0
            return

        # 是否"在地图里(对局/试车场)" —— 完全以 8111 的地图图片为准(见 garage.py):
        #   ① 拿不到地图图片(请求失败/不是图片) → 在车库
        #   ② 图片是固定的 “?” 占位图(128×128, 1519 字节 PNG) → 在车库
        #   ③ 其它(真地图, ≥1024²) → 在地图里
        # 为什么不看 map_info/mission: 那两个是**状态**字段, 退出对局回机库后会残留
        # 一会儿(实测 mission.objectives 会短暂保留上一局的内容), 叠加层就会在机库里
        # 多显示一段时间的“正在确认名单中…”。
        in_map = in_map_by_image(self.client.map_image(limit=PROBE_BYTES))
        now = time.time()
        # 状态驱动进出场: 直接以 in_map 为准, 不再依赖 sig 变化,
        # 避免对局/试车场加载期 sig 抖动导致叠加层闪退。
        if in_map:
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

        # 击杀/发言收集。
        # ⚠️ 不在对局(或本会话第一次轮询)时只“**吸收**”消息: 推进游标、不收集、不匹配。
        # 原因: 8111 的 /hudmsg 是**滚动窗口**, 刚进新对局时里面还留着上一局的消息;
        # 消息 id 跨局只增不重置, 所以只能用“游标之后的新 id”判定“本局新增的”。
        # 这样: 软件启动前就在窗口里的 ID、以及出局期间收到的消息, 都不可能参与本局判断。
        msgs = self.client.gamechat(self._gamechat_cursor)
        hud = self.client.hudmsg(self._evt_cursor, self._dmg_cursor)
        new_msg = bool(msgs)
        new_dmg = (
            isinstance(hud, dict)
            and any(int(d.get("id", -1)) > self._dmg_cursor
                    for d in (hud.get("damage", []) or []))
        )
        # 本会话还没建立过基线, 或当前不在对局 → 只吸收(见上方注释)
        absorbing = (not self._in_battle) or (not self._msg_primed)
        if absorbing and not self._msg_primed:
            # 本会话第一次轮询: 8111 窗口里可能有上一局的旧消息, 这里只建立基线
            log.info("建立消息基线: 吸收窗口里的旧消息(本次 %d 条击杀/伤害)",
                     len(hud.get("damage") or []) if isinstance(hud, dict) else 0)
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
                self._gamechat_cursor = -1  # 游戏/服务器重启, ID 归零 → 视为新对局
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
                log.info("hudmsg: 游标 lastDmg=%s, 返回 %d 条, 解析出 %d 个新昵称 %s",
                         self._dmg_cursor, n_dmg, len(new_names), new_names[:20] or "-")
            if msgs or (isinstance(hud, dict) and hud):
                self.nicknames_updated.emit(sorted(self._collected))
                self._compare()
        self._msg_primed = True

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
        if self._prefetch_running:
            if callback is not None:
                threading.Thread(target=callback, daemon=True).start()
            return
        self._prefetch_running = True

        def work() -> None:
            try:
                now = time.time()
                # 只处理真正需要抓取的 ID(缓存未命中/过期)
                pending: list[str] = []
                for e in list(self.store.entries):
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
                    time.sleep(random.uniform(*self.PREFETCH_INTER_TASK))
                    with lock:
                        if aborted:
                            return  # 等待期间已被中止(限流/连续失败/超时), 不发起请求
                    nick, status = fetch_profile_with_status(pid)
                    fetched_at = time.time()
                    self._bump_wtlive_count()  # 实际访问了一次 WT Live 玩家页
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

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------
    # WT Live 连通性检测(仅由用户点击"检测"触发一次)
    # ------------------------------------------------------------------
    def _on_feed_check(self) -> None:
        """用户点击"检测"后, 一次性测试 WT Live 连通性。"""
        log.info("feed check requested")
        self.feed_status.emit("warn", "检测中…")

        def work() -> None:
            ok, sec = check_feed_latency()
            self._bump_wtlive_count()  # 访问了一次 WT Live 首页
            if not ok:
                level, text = "bad", f"不可达({sec:.1f}s), 建议开启加速器"
            elif sec < 3.0:
                level, text = "good", f"流畅({sec:.2f}s)"
            elif sec < 8.0:
                level, text = "warn", f"较慢({sec:.2f}s), 建议开启加速器"
            else:
                level, text = "warn", f"缓慢({sec:.1f}s), 建议开启加速器"
            log.info("feed latency: %s %s", level, text)
            self.feed_status.emit(level, text)

        threading.Thread(target=work, daemon=True).start()

    def _compare(self) -> None:
        """把当前对局已收集昵称与黑名单昵称做比对,命中则告警。

        性能: 先算一个轻量签名(本局收集数量 + 名单指纹), 没变化就直接返回。
        """
        if not self._collected:
            return
        entries = list(self.store.entries)
        sig = (len(self._collected), len(entries),
               sum(len(e.player_id or "") + len(e.nickname or "") for e in entries))
        if sig == self._cmp_sig:
            return
        self._cmp_sig = sig
        # 区分大小写: 游戏里同一串字母大小写不同就是不同的玩家 ID(不能按小写比较)
        exact = {n: n for n in self._collected}
        lowered = {n.lower(): n for n in self._collected}   # 仅用于“只差大小写”的提示
        for e in entries:
            pid = (e.player_id or "").strip()
            key = pid or (e.nickname or "").strip()
            if not key or key in self._alerted:
                continue
            candidates: list[str] = []
            cached = self._profile_cache.get(pid) if pid else None
            if cached and cached[0]:
                candidates.append(cached[0])
            manual = (e.nickname or "").strip()
            if manual:
                candidates.append(manual)
            for cand in candidates:
                hit = exact.get(cand)
                if hit is None:
                    alt = lowered.get(cand.lower())
                    if alt:
                        # 只有大小写不同 → 不同玩家, 不算命中; 记一笔便于核对名单
                        log.info("昵称仅大小写不同, 按不同玩家处理: 名单候选 %s vs 对局 %s",
                                 cand, alt)
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
                    break
