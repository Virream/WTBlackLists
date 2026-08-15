"""服务端: 处理 [review-request] issue, 把待审核条目追加进 review_pending.json。

由 GitHub Actions 定时调度运行(见 .github/workflows/review-sync.yml)。
逻辑:
1. 列出仓库所有 open issue, 找出标题以 [review-request] 开头的请求。
2. 解析正文 JSON({type: review_request, submitter, entries: [...]})。
3. 严格校验(player_id 纯数字 1-16 位 / 昵称合法字符 ≤32 / 原因非空),
   非法条目丢弃。
4. 为每条合法条目生成待审核 item(uuid, submitter, submitted_at, status=pending),
   追加进现有 review_pending.json(重复 player_id 仍保留, 由审核员人工判断)。
5. 写回仓库, 给 issue 打处理结果评论, 并关闭 issue。

仅接收文本字段, 不含证据文件(客户端提交时已排除)。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
import urllib.request

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
FILE = "review_pending.json"
BRANCH = os.environ.get("GITHUB_REF_NAME", "main").rsplit("/", 1)[-1]

ISSUE_PREFIX = "[review-request]"
_UID_RE = re.compile(r"^\d{1,16}$")
_NICK_RE = re.compile(r"^[\w\-\s#]{1,32}$", re.UNICODE)


def api(method: str, path: str, payload: dict | None = None) -> dict | list:
    req = urllib.request.Request(f"{API}{path}", method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    data = None
    if payload is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(payload).encode()
    with urllib.request.urlopen(req, data=data) as r:
        return json.loads(r.read())


def get_items() -> tuple[list[dict], str | None]:
    """读取现有 review_pending.json, 返回 (items, sha)。文件不存在返回 ([], None)。"""
    try:
        j = api("GET", f"/repos/{REPO}/contents/{FILE}")
        if isinstance(j, dict) and j.get("content"):
            text = base64.b64decode(j["content"]).decode("utf-8", "replace")
            try:
                data = json.loads(text) if text else {}
            except ValueError:
                data = {}
            items = data.get("items") if isinstance(data, dict) else []
            if not isinstance(items, list):
                items = []
            return [d for d in items if isinstance(d, dict)], j.get("sha")
    except Exception:  # noqa: BLE001
        pass
    return [], None


def write_items(items: list[dict], sha: str | None) -> None:
    body = {
        "message": "review_pending.json 同步: 加入 issue 提交的审核请求",
        "content": base64.b64encode(json.dumps({
            "version": 1,
            "updated_at": int(time.time()),
            "items": items,
        }, ensure_ascii=False, indent=2).encode()).decode(),
        "branch": BRANCH,
    }
    if sha:
        body["sha"] = sha
    api("PUT", f"/repos/{REPO}/contents/{FILE}", body)


def validate(pid: str, nick: str, reason: str) -> bool:
    return bool(_UID_RE.match(pid) and _NICK_RE.match(nick) and reason.strip())


def process_issue(issue: dict, items: list[dict]) -> tuple[int, int]:
    """处理一个审核请求 issue, 返回 (加入条数, 忽略条数)。"""
    body = issue.get("body") or ""
    num = int(issue.get("number") or 0)
    submitter = str((issue.get("user") or {}).get("login") or "unknown")
    try:
        req = json.loads(body)
    except ValueError:
        api("POST", f"/repos/{REPO}/issues/{num}/comments",
            {"body": "❌ 无效的请求格式(正文不是 JSON), 已关闭。"})
        api("PATCH", f"/repos/{REPO}/issues/{num}", {"state": "closed"})
        return 0, 0
    if req.get("type") != "review_request":
        return 0, 0
    ok = 0
    bad = 0
    now = int(time.time())
    for e in req.get("entries") or []:
        pid = str(e.get("player_id") or "").strip()
        nick = str(e.get("nickname") or "").strip()
        reason = str(e.get("reason") or "").strip()
        if validate(pid, nick, reason):
            items.append({
                "id": str(uuid.uuid4()),
                "submitter": submitter or str(req.get("submitter") or ""),
                "submitted_at": now,
                "nickname": nick,
                "player_id": pid,
                "reason": reason,
                "event_date": str(e.get("event_date") or "").strip(),
                "replay_link": str(e.get("replay_link") or "").strip(),
                "remarks": str(e.get("remarks") or "").strip(),
                "previous_nicknames": [
                    str(x) for x in (e.get("previous_nicknames") or [])
                ],
                "status": "pending",
                "checkout_by": "",
                "checkout_at": 0,
            })
            ok += 1
        else:
            bad += 1
    api("POST", f"/repos/{REPO}/issues/{num}/comments",
        {"body": f"✅ 已加入待审核队列 {ok} 条, 忽略 {bad} 条非法数据。"})
    api("PATCH", f"/repos/{REPO}/issues/{num}", {"state": "closed"})
    return ok, bad


def main() -> int:
    import sys
    if not REPO or not TOKEN:
        print("缺少 GITHUB_REPOSITORY / GITHUB_TOKEN 环境变量", file=sys.stderr)
        return 1
    items, sha = get_items()
    changed = False
    total_ok = 0
    total_bad = 0
    try:
        issues = api("GET", f"/repos/{REPO}/issues?state=open&per_page=100")
    except Exception as exc:  # noqa: BLE001
        print(f"获取 issues 失败: {exc}", file=sys.stderr)
        return 1
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        if not str(issue.get("title") or "").startswith(ISSUE_PREFIX):
            continue
        ok, bad = process_issue(issue, items)
        total_ok += ok
        total_bad += bad
        if ok:
            changed = True
    if changed:
        write_items(items, sha)
    print(f"processed: added={total_ok} ignored={total_bad}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
