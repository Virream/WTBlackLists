"""关于对话框: 软件说明与作者信息(暗色主题, 白色字体+阴影)。"""
from __future__ import annotations

import re

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QDialog,
    QGraphicsDropShadowEffect,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

_AUTHOR_URL = "https://space.bilibili.com/140035845"

_DESC = (
    "使用本软件造成的任何不良后果均由用户承担,本软件理论上不会造成封号,"
    "如您仍有担忧或不认可上述文字请关闭软件并删除本软件;"
    "本软件通过战争雷霆内置的8111端口读取对局信息,在进入对局时自动与黑名单比对,"
    "发现黑名单玩家时会在屏幕上显示提示,方便您及时做出应对;"
    "通过 War Thunder Live 官方页面按玩家ID获取最新昵称,即使对方改名也能正确匹配,"
    "并会根据官方昵称自动更新名单中的玩家昵称与维护曾用昵称;"
    "支持黑名单条目的导入导出与多人共享(服务器名单 / GitHub 共享昵称表),"
    "并可在数据维护中设置代理,使所有网络请求均通过代理发送;"
    "证据文件按 玩家ID/条目ID 自动归档到软件根目录的 evidences 文件夹,"
    "条目ID由 玩家ID 与事件发生日期 动态生成,请在设置完毕这两个项目后不要随意更改"
)


def _desc_paragraphs() -> str:
    """按分号分句, 每段以制表符宽度缩进。"""
    parts = [p.strip() for p in re.split(r"[;；]", _DESC) if p.strip()]
    return "\n".join(
        f'<p style="margin:4px 0; color:#ffffff;">&emsp;&emsp;{p}</p>' for p in parts
    )


class AboutDialog(QDialog):
    """关于窗口: 上方软件说明(白字+阴影, 分句缩进) + 分割线 + 下方标题与作者。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("关于")
        self.setModal(True)
        self.setFixedWidth(520)
        self.setStyleSheet("QDialog { background-color: #1e1e2e; }")

        html = f"""
        <div style="text-align:center;">
          <h3 style="margin:6px 0 10px 0; color:#ffffff;">软件说明与介绍</h3>
          <div style="text-align:left; font-size:12px; line-height:1.55;">{_desc_paragraphs()}</div>
          <hr style="border:0; border-top:1px solid #44475a; margin:12px 0;"/>
          <p style="font-size:17px; font-weight:bold; margin:12px 0 4px 0; color:#ffffff;">战争雷霆黑名单助手</p>
          <p style="font-size:12px; margin:0 0 8px 0; font-weight:normal; color:#d0d0e0;">
            由<a href="{_AUTHOR_URL}" style="color:#7ec8e3;">切真Viream</a>使用DeepSeek生成
          </p>
        </div>
        """
        label = QLabel(html)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setOpenExternalLinks(False)
        label.setWordWrap(True)
        label.linkActivated.connect(self._on_link)

        # 白色文字 + 阴影, 提升暗色主题下的可读性
        shadow = QGraphicsDropShadowEffect(label)
        shadow.setBlurRadius(5)
        shadow.setOffset(2, 2)
        shadow.setColor(QColor(0, 0, 0, 200))
        label.setGraphicsEffect(shadow)

        close_btn = QPushButton("确定")
        close_btn.setStyleSheet(
            "QPushButton { background:#3a3a5c; color:#ffffff; border:1px solid #55557a; "
            "border-radius:6px; padding:6px 26px; }"
            "QPushButton:hover { background:#4a4a6c; }"
        )
        close_btn.clicked.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(label)
        layout.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignHCenter)

    def _on_link(self, href: str) -> None:
        QDesktopServices.openUrl(QUrl(href))
