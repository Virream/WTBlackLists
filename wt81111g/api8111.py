"""战争雷霆 8111 本地接口客户端。"""
from __future__ import annotations

import json
import threading
import time

import requests

from .config import WT_BASE_URL

# 请求超时: 8111 平时 rtt 才 ~10ms, 但在**换局/换地图那几十秒里游戏会很忙**
# (实测: 刚离开对局时 /map.img、/hudmsg 可能一两秒才回应), 1 秒会把正常答案
# 误判成“没回答”。
_TIMEOUT = 2.0

# 请求优先级(数字越小越优先):
#   0 = 关键 —— 录制轮询(单条/全局的主循环), **永远不避让**;
#   1 = 普通 —— 状态探测、hudmsg、聊天等;
#   2 = 可等待 —— 整张地图图片、校准采集这类又大又不急的请求。
PRIO_KEY, PRIO_NORMAL, PRIO_IDLE = 0, 1, 2


class _Hub:
    """8111 请求的统一闸门 —— **关键请求(录制轮询)优先**。

    8111 是单线程服务: 谁先到谁先被处理。以前三个模块(录制 / 地图图片 / 校准采集)
    各发各的, 一张几百 KB 的地图图片或一次校准采集插进录制那一帧, 就能把那次 tick
    从 5 ms 拖到几十毫秒 —— 表现出来就是“点数率莫名掉一截”。

    这里只做两件事:
      1. 记住“关键轮询”的节奏(`mark_key`);
      2. 低优先级请求**先等一等**, 尽量落在两次关键请求之间的空隙里。

    只在本进程内生效; 两个程序同时开时的互相抢占没法从这里协调
    (见 `docs/8111使用经验.md` §4.3)。
    """

    def __init__(self) -> None:
        self.period = 0.0          # 关键轮询周期(秒); 0 = 录制器没在跑
        self.last_key = 0.0        # 上一次关键请求的时刻(monotonic)
        self.keys = 0
        self.stats: dict[str, list] = {}    # path -> [次数, 累计耗时 ms, 最近一次 ms]

    def mark_key(self, period: float | None = None) -> None:
        """录制主循环每帧调用一次。"""
        if period is not None:
            self.period = max(0.0, float(period))
        self.last_key = time.monotonic()
        self.keys += 1

    def yield_slot(self, priority: int) -> None:
        """低优先级请求避让: 等到关键帧之后的空隙。"""
        if priority <= PRIO_KEY or self.period <= 0.0:
            return
        slot = self.last_key + self.period * 0.35
        now = time.monotonic()
        if now < slot:
            # 最多等一个周期, 避免低优先级请求被饿死
            time.sleep(min(slot - now, self.period))

    def note(self, path: str, ms: float) -> None:
        row = self.stats.get(path)
        if row is None:
            self.stats[path] = [1, ms, ms]
        else:
            row[0] += 1
            row[1] += ms
            row[2] = ms

    def summary(self) -> str:
        """一行统计(调试/日志用)。"""
        items = sorted(self.stats.items(), key=lambda kv: -kv[1][1])
        return "; ".join(f"{p}×{v[0]} 均{v[1] / v[0]:.1f}ms" for p, v in items[:6])


_HUB = _Hub()


def hub() -> _Hub:
    """取进程级闸门(拿它调 `mark_key` 或读统计)。"""
    return _HUB


