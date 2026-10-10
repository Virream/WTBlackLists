"""战争雷霆 8111 本地接口客户端。"""
from __future__ import annotations

import json

import requests

from .config import WT_BASE_URL

# 请求超时: 8111 平时 rtt 才 ~10ms, 但在**换局/换地图那几十秒里游戏会很忙**
# (实测: 刚离开对局时 /map.img、/hudmsg 可能一两秒才回应), 1 秒会把正常答案
# 误判成“没回答”。
_TIMEOUT = 2.0


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
            resp = self.session.get(self.base + "/state", timeout=self.timeout)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def gamechat(self, last_id: int, raise_on_error: bool = False) -> list:
        try:
            data = self.get("/gamechat", {"lastId": last_id})
            return data if isinstance(data, list) else []
        except requests.RequestException:
            if raise_on_error:
                raise
            return []

    def hudmsg(self, last_evt: int, last_dmg: int, raise_on_error: bool = False) -> dict:
        try:
            data = self.get("/hudmsg", {"lastEvt": last_evt, "lastDmg": last_dmg})
            return data if isinstance(data, dict) else {}
        except requests.RequestException:
            if raise_on_error:
                raise
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

    def map_image(self, limit: int | None = None, timeout: float | None = None,
                  raise_on_error: bool = False) -> bytes | None:
        """地图卫星图原始字节(PNG/JPEG, 由 GUI 侧解码为 QPixmap)。

        仅在地图内(对局/试车场)可用; 机库等场景返回固定的 “?” 占位图,
        这里用图片魔数校验, 非图片(如错误页)一律返回 None。

        `limit` 只读取前若干字节, 给“在不在车库”的判定用(见 `garage.py`):
        车库占位图只有 1.5KB, 能完整拿到; 真地图几百 KB~几 MB, 取满即断开连接,
        省掉整张地图的下载。

        `raise_on_error=True` 时请求失败(超时/连接错)不吞掉而是抛 `RequestException`
        —— 调用方需要区分“**没有回答**”和“回答了一个非图片”: 实测进对局初期
        `/map.img` 可能要好几秒才回应, 把它当成“不在地图里”会误判回车库。
        """
        try:
            resp = self.session.get(self.base + "/map.img", timeout=timeout or self.timeout,
                                    stream=bool(limit))
            resp.raise_for_status()
            if limit:
                buf = bytearray()
                for chunk in resp.iter_content(limit):
                    buf += chunk
                    if len(buf) >= limit:
                        break
                resp.close()
                data = bytes(buf)
            else:
                data = resp.content or b""
        except requests.RequestException:
            if raise_on_error:
                raise
            return None
        if data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8\xff"):
            return data
        return None
