"""轨迹回放控制器(支持**多条轨迹同时回放**)。

- 选中多条轨迹时, 它们**从 00:00 一起播放**, 直到**最长的那条**播完才算结束;
- 支持「到下一标记暂停」: 把所有选中轨迹的标记放在同一条时间轴上,
  取当前进度**之后最近**的标记时刻暂停;
- 倍速范围 0.5X ~ 16X(0.5 步进)。

设计: 控制器只关心"时间轴", 具体绘制交给 RouteMapView(按 playhead 切分每条轨迹)。
"""
from __future__ import annotations

import logging
import time

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .route_store import RouteRecord

log = logging.getLogger("route_playback")

# 可选倍速: 0.5X 步进, 最高 16X
PLAYBACK_SPEEDS: tuple[float, ...] = tuple(round(0.5 * i, 1) for i in range(1, 33))

# 判定“进度是否已越过某标记”的时间容差(秒)。
# 不能取 0: 暂停在某个标记上后再点“到下一标记暂停”, 必须跳到**下一个**而不是原地停住。
_MARK_EPS = 1e-4


class RoutePlayer(QObject):
    """按倍速回放一条或多条轨迹记录(共用同一个 playhead)。"""

    progress = pyqtSignal(float)         # 当前回放时刻(秒)
    playing_changed = pyqtSignal(bool)   # 是否正在播放
    finished = pyqtSignal()              # 播放到末尾(最长的一条播完)
    reached_mark = pyqtSignal(object)    # 因"到下一标记暂停"而停在的 RouteMark

    def __init__(self, tick_ms: int = 33, parent=None):
        super().__init__(parent)
        self._recs: list[RouteRecord] = []
        self._t = 0.0
        self._speed = 1.0
        self._pause_at_mark = False
        self._last = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(max(10, int(tick_ms)))   # ~30fps
        self._timer.timeout.connect(self._on_tick)

    # ------------------------------------------------------------------ 状态
    @property
    def records(self) -> list[RouteRecord]:
        """当前一起回放的轨迹(可多条)。"""
        return list(self._recs)

    @property
    def record(self) -> RouteRecord | None:
        """第一条轨迹(单条回放时就是它, 便于既有调用点使用)。"""
        return self._recs[0] if self._recs else None

    @property
    def duration(self) -> float:
        """回放总时长 = **最长的一条**的时长(多条的结束以它为准)。"""
        return max((r.duration for r in self._recs), default=0.0)

    @property
    def time(self) -> float:
        return self._t

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def playing(self) -> bool:
        return self._timer.isActive()

    @property
    def pause_at_mark(self) -> bool:
        return self._pause_at_mark

    # ------------------------------------------------------------------ 控制
    def load(self, rec: RouteRecord | list[RouteRecord] | None) -> None:
        """载入要回放的记录(单条或一个列表)并复位到开头。"""
        self.pause()
        if rec is None:
            self._recs = []
        elif isinstance(rec, (list, tuple)):
            self._recs = [r for r in rec if r is not None]
        else:
            self._recs = [rec]
        self._t = 0.0
        self._pause_at_mark = False
        self.progress.emit(self._t)

    def set_speed(self, speed: float) -> None:
        self._speed = max(0.1, min(64.0, float(speed)))

    def seek(self, t: float) -> None:
        self._t = max(0.0, min(self.duration, float(t)))
        self.progress.emit(self._t)

    def start(self) -> None:
        if not self._recs or not any(len(r.points) >= 2 for r in self._recs):
            return
        if self._t >= self.duration:      # 已播放到末尾 → 从头开始
            self._t = 0.0
        self._last = time.monotonic()
        self._timer.start()
        self.playing_changed.emit(True)
        self.progress.emit(self._t)

    def pause(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
            self.playing_changed.emit(False)

    def toggle(self) -> None:
        """开始 / 暂停。"""
        if self.playing:
            self.pause()
        else:
            self.start()

    def stop(self) -> None:
        self.pause()
        self._pause_at_mark = False
        self._t = 0.0
        self.progress.emit(self._t)

    # ------------------------------------------------------------ 标记时刻
    def next_mark_time(self, after: float | None = None) -> float | None:
        """所有选中轨迹里, 当前进度**之后最近**的标记时刻(没有则 None)。

        多条轨迹的标记统一放到同一条时间轴上比较, 因此“下一个标记”
        可能来自任意一条轨迹 —— 这正是多条同时回放时要的效果。
        """
        cur = self._t if after is None else float(after)
        best: float | None = None
        for rec in self._recs:
            for mk in rec.marks:
                if mk.t > cur + _MARK_EPS and (best is None or mk.t < best):
                    best = mk.t
        return best

    def mark_at(self, t: float):
        """返回时刻 t(容差内)对应的标记, 用于提示文案。"""
        for rec in self._recs:
            for mk in rec.marks:
                if abs(mk.t - t) <= _MARK_EPS:
                    return mk
        return None

    def play_to_next_mark(self) -> None:
        """播放并在(所有轨迹中)下一个标记时刻自动暂停。"""
        if not self._recs:
            return
        if self._t >= self.duration:
            self._t = 0.0
        target = self.next_mark_time()
        if target is None:
            # 后面已经没有标记了 → 直接播到底(等同普通播放)
            self._pause_at_mark = False
            if not self.playing:
                self.start()
            return
        self._pause_at_mark = True
        if not self.playing:
            self.start()

    def cancel_pause_at_mark(self) -> None:
        self._pause_at_mark = False

    # ------------------------------------------------------------------ 推进
    def _on_tick(self, dt: float | None = None) -> None:
        if not self._recs:
            self.pause()
            return
        now = time.monotonic()
        if dt is None:
            dt = max(0.0, now - self._last)
        self._last = now

        duration = self.duration
        prev = self._t
        self._t = min(duration, self._t + dt * self._speed)

        if self._pause_at_mark:
            # 本帧跨越过的**最近**标记 → 精确停在它上面(不会因为一帧跨度大而冲过去)
            target = self.next_mark_time(after=prev)
            if target is not None and target <= self._t + _MARK_EPS:
                self._t = target
                self._pause_at_mark = False
                self.progress.emit(self._t)
                self.pause()
                mk = self.mark_at(target)
                if mk is not None:
                    self.reached_mark.emit(mk)
                return

        self.progress.emit(self._t)
        if self._t >= duration:
            self.pause()
            self.finished.emit()
