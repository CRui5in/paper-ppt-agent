"""Measure selected slide copy before the SVG author chooses coordinates."""

from __future__ import annotations

import html
import json
import re

from backend.generator.svg_text_layout import measure_text
from backend.generator.svg_to_pptx.font_mapping import parse_font_family
from .source_records import page_records


def _content_field(block: str) -> str:
    match = re.search(r'(?m)^[-*][ \t]+(?:\*\*|`)?Content(?:\*\*|`)?[ \t]*:[ \t]*(.*)', block)
    if not match:
        return ''
    tail = block[match.end():]
    following = re.search(r'(?m)^[-*]\s+\S|^#{1,6}\s', tail)
    return match[1] + tail[:following.start() if following else len(tail)]


def _plain(text: str) -> str:
    text = re.sub(r'!?\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'</?(?:span|strong|em|b|i|sub|sup|br)\b[^>]*>', '', text, flags=re.I)
    text = re.sub(r'(\*\*|__)(.+?)\1', r'\2', text).replace('`', '')
    return html.unescape(text).strip()


def copy_measurements(block: str, *, font_family: str = 'Arial', font_size: float = 18) -> dict:
    content = _content_field(block)
    attrs = {'font-family': font_family, 'font-size': str(font_size)}
    copy = []
    for line in content.splitlines():
        text = _plain(re.sub(r'^\s*(?:[-*]\s+|\d+[.)、]\s+)', '', line))
        if not text or '|' in text or '$' in text or re.search(r'\\(?:frac|sum|begin|mathrm|text)\b', text):
            continue
        copy.append({'id':f'copy{len(copy)+1}', 'text':text,
                     'advance_px':round(measure_text(text, attrs), 1)})
    tables = []
    for table in page_records(content, 1)['tables']:
        columns = []
        for index, header in enumerate(table['columns']):
            width = max(measure_text(_plain(header), {**attrs, 'font-weight':'bold'}),
                        *(measure_text(_plain(row['cells'][index]), attrs) for row in table['rows']))
            columns.append({'label':_plain(header), 'max_advance_px':round(width, 1)})
        tables.append({'columns':columns, 'rows':len(table['rows'])})
    return {'reference':{'font_size_px':font_size, 'font_slots':parse_font_family(font_family)},
            'copy':copy, 'tables':tables}


def copy_metrics_context(block: str) -> str:
    measurements = copy_measurements(block)
    if not measurements['copy'] and not measurements['tables']:
        return ''
    return ('## Measured copy reference\n\n'
            'Widths below are unwrapped glyph advances at the stated reference font and size. '
            'Scale advances with the chosen type size, allocate line widths, and reserve the resulting line heights before placing neighboring regions.\n\n'
            '```json\n'+json.dumps(measurements, ensure_ascii=False, separators=(',', ':'))+'\n```')
