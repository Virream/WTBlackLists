"""发布 Release 到 GitHub(Virream/WTBlackLists)。

从 git credential 读取 GitHub 凭据(不打印 token), 创建/重建 release 并上传:
  - dist/WTBlackList.zip         (ZIP 绿色版)
  - dist/WTBlackList_Setup.exe   (自解压安装版)
  - dist/WTBlackList_source.zip  (源码版)

用法:
  .venv\\Scripts\\python.exe tools\\publish_release.py <tag> [<title>] [<notes_file>]

凭据来源: git credential fill(需先对该仓库 push 过以缓存 GitHub 凭据)。
测试阶段默认标记为 prerelease。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

REPO = "Virream/WTBlackLists"
DEFAULT_NOTES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "build_assets", "release_notes.md",
)
ASSETS: list = []  # 由 main 动态构建(产物文件名带版本号)


def _read_version(base: str) -> str:
    """从 config.py 读取当前版本号。"""
    try:
        import re
        with open(os.path.join(base, "wt81111g", "config.py"), encoding="utf-8") as f:
            m = re.search(r"APP_VERSION\s*=\s*['\"]([^'\"]+)['\"]", f.read())
        return m.group(1) if m else ""
    except Exception:  # noqa: BLE001
        return ""


def _asset_files(base: str, ver: str) -> list[tuple[str, str, str]]:
    """(相对路径, content_type, 上传文件名): 带版本文件名优先, 缺则回退旧名。"""
    cands = [
        (f"dist/WTBlackList_{ver}.zip", "application/zip", f"WTBlackList_{ver}.zip"),
        (f"dist/WTBlackList_Setup_{ver}.exe", "application/octet-stream",
         f"WTBlackList_Setup_{ver}.exe"),
        (f"dist/WTBlackList_source_{ver}.zip", "application/zip",
         f"WTBlackList_source_{ver}.zip"),
    ]
    legacy = [
        ("dist/WTBlackList.zip", "application/zip", "WTBlackList.zip"),
        ("dist/WTBlackList_Setup.exe", "application/octet-stream", "WTBlackList_Setup.exe"),
        ("dist/WTBlackList_source.zip", "application/zip", "WTBlackList_source.zip"),
    ]
    out = []
    for cand, leg in zip(cands, legacy):
        if os.path.isfile(os.path.join(base, cand[0])):
            out.append(cand)
        else:
            out.append(leg)
    return out


def get_token() -> str:
    p = subprocess.run(
        ["git", "credential", "fill"],
        input=b"protocol=https\nhost=github.com\n\n",
        capture_output=True,
    )
    lines: dict[str, str] = {}
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            lines[k] = v
    tok = (lines.get("password") or "").strip()
    if not tok:
        raise SystemExit("未找到 GitHub 凭据(请先对该仓库 git push 一次以缓存凭据)")
    return tok


def api(method: str, path: str, token: str,
        body: dict | None = None, raw: bytes | None = None,
        content_type: str = "application/octet-stream") -> dict:
    host = "https://uploads.github.com" if "/assets?" in path else "https://api.github.com"
    req = urllib.request.Request(host + path, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "WTBlackList")
    req.add_header("Accept", "application/vnd.github+json")
    data = None
    if raw is not None:
        data = raw
        req.add_header("Content-Type", content_type)
    elif body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    # 走系统代理(Clash 等); 上传大文件对节点稳定性要求高, 失败时外层重试
    try:
        with urllib.request.urlopen(req, data=data, timeout=600) as r:
            raw = r.read()
            if not raw:
                return {}  # 204 等无返回体
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise ValueError(f"HTTP {e.code} {method} {path}: {detail}") from e


def main() -> int:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tag = sys.argv[1] if len(sys.argv) > 1 else "v2.0.2"
    title = sys.argv[2] if len(sys.argv) > 2 else f"WTBlackList {tag.lstrip('v')}"
    notes_file = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_NOTES
    token = get_token()
    ver = _read_version(base)
    notes = ""
    if os.path.isfile(notes_file):
        with open(notes_file, encoding="utf-8") as f:
            notes = f.read()

    # 若 tag 已有 release, 先删除 release 与 tag(幂等重建)
    exists = False
    try:
        api("GET", f"/repos/{REPO}/releases/tags/{tag}", token)
        exists = True
    except ValueError as e:
        if "HTTP 404" not in str(e):
            raise SystemExit(str(e))
    if exists:
        rel = api("GET", f"/repos/{REPO}/releases/tags/{tag}", token)
        api("DELETE", f"/repos/{REPO}/releases/{rel['id']}", token)
        try:
            api("DELETE", f"/repos/{REPO}/git/refs/tags/{tag}", token)
        except ValueError:
            pass
        print(f"已删除旧 release/tag: {tag}")

    rel = api("POST", f"/repos/{REPO}/releases", token, body={
        "tag_name": tag,
        "name": title,
        "body": notes,
        "draft": False,
        "prerelease": True,  # 测试阶段 → 标记为预发布
    })
    rel_id = rel["id"]
    print(f"已创建 release {tag}: {rel['html_url']}")

    for rel_path, ctype, fname in _asset_files(base, ver):
        full = os.path.join(base, rel_path)
        if not os.path.isfile(full):
            print(f"跳过(文件不存在): {rel_path}")
            continue
        with open(full, "rb") as f:
            data = f.read()
        api("POST",
            f"/repos/{REPO}/releases/{rel_id}/assets?name={fname}",
            token, raw=data, content_type=ctype)
        print(f"已上传 {fname}: {os.path.getsize(full) / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
