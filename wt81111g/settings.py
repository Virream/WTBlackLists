"""应用设置持久化(叠加层等)。"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass

from .config import data_dir


@dataclass
class OverlaySettings:
    pos_x_pct: float = 50.0    # 文本水平中心位置(屏幕宽百分比)
    pos_y_pct: float = 80.0    # 文本顶部位置(屏幕高百分比, 顶端为 0)
    locked: bool = False       # 锁定位置(禁止再调整)
    bg_color: str = "#0a0a1e"  # 背景色
    bg_alpha: int = 190        # 背景透明度 0-255
    font_size: int = 24        # 字体大小 px
    font_family: str = "Microsoft YaHei"  # 字体
    font_color: str = "#e0e0e0"          # 字体颜色
    corner_radius: int = 12    # 背景圆角弧度 px
    show_reason: bool = True   # 命中提示是否显示原因
    text_checking: str = "正在确认名单中..."  # 未命中时提示文本
    text_found: str = "发现肃反人员"          # 命中时的标题文本

    @property
    def bg_rgba(self) -> tuple[int, int, int, int]:
        c = self.bg_color.lstrip("#")
        if len(c) != 6:
            return (10, 10, 30, self.bg_alpha)
        try:
            r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        except ValueError:
            return (10, 10, 30, self.bg_alpha)
        return (r, g, b, self.bg_alpha)


# 官方默认共享仓库(黑名单共享 + 审核 + nickname.json 共享表)
DEFAULT_REPO = "https://github.com/Virream/WTBlackLists.git"


class AppSettings:
    """保存/读取应用设置(保存到 data/config.json)。"""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(data_dir(), "config.json")
        self._lock = threading.RLock()
        self.first_run = True
        self.overlay = OverlaySettings()
        # 拉取服务器: [{"url": "https://github.com/owner/repo", "platform": "github", "name": "..."}]
        self.fetch_servers: list[dict] = []
        # 审核服务器: [{"url": ..., "platform": ..., "name": ..., "token": "", "logged_in": False, "username": ""}]
        self.audit_servers: list[dict] = []
        # 网络同步开关: 抓取到新昵称后是否上传到共享表(需登录 GitHub)
        self.sync_enabled = False
        # 代理: 设置后所有网络请求都走该代理; 空串=直连
        self.proxy = ""
        # 自动更新: 勾选后 24h 过期的昵称在进入新对局时自动更新(默认开启)
        self.auto_browser = True
        existed = os.path.exists(self.path)
        self._load()
        # 首次(无配置文件)预置官方默认仓库; 用户删除后不再自动加回, 可自行更换
        if not existed:
            if not self.fetch_servers:
                self.fetch_servers = [self._default_repo_entry()]
            if not self.audit_servers:
                self.audit_servers = [self._default_repo_entry()]

    def _default_repo_entry(self) -> dict:
        """官方默认仓库条目(黑名单共享/审核 + nickname.json 共享表)。"""
        return {
            "url": DEFAULT_REPO,
            "platform": "github",
            "name": "官方共享仓库 (Virream/WTBlackLists)",
        }

    def _load(self) -> None:
        try:
            if not os.path.exists(self.path):
                return
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.first_run = bool(data.get("first_run", True))
                ov = data.get("overlay")
                if isinstance(ov, dict):
                    for key in OverlaySettings.__dataclass_fields__:
                        if key in ov:
                            setattr(self.overlay, key, ov[key])
                fs = data.get("fetch_servers")
                if isinstance(fs, list):
                    self.fetch_servers = [d for d in fs if isinstance(d, dict)]
                au = data.get("audit_servers")
                if isinstance(au, list):
                    self.audit_servers = [d for d in au if isinstance(d, dict)]
                self.sync_enabled = bool(data.get("sync_enabled", False))
                self.proxy = str(data.get("proxy", "") or "")
                self.auto_browser = bool(data.get("auto_browser", False))
        except Exception:  # noqa: BLE001
            pass

    def save(self) -> None:
        with self._lock:
            payload = {
                "first_run": self.first_run,
                "overlay": asdict(self.overlay),
                "fetch_servers": self.fetch_servers,
                "audit_servers": self.audit_servers,
                "sync_enabled": self.sync_enabled,
                "proxy": self.proxy,
                "auto_browser": self.auto_browser,
            }
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
