"""战争雷霆 8111 本地接口客户端。"""
from __future__ import annotations

import json

import requests

from .config import WT_BASE_URL

_TIMEOUT = 1.0


class WT8111:
    """封装对 localhost:8111 的 HTTP 请求(全部端点)。"""

    def __init__(self, base_url: str = WT_BASE_URL, timeout: float = _TIMEOUT):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def get(self, path: str, params: dict | None = None, timeout: float | None = None):
        url = self.base + path
        resp = self.session.get(url, params=params, timeout=timeout or self.timeout)
        resp.raise_for_status()
        text = resp.text.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def connected(self) -> bool:
        """轻量探测 8111 是否可达。"""
        try:
            resp = self.session.get(self.base + "/state", timeout=0.8)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def gamechat(self, last_id: int) -> list:
        try:
            data = self.get("/gamechat", {"lastId": last_id})
            return data if isinstance(data, list) else []
        except requests.RequestException:
            return []

    def hudmsg(self, last_evt: int, last_dmg: int) -> dict:
        try:
            data = self.get("/hudmsg", {"lastEvt": last_evt, "lastDmg": last_dmg})
            return data if isinstance(data, dict) else {}
        except requests.RequestException:
            return {}

    def mission(self, raise_on_error: bool = False) -> dict:
        try:
            data = self.get("/mission.json")
            return data if isinstance(data, dict) else {}
        except requests.RequestException:
            if raise_on_error:
                raise
            return {}

    def map_info(self) -> dict:
        try:
            data = self.get("/map_info.json")
            return data if isinstance(data, dict) else {}
        except requests.RequestException:
            return {}
