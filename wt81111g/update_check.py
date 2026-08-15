"""GitHub 版本更新检测与连通性检测。"""
from __future__ import annotations

import re
import time

import requests

from .config import APP_VERSION

REPO = "Virream/WTBlackLists"
_RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
_PING_URL = "https://api.github.com"


def parse_version(v: str) -> tuple:
    """把版本号 'v2.0.1' / '2.0.1' 解析为 (2, 0, 1); 无法解析返回空元组。"""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", v or "")
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


def check_latest() -> dict | None:
    """检查 GitHub 最新 release; 存在比本地更新的版本时返回信息 dict, 否则 None。"""
    try:
        r = requests.get(_RELEASES_URL, timeout=8)
        if r.status_code != 200:
            return None
        j = r.json()
        tag = str(j.get("tag_name") or "")
        if parse_version(tag) <= parse_version(APP_VERSION):
            return None
        assets = j.get("assets") or []
        download = ""
        for a in assets:
            n = str(a.get("name") or "").lower()
            if n.endswith((".exe", ".zip")):
                download = str(a.get("browser_download_url") or "")
                break
        return {
            "version": tag,
            "current": APP_VERSION,
            "name": str(j.get("name") or tag),
            "body": str(j.get("body") or ""),
            "html_url": str(j.get("html_url") or f"https://github.com/{REPO}/releases"),
            "download_url": download,
        }
    except Exception:  # noqa: BLE001
        return None


def ping() -> tuple[bool, float]:
    """检测 GitHub API 连通性, 返回 (是否可达, 耗时秒)。"""
    t0 = time.monotonic()
    try:
        resp = requests.get(_PING_URL, timeout=6)
        return resp.status_code == 200, time.monotonic() - t0
    except Exception:  # noqa: BLE001
        return False, time.monotonic() - t0
