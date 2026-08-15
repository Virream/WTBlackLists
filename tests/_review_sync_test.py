# -*- coding: utf-8 -*-
"""审核请求同步(review_sync)纯逻辑验证(不联网, 模拟 API 读写)。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wt81111g import review_sync as rs

# ---- review_json_url ----
assert rs.review_json_url("https://github.com/Virream/WTBlackLists") == (
    "https://raw.githubusercontent.com/Virream/WTBlackLists/main/review_pending.json")
assert rs.review_json_url("https://example.com/x") is None

# ---- validate_entry ----
assert rs.validate_entry({"player_id": "123", "nickname": "abc", "reason": "x"})
assert not rs.validate_entry({"player_id": "abc", "nickname": "x", "reason": "y"})
assert not rs.validate_entry({"player_id": "1", "nickname": "", "reason": "y"})
assert not rs.validate_entry({"player_id": "1", "nickname": "x", "reason": ""})

# ---- _item_from_entry: 仅文本字段, 不含证据文件 ----
item = rs._item_from_entry({
    "player_id": "123", "nickname": "abc", "reason": "x",
    "event_date": "2024-01-01", "replay_link": "https://r", "remarks": "m",
    "previous_nicknames": ["old"],
}, "Alice", 1000)
assert item["id"] and item["submitter"] == "Alice"
assert item["status"] == "pending" and item["checkout_by"] == ""
assert "evidence" not in item and "folder" not in item
for key in ("nickname", "player_id", "reason", "event_date", "replay_link", "remarks"):
    assert key in item, key

# ---- 模拟 API 读写, 测试 pull_next_review / complete_review ----
state = [
    {"id": "a1", "status": "pending", "checkout_by": ""},
    {"id": "a2", "status": "pending", "checkout_by": ""},
]
writes = []


def fake_read(url, token):
    return [dict(i) for i in state], "sha1"


def fake_write(url, token, items, sha, message):
    state[:] = items
    writes.append((list(items), sha, message))


rs._api_read = fake_read
rs._api_write = fake_write

item1 = rs.pull_next_review("https://github.com/u/r", "tok", "Bob")
assert item1["id"] == "a1", "应取第一条 pending"
assert item1["status"] == "checking" and item1["checkout_by"] == "Bob"
assert state[0]["status"] == "checking", "应写回标记"
assert state[0]["checkout_by"] == "Bob"

item2 = rs.pull_next_review("https://github.com/u/r", "tok", "Bob")
assert item2["id"] == "a2", "已被拉取的应跳过"

# 无 pending → 返回 None 且不写回
writes.clear()
state[:] = [{"id": "a1", "status": "checking", "checkout_by": "Bob"}]
assert rs.pull_next_review("https://github.com/u/r", "tok", "Bob") is None
assert writes == [], "无 pending 不应写回"

# complete_review: 移除指定 id
state[:] = [
    {"id": "a1", "status": "checking", "checkout_by": "Bob"},
    {"id": "a2", "status": "pending"},
]
writes.clear()
assert rs.complete_review("https://github.com/u/r", "tok", "a1") is True
saved = writes[-1][0]
assert all(i["id"] != "a1" for i in saved), "应移除该待审核请求"
assert len(saved) == 1

# complete_review: 条目不存在 → 视为成功且不写回(不得写 None)
writes.clear()
assert rs.complete_review("https://github.com/u/r", "tok", "ghost") is True
assert writes == [], "条目不存在不应写回"

# 空 token 应抛错
try:
    rs.pull_next_review("https://github.com/u/r", "", "Bob")
    raise AssertionError("空 token 应抛 ValueError")
except ValueError:
    pass

print("REVIEW SYNC TEST PASSED")
