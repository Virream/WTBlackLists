"""昵称归一化与模糊匹配工具。

解决主机平台玩家的昵称差异:
- WT Live 抓取的昵称带平台后缀, 如 "Uchpochmak1179@live" / "RUNASAPURIZUMU1@psn"
- 游戏内对局昵称可能带联队名/平台标识前缀, 如 "联队名 ⋇昵称" 或 "⋇昵称"
两侧都需要清洗到"真实昵称"后再比对。
"""
from __future__ import annotations

import re

# 主机平台后缀(WTLive 玩家页昵称尾部): @psn / @live / @xbox 等
_PLATFORM_SUFFIX_RE = re.compile(r"@(?:psn|live|xbox|steam)\b", re.IGNORECASE)

# 对局昵称开头的标记: ⋇(主机玩家)、*(真人标记)
_CONSOLE_MARK = "⋇"

# 联队/平台前缀提示词(游戏内主机玩家昵称可能形如 "<联队> ⋇<昵称>")
# 用空格/括号/破折号等分隔, 供子串匹配参考(不强行切分, 由模糊匹配兜底)


def clean_wtlive_nickname(nick: str) -> str:
    """清洗 WT Live 抓取到的昵称: 去掉 @psn/@live/@xbox 平台后缀。"""
    nick = (nick or "").strip()
    if not nick:
        return ""
    # 去掉平台后缀
    nick = _PLATFORM_SUFFIX_RE.sub("", nick).strip()
    # 去掉尾部残留的 @ 与空白
    nick = nick.rstrip("@").strip()
    return nick


def clean_battle_nickname(nick: str) -> str:
    """清洗游戏内对局昵称: 去掉 ⋇ / * 前缀标记。"""
    nick = (nick or "").strip()
    if not nick:
        return ""
    # 去开头标记(可多个)
    while nick.startswith(_CONSOLE_MARK) or nick.startswith("*"):
        nick = nick[1:].lstrip()
    return nick.strip()


def battle_nickname_variants(nick: str) -> list[str]:
    """生成游戏内昵称的候选变体(用于匹配)。

    主机玩家对局昵称可能是 "<联队> ⋇<昵称>" 或 "⋇<昵称>",
    返回: [原始, 去标记, 末尾最长token(昵称)] 等候选。
    """
    nick = (nick or "").strip()
    if not nick:
        return []
    variants = [nick]
    cleaned = clean_battle_nickname(nick)
    if cleaned and cleaned != nick:
        variants.append(cleaned)
    # 取"最后一段"(通常昵称在末尾): 按空格/⋇ 切分取最后一段
    # 注意昵称本身可含空格(官网允许), 所以只在与联队名混排时才有意义
    # 保守: 若末尾 token 以常见字母/数字开头且比整串短, 作为候选
    tokens = [t for t in re.split(r"[\s⋇*]+", cleaned) if t]
    if len(tokens) >= 2:
        last = tokens[-1]
        if last not in variants:
            variants.append(last)
    return variants


class BattleNickIndex:
    """对局昵称索引: 建一次, 之后用候选昵称快速查命中。

    匹配规则与 `matches` 一致: 完全相同(忽略大小写) > 子串包含(候选至少 3 字符)。
    为什么要建索引: 黑名单比对时对局昵称有几百个、名单也有几百条, 逐条两两比对
    光字符串处理就要 100ms(实测 300 条 x 201 昵称), 会把界面卡住。
    这里把“变体 → 昵称”查表做好, 再把所有变体拼成一个大字符串做子串匹配(一次 C 层查找),
    同样的量降到约 2ms。
    """

    def __init__(self, nicknames: list[str]) -> None:
        self._owner: dict[str, str] = {}
        for nick in nicknames:
            for v in battle_nickname_variants(nick):
                v = v.lower()
                if v:
                    self._owner.setdefault(v, nick)
        # \n 分隔, 防止子串匹配跨昵称误命中
        self._joined = "\n" + "\n".join(self._owner) + "\n"

    def match(self, candidates: list[str]) -> str | None:
        """返回命中的对局昵称(原样), 未命中返回 None。"""
        for cand in candidates:
            c = (cand or "").strip().lower()
            if not c:
                continue
            hit = self._owner.get(c)          # 完全相同(忽略大小写)
            if hit is not None:
                return hit
            if len(c) >= 3 and c in self._joined:
                # 子串命中(候选是昵称的一部分): 很少见, 这时才回头找是哪条
                for v, owner in self._owner.items():
                    if c in v:
                        return owner
        return None


def matches(nick_candidates: list[str], battle_nick: str) -> str | None:
    """把黑名单候选昵称(清洗后)与单个游戏内昵称做匹配。

    返回命中的游戏内昵称(原样), 未命中返回 None。
    匹配优先级: 完全相等 > 忽略大小写相等 > 子串包含。
    """
    battle = battle_nick.strip()
    if not battle:
        return None
    battle_lower = battle.lower()
    for cand in nick_candidates:
        c = (cand or "").strip()
        if not c:
            continue
        if c == battle:
            return battle
        cl = c.lower()
        if cl == battle_lower:
            return battle
        # 子串匹配: 候选昵称是游戏内昵称的子串(处理"联队名+平台标识+昵称"混排)
        # 要求候选至少 3 字符, 避免单字符误报
        if len(cl) >= 3 and cl in battle_lower:
            # 排除: 候选只是联队名等短前缀的一部分
            return battle
    return None
