"""服务器同步: GitHub/Gitee 仓库名单的拉取、上传、删除与登录校验。

约定:
- 名单文件固定为仓库根目录的 blacklist.json(与本地相同的 JSON 列表格式)。
- 条目唯一性以 cloud_id(全局 UUID)为准; 旧条目(无 cloud_id)回退到
  player_id + event_date 组合键。
- 拉取(公开仓库)无需鉴权; 上传/删除/登录需要 token 或系统已登录凭据。
"""
from __future__ import annotations

import base64
import json
import re
import os
import time

import requests

# 使用 Windows 系统证书存储(解决 Python certifi 证书链不完整导致的
# SSLCertVerificationError, 尤其在代理/企业环境中 curl/PowerShell 正常但 requests 失败)
try:
    import truststore  # type: ignore
    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001
    pass

DEFAULT_FILE = "blacklist.json"
DEFAULT_BRANCH = "main"
TIMEOUT = 15
UA = "WTBlackList/2.0.2"
MAX_RETRIES = 3          # 乐观锁冲突自动重试上限
RETRY_DELAY = 0.6        # 重试间隔(秒)


class ConflictError(ValueError):
    """乐观锁冲突: 写入前文件被他人修改, 需要重读+重试。"""


# ----------------------------------------------------------------------
# URL 解析
# ----------------------------------------------------------------------
def parse_repo_url(url: str) -> tuple[str, str, str] | None:
    """解析仓库 URL, 返回 (platform, owner, repo)。支持 github.com / gitee.com。

    兼容带 .git 后缀 / 树路径 / 结尾斜杠的地址, 统一提取 owner 与 repo。
    """
    u = (url or "").strip().rstrip("/")
    m = re.match(r"https?://(?:www\.)?github\.com/([^/]+)/([^/]+)", u)
    if m:
        return ("github", m.group(1), _strip_git(m.group(2)))
    m = re.match(r"https?://(?:www\.)?gitee\.com/([^/]+)/([^/]+)", u)
    if m:
        return ("gitee", m.group(1), _strip_git(m.group(2)))
    return None


def _strip_git(repo: str) -> str:
    """去掉仓库名末尾的 .git 后缀(如 WTBlackLists.git -> WTBlackLists)。"""
    repo = repo.strip()
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    return repo.rstrip("/")


def platform_of(url: str) -> str:
    p = parse_repo_url(url)
    return p[0] if p else ""


def raw_url(url: str, branch: str = DEFAULT_BRANCH) -> str | None:
    """公开仓库 raw 拉取地址(无需鉴权)。"""
    p = parse_repo_url(url)
    if not p:
        return None
    plat, owner, repo = p
    if plat == "github":
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{DEFAULT_FILE}"
    return f"https://gitee.com/{owner}/{repo}/raw/{branch}/{DEFAULT_FILE}"


def _headers_github(token: str | None = None) -> dict:
    h = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


# ----------------------------------------------------------------------
# 拉取
# ----------------------------------------------------------------------
def fetch_entries(url: str) -> list[dict]:
    """从公开仓库拉取名单条目列表。文件不存在视为空列表。

    GitHub 走 api.github.com contents API(与上传/审核同一条通道, 避免
    raw.githubusercontent.com 在部分网络下不可达导致长时间卡住);
    Gitee 走 raw 地址。
    """
    p = parse_repo_url(url)
    if not p:
        raise ValueError("不支持的仓库地址(仅支持 GitHub / Gitee)")
    plat, owner, repo = p
    if plat == "github":
        api = (f"https://api.github.com/repos/{owner}/{repo}/contents/"
               f"{DEFAULT_FILE}?ref={DEFAULT_BRANCH}")
        try:
            r = requests.get(api, timeout=TIMEOUT, headers=_headers_github())
        except requests.RequestException as exc:
            raise ValueError(f"无法连接服务器: {exc}") from exc
        if r.status_code == 404:
            return []
        r.raise_for_status()
        try:
            j = r.json()
        except ValueError as exc:
            raise ValueError("服务器文件不是有效的 JSON") from exc
        if not isinstance(j, dict) or not j.get("content"):
            raise ValueError("服务器文件内容读取失败")
        try:
            import base64 as _b64
            text = _b64.b64decode(j["content"]).decode("utf-8", "replace")
            data = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("服务器文件不是有效的 JSON") from exc
    else:
        raw = f"https://gitee.com/{owner}/{repo}/raw/{DEFAULT_BRANCH}/{DEFAULT_FILE}"
        try:
            r = requests.get(raw, timeout=TIMEOUT, headers={"User-Agent": UA})
        except requests.RequestException as exc:
            raise ValueError(f"无法连接服务器: {exc}") from exc
        if r.status_code == 404:
            return []
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError as exc:
            raise ValueError("服务器文件不是有效的 JSON") from exc
    if not isinstance(data, list):
        raise ValueError("服务器名单格式错误: 应为条目列表")
    return [d for d in data if isinstance(d, dict)]


