"""服务器同步核心逻辑测试(纯逻辑, 不联网): URL解析、合并去重、键生成。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wt81111g.server_sync import (
    _entry_key, ConflictError, delete_entries, merge_entries, parse_repo_url,
    platform_of, raw_url, upload_entries, verify_cert, verify_password,
)
from wt81111g.blacklist import BlacklistEntry

results: list[str] = []


def check(name: str, cond: bool) -> None:
    if cond:
        results.append(f"OK: {name}")
    else:
        results.append(f"FAIL: {name}")
        raise AssertionError(name)


def test_url_parsing() -> None:
    check("github 解析", parse_repo_url("https://github.com/user/repo") == ("github", "user", "repo"))
    check("github www 解析", parse_repo_url("https://www.github.com/a/b/") == ("github", "a", "b"))
    check("gitee 解析", parse_repo_url("https://gitee.com/user/repo") == ("gitee", "user", "repo"))
    check("非法地址返回 None", parse_repo_url("https://example.com/x") is None)
    check(".git 后缀剥离", parse_repo_url("https://github.com/u/repo.git") == ("github", "u", "repo"))
    check(".git 后缀+斜杠", parse_repo_url("https://gitee.com/u/r.git/") == ("gitee", "u", "r"))
    check("platform github", platform_of("https://github.com/u/r") == "github")
    check("platform gitee", platform_of("https://gitee.com/u/r") == "gitee")
    check("github raw URL", raw_url("https://github.com/u/r") ==
          "https://raw.githubusercontent.com/u/r/main/blacklist.json")
    check("github raw URL 带.git", raw_url("https://github.com/u/r.git") ==
          "https://raw.githubusercontent.com/u/r/main/blacklist.json")
    check("gitee raw URL", raw_url("https://gitee.com/u/r") ==
          "https://gitee.com/u/r/raw/main/blacklist.json")


def test_entry_key() -> None:
    a = {"cloud_id": "abc", "player_id": "1"}
    b = {"cloud_id": "abc", "player_id": "999"}  # 同 cloud_id 不同 pid → 同键
    check("cloud_id 优先", _entry_key(a) == _entry_key(b))
    c = {"player_id": "1", "event_date": "2026-01-01"}
    d = {"player_id": "1", "event_date": "2026-01-01", "reason": "x"}
    check("无 cloud_id 用 pid+date", _entry_key(c) == _entry_key(d))
    e = {"player_id": "1", "event_date": "2026-01-02"}
    check("不同日期不同键", _entry_key(c) != _entry_key(e))


def test_merge() -> None:
    existing = [{"cloud_id": "A", "player_id": "1"}, {"player_id": "2", "event_date": "2026-01-01"}]
    incoming = [{"cloud_id": "A", "player_id": "1", "reason": "new"}, {"cloud_id": "B", "player_id": "3"}]
    merged, added = merge_entries(existing, incoming, prefer="incoming")
    check("合并去重数量", len(merged) == 3)
    check("新增数 1", added == 1)
    # incoming 覆盖已有
    for m in merged:
        if m.get("cloud_id") == "A":
            check("incoming 覆盖", m.get("reason") == "new")
    # prefer=local 时保留本地
    merged2, added2 = merge_entries(existing, incoming, prefer="local")
    for m in merged2:
        if m.get("cloud_id") == "A":
            check("local 优先保留本地", m.get("reason") is None)


def test_entry_cloud_fields() -> None:
    e = BlacklistEntry()
    check("默认 cloud_id 空", e.cloud_id == "")
    check("默认 locked False", e.locked is False)
    check("默认 source 空", e.source == "")
    e.locked = True
    e.source = "server"
    check("字段可赋值", e.locked and e.source == "server")


def test_upload_retry_on_conflict() -> None:
    """模拟写入冲突: 第一次 409, 第二次成功, 应自动重试并回调 on_retry。"""
    import wt81111g.server_sync as ss

    calls = {"read": 0, "write": 0, "retry": 0}

    def fake_read(plat, owner, repo, token):
        calls["read"] += 1
        return [{"cloud_id": "server1", "player_id": "99"}], f"sha-{calls['read']}"

    def fake_write(plat, owner, repo, token, entries, sha, message):
        calls["write"] += 1
        if calls["write"] == 1:
            raise ConflictError("conflict")
        # 第二次成功

    orig_read, orig_write = ss._api_read, ss._api_write
    ss._api_read = fake_read
    ss._api_write = fake_write
    try:
        def on_retry(n):
            calls["retry"] = n

        res = upload_entries(
            "https://github.com/u/r", "tok",
            [{"cloud_id": "new1", "player_id": "1"}], on_retry=on_retry,
        )
        check("冲突后自动重试(写入2次)", calls["write"] == 2)
        check("重试回调触发", calls["retry"] == 1)
        check("返回 retries 计数", res.get("retries") == 1)
        check("重试后仍返回 uploaded", res.get("uploaded") == 1)
    finally:
        ss._api_read, ss._api_write = orig_read, orig_write


def test_upload_retry_gives_up() -> None:
    """持续冲突超过上限应放弃并抛错。"""
    import wt81111g.server_sync as ss

    def fake_read(plat, owner, repo, token):
        return [], "sha"

    def fake_write(plat, owner, repo, token, entries, sha, message):
        raise ConflictError("conflict")

    orig_read, orig_write = ss._api_read, ss._api_write
    ss._api_read = fake_read
    ss._api_write = fake_write
    try:
        try:
            upload_entries("https://github.com/u/r", "tok",
                           [{"cloud_id": "x", "player_id": "1"}])
            check("持续冲突应抛错", False)
        except ValueError:
            check("持续冲突应抛错", True)
    finally:
        ss._api_read, ss._api_write = orig_read, orig_write


def test_password_login_github_rejected() -> None:
    """GitHub 账号密码直连应被明确拒绝并提示改用 token/证书。"""
    try:
        verify_password("github", "user", "pass")
        check("GitHub 密码登录应拒绝", False)
    except ValueError as exc:
        check("GitHub 密码登录应拒绝", "token" in str(exc) or "Token" in str(exc))


def test_password_login_empty() -> None:
    try:
        verify_password("gitee", "", "")
        check("空账号密码应报错", False)
    except ValueError:
        check("空账号密码应报错", True)


def test_cert_login_missing_file() -> None:
    try:
        verify_cert("github", "C:/nonexistent/id_rsa")
        check("证书文件不存在应报错", False)
    except ValueError as exc:
        check("证书文件不存在应报错", "不存在" in str(exc))


def main() -> int:
    test_url_parsing()
    test_entry_key()
    test_merge()
    test_entry_cloud_fields()
    test_upload_retry_on_conflict()
    test_upload_retry_gives_up()
    test_password_login_github_rejected()
    test_password_login_empty()
    test_cert_login_missing_file()
    print("\n".join(results))
    print("SERVER SYNC TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
