"""将用户提供的 512x512 与 256x256 PNG 图标转换为多尺寸 app.ico。

512 为高分辨率(HiDPI)首选, 256 为兼容性尺寸; 两者内容一致。
"""
import os
import struct
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_512 = os.path.join(BASE, "512x512 ico.png")
SRC_256 = os.path.join(BASE, "256x256 ico.png")
OUT = os.path.join(BASE, "app.ico")


def png_size(png: bytes) -> tuple[int, int]:
    """从 PNG IHDR 读取宽高(big-endian)。"""
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是有效的 PNG 文件")
    w, h = struct.unpack(">II", png[16:24])
    return w, h


def read_png(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def write_ico(pngs: list[bytes], out: str) -> None:
    count = len(pngs)
    data = bytearray()
    data += (0).to_bytes(2, "little")       # reserved
    data += (1).to_bytes(2, "little")       # type: icon
    data += count.to_bytes(2, "little")     # image count
    offset = 6 + 16 * count
    for png in pngs:
        entry = bytearray()
        # 宽/高字节: 0 表示 256(标准约定), 512 也按 0 写入, Windows 读取 PNG 实际尺寸
        entry += (0).to_bytes(1, "little")
        entry += (0).to_bytes(1, "little")
        entry += (0).to_bytes(1, "little")  # color count
        entry += (0).to_bytes(1, "little")  # reserved
        entry += (1).to_bytes(2, "little")  # planes
        entry += (32).to_bytes(2, "little")  # bit count
        entry += (len(png)).to_bytes(4, "little")   # bytes in resource
        entry += offset.to_bytes(4, "little")       # image offset
        data += entry
        offset += len(png)
    for png in pngs:
        data += png
    with open(out, "wb") as f:
        f.write(bytes(data))
    print(f"已写入 {out}: {count} 个尺寸")


def main() -> int:
    png512 = read_png(SRC_512)
    png256 = read_png(SRC_256)
    s512 = png_size(png512)
    s256 = png_size(png256)
    print(f"512x512 ico.png -> {s512}")
    print(f"256x256 ico.png -> {s256}")
    if s512 != (512, 512) or s256 != (256, 256):
        print("尺寸与预期不符, 中止")
        return 1
    write_ico([png512, png256], OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
