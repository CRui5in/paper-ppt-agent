"""Expose manuscript tables as named records before visual planning."""

from __future__ import annotations

import json
import re

from .manuscript import page_title, split_manuscript_pages


def _cells(line: str) -> list[str]:
    """Split GFM cells, retaining escaped pipes and pipes in inline code/math."""
    cells, current = [], []
    escaped = False
    code = False
    math = False
    for char in line.strip():
        if escaped:
            current.extend(('\\', char))
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == '`' and not math:
            code = not code
            current.append(char)
        elif char == '$' and not code:
            math = not math
            current.append(char)
        elif char == '|' and not code and not math:
            cells.append(''.join(current).strip())
            current = []
        else:
            current.append(char)
    if escaped:
        current.append('\\')
    cells.append(''.join(current).strip())
    if line.lstrip().startswith('|'):
        cells = cells[1:]
    if line.rstrip().endswith('|') and not line.rstrip().endswith('\\|'):
        cells = cells[:-1]
    return cells


def page_records(content: str, page_number: int) -> dict:
    lines = content.splitlines()
    tables = []
    index = 1
    fenced = False
    while index < len(lines):
        if lines[index-1].lstrip().startswith(('```', '~~~')):
            fenced = not fenced
        separator = _cells(lines[index])
        if fenced or len(separator) < 2 or not all(re.fullmatch(r':?-{3,}:?', cell) for cell in separator):
            index += 1
            continue
        headers = _cells(lines[index-1])
        if len(headers) != len(separator):
            index += 1
            continue
        table_id = f'p{page_number}.t{len(tables)+1}'
        rows = []
        index += 1
        while index < len(lines) and lines[index].strip():
            values = _cells(lines[index])
            if len(values) != len(headers):
                break
            rows.append({'id': f'{table_id}.r{len(rows)+1}', 'cells': values})
            index += 1
        if rows:
            tables.append({'id': table_id, 'columns': headers, 'rows': rows})
    return {'page': page_number, 'title': page_title(content), 'tables': tables}


def manuscript_records(manuscript: str) -> list[dict]:
    return [page_records(page, index) for index, page in enumerate(split_manuscript_pages(manuscript), 1)]


def records_context(records: list[dict]) -> str:
    available = [page for page in records if page['tables']]
    if not available:
        return ''
    return ('## Source table records\n\n'
            'These entries retain the manuscript table cells and their page scope. '
            'The listed row IDs identify selected cells when composing a table or quantitative comparison.\n\n'
            '```json\n' + json.dumps(available, ensure_ascii=False, separators=(',', ':')) + '\n```')
