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

from PyQt6.QtWidgets import QApplication, QCheckBox, QPushButton

from wt81111g.browser_capture_dialog import BrowserCaptureDialog
from wt81111g.settings import AppSettings
from wt81111g.webview2_capture import child_main, run_capture

_APP = None
_last_cmd: list = []


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def main() -> int:
    _app()

    # 1) 对话框: 应用内浏览器为推荐首选, 系统浏览器默认折叠, 含'下次自动打开浏览器'勾选框
    d = tempfile.mkdtemp()
    settings = AppSettings(os.path.join(d, "config.json"))
    dlg = BrowserCaptureDialog("123", "旧昵称", settings, None)
    texts = [b.text() for b in dlg.findChildren(QPushButton)]
    assert any("🔷 应用内浏览器" in t for t in texts), texts
    assert dlg._wv2_btn is not None
    assert dlg._open_btn.isHidden(), "系统浏览器按钮默认应折叠隐藏"
    # 勾选框: 初始未勾选, 勾选后写回 settings 并持久化
    assert isinstance(dlg._auto_check, QCheckBox)
    assert dlg._auto_check.isChecked() is False
    dlg._auto_check.setChecked(True)
    assert settings.auto_browser is True, "勾选应写回 settings"
    s2 = AppSettings(os.path.join(d, "config.json"))
    assert s2.auto_browser is True, "auto_browser 应持久化"
    # 展开/收起
    dlg._toggle_system_browser()
    assert not dlg._open_btn.isHidden(), "展开后应显示系统浏览器按钮"
    dlg._toggle_system_browser()
    assert dlg._open_btn.isHidden(), "再次点击应收起"
    dlg.close()

    # 2) run_capture: 用 mock Popen 验证子进程命令与结果解析
    current_payload = {"ok": True, "nickname": "Squirlykid14938@live"}

    class _FakePopen:
        def __init__(self, cmd, creationflags=0):
            global _last_cmd
            _last_cmd = cmd
            self._cmd = cmd
            assert "--webview2-capture" in cmd, cmd
            assert "123" in cmd, cmd
            assert any(c.endswith(".json") for c in cmd), cmd

        def wait(self, timeout=None):
            # 模拟子进程把抓取结果写入 outfile
            with open(self._cmd[-1], "w", encoding="utf-8") as f:
                json.dump(current_payload, f)
            return 0

        def kill(self):
            pass

    # 普通模式: 不带 --hidden
    with mock.patch("wt81111g.webview2_capture.subprocess.Popen", _FakePopen):
        run_capture("123")
    assert "--hidden" not in _last_cmd
    # 后台模式: 带 --hidden
    with mock.patch("wt81111g.webview2_capture.subprocess.Popen", _FakePopen):
        run_capture("123", hidden=True)
    assert "--hidden" in _last_cmd

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
