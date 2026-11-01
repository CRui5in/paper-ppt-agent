"""Flatten multi-line <text> with <tspan> elements into separate <text> nodes.

PowerPoint's SVG renderer doesn't handle tspan positioning well.
This module splits multi-line text into independent <text> elements.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

SVG_NS = "http://www.w3.org/2000/svg"

TEXT_STYLE_ATTRS = {
    "font-family", "font-size", "font-weight", "font-style", "font-variant",
    "letter-spacing", "word-spacing", "text-anchor", "text-decoration",
    "dominant-baseline", "fill", "fill-opacity", "stroke", "stroke-width",
    "stroke-opacity", "opacity", "paint-order", "direction", "writing-mode",
}


def flatten_text_in_svg(svg_path: Path) -> int:
    """Flatten tspan elements in an SVG file.

    Returns:
        Number of text elements flattened.
    """
    ET.register_namespace("", SVG_NS)

    try:
        tree = ET.parse(svg_path)
    except ET.ParseError:
        return 0

    root = tree.getroot()
    count = _flatten_text_with_tspans(root)

    if count > 0:
        tree.write(str(svg_path), xml_declaration=True, encoding="unicode")
    return count


def _flatten_text_with_tspans(root: ET.Element) -> int:
    """Process all text elements in the tree.

    Returns:
        Number of text elements modified.
    """
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    text_tag = f"{ns}text"
    tspan_tag = f"{ns}tspan"
    count = 0

    # Collect text elements and their parents
    parent_map: dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parent_map[child] = parent

    for text_elem in list(root.iter(text_tag)):
        tspans = list(text_elem.iter(tspan_tag))
        if not tspans:
            continue
        # Relative offsets encode inline geometry, including mathematical
        # scripts. They are not paragraph line breaks.
        if any(span.get("dx") is not None or span.get("dy") is not None for span in tspans):
            continue
        if any(len(span) for span in tspans):
            continue
        if (text_elem.text or "").strip():
            continue
        # Only explicit paragraph lines can be separated without calculating
        # glyph advances. Keep inline and independently positioned runs intact.
        if any(span.get("x") is None or span.get("y") is None or span.get("transform") for span in tspans):
            continue
        if len({_parse_num(span.get("y", "0")) for span in tspans}) != len(tspans):
            continue

        parent = parent_map.get(text_elem)
        if parent is None:
            continue

        # Build new text elements for each line
        new_elements = _split_into_lines(text_elem, tspans, ns)
        if not new_elements:
            continue

        # Replace original text element
        idx = list(parent).index(text_elem)
        parent.remove(text_elem)
        for i, new_elem in enumerate(new_elements):
            parent.insert(idx + i, new_elem)
        count += 1

    return count


def _split_into_lines(
    text_elem: ET.Element, tspans: list[ET.Element], ns: str
) -> list[ET.Element]:
    """Split a text element into multiple text elements, one per line.

    Inline emphasis within the same line is preserved as nested <tspan> runs.
    """
    text_tag = f"{ns}text"
    parent_styles = {
        k: v for k, v in text_elem.attrib.items() if k not in {"x", "y", "dx", "dy"}
    }
    base_x = _parse_num(text_elem.get("x", "0"))
    base_y = _parse_num(text_elem.get("y", "0"))

    cur_x = base_x
    cur_y = base_y
    line_x = base_x
    line_y = base_y
    lines: list[tuple[float, float, list[tuple[str, dict[str, str]]]]] = []
    line_parts: list[tuple[str, dict[str, str]]] = []

    initial_text = _normalize_text(text_elem.text)
    if initial_text:
        line_parts.append((initial_text, {}))

    for tspan in tspans:
        next_x = cur_x
        next_y = cur_y
        has_x = tspan.get("x") is not None
        has_y = tspan.get("y") is not None
        dy_raw = tspan.get("dy")

        # Update position
        if tspan.get("x") is not None:
            next_x = _parse_num(tspan.get("x", "0"))
        elif tspan.get("dx") is not None:
            next_x += _parse_num(tspan.get("dx", "0"))

        if tspan.get("y") is not None:
            next_y = _parse_num(tspan.get("y", "0"))
        elif tspan.get("dy") is not None:
            next_y += _parse_num(tspan.get("dy", "0"))

        starts_new_line = False
        if line_parts:
            if has_y:
                starts_new_line = abs(next_y - line_y) > 0.01
            elif dy_raw is not None:
                starts_new_line = abs(_parse_num(dy_raw)) > 0.01
            elif has_x:
                starts_new_line = True

        if starts_new_line:
            lines.append((line_x, line_y, line_parts))
            line_parts = []
            line_x = next_x
            line_y = next_y
        elif not line_parts:
            line_x = next_x
            line_y = next_y

        cur_x = next_x
        cur_y = next_y

        text = _normalize_text(tspan.text)
        if not text:
            text = ""

        # Keep only line-local style overrides on inline tspans.
        styles = {
            k: v for k, v in tspan.attrib.items()
            if k in TEXT_STYLE_ATTRS and k not in {"x", "y", "dx", "dy"}
        }
        if text:
            line_parts.append((text, styles))

        tail = _normalize_text(tspan.tail)
        if tail:
            line_parts.append((tail, {}))

    if line_parts:
        lines.append((line_x, line_y, line_parts))

    # Create new text elements
    result = []
    for x, y, parts in lines:
        new_text = ET.Element(text_tag)
        new_text.set("x", _fmt(x))
        new_text.set("y", _fmt(y))
        for k, v in parent_styles.items():
            new_text.set(k, v)
        if len(lines) > 1:
            new_text.attrib.pop("id", None)

        if len(parts) == 1 and not parts[0][1]:
            new_text.text = parts[0][0]
        else:
            for text, styles in parts:
                tspan = ET.SubElement(new_text, f"{ns}tspan")
                for k, v in styles.items():
                    tspan.set(k, v)
                tspan.text = text
        result.append(new_text)

    return result


def _parse_num(s: str) -> float:
    """Parse a number from a string, handling units."""
    match = re.match(r"^\s*([+-]?(?:\d+\.?\d*|\d*\.\d+))", s)
    return float(match.group(1)) if match else 0.0


def _fmt(val: float) -> str:
    if val == int(val):
        return str(int(val))
    return f"{val:.2f}".rstrip("0").rstrip(".")


def _normalize_text(text: str | None) -> str:
    if not text:
        return ""
    collapsed = re.sub(r"\s+", " ", text)
    return collapsed.strip()
