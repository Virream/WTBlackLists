"""曾用昵称 / 缓存统计 / 昵称变更提醒 专项测试(离屏模式)。"""
import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 禁用主窗口启动时的版本检测网络线程: 测试环境无网络, 避免后台线程残留/超时导致原生崩溃
import wt81111g.main_window as _mw
_mw._check_latest = lambda: None

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from wt81111g.blacklist import (
    MAX_PREVIOUS_NICKNAMES,
    BlacklistEntry,
    BlacklistStore,
)
from wt81111g.main_window import MainWindow
from wt81111g.monitor import MonitorWorker
from wt81111g.nickname_cache import NicknameCache
from wt81111g.settings import AppSettings

results = []


def test_blacklist_history() -> None:
    e = BlacklistEntry(player_id="1")
    e.push_previous_nickname("  Foo  ")   # 自动去空格
    e.push_previous_nickname("foo")       # 去重(已存在, 但大小写不同 → 视为不同昵称)
    assert e.previous_nicknames == ["Foo", "foo"], e.previous_nicknames
    e.push_previous_nickname("Foo")       # 完全重复 → 不新增
    assert e.previous_nicknames == ["Foo", "foo"], e.previous_nicknames
    # 超过上限删旧
    for i in range(12):
        e.push_previous_nickname(f"Nick{i}")
    assert len(e.previous_nicknames) == MAX_PREVIOUS_NICKNAMES == 10
    # 最早的两个 "Foo"/"foo" 已被删除
    assert "Foo" not in e.previous_nicknames and "foo" not in e.previous_nicknames
    # 最近的 10 个保留, 新昵称在后
    assert e.previous_nicknames[0] == "Nick2"
    assert e.previous_nicknames[-1] == "Nick11"
    results.append("blacklist previous_nicknames OK")


def test_monitor_fresh_only() -> None:
    tmpdir = tempfile.mkdtemp()
    store = BlacklistStore(os.path.join(tmpdir, "b.json"))
    cache = NicknameCache(os.path.join(tmpdir, "nc.json"))
    now = time.time()
    store.add(BlacklistEntry(player_id="111"))   # 无缓存 → 抓
    store.add(BlacklistEntry(player_id="222"))   # 无缓存 → 抓
    cache.set("333", "Nick333", now)            # 有缓存 → 不抓(缓存库而非黑名单条目)
    import wt81111g.monitor as mon
    mon.fetch_profile_best_effort = lambda pid, timeout=8, retry_website=2: (f"Nick{pid}", 200)
    worker = MonitorWorker(store, nickname_cache=cache)
    emitted = []
    worker.profiles_updated.connect(
        lambda r: emitted.append(r), Qt.ConnectionType.DirectConnection
    )
    worker._start_prefetch()
    deadline = time.time() + 8
    while worker._prefetch_running and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    assert worker._wtlive_count == 2, worker._wtlive_count
    last = emitted[-1] if emitted else {}
    assert set(last.keys()) == {"111", "222"}, last  # 缓存命中不计入 result
    assert "333" not in last
    # 抓取结果已写入独立缓存数据库
    assert cache.get("111")["nickname"] == "Nick111"
    assert cache.get("333")["nickname"] == "Nick333"
    assert cache.get("333")["fetched_at"] == now
    results.append("monitor result=fresh-only + cache-db write OK")
    worker.stop()


def test_cache_survives_entry_delete() -> None:
    """回归: 删除条目后重新添加相同ID, 缓存窗口仍显示已缓存的昵称。"""
    tmpdir = tempfile.mkdtemp()
    cache_path = os.path.join(tmpdir, "nc.json")
    cache = NicknameCache(cache_path)
    cache.set("999", "CachedNick", time.time())

    # 第一次: 有缓存 → 不访问 WT Live
    store1 = BlacklistStore(os.path.join(tmpdir, "b1.json"))
    store1.add(BlacklistEntry(player_id="999"))
    mon = __import__("wt81111g.monitor", fromlist=["MonitorWorker"])
    mon.fetch_profile_best_effort = lambda pid, timeout=8, retry_website=2: (f"Fresh{pid}", 200)
    w1 = MonitorWorker(store1, nickname_cache=cache)
    w1._start_prefetch()
    deadline = time.time() + 8
    while w1._prefetch_running and time.time() < deadline:
        time.sleep(0.05)
    assert w1._wtlive_count == 0, "缓存命中不应访问 WT Live"
    w1.stop()

    # 用户删除该条目, 再重新添加相同 ID(新存储)
    store2 = BlacklistStore(os.path.join(tmpdir, "b2.json"))
    store2.add(BlacklistEntry(player_id="999"))
    w2 = MonitorWorker(store2, nickname_cache=cache)
    w2._start_prefetch()
    deadline = time.time() + 8
    while w2._prefetch_running and time.time() < deadline:
        time.sleep(0.05)
    assert w2._wtlive_count == 0, "重新添加后仍应命中缓存"
    w2.stop()

    # 缓存窗口直接读缓存库, 依然能看到该ID的昵称
    from wt81111g.cache_dialog import CacheDialog
    from PyQt6.QtWidgets import QApplication as _QA
    app = _QA.instance() or _QA([])  # 复用已有实例, 避免重复创建崩溃
    dlg = CacheDialog(cache)
    dlg.refresh()
    rows = dlg.table.rowCount()
    assert rows >= 1, rows
    found = any(
        dlg.table.item(r, 0).text() == "999" and dlg.table.item(r, 1).text() == "CachedNick"
        for r in range(rows)
    )
    assert found, "缓存窗口应显示已缓存昵称(即使条目已删除重加)"
    results.append("cache survives entry delete + redisplay OK")
    app.quit()


