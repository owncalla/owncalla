"""AiLog: fill the provided diary_template.hwpx without reordering its tables.

The original template contains two floating tables (basic info and timetable)
followed by three inline tables in shared HWPX paragraphs. During Hancom's
layout recalculation, the floating timetable can move behind inline tables.
This generator places all five content tables in independent, in-flow
paragraphs, keeping the template's table geometry and formatting unchanged.
"""
from copy import deepcopy
from pathlib import Path
import zipfile

from lxml import etree

HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
NS = {"hp": HP}


def tag(name):
    return f"{{{HP}}}{name}"


P, RUN, T, TC, TR, TBL = (tag(n) for n in ("p", "run", "t", "tc", "tr", "tbl"))


def cells(table):
    return [row.findall(TC) for row in table.findall(TR)]


def put(cell, value):
    """Insert text into an existing cell, preserving its paragraph/font style."""
    value = str(value or "").strip()
    if not value:
        return

    sublist = cell.find(tag("subList"))
    if sublist is None:
        raise ValueError("HWPX cell has no subList")
    paragraphs = sublist.findall(P)
    if not paragraphs:
        raise ValueError("HWPX cell has no paragraph")

    original = paragraphs[0]
    original_run = original.find(RUN)
    char_style = original_run.get("charPrIDRef", "0") if original_run is not None else "0"

    # Invalidate cached glyph layout; otherwise Hancom can use stale line sizes.
    for paragraph in paragraphs:
        sublist.remove(paragraph)
    lines = value[:12000].replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for line in lines:
        paragraph = deepcopy(original)
        for child in list(paragraph):
            paragraph.remove(child)
        run = etree.SubElement(paragraph, RUN, charPrIDRef=char_style)
        etree.SubElement(run, T).text = line
        sublist.append(paragraph)

    sublist.set("vertAlign", "TOP")
    sublist.set("textHeight", "0")
    sublist.set("textWidth", "0")


def _fix_table_flow(section, tables):
    """Put original tables in body text order, avoiding floating-table reflow.

    Table 0 is the title/signature table, intentionally preserved as-is.
    Tables 1..5 correspond to header, timetable, special notes, evaluation,
    and family communication. Preserve each table's inner rows, sizes, etc.
    """
    body_paragraphs = section.findall(P)
    if len(body_paragraphs) < 3:
        raise ValueError("Unexpected HWPX section layout")

    # These five elements are in their desired order in the template XML,
    # but the first two are floating while the other three are in-flow.
    for table in tables[1:6]:
        pos = table.find(tag("pos"))
        if pos is None:
            raise ValueError("HWPX table has no position information")
        pos.set("treatAsChar", "1")
        pos.set("flowWithText", "1")
        pos.set("allowOverlap", "0")
        pos.set("horzOffset", "0")
        pos.set("vertOffset", "0")
        table.set("textWrap", "TOP_AND_BOTTOM")
        table.set("textFlow", "BOTH_SIDES")

    # Separate tables into distinct body paragraphs. If several tables share
    # the same paragraph/run, making them all inline alone may not keep their
    # vertical order across a page break.
    base = body_paragraphs[1]
    section_index = section.index(base)
    for paragraph in body_paragraphs[1:]:
        section.remove(paragraph)

    for offset, table in enumerate(tables[1:6]):
        paragraph = etree.Element(P, attrib=dict(base.attrib))
        paragraph.set("pageBreak", "0")
        paragraph.set("columnBreak", "0")
        run = etree.SubElement(paragraph, RUN, charPrIDRef="1")
        run.append(table)  # detaches from the old floating/inline run
        etree.SubElement(run, T)  # required after drawing objects in many HWPX templates
        section.insert(section_index + offset, paragraph)


def _assert_template(tables):
    if len(tables) != 6:
        raise ValueError(f"Expected 6 template tables; found {len(tables)}")

    signatures = (
        "개별 유아 보육일지",
        "작성일",
        "일과 및 활동",
        "특이사항",
        "오늘의 평가",
        "가정과의 연계사항",
    )
    for table, expected in zip(tables, signatures):
        visible = "".join(table.itertext())
        if expected not in visible:
            raise ValueError(f"Template table mismatch: expected {expected!r}")


def generate_hwpx(template_path, output_path, data):
    template_path = Path(template_path)
    output_path = Path(output_path)
    if not template_path.is_file():
