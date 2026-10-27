"""Generate AiLog HWPX documents while preserving the supplied table template.

Text layout is recalculated by Hancom Office: stale cached line-segment data
must not be retained when replacing empty template paragraphs with long text.
"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import zipfile
from lxml import etree

HP = 'http://www.hancom.co.kr/hwpml/2011/paragraph'
NS = {'hp': HP}

def tag(name):
    return f'{{{HP}}}{name}'

P, RUN, T, TC, TR, TBL = (tag(x) for x in ('p', 'run', 't', 'tc', 'tr', 'tbl'))


def put(cell, value):
    """Replace a cell's text, removing obsolete line layout and adding paragraphs."""
    value = str(value or '').strip()
    if not value:
        return
    sublist = cell.find(tag('subList'))
    if sublist is None:
        raise ValueError('HWPX cell has no subList')
    paragraphs = sublist.findall(P)
    if not paragraphs:
        raise ValueError('HWPX cell has no paragraph')
    template_p = paragraphs[0]
    existing_run = template_p.find(RUN)
    char_style = existing_run.get('charPrIDRef', '0') if existing_run is not None else '0'

    # Reuse the paragraph's original formatting, but NOT the cached line layout.
    # Each logical line becomes a separate HWPX paragraph.
    lines = value[:12000].replace('\r\n', '\n').replace('\r', '\n').split('\n')
    for old in paragraphs:
        sublist.remove(old)
    for line in lines:
        para = deepcopy(template_p)
        for child in list(para):
            para.remove(child)
        run = etree.SubElement(para, RUN, charPrIDRef=char_style)
        text = etree.SubElement(run, T)
        text.text = line
        sublist.append(para)

    # Large content must start at the top of the cell rather than vertically centered.
    sublist.set('vertAlign', 'TOP')
    sublist.set('textHeight', '0')
    sublist.set('textWidth', '0')


def cells(table):
    return [row.findall(TC) for row in table.findall(TR)]


def generate_hwpx(template_path, output_path, data):
    template_path = Path(template_path)
    output_path = Path(output_path)
    if not template_path.is_file():
        raise FileNotFoundError(f'HWPX template missing: {template_path}')

    with zipfile.ZipFile(template_path, 'r') as src:
        contents = {info.filename: src.read(info.filename) for info in src.infolist()}

    section_name = 'Contents/section0.xml'
    if section_name not in contents:
        raise ValueError('HWPX template has no Contents/section0.xml')
    root = etree.fromstring(contents[section_name])
    tables = root.findall('.//' + TBL)
    if len(tables) < 6:
        raise ValueError('HWPX template must contain six expected tables')

    header = cells(tables[1])
    header_values = {
        (0, 1): data.get('diary_date', ''),
        (0, 3): data.get('teacher_name', ''),
        (1, 1): data.get('child_name', ''),
        (1, 3): data.get('class_name', ''),
        (1, 5): data.get('age', ''),
        (2, 1): data.get('life_theme', ''),
        (2, 3): data.get('theme', ''),
        (2, 5): data.get('subtheme', ''),
        (3, 1): data.get('teacher_expectation', ''),
    }
    for (row, col), value in header_values.items():
        if row < len(header) and col < len(header[row]):
            put(header[row][col], value)

    grid = cells(tables[2])
    for i, item in enumerate(data.get('schedule') or []):
        if i + 1 >= len(grid):
            break
        row = grid[i + 1]
        for col, field in ((0, 'time'), (1, 'activity'),
                           (2, 'activity_process'), (3, 'child_response'),
                           (4, 'teacher_support'), (5, 'remarks')):
            if col < len(row):
                put(row[col], item.get(field, ''))

    summary = [
        data.get('special_notes', ''),
        '\n'.join(str(x).strip() for x in (
            data.get('observation_evaluation', ''),
            data.get('future_support', ''),
            data.get('main_activity', ''),
            data.get('activity_process', ''),
            data.get('child_response', ''),
            data.get('teacher_support', ''),
        ) if str(x or '').strip()),
        data.get('family_connection', ''),
    ]
    for index, value in enumerate(summary, start=3):
        rows = cells(tables[index])
        if len(rows) > 1 and rows[1]:
            put(rows[1][0], value)

    contents[section_name] = etree.tostring(root, encoding='UTF-8', xml_declaration=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, 'w') as dst:
        for name, raw in contents.items():
            dst.writestr(name, raw, compress_type=(
                zipfile.ZIP_STORED if name == 'mimetype' else zipfile.ZIP_DEFLATED
            ))
    with zipfile.ZipFile(output_path, 'r') as check:
        if check.testzip() is not None:
            raise ValueError('Generated HWPX archive is corrupt')
        etree.fromstring(check.read(section_name))
