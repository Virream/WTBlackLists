"""从发言记录/击杀列表提取玩家昵称。"""
from __future__ import annotations

import re

# 主机平台(console)玩家昵称前会带 "⋇" 标记, 它不是昵称的一部分
_CONSOLE_MARK = "⋇"

# 匹配形如 "玩家名 (载具名)" / "*玩家名 (载具名)" / "⋇玩家名 (载具名)" 的片段。
# 带标记前缀(WT 中标记真人/主机玩家)允许多词昵称;不带标记只取单 token,避免动词混入。
_HUD_NAME_RE = re.compile(r"(?:(?:\*|⋇)\s*([^()]+?)|([^\s()]+))\s*\([^)]*\)")

# 常见非昵称词,过滤系统消息噪声
_NON_NAME = {
    "", "you", "your", "you have", "system", "server", "all",
    "unknown", "n/a", "game", "battle", "mission", "squadron",
}


def _clean(name: str) -> str:
    name = name.strip()
    # 去除开头的 ⋇(第一个)与 * 前缀
    if name.startswith(_CONSOLE_MARK):
        name = name[len(_CONSOLE_MARK):].lstrip()
    elif name.startswith("*"):
        name = name[1:].lstrip()
    return name.strip()


def is_plausible_name(name: str) -> bool:
    """昵称至少应包含一个字母/中文,且不是纯数字/纯符号。"""
    if not name:
        return False
    lowered = name.lower()
    if lowered in _NON_NAME:
        return False
    has_letter = any(ch.isalpha() for ch in name)
    if not has_letter:
        return False
    return True


def parse_hudmsg_names(msg: str) -> list[str]:
    """从 HUD 击杀/伤害消息文本中解析出候选玩家昵称。"""
    if not msg:
        return []
    names: list[str] = []
    for m in _HUD_NAME_RE.finditer(msg):
        raw = m.group(1) if m.group(1) is not None else m.group(2)
        name = _clean(raw)
        if is_plausible_name(name) and name not in names:
            names.append(name)
    return names
