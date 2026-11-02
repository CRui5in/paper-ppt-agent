"""Typeset explicitly declared vertical text flows during initial SVG preparation."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET

from .svg_text_layout import (
    TEXT_ATTRIBUTES, _glyph_vertical_bounds, measure_text, number,
    paragraph_fragments, text_runs, wrap_fragments,
)


def _plans(root: ET.Element):
    parents = {child: parent for parent in root.iter() for child in parent}
    for group in root.iter():
        if group.get('data-layout') != 'text-flow':
            continue
        frame = group.get('data-flow-box', '').replace(',', ' ').split()
        try:
            x, y, width, height = map(float, frame)
        except ValueError as error:
            raise ValueError('text-flow needs data-flow-box="x y width height"') from error
        if not all(math.isfinite(v) for v in (x, y, width, height)) or width <= 0 or height <= 0:
            raise ValueError('text-flow needs finite coordinates and positive dimensions')
        gap = number(group.get('data-gap'), 12)
        if gap < 0:
            raise ValueError('text-flow data-gap must be nonnegative')
        ancestry, current = [], group
        while current is not None:
            ancestry.append(current)
            current = parents.get(current)
        inherited = {}
        for ancestor in reversed(ancestry):
            inherited.update({k: v for k, v in ancestor.attrib.items() if k in TEXT_ATTRIBUTES})
        cursor, plans = y, []
        for text in group:
            if text.tag.rsplit('}', 1)[-1] != 'text' or text.get('transform'):
                raise ValueError('text-flow contains direct, untransformed text elements')
            runs = text_runs(text, inherited)
            if not runs:
                continue
            first = runs[0]
            size = max(number(run.attrs.get('font-size'), 16) for run in runs)
            line_height = number(text.get('data-line-height'), size * 1.5)
            if line_height <= 0:
                raise ValueError('text-flow line height must be positive')
            lines = wrap_fragments(paragraph_fragments(text, inherited), width, first.attrs)
            visible = [(content, attrs) for line in lines for content, attrs in line if content.strip()]
            if not visible:
                continue
            top = min(_glyph_vertical_bounds(content, attrs)[0] - number(attrs.get('baseline-shift')) for content, attrs in visible)
            bottom = max(_glyph_vertical_bounds(content, attrs)[1] - number(attrs.get('baseline-shift')) for content, attrs in visible)
            glyph_height = bottom - top
            block_height = max(line_height, glyph_height) + (len(lines) - 1) * line_height
            baseline = cursor + max(0, (line_height - glyph_height) / 2) - top
            anchor = first.attrs.get('text-anchor', 'start')
            anchor_x = x + (width / 2 if anchor == 'middle' else width if anchor == 'end' else 0)
            plans.append({'element': text, 'lines': lines, 'baseline': baseline, 'x': anchor_x,
                          'line_height': line_height, 'right': max(sum(measure_text(t, a) for t, a in line) for line in lines)})
            cursor += block_height + gap
        yield group, (x, y, width, height), plans, max(0, cursor - y - (gap if plans else 0))


def text_flow_issues(root: ET.Element) -> list[dict]:
    issues = []
    for group, frame, plans, height in _plans(root):
        width = max((plan['right'] for plan in plans), default=0)
        if height > frame[3] + .1 or width > frame[2] + .1:
            issues.append({'box': frame, 'required_height': height, 'required_width': width,
                           'paragraphs': len(plans), 'lines': sum(len(p['lines']) for p in plans),
                           'element': group.get('data-editor-id') or group.get('id', 'text-flow')})
    return issues


def layout_text_flows(root: ET.Element) -> int:
    """Resolve authored text-flow frames without shrinking fonts or changing frames."""
    namespace = root.tag.split('}', 1)[0] + '}' if root.tag.startswith('{') else ''
    count = 0
    for _group, _frame, plans, _height in _plans(root):
        for plan in plans:
            text = plan['element']
            original = ET.tostring(text)
            text.text = None
            for child in list(text):
                text.remove(child)
            for name in ('dx', 'dy'):
                text.attrib.pop(name, None)
            text.set('x', f'{plan["x"]:g}')
            text.set('y', f'{plan["baseline"]:g}')
            for index, line in enumerate(plan['lines']):
                row = ET.SubElement(text, namespace + 'tspan', {
                    'x': f'{plan["x"]:g}', 'y': f'{plan["baseline"] + index * plan["line_height"]:g}'})
                previous = None
                for content, attrs in line:
                    style = {k: v for k, v in attrs.items() if k in TEXT_ATTRIBUTES | {'baseline-shift'} and text.get(k) != v}
                    if previous is not None and previous.attrib == style:
                        previous.text = (previous.text or '') + content
                    else:
                        previous = ET.SubElement(row, namespace + 'tspan', style)
                        previous.text = content
            count += original != ET.tostring(text)
    return count
