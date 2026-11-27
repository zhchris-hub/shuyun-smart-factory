from io import BytesIO

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from factory.store import METRIC_LABELS, UNITS, formatted_time


def _style_document(document):
    section = document.sections[0]
    section.page_height, section.page_width = Cm(29.7), Cm(21)
    section.top_margin = section.bottom_margin = Cm(2.1)
    section.left_margin = section.right_margin = Cm(2.15)
    for name, size in (("Normal", 10.5), ("Title", 24), ("Heading 1", 14)):
        style = document.styles[name]
        style.font.name = "Microsoft YaHei"
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        style.paragraph_format.space_after = Pt(7)
        style.paragraph_format.line_spacing = 1.22
        properties = style.element.get_or_add_pPr()
        border = properties.find(qn("w:pBdr"))
        if border is not None:
            properties.remove(border)


def _table(document, columns, rows, widths):
    table = document.add_table(rows=1, cols=len(columns))
    table.autofit = False
    for column, width in zip(table.columns, widths):
        column.width = Cm(width)
    settings = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        tag = OxmlElement("w:" + edge)
        tag.set(qn("w:val"), "single")
        tag.set(qn("w:sz"), "4")
        tag.set(qn("w:color"), "D9D9D9")
        borders.append(tag)
    settings.append(borders)
    margins = OxmlElement("w:tblCellMar")
    for edge in ("top", "bottom", "left", "right"):
        tag = OxmlElement("w:" + edge)
        tag.set(qn("w:w"), "100")
        tag.set(qn("w:type"), "dxa")
        margins.append(tag)
    settings.append(margins)
    table.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
    for index, values in enumerate([columns, *rows]):
        row = table.rows[0] if index == 0 else table.add_row()
        row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
        for j, (cell, value) in enumerate(zip(row.cells, values)):
            cell.width = Cm(widths[j])
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.space_before = Pt(0)
            run = p.add_run(str(value))
            if index == 0:
                color = OxmlElement("w:shd")
                color.set(qn("w:fill"), "E6EFF2")
                cell._tc.get_or_add_tcPr().append(color)
                run.bold = True
    document.add_paragraph()


def _fmt(value, unit):
    return f"{value:.2f} {unit}" if value is not None else "—"


def build_report(kind: str, summary: dict, name: str, demo: bool, alerts: list[dict]) -> bytes:
    document = Document()
    _style_document(document)
    title = "车间巡检报告" if kind == "inspection" else "设备能耗估算报告"
    document.core_properties.title = title
    document.core_properties.author = "蜀云智造"
    document.add_paragraph(title, style="Title")
    mode = "模拟设备" if demo else "已登记的 ESP32 设备"
    document.add_paragraph(
        f"{summary['day']}（UTC+08:00），统计范围为{name}。"
        f"数据来自{mode}在本地数据库中的 {summary['samples']} 条采样记录。"
    )
    document.add_paragraph(f"最新采样 {formatted_time(summary['updated_at'])}。")
    if kind == "inspection":
        document.add_heading("当日环境与事件", level=1)
        _table(document, ("指标", "数值", "统计口径"), [
            ("温度", _fmt(summary["avg_temperature"], "°C"), "有效采样均值"),
            ("湿度", _fmt(summary["avg_humidity"], "%"), "有效采样均值"),
            ("光照", _fmt(summary["avg_light"], "ADC"), "有效采样均值"),
            ("告警", summary["alerts"], "当日首次触发记录"),
            ("人员出现", summary["events"].get("entry", 0), "人体感应由无人切换为有人"),
        ], (3.3, 4.2, 9.2))
        document.add_heading("告警处置记录", level=1)
        if alerts:
            rows = []
            for alert in alerts:
                metric = alert["metric"]
                state = "已关闭" if alert["closed_at"] else "恢复待处置" if alert["resolved_at"] else "越限中"
                rows.append((
                    formatted_time(alert["opened_at"]), alert["device_name"],
                    f"{METRIC_LABELS[metric]} {alert['value']:g} {UNITS[metric]}", state, alert["note"] or "—",
                ))
            _table(document, ("触发时间", "设备", "指标", "状态", "处置说明"), rows, (4.0, 3.3, 3.0, 2.4, 4.0))
            if summary["alerts"] > len(alerts):
                document.add_paragraph(f"此处列出最近 {len(alerts)} 条。全部记录可在平台检索。")
        else:
            document.add_paragraph("所选日期内暂无告警记录。")
    else:
        document.add_heading("当日用电估算", level=1)
        document.add_paragraph(f"采样覆盖区间的累计估算值为 {summary['kwh']:.6f} kWh。")
        document.add_paragraph(summary["energy_note"] + " 断线期间未计入，结果用于实训分析。")
        document.add_heading("分时用电", level=1)
        if summary["hourly"]:
            _table(document, ("时段", "估算用电 kWh"), [
                (f"{row['hour']:02d}:00–{row['hour'] + 1:02d}:00", f"{row['kwh']:.6f}")
                for row in summary["hourly"]
            ], (7.6, 9.1))
        else:
            document.add_paragraph("所选日期内暂无样本。")
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()
