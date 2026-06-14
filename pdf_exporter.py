"""
PDF 导出模块 - 精简版：日期 / 报销人 / 品类 / 金额 + 汇总统计
"""

import os
import sys
from datetime import datetime
from fpdf import FPDF

FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "simhei.ttf")


def _truncate(pdf, text, max_width_mm):
    """截断文本使其不超出 max_width_mm，超长尾部加 '…'"""
    if pdf.get_string_width(text) <= max_width_mm:
        return text
    while len(text) > 1 and pdf.get_string_width(text + "…") > max_width_mm:
        text = text[:-1]
    return text + "…"


class ExpensePDF(FPDF):
    def __init__(self):
        super().__init__("L", "mm", "A4")
        self.add_font("CJK", "", FONT_PATH, uni=True)
        self.set_auto_page_break(True, 15)

    def header(self):
        if self.page_no() == 1:
            self.set_font("CJK", "", 18)
            self.cell(0, 12, "报销统计报表", align="C", ln=1)
            self.set_font("CJK", "", 10)
            self.set_text_color(128, 128, 128)
            self.cell(0, 6, datetime.now().strftime("%Y-%m-%d %H:%M"), align="C", ln=1)
            self.set_text_color(0, 0, 0)
            self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font("CJK", "", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"第 {self.page_no()} 页", align="C")


def export_to_pdf(expenses: list[dict], output_path: str = None) -> str:
    if output_path is None:
        if getattr(sys, 'frozen', False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(base, f"报销统计_{timestamp}.pdf")

    pdf = ExpensePDF()
    pdf.add_page()
    _write_detail_table(pdf, expenses)
    pdf.add_page()
    _write_summary(pdf, expenses)
    pdf.output(output_path)
    return output_path


def _write_detail_table(pdf, expenses):
    col_widths = [50, 65, 65, 55]
    headers = ["日期", "报销人", "品类", "金额（元）"]
    header_bg = (68, 114, 196)

    # 预计算每列内容最大宽度，截断文本
    pdf.set_font("CJK", "", 9)
    cell_padding = 4  # 左右各2mm

    def fit(val, w):
        return _truncate(pdf, str(val), w - cell_padding)

    # 表头
    pdf.set_font("CJK", "", 11)
    pdf.set_fill_color(*header_bg)
    pdf.set_text_color(255, 255, 255)
    for h, w in zip(headers, col_widths):
        pdf.cell(w, 10, h, border=1, fill=True, align="C")
    pdf.ln()

    pdf.set_font("CJK", "", 9)
    pdf.set_text_color(0, 0, 0)
    total = 0
    fill = False
    for exp in expenses:
        if fill:
            pdf.set_fill_color(240, 245, 250)
        else:
            pdf.set_fill_color(255, 255, 255)
        amount = exp.get("amount", 0)
        row = [
            fit(exp.get("date", ""), col_widths[0]),
            fit(exp.get("person", ""), col_widths[1]),
            fit(exp.get("category", ""), col_widths[2]),
            f'{amount:,.2f}',
        ]
        for val, w in zip(row, col_widths):
            pdf.cell(w, 8, val, border=1, fill=True, align="C")
        pdf.ln()
        total += amount
        fill = not fill

    # 合计行
    merge_w = sum(col_widths[:3])
    pdf.set_font("CJK", "", 11)
    pdf.set_fill_color(255, 230, 200)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(merge_w, 10, f"合计（{len(expenses)} 笔）", border=1, fill=True, align="R")
    pdf.set_text_color(200, 0, 0)
    pdf.cell(col_widths[3], 10, f'{total:,.2f}', border=1, fill=True, align="C")
    pdf.set_text_color(0, 0, 0)
    pdf.ln()


def _write_summary(pdf, expenses):
    total_amount = sum(e.get("amount", 0) for e in expenses)

    # --- 按人员统计 ---
    pdf.set_font("CJK", "", 14)
    pdf.set_text_color(31, 78, 121)
    pdf.cell(0, 12, "按人员统计", align="L", ln=1)
    pdf.set_text_color(0, 0, 0)

    person_stats = {}
    for e in expenses:
        p = e.get("person", "未知")
        person_stats.setdefault(p, {"count": 0, "total": 0})
        person_stats[p]["count"] += 1
        person_stats[p]["total"] += e.get("amount", 0)
    person_sorted = sorted(person_stats.items(), key=lambda x: x[1]["total"], reverse=True)

    p_cols = [60, 30, 55, 40]
    p_headers = ["报销人", "笔数", "合计金额（元）", "占比"]
    pdf.set_font("CJK", "", 10)
    pdf.set_fill_color(91, 155, 213)
    pdf.set_text_color(255, 255, 255)
    for h, w in zip(p_headers, p_cols):
        pdf.cell(w, 9, h, border=1, fill=True, align="C")
    pdf.ln()

    pdf.set_font("CJK", "", 9)
    pdf.set_text_color(0, 0, 0)
    fill = False
    for person, stat in person_sorted:
        pdf.set_fill_color(240 if fill else 255, 245 if fill else 255, 250 if fill else 255)
        pct = (stat["total"] / total_amount * 100) if total_amount > 0 else 0
        row = [person, str(stat["count"]), f'{stat["total"]:,.2f}', f"{pct:.1f}%"]
        for val, w in zip(row, p_cols):
            pdf.cell(w, 8, val, border=1, fill=True, align="C")
        pdf.ln()
        fill = not fill

    pdf.ln(8)

    # --- 按品类统计 ---
    pdf.set_font("CJK", "", 14)
    pdf.set_text_color(55, 86, 35)
    pdf.cell(0, 12, "按品类统计", align="L", ln=1)
    pdf.set_text_color(0, 0, 0)

    cat_stats = {}
    for e in expenses:
        c = e.get("category", "其他")
        cat_stats.setdefault(c, {"count": 0, "total": 0})
        cat_stats[c]["count"] += 1
        cat_stats[c]["total"] += e.get("amount", 0)
    cat_sorted = sorted(cat_stats.items(), key=lambda x: x[1]["total"], reverse=True)

    c_cols = [60, 30, 55, 40]
    c_headers = ["品类", "笔数", "合计金额（元）", "占比"]
    pdf.set_font("CJK", "", 10)
    pdf.set_fill_color(112, 173, 71)
    pdf.set_text_color(255, 255, 255)
    for h, w in zip(c_headers, c_cols):
        pdf.cell(w, 9, h, border=1, fill=True, align="C")
    pdf.ln()

    pdf.set_font("CJK", "", 9)
    pdf.set_text_color(0, 0, 0)
    fill = False
    for cat, stat in cat_sorted:
        pdf.set_fill_color(240 if fill else 255, 245 if fill else 255, 250 if fill else 255)
        pct = (stat["total"] / total_amount * 100) if total_amount > 0 else 0
        row = [cat, str(stat["count"]), f'{stat["total"]:,.2f}', f"{pct:.1f}%"]
        for val, w in zip(row, c_cols):
            pdf.cell(w, 8, val, border=1, fill=True, align="C")
        pdf.ln()
        fill = not fill