# ----------------------------------------------------------------------
# 登录校验
# ----------------------------------------------------------------------
def verify_login(platform: str, token: str) -> str:
    """校验 token 是否有效, 返回登录用户名。失败抛出 ValueError。"""
    token = (token or "").strip()
    if not token:
        raise ValueError("token 不能为空")
    if platform == "github":
        r = requests.get(
            "https://api.github.com/user",
            headers=_headers_github(token), timeout=TIMEOUT,
        )
        if r.status_code == 200:
            return str(r.json().get("login") or "")
        raise ValueError(f"GitHub 登录失败 (HTTP {r.status_code}): {r.text[:200]}")
    # gitee
    r = requests.get(
        "https://gitee.com/api/v5/user",
        params={"access_token": token}, headers={"User-Agent": UA}, timeout=TIMEOUT,
    )
    if r.status_code == 200:
        j = r.json()
        return str(j.get("login") or j.get("name") or "")
    raise ValueError(f"Gitee 登录失败 (HTTP {r.status_code}): {r.text[:200]}")


def verify_password(platform: str, username: str, password: str) -> str:
    """账号密码登录, 返回登录用户名。失败抛出 ValueError。

    - Gitee: 通过 OAuth password grant 交换 access_token(需应用 client_id/secret,
      这里使用 Gitee 开放平台的通用参数, 用户可后续在服务器设置中补充)。
    - GitHub: 已停止支持密码直连, 引导使用 token/证书。
    """
    username = (username or "").strip()
    password = (password or "").strip()
    if not username or not password:
        raise ValueError("账号与密码不能为空")
    if platform == "github":
        raise ValueError(
            "GitHub 已停止支持账号密码直连登录。\n"
            "请改用「Token 登录」(Personal Access Token) 或「证书登录」(SSH key)。"
        )
    # Gitee OAuth password grant
    try:
        r = requests.post(
            "https://gitee.com/oauth/token",
            data={
                "grant_type": "password",
                "username": username,
                "password": password,
                "client_id": "",      # 需要 Gitee 开放平台应用; 留空由 Gitee 返回错误
                "client_secret": "",
            },
            headers={"User-Agent": UA}, timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ValueError(f"无法连接 Gitee: {exc}") from exc
    if r.status_code == 200:
        j = r.json()
        token = str(j.get("access_token") or "")
        if token:
            return verify_login("gitee", token)
    raise ValueError(
        "Gitee 账号密码登录失败(需在 Gitee 开放平台注册应用获取 client_id/secret)。\n"
        "更简单的方式: 使用「Token 登录」填入私人令牌。"
    )


def verify_cert(platform: str, cert_path: str) -> str:
    """证书(SSH key)登录校验, 返回登录用户名。失败抛出 ValueError。"""
    import subprocess

    cert_path = (cert_path or "").strip()
    if not cert_path or not os.path.isfile(cert_path):
        raise ValueError(f"证书文件不存在: {cert_path}")
    host = "git@github.com" if platform == "github" else "git@gitee.com"
    try:
        proc = subprocess.run(
            ["ssh", "-i", cert_path, "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=no", "-T", host],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"SSH 执行失败: {exc}") from exc
    out = (proc.stdout or "") + (proc.stderr or "")
    # GitHub 输出形如: Hi <username>! You've successfully authenticated...
    # Gitee 输出形如: Hi <username>! You've successfully authenticated...
    import re as _re
    m = _re.search(r"Hi\s+([^!]+)!", out)
    if m:
        return m.group(1).strip()
    raise ValueError(
        f"证书验证失败, 请确认 SSH key 已添加到对应平台账号:\n{out.strip()[:200]}"
    )


# ----------------------------------------------------------------------
# 服务器文件读写(API, 需鉴权)
# ----------------------------------------------------------------------
def _api_read(platform: str, owner: str, repo: str, token: str) -> tuple[list[dict], str | None]:
    """读取仓库内名单文件, 返回 (条目列表, sha)。文件不存在返回 ([], None)。"""
    if platform == "github":
        r = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}/contents/{DEFAULT_FILE}",
            headers=_headers_github(token), timeout=TIMEOUT,
        )
        if r.status_code == 404:
            return [], None
        r.raise_for_status()
        j = r.json()
        content = base64.b64decode(j.get("content") or "").decode("utf-8", "replace")
        try:
            data = json.loads(content)
        except ValueError as exc:
            raise ValueError("服务器文件不是有效的 JSON") from exc
        return (data if isinstance(data, list) else []), j.get("sha")
    # gitee
    r = requests.get(
        f"https://gitee.com/api/v5/repos/{owner}/{repo}/contents/{DEFAULT_FILE}",
        params={"access_token": token}, headers={"User-Agent": UA}, timeout=TIMEOUT,
    )
    if r.status_code == 404:
        return [], None
    r.raise_for_status()
    j = r.json()
    if isinstance(j, list):
        # Gitee 大文件返回 base64, 小文件返回文本
        content = ""
        for f in j:
            if f.get("type") == "file":
                content = f.get("content") or ""
                break
        data = json.loads(content) if content else []
        sha = None
    else:
        content = j.get("content") or ""
        sha = j.get("sha")
        data = json.loads(content) if content else []
    return (data if isinstance(data, list) else []), sha