def test_wtlive_optimization() -> None:
    """404无效ID长缓存 + 连续失败中止整批。"""
    tmpdir = tempfile.mkdtemp()
    mon = __import__("wt81111g.monitor", fromlist=["MonitorWorker"])

    # 1) 404 → 无效ID, 缓存 7 天, 不反复抓取
    store = BlacklistStore(os.path.join(tmpdir, "b.json"))
    cache = NicknameCache(os.path.join(tmpdir, "nc.json"))
    store.add(BlacklistEntry(player_id="900"))
    calls = []
    mon.fetch_profile_best_effort = lambda pid, timeout=8, retry_website=2: (calls.append(pid) or (None, 404))
    w = MonitorWorker(store, nickname_cache=cache)
    w._start_prefetch()
    deadline = time.time() + 8
    while w._prefetch_running and time.time() < deadline:
        time.sleep(0.05)
    assert calls == ["900"], calls
    assert cache.get("900")["invalid"] is True
    # 再次 prefetch → 7 天内不重试
    w._start_prefetch()
    deadline = time.time() + 8
    while w._prefetch_running and time.time() < deadline:
        time.sleep(0.05)
    assert calls == ["900"], "无效ID不应在缓存期内重复访问"
    assert w._wtlive_count == 1, w._wtlive_count
    w.stop()
    results.append("wtlive 404 invalid-ID long cache OK")

    # 2) 连续临时失败 3 次 → 中止整批并提示不可达
    store2 = BlacklistStore(os.path.join(tmpdir, "b2.json"))
    cache2 = NicknameCache(os.path.join(tmpdir, "nc2.json"))
    for pid in ("11", "22", "33", "44"):
        store2.add(BlacklistEntry(player_id=pid))
    mon.fetch_profile_best_effort = lambda pid, timeout=8, retry_website=2: (None, 0)  # 网络错误
    w2 = MonitorWorker(store2, nickname_cache=cache2)
    statuses = []
    w2.feed_status.connect(
        lambda lvl, txt: statuses.append((lvl, txt)), Qt.ConnectionType.DirectConnection
    )
    progress = []
    w2.prefetch_progress.connect(
        lambda d, t: progress.append((d, t)), Qt.ConnectionType.DirectConnection
    )
    w2._start_prefetch()
    deadline = time.time() + 8
    while w2._prefetch_running and time.time() < deadline:
        time.sleep(0.05)
    assert w2._wtlive_count == 3, "第3次失败后应中止, 只访问3次"
    assert any(lvl == "bad" for lvl, _ in statuses), statuses
    assert progress and progress[-1][1] == 4, progress  # 总数4, 在抓到第3个时中止
    w2.stop()
    results.append("wtlive consecutive-fail abort OK")


