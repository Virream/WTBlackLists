"""关于对话框: 软件说明与作者信息(暗色主题, 白色字体+阴影), 以及作者与Gaijin客服对话窗口。"""
from __future__ import annotations

import html
import re

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QDialog,
    QGraphicsDropShadowEffect,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

_AUTHOR_URL = "https://space.bilibili.com/140035845"
_TOS_URL = "https://legal.gaijin.net/termsofservice"

_DESC = (
    "使用本软件造成的任何不良后果均由用户承担,本软件理论上不会造成封号,"
    "如您仍有担忧或不认可上述文字请关闭软件并删除本软件;"
    "在使用本软件时请确保WT Live访问通畅,WT Live用于根据名单中的玩家ID获取玩家最新昵称,"
    "防止黑名单中的玩家修改昵称后匹配不到目标,默认每24小时从WT Live更新一次信息;"
    "建议在条目对应的证据文件夹内存放事发时的截图、游戏录屏、游戏Replay文件等信息作为证据方便日后取用,"
    "证据文件夹默认生成在软件根目录的evidences文件夹下以用户ID进行一次分类以条目ID进行二次分类,"
    "条目ID根据玩家ID与事件发生日期动态生成,请在设置完毕这两个项目后不要随意更改"
)

# 作者向 Gaijin 客服的提问
_QUESTION = (
    "尊敬的管理员你好:\n"
    "我利用DeepSeek花了3小时左右做出来了一个战雷黑名单管理系统,我有数个疑问需要进行请教,"
    "但在我问这些问题之前我需要先来简单介绍一下我做的工具,它的功能与运行逻辑是这样的:"
    "用户可以手动添加在名单中添加昵称,ID等信息,在游戏进入对局时此应用会利用8111端口获取对局中的玩家昵称并临时存储"
    "然后与名单中的昵称和ID进行比对,如果发现了黑名单中的玩家就在屏幕上提示用户发现了黑名单中的XXX;"
    "如何通过8111获取的玩家昵称?由于8111端口无法在游戏开局获取所有玩家的昵称所以应用会解析8111端口的玩家击杀列表和发言记录逐步获取玩家昵称;"
    "如何对黑名单中的玩家进行比对?应用比对的是昵称,但为了防止黑名单中的玩家更改昵称后无法找到此人,"
    "所以应用会在比对昵称之前先访问\"https://live.warthunder.com/user/玩家ID/\"来获取此玩家当前的昵称然后再进行比对;"
    "如何在屏幕上提示用户?我让AI分析了WTRTI的代码,在屏幕上绘制文字的逻辑与其一致理论上不会触发反作弊;"
    "我想要知道这是否符合8111端口的使用规定?这是否属于作弊从而导致使用这个软件的玩家账号封禁?"
    "使用此软件对\"https://live.warthunder.com/user/玩家ID/\"进行HTML解析获取玩家昵称的行为是否合法?"
    "如果需要的话我将附上源码以供审查\n祝您生活愉快"
)

# Gaijin 客服的回复
_ANSWER_INTRO = (
    "Hello,\n\nThank you for contacting our Customer Support.\n\n"
    "Please read our Terms of service: "
)
_ANSWER_TOS = """3.2.3. No Malicious or Deceptive Use:

- modify the server code (including the use of cheats, hacks, or similar tools);

- exploit any flaws or bugs in the Game(s)' mechanics (exploits, bugs, etc.);

- use software (such as bots or mods) that automates gameplay, alters the game's mechanics or functionality, provides an unfair advantage over other players who do not use such software, or otherwise disrupts the intended game experience for you or any other player;

- employ any third-party software that interferes with the Game(s) in any way, unless explicitly authorized by Gaijin for that specific Game(s) or related Service(s), or unless permitted by law as a mandatory exception;

- damage, disable, or assist in damaging or disabling any computer or server supporting the Game(s) and related Service(s). This includes uploading files containing malicious code (such as viruses, spyware, trojans, worms, or corrupted data) that may harm or disrupt the Game(s) and related Service(s). You must not engage in or support any form of cyberattack, including but not limited to denial-of-service attacks, or otherwise attempt to interfere with or disrupt the operation of the Game(s) or related Service(s);

- use the Game(s) in violation of any applicable laws or regulations, whether knowingly or unintentionally, or contribute to violating any such laws or regulations;

- offer, promote, distribute, or otherwise make available any of the above-described malicious or deceptive actions, whether through the Game(s) itself or by any other means."""
_ANSWER_ENDING = "Best regards,\n\nSupport Specialist (ISL)"


def _esc_br(text: str) -> str:
    return html.escape(text).replace("\n", "<br>")


def _desc_paragraphs() -> str:
    """按分号分句, 每段以制表符宽度缩进; 在'请关闭软件并删除本软件'段后插入客服对话链接。"""
    parts = [p.strip() for p in re.split(r"[;；]", _DESC) if p.strip()]
    blocks = [
        f'<p style="margin:4px 0; color:#ffffff;">&emsp;&emsp;{p}</p>' for p in parts
    ]
    link_block = (
        '<p style="margin:4px 0; color:#ffffff;">&emsp;&emsp;'
        '<a href="about:conversation" style="color:#7ec8e3; text-decoration:underline;">'
        '点击查看作者与gaijin客服的对话</a></p>'
    )
    blocks.insert(1, link_block)
    return "\n".join(blocks)


class ConversationDialog(QDialog):
    """作者与 Gaijin 客服的对话窗口(只读, 仅有关闭功能)。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("作者与Gaijin客服的对话")
        self.setModal(True)
        self.setMinimumSize(560, 640)
        self.resize(620, 720)
        self.setStyleSheet("QDialog { background-color: #1e1e2e; }")

        question_html = _esc_br(_QUESTION)
        answer_html = (
            f'<div>{_esc_br(_ANSWER_INTRO)}'
            f'<a href="{_TOS_URL}" style="color:#7ec8e3;">{_TOS_URL}</a></div>'
            f'<blockquote style="background:#2a2a44; border-left:4px solid #7ec8e3; '
            f'margin:10px 0; padding:10px 14px; border-radius:6px; color:#c8c8d8;">'
            f'{_esc_br(_ANSWER_TOS)}</blockquote>'
            f'<div>{_esc_br(_ANSWER_ENDING)}</div>'
        )
        doc = f"""
        <div style="color:#e0e0e0; font-size:13px;">
          <h4 style="color:#ffffff; margin:6px 0 10px 0;">我的提问</h4>
          <div style="background:#23233a; border-radius:8px; padding:10px 14px; line-height:1.65;">{question_html}</div>
          <hr style="border:0; border-top:1px solid #44475a; margin:16px 0;"/>
          <h4 style="color:#ffffff; margin:6px 0 10px 0;">Gaijin 客服回复</h4>
          <div style="background:#23233a; border-radius:8px; padding:10px 14px; line-height:1.65;">{answer_html}</div>
        </div>
        """

        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setHtml(doc)
        browser.setStyleSheet(
            "QTextBrowser { background:#1a1a2e; border:1px solid #3a3a5c; "
            "border-radius:8px; color:#e0e0e0; font-size:13px; }"
        )

        close_btn = QPushButton("关闭")
        close_btn.setStyleSheet(
            "QPushButton { background:#3a3a5c; color:#ffffff; border:1px solid #55557a; "
            "border-radius:6px; padding:6px 26px; }"
            "QPushButton:hover { background:#4a4a6c; }"
        )
        close_btn.clicked.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(browser)
        layout.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignHCenter)


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
        if href == "about:conversation":
            ConversationDialog(self).exec()
        else:
            QDesktopServices.openUrl(QUrl(href))
