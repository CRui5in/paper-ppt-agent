"""Main SVG to DrawingML dispatcher.

Parses an SVG file and converts all elements to DrawingML shape XML
suitable for embedding in a PPTX slide.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .context import ConvertContext
from .elements import (
    convert_circle,
    convert_ellipse,
    convert_image,
    convert_line,
    convert_path,
    convert_polygon,
    convert_polyline,
    convert_rect,
    convert_text,
)
from .utils import parse_style_attribute, parse_transform, parse_transform_matrix

# SVG elements that should be skipped
SKIP_TAGS = {"defs", "title", "desc", "metadata", "style", "clipPath", "mask", "filter"}

# Element tag → converter function
CONVERTERS = {
    "rect": convert_rect,
    "circle": convert_circle,
    "ellipse": convert_ellipse,
    "line": convert_line,
    "path": convert_path,
    "polygon": convert_polygon,
    "polyline": convert_polyline,
    "text": convert_text,
    "image": convert_image,
}


def convert_svg_to_slide_shapes(
    svg_path: Path,
    slide_num: int = 1,
) -> tuple[str, dict[str, bytes], list[dict]]:
    """Convert an SVG file to DrawingML shapes for a PPTX slide.

    Args:
        svg_path: Path to the SVG file.
        slide_num: Slide number (1-based).

    Returns:
        Tuple of (slide_xml, media_files_dict, rel_entries).
    """
    tree = ET.parse(svg_path)
    root = tree.getroot()

    # Collect <defs>
    defs = _collect_defs(root)

    # Create context
    ctx = ConvertContext(
        defs=defs,
        slide_num=slide_num,
        svg_dir=svg_path.parent,
    )

    # Convert all elements
    shapes_xml = _convert_children(root, ctx)

    # Wrap in slide XML
    slide_xml = _build_slide_xml(shapes_xml)

    return slide_xml, ctx.media_files, ctx.rel_entries


def _convert_children(parent: Any, ctx: ConvertContext) -> str:
    """Recursively convert all child elements."""
    parts = []
    for child in parent:
        xml = _convert_element(child, ctx)
        if xml:
            parts.append(xml)
    return "".join(parts)


def _convert_element(elem: Any, ctx: ConvertContext) -> str:
    """Dispatch element to appropriate converter."""
    tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag

    if tag in SKIP_TAGS:
        return ""

    if tag == "g":
        return _convert_g(elem, ctx)

    converter = CONVERTERS.get(tag)
    if converter:
        transform_str = elem.get("transform", "")
        if transform_str:
            dx, dy, sx, sy, _angle = parse_transform(transform_str)
            child_ctx = ctx.child(
                matrix=parse_transform_matrix(transform_str),
                legacy=(dx, dy, sx, sy),
            )
            xml = converter(elem, child_ctx)
            ctx.sync_from_child(child_ctx)
            return xml
        return converter(elem, ctx)

    return ""


def _convert_g(elem: Any, ctx: ConvertContext) -> str:
    """Convert <g> group element with transform propagation."""
    transform_str = elem.get("transform", "")

    # Extract inheritable styles
    style_overrides = parse_style_attribute(elem.get("style"))
    for attr in [
        "fill",
        "stroke",
        "opacity",
        "fill-opacity",
        "stroke-opacity",
        "font-family",
        "font-size",
        "font-weight",
        "font-style",
        "text-anchor",
        "text-decoration",
    ]:
        val = elem.get(attr)
        if val is not None:
            style_overrides[attr] = val

    dx, dy, sx, sy, _angle = parse_transform(transform_str)
    child_ctx = ctx.child(
        matrix=parse_transform_matrix(transform_str),
        legacy=(dx, dy, sx, sy),
        style_overrides=style_overrides,
    )
    shapes_xml = _convert_children(elem, child_ctx)
    ctx.sync_from_child(child_ctx)

    return shapes_xml


def _collect_defs(root: Any) -> dict[str, Any]:
    """Collect all <defs> children into a dict by ID."""
    defs: dict[str, Any] = {}
    ns = root.tag.split("}")[0] + "}" if "}" in root.tag else ""
    defs_tag = f"{ns}defs"

    for defs_elem in root.iter(defs_tag):
        for child in defs_elem:
            eid = child.get("id")
            if eid:
                defs[eid] = child
    return defs


def _build_slide_xml(shapes_xml: str) -> str:
    """Wrap shapes in a complete slide XML document."""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        '<p:cSld><p:spTree>'
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr>'
        '<a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm>'
        '</p:grpSpPr>'
        f'{shapes_xml}'
        '</p:spTree></p:cSld>'
        '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>'
        '</p:sld>'
    )
