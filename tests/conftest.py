# -*- coding: utf-8 -*-
"""pytest 收集配置。

tests/ 下的 _*.py 是带独立 main() 入口的验证脚本(用 python 直接运行,
顶层即执行断言与打印), 并非 pytest 测试。若被 pytest 当作 *_test.py 收集,
会在收集阶段执行顶层逻辑(含 SystemExit / 可能的阻塞), 导致 pytest 卡住。
这里显式忽略所有 _*.py, 让 pytest 只收集真正的 pytest 测试文件。
"""
import os

collect_ignore = [
    f for f in os.listdir(os.path.dirname(__file__))
    if f.startswith("_") and f.endswith(".py")
]
