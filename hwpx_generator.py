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
        raise FileNotFoundError(f"HWPX template missing: {template_path}")

    with zipfile.ZipFile(template_path, "r") as src:
        archive = [(info, src.read(info.filename)) for info in src.infolist()]

    section_name = "Contents/section0.xml"
    files = dict((info.filename, raw) for info, raw in archive)
    if section_name not in files:
        raise ValueError("HWPX template has no Contents/section0.xml")

    root = etree.fromstring(files[section_name])
    tables = root.findall(".//" + TBL)
    _assert_template(tables)

    header = cells(tables[1])
    header_values = {
        (0, 1): data.get("diary_date", ""),
        (0, 3): data.get("teacher_name", ""),
        (1, 1): data.get("child_name", ""),
        (1, 3): data.get("class_name", ""),
        (1, 5): data.get("age", ""),
        (2, 1): data.get("life_theme", ""),
        (2, 3): data.get("theme", ""),
        (2, 5): data.get("subtheme", ""),
        (3, 1): data.get("teacher_expectation", ""),
    }
    for (row, col), value in header_values.items():
        if row < len(header) and col < len(header[row]):
            put(header[row][col], value)

    # Always fill the corresponding time row, not the input list position.
    grid = cells(tables[2])
    slot_names = [f"{hour:02d}:00~{hour + 1:02d}:00" for hour in range(9, 18)]
    by_time = {
        str(item.get("time", "")).replace(" ", "").replace("-", "~"): item
        for item in (data.get("schedule") or [])
        if isinstance(item, dict)
    }
    for i, slot in enumerate(slot_names):
        if i + 1 >= len(grid):
            break
        row = grid[i + 1]
        item = by_time.get(slot, {})
        for col, field in (
            (0, "time"), (1, "activity"), (2, "activity_process"),
            (3, "child_response"), (4, "teacher_support"), (5, "remarks")
        ):
            if col < len(row):
                put(row[col], slot if field == "time" else item.get(field, ""))

    # The evaluation section of the supplied template is intended for the
    # evaluation and next-step plan, not a repeat of all other diary fields.
    evaluation = "\n".join(
        str(value).strip()
        for value in (data.get("observation_evaluation"), data.get("future_support"))
        if str(value or "").strip()
    )
    summary = (data.get("special_notes", ""), evaluation, data.get("family_connection", ""))
    for idx, value in enumerate(summary, start=3):
        rows = cells(tables[idx])
        if len(rows) > 1 and rows[1]:
            put(rows[1][0], value)

    _fix_table_flow(root, tables)
    files[section_name] = etree.tostring(root, encoding="UTF-8", xml_declaration=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w") as dst:
        for info, raw in archive:
            dst.writestr(info, files.get(info.filename, raw))

    with zipfile.ZipFile(output_path, "r") as check:
        if check.testzip() is not None:
            raise ValueError("Generated HWPX archive is corrupt")
        result_root = etree.fromstring(check.read(section_name))
        final_tables = result_root.findall(".//" + TBL)
        _assert_template(final_tables)
        if len(result_root.findall(P)) != 6:
            raise ValueError("Unexpected output paragraph/table order")
        if any(tbl.find(tag("pos")).get("treatAsChar") != "1" for tbl in final_tables[1:]):
            raise ValueError("Output table flow was not normalized")
