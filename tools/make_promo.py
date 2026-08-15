"""生成宣传海报: 结合软件作用 + 应用图标。

输出: WTBlackList_promo.png (1920x1080, 16:9 便于宣传/直播封面/社交媒体)
用 PyQt6 离屏渲染, 复用 512x512 ico.png 图标, 配色呼应图标(深蓝黑 + 蓝 + 警示红)。
运行: .venv\\Scripts\\python.exe tools\\make_promo.py
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QFont, QFontDatabase, QLinearGradient, QPainter,
    QPainterPath, QPen, QPixmap, QRadialGradient,
)
from PyQt6.QtWidgets import QApplication

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON = os.path.join(BASE, "icon", "2.0", "icon_2..0_512x512.png")
OUT = os.path.join(BASE, "WTBlackList_promo.png")

W, H = 1920, 1080

# ---- 配色(呼应图标: 深蓝黑 / 蓝 / 白 / 警示红 / 军橙) ----
C_BG_TOP = QColor("#0c1322")
C_BG_MID = QColor("#101c30")
C_BG_BOT = QColor("#0a1718")
C_BLUE = QColor("#5ab0ff")
C_BLUE_DEEP = QColor("#2b6cb0")
C_RED = QColor("#e0483e")
C_ORANGE = QColor("#e8a33d")
C_WHITE = QColor("#f2f5f9")
C_MUTED = QColor("#9fb0c5")
C_LINE = QColor(90, 176, 255, 90)

# ---- 免费商用字体(无需联网下载) ----
# 得意黑 Smiley Sans(OFL 开源, 免费商用) —— 标题/大字
FONT_SMILEY = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts",
    "SmileySans-Oblique.ttf")
# 小米 MiSans(小米官方免费商用授权) —— 正文
FONT_MISANS = r"C:\Windows\Fonts\MiSans-Regular.otf"

_TITLE_FAMILY = "Microsoft YaHei"
_BODY_FAMILY = "Microsoft YaHei"


def load_fonts() -> None:
    """离屏平台看不到系统字体, 必须显式 addApplicationFont 加载字体文件。

    得意黑 → 标题, MiSans → 正文; 加载失败则回退微软雅黑。
    """
    global _TITLE_FAMILY, _BODY_FAMILY
    for path, is_title in ((FONT_SMILEY, True), (FONT_MISANS, False)):
        if not os.path.exists(path):
            print(f"[fonts] 缺失: {path}")
            continue
        fid = QFontDatabase.addApplicationFont(path)
        fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
        if not fams:
            print(f"[fonts] 加载失败: {path}")
            continue
        family = fams[0]
        if is_title:
            _TITLE_FAMILY = family
        else:
            _BODY_FAMILY = family
        print(f"[fonts] 已加载: {family} <- {os.path.basename(path)}")


def rounded_pixmap(src: QPixmap, radius: float) -> QPixmap:
    """给图片加圆角。"""
    size = min(src.width(), src.height())
    src = src.copy(0, 0, size, size)
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, src)
    p.end()
    return out


def draw_hud_corners(p: QPainter, x0: float, y0: float, x1: float, y1: float,
                     length: float = 46.0, color: QColor = C_LINE) -> None:
    """绘制 HUD 风格四角线框。"""
    pen = QPen(color, 3)
    p.setPen(pen)
    for cx, cy, sx, sy in ((x0, y0, 1, 1), (x1, y0, -1, 1),
                           (x0, y1, 1, -1), (x1, y1, -1, -1)):
        p.drawLine(QPointF(cx, cy), QPointF(cx + sx * length, cy))
        p.drawLine(QPointF(cx, cy), QPointF(cx, cy + sy * length))


def draw_feature(p: QPainter, x: float, y: float, title: str, desc: str,
                 accent: QColor) -> None:
    """功能条目: 菱形标记 + 标题 + 描述。"""
    # 菱形标记
    pen = QPen(accent, 2)
    p.setPen(pen)
    p.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 60))
    d = 22.0
    diamond = QPainterPath()
    diamond.moveTo(x, y)
    diamond.lineTo(x + d / 2, y + d / 2)
    diamond.lineTo(x, y + d)
    diamond.lineTo(x - d / 2, y + d / 2)
    diamond.closeSubpath()
    p.drawPath(diamond)
    # 标题(得意黑)
    f = QFont(_TITLE_FAMILY, 30)
    f.setBold(True)
    p.setFont(f)
    p.setPen(C_WHITE)
    p.drawText(QRectF(x + 44, y - 8, 960, 44), Qt.AlignmentFlag.AlignLeft, title)
    # 描述(MiSans)
    f2 = QFont(_BODY_FAMILY, 21)
    f2.setWeight(QFont.Weight.Normal)
    p.setFont(f2)
    p.setPen(C_MUTED)
    p.drawText(QRectF(x + 44, y + 40, 980, 40), Qt.AlignmentFlag.AlignLeft, desc)


def main() -> int:
    app = QApplication(sys.argv)
    load_fonts()  # 离屏平台必须显式加载字体文件
    icon_src = QPixmap(ICON)
    if icon_src.isNull():
        print("无法加载图标:", ICON)
        return 1

    canvas = QPixmap(W, H)
    canvas.fill(Qt.GlobalColor.transparent)
    p = QPainter(canvas)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

    # ---- 背景: 深色军事渐变 ----
    bg = QLinearGradient(0, 0, 0, H)
    bg.setColorAt(0.0, C_BG_TOP)
    bg.setColorAt(0.55, C_BG_MID)
    bg.setColorAt(1.0, C_BG_BOT)
    p.fillRect(0, 0, W, H, bg)

    # 右上光晕(蓝色, 呼应图标)
    glow = QRadialGradient(W * 0.82, H * 0.15, 520)
    glow.setColorAt(0.0, QColor(90, 176, 255, 70))
    glow.setColorAt(1.0, QColor(90, 176, 255, 0))
    p.fillRect(0, 0, W, H, glow)
    # 左下光晕(军橙/红, 呼应坦克警示)
    glow2 = QRadialGradient(W * 0.15, H * 0.9, 480)
    glow2.setColorAt(0.0, QColor(232, 163, 61, 45))
    glow2.setColorAt(1.0, QColor(232, 163, 61, 0))
    p.fillRect(0, 0, W, H, glow2)

    # 对角细斜线纹理(军事 HUD 质感)
    p.setPen(QPen(QColor(255, 255, 255, 8), 1))
    for k in range(-6, 14):
        x0 = k * 220.0
        p.drawLine(QPointF(x0, 0), QPointF(x0 - H * 0.7, H))

    # 整体 HUD 四角线框
    draw_hud_corners(p, 46, 46, W - 46, H - 46, length=56)

    # 顶部小标签
    tag_f = QFont(_TITLE_FAMILY, 22)
    tag_f.setBold(True)
    p.setFont(tag_f)
    p.setPen(C_BLUE)
    p.drawText(QRectF(90, 78, 900, 40), Qt.AlignmentFlag.AlignLeft,
               "WT8111G  ·  陆战黑名单工具")

    # ---- 主图标(圆角 + 蓝色辉光) ----
    icon = rounded_pixmap(icon_src, 60.0)
    icon_size = 500
    icon_x, icon_y = 200, 240
    # 辉光
    glow_icon = QRadialGradient(icon_x + icon_size / 2, icon_y + icon_size / 2, icon_size * 0.85)
    glow_icon.setColorAt(0.0, QColor(90, 176, 255, 110))
    glow_icon.setColorAt(1.0, QColor(90, 176, 255, 0))
    p.fillRect(QRectF(icon_x - 60, icon_y - 60, icon_size + 120, icon_size + 120), glow_icon)
    # 外框
    p.setPen(QPen(C_BLUE_DEEP, 4))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(icon_x, icon_y, icon_size, icon_size), 60, 60)
    p.drawPixmap(int(icon_x), int(icon_y), icon_size, icon_size, icon)
    # 图标下方软件名(居中于图标, 而不是整个画面)
    name_f = QFont(_TITLE_FAMILY, 40)
    name_f.setBold(True)
    p.setFont(name_f)
    p.setPen(C_WHITE)
    p.drawText(QRectF(icon_x, icon_y + icon_size + 46, icon_size, 60),
               Qt.AlignmentFlag.AlignHCenter, "WTBlackList")

    # ---- 右侧: 标题 + 卖点 ----
    tx = 830.0
    # 大标题
    h1 = QFont(_TITLE_FAMILY, 64)
    h1.setBold(True)
    p.setFont(h1)
    p.setPen(C_WHITE)
    p.drawText(QRectF(tx, 230, 980, 90), Qt.AlignmentFlag.AlignLeft,
               "对局中,一眼认出黑名单玩家")
    # 副标题
    h2 = QFont(_BODY_FAMILY, 30)
    h2.setBold(False)
    p.setFont(h2)
    p.setPen(C_MUTED)
    p.drawText(QRectF(tx, 338, 1000, 50), Qt.AlignmentFlag.AlignLeft,
               "战争雷霆 · 陆战黑名单助手 —— 击杀与发言自动比对,实时告警")

    # 分隔线
    p.setPen(QPen(C_LINE, 2))
    p.drawLine(QPointF(tx, 430), QPointF(tx + 900, 430))

    # 功能列表
    feats = [
        ("实时识别", "对局/试车场中,击杀与发言昵称自动与黑名单比对", C_BLUE),
        ("屏幕叠加层", "命中即置顶提示「发现肃反人员」,离场自动隐藏", C_RED),
        ("曾用昵称追踪", "以玩家ID唯一识别,改过名也照样认出来", C_ORANGE),
        ("导入导出与证据", "一键备份/恢复名单与击杀证据,自动清理失效证据", C_BLUE),
    ]
    fy = 470.0
    for title, desc, accent in feats:
        draw_feature(p, tx, fy, title, desc, accent)
        fy += 135.0

    # ---- 底部信息条 ----
    p.setPen(QPen(QColor(90, 176, 255, 90), 1))
    p.drawLine(QPointF(90, 968), QPointF(W - 90, 968))
    foot = QFont(_BODY_FAMILY, 20)
    p.setFont(foot)
    p.setPen(C_MUTED)
    p.drawText(QRectF(90, 986, W - 180, 44), Qt.AlignmentFlag.AlignHCenter,
               "仅读取官方 8111 遥测接口  ·  不注入 · 不挂钩 · 不读写游戏内存  ·  数据本地保存")

    p.end()

    ok = canvas.save(OUT, "PNG")
    print("已生成:", OUT, "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
