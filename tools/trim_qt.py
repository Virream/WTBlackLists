"""打包后精简 Qt 体积: 删除用不到的 Qt6 DLL / 翻译文件 / 多余图片插件。

仅用于 PyInstaller onedir 打包完成后(dist/<应用名>/_internal/PyQt6/Qt6)。

用法:
    python tools/trim_qt.py                        # 默认 WTBlackList(删 jpeg 等)
    python tools/trim_qt.py WTRouteTester --keep-imageformats

`--keep-imageformats` 保留 jpeg/webp 图片插件 —— 跑图线路测试器需要显示
8111 下载的地图图片(可能是 JPEG), 删掉 qjpeg.dll 会导致地图无法显示。
"""
from __future__ import annotations

import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 用不到的 Qt6 动态库
REMOVE_BIN = ["Qt6Pdf.dll", "Qt6Network.dll"]
# 多余的图片格式插件(只需 ico/png, 由 Qt 内置支持)
REMOVE_PLUGINS = [
    "qjpeg.dll", "qwebp.dll", "qtiff.dll", "qicns.dll",
    "qtga.dll", "qwbmp.dll", "qgif.dll", "qpdf.dll",
]
# 保留 jpeg/webp 时仍可安全删除的图片插件
REMOVE_PLUGINS_KEEP_IMG = [
    "qtiff.dll", "qicns.dll", "qtga.dll", "qwbmp.dll", "qgif.dll", "qpdf.dll",
]


def main() -> int:
    args = sys.argv[1:]
    keep_img = "--keep-imageformats" in args
    names = [a for a in args if not a.startswith("--")]
    app_name = names[0] if names else "WTBlackList"
    qt = os.path.join(BASE, "dist", app_name, "_internal", "PyQt6", "Qt6")
    if not os.path.isdir(qt):
        print("未找到 Qt6 目录:", qt)
        return 1
    plugins = REMOVE_PLUGINS_KEEP_IMG if keep_img else REMOVE_PLUGINS
    removed = 0
    for name in REMOVE_BIN:
        p = os.path.join(qt, "bin", name)
        if os.path.exists(p):
            os.remove(p)
            removed += 1
    for name in plugins:
        p = os.path.join(qt, "plugins", "imageformats", name)
        if os.path.exists(p):
            os.remove(p)
            removed += 1
    tr = os.path.join(qt, "translations")
    if os.path.isdir(tr):
        shutil.rmtree(tr)
        removed += 1
    kept = "(保留 jpeg/webp 图片插件)" if keep_img else ""
    print(f"[{app_name}] Qt 精简完成, 移除 {removed} 项{kept}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
