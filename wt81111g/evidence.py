"""证据文件夹: 在 <根目录>/evidences/<玩家ID>/<条目ID>/ 下创建并打开。"""
from __future__ import annotations

import os
import re
import subprocess
import sys

from .config import evidences_dir

# 条目ID格式: 玩家ID_YYYYMMDD_HH_MM[可选 _k]
_ENTRY_ID_RE = re.compile(r"^\d+_\d{8}_\d{2}_\d{2}(?:_\d+)?$")


def evidence_folder(player_id: str, entry_id: str) -> str:
    """返回证据文件夹路径; 玩家ID/条目ID 非法时抛 ValueError(防路径穿越)。"""
    pid = (player_id or "").strip()
    eid = (entry_id or "").strip()
    if not pid.isdigit():
        raise ValueError("玩家ID必须为数字")
    if not _ENTRY_ID_RE.match(eid):
        raise ValueError("条目ID格式非法")
    return os.path.join(evidences_dir(), pid, eid)


def ensure_and_open(player_id: str, entry_id: str) -> str:
    """创建(若已存在则复用)证据文件夹并打开,返回文件夹路径。"""
    path = evidence_folder(player_id, entry_id)
    os.makedirs(path, exist_ok=True)
    open_folder(path)
    return path


def open_folder(path: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])
