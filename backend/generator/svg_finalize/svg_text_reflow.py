"""Post-processing: fix icon-text alignment and merge underfilled text lines.

Two safe, reversible fixes applied to SVG output:

1. **Icon-text vertical alignment**: When a circle/shape icon sits beside a
   text label on the same visual row, SVG ``y`` is the baseline (not visual
   center), so ``text y == circle cy`` looks misaligned.  This pass adjusts
   the text ``y`` to ``circle_cy + font_size * 0.35`` for visual centering.

2. **Same-line text merging**: When two consecutive ``<text>`` siblings share
   the same ``x``, have y-spacing equal to one line-height, and the first line
   uses < 55% of available width, merge them into one line.  This is
   conservative: only merges when there is clearly wasted space and the two
   lines logically belong together (same parent, same font styling).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

SVG_NS = "http://www.w3.org/2000/svg"

# --- Helpers -----------------------------------------------------------------

def _parse_num(raw: str | None, default: float = 0.0) -> float:
    if not raw:
        return default
    m = re.match(r"^\s*([+-]?(?:\d+\.?\d*|\d*\.\d+))", raw)
    return float(m.group(1)) if m else default


def _text_len(elem: ET.Element) -> int:
    """Count total visible characters in a <text> element (including tspans)."""
    total = 0
    if elem.text:
        total += len(elem.text.strip())
    for child in elem:
        if child.tag.endswith("tspan"):
            if child.text:
                total += len(child.text.strip())
            if child.tail:
                total += len(child.tail.strip())
    if elem.tail:
        total += len(elem.tail.strip())
    return total


def _font_size(elem: ET.Element) -> float:
    """Extract font-size from element attributes (self or parent)."""
    raw = elem.get("font-size", "")
    if raw:
        return _parse_num(raw, 16.0)
    # Check parent
    parent = elem  # fallback
    return 16.0


def _available_width(elem: ET.Element, right_edge: float = 1240.0) -> float:
    """Estimate available width from text x to right_edge of content area."""
    x = _parse_num(elem.get("x", "0"))
    return max(0, right_edge - x)


# --- Fix 1: Icon-text alignment ---------------------------------------------

def _fix_icon_text_alignment(root: ET.Element) -> int:
    """Fix vertical alignment between circle icons and adjacent text labels.

    When a <circle> and <text> are siblings in the same <g>, and their
    vertical positions indicate they should be on the same visual row,
    adjust text y to ``circle_cy + font_size * 0.35`` for visual centering.
    """
    fixes = 0
    for parent in list(root.iter()):
        children = list(parent)
        for i, child in enumerate(children):
            if not child.tag.endswith("circle"):
                continue
            cy = _parse_num(child.get("cy"))
            r = _parse_num(child.get("r"), 10.0)
            if r < 5:
                continue  # too small to be an icon

            # Look at adjacent siblings for a text label
            for j in (i + 1, i - 1):
                if j < 0 or j >= len(children):
                    continue
                sibling = children[j]
                if not sibling.tag.endswith("text"):
                    continue

                text_y = _parse_num(sibling.get("y"))
                fs = _font_size(sibling)

                # They're on the "same row" if text_y is within ~15px of circle cy
                if abs(text_y - cy) > fs * 0.8:
                    continue

                # Target y for visual centering
                target_y = cy + fs * 0.35
                # Only adjust if offset is significant (> 2px)
                if abs(text_y - target_y) > 2.0:
                    sibling.set("y", f"{target_y:.1f}")
                    fixes += 1
    return fixes


# --- Fix 2: Same-line merging -----------------------------------------------

# Minimum waste ratio to consider merging (line uses < 55% of available width)
_MERGE_WASTE_THRESHOLD = 0.55
# Characters per pixel at font_size (blend CJK + Latin)
_CHARS_PER_PX = 1.0 / (16 * 0.75)  # ≈ 0.0833 chars/px at font 16


def _fix_underfilled_lines(root: ET.Element) -> int:
    """Merge consecutive underfilled text lines when safe.

    Only merges when:
    - Same parent <g>
    - Same x position
    - y-spacing equals one line-height
    - First line uses < 55% of available width
    - Same font-size (or very close)
    """
    merges = 0
    for parent in list(root.iter()):
        children = list(parent)
        text_children = [(i, c) for i, c in enumerate(children)
                         if c.tag.endswith("text")]

        # Process in reverse order so index shifts don't affect us
        for idx in range(len(text_children) - 2, -1, -1):
            _, elem_a = text_children[idx]
            real_j, elem_b = text_children[idx + 1]

            x_a = _parse_num(elem_a.get("x"))
            x_b = _parse_num(elem_b.get("x"))
            y_a = _parse_num(elem_a.get("y"))
            y_b = _parse_num(elem_b.get("y"))
            fs_a = _font_size(elem_a)
            fs_b = _font_size(elem_b)

            # Same x position (allow 2px tolerance)
            if abs(x_a - x_b) > 2.0:
                continue
            # Same font size (allow 1px tolerance)
            if abs(fs_a - fs_b) > 1.0:
                continue
            # y-spacing = 1 line height (font_size * 1.3, allow 30% tolerance)
            line_h = fs_a * 1.3
            y_diff = y_b - y_a
            if abs(y_diff - line_h) > line_h * 0.3:
                continue

            # Check if first line is underfilled
            avail_w = _available_width(elem_a)
            chars_a = _text_len(elem_a)
            if avail_w <= 0:
                continue
            # Estimate chars that could fit
            chars_fit = int(avail_w / (fs_a * 0.75))
            if chars_fit <= 0:
                continue
            utilization = chars_a / chars_fit

            if utilization > _MERGE_WASTE_THRESHOLD:
                continue  # Line is reasonably full

            # Safe to merge: append elem_b's text content to elem_a
            _append_text(elem_a, elem_b)
            parent.remove(elem_b)
            merges += 1

    return merges


def _append_text(target: ET.Element, source: ET.Element) -> None:
    """Append source text content to target, preserving tspan structure."""
    # Get last content position in target
    source_text = _extract_text_runs(source)
    if not source_text:
        return

    # Add a space before the appended content
    last_child = None
    for child in target:
        if child.tag.endswith("tspan"):
            last_child = child

    if last_child is not None:
        # Append to last tspan's tail
        tail = (last_child.tail or "").rstrip()
        last_child.tail = tail + " " + source_text[0] if tail else source_text[0]
        for run in source_text[1:]:
            # Create new tspan with same style as last tspan
            new_tspan = ET.SubElement(target, _tspan_tag(target.tag))
            for k, v in last_child.attrib.items():
                new_tspan.set(k, v)
            new_tspan.text = run
    else:
        # No tspan children — append directly to text content
        text = (target.text or "").rstrip()
        target.text = text + " " + source_text[0] if text else source_text[0]


def _extract_text_runs(elem: ET.Element) -> list[str]:
    """Extract plain text runs from a <text> element."""
    runs: list[str] = []
    if elem.text and elem.text.strip():
        runs.append(elem.text.strip())
    for child in elem:
        if child.tag.endswith("tspan"):
            if child.text and child.text.strip():
                runs.append(child.text.strip())
            if child.tail and child.tail.strip():
                runs.append(child.tail.strip())
    return runs


def _tspan_tag(text_tag: str) -> str:
    if text_tag.startswith("{"):
        ns = text_tag.split("}")[0] + "}"
        return f"{ns}tspan"
    return "tspan"


# --- Public API --------------------------------------------------------------

def reflow_text_in_svg(svg_path: Path) -> int:
    """Apply text reflow fixes to an SVG file in-place.

    Returns the total number of fixes applied.
    """
    ET.register_namespace("", SVG_NS)

    try:
        tree = ET.parse(svg_path)
    except ET.ParseError:
        return 0

    root = tree.getroot()
    total = 0

    total += _fix_icon_text_alignment(root)
    total += _fix_underfilled_lines(root)

    if total > 0:
        tree.write(str(svg_path), xml_declaration=True, encoding="unicode")
    return total
