"""共享昵称表(nickname.json)同步: 拉取合并 + issue 上传。

共享表格式(公开仓库根目录 nickname.json):
{
  "version": 1,
  "updated_at": <epoch>,
  "nicknames": { "<uid>": {"nickname": "...", "ts": <epoch>} }
}
- 拉取: 公开仓库 raw, 免鉴权, 合并进本地 NicknameCache(缺失补上, 取 ts 更新者)。
- 上传: 通过 GitHub issue 提交请求(正文为 JSON), 由服务端(Actions/常驻脚本)
  校验后合并进共享表。客户端不直接写仓库, 降低权限面。
"""
from __future__ import annotations

import json
import re
import time

import requests

DEFAULT_BRANCH = "main"
TIMEOUT = 15
UA = "WTBlackList/2.0.0"

_NICKNAME_MAX = 32
_UID_RE = re.compile(r"^\d{1,16}$")
_NICK_RE = re.compile(r"^[\w\-\s#]{1,32}$", re.UNICODE)

# issue 标题约定(服务端据此识别)
ISSUE_TITLE = "[nickname-sync] 客户端提交昵称更新"


def shared_json_url(repo_url: str) -> str | None:
    """公开仓库 nickname.json 的 raw 地址(GitHub/Gitee)。"""
    from .server_sync import parse_repo_url
    p = parse_repo_url(repo_url)
    if not p:
        return None
    plat, owner, repo = p
    if plat == "github":
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{DEFAULT_BRANCH}/nickname.json"
    return f"https://gitee.com/{owner}/{repo}/raw/{DEFAULT_BRANCH}/nickname.json"


def fetch_shared_table(repo_url: str) -> dict[str, dict]:
    """拉取共享表, 返回 {uid: {"nickname": str, "ts": float}}。

    文件不存在/格式错误/网络失败均返回 {} (不抛异常)。
    """
    url = shared_json_url(repo_url)
    if not url:
        return {}
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA})
    except requests.RequestException:
        return {}
    if r.status_code != 200:
        return {}
    try:
        data = r.json()
    except ValueError:
        return {}
    nicks = data.get("nicknames") if isinstance(data, dict) else None
    if not isinstance(nicks, dict):
        return {}
    out: dict[str, dict] = {}
    for uid, v in nicks.items():
        if not isinstance(v, dict):
            continue
        nick = str(v.get("nickname") or "").strip()
        try:
            ts = float(v.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if uid and nick:
            out[str(uid)] = {"nickname": nick, "ts": ts}
    return out


def merge_shared_into_cache(remote: dict[str, dict], cache) -> tuple[int, int]:
    """把共享表合并进本地缓存(缺失补上, 取 ts 更新者)。返回 (新增, 更新)。"""
    added = 0
    updated = 0
    for uid, rec in remote.items():
        local = cache.get(uid)
        if local is None or not local.get("nickname"):
            cache.set(uid, rec["nickname"], rec["ts"], save=False)
            added += 1
        elif (rec.get("ts") or 0) > (local.get("fetched_at") or 0):
            cache.set(uid, rec["nickname"], rec["ts"], save=False)
            updated += 1
    cache.save()
    return added, updated


def collect_pending(remote: dict[str, dict], cache) -> list[dict]:
    """对比共享表, 收集待上传: 本地有昵称、共享表缺失或本地更新者。"""
    pending: list[dict] = []
    for uid, nick, ts, invalid in cache.items():
        if invalid or not nick:
            continue
        r = remote.get(uid)
        if r is None or ts > (r.get("ts") or 0):
            pending.append({"uid": uid, "nickname": nick, "ts": ts})
    pending.sort(key=lambda e: e.get("ts") or 0, reverse=True)
    return pending


def validate_entries(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """校验条目(uid 纯数字 1-16 位 / 昵称合法字符 ≤32)。返回 (合法, 非法)。"""
    ok: list[dict] = []
    bad: list[dict] = []
    for e in entries:
        uid = str(e.get("uid") or "").strip()
        nick = str(e.get("nickname") or "").strip()
        if _UID_RE.match(uid) and _NICK_RE.match(nick):
            ok.append({"uid": uid, "nickname": nick})
        else:
            bad.append({"uid": uid, "nickname": nick})
    return ok, bad


def submit_issue(repo_url: str, token: str, entries: list[dict]) -> tuple[int, str]:
    """通过 GitHub issue 提交昵称更新请求(服务端处理后合并进共享表)。

    返回 (issue 编号, issue 网页地址)。失败抛 ValueError。
    """
    from .server_sync import parse_repo_url
    p = parse_repo_url(repo_url)
    if not p:
        raise ValueError("不支持的仓库地址(仅支持 GitHub / Gitee)")
    plat, owner, repo = p
    if plat != "github":
        raise ValueError("共享表上传目前仅支持 GitHub 仓库")
    token = (token or "").strip()
    if not token:
        raise ValueError("未登录 GitHub, 无法上传共享昵称")
    ok, bad = validate_entries(entries)
    if not ok:
        raise ValueError("没有可上传的合法昵称条目")
    body = json.dumps({
        "type": "nickname_sync",
        "entries": ok,
        "client": "WTBlackList",
        "ts": int(time.time()),
    }, ensure_ascii=False)
    payload = {"title": ISSUE_TITLE, "body": body}
    r = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        headers={
            "User-Agent": UA,
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
        },
        json=payload,
        timeout=TIMEOUT,
    )
    if r.status_code not in (200, 201):
        raise ValueError(f"创建 issue 失败 (HTTP {r.status_code}): {r.text[:200]}")
    j = r.json()
    return int(j.get("number") or 0), str(j.get("html_url") or "")


def find_github_token(settings) -> str:
    """从已登录的 GitHub 审核服务器里取第一个 token(用于发 issue)。"""
    for s in getattr(settings, "audit_servers", []) or []:
        if s.get("logged_in") and s.get("token"):
            return str(s.get("token") or "").strip()
    return ""
