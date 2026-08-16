"""审核请求同步: 用户提交审核请求(issue) + 审核员拉取/完成待审核条目。

待审核队列存于仓库根目录 review_pending.json:
{
  "version": 1,
  "updated_at": <epoch>,
  "items": [
    {
      "id": "<uuid>",
      "submitter": "<github用户名>",
      "submitted_at": <epoch>,
      "nickname": "", "player_id": "", "reason": "", "event_date": "",
      "replay_link": "", "remarks": "",
      "previous_nicknames": [],
      "status": "pending" | "checking",   # pending=待审, checking=已被某审核员拉取
      "checkout_by": "", "checkout_at": 0
    }
  ]
}

流程:
- 普通用户: 通过 GitHub issue([review-request] 前缀)提交待审核条目文本
  (仅文本字段, 不含证据文件), 由服务端(Actions 定时)解析后追加进 review_pending.json。
- 审核员: 通过 API 直接读写 review_pending.json: 拉取一条 pending
  (乐观锁打标 checking, 防止多人拉同一条) → 审核 → 上传 blacklist.json
  (已有 upload_entries) → complete_review 删除该条待审核请求。
"""
from __future__ import annotations

import base64
import json
import re
import time

import requests

DEFAULT_BRANCH = "main"
TIMEOUT = 15
UA = "WTBlackList/2.0.1"
MAX_RETRIES = 3          # 乐观锁冲突自动重试上限
RETRY_DELAY = 0.6        # 重试间隔(秒)

REVIEW_FILE = "review_pending.json"
# issue 标题约定(服务端据此识别审核请求)
ISSUE_PREFIX = "[review-request]"

_UID_RE = re.compile(r"^\d{1,16}$")
_NICK_RE = re.compile(r"^[\w\-\s#]{1,32}$", re.UNICODE)


# ----------------------------------------------------------------------
# URL / 数据
# ----------------------------------------------------------------------
def review_json_url(repo_url: str) -> str | None:
    """公开仓库 review_pending.json 的 raw 地址(GitHub/Gitee)。"""
    from .server_sync import parse_repo_url
    p = parse_repo_url(repo_url)
    if not p:
        return None
    plat, owner, repo = p
    if plat == "github":
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{DEFAULT_BRANCH}/{REVIEW_FILE}"
    return f"https://gitee.com/{owner}/{repo}/raw/{DEFAULT_BRANCH}/{REVIEW_FILE}"


def validate_entry(e: dict) -> bool:
    """待审核条目的基本校验(player_id 纯数字 / 昵称合法 / 原因非空)。"""
    pid = str(e.get("player_id") or "").strip()
    nick = str(e.get("nickname") or "").strip()
    reason = str(e.get("reason") or "").strip()
    return bool(_UID_RE.match(pid) and _NICK_RE.match(nick) and reason)


def _item_from_entry(e: dict, submitter: str, ts: float) -> dict:
    """把本地条目转成待审核 json 条目(仅文本字段, 不含证据文件)。"""
    import uuid
    return {
        "id": str(uuid.uuid4()),
        "submitter": submitter,
        "submitted_at": int(ts),
        "nickname": str(e.get("nickname") or "").strip(),
        "player_id": str(e.get("player_id") or "").strip(),
        "reason": str(e.get("reason") or "").strip(),
        "event_date": str(e.get("event_date") or "").strip(),
        "replay_link": str(e.get("replay_link") or "").strip(),
        "remarks": str(e.get("remarks") or "").strip(),
        "previous_nicknames": [str(x) for x in (e.get("previous_nicknames") or [])],
        "status": "pending",
        "checkout_by": "",
        "checkout_at": 0,
    }


# ----------------------------------------------------------------------
# 拉取(公开, 免鉴权)
# ----------------------------------------------------------------------
def fetch_pending(repo_url: str) -> list[dict]:
    """公开拉取待审核条目列表。文件不存在/失败返回 [] (不抛异常)。"""
    url = review_json_url(repo_url)
    if not url:
        return []
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA})
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except ValueError:
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return [d for d in items if isinstance(d, dict)] if isinstance(items, list) else []


# ----------------------------------------------------------------------
# 用户提交审核请求(issue)
# ----------------------------------------------------------------------
def submit_review_request(repo_url: str, token: str,
                          entries: list[dict], submitter: str) -> tuple[int, str]:
    """通过 GitHub issue 提交审核请求(服务端解析后追加进 review_pending.json)。

    返回 (issue 编号, issue 网页地址)。失败抛 ValueError。
    """
    from .server_sync import parse_repo_url
    p = parse_repo_url(repo_url)
    if not p:
        raise ValueError("不支持的仓库地址(仅支持 GitHub / Gitee)")
    plat, owner, repo = p
    if plat != "github":
        raise ValueError("审核请求目前仅支持 GitHub 仓库")
    token = (token or "").strip()
    if not token:
        raise ValueError("未登录 GitHub, 无法提交审核请求")
    ok = [e for e in entries if validate_entry(e)]
    if not ok:
        raise ValueError("没有可提交的合法条目(需玩家ID/昵称/原因)")
    body = json.dumps({
        "type": "review_request",
        "submitter": submitter,
        "entries": ok,
        "ts": int(time.time()),
    }, ensure_ascii=False)
    payload = {"title": f"{ISSUE_PREFIX} 客户端提交审核请求", "body": body}
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


