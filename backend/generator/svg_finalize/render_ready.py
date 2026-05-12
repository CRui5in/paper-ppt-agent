"""Prepare SVG files/content for browser preview and PPT export.

Applies the same high-value normalization steps on a temporary copy so
preview/export do not depend on previous finalize state being perfect.
"""

from __future__ import annotations

import uuid
import re
from pathlib import Path

from backend.config import settings

from .embed_icons import embed_icons_in_file
from .embed_images import embed_images_in_svg
from .flatten_tspan import flatten_text_in_svg
from .merge_adjacent_text import merge_adjacent_text_in_svg
from .normalize_fonts import normalize_text_fonts_in_svg
from .repair_svg import repair_svg_file


def prepare_svg_file_for_render(svg_path: Path) -> Path:
    """Return a temporary sibling SVG with assets and text normalized."""
    if not svg_path.exists():
        raise FileNotFoundError(f"SVG file does not exist: {svg_path}")
    base_name = svg_path.name
    while True:
        match = re.match(r"^\.__render_[0-9a-f]+_(.+)$", base_name)
        if not match:
            break
        base_name = match.group(1)
    temp_path = svg_path.with_name(f".__render_{uuid.uuid4().hex}_{base_name}")
    temp_path.write_text(svg_path.read_text(encoding="utf-8"), encoding="utf-8")

    _prepare_in_place(temp_path)
    return temp_path


def prepare_svg_content_for_render(svg_content: str, svg_dir: Path) -> str:
    """Normalize raw SVG content for browser rendering without mutating source files."""
    temp_path = svg_dir / f".__preview_{uuid.uuid4().hex}.svg"
    temp_path.write_text(svg_content, encoding="utf-8")
    try:
        _prepare_in_place(temp_path)
        return temp_path.read_text(encoding="utf-8")
    finally:
        temp_path.unlink(missing_ok=True)


def _prepare_in_place(svg_path: Path) -> None:
    repair_svg_file(svg_path)
    embed_icons_in_file(svg_path, settings.icons_dir)
    embed_images_in_svg(svg_path)
    flatten_text_in_svg(svg_path)
    merge_adjacent_text_in_svg(svg_path)
    normalize_text_fonts_in_svg(svg_path)