def _api_write(platform: str, owner: str, repo: str, token: str,
               entries: list[dict], sha: str | None, message: str) -> None:
    """把条目列表写入仓库文件(覆盖式, 调用前已合并好)。"""
    content = json.dumps(entries, ensure_ascii=False, indent=2).encode("utf-8")
    b64 = base64.b64encode(content).decode()
    if platform == "github":
        body: dict = {
            "message": message or "WTBlackList 同步",
            "content": b64,
            "branch": DEFAULT_BRANCH,
        }
        if sha:
            body["sha"] = sha
        r = requests.put(
            f"https://api.github.com/repos/{owner}/{repo}/contents/{DEFAULT_FILE}",
            headers=_headers_github(token), json=body, timeout=TIMEOUT,
        )
        if r.status_code not in (200, 201):
            if r.status_code == 409:
                raise ConflictError("服务器文件已被他人修改(乐观锁冲突)")
            raise ValueError(f"GitHub 写入失败 (HTTP {r.status_code}): {r.text[:200]}")
        return
    # gitee
    body = {
        "access_token": token,
        "content": b64,
        "message": message or "WTBlackList 同步",
        "branch": DEFAULT_BRANCH,
    }
    if sha:
        body["sha"] = sha
    r = requests.put(
        f"https://gitee.com/api/v5/repos/{owner}/{repo}/contents/{DEFAULT_FILE}",
        data=body, headers={"User-Agent": UA}, timeout=TIMEOUT,
    )
    if r.status_code not in (200, 201):
        if r.status_code == 409:
            raise ConflictError("服务器文件已被他人修改(乐观锁冲突)")
        raise ValueError(f"Gitee 写入失败 (HTTP {r.status_code}): {r.text[:200]}")


