# -*- coding: utf-8 -*-
"""关于窗口重构 + 版本更新检测 + 连接检测区 验证(offscreen)。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication, QPushButton

from wt81111g import about_dialog, update_check
from wt81111g.main_window import MainWindow
from wt81111g.update_dialog import UpdateDialog

_APP = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def main() -> int:
    _app()

    # 1) 关于窗口: 免责声明句保留 + 新说明 + Gaijin 对话已删除
    assert "使用本软件造成的任何不良后果均由用户承担" in about_dialog._DESC
    assert "gaijin" not in about_dialog._DESC.lower(), "不应再含 gaijin 说明"
    assert "点击查看作者" not in about_dialog._desc_paragraphs(), "对话链接应删除"
    assert "8111" in about_dialog._DESC, "应含我生成的8111说明"
    assert not hasattr(about_dialog, "ConversationDialog"), "Gaijin 对话窗口应删除"
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "wt81111g", "about_dialog.py"), encoding="utf-8").read()
    assert "<hr" in src, "分割线应保留"
    assert "战争雷霆黑名单助手" in src and "切真Viream" in src, "分割线下作者信息应保留"
    dlg = about_dialog.AboutDialog(None)
    dlg.close()

    # 2) update_check: 版本解析
    assert update_check.parse_version("v2.0.1") == (2, 0, 1)
    assert update_check.parse_version("2.0") == (2, 0)
    assert update_check.parse_version("") == ()

    # 3) main_window: 连接检测区控件 + 检查更新按钮 + 代理标示
    d = tempfile.mkdtemp()
    win = MainWindow(os.path.join(d, "bl.json"), start_monitor=False)
    win.show()
    for attr in ("conn_label", "feed_label", "wtlive_label",
                 "github_label", "proxy_state_label"):
        assert hasattr(win, attr), f"缺少 {attr}"
    texts = {b.text() for b in win.findChildren(QPushButton)}
    assert "🔄 检查更新" in texts, "缺少检查更新按钮"
    assert win._update_btn is not None and win._update_btn.text() == "🔄 检查更新"
    assert "代理: 未开启" in win.proxy_state_label.text(), win.proxy_state_label.text()

    # 4) 连接检测标签: 可点击 + 初始'点击检测' + 检测后冒号后显示延迟
    from wt81111g.main_window import _ClickableLabel
    assert "点击检测" in win.feed_label.text(), win.feed_label.text()
    assert "点击检测" in win.github_label.text()
    assert isinstance(win.feed_label, _ClickableLabel)
    assert isinstance(win.github_label, _ClickableLabel)
    win._on_github_checked(True, 0.35)
    assert "GitHub 访问: 350ms" in win.github_label.text(), win.github_label.text()
    win._on_github_checked(False, 0.0)
    assert "不可达" in win.github_label.text()
    win._on_feed_status("good", "350ms")
    assert "WT Live 访问: 350ms" in win.feed_label.text(), win.feed_label.text()

    from wt81111g.proxy_config import set_proxy
    set_proxy("127.0.0.1:7890")
    win._refresh_proxy_state()
    assert "已开启" in win.proxy_state_label.text(), win.proxy_state_label.text()
    set_proxy("")
    win._refresh_proxy_state()
    assert "未开启" in win.proxy_state_label.text()

    # 5) UpdateDialog 可构造
    ud = UpdateDialog({
        "version": "2.0.1", "current": "2.0.0", "body": "更新日志",
        "html_url": "https://github.com", "download_url": "",
    }, None)
    assert "2.0.1" in ud.windowTitle()
    ud.close()

    print("UPDATE ABOUT TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
