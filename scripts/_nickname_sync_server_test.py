# -*- coding: utf-8 -*-
"""服务端脚本本地测试(不真正调用 GitHub API)。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import nickname_sync_server as srv  # noqa: E402

# ---- validate ----
assert srv.validate("123456789", "Alice_01")
assert srv.validate("0", "中文昵称")
assert not srv.validate("abc", "x")               # uid 非数字
assert not srv.validate("12345678901234567", "x")  # uid 17 位
assert not srv.validate("123", "a" * 33)          # 昵称超 32
assert not srv.validate("123", "!!bad!!")         # 昵称含非法字符
print("validate OK")

# ---- process_issue ----
calls = []


def fake_api(method, path, payload=None):
    calls.append((method, path, payload))
    return {}


srv.api = fake_api
nicks = {}
ok, bad = srv.process_issue({
    "number": 5,
    "title": "[nickname-sync] x",
    "body": json.dumps({"type": "nickname_sync", "entries": [
        {"uid": "100000000", "nickname": "Alice"},
        {"uid": "bad", "nickname": "!!!"},
        {"uid": "100000001", "nickname": " 空白昵称 "},
    ]}),
}, nicks)
assert ok == 2, f"ok={ok}"          # Alice + strip 后的"空白昵称"均合法
assert bad == 1, f"bad={bad}"       # "!!!" 非法
assert nicks["100000000"]["nickname"] == "Alice"
assert nicks["100000000"]["ts"] > 0
assert "100000001" in nicks          # strip 后合法
# 评论 + 关闭 issue
assert any(p and p.get("state") == "closed" for _, _, p in calls)
assert any(p and "已合并" in p.get("body", "") for _, _, p in calls)
print("process_issue OK")

# ---- 无效 JSON body ----
calls.clear()
ok, bad = srv.process_issue({"number": 6, "body": "not-json"}, nicks)
assert ok == 0 and bad == 0
assert any(p and p.get("state") == "closed" for _, _, p in calls)
print("bad-json issue OK")

print("NICKNAME SYNC SERVER TEST PASSED")
