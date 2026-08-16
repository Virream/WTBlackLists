"""清理仓库 blacklist.json 中已被封号的玩家(独立工具, 仅仓库所有者使用)。

按封号名单(默认: 社区 Fair Play Google Sheet)比对 blacklist.json 的 player_id,
删除命中条目。默认 dry-run(仅预览), 加 --apply 才真正删除并写回 GitHub。

安全设计:
- 只操作仓库 blacklist.json, 不碰本地数据 / 主应用, 不影响现有软件功能
- token 从 git credential 读取(所有者本地凭据), 不落盘、不打印
- 默认只预览命中, 需显式 --apply + 输入 y 确认才会写回

用法:
  .venv\\Scripts\\python.exe tools\\clean_blacklist.py <repo_url>            # dry-run 预览
  .venv\\Scripts\\python.exe tools\\clean_blacklist.py <repo_url> --apply    # 确认后删除写回
  .venv\\Scripts\\python.exe tools\\clean_blacklist.py <repo_url> --ban-file ban.csv
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wt81111g.server_sync import _api_read, _api_write, _entry_key, parse_repo_url  # noqa: E402

DEFAULT_BAN_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vSb9OfOUuFsZCv4_nOXdE-okkWblZ0B06X7rIX1bUJr11KsZCiHd1BaHDrBiTvFMRVWH_HAYTUkOdZm"
    "/pub?gid=1390464946&single=true&output=csv"
)


def get_token() -> str:
    p = subprocess.run(
        ["git", "credential", "fill"],
        input=b"protocol=https\nhost=github.com\n\n",
        capture_output=True,
    )
    lines = dict(
        l.split("=", 1) for l in p.stdout.decode("utf-8", "replace").splitlines() if "=" in l
    )
    tok = (lines.get("password") or "").strip()
    if not tok:
        raise SystemExit("未找到 GitHub 凭据(请先对该仓库 git push 一次以缓存凭据)")
    return tok


def fetch_ban_ids(source: str) -> set[str]:
    """拉取封号名单 ID。source 为 URL 或本地文件路径; CSV 列: IGN,ID,Reason。"""
    if os.path.isfile(source):
        with open(source, encoding="utf-8", errors="replace") as f:
            text = f.read()
    else:
        import urllib.request
        with urllib.request.urlopen(source, timeout=30) as r:
            text = r.read().decode("utf-8", "replace")
    reader = csv.reader(io.StringIO(text))
    ids: set[str] = set()
    for row in reader:
        if len(row) < 2:
            continue
        ign = (row[0] or "").strip()
        iid = (row[1] or "").strip()
        # 跳过 "Total:..." 汇总行 / 表头 / 空 IGN 行; 只收纯数字 ID
        if ign.lower() in ("", "total:", "ign"):
            continue
        if iid.isdigit():
            ids.add(iid)
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(description="清理仓库 blacklist.json 中已封号玩家")
    parser.add_argument("repo_url", help="GitHub 仓库地址, 如 https://github.com/Virream/WTBlackLists")
    parser.add_argument("--apply", action="store_true", help="真正删除命中条目并写回(默认仅预览)")
    parser.add_argument("--ban-file", default=None, help="本地封号名单 CSV 路径(默认拉取社区 Fair Play 名单)")
    args = parser.parse_args()

    p = parse_repo_url(args.repo_url)
    if not p:
        raise SystemExit("不支持的仓库地址(仅支持 GitHub)")
    plat, owner, repo = p
    if plat != "github":
        raise SystemExit("仅支持 GitHub 仓库")
    token = get_token()

    ban_source = args.ban_file or DEFAULT_BAN_URL
    print(f"拉取封号名单: {ban_source}")
    ban_ids = fetch_ban_ids(ban_source)
    print(f"封号名单 ID 数: {len(ban_ids)}")
    if not ban_ids:
        raise SystemExit("封号名单为空, 中止")

    entries, sha = _api_read(plat, owner, repo, token)
    if not isinstance(entries, list):
        raise SystemExit("blacklist.json 不是条目列表")
    print(f"仓库 blacklist.json 条目数: {len(entries)}")

    hits = [e for e in entries if str(e.get("player_id") or "").strip() in ban_ids]
    print(f"\n命中(在封号名单中): {len(hits)} 条")
    for e in hits:
        print(f"  - ID {e.get('player_id')} | 昵称 {e.get('nickname','')} | "
              f"原因 {e.get('reason','')}")
    if not hits:
        print("没有需要清理的条目。")
        return 0

    if not args.apply:
        print("\n[dry-run] 仅预览, 未删除。加 --apply 才真正删除并写回。")
        return 0

    confirm = input(f"确认从仓库删除以上 {len(hits)} 条? (输入 y 确认, 其他取消): ").strip().lower()
    if confirm != "y":
        print("已取消, 未修改仓库。")
        return 0

    hit_keys = {_entry_key(e) for e in hits}
    kept = [e for e in entries if _entry_key(e) not in hit_keys]
    _api_write(plat, owner, repo, token, kept, sha, "清理已封号玩家")
    print(f"✅ 已清理 {len(entries) - len(kept)} 条, 剩余 {len(kept)} 条。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
