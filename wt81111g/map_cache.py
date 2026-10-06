"""地图图片本地缓存。

为什么要缓存: 8111 的 `/map.img` **只有在游戏运行时**才拿得到, 关掉游戏后就无法查看
历史轨迹的底图。因此在战局里第一次见到某张地图时把它存到本地, 之后回放/显示轨迹
都能用缓存的图。

地图身份: 8111 **不提供地图名**, 因此用图片字节的 sha1 前 12 位作为地图键
(与 `routes.json` 里的 `map_key` 完全一致)。哈希相同 = 同一张图 → **不重复保存**。

缓存目录: `<可写根>/data/maps/<key>.<ext>`; 名称由 `RouteStore.map_names` 统一保存
(这样侧栏与「地图管理」用的是同一份名称)。
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass

from .config import data_dir
from .route_store import PLACEHOLDER_MAP_SHA1

log = logging.getLogger("map_cache")

# 图片格式判定(按文件头魔数), 决定缓存文件扩展名
_MAGIC = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"RIFF", "webp"),          # WEBP: RIFF....WEBP
    (b"GIF8", "gif"),
)


def detect_ext(data: bytes) -> str:
    """按文件头判断图片扩展名(未知则用 .img)。"""
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    return "img"


def map_key(data: bytes) -> str:
    """图片字节 -> 地图键(与 RouteStore 使用同一算法)。"""
    return hashlib.sha1(data).hexdigest()[:12]


@dataclass
class MapEntry:
    """一条缓存记录。"""
    key: str          # 地图键(sha1 前 12 位)
    path: str         # 缓存文件路径
    saved_at: float   # 首次缓存时间(文件 mtime)
    size: int         # 字节数

    @property
    def saved_at_text(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.saved_at))

    @property
    def size_text(self) -> str:
        kb = self.size / 1024.0
        return f"{kb:.0f} KB" if kb < 1024 else f"{kb / 1024.0:.1f} MB"


class MapCache:
    """地图图片缓存(线程安全: 文件名即哈希, 写入用临时文件 + 原子替换)。"""

    def __init__(self, dir_path: str | None = None):
        self.dir = dir_path or os.path.join(data_dir(), "maps")
        try:
            os.makedirs(self.dir, exist_ok=True)
        except OSError as exc:  # noqa: BLE001 - 缓存不可用不应影响主功能
            log.warning("地图缓存目录创建失败: %s", exc)

    # ------------------------------------------------------------------ 查询
    def path_of(self, key: str) -> str | None:
        """已缓存则返回文件路径(不存在返回 None)。"""
        if not key or key == "unknown":
            return None
        for ext in ("jpg", "png", "webp", "gif", "img"):
            p = os.path.join(self.dir, f"{key}.{ext}")
            if os.path.exists(p):
                return p
        return None

    def has(self, key: str) -> bool:
        return self.path_of(key) is not None

    def load(self, key: str) -> bytes | None:
        """读取缓存的图片字节(没有则 None)。"""
        path = self.path_of(key)
        if path is None:
            return None
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError as exc:  # noqa: BLE001
            log.warning("读取地图缓存失败 %s: %s", path, exc)
            return None

    def keys(self) -> list[str]:
        out: list[str] = []
        for name in sorted(os.listdir(self.dir)):
            stem, dot, _ext = name.rpartition(".")
            if dot and stem and stem not in out:
                out.append(stem)
        return out

    def entries(self) -> list[MapEntry]:
        """全部缓存条目, 按首次缓存时间倒序(最新在前)。"""
        rows: list[MapEntry] = []
        for key in self.keys():
            path = self.path_of(key)
            if path is None:
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            rows.append(MapEntry(key=key, path=path, saved_at=st.st_mtime, size=st.st_size))
        rows.sort(key=lambda e: e.saved_at, reverse=True)
        return rows

    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries())

    # ------------------------------------------------------------------ 写入
    def save(self, data: bytes | None) -> tuple[str, bool]:
        """保存地图图片。返回 (地图键, 是否新建)。

        - **同一张图只存一次**: 文件已存在(键相同)就直接返回, 不重复写盘;
        - 战局外的 "?" 占位图不缓存(它不是地图)。
        """
        if not data:
            return ("unknown", False)
        key = map_key(data)
        if hashlib.sha1(data).hexdigest() == PLACEHOLDER_MAP_SHA1:
            return (key, False)                     # 占位图不入缓存
        path = self.path_of(key)
        if path is not None:
            return (key, False)                      # 已有 → 不重复保存
        ext = detect_ext(data)
        out = os.path.join(self.dir, f"{key}.{ext}")
        tmp = out + ".tmp"
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, out)                     # 原子替换, 强杀不残留半截文件
        except OSError as exc:  # noqa: BLE001
            log.warning("写入地图缓存失败 %s: %s", out, exc)
            return (key, False)
        log.info("已缓存地图图片: %s (%d 字节)", os.path.basename(out), len(data))
        return (key, True)

    def remove(self, key: str) -> bool:
        """删除某张缓存图片(只删图片, 轨迹记录与名称不受影响)。"""
        path = self.path_of(key)
        if path is None:
            return False
        try:
            os.remove(path)
            log.info("已删除地图缓存: %s", os.path.basename(path))
            return True
        except OSError as exc:  # noqa: BLE001
            log.warning("删除地图缓存失败 %s: %s", path, exc)
            return False
