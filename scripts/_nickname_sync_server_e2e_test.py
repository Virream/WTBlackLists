# -*- coding: utf-8 -*-
"""服务端 main() 全流程端到端模拟测试(monkeypatch api, 不访问真实 GitHub)。

覆盖云端 Actions 将执行的完整逻辑:
列出 open issues → 处理 [nickname-sync] issue → 校验合并 → 写回 nickname.json
→ 评论并关闭 issue; 同时校验 workflow 的 cron 表达式合法性。
"""
import base64
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import nickname_sync_server as srv  # noqa: E402

# 模拟云端已存在的 nickname.json
EXISTING = {"version": 1, "updated_at": 100,
            "nicknames": {"100": {"nickname": "旧昵称", "ts": 50}}}

# 模拟当前 open 的 issues(处理后会从列表移除, 模拟被关闭)
OPEN_ISSUES = [
    {"number": 1, "title": "[nickname-sync] 客户端提交",
     "body": json.dumps({"type": "nickname_sync", "entries": [
         {"uid": "100", "nickname": "新昵称"},   # 已有 → 更新
         {"uid": "200", "nickname": "新增玩家"},  # 新增
         {"uid": "bad", "nickname": "!!"},        # 非法 → 忽略
     ]})},
    {"number": 2, "title": "无关 issue", "body": "x"},  # 不处理
    {"number": 3, "title": "[nickname-sync] 坏JSON", "body": "not-json"},
]

calls: list = []  # (method, path, payload)


def fake_api(method: str, path: str, payload=None):
    calls.append((method, path, payload))
    if method == "GET" and path.endswith("/contents/nickname.json"):
        return {"content": base64.b64encode(
            json.dumps(EXISTING).encode()).decode(), "sha": "sha-abc"}
    if method == "GET" and "issues" in path:
        return list(OPEN_ISSUES)
    if method == "PATCH" and payload and payload.get("state") == "closed":
        # 模拟 issue 被关闭: 从 open 列表移除
        num = path.rsplit("/", 1)[-1]
        for i, iss in enumerate(OPEN_ISSUES):
            if str(iss.get("number")) == num:
                OPEN_ISSUES.pop(i)
                break
    return {}


def main() -> int:
    srv.api = fake_api
    srv.REPO = "Virream/WTBlackListsData"
    srv.TOKEN = "tok"
    srv.BRANCH = "main"

    rc = srv.main()
    assert rc == 0, f"main 应返回 0, 实际 {rc}"

    # 1) 写回 nickname.json 内容正确
    puts = [pl for m, p, pl in calls if m == "PUT" and pl]
    assert puts, "应写回 nickname.json"
    data = json.loads(base64.b64decode(puts[0]["content"]).decode())
    nicks = data["nicknames"]
    assert nicks["100"]["nickname"] == "新昵称", nicks["100"]
    assert nicks["200"]["nickname"] == "新增玩家", nicks.get("200")
    assert "bad" not in nicks, "非法条目应被忽略"
    assert data["version"] == 1 and "updated_at" in data

    # 2) 合法 issue 被评论 + 关闭; 坏 JSON 的 issue 也被关闭(不合并)
    closed = [p for m, p, pl in calls
              if m == "PATCH" and pl and pl.get("state") == "closed"]
    assert len(closed) == 2, f"应关闭2个issue([nickname-sync]), 实际 {closed}"
    comments = [pl for m, p, pl in calls if m == "POST" and "/comments" in p and pl]
    assert len(comments) == 2, f"应评论2次, 实际 {comments}"
    merged_comment = [c for c in comments if "已合并" in c["body"]]
    assert merged_comment and "已合并 2 条" in merged_comment[0]["body"], comments

    # 3) 无关 issue 不受影响
    assert not any("无关" in str(p) for _, p, _ in calls)

    # 4) 幂等性: 再跑一次(无新提交)不应重复写回
    calls.clear()
    rc2 = srv.main()
    assert rc2 == 0
    puts2 = [p for m, p, pl in calls if m == "PUT"]
    assert not puts2, "无新数据时不应重复写回 nickname.json"

    print("NICKNAME SYNC SERVER E2E PASSED")
    return 0


def check_cron() -> None:
    """校验 workflow 的 cron 表达式: 5 字段, 字段值域合法。"""
    wf = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), ".github", "workflows",
        "nickname-sync.yml"), encoding="utf-8").read()
    m = re.search(r'cron:\s*"([^"]+)"', wf)
    assert m, "workflow 应包含 cron 表达式"
    cron = m.group(1)
    fields = cron.split()
    assert len(fields) == 5, f"cron 应为 5 字段: {cron}"
    mins, hours, dom, mon, dow = fields
    assert all(int(x) in range(0, 60) for x in mins.split(",")), mins
    assert all(int(x) in range(0, 24) for x in hours.split(",")), hours
    # 每 6 小时: 小时字段应为 0,6,12,18
    assert sorted(int(x) for x in hours.split(",")) == [0, 6, 12, 18], hours
    print(f"cron 校验 OK: {cron}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or check_cron())
