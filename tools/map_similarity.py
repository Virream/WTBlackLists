"""地图相似度自检工具: 用**你本机缓存的地图**算一遍“同图 / 不同图”的差异分布。

用法:
    .venv\\Scripts\\python.exe tools\\map_similarity.py

它做三件事(只读缓存目录, 不改任何数据):
1. 所有已缓存地图**两两**的 64×64 灰度平均差 + 结构哈希汉明距离(不同图 → 应该很大);
2. 每张地图**自己做一份“另一种分辨率/重新压缩”的副本**(缩到 1/2、JPEG 质量 85),
   再和原图比(同图 → 应该很小) —— 用来估计“同图被误判成不同图”的风险;
3. 按当前判据(`SAME_IMAGE_TOL` + 最近邻余量)给出“会不会被认成同一张”的结论。

结果即“算法在你的数据上有多准”的直接证据。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt  # noqa: E402
from PyQt6.QtGui import QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from wt81111g.config import data_dir  # noqa: E402
from wt81111g.map_cache import (  # noqa: E402
    SAME_IMAGE_HAMMING, SAME_IMAGE_TOL, MapCache, gray_distance, hamming, image_info,
    phash,
)

app = QApplication([])
# 默认看打包版的数据目录(那是实际在用的缓存); 传参可指定其它目录
maps_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    "dist", "WTRouteTester", "data", "maps")
if not os.path.isdir(maps_dir):
    maps_dir = os.path.join(data_dir(), "maps")
cache = MapCache(maps_dir)


def recompress(blob: bytes, scale: float = 0.5, quality: int = 85) -> bytes | None:
    """把图片缩放 + 重存为 JPEG —— 模拟“8111 换了个分辨率/重新编码”。"""
    img = QImage()
    if not img.loadFromData(blob):
        return None
    w = max(8, int(img.width() * scale))
    small = img.scaled(w, w, Qt.AspectRatioMode.IgnoreAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    ok = small.save(buf, "JPG", quality)
    buf.close()
    return bytes(ba) if ok else None

rows: list[tuple[str, bytes]] = []
for key in cache.keys():
    blob = cache.load(key)
    info = image_info(blob) if blob else None
    if blob and info:
        rows.append((key, blob))

print(f"缓存地图: {len(rows)} 张  |  当前阈值 SAME_IMAGE_TOL = {SAME_IMAGE_TOL}"
      f"  (64×64 灰度平均差)\n")

infos = {k: image_info(b) for k, b in rows}

print("① 不同地图两两对比(距离越小越容易被误判):")
pairs = []
for i, (ka, _) in enumerate(rows):
    for kb, _ in rows[i + 1:]:
        ia, ib = infos[ka], infos[kb]
        pairs.append((gray_distance(ia.gray, ib.gray), hamming(phash(ia.gray), phash(ib.gray)),
                      ka, kb))
pairs.sort()
for d, h, ka, kb in pairs[:5]:
    print(f"  距离 {d:6.2f}  汉明 {h:5d}  {ka} vs {kb}")
print(f"  ...  共 {len(pairs)} 对; 最小 {pairs[0][0]:.2f}, 中位 "
      f"{pairs[len(pairs) // 2][0]:.2f}, 最大 {pairs[-1][0]:.2f}")
if pairs:
    print(f"  → 离阈值最近的这张不同图对比, 距离是阈值的 {pairs[0][0] / SAME_IMAGE_TOL:.1f} 倍\n")

print("② 同一张地图“换分辨率 + 重新压缩”后对比(距离越小越安全):")
same = []
for key, blob in rows:
    for scale in (0.25, 0.5, 0.75, 1.0):
        for q in (60, 75, 85, 95):
            if scale == 1.0 and q == 95:
                continue                       # 原图自己不用比
            small = recompress(blob, scale, q)
            if not small:
                continue
            a, b = infos[key], image_info(small)
            if b is None:
                continue
            d = gray_distance(a.gray, b.gray)
            h = hamming(phash(a.gray), phash(b.gray))
            same.append((d, h, f"{key} {scale:.2f}x q{q}"))
same.sort()
for d, h, what in same[:3]:
    print(f"  距离 {d:6.2f}  汉明 {h:5d}  {what}   (最像的几份)")
for d, h, what in same[-3:]:
    print(f"  距离 {d:6.2f}  汉明 {h:5d}  {what}   (最不像的几份)")
print(f"  → 共 {len(same)} 份“同图换分辨率/重压”: 灰度 最大 {same[-1][0]:.2f}, "
      f"汉明 最大 {max(r[1] for r in same)}\n")

print("③ 结论:")
if same and pairs:
    worst_same, worst_same_ham = same[-1][0], max(r[1] for r in same)
    best_diff, best_diff_ham = pairs[0][0], min(r[1] for r in pairs)
    print(f"  * 同图(换分辨率+重压) 最坏: 灰度 {worst_same:.2f} / 汉明 {worst_same_ham}")
    print(f"  * 不同图最像的一对:    灰度 {best_diff:.2f} / 汉明 {best_diff_ham}")
    print(f"  * 灰度闸 {SAME_IMAGE_TOL}: 同图只要 < {SAME_IMAGE_TOL}(最坏的 "
          f"{SAME_IMAGE_TOL / max(worst_same, 1e-6):.1f} 倍), 不同图要 < {SAME_IMAGE_TOL} "
          f"才误判(最像的还差 {best_diff - SAME_IMAGE_TOL:.1f})")
    print(f"  * 汉明闸 {SAME_IMAGE_HAMMING}: 同图汉明 {worst_same_ham} "
          f"(余量 {SAME_IMAGE_HAMMING / max(worst_same_ham, 1):.1f} 倍), "
          f"不同图汉明 {best_diff_ham}(差 {best_diff_ham - SAME_IMAGE_HAMMING} 位)")
print("  * 还有一道最近邻余量: 次近的那张必须 ≥ 2×阈值(8.0), 否则一律不认")
print("  * 误判的后果很轻: 两张地图合成一条(轨迹仍按“战区指纹”分版本), 且合并前")
print("    routes.json 已备份成 routes.json.bak")


# ---------------------------------------------------------------- 极端情况压力测试
def gray_of_image(img: QImage) -> bytes:
    from wt81111g.map_cache import CONTENT_GRID
    tiny = img.scaled(CONTENT_GRID, CONTENT_GRID, Qt.AspectRatioMode.IgnoreAspectRatio,
                      Qt.TransformationMode.SmoothTransformation).convertToFormat(
                          QImage.Format.Format_RGB32)
    out = bytearray()
    for y in range(CONTENT_GRID):
        for x in range(CONTENT_GRID):
            p = tiny.pixel(x, y)
            out.append((((p >> 16) & 255) * 77 + ((p >> 8) & 255) * 151
                        + (p & 255) * 28) >> 8)
    return bytes(out)


biggest = max(rows, key=lambda kv: infos[kv[0]].pixels, default=None)
if biggest is not None:
    src = QImage()
    src.loadFromData(biggest[1])
    side = max(64, int(src.width() * 0.45))
    crops = []
    for i in range(12):
        ox = (i * 37) % max(1, src.width() - side)
        oy = (i * 53) % max(1, src.height() - side)
        crops.append(gray_of_image(src.copy(ox, oy, side, side)))
    print(f"\n④ 极端情况: 把「{biggest[0]}」({infos[biggest[0]].width}x"
          f"{infos[biggest[0]].width}) 的 12 个不同区域裁切两两对比:")
    ext = []
    for i in range(len(crops)):
        for j in range(i + 1, len(crops)):
            ext.append((gray_distance(crops[i], crops[j]),
                        hamming(phash(crops[i]), phash(crops[j])), i, j))
    ext.sort()
    for d, h, i, j in ext[:3]:
        print(f"  距离 {d:6.2f}  汉明 {h:5d}  裁切#{i} vs 裁切#{j}")
    print(f"  → 同一张图的不同区域(最小 {ext[0][0]:.2f} / 汉明 {ext[0][1]}):")
    print(f"    灰度闸 {SAME_IMAGE_TOL}: {ext[0][0]:.2f} → "
          f"{'拦住' if ext[0][0] > SAME_IMAGE_TOL else '会误放'}"
          f"(超出 {ext[0][0] / SAME_IMAGE_TOL:.1f} 倍);  汉明闸 {SAME_IMAGE_HAMMING}: "
          f"{ext[0][1]} → {'拦住' if ext[0][1] > SAME_IMAGE_HAMMING else '会误放'}"
          f"(超出 {ext[0][1] / SAME_IMAGE_HAMMING:.1f} 倍)")
    print("  (这是最容易接近的“不同图”形态: 同一张图的局部; 而**同图**最坏只有 "
          f"{same[-1][0]:.2f} / {max(r[1] for r in same)} —— 两道闸各留约 2 倍余量)")

