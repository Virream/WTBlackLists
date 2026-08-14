"""打包后精简 Qt 体积: 删除用不到的 Qt6 DLL / 翻译文件 / 多余图片插件。

仅用于 PyInstaller onedir 打包完成后(dist/WTBlackList/_internal/PyQt6/Qt6)。
本项目只用 QtWidgets/Core/Gui, 无需 PDF/Network; 图片只需 png/ico(内置)。
运行: .venv\\Scripts\\python.exe tools\\trim_qt.py   (由 build.ps1 自动调用)
"""
from __future__ import annotations

import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QT = os.path.join(BASE, "dist", "WTBlackList", "_internal", "PyQt6", "Qt6")

# 用不到的 Qt6 动态库
REMOVE_BIN = ["Qt6Pdf.dll", "Qt6Network.dll"]
# 多余的图片格式插件(只需 ico/png, 由 Qt 内置支持)
REMOVE_PLUGINS = [
    "qjpeg.dll", "qwebp.dll", "qtiff.dll", "qicns.dll",
    "qtga.dll", "qwbmp.dll", "qgif.dll", "qpdf.dll",
]


def main() -> int:
    if not os.path.isdir(QT):
        print("未找到 Qt6 目录:", QT)
        return 1
    removed = 0
    for name in REMOVE_BIN:
        p = os.path.join(QT, "bin", name)
        if os.path.exists(p):
            os.remove(p)
            removed += 1
    for name in REMOVE_PLUGINS:
        p = os.path.join(QT, "plugins", name)
        if os.path.exists(p):
            os.remove(p)
            removed += 1
    tr = os.path.join(QT, "translations")
    if os.path.isdir(tr):
        shutil.rmtree(tr)
        removed += 1
    print(f"Qt 精简完成, 移除 {removed} 项(体积约减少 12MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
