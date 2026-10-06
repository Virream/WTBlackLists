# -*- coding: utf-8 -*-
"""把试车场换挡实验数据生成 Excel 速度-时间折线图。

数据来源: 2026-08-28 试车场实测(8111 /indicators 的 speed 字段, 挡位变化事件采样)。
- 不用空挡 3 次 + 用空挡 3 次
- 每次实验从"停车"开始计时(相对 t=0), 含加速到极速 → 减速 → 倒车全段
"""
import os

from openpyxl import Workbook
from openpyxl.chart import Reference, ScatterChart, Series
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "WTBlackList_gear_report.xlsx")

# (名称, 分组, [(相对时间秒, 速度km/h), ...])
EXPERIMENTS = [
    ("不用空挡 #1", "no_neutral", [
        (0.00, 0), (3.20, 7), (4.26, 19), (5.32, 25), (6.39, 31), (8.51, 40),
        (10.64, 46), (13.83, 54), (27.71, 2), (28.77, -12), (29.83, -20),
        (41.55, -5), (42.61, 0)]),
    ("不用空挡 #2", "no_neutral", [
        (0.00, 4), (1.07, 15), (2.13, 23), (4.26, 35), (5.32, 39), (7.45, 45),
        (10.65, 54), (24.48, 3), (25.55, -10), (26.62, -20), (64.95, -14),
        (66.02, 0)]),
    ("不用空挡 #3", "no_neutral", [
        (0.00, 14), (1.07, 22), (3.20, 34), (4.26, 38), (6.39, 45), (10.65, 55),
        (23.43, 32), (24.50, 0), (25.56, -14), (26.63, -21), (41.55, -14),
        (42.62, -1)]),
    ("用空挡 #1", "neutral", [
        (0.00, 0), (4.26, 7), (5.32, 18), (6.39, 25), (7.45, 31), (9.58, 40),
        (11.71, 46), (14.90, 54), (28.76, 52), (30.89, -10), (31.95, -20),
        (68.17, -4), (69.24, 0)]),
    ("用空挡 #2", "neutral", [
        (0.00, 3), (1.07, 15), (2.13, 22), (4.26, 34), (5.33, 38), (7.46, 45),
        (11.72, 56), (23.44, 59), (24.50, 22), (25.57, -7), (26.63, -19),
        (27.70, -25), (55.39, -6), (56.45, 0)]),
    ("用空挡 #3", "neutral", [
        (0.00, 7), (1.07, 19), (2.13, 25), (3.20, 31), (5.33, 40), (7.46, 47),
        (10.66, 54), (23.45, 57), (24.51, 19), (25.58, -8), (26.64, -20),
        (27.70, -26), (40.50, -9), (41.56, 0)]),
]

# 每次实验: (极速时刻(相对s), 速度变负时刻(相对s))
SUMMARY = [
    ("不用空挡 #1", 13.83, 28.77),
    ("不用空挡 #2", 10.65, 25.55),
    ("不用空挡 #3", 10.65, 25.56),
    ("用空挡 #1", 14.90, 30.89),
    ("用空挡 #2", 11.72, 25.57),
    ("用空挡 #3", 10.66, 25.58),
]

FILL_HDR = PatternFill("solid", fgColor="2F5496")
FONT_HDR = Font(color="FFFFFF", bold=True)


def write_sheet(wb, name, group, pts):
    ws = wb.create_sheet(name)
    ws["A1"] = "时间(秒, 从停车起)"
    ws["B1"] = "速度(km/h)"
    ws["C1"] = f"分组: {'用空挡' if group == 'neutral' else '不用空挡'}"
    for c in ("A1", "B1", "C1"):
        ws[c].fill = FILL_HDR
        ws[c].font = FONT_HDR
    for i, (t, v) in enumerate(pts, start=2):
        ws.cell(row=i, column=1, value=t)
        ws.cell(row=i, column=2, value=v)
    n = len(pts)
    # 速度-时间折线图(散点折线, 数值 x 轴)
    sc = ScatterChart()
    sc.title = f"{name} 速度-时间"
    sc.style = 13
    sc.x_axis.title = "时间 (秒)"
    sc.y_axis.title = "速度 (km/h)"
    sc.x_axis.scaling.min = 0
    sc.y_axis.scaling.min = -30
    sc.y_axis.scaling.max = 70
    sc.height = 12
    sc.width = 24
    xv = Reference(ws, min_col=1, min_row=2, max_row=n + 1)
    yv = Reference(ws, min_col=2, min_row=2, max_row=n + 1)
    s = Series(yv, xv, title=f"{name} 速度")
    s.marker.symbol = "circle"
    s.marker.size = 5
    s.smooth = False
    sc.series.append(s)
    ws.add_chart(sc, "D2")
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 26


def write_summary(wb):
    ws = wb.create_sheet("汇总-极速到倒车耗时")
    headers = ["实验", "分组", "极速时刻(相对s)", "速度变负时刻(相对s)", "极速→开始倒车耗时(s)"]
    for j, h in enumerate(headers, start=1):
        c = ws.cell(row=1, column=j, value=h)
        c.fill = FILL_HDR
        c.font = FONT_HDR
    for i, (name, tpeak, tneg) in enumerate(SUMMARY, start=2):
        group = "用空挡" if "空挡" in name else "不用空挡"
        ws.cell(row=i, column=1, value=name)
        ws.cell(row=i, column=2, value=group)
        ws.cell(row=i, column=3, value=tpeak)
        ws.cell(row=i, column=4, value=tneg)
        ws.cell(row=i, column=5, value=round(tneg - tpeak, 1))
    for j, h in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(j)].width = 24
    ws.freeze_panes = "A2"


def main() -> int:
    wb = Workbook()
    wb.remove(wb.active)
    for name, group, pts in EXPERIMENTS:
        write_sheet(wb, name, group, pts)
    write_summary(wb)
    wb.save(OUT)
    print("已生成:", OUT)
    return 0


if __name__ == "__main__":
    main()
