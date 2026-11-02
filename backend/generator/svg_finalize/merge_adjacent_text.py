"""Merge adjacent SVG text nodes that accidentally overlap on the same line.

LLM-generated SVGs sometimes emit multiple sibling ``<text>`` elements with the
same x/y anchor in an attempt to style fragments inline. Browsers then render
those fragments on top of each other. This pass merges such runs back into a
single ``<text>`` element with inline ``<tspan>`` runs so preview/export stay
readable.
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

MERGE_X_TOLERANCE = 1.0
MERGE_Y_TOLERANCE = 0.5


def merge_adjacent_text_in_svg(svg_path: Path) -> int:
    """Merge overlapping sibling text nodes in-place.

    Returns the number of merged text groups.
    """
    ET.register_namespace("", SVG_NS)

    try:
        tree = ET.parse(svg_path)
    except ET.ParseError:
        return 0

    root = tree.getroot()
    merged = _merge_text_groups(root)
    if merged > 0:
        tree.write(str(svg_path), xml_declaration=True, encoding="unicode")
    return merged


def _merge_text_groups(root: ET.Element) -> int:
    count = 0
    for parent in list(root.iter()):
        if len(parent) < 2:
            continue

        index = 0
        while index < len(parent):
            child = parent[index]
            if not _is_mergeable_text(child):
                index += 1
                continue

            group = [child]
            anchor = _anchor(child)
            next_index = index + 1
            while next_index < len(parent):
                sibling = parent[next_index]
                if not _is_mergeable_text(sibling):
                    break
                if not _can_merge(anchor, _anchor(sibling)):
                    break
                group.append(sibling)
                next_index += 1

            if len(group) <= 1:
                index = next_index
                continue

            merged = _merge_group(group)
            parent.insert(index, merged)
            for elem in group:
                parent.remove(elem)
            count += 1
            index += 1

    return count


def _merge_group(group: list[ET.Element]) -> ET.Element:
    base = group[0]
    merged = ET.Element(base.tag)
    for key, value in base.attrib.items():
        if key not in {"x", "y", "dx", "dy"}:
            merged.set(key, value)
    merged.set("x", base.get("x", "0"))
    merged.set("y", base.get("y", "0"))

    for run in _iter_runs(group):
        text, styles = run
        if not text:
            continue
        tspan = ET.SubElement(merged, _tspan_tag(base.tag))
        for key, value in styles.items():
            if key in TEXT_STYLE_ATTRS:
                tspan.set(key, value)
        tspan.text = text

    return merged


def _iter_runs(group: list[ET.Element]) -> list[tuple[str, dict[str, str]]]:
    from ..svg_text_layout import text_runs

    return [(run.text, run.attrs) for element in group for run in text_runs(element)]


def _is_text(elem: ET.Element) -> bool:
    return elem.tag.endswith("text")


def _is_mergeable_text(elem: ET.Element) -> bool:
    if not _is_text(elem) or "x" not in elem.attrib or "y" not in elem.attrib:
        return False
    if elem.get("transform") or elem.get("text-anchor", "start") != "start":
        return False
    return all(
        not any(attr in child.attrib for attr in ("x", "y", "dx", "dy", "transform", "baseline-shift"))
        for child in elem.iter() if child is not elem
    )


def _is_tspan(elem: ET.Element) -> bool:
    return elem.tag.endswith("tspan")


def _tspan_tag(text_tag: str) -> str:
    if text_tag.startswith("{"):
        ns = text_tag.split("}")[0] + "}"
        return f"{ns}tspan"
    return "tspan"


def _anchor(elem: ET.Element) -> tuple[float, float]:
    return (_parse_num(elem.get("x", "0")), _parse_num(elem.get("y", "0")))


def _can_merge(left: tuple[float, float], right: tuple[float, float]) -> bool:
    return (
        abs(left[0] - right[0]) <= MERGE_X_TOLERANCE
        and abs(left[1] - right[1]) <= MERGE_Y_TOLERANCE
    )


def _parse_num(raw: str) -> float:
    match = re.match(r"^\s*([+-]?(?:\d+\.?\d*|\d*\.\d+))", raw)
    return float(match.group(1)) if match else 0.0


def _normalize_text(text: str | None) -> str:
    if not text:
        return ""
    collapsed = re.sub(r"\s+", " ", text)
    return collapsed.strip()
