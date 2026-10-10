"""将项目源码打包为 zip, 供与软件一起分发。

只打包 **git 跟踪的文件**(`git ls-files`)。
这样 .gitignore 里排除的运行/构建产物、本地解包的第三方工具(WTRTI/)以及
探针脚本采样的数据(speed_log*.csv 等)都不会混进 zip —— 之前用目录扫描时,
这些未跟踪文件会被一起打进去。

输出: WTBlackList_source.zip (项目根目录)
运行: .venv\\Scripts\\python.exe tools\\make_source_zip.py
"""
from __future__ import annotations

import os
import subprocess
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "WTBlackList_source.zip")

# 仅在 git 不可用(非工作区)时的兜底目录扫描规则
EXCLUDE_DIRS = {
    ".venv", "build", "build_assets", "dist", "data", "evidences", "logs",
    "__pycache__", ".git", ".pytest_cache", "backups", "WTRTI",
}
EXCLUDE_FILES = {
    "build.log", "WTBlackList_source.zip",
}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".spec"}


def git_tracked() -> list[str] | None:
    """git 跟踪的相对路径列表(以 '/' 分隔); 不是 git 工作区则返回 None。"""
    try:
        p = subprocess.run(
            ["git", "ls-files", "-z"], cwd=BASE, capture_output=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [i for i in p.stdout.decode("utf-8", "surrogateescape").split("\0") if i]


def walk_files() -> list[str]:
    """兜底: 直接扫磁盘(会带上未跟踪文件, 仅在没有 git 时使用)。"""
    out: list[str] = []
    for root, dirs, names in os.walk(BASE):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        rel_root = os.path.relpath(root, BASE)
        rel_root = "" if rel_root == "." else rel_root
        for n in names:
            if n in EXCLUDE_FILES or os.path.splitext(n)[1].lower() in EXCLUDE_SUFFIX:
                continue
            rel = os.path.join(rel_root, n) if rel_root else n
            out.append(rel.replace(os.sep, "/"))
    return out


def main() -> int:
    rels = git_tracked()
    source = "git ls-files"
    if rels is None:
        rels, source = walk_files(), "目录扫描(未检测到 git)"
    rels = sorted(r for r in rels if os.path.basename(r) not in EXCLUDE_FILES)

    written: list[str] = []
    skipped: list[str] = []
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in rels:
            full = os.path.join(BASE, rel)
            if not os.path.isfile(full):  # 索引里有但工作区已删(未提交)
                skipped.append(rel)
                continue
            z.write(full, rel)
            written.append(rel)

    size = os.path.getsize(OUT)
    print(f"已生成: {OUT}")
    print(f"  来源: {source}, 文件数: {len(written)}, "
          f"大小: {size/1024:.0f} KB ({size/1024/1024:.2f} MB)")
    if skipped:
        print(f"  跳过工作区缺失的 {len(skipped)} 个: {', '.join(skipped[:5])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
