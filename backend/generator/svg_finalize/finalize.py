"""Unified SVG post-processing pipeline.

Orchestrates all finalization steps in sequence:
1. Embed icons
2. Crop images (preserveAspectRatio="slice")
3. Fix image aspect ratios
4. Embed external images as Base64
5. Flatten tspan text elements
5.5. Merge overlapping sibling text nodes
5.55. Fix icon-text alignment & merge underfilled text lines
5.6. Normalize CSS font fallback stacks
6. Convert rounded rects to paths
"""

from __future__ import annotations

from pathlib import Path
import re
import xml.etree.ElementTree as ET

from backend.config import settings

from .crop_images import crop_images_in_svg
from .embed_icons import embed_icons_in_file
from .embed_images import build_image_index, embed_images_in_svg
from .fix_image_aspect import fix_image_aspect_in_svg
from .flatten_tspan import flatten_text_in_svg
from .merge_adjacent_text import merge_adjacent_text_in_svg
from .normalize_fonts import normalize_text_fonts_in_svg
from .svg_text_reflow import reflow_text_in_svg
from .repair_svg import repair_svg_file
from .svg_rect_to_path import convert_rounded_rects_in_svg
from ..project_manager import prepare_for_finalize, get_svg_files


def finalize_project(
    project_dir: Path,
    icons_dir: Path | None = None,
    compress: bool = False,
    max_dimension: int | None = None,
) -> dict[str, int]:
    """Run the complete SVG finalization pipeline on a project.

    Args:
        project_dir: Project directory path.
        icons_dir: Icons directory (defaults to settings.icons_dir).
        compress: Whether to compress embedded images.
        max_dimension: Max pixel dimension for images.

    Returns:
        Dict with counts of modifications per step.
    """
    if icons_dir is None:
        icons_dir = settings.icons_dir

    # Step 0: Copy svg_output/ → svg_final/
    prepare_for_finalize(project_dir)

    svg_files = get_svg_files(project_dir, source="final")
    if not svg_files:
        return {"total_files": 0}

    # Build a project-wide image index once so embed_images_in_svg can resolve
    # image paths regardless of how deeply they are nested under the project dir
    # (e.g. sources/latex_src/Figure/ or any other arbitrary layout).
    image_index = build_image_index(project_dir)

    stats = {
        "total_files": len(svg_files),
        "svgs_repaired": 0,
        "icons_embedded": 0,
        "images_cropped": 0,
        "aspects_fixed": 0,
        "images_embedded": 0,
        "texts_flattened": 0,
        "texts_merged": 0,
        "texts_reflowed": 0,
        "fonts_normalized": 0,
        "text_integrity_restored": 0,
        "rects_converted": 0,
    }

    for svg_path in svg_files:
        # Step 0.5: Repair common malformed XML so downstream parse-based
        # finalizers and PPT export can continue instead of failing hard.
        stats["svgs_repaired"] += repair_svg_file(svg_path)

        # Step 1: Embed icons
        stats["icons_embedded"] += embed_icons_in_file(svg_path, icons_dir)

        # Step 2: Crop images with slice
        stats["images_cropped"] += crop_images_in_svg(svg_path)

        # Step 3: Fix image aspect ratios
        stats["aspects_fixed"] += fix_image_aspect_in_svg(svg_path)

        # Step 4: Embed external images as Base64
        stats["images_embedded"] += embed_images_in_svg(
            svg_path,
            compress=compress,
            max_dimension=max_dimension,
            image_index=image_index,
        )

        text_safe_checkpoint = _read_svg_text(svg_path)
        text_signature_before = _visible_text_signature(text_safe_checkpoint)

        # Step 5: Flatten tspan text
        stats["texts_flattened"] += flatten_text_in_svg(svg_path)

        # Step 5.5: Merge overlapping sibling text nodes emitted by the LLM
        stats["texts_merged"] += merge_adjacent_text_in_svg(svg_path)

        # Step 5.55: Fix icon-text alignment & merge underfilled text lines
        stats["texts_reflowed"] += reflow_text_in_svg(svg_path)

        # Step 5.6: Normalize CSS font fallback stacks to concrete PPT fonts
        stats["fonts_normalized"] += normalize_text_fonts_in_svg(svg_path)

        text_signature_after = _visible_text_signature(_read_svg_text(svg_path))
        if (
            text_signature_before
            and text_signature_after
            and text_signature_before != text_signature_after
        ):
            svg_path.write_text(text_safe_checkpoint, encoding="utf-8")
            stats["text_integrity_restored"] += 1
            stats["fonts_normalized"] += normalize_text_fonts_in_svg(svg_path)

        # Step 6: Convert rounded rects to paths
        stats["rects_converted"] += convert_rounded_rects_in_svg(svg_path)

    return stats


def _read_svg_text(svg_path: Path) -> str:
    try:
        return svg_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _visible_text_signature(svg_content: str) -> str:
    """Return rendered text content in document order, ignoring whitespace."""
    if not svg_content:
        return ""
    try:
        root = ET.fromstring(svg_content)
    except ET.ParseError:
        return ""
    parts: list[str] = []
    for elem in root.iter():
        if elem.tag.endswith("text") or elem.tag.endswith("tspan"):
            if elem.text:
                parts.append(elem.text)
            if elem.tail:
                parts.append(elem.tail)
    return re.sub(r"\s+", "", "".join(parts))