def test_main_window_history_and_reminder() -> None:
    app = QApplication.instance() or QApplication([])  # 复用已有实例
    tmpdir = tempfile.mkdtemp()
    store_path = os.path.join(tmpdir, "blacklist.json")
    win = MainWindow(store_path, start_monitor=False)
    win.show()

    win._add_row()
    entry = win.store.entries[0]
    entry.nickname = "OldName"
    entry.player_id = "80931116"
    entry.fetched_nickname = "OldName"
    entry.fetched_at = time.time()
    win.store.save()
    win._reload_table()  # 重新加载以让 prev 单元格就位

    prev = win.table.cellWidget(0, 5)  # 曾用昵称列(勾选列之后)
    assert prev.isReadOnly()
    assert prev.text() == ""

    # 模拟通过ID抓取到新昵称: 自动替换玩家昵称, 旧值记入曾用
    win._on_profiles({"80931116": "NewName"})
    assert entry.previous_nicknames == ["OldName"], entry.previous_nicknames
    assert entry.fetched_nickname == "NewName"
    assert entry.nickname == "NewName", "抓取后应自动替换玩家昵称"
    assert prev.text() == "OldName", prev.text()

    # 玩家昵称已自动同步 → 提醒区不提示
    assert win.reminder_label.isHidden(), "抓取后玩家昵称已自动同步, 不应再提醒"

    # 用户更新昵称后提醒保持隐藏
    entry.nickname = "NewName"
    win._update_nickname_reminder()
    assert win.reminder_label.isHidden(), "昵称一致后不应再提醒"

    # 曾用昵称单元格在 _on_profiles 中自动刷新
    win._on_profiles({"80931116": "ThirdName"})
    assert entry.previous_nicknames == ["OldName", "NewName"], entry.previous_nicknames
    assert prev.text() == "OldName、NewName", prev.text()

    # 缓存窗口可打开并显示记录(独立缓存库需先有记录)
    win.nickname_cache.set("80931116", "NewName", time.time())
    win._open_cache()
    dlg = win._cache_dialog
    assert dlg.isVisible()
    dlg.refresh()
    rows = dlg.table.rowCount()
    assert rows >= 1, rows
    pid0 = dlg.table.item(0, 0).text()
    assert pid0 == "80931116", pid0
    assert "小时" in dlg.table.item(0, 2).text() or "分钟" in dlg.table.item(0, 2).text()

    # 回车库 → 清空“当前对局已收集昵称”(上一局残留)
    win.nick_list.addItem("StaleNick")
    win._on_battle_ended()
    assert win.nick_list.count() == 0, "回车库应清空昵称列表"
    results.append("main_window history + reminder + cache dialog OK")

    win.close()
    app.quit()


class _FakeClient:
    """模拟 8111 客户端: 按序列返回 mission 与地图图片, gamechat/hudmsg 恒空。

    `map_images` 的元素就是 `/map.img` 的返回: `None` = 拿不到图片(实测机库里有这种
    情形, 连浏览器都加载不出来), 图片字节 = 拿到了(车库 “?” 占位图 / 真地图)。
    """

    def __init__(self, missions, map_images, chats=None):
        self.missions = missions
        self.map_images = map_images
        self.chats = list(chats or [])   # 依次给出的发言昵称(验证收集/清空)
        self._chat_id = 0
        self.i = 0

    def connected(self):
        return True

    def mission(self, raise_on_error: bool = False):
        return self.missions[min(self.i, len(self.missions) - 1)]

    def map_image(self, limit: int | None = None):
        return self.map_images[min(self.i, len(self.map_images) - 1)]

    def gamechat(self, last_id):
        if not self.chats:
            return []
        self._chat_id += 1
        return [{"id": self._chat_id, "sender": self.chats.pop(0), "msg": "hi"}]

    def hudmsg(self, a, b):
        return {}


def test_battle_detection_by_image() -> None:
    """进出对局以 8111 的地图图片为准: 真地图 → 开始; 占位图/没图片 → 结束。

    mission 一路都是 “running + 有目标”(实测回机库后会残留一会儿) —— 必须不干扰判定。
    """
    from garage_test import png_bytes

    tmpdir = tempfile.mkdtemp()
    store = BlacklistStore(os.path.join(tmpdir, "b.json"))
    cache = NicknameCache(os.path.join(tmpdir, "nc.json"))
    running = [{"status": "running", "objectives": [{"id": 1}]}] * 6
    real = png_bytes(1024, 1024)
    holder = png_bytes(128, 128)          # “?” 占位图(真图: 1519 字节 PNG, 128×128)
    w = MonitorWorker(store, nickname_cache=cache)
    w.client = _FakeClient(running, [real, holder, holder, real, None, None])
    starts = []
    ends = []
    w.new_battle.connect(lambda: starts.append(1), Qt.ConnectionType.DirectConnection)
    w.battle_ended.connect(lambda: ends.append(1), Qt.ConnectionType.DirectConnection)

    w.client.i = 0
    w._tick()                       # 真地图 → 对局开始
    assert len(starts) == 1 and w._in_battle
    w.client.i = 1
    w._tick()                       # 第 1 tick 只见占位图 → 还差一次连续确认
    assert not ends and w._in_battle
    w._tick()                       # 连续第 2 tick → 认定回到车库(叠加层隐藏)
    assert len(ends) == 1 and not w._in_battle
    w.client.i = 3
    w._tick()                       # 又拿到真地图 → 重新开始
    assert len(starts) == 2 and w._in_battle
    w.client.i = 4
    w._tick()                       # 拿不到图片(机库里的另一种情形)
    w._tick()                       # 连续确认后 → 同样算回到车库
    assert len(ends) == 2 and not w._in_battle
    results.append("battle start/end by map image OK")


