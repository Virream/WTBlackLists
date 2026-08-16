"""路径与常量配置。"""
from __future__ import annotations

import os
import sys

APP_NAME = "WTBlackList"
APP_VERSION = "2.0.1"

# 战争雷霆 8111 本地接口
WT_BASE_URL = "http://localhost:8111"

# 黑名单玩家在 War Thunder Live 上的主页模板,数字部分为玩家ID
PROFILE_URL_TEMPLATE = "https://live.warthunder.com/user/{player_id}/"

# 官网社区玩家资料页模板(备选源, 偶发触发 Cloudflare 人机验证, 不稳定但可用)
WEBSITE_USERINFO_TEMPLATE = "https://warthunder.com/zh/community/userinfo/?uid={player_id}/"


def app_root() -> str:
    """软件根目录(打包后为 exe 所在目录,开发时为项目根目录)。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_path(rel: str) -> str:
    """定位资源文件(onefile 打包时在解压目录, 开发时在项目根目录)。"""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        return os.path.join(base, rel)
    return os.path.join(app_root(), rel)


def _writable_root() -> str:
    """返回可写的应用根目录; 若安装目录只读(如 Program Files)则退回用户目录。"""
    root = app_root()
    probe = os.path.join(root, ".wtest")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("")
        os.remove(probe)
        return root
    except OSError:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_NAME)


def evidences_dir() -> str:
    """证据文件夹根目录: <可写根目录>/evidences"""
    d = os.path.join(_writable_root(), "evidences")
    os.makedirs(d, exist_ok=True)
    return d


def data_dir() -> str:
    d = os.path.join(_writable_root(), "data")
    os.makedirs(d, exist_ok=True)
    return d


def blacklist_file() -> str:
    return os.path.join(data_dir(), "blacklist.json")


def nickname_cache_file() -> str:
    """独立昵称缓存数据库(与黑名单条目解耦, 删除条目后缓存仍保留)。"""
    return os.path.join(data_dir(), "nickname_cache.json")
