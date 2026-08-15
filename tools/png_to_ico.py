"""将 icon/2.0 目录下的 512x512 与 256x256 PNG 图标转换为多尺寸 app.ico。

图标源固定从项目 `icon/2.0/` 目录读取(按实际像素尺寸识别 512/256,
不受文件名影响); 512 为高分辨率(HiDPI)首选, 256 为兼容性尺寸。

生成标准 ICO: 16/24/32/48/64/128 用 BMP(DIB+AND mask)格式,
256 用 PNG 格式 —— 保证 Windows 能正确提取任意尺寸(此前仅两个 PNG
的 ICO 会导致资源管理器/快捷方式提取 32x32 图标异常, 显示旧图标)。
"""
import os
import struct
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PyQt6.QtGui import QGuiApplication, QImage, QPixmap

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(BASE, "icon", "2.0")
OUT = os.path.join(BASE, "app.ico")

# 生成尺寸: 小尺寸用 BMP, 256 用 PNG
_SIZES = (16, 24, 32, 48, 64, 128, 256)

_app = QGuiApplication([])


def png_size(png: bytes) -> tuple[int, int]:
    """从 PNG IHDR 读取宽高(big-endian)。"""
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是有效的 PNG 文件")
    w, h = struct.unpack(">II", png[16:24])
    return w, h


def read_png(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def find_sources() -> tuple[bytes, bytes]:
    """扫描 icon/2.0 目录, 按实际尺寸找出 512x512 与 256x256 PNG。"""
    if not os.path.isdir(SRC_DIR):
        raise FileNotFoundError(f"图标目录不存在: {SRC_DIR}")
    png512: bytes | None = None
    png256: bytes | None = None
    src512 = src256 = ""
    for n in sorted(os.listdir(SRC_DIR)):
        if not n.lower().endswith(".png"):
            continue
        p = os.path.join(SRC_DIR, n)
        data = read_png(p)
        try:
            w, h = png_size(data)
        except ValueError:
            continue
        if (w, h) == (512, 512) and png512 is None:
            png512, src512 = data, p
        elif (w, h) == (256, 256) and png256 is None:
            png256, src256 = data, p
    if png512 is None or png256 is None:
        raise ValueError(
            f"icon/2.0 缺少 512x512 或 256x256 PNG(找到 512={bool(png512)} 256={bool(png256)})")
    print(f"512 源: {os.path.basename(src512)}")
    print(f"256 源: {os.path.basename(src256)}")
    return png512, png256


def _load_img(png: bytes) -> QImage:
    img = QImage()
    img.loadFromData(png, "PNG")
    return img.convertToFormat(QImage.Format.Format_ARGB32)


def _resize(img: QImage, size: int) -> QImage:
    """等比缩放(扩张至填满方形)到目标尺寸。"""
    pm = QPixmap.fromImage(img).scaled(
        size, size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    return pm.toImage().convertToFormat(QImage.Format.Format_ARGB32)


def _encode_dib(img: QImage, size: int) -> bytes:
    """编码为 32bpp BGRA 的 BITMAPINFOHEADER + XOR(自下而上) + 全0 AND mask。"""
    header = struct.pack(
        "<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, size * size * 4, 0, 0, 0, 0)
    pixels = bytearray()
    for y in range(size - 1, -1, -1):
        for x in range(size):
            c = img.pixelColor(x, y)
            pixels += bytes((c.blue(), c.green(), c.red(), c.alpha()))
    and_stride = ((size + 31) // 32) * 4
    and_mask = bytes(and_stride * size)
    return header + bytes(pixels) + and_mask


def _encode_png(img: QImage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def write_ico(entries: list[tuple[int, bytes, bool]], out: str) -> None:
    """写标准 ICO。entries: (尺寸, 图像字节, 是否PNG格式)。"""
    count = len(entries)
    data = bytearray()
    data += (0).to_bytes(2, "little")       # reserved
    data += (1).to_bytes(2, "little")       # type: icon
    data += count.to_bytes(2, "little")     # image count
    offset = 6 + 16 * count
    for size, blob, _is_png in entries:
        dim = 0 if size >= 256 else size  # 宽/高字节: 0 表示 256
        data += bytes((dim, dim, 0, 0))    # width, height, colorCount, reserved
        data += (1).to_bytes(2, "little")  # planes
        data += (32).to_bytes(2, "little")  # bit count
        data += len(blob).to_bytes(4, "little")   # bytes in resource
        data += offset.to_bytes(4, "little")       # image offset
        offset += len(blob)
    for _size, blob, _is_png in entries:
        data += blob
    with open(out, "wb") as f:
        f.write(bytes(data))
    print(f"已写入 {out}: {count} 个尺寸 {sorted(e[0] for e in entries)}")


def main() -> int:
    try:
        png512, png256 = find_sources()
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误: {exc}")
        return 1
    s512 = png_size(png512)
    s256 = png_size(png256)
    print(f"512 PNG -> {s512}")
    print(f"256 PNG -> {s256}")
    if s512 != (512, 512) or s256 != (256, 256):
        print("尺寸与预期不符, 中止")
        return 1
    # 以 512 源为基准缩放生成全部尺寸(质量更好)
    base = _load_img(png512)
    entries: list[tuple[int, bytes, bool]] = []
    for s in _SIZES:
        qi = _resize(base, s)
        if s >= 256:
            entries.append((s, _encode_png(qi), True))
        else:
            entries.append((s, _encode_dib(qi, s), False))
    write_ico(entries, OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