def test_leaving_battle_clears_collected() -> None:
    """回车库 → 清空“当前对局已收集昵称”(上一局残留); 进下一局再重新收集。"""
    from garage_test import png_bytes

    tmpdir = tempfile.mkdtemp()
    store = BlacklistStore(os.path.join(tmpdir, "b.json"))
    cache = NicknameCache(os.path.join(tmpdir, "nc.json"))
    real = png_bytes(1024, 1024)
    holder = png_bytes(128, 128)
    w = MonitorWorker(store, nickname_cache=cache)
    w.client = _FakeClient([{"status": "running", "objectives": [{"id": 1}]}] * 4,
                           [real, holder, holder, real],
                           chats=["EnemyOne", "EnemyTwo"])
    updates = []
    ends = []
    w.nicknames_updated.connect(lambda names: updates.append(list(names)),
                                Qt.ConnectionType.DirectConnection)
    w.battle_ended.connect(lambda: ends.append(1), Qt.ConnectionType.DirectConnection)

    w.client.i = 0
    w._tick()                       # 对局中: 从发言里收集
    w._tick()
    assert sorted(w._collected) == ["EnemyOne", "EnemyTwo"], w._collected
    assert updates[-1] == ["EnemyOne", "EnemyTwo"], updates
    w._found_nicks = [("EnemyOne", "开挂")]     # 模拟本局命中
    w.client.i = 1
    w._tick()                       # 刚有发言(ACTIVITY_GRACE 内) → 不判离开
    w._tick()
    assert not ends and w._collected, "刚有击杀/发言时不该判离开"
    w._last_activity = 0.0          # 模拟“确实安静下来了”
    w._tick()                       # 回车库第 1 tick: 还在连续确认
    assert not ends and w._collected, "确认前不应该清空"
    w._tick()                       # 连续确认 → 回到车库: 清空本局收集
    assert len(ends) == 1 and not w._in_battle
    assert w._collected == set(), w._collected
    assert w._found_nicks == []
    assert updates[-1] == [], updates
    w.client.i = 3
    w._tick()                       # 再进对局 → 从零重新收集
    assert w._in_battle and w._collected == set()
    results.append("leaving battle clears collected nicknames OK")


def test_garage_never_starts_battle() -> None:
    """机库两种情形(拿不到图片 / 问号占位图)都不该开始对局 → 叠加层不出现。"""
    from garage_test import png_bytes

    running = [{"status": "running", "objectives": [{"id": 1}]}]
    for label, image in (("拿不到图片", None), ("问号占位图", png_bytes(128, 128))):
        tmpdir = tempfile.mkdtemp()
        store = BlacklistStore(os.path.join(tmpdir, "b.json"))
        cache = NicknameCache(os.path.join(tmpdir, "nc.json"))
        w = MonitorWorker(store, nickname_cache=cache)
        w.client = _FakeClient(running, [image])
        starts = []
        w.new_battle.connect(lambda: starts.append(1), Qt.ConnectionType.DirectConnection)
        for _ in range(3):
            w._tick()
        assert not starts and not w._in_battle, label
        assert w._garage_ticks == 0, label
        w.stop()
    results.append("garage (no image / placeholder) never starts battle OK")


def test_shared_table_avoids_wtlive() -> None:
    """自动刷新优先查 GitHub 共享表: 命中则免访问 WTLive/官网。"""
    tmpdir = tempfile.mkdtemp()
    store = BlacklistStore(os.path.join(tmpdir, "b.json"))
    cache = NicknameCache(os.path.join(tmpdir, "nc.json"))
    store.add(BlacklistEntry(player_id="111"))
    store.add(BlacklistEntry(player_id="222"))
    import wt81111g.monitor as mon
    mon.fetch_shared_table = lambda repo_url: {
        "111": {"nickname": "SharedOne"},
        "222": {"nickname": "  SharedTwo@live  "},
    }
    mon.fetch_profile_best_effort = lambda pid, timeout=8, retry_website=2: (
        f"Fresh{pid}", 200)
    worker = MonitorWorker(store, nickname_cache=cache,
                           shared_repo_url="https://github.com/x/y")
    worker._start_prefetch()
    deadline = time.time() + 8
    while worker._prefetch_running and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    assert worker._wtlive_count == 0, "共享表命中不应访问 WTLive"
    assert cache.get("111")["nickname"] == "SharedOne"
    assert cache.get("222")["nickname"] == "SharedTwo", cache.get("222")
    results.append("shared table avoids wtlive OK")
    worker.stop()


