"""叠加层: 原因显示 / 多条轮换 / 自定义文本 专项测试(离屏)。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication

from wt81111g.overlay import OverlayWindow
from wt81111g.settings import OverlaySettings

results = []


def _plain(html: str) -> str:
    return (html.replace('<span style="color:#e0e0e0;">', "")
                .replace("</span>", "").replace("<br>", " | "))


def test_defaults_and_multi_rotate() -> None:
    s = OverlaySettings()
    assert s.show_reason is True
    assert s.text_found == "发现肃反人员" and s.text_checking == "正在确认名单中..."
    ov = OverlayWindow(s)
    ov.set_battle(True)
    ov.set_found([("玩家甲", "TK(空对地)"), ("玩家乙", "辱骂玩家")])
    html = ov._label.text()
    assert "发现肃反人员" in html and "玩家甲" in html and "TK(空对地)" in html, _plain(html)
    assert ov._rotate_timer.isActive(), "多条应启动轮换"
    ov._rotate()
    html2 = ov._label.text()
    assert "玩家乙" in html2 and "辱骂玩家" in html2, _plain(html2)
    results.append("multi rotate + reason OK")


def test_single_no_rotate() -> None:
    s = OverlaySettings()
    ov = OverlayWindow(s)
    ov.set_battle(True)
    ov.set_found([("玩家甲", "TK(空对地)")])
    assert not ov._rotate_timer.isActive(), "单条不应轮换"
    results.append("single no rotate OK")


def test_hide_reason() -> None:
    s = OverlaySettings()
    s.show_reason = False
    ov = OverlayWindow(s)
    ov.set_battle(True)
    ov.set_found([("玩家甲", "TK(空对地)")])
    html = ov._label.text()
    assert "TK(空对地)" not in html and "玩家甲" in html, _plain(html)
    results.append("hide reason OK")


def test_custom_texts() -> None:
    s = OverlaySettings()
    s.text_found = "危险人物"
    s.text_checking = "正在扫描..."
    ov = OverlayWindow(s)
    ov.set_battle(True)
    ov.set_found([])
    assert "正在扫描..." in ov._label.text(), ov._label.text()
    ov.set_found([("玩家甲", "TK(空对地)")])
    assert "危险人物" in ov._label.text(), ov._label.text()
    results.append("custom texts OK")


def test_str_compat() -> None:
    s = OverlaySettings()
    ov = OverlayWindow(s)
    ov.set_battle(True)
    ov.set_found(["纯昵称"])
    assert "纯昵称" in ov._label.text(), ov._label.text()
    results.append("str compat OK")


def test_dialog_widgets() -> None:
    from PyQt6.QtWidgets import QCheckBox, QLineEdit

    from wt81111g.overlay_settings_dialog import OverlaySettingsDialog

    s = OverlaySettings()
    dlg = OverlaySettingsDialog(s)
    assert isinstance(dlg.show_reason_check, QCheckBox)
    assert isinstance(dlg.text_found_edit, QLineEdit)
    assert isinstance(dlg.text_checking_edit, QLineEdit)
    dlg.text_found_edit.setText("改了标题")
    assert s.text_found == "改了标题"
    dlg.close()
    results.append("dialog widgets OK")


def main() -> int:
    app = QApplication([])
    for fn in (
        test_defaults_and_multi_rotate,
        test_single_no_rotate,
        test_hide_reason,
        test_custom_texts,
        test_str_compat,
        test_dialog_widgets,
    ):
        fn()
    for r in results:
        print("OK:", r)
    print("OVERLAY TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
