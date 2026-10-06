"""战争雷霆 8111 本地接口客户端。"""
from __future__ import annotations

import json

import requests

from .config import WT_BASE_URL

_TIMEOUT = 1.0


class WT8111:
    """封装对 8111 本地接口(127.0.0.1:8111)的 HTTP 请求(全部端点)。"""

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

    def indicators(self) -> dict:
        """仪表数据。陆战含 rpm/gear/army/stabilizer 等, 空战含姿态/发动机等。

        `type` 字段为载具内部名(如 germ_leopard_2pl), `speed` 为 km/h。
        """
        try:
            data = self.get("/indicators")
            return data if isinstance(data, dict) else {}
        except requests.RequestException:
            return {}

    def map_info(self) -> dict:
        try:
            data = self.get("/map_info.json")
            return data if isinstance(data, dict) else {}
        except requests.RequestException:
            return {}

    def map_objects(self) -> list:
        """地图上的对象列表(每个含 type/icon/x/y/dx/dy 等)。

        坐标 x/y 为 0~1 归一化值, 直接乘以地图图片尺寸即为像素位置。
        玩家自己通常为 type == "player"; 部分场景(如自定义 bot 对局)可能无玩家对象。
        """
        try:
            data = self.get("/map_obj.json")
            return data if isinstance(data, list) else []
        except requests.RequestException:
            return []

    def map_image(self) -> bytes | None:
        """地图卫星图原始字节(PNG/JPEG, 由 GUI 侧解码为 QPixmap)。

        仅在地图内(对局/试车场)可用; 机库等场景可能返回错误页,
        这里用图片魔数校验, 非图片一律返回 None。
        """
        try:
            resp = self.session.get(self.base + "/map.img", timeout=self.timeout)
            resp.raise_for_status()
            data = resp.content or b""
        except requests.RequestException:
            return None
        if data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8\xff"):
            return data
        return None
