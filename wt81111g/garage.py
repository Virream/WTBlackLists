"""车库判定: 用 8111 的地图图片判断“是否在车库”。

实测(2026-10-09, 机库内, **两种情形都是“不在对局”**):

① 刚开游戏 / 这一局还没进过对局:
   `GET /map.img` **拿不到地图图片** —— 请求失败或返回的不是图片,
   连浏览器里也加载不出来;
② 进过对局再退回机库:
   `GET /map.img` 变成固定的 “?” 占位图(1519 字节 PNG, 128×128,
   sha1 = ab516d02191d001d809253c346e8e3f93e13f136)。

进对局/试车场后, `/map.img` 换成真正的地图图片(≥1024², 几百 KB~几 MB,
见 `docs/8111使用经验.md` §3.2)。所以规则是:

    没有地图图片 或 图片是那张 “?” 占位图  →  在车库
    其它(真地图)                         →  在地图(对局/试车场)里

这比 `map_info`/`mission` 靠谱: 那两个是**状态**字段, 退出对局回机库后会残留
一会儿(实测 `mission.objectives` 会短暂保留上一局的内容), 叠加层就会在机库里
多显示一段时间的“正在确认名单中…”。

用法:
    data = client.map_image(limit=PROBE_BYTES)   # 只读前 16KB, 不下载整张地图
    in_map = in_map_by_image(data)               # True / False

本模块只用标准库(PNG/JPEG 头解析), 不依赖 PyQt, 也就不会被打包时裁掉的
图片插件影响。
"""
from __future__ import annotations

import hashlib

# 车库/战局外: 8111 返回的固定 “?” 占位图(实测: 1519 字节 PNG, 128×128)
GARAGE_MAP_SHA1 = "ab516d02191d001d809253c346e8e3f93e13f136"
GARAGE_MAP_BYTES = 1519
GARAGE_MAP_EDGE = 128

# 真地图 ≥1024²(实测 1024/2048/4096), 占位图 128²:
# 短边不超过该阈值的一律当作“不是地图”。
GARAGE_MAP_MAX_EDGE = 256

# 判定时只读图片前这么多字节: 占位图能完整拿到, 真地图取满即断开连接
# (不必下载整张地图 —— 几百 KB 到几 MB, 每秒一次太浪费)。
PROBE_BYTES = 16384

# 已经判定“在战斗中”时, 需要连续这么多个 tick 都看不到真地图才认定回到车库。
# 原因: 图片一时拿不到(8111 偶发超时/不是图片)不该立刻把叠加层撤掉,
# 而“刚回机库”的瞬间也可能还能拿到上一张地图。
GARAGE_CONFIRM_TICKS = 2

# JPEG 里带尺寸的帧起始标记(SOF0~SOF15, 去掉 DHT/JPG/DAC)
_SOF_MARKERS = frozenset(
    (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)
)


def _png_size(data: bytes) -> tuple[int, int] | None:
    """PNG: 紧跟在 8 字节签名 + IHDR 后面的两个大端 uint32。"""
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    w = int.from_bytes(data[16:20], "big")
    h = int.from_bytes(data[20:24], "big")
    return (w, h) if w and h else None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """JPEG: 顺序扫段, 遇到 SOFn 段即为 (高, 宽)。"""
    i = 2  # 跳过 SOI
    n = len(data)
    while i + 1 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:          # 填充字节
            i += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            i += 2                  # 无长度字段的独立标记
            continue
        if i + 4 > n:
            return None
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if seg_len < 2:
            return None
        if marker in _SOF_MARKERS:
            if i + 9 > n:
                return None
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h) if w and h else None
        i += 2 + seg_len
    return None


def image_size(data: bytes) -> tuple[int, int] | None:
    """从图片头解析 (宽, 高); 支持 PNG / JPEG。

    截断的图片也可以(只要头部完整) —— 探测时只读前 `PROBE_BYTES` 字节。
    解析不出来返回 None(调用方按“判断不了”处理)。
    """
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png_size(data)
    if data.startswith(b"\xff\xd8"):
        return _jpeg_size(data)
    return None


def in_map_by_image(data: bytes | None) -> bool:
    """用 8111 的地图图片判断是否在地图(对局/试车场)里。

    - 没有图片(请求失败 / 不是图片 / 空) → `False`(在车库)
    - 图片是 “?” 占位图(sha1 一致, 或尺寸 ≤256) → `False`(在车库)
    - 其它 → `True`(在地图里)
    """
    if not data:
        return False
    # 取满了探测长度 → 一定不是 1.5KB 的占位图(真地图几百 KB 起)
    if len(data) >= PROBE_BYTES:
        return True
    if len(data) == GARAGE_MAP_BYTES and hashlib.sha1(data).hexdigest() == GARAGE_MAP_SHA1:
        return False
    size = image_size(data)
    if size is None:
        return False         # 看不懂的小图 → 同样当作“没有地图图片”
    return max(size) > GARAGE_MAP_MAX_EDGE