# ----------------------------------------------------------------------
# GitHub API 读写 review_pending.json(需鉴权, 带乐观锁)
# ----------------------------------------------------------------------
def _api_read(url: str, token: str) -> tuple[list[dict], str | None]:
    """通过 API 读取 review_pending.json, 返回 (items, sha)。不存在返回 ([], None)。"""
    from .server_sync import parse_repo_url
    p = parse_repo_url(url)
    if not p:
        raise ValueError("不支持的仓库地址")
    plat, owner, repo = p
    if plat != "github":
        raise ValueError("审核拉取/完成目前仅支持 GitHub 仓库")
    r = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{REVIEW_FILE}",
        headers={"User-Agent": UA, "Accept": "application/vnd.github+json",
                 "Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    if r.status_code == 404:
        return [], None
    r.raise_for_status()
    j = r.json()
    content = base64.b64decode(j.get("content") or "").decode("utf-8", "replace")
    try:
        data = json.loads(content)
    except ValueError:
        data = {}
    items = data.get("items") if isinstance(data, dict) else []
    return ([d for d in items if isinstance(d, dict)] if isinstance(items, list) else []), \
        j.get("sha")


def _api_write(url: str, token: str, items: list[dict], sha: str | None,
               message: str) -> None:
    """通过 API 写回 review_pending.json。"""
    from .server_sync import parse_repo_url
    p = parse_repo_url(url)
    plat, owner, repo = p
    data = json.dumps({
        "version": 1,
        "updated_at": int(time.time()),
        "items": items,
    }, ensure_ascii=False, indent=2)
    body = {
        "message": message,
        "content": base64.b64encode(data.encode()).decode(),
        "branch": DEFAULT_BRANCH,
    }
    if sha:
        body["sha"] = sha
    r = requests.put(
        f"https://api.github.com/repos/{owner}/{repo}/contents/{REVIEW_FILE}",
        headers={"User-Agent": UA, "Accept": "application/vnd.github+json",
                 "Authorization": f"Bearer {token}"},
        json=body,
        timeout=TIMEOUT,
    )
    if r.status_code not in (200, 201):
        raise ValueError(f"写入待审核文件失败 (HTTP {r.status_code}): {r.text[:200]}")


def _read_write_with_retry(url: str, token: str, mutate) -> dict | None:
    """乐观锁读-改-写重试。mutate(items) 返回 (result, new_items);
    new_items 为 None 表示无变化不写回。"""
    for attempt in range(MAX_RETRIES + 1):
        items, sha = _api_read(url, token)
        result, new_items = mutate(items)
        if new_items is None:
            # 无变化, 不写回(避免把 None 写入文件)
            return result
        try:
            _api_write(url, token, new_items, sha, "审核请求: 更新待审核队列")
            return result
        except Exception as exc:  # noqa: BLE001
            if attempt >= MAX_RETRIES or "409" not in str(exc):
                raise
            time.sleep(RETRY_DELAY)
    return None


# ----------------------------------------------------------------------
# 审核员: 拉取 / 完成
# ----------------------------------------------------------------------
def pull_next_review(url: str, token: str, auditor: str) -> dict | None:
    """拉取一条待审核条目并打标 checking(防止多人拉同一条)。

    返回条目 dict; 没有待审条目返回 None。冲突自动重试。
    """
    token = (token or "").strip()
    if not token:
        raise ValueError("未登录, 无法拉取审核请求")
    if not (auditor or "").strip():
        raise ValueError("未选择审核员昵称")

    def mutate(items: list[dict]):
        for item in items:
            if item.get("status") == "pending":
                item["status"] = "checking"
                item["checkout_by"] = auditor
                item["checkout_at"] = int(time.time())
                return item, items
        return None, None  # 无待审条目, 不写回

    return _read_write_with_retry(url, token, mutate)


def complete_review(url: str, token: str, item_id: str) -> bool:
    """审核完毕, 从待审核队列移除该条目(幂等: 条目不存在也视为成功)。"""
    token = (token or "").strip()
    if not token:
        raise ValueError("未登录, 无法删除待审核请求")
    if not item_id:
        return True

    def mutate(items: list[dict]):
        new_items = [i for i in items if str(i.get("id") or "") != item_id]
        if len(new_items) == len(items):
            return True, None  # 已不存在, 视为成功且不写回
        return True, new_items

    _read_write_with_retry(url, token, mutate)
    return True
