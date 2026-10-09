"""地图图片本地缓存。

为什么要缓存: 8111 的 `/map.img` **只有在游戏运行时**才拿得到, 关掉游戏后就无法查看
历史轨迹的底图。因此在战局里第一次见到某张地图时把它存到本地, 之后回放/显示轨迹
都能用缓存的图。

地图身份: 8111 **不提供地图名**。早期直接用图片字节的 sha1 前 12 位当地图键 ——
但实测同一张地图会以**不同分辨率**发过来(1024²/2048²/4096² 都见过), 字节不同
哈希就不同, 于是同一张图被当成了两张地图(2026-10-09 反馈的 bug)。
现在: 除了字节哈希(仍用作文件名/变化检测), 还算一份**与分辨率无关的内容指纹**
(缩到 64×64 灰度), 新图会先跟已缓存的地图比一比 —— 足够像就**归到已有地图键上**,
只更新缓存图(分辨率更高才换), 不再新建地图条目。

缓存目录: `<可写根>/data/maps/<key>.<ext>`; 名称由 `RouteStore.map_names` 统一保存
(这样侧栏与「地图管理」用的是同一份名称)。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage

from .config import data_dir
from .route_store import PLACEHOLDER_MAP_SHA1

log = logging.getLogger("map_cache")

# 内容指纹的网格边长(缩到这么大再比)
CONTENT_GRID = 64
# “同一张地图”的判定: 两张 64×64 灰度指纹的平均差(0~255)。
# 实测(2026-10-09, 10 张真实地图): 同一张图的 1024²/2048² 两份 = **0.41**,
# 而**最接近的两张不同地图** = **11.97** → 取 4 做阈值(两边都有 ~10 倍余量)。
SAME_IMAGE_TOL = 4.0

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
class MapImageInfo:
    """图片的基本信息 + 与分辨率无关的内容指纹。"""
    width: int
    height: int
    gray: bytes          # 64×64 灰度(CONTENT_GRID² 字节)

    @property
    def pixels(self) -> int:
        return self.width * self.height


def image_info(data: bytes) -> MapImageInfo | None:
    """解码图片 → 尺寸 + 64×64 灰度指纹; 解码失败返回 None。

    用 QImage 解码(Qt 允许在非 GUI 线程里用 QImage; QPixmap 才不行)。
    """
    img = QImage()
    if not img.loadFromData(data):
        return None
    tiny = img.scaled(CONTENT_GRID, CONTENT_GRID,
                      Qt.AspectRatioMode.IgnoreAspectRatio,
                      Qt.TransformationMode.SmoothTransformation)
    tiny = tiny.convertToFormat(QImage.Format.Format_RGB32)
    gray = bytearray()
    for y in range(CONTENT_GRID):
        for x in range(CONTENT_GRID):
            p = tiny.pixel(x, y)
            # 整数亮度(和 = 256), 与 Qt 的灰度公式一致但可重现
            gray.append((((p >> 16) & 255) * 77 + ((p >> 8) & 255) * 151
                         + (p & 255) * 28) >> 8)
    return MapImageInfo(img.width(), img.height(), bytes(gray))


def gray_distance(a: bytes, b: bytes) -> float:
    """两张 64×64 灰度指纹的平均差(0~255); 尺寸不对就当完全不同。"""
    if not a or not b or len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


# 认为“是地图图片”的扩展名(遍历缓存目录时用)
_EXTS = ("jpg", "png", "webp", "gif", "img")


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
        self._lock = threading.RLock()
        # 索引: {地图键: {"w":, "h":, "bytes":, "gray": base64, "seen_at":}}
        # 只为了“认出同一张地图的另一种分辨率” —— 图片本身仍以 <key>.<ext> 存盘。
        self._index: dict[str, dict] = {}
        try:
            os.makedirs(self.dir, exist_ok=True)
        except OSError as exc:  # noqa: BLE001 - 缓存不可用不应影响主功能
            log.warning("地图缓存目录创建失败: %s", exc)
        self._load_index()

    # ------------------------------------------------------------------ 索引
    @property
    def index_path(self) -> str:
        return os.path.join(self.dir, "index.json")

    def _load_index(self) -> None:
        try:
            with open(self.index_path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._index = {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            self._index = {}

    def _save_index(self) -> None:
        tmp = self.index_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._index, fh, ensure_ascii=False)
            os.replace(tmp, self.index_path)
        except OSError as exc:  # noqa: BLE001
            log.warning("写入地图缓存索引失败: %s", exc)

    def _index_entry(self, key: str) -> dict | None:
        entry = self._index.get(key)
        if entry and self.path_of(key) is not None:
            return entry
        return None

    def _remember(self, key: str, info: MapImageInfo | None, size: int,
                  seen_at: float | None = None) -> None:
        if info is None:
            return
        old = self._index.get(key) or {}
        self._index[key] = {
            "w": info.width, "h": info.height, "bytes": size,
            "gray": base64.b64encode(info.gray).decode("ascii"),
            "seen_at": float(seen_at if seen_at is not None else old.get("seen_at") or time.time()),
        }

    @staticmethod
    def _gray_of(entry: dict) -> bytes:
        try:
            return base64.b64decode(entry.get("gray") or "")
        except (ValueError, TypeError):
            return b""

    def refresh_index(self) -> int:
        """给还没索引的缓存图片补上指纹(老版本留下的缓存用)。返回补了几张。

        每张图要解码一次(2048² 的 JPEG ≈ 几十毫秒), 但只在“第一次”发生 ——
        指纹会写进 index.json, 之后启动只需读一个 JSON。
        """
        added = 0
        with self._lock:
            for key in self.keys():
                if self._index_entry(key) is not None:
                    continue
                path = self.path_of(key)
                if path is None:
                    continue
                try:
                    with open(path, "rb") as fh:
                        blob = fh.read()
                    mtime = os.stat(path).st_mtime
                except OSError:
                    continue
                info = image_info(blob)
                if info is None:
                    continue
                self._remember(key, info, len(blob), mtime)
                added += 1
            if added:
                self._save_index()
        return added

    def similar_key(self, info: MapImageInfo | None) -> str | None:
        """在已缓存的地图里找**同一张图**(不同分辨率): 返回它的地图键, 没有则 None。"""
        if info is None:
            return None
        best: tuple[float, str] | None = None
        with self._lock:
            for key, entry in self._index.items():
                if self.path_of(key) is None:
                    continue
                d = gray_distance(info.gray, self._gray_of(entry))
                if d <= SAME_IMAGE_TOL and (best is None or d < best[0]):
                    best = (d, key)
        return best[1] if best else None

    # ------------------------------------------------------------------ 查询
    def path_of(self, key: str) -> str | None:
        """已缓存则返回文件路径(不存在返回 None)。"""
        if not key or key == "unknown":
            return None
        for ext in _EXTS:
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
        """全部已缓存地图键(只看图片文件; 索引 index.json / 临时文件不算)。"""
        out: list[str] = []
        for name in sorted(os.listdir(self.dir)):
            stem, dot, ext = name.rpartition(".")
            if not dot or not stem or ext not in _EXTS:
                continue
            if stem not in out:
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
        - **同一张图的另一种分辨率**: 按键不同但内容一样 → 归到已有地图键上,
          不新建地图条目(分辨率更高时顺便把缓存图换成更清晰的那份);
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
        info = image_info(data)
        same = self.similar_key(info)
        if same is not None:
            # 同一张地图的另一种分辨率/编码 → 归到已有键, 不新建地图
            self._upgrade_image(same, data, info)
            log.info("8111 发来同一张地图的另一种分辨率(%sx%s) → 归到已有地图 %s",
                     info.width if info else "?", info.height if info else "?", same)
            return (same, False)
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
        with self._lock:
            self._remember(key, info, len(data))
            self._save_index()
        log.info("已缓存地图图片: %s (%d 字节, %sx%s)", os.path.basename(out), len(data),
                 info.width if info else "?", info.height if info else "?")
        return (key, True)

    def _upgrade_image(self, key: str, data: bytes, info: MapImageInfo | None) -> None:
        """同一张地图又见到一份图: 只保留**分辨率更高**的那份(键不变)。"""
        if info is None:
            return
        entry = self._index.get(key) or {}
        old_px = int(entry.get("w") or 0) * int(entry.get("h") or 0)
        if old_px and info.pixels <= old_px:
            return                                   # 旧的更清晰 → 什么都不做
        old_path = self.path_of(key)
        ext = detect_ext(data)
        out = os.path.join(self.dir, f"{key}.{ext}")
        tmp = out + ".tmp"
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, out)
            if old_path and os.path.abspath(old_path) != os.path.abspath(out):
                os.remove(old_path)                  # 换了扩展名 → 删掉旧的
        except OSError as exc:  # noqa: BLE001
            log.warning("更新地图缓存失败 %s: %s", out, exc)
            return
        with self._lock:
            # seen_at 保留“第一次见到”的时间, 免得侧栏排序乱跳
            self._remember(key, info, len(data), entry.get("seen_at"))
            self._save_index()
        log.info("地图缓存升级为更清晰的版本: %s (%sx%s)", os.path.basename(out),
                 info.width, info.height)

    # ------------------------------------------------------------------ 归并
    def duplicate_groups(self) -> list[list[str]]:
        """分组: 内容相同(只是分辨率/编码不同)的地图键归一到一个组里。"""
        self.refresh_index()
        with self._lock:
            keys = [k for k in self._index if self.path_of(k) is not None]
        groups: list[list[str]] = []
        for key in keys:
            entry = self._index.get(key) or {}
            gray = self._gray_of(entry)
            for group in groups:
                head = self._gray_of(self._index.get(group[0]) or {})
                if gray_distance(gray, head) <= SAME_IMAGE_TOL:
                    group.append(key)
                    break
            else:
                groups.append([key])
        return [g for g in groups if len(g) > 1]

    def merge_duplicates(self, store) -> list[tuple[str, str]]:
        """把“同一张地图的不同分辨率”合并成一张地图(启动时跑一次)。

        保留哪个键: 优先**有轨迹记录的**, 其次**用户起过名字的**, 再其次**分辨率高的**,
        最后取先缓存的。返回 [(被合并掉的键, 保留的键), ...]。
        """
        merged: list[tuple[str, str]] = []
        for group in self.duplicate_groups():
            keep = self.pick_representative(group, store)
            for other in group:
                if other == keep:
                    continue
                if store is not None and store.merge_map_keys(keep, other):
                    merged.append((other, keep))
                self._merge_files(keep, other)
        if merged:
            log.info("已合并 %d 个重复地图条目: %s", len(merged),
                     ", ".join(f"{o}→{k}" for o, k in merged))
        return merged

    def pick_representative(self, group: list[str], store) -> str:
        """一组重复里挑一个当代表(规则见 merge_duplicates)。"""
        def score(key: str) -> tuple:
            count = 0
            named = 0
            if store is not None:
                count = sum(1 for r in store.routes if r.map_key == key)
                named = 1 if store.name_of(key) else 0
            entry = self._index.get(key) or {}
            px = int(entry.get("w") or 0) * int(entry.get("h") or 0)
            path = self.path_of(key)
            try:
                mtime = os.stat(path).st_mtime if path else float("inf")
            except OSError:
                mtime = float("inf")
            return (count > 0, named, px, -mtime)

        return max(group, key=score)

    def _merge_files(self, keep: str, drop: str) -> None:
        """把 drop 的图片并到 keep 上(分辨率更高就用它的), 然后删掉 drop 的文件。"""
        keep_entry = self._index.get(keep) or {}
        drop_entry = self._index.get(drop) or {}
        keep_px = int(keep_entry.get("w") or 0) * int(keep_entry.get("h") or 0)
        drop_px = int(drop_entry.get("w") or 0) * int(drop_entry.get("h") or 0)
        drop_path = self.path_of(drop)
        if drop_path is not None and drop_px > keep_px:
            try:
                with open(drop_path, "rb") as fh:
                    blob = fh.read()
                info = image_info(blob)
                if info is not None:
                    self._upgrade_image(keep, blob, info)
            except OSError as exc:  # noqa: BLE001
                log.warning("合并地图图片失败 %s: %s", drop_path, exc)
        try:
            if drop_path is not None:
                os.remove(drop_path)
        except OSError as exc:  # noqa: BLE001
            log.warning("删除重复地图图片失败 %s: %s", drop_path, exc)
        with self._lock:
            self._index.pop(drop, None)
            self._save_index()

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
