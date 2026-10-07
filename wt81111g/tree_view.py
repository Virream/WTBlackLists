"""侧栏用的树控件: 统一背景色 + VS Code 风格的层级竖线。

Qt 默认靠**交替行底色**区分行, 但那是"隔一行一个色", 跟层级毫无关系 ——
四层结构(地图 → 模式 → 版本 → 轨迹)看起来就是一片深灰浅灰交错, 分不清
谁是谁的子项。这里把它换成**统一背景 + 每层一条竖线**(像 VS Code 的侧栏):

- 关掉交替条纹(`setAlternatingRowColors(False)`);
- 重写 `drawBranches()`: Qt 会把某一行的**分支区**(从控件左边到该条目缩进处
  的矩形)交给它画, 于是我们按"每个祖先一层"在那一格缩进的正中画一条竖线,
  再把展开箭头交给基类画在最右边那格里(竖线不会盖住箭头)。
"""
from __future__ import annotations

from PyQt6.QtCore import QModelIndex, QRect
from PyQt6.QtGui import QPainter, QPalette
from PyQt6.QtWidgets import QTreeWidget

# 竖线颜色 = 文字色 + 这个透明度: 深浅主题下都看得见, 但不抢眼
LINE_ALPHA = 70


class RouteTreeWidget(QTreeWidget):
    """带层级竖线的树(侧栏专用)。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        # 统一背景: 交替条纹在浅色主题下是灰白相间, 层级更难读
        self.setAlternatingRowColors(False)

    def drawBranches(self, painter: QPainter, rect: QRect, index: QModelIndex) -> None:
        super().drawBranches(painter, rect, index)   # 展开箭头/背景由基类画
        depth = self._depth(index)
        if depth <= 0:
            return                                  # 顶层条目没有祖先, 不用画
        indent = max(1, self.indentation())
        color = self.palette().color(QPalette.ColorRole.WindowText)
        color.setAlpha(LINE_ALPHA)
        painter.save()
        painter.setPen(color)
        # 第 L 层祖先的那一格缩进是 [indent*L, indent*(L+1)), 竖线画在正中
        for level in range(depth):
            x = rect.x() + indent * level + indent // 2
            painter.drawLine(x, rect.top(), x, rect.bottom())
        painter.restore()

    def _depth(self, index: QModelIndex) -> int:
        """条目层数(顶层=0)。"""
        depth = 0
        parent = index.parent()
        while parent.isValid():
            depth += 1
            parent = parent.parent()
        return depth
