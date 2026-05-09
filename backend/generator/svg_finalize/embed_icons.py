"""Embed icon placeholders in SVG files.

Replaces <use data-icon="name"/> elements with actual SVG icon shapes.
Icons are loaded from the assets/icons/ directory (chunk, tabler-filled, tabler-outline).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

ICON_BASE_SIZES = {
    "chunk": 16,
    "tabler-filled": 24,
    "tabler-outline": 24,
}

SHAPE_TAGS = {"path", "circle", "rect", "line", "polyline", "polygon", "ellipse"}


def embed_icons_in_file(svg_path: Path, icons_dir: Path) -> int:
    """Replace icon placeholders with actual SVG icon shapes.

    Args:
        svg_path: Path to SVG file to process.
        icons_dir: Root directory containing icon libraries.

    Returns:
        Number of icons embedded.
    """
    content = svg_path.read_text(encoding="utf-8")

    # Find <use data-icon="..."> elements via regex (more robust than ET for mixed content)
    pattern = re.compile(
        r'<use\s+([^>]*data-icon="([^"]+)"[^>]*)(?:/>|>\s*</use>)',
        re.DOTALL,
    )

    count = 0
    def replace_icon(match: re.Match) -> str:
        nonlocal count
        attrs_str = match.group(1)
        icon_name = match.group(2)

        # Parse attributes
        x = _parse_attr(attrs_str, "x", 0)
        y = _parse_attr(attrs_str, "y", 0)
        width = _parse_attr(attrs_str, "width", 24)
        height = _parse_attr(attrs_str, "height", 24)
        fill = _parse_str_attr(attrs_str, "fill", "#000000")

        # Resolve icon file
        icon_path, base_size = _resolve_icon(icon_name, icons_dir)
        if not icon_path or not icon_path.exists():
            return match.group(0)  # Keep original if icon not found

        # Load icon shapes
        shapes, is_stroke = _extract_icon_shapes(icon_path)
        if not shapes:
            return match.group(0)

        # Calculate transform
        scale_x = width / base_size
        scale_y = height / base_size
        if abs(scale_x - scale_y) < 1e-6:
            transform = f"translate({x}, {y}) scale({scale_x})"
        else:
            transform = f"translate({x}, {y}) scale({scale_x}, {scale_y})"

        # Build replacement group
        if is_stroke:
            g_open = f'<g transform="{transform}" fill="none" stroke="{fill}">'
        else:
            g_open = f'<g transform="{transform}" fill="{fill}">'

        count += 1
        return f"<!-- icon: {icon_name} -->\n{g_open}\n{shapes}\n</g>"

    content = pattern.sub(replace_icon, content)
    svg_path.write_text(content, encoding="utf-8")
    return count


def _resolve_icon(icon_name: str, icons_dir: Path) -> tuple[Path | None, int]:
    """Resolve icon name to file path and base size."""
    if "/" in icon_name:
        lib, name = icon_name.split("/", 1)
        return icons_dir / lib / f"{name}.svg", ICON_BASE_SIZES.get(lib, 24)
    # Default to chunk library
    return icons_dir / "chunk" / f"{icon_name}.svg", ICON_BASE_SIZES.get("chunk", 16)


def _extract_icon_shapes(icon_path: Path) -> tuple[str, bool]:
    """Extract shape elements from an icon SVG file.

    Returns:
        Tuple of (shapes_xml, is_stroke_icon).
    """
    try:
        tree = ET.parse(icon_path)
    except ET.ParseError:
        return "", False

    root = tree.getroot()
    is_stroke = (
        root.get("stroke") == "currentColor" and root.get("fill") == "none"
    )

    shapes = []
    for elem in root.iter():
        local_tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if local_tag in SHAPE_TAGS:
            # Remove color attributes (parent <g> controls color)
            for attr in ["fill", "stroke"]:
                if attr in elem.attrib:
                    del elem.attrib[attr]
            shapes.append(_serialize_svg_shape(elem))

    return "\n".join(shapes), is_stroke


def _serialize_svg_shape(elem: ET.Element) -> str:
    """Serialize icon shapes as plain SVG tags, not `ns0:path` prefixed tags."""
    xml = ET.tostring(elem, encoding="unicode")
    xml = re.sub(r"\sxmlns:ns\d+=\"http://www\.w3\.org/2000/svg\"", "", xml)
    xml = re.sub(r"<(/?)ns\d+:", r"<\1", xml)
    return xml


def _parse_attr(attrs: str, name: str, default: float) -> float:
    match = re.search(rf'{name}="([^"]+)"', attrs)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return default


def _parse_str_attr(attrs: str, name: str, default: str) -> str:
    match = re.search(rf'{name}="([^"]+)"', attrs)
    return match.group(1) if match else default