class WT8111:
    """封装对 8111 本地接口(127.0.0.1:8111)的 HTTP 请求(全部端点)。

    `priority` 是这个实例的默认优先级(见 `PRIO_*`), 单次调用也能覆盖。
    """

    def __init__(self, base_url: str = WT_BASE_URL, timeout: float = _TIMEOUT,
                 priority: int = PRIO_NORMAL):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.priority = int(priority)
        self.session = requests.Session()
        # 诊断用: 累计经 `get()` 发出的请求数。录制循环取帧内增量, 判断
        # “一帧到底发了几个请求”(见 route_recorder._diag_add)。
        self.req_count = 0

    def get(self, path: str, params: dict | None = None, timeout: float | None = None,
            priority: int | None = None):
        url = self.base + path
        pri = self.priority if priority is None else int(priority)
        _HUB.yield_slot(pri)                # 低优先级的先让开录制那一帧
        self.req_count += 1                 # 诊断计数(见 __init__)
        t0 = time.perf_counter()
        try:
            resp = self.session.get(url, params=params, timeout=timeout or self.timeout)
            resp.raise_for_status()
        except requests.RequestException:
            # 请求失败时丢掉连接池: 换局/换地图时游戏会把旧连接变成“死连接”,
            # 复用它会一直卡到超时(实测换地图那一下 mission/hudmsg 要 1.7~2 秒),
            # 关掉连接池后下一次请求会重新建连, 立刻恢复。
            self.session.close()
            raise
        finally:
            _HUB.note(path, (time.perf_counter() - t0) * 1000.0)
        text = resp.text.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def connected(self) -> bool:
        """轻量探测 8111 是否可达。"""
        try:
            resp = self.session.get(self.base + "/state", timeout=self.timeout)
            return resp.status_code == 200
        except requests.RequestException:
            self.session.close()
            return False

    def gamechat(self, last_id: int, raise_on_error: bool = False) -> list:
        try:
            data = self.get("/gamechat", {"lastId": last_id})
            return data if isinstance(data, list) else []
        except requests.RequestException:
            if raise_on_error:
                raise
            return []

    def hudmsg(self, last_evt: int, last_dmg: int, raise_on_error: bool = False) -> dict:
        try:
            data = self.get("/hudmsg", {"lastEvt": last_evt, "lastDmg": last_dmg})
            return data if isinstance(data, dict) else {}
        except requests.RequestException:
            if raise_on_error:
                raise
            return {}

    def mission(self, raise_on_error: bool = False) -> dict:
        try:
            data = self.get("/mission.json")
            return data if isinstance(data, dict) else {}
        except requests.RequestException:
            if raise_on_error:
                raise
            return {}

    def indicators(self) -> dict:
        """仪表数据。陆战含 rpm/gear/army/stabilizer 等, 空战含姿态/发动机等。

        `type` 字段为载具内部名(如 germ_leopard_2pl), `speed` 为 km/h。
        """
        try:
            data = self.get("/indicators")
            return data if isinstance(data, dict) else {}
        except requests.RequestException:
            return {}

    def map_info(self) -> dict:
        try:
            data = self.get("/map_info.json")
            return data if isinstance(data, dict) else {}
        except requests.RequestException:
            return {}

    def map_objects(self) -> list:
        """地图上的对象列表(每个含 type/icon/x/y/dx/dy 等)。

        坐标 x/y 为 0~1 归一化值, 直接乘以地图图片尺寸即为像素位置。
        玩家自己通常为 type == "player"; 部分场景(如自定义 bot 对局)可能无玩家对象。
        """
        try:
            data = self.get("/map_obj.json")
            return data if isinstance(data, list) else []
        except requests.RequestException:
            return []

    def map_image(self, limit: int | None = None, timeout: float | None = None,
                  raise_on_error: bool = False) -> bytes | None:
        """地图卫星图原始字节(PNG/JPEG, 由 GUI 侧解码为 QPixmap)。

        仅在地图内(对局/试车场)可用; 机库等场景返回固定的 “?” 占位图,
        这里用图片魔数校验, 非图片(如错误页)一律返回 None。

        `limit` 只读取前若干字节, 给“在不在车库”的判定用(见 `garage.py`):
        车库占位图只有 1.5KB, 能完整拿到; 真地图几百 KB~几 MB, 取满即断开连接,
        省掉整张地图的下载。

        `raise_on_error=True` 时请求失败(超时/连接错)不吞掉而是抛 `RequestException`
        —— 调用方需要区分“**没有回答**”和“回答了一个非图片”: 实测进对局初期
        `/map.img` 可能要好几秒才回应, 把它当成“不在地图里”会误判回车库。
        """
        try:
            _HUB.yield_slot(self.priority)      # 整张图很重, 让开录制那一帧
            t0 = time.perf_counter()
            resp = self.session.get(self.base + "/map.img", timeout=timeout or self.timeout,
                                    stream=bool(limit))
            resp.raise_for_status()
            if limit:
                buf = bytearray()
                for chunk in resp.iter_content(limit):
                    buf += chunk
                    if len(buf) >= limit:
                        break
                resp.close()
                data = bytes(buf)
            else:
                data = resp.content or b""
            _HUB.note("/map.img", (time.perf_counter() - t0) * 1000.0)
        except requests.RequestException:
            self.session.close()    # 见 get(): 丢掉可能已经死掉的连接
            if raise_on_error:
                raise
            return None
        if data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8\xff"):
            return data
        return None
