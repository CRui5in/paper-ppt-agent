"""Fix image aspect ratios in SVG to prevent stretching in PowerPoint."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"


def fix_image_aspect_in_svg(svg_path: Path) -> int:
    """Recalculate image dimensions to maintain original aspect ratio.

    PowerPoint's SVG converter ignores preserveAspectRatio, so we
    pre-compute the correct x/y/width/height centered in the box.

    Returns:
        Number of images fixed.
    """
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)

    try:
        tree = ET.parse(svg_path)
    except ET.ParseError:
        return 0

    root = tree.getroot()
    svg_dir = svg_path.parent
    count = 0

    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag != "image":
            continue

        par = elem.get("preserveAspectRatio", "xMidYMid meet")
        if "none" in par.lower():
            continue

        href = elem.get(f"{{{XLINK_NS}}}href") or elem.get("href", "")
        if not href:
            continue

        try:
            box_x = float(elem.get("x", 0))
            box_y = float(elem.get("y", 0))
            box_w = float(elem.get("width", 0))
            box_h = float(elem.get("height", 0))
        except ValueError:
            continue
        if box_w <= 0 or box_h <= 0:
            continue

        # Get actual image dimensions
        img_w, img_h = _get_image_dimensions(href, svg_dir)
        if img_w <= 0 or img_h <= 0:
            continue

        # Calculate fitted dimensions
        mode = "slice" if "slice" in par else "meet"
        new_w, new_h, off_x, off_y = _calculate_fitted(
            img_w, img_h, box_w, box_h, mode
        )

        # Check if adjustment needed (tolerance ±0.5px)
        if abs(new_w - box_w) < 0.5 and abs(new_h - box_h) < 0.5:
            continue

        elem.set("x", _fmt(box_x + off_x))
        elem.set("y", _fmt(box_y + off_y))
        elem.set("width", _fmt(new_w))
        elem.set("height", _fmt(new_h))
        count += 1

    if count > 0:
        tree.write(str(svg_path), xml_declaration=True, encoding="unicode")
    return count


def _calculate_fitted(
    img_w: float,
    img_h: float,
    box_w: float,
    box_h: float,
    mode: str,
) -> tuple[float, float, float, float]:
    """Calculate dimensions to fit image in box while maintaining aspect ratio.

    Returns:
        (new_width, new_height, offset_x, offset_y)
    """
    img_ratio = img_w / img_h
    box_ratio = box_w / box_h

    if mode == "meet":
        if img_ratio > box_ratio:
            new_w = box_w
            new_h = box_w / img_ratio
        else:
            new_h = box_h
            new_w = box_h * img_ratio
    else:  # slice
        if img_ratio > box_ratio:
            new_h = box_h
            new_w = box_h * img_ratio
        else:
            new_w = box_w
            new_h = box_w / img_ratio

    off_x = (box_w - new_w) / 2
    off_y = (box_h - new_h) / 2
    return new_w, new_h, off_x, off_y


def _get_image_dimensions(href: str, svg_dir: Path) -> tuple[int, int]:
    """Get image dimensions from file or data URI."""
    if href.startswith("data:"):
        import base64
        import io

        # Extract base64 data. Some imported PPTX templates produce data URIs
        # with missing padding; normalize so image probing does not abort export.
        _, data = href.split(",", 1)
        payload = "".join(data.split())
        payload += "=" * (-len(payload) % 4)
        img_bytes = base64.b64decode(payload, validate=False)
        try:
            img = Image.open(io.BytesIO(img_bytes))
            return img.size
        except Exception:
            return 0, 0

    # External file
    img_path = (svg_dir / href).resolve()
    if not img_path.exists():
        img_path = (svg_dir.parent / href).resolve()
    if not img_path.exists():
        return 0, 0

    try:
        img = Image.open(img_path)
        return img.size
    except Exception:
        return 0, 0


def _fmt(val: float) -> str:
    """Format float compactly."""
    if val == int(val):
        return str(int(val))
    return f"{val:.2f}".rstrip("0").rstrip(".")
