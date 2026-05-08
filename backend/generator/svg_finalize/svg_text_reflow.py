"""Post-processing: fix local text alignment without changing line breaks.

One safe, reversible fix is applied to SVG output:

1. **Icon-text vertical alignment**: When a circle/shape icon sits beside a
   text label on the same visual row, SVG ``y`` is the baseline (not visual
   center), so ``text y == circle cy`` looks misaligned.  This pass adjusts
   the text ``y`` to ``circle_cy + font_size * 0.35`` for visual centering.

This module intentionally does not merge wrapped text lines. The executor's
manual line breaks are layout decisions; merging them in post-processing can
make final SVG/PPT output overflow cards and columns.
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

    if total > 0:
        tree.write(str(svg_path), xml_declaration=True, encoding="unicode")
    return total
