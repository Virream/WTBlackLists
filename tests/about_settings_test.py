"""关于对话框(白字/阴影/分段) + first_run + 按钮改名验证。"""
import os
import re
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QLabel,
    QPushButton,
)

from wt81111g import about_dialog
from wt81111g.about_dialog import AboutDialog, _DESC
from wt81111g.main_window import MainWindow
from wt81111g.settings import AppSettings


def main() -> int:
    app = QApplication([])

    # 1. 关于对话框(免责声明句保留 + 新说明 + 无客服对话链接)
    dlg = AboutDialog()
    label = dlg.findChildren(QLabel)[0]
    txt = label.text()
    assert "color:#ffffff" in txt, "白字缺失"
    assert "&emsp;&emsp;" in txt, "制表符缩进缺失"
    assert "点击查看作者" not in txt, "客服对话链接应已删除"
    assert "使用本软件造成的任何不良后果均由用户承担" in txt, "免责声明句应保留"
    assert "8111" in _DESC, "应含我生成的8111说明"
    n_semi = len(re.findall(r"[;；]", _DESC))
    n_paras = txt.count('<p style="margin:4px 0; color:#ffffff;">&emsp;&emsp;')
    assert n_paras == n_semi + 1, (n_paras, n_semi)  # 分句段数(无额外链接段)
    effect = label.graphicsEffect()
    assert isinstance(effect, QGraphicsDropShadowEffect), "阴影缺失"
    print(f"about dialog OK: paras={n_paras} shadow={type(effect).__name__}")

    # 4. first_run 持久化
    td = tempfile.mkdtemp()
    s = AppSettings(os.path.join(td, "c.json"))
    assert s.first_run is True
    s.first_run = False
    s.save()
    s2 = AppSettings(os.path.join(td, "c.json"))
    assert s2.first_run is False
    print("first_run persistence OK")

    # 5. 数据维护区按钮齐全
    win = MainWindow(os.path.join(tempfile.mkdtemp(), "bl.json"), start_monitor=False)
    texts = {b.text() for b in win.findChildren(QPushButton)}
    assert "🔁 刷新昵称" in texts, "缺少刷新昵称按钮"
    assert "🔄 检查更新" in texts, "缺少检查更新按钮"
    print("buttons OK")

    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
