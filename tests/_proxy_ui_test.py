# -*- coding: utf-8 -*-
"""代理配置 + 功能区双排布局 + 浏览器抓取提示 验证(offscreen)。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication, QPushButton

from wt81111g import proxy_config
from wt81111g.browser_capture_dialog import BrowserCaptureDialog
from wt81111g.main_window import MainWindow
from wt81111g.proxy_dialog import ProxyDialog
from wt81111g.settings import AppSettings

_APP = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def main() -> int:
    _app()

    # 1) proxy_config: patch 生效 + proxies 格式
    assert proxy_config._requests.Session.request is not proxy_config._orig_request, \
        "requests.Session.request 应已被 patch"
    proxy_config.set_proxy("127.0.0.1:7890")
    assert proxy_config.get_proxy() == "127.0.0.1:7890"
    assert proxy_config.proxies() == {
        "http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890",
    }
    proxy_config.set_proxy("http://1.2.3.4:8080")
    assert proxy_config.proxies()["https"] == "http://1.2.3.4:8080"
    proxy_config.set_proxy("")
    assert proxy_config.proxies() is None

    # 2) ProxyDialog 保存: 写 settings.proxy + 全局代理生效 + 持久化
    d = tempfile.mkdtemp()
    settings = AppSettings(os.path.join(d, "config.json"))
    dlg = ProxyDialog(settings, None)
    dlg._edit.setText("192.168.1.1:1080")
    dlg._save()
    assert settings.proxy == "192.168.1.1:1080", settings.proxy
    assert proxy_config.get_proxy() == "192.168.1.1:1080"
    s2 = AppSettings(os.path.join(d, "config.json"))
    assert s2.proxy == "192.168.1.1:1080", "proxy 应持久化"
    dlg2 = ProxyDialog(settings, None)
    dlg2._clear()
    assert settings.proxy == "" and proxy_config.get_proxy() == ""

    # 3) main_window: 各功能区按钮齐全
    win = MainWindow(os.path.join(d, "bl.json"), start_monitor=False)
    win.show()
    texts = {b.text() for b in win.findChildren(QPushButton)}
    for t in ["📤 导出", "📥 导入", "🔁 刷新昵称", "🔄 同步服务器名单",
              "🗄 昵称缓存", "🌐 服务器设置", "☁️ 共享昵称表", "⚙ 代理设置",
              "⚙ 叠加层设置", "ℹ 关于", "🧹 未使用证据检测"]:
        assert t in texts, f"缺少按钮: {t}"
    assert win._export_action is not None and win._import_action is not None

    # 4) 浏览器抓取窗口提示包含新文字
    bdlg = BrowserCaptureDialog("123", "旧昵称", None)
    info = bdlg._info.text()
    assert "软件会自动检测页面加载完成并抓取昵称" in info
    assert "卡在人机验证界面通常是网络问题导致" in info, info
    bdlg.close()

    print("PROXY UI TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
