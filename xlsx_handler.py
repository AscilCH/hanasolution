import zipfile
import xml.etree.ElementTree as ET
import re
import os
import shutil

NS = {'main': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}

def _get_col_str(idx):
    res = ""
    idx += 1
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        res = chr(65 + rem) + res
    return res

def _get_col_index(col_str):
    idx = 0
    for c in col_str:
        if c.isalpha():
            idx = idx * 26 + (ord(c.upper()) - ord('A') + 1)
    return idx - 1

def read_xlsx(filepath):
    with zipfile.ZipFile(filepath, 'r') as z:
        # Load shared strings
        shared_strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            ss_data = z.read('xl/sharedStrings.xml')
            ss_root = ET.fromstring(ss_data)
            for si in ss_root.findall('main:si', NS):
                t = si.find('main:t', NS)
                if t is not None:
                    shared_strings.append(t.text or "")
                else:
                    # Sometimes text is in multiple runs
                    text = "".join([t_node.text or "" for t_node in si.findall('.//main:t', NS)])
                    shared_strings.append(text)

        # Determine which sheet to read (assume sheet1.xml for now, or find it in workbook.xml/rels)
        sheet_path = 'xl/worksheets/sheet1.xml'
        if sheet_path not in z.namelist():
            # Try to find any sheet
            sheets = [n for n in z.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml')]
            if sheets:
                sheet_path = sheets[0]
            else:
                return []

        sheet_data = z.read(sheet_path)
        sheet_root = ET.fromstring(sheet_data)

        rows = sheet_root.findall('.//main:row', NS)
        
        parsed_rows = []
        max_col_idx = -1
        for row in rows:
            row_num = int(row.get('r'))
            cells = row.findall('main:c', NS)
            row_data = {}
            for cell in cells:
                col_ref = cell.get('r')
                col_idx = _get_col_index(re.sub(r'[0-9]+', '', col_ref))
                if col_idx > max_col_idx:
                    max_col_idx = col_idx
                
                v_node = cell.find('main:v', NS)
                val = ""
                if v_node is not None:
                    val = v_node.text or ""
                    t_attr = cell.get('t')
                    if t_attr == 's':
                        val = shared_strings[int(val)] if val and val.isdigit() and int(val) < len(shared_strings) else val
                    elif t_attr == 'inlineStr':
                        is_node = cell.find('.//main:t', NS)
                        if is_node is not None:
                            val = is_node.text
                row_data[col_idx] = val
            parsed_rows.append((row_num, row_data))

    if not parsed_rows:
        return []

    # Detect header row - assume it's the first non-empty row
    header_row_idx = 0
    header_mapping = {}
    for i, (row_num, rdata) in enumerate(parsed_rows):
        if any(rdata.values()):
            header_row_idx = i
            header_mapping = {col: str(val).strip() for col, val in rdata.items() if str(val).strip()}
            break

    result = []
    for row_num, rdata in parsed_rows[header_row_idx+1:]:
        if not any(rdata.values()):
            continue
        entry = {'_row_num': row_num}
        for col_idx, col_name in header_mapping.items():
            entry[col_name] = rdata.get(col_idx, "")
        result.append(entry)

    return result

def write_xlsx(input_path, output_path, results_list):
    ET.register_namespace('', NS['main'])
    
    # We will modify sheet1.xml and copy the rest
    with zipfile.ZipFile(input_path, 'r') as z_in:
        # Find sheet
        sheet_path = 'xl/worksheets/sheet1.xml'
        if sheet_path not in z_in.namelist():
            sheets = [n for n in z_in.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml')]
            if sheets:
                sheet_path = sheets[0]
        
        sheet_data = z_in.read(sheet_path)
        sheet_root = ET.fromstring(sheet_data)

        # find dimensions and columns
        rows = sheet_root.findall('.//main:row', NS)
        max_col = 0
        for row in rows:
            for cell in row.findall('main:c', NS):
                col_ref = cell.get('r')
                col_idx = _get_col_index(re.sub(r'[0-9]+', '', col_ref))
                if col_idx > max_col:
                    max_col = col_idx
                    
        new_cols_start = max_col + 1
        headers = ['Phone(s) Found', 'Source', 'Status']
        
        # update sheet
        results_by_row = {r['row_num']: r for r in results_list}
        
        for row in rows:
            r_idx = int(row.get('r'))
            
            # Header row assumption: r_idx == 1
            if r_idx == 1:
                for i, h in enumerate(headers):
                    c = ET.SubElement(row, '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c')
                    c.set('r', f"{_get_col_str(new_cols_start + i)}{r_idx}")
                    c.set('t', 'inlineStr')
                    is_el = ET.SubElement(c, '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}is')
                    t_el = ET.SubElement(is_el, '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t')
                    t_el.text = h
            elif r_idx in results_by_row:
                res = results_by_row[r_idx]
                vals = [res.get('phones', ''), res.get('source', ''), res.get('status', '')]
                for i, v in enumerate(vals):
                    c = ET.SubElement(row, '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c')
                    c.set('r', f"{_get_col_str(new_cols_start + i)}{r_idx}")
                    c.set('t', 'inlineStr')
                    is_el = ET.SubElement(c, '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}is')
                    t_el = ET.SubElement(is_el, '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t')
                    t_el.text = str(v)
                    
        modified_sheet = ET.tostring(sheet_root, encoding='utf-8', xml_declaration=True)
        
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as z_out:
            for item in z_in.infolist():
                if item.filename == sheet_path:
                    z_out.writestr(item, modified_sheet)
                else:
                    z_out.writestr(item, z_in.read(item.filename))
