"""将项目源码打包为 zip, 供与软件一起分发。

排除: .venv / build / build_assets / dist / data / evidences / logs /
      __pycache__ / *.spec / build.log 等运行与构建产物。
输出: WTBlackList_source.zip (项目根目录)
运行: .venv\\Scripts\\python.exe tools\\make_source_zip.py
"""
from __future__ import annotations

import os
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "WTBlackList_source.zip")

EXCLUDE_DIRS = {
    ".venv", "build", "build_assets", "dist", "data", "evidences", "logs",
    "__pycache__", ".git", ".pytest_cache", "backups",
}
EXCLUDE_FILES = {
    "build.log", "WTBlackList_source.zip",
}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".spec"}
ALWAYS_INCLUDE_DIRS = {"wt81111g", "tools", "tests", "docs"}


def main() -> int:
    files: list[tuple[str, str]] = []  # (绝对路径, 包内相对路径)
    for root, dirs, names in os.walk(BASE):
        rel_root = os.path.relpath(root, BASE)
        # 过滤目录
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for n in names:
            full = os.path.join(root, n)
            rel = os.path.normpath(os.path.join(rel_root, n))
            if n in EXCLUDE_FILES:
                continue
            if os.path.splitext(n)[1].lower() in EXCLUDE_SUFFIX:
                continue
            files.append((full, rel))

    files.sort(key=lambda t: t[1])
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for full, rel in files:
            z.write(full, rel)

    n = len(files)
    size = os.path.getsize(OUT)
    print(f"已生成: {OUT}")
    print(f"  文件数: {n}, 大小: {size/1024:.0f} KB ({size/1024/1024:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
