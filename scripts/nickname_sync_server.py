"""服务端: 处理 [nickname-sync] issue, 把合法昵称合并进共享表 nickname.json。

由 GitHub Actions 定时调度运行(见 .github/workflows/nickname-sync.yml)。
逻辑:
1. 列出仓库所有 open issue, 找出标题以 [nickname-sync] 开头的请求。
2. 解析正文 JSON({type: nickname_sync, entries: [{uid, nickname}]})。
3. 严格校验(uid 纯数字 1-16 位 / 昵称合法字符 ≤32), 非法条目丢弃。
4. 合并进现有 nickname.json(ts 取当前时间, 覆盖旧值)。
5. 写回仓库, 给 issue 打处理结果评论, 并关闭 issue。

攻击防护: 即使收到垃圾 issue, 非法数据在校验阶段被丢弃, 仓库不会污染;
issue 关闭后可追溯, GitHub 对垃圾 issue 也有风控。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.request

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
FILE = "nickname.json"
BRANCH = os.environ.get("GITHUB_REF_NAME", "main").rsplit("/", 1)[-1]

ISSUE_PREFIX = "[nickname-sync]"
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


def get_table() -> tuple[dict, str | None]:
    """读取现有 nickname.json, 返回 (数据dict, sha)。文件不存在返回 ({}, None)。"""
    try:
        j = api("GET", f"/repos/{REPO}/contents/{FILE}")
        if isinstance(j, dict) and j.get("content"):
            text = base64.b64decode(j["content"]).decode("utf-8", "replace")
            try:
                return json.loads(text) if text else {}, j.get("sha")
            except ValueError:
                return {}, j.get("sha")
    except Exception:  # noqa: BLE001
        pass
    return {}, None


def write_table(data: dict, sha: str | None) -> None:
    body = {
        "message": "nickname.json 同步: 合并 issue 提交的昵称",
        "content": base64.b64encode(json.dumps(
            data, ensure_ascii=False, indent=2).encode()).decode(),
        "branch": BRANCH,
    }
    if sha:
        body["sha"] = sha
    api("PUT", f"/repos/{REPO}/contents/{FILE}", body)


def validate(uid: str, nick: str) -> bool:
    return bool(_UID_RE.match(uid) and _NICK_RE.match(nick))


def process_issue(issue: dict, nicks: dict) -> tuple[int, int]:
    """处理一个 issue, 返回 (合并条数, 忽略条数)。"""
    body = issue.get("body") or ""
    num = int(issue.get("number") or 0)
    try:
        req = json.loads(body)
    except ValueError:
        api("POST", f"/repos/{REPO}/issues/{num}/comments",
            {"body": "❌ 无效的请求格式(正文不是 JSON), 已关闭。"})
        api("PATCH", f"/repos/{REPO}/issues/{num}", {"state": "closed"})
        return 0, 0
    if req.get("type") != "nickname_sync":
        return 0, 0
    ok = 0
    bad = 0
    for e in req.get("entries") or []:
        uid = str(e.get("uid") or "").strip()
        nick = str(e.get("nickname") or "").strip()
        if validate(uid, nick):
            nicks[uid] = {"nickname": nick, "ts": int(time.time())}
            ok += 1
        else:
            bad += 1
    api("POST", f"/repos/{REPO}/issues/{num}/comments",
        {"body": f"✅ 已合并 {ok} 条, 忽略 {bad} 条非法数据。"})
    api("PATCH", f"/repos/{REPO}/issues/{num}", {"state": "closed"})
    return ok, bad


def main() -> int:
    if not REPO or not TOKEN:
        print("缺少 GITHUB_REPOSITORY / GITHUB_TOKEN 环境变量", file=sys.stderr)
        return 1
    data, sha = get_table()
    nicks = data.get("nicknames") if isinstance(data, dict) else {}
    if not isinstance(nicks, dict):
        nicks = {}
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
        ok, bad = process_issue(issue, nicks)
        total_ok += ok
        total_bad += bad
        if ok:
            changed = True
    if changed:
        write_table({
            "version": 1,
            "updated_at": int(time.time()),
            "nicknames": nicks,
        }, sha)
    print(f"processed: merged={total_ok} ignored={total_bad}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
