"""从 War Thunder Live 玩家主页获取玩家昵称。"""
from __future__ import annotations

import logging
import re
import time

import requests

from .config import PROFILE_URL_TEMPLATE

log = logging.getLogger("warthunder")

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# 主机平台后缀(WTLive 玩家页昵称尾部): @psn / @live / @xbox 等
_PLATFORM_SUFFIX_RE = re.compile(r"@(?:psn|live|xbox|steam)\b", re.IGNORECASE)


def clean_wtlive_nickname(nick: str) -> str:
    """清洗 WT Live 抓取到的昵称: 去掉 @psn/@live/@xbox 平台后缀。"""
    nick = (nick or "").strip()
    if not nick:
        return ""
    nick = _PLATFORM_SUFFIX_RE.sub("", nick).strip()
    nick = nick.rstrip("@").strip()
    return nick
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# 复用同一 HTTP 连接(keep-alive), 避免对同一域名逐次新建 TCP+TLS 握手
_SESSION = requests.Session()
_SESSION.headers.update(_HEADERS)

# 状态码分类
STATUS_PERMANENT = 404   # 页面不存在 → 无效/注销 ID, 长缓存不反复重试
STATUS_RATE_LIMIT = 429  # 被限流 → 停止整批抓取


def _parse_nickname(html: str) -> str | None:
    m = _TITLE_RE.search(html)
    if not m:
        return None
    title = m.group(1).strip()
    # 正常页面标题形如 "WT Live // 目击而道存矣"
    if "//" in title:
        nick = title.split("//", 1)[1].strip()
    else:
        nick = title
    if not nick or nick.lower() in ("wt live", "error", "404"):
        return None
    return clean_wtlive_nickname(nick)


def fetch_profile_with_status(player_id: str, timeout: float = 8.0) -> tuple[str | None, int]:
    """根据玩家ID访问 live.warthunder.com/user/<id>/ 并解析昵称。

    返回 (昵称或None, HTTP状态码); 网络异常时状态码为 0。
    状态码可用于区分: 404(无效ID, 永久失败) / 429(被限流) / 其他(临时失败)。
    """
    pid = (player_id or "").strip()
    if not pid.isdigit():
        return None, 400
    url = PROFILE_URL_TEMPLATE.format(player_id=pid)
    try:
        resp = _SESSION.get(url, timeout=timeout)
        if resp.status_code != 200:
            log.warning("profile %s -> HTTP %s", pid, resp.status_code)
            return None, resp.status_code
        nick = _parse_nickname(resp.text)
        if nick is None:
            log.warning("profile %s: 疑似无效页面 title 无法解析", pid)
            return None, resp.status_code
        log.info("profile %s -> nickname %s", pid, nick)
        return nick, resp.status_code
    except requests.RequestException as exc:
        log.warning("profile %s fetch failed: %s", pid, exc)
        return None, 0


def fetch_profile_nickname(player_id: str, timeout: float = 8.0) -> str | None:
    """兼容入口: 仅返回昵称(或None)。"""
    nick, _ = fetch_profile_with_status(player_id, timeout)
    return nick


# WT Live 首页,用于评估黑名单查询的访问流畅度(服务器在国外)
FEED_URL = "https://live.warthunder.com/feed/all/"


def check_feed_latency(timeout: float = 8.0) -> tuple[bool, float]:
    """检测 WT Live 首页访问延迟。返回 (是否可达, 耗时秒)。"""
    start = time.monotonic()
    try:
        resp = _SESSION.get(FEED_URL, timeout=timeout)  # 复用 keep-alive 会话
        elapsed = time.monotonic() - start
        return resp.status_code == 200, elapsed
    except requests.RequestException:
        return False, time.monotonic() - start
