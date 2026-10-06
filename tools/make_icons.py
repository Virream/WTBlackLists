"""把 assets/icons/*.svg 光栅化为 PNG(Qt 内置 PNG 解码, 打包后不依赖 qsvg 插件)。

为什么要转 PNG:
- 项目打包时用 `tools/trim_qt.py` 精简过 Qt 插件, SVG 图片插件(`qsvg.dll`)可能被删掉,
  而 **PNG 是 QtGui 内建格式**, 一定可用;
- 图标本身还要按控件尺寸/高 DPI 缩放, 直接读现成的 PNG 更省事。

图标来源: **Lucide**(https://github.com/lucide-icons/lucide, ISC 许可, 源自 Feather)。
SVG 里的 `stroke="currentColor"` 会被改写为 `#000000` —— 因为运行时统一用
SourceIn 合成着色(一套黑描边图可以适配任意状态的颜色), 而 Qt 的 SVG 渲染对
`currentColor` 支持不完整。

用法: .venv\\Scripts\\python.exe tools\\make_icons.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QByteArray, Qt                       # noqa: E402
from PyQt6.QtGui import QImage, QPainter                      # noqa: E402
from PyQt6.QtSvg import QSvgRenderer                          # noqa: E402
from PyQt6.QtWidgets import QApplication                      # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON_DIR = os.path.join(ROOT, "assets", "icons")
SIZE = 64          # 光栅化边长(px): 树里按 18 逻辑像素显示, 留足缩放余量

# 文件名(不含扩展名) → 说明
ICONS = {
    "eye": "眼睛 —— 轨迹在地图上显示中",
    "eye_off": "眼睛加斜杠 —— 轨迹在地图上已隐藏",
}


def render(name: str) -> str | None:
    svg_path = os.path.join(ICON_DIR, f"{name}.svg")
    if not os.path.exists(svg_path):
        print(f"[跳过] 缺少 {svg_path}")
        return None
    with open(svg_path, encoding="utf-8") as fh:
        svg = fh.read()
    if "currentColor" in svg:          # 统一黑色描边, 颜色交给运行时合成
        svg = svg.replace("currentColor", "#000000")
        with open(svg_path, "w", encoding="utf-8") as fh:
            fh.write(svg)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        print(f"[失败] SVG 解析失败: {svg_path}")
        return None
    img = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    out = os.path.join(ICON_DIR, f"{name}.png")
    img.save(out, "PNG")
    print(f"[OK] {os.path.relpath(out, ROOT)}  {SIZE}x{SIZE}  {os.path.getsize(out)} 字节")
    return out


def main() -> int:
    QApplication([])
    ok = 0
    for name in ICONS:
        if render(name):
            ok += 1
    print(f"\n完成: {ok}/{len(ICONS)} 个图标")
    return 0 if ok == len(ICONS) else 1


if __name__ == "__main__":
    sys.exit(main())
