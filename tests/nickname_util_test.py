"""昵称清洗与模糊匹配测试(主机平台 @psn/@live 后缀、联队名前缀)。"""
from __future__ import annotations

import io
import os
import sys

# 兼容 GBK 控制台的重定向放在 main() 入口(见 main), 避免破坏 pytest 捕获
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wt81111g.nickname_util import (
    BattleNickIndex, battle_nickname_variants, clean_battle_nickname,
    clean_wtlive_nickname, matches,
)

results: list[str] = []


def check(name: str, cond: bool) -> None:
    if cond:
        results.append(f"OK: {name}")
    else:
        results.append(f"FAIL: {name}")
        raise AssertionError(name)


def test_clean_wtlive() -> None:
    check("清 @live", clean_wtlive_nickname("Uchpochmak1179@live") == "Uchpochmak1179")
    check("清 @psn", clean_wtlive_nickname("RUNASAPURIZUMU1@psn") == "RUNASAPURIZUMU1")
    check("无后缀原样", clean_wtlive_nickname("VladikaSitxov") == "VladikaSitxov")
    check("中文昵称原样", clean_wtlive_nickname("目击而道存矣") == "目击而道存矣")
    check("@xbox 也清", clean_wtlive_nickname("PlayerOne@xbox") == "PlayerOne")


def test_clean_battle() -> None:
    check("去 ⋇", clean_battle_nickname("⋇Uchpochmak1179") == "Uchpochmak1179")
    check("去 *", clean_battle_nickname("*Uchpochmak1179") == "Uchpochmak1179")
    check("去多个标记", clean_battle_nickname("⋇ *Player") == "Player")
    check("无标记原样", clean_battle_nickname("Player") == "Player")


def test_variants() -> None:
    v = battle_nickname_variants("⋇Uchpochmak1179")
    check("变体含原始", "⋇Uchpochmak1179" in v)
    check("变体含清洗后", "Uchpochmak1179" in v)
    v2 = battle_nickname_variants("SKW Uchpochmak1179")
    check("联队名+昵称 变体含末尾昵称", "Uchpochmak1179" in v2)
    check("联队名+昵称 变体含整串", "SKW Uchpochmak1179" in v2)


def test_matches() -> None:
    # 黑名单候选: WTLive 清洗后的昵称
    cand = [clean_wtlive_nickname("RUNASAPURIZUMU1@psn")]
    check("精确匹配", matches(cand, "RUNASAPURIZUMU1") == "RUNASAPURIZUMU1")
    check("带⋇匹配", matches(cand, "⋇RUNASAPURIZUMU1") == "⋇RUNASAPURIZUMU1")
    check("联队名+昵称 匹配", matches(cand, "联队A RUNASAPURIZUMU1") is not None)
    # 中文昵称
    cand_cn = ["目击而道存矣"]
    check("中文精确", matches(cand_cn, "目击而道存矣") == "目击而道存矣")
    # 大小写不敏感
    check("大小写", matches(["abcDEF"], "ABCDEF") is not None)
    # 防误报: 候选太短不子串匹配
    check("短昵称不误报", matches(["ab"], "labc") is None)
    # 完全不相关
    check("不相关不匹配", matches(["PlayerOne"], "AnotherPlayer") is None)


def test_battle_nick_index() -> None:
    """批量比对索引: 与 matches 同样规则, 但不逐条两两扫(性能)。"""
    idx = BattleNickIndex(["⋇RUNASAPURIZUMU1", "=AZUR2= 目击而道存矣 (豹 2PL)",
                           "SKW Uchpochmak1179", "ThyTree"])
    check("索引: 精确", idx.match(["ThyTree"]) == "ThyTree")
    check("索引: 忽略大小写", idx.match(["thytree"]) == "ThyTree")
    check("索引: 去⋇后匹配", idx.match(["RUNASAPURIZUMU1"]) == "⋇RUNASAPURIZUMU1")
    check("索引: 联队名前缀匹配", idx.match(["Uchpochmak1179"]) == "SKW Uchpochmak1179")
    check("索引: 中文(带联队前缀)匹配",
          idx.match(["目击而道存矣"]) == "=AZUR2= 目击而道存矣 (豹 2PL)")
    check("索引: 子串命中", idx.match(["RUNASAPURIZU"]) == "⋇RUNASAPURIZUMU1")
    check("索引: 太长不误报", idx.match(["RUNASAPURIZUMU1X"]) is None)
    check("索引: 短候选不误报", idx.match(["ab"]) is None)
    check("索引: 不相关不匹配", idx.match(["PlayerOne"]) is None)
    check("索引: 不跨昵称误匹配", idx.match(["ThyTree\n=AZUR2="]) is None)
    check("索引: 空候选", idx.match(["", None]) is None)


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")  # GBK 控制台兼容
    test_clean_wtlive()
    test_clean_battle()
    test_variants()
    test_matches()
    test_battle_nick_index()
    print("\n".join(results))
    print("NICKNAME UTIL TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
