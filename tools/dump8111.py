"""诊断工具: 打印 8111 各端点返回, 用于排查"试车场/对局判定"问题。

用法:
    1) 启动战争雷霆, 进入【试车场】(Test Flight), 保持停留在试车场地图内
    2) 运行:  python tools/dump8111.py
    3) 把终端输出完整贴回给开发助手
"""
from __future__ import annotations

import json
import sys

import requests

BASE = "http://localhost:8111"


def show(name: str, path: str, params: dict | None = None) -> None:
    print(f"===== {name}  ({path}) =====")
    try:
        r = requests.get(BASE + path, params=params, timeout=2)
        print(f"HTTP {r.status_code}")
        if r.status_code != 200:
            print((r.text or "")[:300])
            return
        try:
            data = r.json()
            s = json.dumps(data, ensure_ascii=False, indent=2)
        except ValueError:
            s = r.text
        print(s[:2500])
    except requests.RequestException as exc:
        print("请求失败:", exc)
    print()


def main() -> int:
    print("连接:", BASE, "| Python", sys.version.split()[0])
    print()
    show("STATE", "/state")
    show("MISSION", "/mission.json")
    show("MAP_INFO", "/map_info.json")
    show("MAP_OBJ(截断)", "/map_obj.json")
    show("HUDMSG", "/hudmsg", {"lastEvt": 0, "lastDmg": 0})
    return 0


if __name__ == "__main__":
    sys.exit(main())
