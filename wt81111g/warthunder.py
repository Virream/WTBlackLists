"""从 War Thunder Live 玩家主页获取玩家昵称。

主源: live.warthunder.com/user/<id>/ (稳定, 无验证)
备选源: warthunder.com/zh/community/userinfo/?uid=<id>/ (官网, 偶发 Cloudflare 人机验证)
"""
from __future__ import annotations

import logging
import re
import time

import requests

from .config import PROFILE_URL_TEMPLATE, WEBSITE_USERINFO_TEMPLATE
from .nickname_util import clean_wtlive_nickname

log = logging.getLogger("warthunder")

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
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
    # 清洗掉主机平台后缀: "RUNASAPURIZUMU1@psn" -> "RUNASAPURIZUMU1"
    nick = clean_wtlive_nickname(nick)
    return nick or None


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
    """兼容入口: 仅返回昵称(或None)。优先主源, 失败时尝试官网备选源。"""
    nick, _ = fetch_profile_best_effort(player_id, timeout)
    return nick


# ----------------------------------------------------------------------
# 备选源: 官网社区 userinfo(偶发 Cloudflare 人机验证, 不稳定)
# ----------------------------------------------------------------------
# 官网 userinfo 页面昵称解析(基于社区页面常见结构, 拿到真实 HTML 后校准)
_WEBSITE_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
# 官网 userinfo 页面的昵称结构(实测 SingleFile 快照):
#   <li class=user-profile__data-nick>  昵称  </li>
#   <img ... alt=昵称>  (头像 alt, 备用)
# 注意: 属性可能带引号也可能不带(SingleFile 快照中为 class=... 无引号)
_WEBSITE_NICK_RE = re.compile(
    r'class=["\']?user-profile__data-nick["\']?[^>]*>\s*([^<]{1,40}?)\s*<',
    re.IGNORECASE,
)
_WEBSITE_AVATAR_ALT_RE = re.compile(
    r'user-profile__ava[^>]*>\s*<img[^>]*\salt=["\']?([^"\'>]{1,40})["\']?',
    re.IGNORECASE,
)


def _parse_website_nickname(html: str) -> str | None:
    """从官网 userinfo 页面解析昵称。返回清洗后的昵称或 None。

    优先 .user-profile__data-nick 元素; 其次头像 alt; 最后 title 回退。
    """
    if not html:
        return None
    # 1. data-nick 元素(最可靠)
    m = _WEBSITE_NICK_RE.search(html)
    if m:
        nick = clean_wtlive_nickname(m.group(1).strip())
        if nick:
            return nick
    # 2. 头像 alt 属性
    m = _WEBSITE_AVATAR_ALT_RE.search(html)
    if m:
        nick = clean_wtlive_nickname(m.group(1).strip())
        if nick:
            return nick
    # 3. title 回退
    m = _WEBSITE_TITLE_RE.search(html)
    if m:
        title = m.group(1).strip()
        # 排除挑战页/无意义标题
        _BAD_TITLES = ("请稍候", "war thunder", "战争雷霆", "error", "404",
                       "just a moment", "attention required")
        tl = title.lower()
        if any(b in tl for b in _BAD_TITLES):
            return None
        # 形如 "昵称 - War Thunder" 或 "玩家昵称 | 战争雷霆"
        for sep in (" - ", " | ", " – "):
            if sep in title:
                nick = title.split(sep)[0].strip()
                nick = clean_wtlive_nickname(nick)
                if nick and not nick.lower() in ("war thunder", "战争雷霆", "error", "404"):
                    return nick
        nick = clean_wtlive_nickname(title)
        if nick and not nick.lower() in ("war thunder", "战争雷霆"):
            return nick
    return None


def fetch_website_profile(player_id: str, timeout: float = 10.0) -> tuple[str | None, int]:
    """访问官网 userinfo 页面获取昵称(备选源)。

    返回 (昵称或None, HTTP状态码)。403 = 被 Cloudflare 挑战拦截(临时失败);
    404 = ID 无效。
    """
    pid = (player_id or "").strip()
    if not pid.isdigit():
        return None, 400
    url = WEBSITE_USERINFO_TEMPLATE.format(player_id=pid)
    try:
        resp = _SESSION.get(url, timeout=timeout)
        if resp.status_code != 200:
            log.info("website userinfo %s -> HTTP %s", pid, resp.status_code)
            return None, resp.status_code
        nick = _parse_website_nickname(resp.text)
        if nick:
            log.info("website userinfo %s -> nickname %s", pid, nick)
            return nick, resp.status_code
        log.warning("website userinfo %s: 页面无昵称(疑似被挑战/结构变化)", pid)
        return None, resp.status_code
    except requests.RequestException as exc:
        log.warning("website userinfo %s fetch failed: %s", pid, exc)
        return None, 0


def fetch_profile_best_effort(player_id: str,
                              timeout: float = 8.0,
                              retry_website: int = 2) -> tuple[str | None, int]:
    """尽力获取昵称: 主源 WTLive → 404/失败时重试官网 userinfo。

    返回 (昵称或None, 状态码)。状态码语义沿用主源(404=无效ID)。
    """
    nick, status = fetch_profile_with_status(player_id, timeout)
    if nick:
        return nick, status
    if status not in (0, 404, 403):
        # 主源网络异常/其他错误, 官网未必更好, 但仍试一次
        pass
    # WTLive 没有(404)或失败 → 尝试官网
    for attempt in range(retry_website):
        wn, ws = fetch_website_profile(player_id, timeout)
        if wn:
            return wn, 200
        if ws == 404:
            break  # 官网也 404 → 无效 ID
        if ws != 403:
            break  # 非挑战类失败(网络/5xx), 重试意义不大
        time.sleep(0.8 * (attempt + 1))  # 挑战失败 → 退避重试
    return nick, status


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
