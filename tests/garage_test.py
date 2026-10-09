"""车库判定(用 8111 地图图片)单元测试: 纯逻辑, 不依赖游戏/网络/Qt。

实测两种“在机库”的情形(记录于 `wt81111g/garage.py`):
    ① 没进过对局: /map.img 拿不到图片(请求失败 / 不是图片, 渲染不出来)
    ② 进过对局退回机库: /map.img 变成 1519 字节 PNG “?” 占位图(128×128)
进对局后换成 ≥1024² 的真地图, 所以这里用“图像头合法的最小图片”验证各判定分支。

注意: 这里**故意不用 QImage/Qt 造图** —— 单元测试不需要真像素, 而提前创建
QApplication 会影响同进程内的老 UI 测试(实测会原生崩溃)。
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wt81111g import garage  # noqa: E402
from wt81111g.garage import PROBE_BYTES, image_size, in_map_by_image  # noqa: E402

results = []


def png_bytes(w: int, h: int, pad: int = 64) -> bytes:
    """构造一个 PNG(判定只看头部, 不需要真像素)。"""
    ihdr = (w.to_bytes(4, "big") + h.to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00" + b"\x00\x00\x00\x00")
    return (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + ihdr
            + b"\x00" * pad)


def jpeg_bytes(w: int, h: int, pad: int = 4096) -> bytes:
    """构造一个 JPEG: SOI + APP0 + SOF0(带尺寸) + 填充。"""
    app0 = (b"\xff\xe0" + (16).to_bytes(2, "big")
            + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    sof0 = (b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08"
            + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x03"
            + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01")
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xda" + b"\x00" * pad


def test_image_size() -> None:
    """PNG/JPEG 头解析(含截断/垃圾数据)。"""
    assert image_size(png_bytes(128, 128)) == (128, 128)
    assert image_size(jpeg_bytes(1024, 768)) == (1024, 768)
    # 截断的图片只要头部完整照样能读出尺寸(探测时只读前 16KB)
    assert image_size(jpeg_bytes(2048, 1024, pad=PROBE_BYTES * 2)[:PROBE_BYTES]) == (2048, 1024)
    assert image_size(b"") is None
    assert image_size(b"not an image at all") is None
    assert image_size(b"\x89PNG\r\n\x1a\n") is None      # 头部不全
    assert image_size(b"\xff\xd8") is None               # 只有 SOI
    results.append("garage image header parsing OK")


def test_in_map_by_image() -> None:
    """真地图 → True; 没图片 / 问号占位图 → False(在车库)。"""
    # ① 拿不到图片(实测机库里有这种情况: 连浏览器都加载不出来) → 车库
    assert in_map_by_image(None) is False
    assert in_map_by_image(b"") is False
    assert in_map_by_image(b"<html>404</html>") is False
    # ② 尺寸规则: 占位图 128², 真地图 ≥1024²
    assert in_map_by_image(png_bytes(128, 128)) is False
    assert in_map_by_image(jpeg_bytes(1024, 1024)) is True
    assert in_map_by_image(png_bytes(2048, 1024)) is True
    # ③ 截断的真地图(探测只读前 16KB, 取满即断开): 照样能判出是真地图
    big = jpeg_bytes(2048, 2048, pad=PROBE_BYTES * 2)
    assert len(big) > PROBE_BYTES, len(big)
    assert in_map_by_image(big[:PROBE_BYTES]) is True
    # ④ 取满探测长度 → 不看内容也当成真地图(占位图只有 1519 字节)
    assert in_map_by_image(b"\xff\xd8\xff" + b"\x00" * PROBE_BYTES) is True
    # ⑤ sha1 规则: 与记录的占位图逐字节一致 → 车库(即使尺寸看着像真地图)
    fake = png_bytes(1024, 1024, pad=garage.GARAGE_MAP_BYTES - 33)
    assert len(fake) == garage.GARAGE_MAP_BYTES, len(fake)
    assert in_map_by_image(fake) is True                 # 未登记 → 按尺寸当真地图
    saved = garage.GARAGE_MAP_SHA1
    try:
        garage.GARAGE_MAP_SHA1 = hashlib.sha1(fake).hexdigest()
        assert in_map_by_image(fake) is False, "登记过的占位图应判为车库"
    finally:
        garage.GARAGE_MAP_SHA1 = saved
    results.append("garage map-image classification OK")


def test_real_placeholder_sample() -> None:
    """仓库里存的真实样本(如果有)必须与常量一致且判为“车库”。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "docs", "assets", "8111_map_placeholder.png")
    if not os.path.exists(path):
        results.append("garage placeholder sample: 无存档样本, 跳过")
        return
    with open(path, "rb") as f:
        data = f.read()
    assert len(data) == garage.GARAGE_MAP_BYTES, len(data)
    assert hashlib.sha1(data).hexdigest() == garage.GARAGE_MAP_SHA1
    assert image_size(data) == (garage.GARAGE_MAP_EDGE, garage.GARAGE_MAP_EDGE)
    assert in_map_by_image(data) is False
    results.append("garage placeholder sample matches OK")


def main() -> int:
    test_image_size()
    test_in_map_by_image()
    test_real_placeholder_sample()
    for r in results:
        print("OK:", r)
    print("GARAGE TEST PASSED" if len(results) == 3 else "FAILED")
    return 0 if len(results) == 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
