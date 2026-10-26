"""Preserves supplied HWPX template; fills existing table cells with diary values."""
from pathlib import Path
import zipfile
from lxml import etree
from datetime import datetime

NS = {'hp': 'http://www.hancom.co.kr/hwpml/2011/paragraph'}
T = '{%s}t' % NS['hp']
P = '{%s}p' % NS['hp']
RUN = '{%s}run' % NS['hp']
TC = '{%s}tc' % NS['hp']
TR = '{%s}tr' % NS['hp']
TBL = '{%s}tbl' % NS['hp']

def put(cell, value):
    value = str(value or '').strip()
    if not value: return
    para = cell.find('.//' + P)
    if para is None: return
    run = para.find(RUN)
    if run is None:
        run = etree.SubElement(para, RUN, charPrIDRef='8')
    for t in para.findall('.//' + T):
        t.text = None
    t = run.find(T)
    if t is None: t = etree.SubElement(run, T)
    t.text = value[:2500]

def cells(tbl):
    return [[tc for tc in tr.findall(TC)] for tr in tbl.findall(TR)]

def generate_hwpx(template_path, output_path, data):
    template_path, output_path = Path(template_path), Path(output_path)
    if not template_path.is_file(): raise FileNotFoundError('HWPX template missing')
    with zipfile.ZipFile(template_path) as z:
        content = {name: z.read(name) for name in z.namelist()}
    doc = etree.fromstring(content['Contents/section0.xml'])
    tables = doc.findall('.//' + TBL)
    if len(tables) < 4: raise ValueError('HWPX template tables are missing')
    # Header table: date, teacher, child, class, age, themes
    header = cells(tables[1])
    mappings = {
        (0,1):data.get('diary_date',''), (0,3):data.get('teacher_name',''),
        (1,1):data.get('child_name',''), (1,3):data.get('class_name',''),
        (1,5):data.get('age',''), (2,1):data.get('life_theme',''),
        (2,3):data.get('theme',''), (2,5):data.get('subtheme',''),
        (3,1):data.get('teacher_expectation','')
    }
    for (r,c),v in mappings.items():
        if r<len(header) and c<len(header[r]): put(header[r][c],v)
    # Daily schedule: fill only data cells, never overwrite labels.
    schedule = data.get('schedule') or []
    grid = cells(tables[2])
    for i, item in enumerate(schedule):
        if i+1 >= len(grid): break
        row = grid[i+1]
        for c,key in [(0,'time'),(1,'activity'),(2,'activity_process'),
                      (3,'child_response'),(4,'teacher_support'),(5,'remarks')]:
            if c < len(row): put(row[c], item.get(key,''))
    # Summary and special notes are in following 1-column tables.
    summary = [
        data.get('special_notes',''),
        '\\n'.join(filter(None,[
            data.get('observation_evaluation',''),
            data.get('future_support',''),
            data.get('main_activity',''),
            data.get('activity_process',''),
            data.get('child_response',''),
            data.get('teacher_support','')
        ])),
        data.get('family_connection','')
    ]
    for j,value in enumerate(summary):
        idx=3+j
        if idx<len(tables):
            rows=cells(tables[idx])
            if len(rows)>1 and rows[1]: put(rows[1][0],value)
    content['Contents/section0.xml'] = etree.tostring(doc, encoding='UTF-8', xml_declaration=True)
    output_path.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output_path,'w') as z:
        for name, raw in content.items():
            z.writestr(name,raw,compress_type=zipfile.ZIP_STORED if name=='mimetype' else zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(output_path) as z:
        if z.testzip() is not None: raise ValueError('Generated HWPX corrupt')