def test_shared_table_fallback_on_fail() -> None:
    """共享表为空/拉取失败 → 静默降级走 WTLive, 不影响原有功能。"""
    tmpdir = tempfile.mkdtemp()
    store = BlacklistStore(os.path.join(tmpdir, "b.json"))
    cache = NicknameCache(os.path.join(tmpdir, "nc.json"))
    store.add(BlacklistEntry(player_id="111"))
    import wt81111g.monitor as mon
    mon.fetch_shared_table = lambda repo_url: {}
    calls = []
    mon.fetch_profile_best_effort = lambda pid, timeout=8, retry_website=2: (
        calls.append(pid) or (f"Fresh{pid}", 200))
    worker = MonitorWorker(store, nickname_cache=cache,
                           shared_repo_url="https://github.com/x/y")
    worker._start_prefetch()
    deadline = time.time() + 8
    while worker._prefetch_running and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    assert calls == ["111"], "共享表为空应回退 WTLive"
    assert cache.get("111")["nickname"] == "Fresh111"
    results.append("shared table fallback to wtlive OK")
    worker.stop()


def test_auto_upload_nicknames() -> None:
    """自动上传: 刷新后自动把本地最新昵称上传共享表(需已登录审核服务器)。"""
    app = QApplication.instance() or QApplication([])  # 复用已有实例
    tmpdir = tempfile.mkdtemp()
    win = MainWindow(os.path.join(tmpdir, "blacklist.json"), start_monitor=False)
    win.app_settings = AppSettings(os.path.join(tmpdir, "config.json"))
    win.app_settings.auto_upload = True
    win.app_settings.audit_servers = [{
        "url": "https://github.com/Virream/WTBlackListsData.git",
        "platform": "github", "name": "官方", "token": "tok",
        "logged_in": True, "username": "Alice",
    }]
    win.nickname_cache.set("111", "Nick111", time.time())

    # main_window 用 `from .nickname_sync import ...` 绑定到自身命名空间,
    # 必须 mock main_window 模块上的引用才有效
    import wt81111g.main_window as mw
    mw.fetch_shared_table = lambda url: {}
    submitted = []
    done_evt = threading.Event()
    mw.submit_issue = lambda url, token, entries: (
        submitted.append((url, token, list(entries))) or done_evt.set()
        or (1, "http://x"))

    # 已登录 + 开启 → 提交 issue
    win._auto_upload_nicknames()
    assert done_evt.wait(5), "已登录应提交 issue"
    assert submitted
    _url, token, entries = submitted[0]
    assert token == "tok"
    assert any(e["uid"] == "111" for e in entries)

    # 关闭自动上传 → 不再提交
    win.app_settings.auto_upload = False
    done_evt.clear()
    submitted.clear()
    win._auto_upload_nicknames()
    assert not done_evt.wait(0.5), "关闭后不应提交"
    assert not submitted

    # 未登录服务器 → 不提交
    win.app_settings.auto_upload = True
    win.app_settings.audit_servers = [{
        "url": "https://github.com/Virream/WTBlackListsData.git",
        "platform": "github", "name": "官方", "token": "", "logged_in": False,
    }]
    done_evt.clear()
    submitted.clear()
    win._auto_upload_nicknames()
    assert not done_evt.wait(0.5), "未登录不应提交"
    assert not submitted

    results.append("auto upload nicknames OK")
    win.close()


def main() -> int:
    test_blacklist_history()
    test_monitor_fresh_only()
    test_cache_survives_entry_delete()
    test_wtlive_optimization()
    test_battle_detection_by_image()
    test_leaving_battle_clears_collected()
    test_garage_never_starts_battle()
    test_shared_table_avoids_wtlive()
    test_shared_table_fallback_on_fail()
    test_auto_upload_nicknames()
    test_main_window_history_and_reminder()
    for r in results:
        print("OK:", r)
    print("NICKNAME HISTORY TEST PASSED" if len(results) == 12 else "FAILED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
