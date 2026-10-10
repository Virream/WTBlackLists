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
        try:
            resp = self.session.get(url, params=params, timeout=timeout or self.timeout)
            resp.raise_for_status()
        except requests.RequestException:
            # 请求失败时丢掉连接池: 换局/换地图时游戏会把旧连接变成“死连接”,
            # 复用它会一直卡到超时, 关掉连接池后下一次请求重新建连即恢复。
            self.session.close()
            raise
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
            self.session.close()
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

    def mission(self) -> dict:
        try:
            data = self.get("/mission.json")
            return data if isinstance(data, dict) else {}
        except requests.RequestException:
            return {}

    def map_info(self) -> dict:
        try:
            data = self.get("/map_info.json")
            return data if isinstance(data, dict) else {}
        except requests.RequestException:
            return {}

    def map_image(self, limit: int | None = None, timeout: float | None = None,
                  raise_on_error: bool = False) -> bytes | None:
        """地图图片原始字节(PNG/JPEG); 请求失败或返回的不是图片 → None。

        机库里有两种情形: 一是**拿不到地图图片**(连浏览器也加载不出来), 二是进过对局
        退回机库后拿到固定的 “?” 占位图; 进对局/试车场则换成真地图(≥1024²)。
        所以“能否拿到地图图片”本身就是判定“在不在车库”的依据(见 `garage.py`)。

        `limit` 只读取前若干字节: 占位图只有 1.5KB 能完整拿到, 真地图几百 KB~几 MB,
        取满即断开连接, 省掉整张地图的下载。

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
            self.session.close()    # 见 get(): 丢掉可能已经死掉的连接
            if raise_on_error:
                raise
            return None
        if data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8\xff"):
            return data
        return None