# ----------------------------------------------------------------------
# 合并 / 去重
# ----------------------------------------------------------------------
def _entry_key(d: dict) -> str:
    """条目唯一键: cloud_id 优先, 否则 player_id + event_date 组合。"""
    cid = str(d.get("cloud_id") or "").strip()
    if cid:
        return f"cid:{cid}"
    pid = str(d.get("player_id") or "").strip()
    date = str(d.get("event_date") or "").strip()
    if pid and date:
        return f"pid:{pid}|{date}"
    if pid:
        return f"pid:{pid}"
    return f"raw:{json.dumps(d, ensure_ascii=False, sort_keys=True)}"


def merge_entries(existing: list[dict], incoming: list[dict],
                  prefer: str = "incoming") -> tuple[list[dict], int]:
    """合并两组条目, 按唯一键去重。返回 (合并结果, 新增数量)。

    prefer: "incoming" 时服务器条目覆盖本地(供审核推送), "local" 时本地优先(供普通用户拉取)。
    """
    out: dict[str, dict] = {}
    for e in existing:
        out[_entry_key(e)] = dict(e)
    added = 0
    for e in incoming:
        k = _entry_key(e)
        if k not in out:
            added += 1
        elif prefer == "local":
            continue  # 本地已存在, 保留本地
        out[k] = dict(e)
    return list(out.values()), added


# ----------------------------------------------------------------------
# 上传 / 删除(含乐观锁冲突自动重试)
# ----------------------------------------------------------------------
def upload_entries(url: str, token: str, entries: list[dict],
                   message: str = "WTBlackList 审核上传",
                   on_retry=None) -> dict:
    """把条目合并写入服务器(以 cloud_id 去重, 服务器已有同键条目被覆盖)。

    遇到乐观锁冲突自动重读最新文件并重试(最多 MAX_RETRIES 次),
    每次重试前调用 on_retry(retry_count) 供 UI 提醒用户。

    返回 {"uploaded": int, "added": int, "total": int, "retries": int}。
    """
    p = parse_repo_url(url)
    if not p:
        raise ValueError("不支持的仓库地址(仅支持 GitHub / Gitee)")
    plat, owner, repo = p
    retries = 0
    while True:
        existing, sha = _api_read(plat, owner, repo, token)
        merged, added = merge_entries(existing, entries, prefer="incoming")
        try:
            _api_write(plat, owner, repo, token, merged, sha, message)
            return {"uploaded": len(entries), "added": added,
                    "total": len(merged), "retries": retries}
        except ConflictError:
            retries += 1
            if retries > MAX_RETRIES:
                raise ValueError(
                    f"服务器文件持续被他人修改, 自动重试 {MAX_RETRIES} 次后放弃, 请稍后再试"
                ) from None
            if on_retry:
                on_retry(retries)
            time.sleep(RETRY_DELAY)


def delete_entries(url: str, token: str, cloud_ids: set[str],
                   message: str = "WTBlackList 审核删除",
                   on_retry=None) -> dict:
    """按 cloud_id 从服务器文件移除条目。

    乐观锁冲突自动重试, 逻辑同 upload_entries。
    返回 {"removed": int, "total": int, "retries": int}。
    """
    p = parse_repo_url(url)
    if not p:
        raise ValueError("不支持的仓库地址(仅支持 GitHub / Gitee)")
    plat, owner, repo = p
    removed_ids = {str(x) for x in cloud_ids if x}
    retries = 0
    while True:
        existing, sha = _api_read(plat, owner, repo, token)
        keep = [e for e in existing
                if str(e.get("cloud_id") or "").strip() not in removed_ids]
        removed = len(existing) - len(keep)
        try:
            _api_write(plat, owner, repo, token, keep, sha, message)
            return {"removed": removed, "total": len(keep), "retries": retries}
        except ConflictError:
            retries += 1
            if retries > MAX_RETRIES:
                raise ValueError(
                    f"服务器文件持续被他人修改, 自动重试 {MAX_RETRIES} 次后放弃, 请稍后再试"
                ) from None
            if on_retry:
                on_retry(retries)
            time.sleep(RETRY_DELAY)
