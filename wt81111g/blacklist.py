"""黑名单数据模型与持久化。"""
from __future__ import annotations

import datetime
import json
import os
import threading
from dataclasses import asdict, dataclass, field

from .config import blacklist_file as _default_blacklist_file

REASON_CHOICES = [
    "TK(攻击核弹机)",
    "TK(抢战区)",
    "TK(挤出掩体)",
    "TK(空对地)",
    "TK(地对空)",
    "种族仇恨言论",
    "辱骂玩家",
    "阻止占领战区",
    "疑似作弊",
    "其他原因",
]
DEFAULT_REASON = "其他原因"

# 曾用昵称最多保存数量, 超出后删除最早的昵称
MAX_PREVIOUS_NICKNAMES = 10


@dataclass(eq=False)
class BlacklistEntry:
    """黑名单中的一条记录。eq=False 以对象身份比较,便于 index() 定位。"""

    entry_id: str = ""            # 自动生成: 玩家ID_YYYYMMDD_HH_MM
    nickname: str = ""            # 玩家昵称(手动输入)
    previous_nicknames: list[str] = field(default_factory=list)  # 曾用昵称(自动维护, 用户不可改)
    player_id: str = ""           # 玩家ID(手动输入)
    replay_link: str = ""         # 录像链接(手动输入)
    reason: str = ""              # 原因(下拉选择)
    event_date: str = ""          # 事件发生日期(手动输入)
    remarks: str = ""             # 备注(手动输入)
    fetched_nickname: str = ""    # 内部字段:从 War Thunder Live 抓取到的昵称
    fetched_at: float = 0.0        # 内部字段:抓取昵称的时间戳(epoch 秒)
    created_at: str = ""          # 内部字段:创建时间(ISO)
    audited: bool = False          # 是否已审核(网络拉取的条目自动勾选, 用户不可改)
    auditor: str = ""              # 审核员(网络条目来源, 用户不可改)
    cloud_id: str = ""             # 云端唯一标识(UUID, 上传时生成, 用于跨服务器比对/删除)
    locked: bool = False           # 是否锁定(来自服务器下载的条目, 禁止本地编辑)
    source: str = ""               # 来源标识: "local" 或 "server"
    review_id: str = ""            # 审核请求条目ID(从待审核队列拉取时记录, 审核完上传后删除待审核请求)

    def needs_entry_id(self) -> bool:
        """条目ID需要 玩家ID 与 事件发生日期 都已填写。"""
        return bool(self.player_id.strip()) and bool(self.event_date.strip())

    def generate_entry_id(self) -> str:
        """生成条目ID,格式: 玩家ID_当前时间(YYYYMMDD_HH_MM)。"""
        if not self.needs_entry_id():
            return ""
        now = datetime.datetime.now()
        return f"{self.player_id.strip()}_{now:%Y%m%d_%H_%M}"

    def push_previous_nickname(self, old_nickname: str) -> None:
        """把旧昵称加入曾用昵称列表(去重), 超出上限时删除最早的昵称。"""
        old = (old_nickname or "").strip()
        if not old:
            return
        if old in self.previous_nicknames:
            return
        self.previous_nicknames.append(old)
        if len(self.previous_nicknames) > MAX_PREVIOUS_NICKNAMES:
            del self.previous_nicknames[: len(self.previous_nicknames) - MAX_PREVIOUS_NICKNAMES]


class BlacklistStore:
    """黑名单持久化(JSON),支持线程安全增删改。"""

    def __init__(self, path: str | None = None):
        self.path = path or _default_blacklist_file()
        self._lock = threading.RLock()
        self.entries: list[BlacklistEntry] = []
        self.load_error: str = ""  # 加载失败时记录原因, 供主界面提示
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.entries = []
            for d in data if isinstance(data, list) else []:
                if not isinstance(d, dict):
                    continue
                entry = BlacklistEntry()
                for key in BlacklistEntry.__dataclass_fields__:
                    if key in d:
                        setattr(entry, key, d[key])
                # 曾用昵称必须为字符串列表, 且不超过上限
                if not isinstance(entry.previous_nicknames, list):
                    entry.previous_nicknames = []
                entry.previous_nicknames = [str(x) for x in entry.previous_nicknames][:MAX_PREVIOUS_NICKNAMES]
                try:
                    entry.fetched_at = float(entry.fetched_at or 0)
                except (TypeError, ValueError):
                    entry.fetched_at = 0.0
                self.entries.append(entry)
        except Exception as exc:  # noqa: BLE001
            # 不静默丢弃: 备份损坏文件, 记录原因, 避免后续保存覆盖原始数据
            self.load_error = str(exc)
            try:
                if os.path.exists(self.path):
                    backup = f"{self.path}.corrupt-{datetime.datetime.now():%Y%m%d_%H%M%S}"
                    os.replace(self.path, backup)
            except OSError:
                pass
            self.entries = []

    def save(self) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump([asdict(e) for e in self.entries], f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)  # 原子替换, 避免崩溃产生半截文件

    def add(self, entry: BlacklistEntry) -> None:
        with self._lock:
            self.entries.append(entry)
            self.save()

    def append(self, entry: BlacklistEntry, save: bool = True) -> None:
        """线程安全追加; save=False 时不落盘(由调用方稍后统一 save, 供批量导入)。"""
        with self._lock:
            self.entries.append(entry)
            if save:
                self.save()

    def snapshot(self) -> list[BlacklistEntry]:
        """返回条目列表的线程安全快照副本(后台线程读取时避免 size 变化异常)。"""
        with self._lock:
            return list(self.entries)

    def remove_at(self, index: int) -> None:
        with self._lock:
            if 0 <= index < len(self.entries):
                del self.entries[index]
                self.save()

    def entry_ids(self) -> set[str]:
        with self._lock:
            return {e.entry_id for e in self.entries if e.entry_id}
