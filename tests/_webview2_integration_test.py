# -*- coding: utf-8 -*-
"""应用内浏览器(WebView2)集成 验证(offscreen)。

覆盖: 子进程入口签名 / 主进程 run_capture 参数构造(mock Popen) /
对话框新增'应用内浏览器'按钮。
"""
import os
import sys
import tempfile
import json
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication, QPushButton

from wt81111g.browser_capture_dialog import BrowserCaptureDialog
from wt81111g.webview2_capture import child_main, run_capture

_APP = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def main() -> int:
    _app()

    # 1) 对话框包含'应用内浏览器'按钮
    dlg = BrowserCaptureDialog("123", "旧昵称", None)
    texts = {b.text() for b in dlg.findChildren(QPushButton)}
    assert "🔷 应用内浏览器" in texts, texts
    assert dlg._wv2_btn is not None
    dlg.close()

    # 2) run_capture: 用 mock Popen 验证子进程命令与结果解析
    current_payload = {"ok": True, "nickname": "Squirlykid14938@live"}

    class _FakePopen:
        def __init__(self, cmd, creationflags=0):
            self._cmd = cmd
            assert "--webview2-capture" in cmd, cmd
            assert "123" in cmd, cmd
            assert cmd[-1].endswith(".json"), cmd

        def wait(self, timeout=None):
            # 模拟子进程把抓取结果写入 outfile
            with open(self._cmd[-1], "w", encoding="utf-8") as f:
                json.dump(current_payload, f)
            return 0

        def kill(self):
            pass

    with mock.patch("wt81111g.webview2_capture.subprocess.Popen", _FakePopen):
        nick, state = run_capture("123")
    assert nick == "Squirlykid14938@live", (nick, state)
    assert "成功" in state, state

    # 3) 未抓到
    current_payload = {"ok": False, "nickname": ""}
    with mock.patch("wt81111g.webview2_capture.subprocess.Popen", _FakePopen):
        nick, state = run_capture("123")
    assert nick is None and "未抓到" in state, (nick, state)

    # 4) child_main 可 import(不运行 GUI)
    assert callable(child_main)

    print("WEBVIEW2 INTEGRATION TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
