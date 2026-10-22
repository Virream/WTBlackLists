"""「指定观战玩家」对话框 —— 修正摄像机挂着那位玩家的死亡时刻。

============================================================= 为什么要手动指定
录制全局路线时, 镜头必须挂在我方一位玩家身上(8111 才肯报这一方), 于是那位玩家
(唯一 `icon == "Player"` 的线)的死亡时刻拿不到:

* 他阵亡后 8111 不会让 icon 立刻消失 —— 那条线继续上报残骸, 一直画到“他重生
  那一刻”才断, 所以线尾 = 重生时刻, 不是死亡时刻;
* 他若就此退出对局、而比赛还没结束, 残骸会一直上报到录制结束。

`/hudmsg` 的击毁文本里有**双方昵称**(`A (载具) 击毁了 B (载具)`), 只要知道镜头挂在
谁身上, 就能按时间段把他每次阵亡认领到对应的那条线上。而 8111 不告诉我们这件事 ——
只能问用户。本对话框就是从**本局击毁名单**里点选, 避免手打昵称出错(昵称里还夹着
军团前缀和零宽空格)。

=============================================================== 交互
每队一个下拉框: 第一项是「这一队不用修正(他活到录制结束)」, 后面按“他的阵亡命中
了几段 Player 线”排序(命中越多越可能是他) —— 默认**不预选**, 因为同一时刻往往
好几个人在死。下方实时显示推算结果(“修正 2/2 段 ｜ 阵亡 3:28、6:07”), 选错了能
一眼看出来。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFrame, QGridLayout, QLabel,
    QVBoxLayout,
)

from . import kill_timeline as kt
from .route_store import GLOBAL_SIDE_LABELS, GLOBAL_SIDES, fmt_time

_TIP = (
    "8111 不告诉我们录制时镜头挂在谁身上, 而那位玩家阵亡后 8111 仍会继续上报残骸 ——\n"
    "他的线路要到「他重生那一刻」(或整局结束)才断, 所以死亡时刻偏晚。这里从本局\n"
    "击毁名单里点选他, 程序就会用 `/hudmsg` 的击毁时刻逐段把死亡时刻补上。"
)


class WatcherDialog(QDialog):
    """让用户点选 A/B 队录制时的观战对象(两队的名单都来自本局 hudmsg)。"""

    def __init__(self, record, parent=None):
        super().__init__(parent)
        self.setWindowTitle("指定观战玩家 · 修正死亡时刻")
        self.setMinimumWidth(520)
        self._record = record
        self._kills = kt.merge_kills(record)
        self._combos: dict[str, QComboBox] = {}
        self._notes: dict[str, QLabel] = {}
        self._ranked: dict[str, list[dict]] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        head = QLabel(_TIP)
        head.setWordWrap(True)
        head.setStyleSheet("color: #6b7280;")
        outer.addWidget(head)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(line)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        for r, side in enumerate(GLOBAL_SIDES):
            label = QLabel(f"{GLOBAL_SIDE_LABELS[side]}录制时, 镜头挂在这位玩家身上:")
            grid.addWidget(label, r * 2, 0, Qt.AlignmentFlag.AlignTop)
            combo = QComboBox()
            combo.setMinimumWidth(300)
            combo.currentIndexChanged.connect(self._refresh)
            grid.addWidget(combo, r * 2, 1)
            note = QLabel("")
            note.setWordWrap(True)
            note.setStyleSheet("color: #2563eb;")
            grid.addWidget(note, r * 2 + 1, 1)
            self._combos[side] = combo
            self._notes[side] = note
        grid.setColumnStretch(1, 1)
        outer.addLayout(grid)

        self._warn = QLabel("")
        self._warn.setWordWrap(True)
        self._warn.setStyleSheet("color: #b45309;")
        outer.addWidget(self._warn)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用修正")
        self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        outer.addWidget(self._buttons)

        self._fill()
        self._refresh()

    # ------------------------------------------------------------------
    def _fill(self) -> None:
        """按队填下拉框: 第一项是"不修正", 其余按可能性排序。"""
        for side in GLOBAL_SIDES:
            combo = self._combos[side]
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("（这一队不用修正 — 他活到录制结束）", "")
            ranked = kt.rank_watchers(self._record, side, self._kills)
            self._ranked[side] = ranked
            if not kt.player_lines(self._record, side):
                combo.addItem("（这一队没有观战玩家的线路）", "")
                combo.setEnabled(False)
                combo.blockSignals(False)
                continue
            if not self._kills:
                # 旧版本录的条目可能没存过击毁消息 —— 直说该怎么做, 别让人以为是软件坏了
                combo.addItem("（本条目没有存到击毁记录 — 请重录 A/B 队后再修正）", "")
                combo.setEnabled(False)
                combo.blockSignals(False)
                continue
            for row in ranked:
                fixed = [f for f in row.get("fixes") or [] if f.get("ok")]
                strong = bool(row.get("strong"))
                head = "★ " if strong else ("✔ " if fixed else "")
                text = "%s%s（%s）· 阵亡 %d 次" % (
                    head, row["watcher"], row.get("vehicle") or "?",
                    len(row["deaths"]))
                if fixed:
                    text += " · 命中 %d/%d 段" % (len(fixed), row.get("segments") or 0)
                    if strong:
                        text += "(每一条命都对上)"
                else:
                    text += " · 不适用(他的阵亡不在这几条线上)"
                combo.addItem(text, row["watcher"])
                idx = combo.count() - 1
                # 着重显示: ★ = 看上去就是观战对象(粗体+深绿); ✔ = 能修一部分;
                # 其余灰色 —— 用户扫一眼就知道哪些名字值得点。
                if strong:
                    f = QFont()
                    f.setBold(True)
                    combo.setItemData(idx, f, Qt.ItemDataRole.FontRole)
                    combo.setItemData(idx, QBrush(QColor("#047857")),
                                      Qt.ItemDataRole.ForegroundRole)
                elif fixed:
                    combo.setItemData(idx, QBrush(QColor("#1d4ed8")),
                                      Qt.ItemDataRole.ForegroundRole)
                else:
                    combo.setItemData(idx, QBrush(QColor("#9ca3af")),
                                      Qt.ItemDataRole.ForegroundRole)
                combo.setItemData(
                    idx,
                    "阵亡时刻: %s\n%s" % (
                        "、".join(fmt_time(t) for t in row["deaths"][:6]),
                        row.get("note") or ""),
                    Qt.ItemDataRole.ToolTipRole)
            # 只预选**上次确认过的**名字。按"最可能"预选看着方便, 但实测排序只能
            # 把真正的观战对象提到前几个(同一时刻往往好几个人在死), 预选错了用户
            # 直接点确定就会把数据改坏 —— 宁可让他自己扫一眼名单。
            want = (self._record.watchers or {}).get(side) or ""
            idx = combo.findData(want) if want else -1
            if idx > 0:
                combo.setCurrentIndex(idx)
            combo.blockSignals(False)

    def watchers(self) -> dict[str, str]:
        """`{"A": 昵称, "B": 昵称}`(留空 = 这一队不修正)。"""
        return {side: str(self._combos[side].currentData() or "")
                for side in GLOBAL_SIDES}

    def _refresh(self) -> None:
        """选完立刻显示推算结果 —— 选错了用户能一眼看出来。"""
        chosen = self.watchers()
        resolved = kt.resolve_deaths(self._record, chosen, self._kills)
        for side in GLOBAL_SIDES:
            note = self._notes[side]
            name = chosen.get(side) or ""
            if not name:
                note.setText("这一队不修正(保持原样)")
                note.setStyleSheet("color: #6b7280;")
                continue
            row = resolved.get(side) or {}
            fixed = [f for f in row.get("fixes") or [] if f.get("ok")]
            if fixed:
                note.setText(
                    "→ %s修正 %d/%d 段 ｜ 阵亡 %s ｜ 线尾多出来的残骸 %s 会被截掉 ｜ "
                    "这一队时间轴偏移 %+.1f 秒(用其他玩家的 %d 条线对齐)"
                    % ("看上去就是观战对象: " if row.get("strong") else "",
                       len(fixed), row.get("segments") or 0,
                       "、".join(fmt_time(f["death"]) for f in fixed),
                       "、".join("%.1f 秒" % f["tail"] for f in fixed),
                       row.get("shift") or 0.0, row.get("hits") or 0))
                miss = (row.get("segments") or 0) - len(fixed)
                if miss > 0:
                    note.setText(note.text() + "\n⚠ 还有 %d 段没找到他的阵亡记录"
                                 "(hudmsg 里没有) —— 那几段保持原样" % miss)
                note.setStyleSheet("color: #047857;")
            else:
                note.setText("→ 不修正: %s" % (row.get("note") or "无法定位")
                             + "\n(这一队不会有任何改动, 黄色感叹号会保留)")
                note.setStyleSheet("color: #b45309;")
        both = [n for n in chosen.values() if n]
        same = len(both) == 2 and both[0] == both[1]
        self._warn.setText(
            "两队不可能挂在同一位玩家身上(他们属于不同阵营), 请重新选。" if same
            else "")
        ok_btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setEnabled(not same)
