# -*- coding: utf-8 -*-
"""共享昵称表同步逻辑测试(不发起真实网络请求)。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wt81111g.nickname_cache import NicknameCache
from wt81111g.nickname_sync import (
    collect_pending, merge_shared_into_cache, shared_json_url,
    submit_issue, validate_entries,
)

results: list[str] = []


def check(name: str, cond: bool, *extra) -> None:
    if cond:
        results.append(f"OK: {name}")
    else:
        results.append(f"FAIL: {name} {extra}")
        raise AssertionError(name)


# 1. raw URL 解析
u = shared_json_url("https://github.com/Virream/WTBlackLists")
check("github raw 地址",
      u == "https://raw.githubusercontent.com/Virream/WTBlackLists/main/nickname.json")
check("不支持地址返回 None", shared_json_url("https://example.com/x") is None)

# 2. 合并进本地缓存
d = tempfile.mkdtemp()
cache = NicknameCache(os.path.join(d, "nc.json"))
cache.set("111", "本地昵称", 200, save=False)      # 本地已有(旧 ts)
cache.set("222", "本地222", 300, save=False)      # 本地无对应远端
cache.save()
remote = {
    "111": {"nickname": "远端昵称", "ts": 500},     # 远端 ts 更新 → 覆盖
    "333": {"nickname": "远端333", "ts": 400},     # 本地无 → 新增
}
added, updated = merge_shared_into_cache(remote, cache)
check("新增 1 条(333)", added == 1, (added, updated))
check("更新 1 条(111)", updated == 1)
check("111 用远端", cache.get("111")["nickname"] == "远端昵称")
check("333 加入", cache.get("333")["nickname"] == "远端333")
check("222 保留本地", cache.get("222")["nickname"] == "本地222")

# 3. 收集待上传(共享表缺失或本地更新)
remote2 = {
    "111": {"nickname": "远端昵称", "ts": 999},     # 远端更新 → 111 不待传
    "333": {"nickname": "远端333", "ts": 400},
}
pending = collect_pending(remote2, cache)
pids = {p["uid"] for p in pending}
check("111 已同步不待传", "111" not in pids)
check("222 本地独有待传", "222" in pids)
check("333 远端更新不待传", "333" not in pids)

# 4. 校验
ok, bad = validate_entries([
    {"uid": "123456", "nickname": "目击而道存矣"},
    {"uid": "12a", "nickname": "非法id"},        # uid 非纯数字
    {"uid": "1", "nickname": "x" * 40},          # 昵称超长
    {"uid": "999", "nickname": ""},              # 空昵称
])
check("合法 1 条", len(ok) == 1)
check("非法 3 条", len(bad) == 3)

# 5. 无 token 发 issue → ValueError
try:
    submit_issue("https://github.com/a/b", "", [{"uid": "1", "nickname": "x"}])
    check("无 token 应抛异常", False)
except ValueError:
    check("无 token 抛异常", True)

# 6. 非法条目 → ValueError
try:
    submit_issue("https://github.com/a/b", "tok", [{"uid": "bad", "nickname": ""}])
    check("全非法应抛异常", False)
except ValueError:
    check("全非法抛异常", True)

print("\n".join(results))
print("NICKNAME SYNC TEST PASSED")
