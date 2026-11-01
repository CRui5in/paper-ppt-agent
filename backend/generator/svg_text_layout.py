"""Resolve SVG text trees into positioned, styled runs without losing text."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
import unicodedata
import xml.etree.ElementTree as ET

from .svg_to_pptx.font_mapping import parse_font_family

TEXT_ATTRIBUTES = {
    "font-family", "font-size", "font-weight", "font-style", "fill",
    "fill-opacity", "opacity", "text-decoration", "text-anchor",
    "letter-spacing", "word-spacing", "dominant-baseline",
}


def number(value: str | None, default: float = 0.0) -> float:
    match = re.match(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))", value or "")
    return float(match.group(1)) if match else default


@lru_cache(maxsize=128)
def _font(family: str, size: float, weight: str, italic: bool, cjk: bool):
    from matplotlib.font_manager import FontProperties, findfont
    from PIL import ImageFont

    fonts = parse_font_family(family)
    prop = FontProperties(
        family=[fonts["ea"] if cjk else fonts["latin"]],
        weight="bold" if weight in {"bold", "600", "700", "800", "900"} else "normal",
        style="italic" if italic else "normal",
    )
    return ImageFont.truetype(findfont(prop), max(1, round(size * 16)))


def measure_text(text: str, attrs: dict[str, str], default_size: float = 16.0) -> float:
    size = number(attrs.get("font-size"), default_size)
    cjk = any(unicodedata.east_asian_width(c) in {"W", "F"} for c in text)
    try:
        font = _font(attrs.get("font-family", "Arial"), size, attrs.get("font-weight", "normal"), attrs.get("font-style") == "italic", cjk)
        width = float(font.getlength(text)) / 16
    except (OSError, ValueError):
        width = sum(size if unicodedata.east_asian_width(c) in {"W", "F"} else size * 0.62 for c in text)
    return width + max(0, len(text) - 1) * number(attrs.get("letter-spacing"))


def _glyph_vertical_bounds(text: str, attrs: dict[str, str]) -> tuple[float, float]:
    size = number(attrs.get("font-size"), 16)
    try:
        font = _font(attrs.get("font-family", "Arial"), size, attrs.get("font-weight", "normal"), attrs.get("font-style") == "italic", any(unicodedata.east_asian_width(c) in {"W", "F"} for c in text))
        _left, top, _right, bottom = font.getbbox(text, anchor="ls")
        return top / 16, bottom / 16
    except (OSError, ValueError):
        return -size, size * .25


def _bounds(element: ET.Element) -> tuple[float, float, float, float] | None:
    runs = text_runs(element)
    boxes = []
    for run in runs:
        if not run.text.strip():
            continue
        width = measure_text(run.text, run.attrs)
        anchor = run.attrs.get("text-anchor", "start")
        left = run.x - (width / 2 if anchor == "middle" else width if anchor == "end" else 0)
        top, bottom = _glyph_vertical_bounds(run.text, run.attrs)
        boxes.append((left, run.y + top, left + width, run.y + bottom))
    if not boxes:
        return None
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)


def _move_baseline(element: ET.Element, delta: float) -> None:
    element.set("y", f"{number(element.get('y')) + delta:g}")
    for child in element.iter():
        if child is not element and child.get("y") is not None:
            child.set("y", f"{number(child.get('y')) + delta:g}")


def layout_text_groups(root: ET.Element) -> int:
    """Keep header rows separate and centered labels inside their shape boxes."""
    count = 0
    ns = root.tag.split("}", 1)[0] + "}" if root.tag.startswith("{") else ""
    for group in root.iter(ns + "g"):
        if group.get("data-role") == "header" or group.get("id") == "header":
            texts = [e for e in group if e.tag == ns + "text" and not e.get("transform")]
            placed = []
            for element in sorted(texts, key=lambda e: text_runs(e)[0].y if text_runs(e) else number(e.get("y"))):
                box = _bounds(element)
                if box is None:
                    continue
                shift = 0.0
                for previous, previous_box in placed:
                    if min(box[2], previous_box[2]) > max(box[0], previous_box[0]):
                        gap = min(number(element.get("font-size"), 16), number(previous.get("font-size"), 16)) * .5
                        shift = max(shift, previous_box[3] + gap - box[1])
                if shift > .01:
                    _move_baseline(element, shift)
                    count += 1
                    box = _bounds(element)
                placed.append((element, box))
            if placed:
                bottom = max(b[3] for _, b in placed)
                for shape in group:
                    if shape.tag == ns + "rect" and 0 < number(shape.get("height")) < 8 and number(shape.get("width")) > 20:
                        target = bottom + min(number(e.get("font-size"), 16) for e, _ in placed) * .5
                        if number(shape.get("y")) < target:
                            shape.set("y", f"{target:g}")
                            count += 1

        rects = [e for e in group if e.tag == ns + "rect" and number(e.get("width")) > 0 and number(e.get("height")) > 0]
        owners: dict[ET.Element, list[ET.Element]] = {}
        for text in group:
            if text.tag != ns + "text" or text.get("text-anchor") != "middle" or list(text) or text.get("transform"):
                continue
            size = number(text.get("font-size"), 16)
            x, y = number(text.get("x")), number(text.get("y"))
            candidates = [r for r in rects if abs(x - number(r.get("x")) - number(r.get("width")) / 2) <= 1 and number(r.get("y")) - size <= y <= number(r.get("y")) + number(r.get("height")) + size]
            if candidates:
                rect = min(candidates, key=lambda r: number(r.get("width")) * number(r.get("height")))
                owners.setdefault(rect, []).append(text)
        for rect, texts in owners.items():
            top, bottom = number(rect.get("y")), number(rect.get("y")) + number(rect.get("height"))
            texts.sort(key=lambda e: number(e.get("y")))
            bounds = [_glyph_vertical_bounds(e.text or "", e.attrib) for e in texts]
            if all(top <= number(e.get("y")) + b[0] and number(e.get("y")) + b[1] <= bottom for e, b in zip(texts, bounds)):
                continue
            gap = min(number(e.get("font-size"), 16) for e in texts) * .2
            height = sum(b[1] - b[0] for b in bounds) + gap * (len(texts) - 1)
            if height > bottom - top:
                continue
            cursor = top + (bottom - top - height) / 2
            for text, box in zip(texts, bounds):
                text.set("y", f"{cursor - box[0]:g}")
                cursor += box[1] - box[0] + gap
                count += 1
    return count


@dataclass
class TextRun:
    text: str
    attrs: dict[str, str]
    x: float
    y: float
    line: int


def text_runs(element: ET.Element, inherited: dict[str, str] | None = None) -> list[TextRun]:
    """Walk nested tspans in document order, including every descendant tail."""
    runs: list[TextRun] = []
    x = number(element.get("x"))
    y = number(element.get("y"))
    line = 0

    def emit(raw: str | None, attrs: dict[str, str]) -> None:
        nonlocal x
        if not raw or (not raw.strip() and any(c in raw for c in "\n\r\t")):
            return
        text = re.sub(r"[\t\r\n ]+", " ", raw)
        if text:
            runs.append(TextRun(text, dict(attrs), x, y, line))
            x += measure_text(text, attrs)

    def visit(node: ET.Element, styles: dict[str, str], is_root: bool = False) -> None:
        nonlocal x, y, line
        attrs = dict(styles)
        attrs.update({key: value for key, value in node.attrib.items() if key in TEXT_ATTRIBUTES})
        if not is_root and any(key in node.attrib for key in ("x", "y", "dx", "dy")):
            line += 1
            x = number(node.get("x"), x) + number(node.get("dx"))
            y = number(node.get("y"), y) + number(node.get("dy"))
        emit(node.text, attrs)
        for child in node:
            if child.tag.rsplit("}", 1)[-1] == "tspan":
                visit(child, attrs)
            emit(child.tail, attrs)

    visit(element, inherited or {}, True)
    return runs


def paragraph_fragments(element: ET.Element, inherited: dict[str, str] | None = None) -> list[tuple[str, dict[str, str]]]:
    """Return logical paragraph content, retaining nested styles and word spaces."""
    fragments: list[tuple[str, dict[str, str]]] = []
    previous_line: int | None = None
    for run in text_runs(element, inherited):
        text = run.text
        if run.line != previous_line:
            text = text.lstrip()
            if fragments and text and fragments[-1][0][-1:].isascii() and fragments[-1][0][-1:].isalnum() and text[0].isascii() and text[0].isalnum():
                fragments.append((" ", {}))
        if text:
            fragments.append((text, run.attrs))
        previous_line = run.line
    return fragments


def wrap_fragments(fragments: list[tuple[str, dict[str, str]]], width: float, base_attrs: dict[str, str] | None = None) -> list[list[tuple[str, dict[str, str]]]]:
    """Break CJK text and keep Latin words, numeric values, and closing punctuation together."""
    tokens: list[tuple[str, dict[str, str]]] = []
    base_attrs = base_attrs or (fragments[0][1] if fragments else {})
    for text, attrs in fragments:
        emphasized = any(attrs.get(key) != base_attrs.get(key) for key in ("fill", "font-weight", "font-style"))
        if emphasized and text.strip() and measure_text(text, attrs) <= width:
            tokens.append((text, attrs))
        else:
            tokens.extend((m.group(), attrs) for m in re.finditer(r"[A-Za-z0-9_]+(?:[./%+−-][A-Za-z0-9_]+)*|\s+|.", text))
    lines: list[list[tuple[str, dict[str, str]]]] = []
    current: list[tuple[str, dict[str, str]]] = []
    current_width = 0.0
    for index, (token, attrs) in enumerate(tokens):
        if token.isspace() and not current:
            continue
        size = measure_text(token, attrs)
        closing = unicodedata.category(token[0]) in {"Pe", "Pf"} or token[0] in "，。、；：！？%"
        following = 0.0
        if index + 1 < len(tokens):
            next_text, next_attrs = tokens[index + 1]
            if unicodedata.category(next_text[0]) in {"Pe", "Pf"} or next_text[0] in "，。、；：！？%":
                following = measure_text(next_text, next_attrs)
        if token.isascii() and token.isalpha() and index + 2 < len(tokens) and tokens[index + 1][0].isspace() and tokens[index + 2][0][:1].isdigit():
            following = sum(measure_text(t, a) for t, a in tokens[index + 1:index + 3])
        if current and current_width + size + following > width and not closing:
            while current and current[-1][0].isspace():
                current.pop()
            lines.append(current)
            current = []
            current_width = 0.0
            if token.isspace():
                continue
        current.append((token, attrs))
        current_width += size
    while current and current[-1][0].isspace():
        current.pop()
    if current:
        lines.append(current)
    return lines


def reflow_declared_paragraphs(root: ET.Element) -> int:
    """Lay out body paragraphs only inside explicitly declared text boxes."""
    parent_map = {child: parent for parent in root.iter() for child in parent}
    count = 0
    namespace = root.tag.split("}", 1)[0] + "}" if root.tag.startswith("{") else ""
    for element in list(root.iter(namespace + "text")):
        current = element
        box = None
        while current is not None:
            if current.get("data-textbox") and current.get("data-role", "body") in {"body", "caption", "callout"}:
                values = current.get("data-textbox", "").split()
                if len(values) == 4:
                    box = [number(v) for v in values]
                break
            current = parent_map.get(current)
        if box is None or element.get("text-anchor", "start") != "start" or element.get("transform"):
            continue
        descendants = list(element.iter())[1:]
        # Mathematical scripts and explicit horizontal diagrams keep their geometry.
        if any(child.get("baseline-shift") or child.get("transform") or number(child.get("dy")) < 0 for child in descendants):
            continue
        ancestors = []
        ancestor = parent_map.get(element)
        while ancestor is not None:
            ancestors.append(ancestor)
            ancestor = parent_map.get(ancestor)
        inherited = {}
        for ancestor in reversed(ancestors):
            inherited.update({k: v for k, v in ancestor.attrib.items() if k in TEXT_ATTRIBUTES})
        runs = text_runs(element, inherited)
        if not runs:
            continue
        first = runs[0]
        size = number(first.attrs.get("font-size"), 16)
        if any(abs(number(run.attrs.get("font-size"), size) - size) > 0.01 for run in runs):
            continue
        if any(abs(run.x - first.x) > 1 and run.line != first.line and run.y == first.y for run in runs):
            continue
        x, y, width, height = box
        available = x + width - first.x
        if available <= 0:
            continue
        lines = wrap_fragments(paragraph_fragments(element, inherited), available, first.attrs)
        if not lines:
            continue
        line_height = number(element.get("data-line-height"), size * 1.5)
        baselines = sorted({run.y for run in runs})
        if len(baselines) > 1 and element.get("data-line-height") is None:
            line_height = min(max(size * 1.2, baselines[1] - baselines[0]), size * 1.6)
        if first.y + (len(lines) - 1) * line_height + size * .25 > y + height:
            # Keep complete content; the layout audit handles an insufficient box.
            continue
        before = ET.tostring(element)
        element.text = None
        for child in list(element):
            element.remove(child)
        element.set("x", f"{first.x:g}")
        element.set("y", f"{first.y:g}")
        for index, line in enumerate(lines):
            span = ET.SubElement(element, namespace + "tspan", {"x": f"{first.x:g}", "y": f"{first.y + index * line_height:g}"})
            last = None
            for text, attrs in line:
                styles = {k: v for k, v in attrs.items() if k in TEXT_ATTRIBUTES and element.get(k) != v}
                if last is not None and last.attrib == styles:
                    last.text = (last.text or "") + text
                else:
                    last = ET.SubElement(span, namespace + "tspan", styles)
                    last.text = text
        count += ET.tostring(element) != before
    return count
