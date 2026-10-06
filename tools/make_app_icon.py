"""把 AI 生成的 PNG 图标转成 Windows 多尺寸 .ico(打包用)。

用法:
    .venv\\Scripts\\python.exe tools\\make_app_icon.py
        # 默认: icon\\icon_512X512_WTRouteTester.png -> icon\\WTRouteTester.ico

为什么自己写而不用 Pillow: 本仓库的开发环境**没有装 Pillow**, 而 PyInstaller 的
`--icon xx.png` 需要 Pillow 才能转换; 直接给一个合法的 .ico 就没这问题。
帧格式照抄本仓库既有的图标工具 `tools\\png_to_ico.py`(黑名单主程序用的):
16~128 用经典 32 位 BMP(带 AND 掩码), **256 用 PNG 帧** —— 兼容性最好,
避免资源管理器/快捷方式提取大图标时出问题。
"""
from __future__ import annotations

import os
import struct
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QRect, Qt   # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter                     # noqa: E402
from PyQt6.QtWidgets import QApplication                             # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256)
SRC = os.path.join(ROOT, "icon", "icon_512X512_WTRouteTester.png")
DST = os.path.join(ROOT, "icon", "WTRouteTester.ico")


def _png_bytes(img: QImage) -> bytes:
    """用 Qt 把图像编成 PNG(256 尺寸的帧用它, 体积小且是 Vista+ 标准做法)。"""
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def _frames(src: QImage) -> list[tuple[int, bytes, bool]]:
    """把源图缩成各尺寸帧: (尺寸, 字节, 是否 PNG 帧)。"""
    out: list[tuple[int, bytes, bool]] = []
    for size in SIZES:
        # 先等比缩放, 再居中贴到透明方画布上(非正方形图也不会变形)
        scaled = src.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
        canvas = QImage(size, size, QImage.Format.Format_ARGB32)
        canvas.fill(QColor(0, 0, 0, 0))
        painter = QPainter(canvas)
        try:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(QRect((size - scaled.width()) // 2,
                                    (size - scaled.height()) // 2,
                                    scaled.width(), scaled.height()), scaled)
        finally:
            painter.end()

        # BITMAPINFOHEADER: 高度写 2 倍(颜色数据 + AND 掩码)
        header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                             size * size * 4, 0, 0, 0, 0)
        rows = bytearray()
        for y in range(size - 1, -1, -1):        # BMP 自下而上
            for x in range(size):
                c = canvas.pixelColor(x, y)
                rows += bytes((c.blue(), c.green(), c.red(), c.alpha()))
        mask_row = ((size + 31) // 32) * 4       # AND 掩码按 4 字节对齐
        mask = bytes(mask_row * size)            # 全 0: 透明度交给 alpha 通道
        if size >= 256:
            out.append((size, _png_bytes(canvas), True))
        else:
            out.append((size, header + bytes(rows) + mask, False))
    return out


def main() -> int:
    app = QApplication([])          # noqa: F841 - QImage/QPainter 需要 QApplication
    src = QImage(SRC)
    if src.isNull():
        print(f"源图读不到: {SRC}")
        return 1
    frames = _frames(src)
    offset = 6 + 16 * len(frames)
    entries, blobs = bytearray(), bytearray()
    for size, data, _is_png in frames:
        entries += struct.pack("<BBBBHHII", size if size < 256 else 0,
                               size if size < 256 else 0, 0, 0, 1, 32,
                               len(data), offset)
        blobs += data
        offset += len(data)
    with open(DST, "wb") as fh:
        fh.write(struct.pack("<HHH", 0, 1, len(frames)) + bytes(entries) + bytes(blobs))
    print(f"已生成 {DST} ({os.path.getsize(DST) / 1024:.0f} KB, "
          f"{len(frames)} 个尺寸: {', '.join(str(s) for s in SIZES)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
