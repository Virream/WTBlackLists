"""独立昵称缓存数据库: ID → 昵称 + 抓取时间, 与黑名单条目解耦。

删除/重新添加黑名单条目不会影响缓存; 缓存窗口直接读取本数据库。
每条记录: {"nickname": str, "fetched_at": float, "invalid": bool}。
invalid=True 表示该 ID 页面不存在(无效/注销 ID), 应长缓存不反复重试。
"""
from __future__ import annotations

import json
import os
import threading
import time

from .config import nickname_cache_file as _default_cache_file


class NicknameCache:
    """线程安全的昵称缓存持久化(JSON): {player_id: {"nickname": str, "fetched_at": float}}。"""

    def __init__(self, path: str | None = None):
        self.path = path or _default_cache_file()
        self._lock = threading.RLock()
        self._data: dict[str, dict] = {}
        self.load_error: str = ""
        self._load()

    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items() if isinstance(data, dict) else []:
                if not isinstance(v, dict):
                    continue
                try:
                    ts = float(v.get("fetched_at") or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                self._data[str(k)] = {
                    "nickname": str(v.get("nickname") or ""),
                    "fetched_at": ts,
                    "invalid": bool(v.get("invalid", False)),
                }
        except Exception as exc:  # noqa: BLE001
            self.load_error = str(exc)
            try:
                if os.path.exists(self.path):
                    backup = f"{self.path}.corrupt-{time.strftime('%Y%m%d_%H%M%S')}"
                    os.replace(self.path, backup)
            except OSError:
                pass
            self._data = {}

    def save(self) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)  # 原子替换

    # ------------------------------------------------------------------
    def get(self, player_id: str) -> dict | None:
        """返回该ID的记录 dict {"nickname", "fetched_at", "invalid"}; 无记录时返回 None。"""
        with self._lock:
            d = self._data.get(str(player_id))
            if not d:
                return None
            return dict(d)

    def set(self, player_id: str, nickname: str, fetched_at: float | None = None,
            invalid: bool = False, save: bool = True) -> None:
        """写入/覆盖一条缓存。nickname 为空表示抓取失败; invalid=True 表示无效ID。

        批量抓取时可传 save=False 并在结束时统一 save(), 减少磁盘写入。
        """
        with self._lock:
            self._data[str(player_id)] = {
                "nickname": (nickname or "").strip(),
                "fetched_at": fetched_at if fetched_at is not None else time.time(),
                "invalid": bool(invalid),
            }
            if save:
                self.save()

    def items(self) -> list[tuple[str, str, float, bool]]:
        """返回 [(player_id, nickname, fetched_at, invalid), ...]。"""
        with self._lock:
            return [
                (k, v["nickname"], v["fetched_at"], bool(v.get("invalid", False)))
                for k, v in self._data.items()
            ]

    def remove(self, player_id: str) -> None:
        with self._lock:
            self._data.pop(str(player_id), None)
            self.save()
