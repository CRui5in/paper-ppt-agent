"""Prepare SVG files/content for browser preview and PPT export.

Applies the same high-value normalization steps on a temporary copy so
preview/export do not depend on previous finalize state being perfect.
"""

from __future__ import annotations

from collections import OrderedDict
import uuid
import re
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

from backend.config import settings

from .embed_icons import embed_icons_in_file
from .embed_images import embed_images_in_svg
from .flatten_tspan import flatten_text_in_svg
from .normalize_fonts import normalize_text_fonts_in_svg
from .render_math import render_math_in_svg
from ..svg_text_layout import reflow_declared_paragraphs, layout_text_groups, normalize_inline_scripts
from ..svg_text_flow import layout_text_flows

_PREVIEW_CONTENT_CACHE_LIMIT = 256
_preview_content_cache: OrderedDict[str, tuple[int, int, str]] = OrderedDict()
_preview_content_cache_lock = threading.Lock()
_preview_prepare_lock = threading.Lock()


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

    try:
        _prepare_in_place(temp_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


def prepare_svg_file_content_for_render(svg_path: Path) -> str:
    """Return normalized SVG text from a temporary render copy."""
    source_path = Path(svg_path)
    stat = source_path.stat()
    cache_key = str(source_path.resolve())
    fingerprint = (stat.st_mtime_ns, stat.st_size)

    with _preview_content_cache_lock:
        cached = _preview_content_cache.get(cache_key)
        if cached and cached[:2] == fingerprint:
            _preview_content_cache.move_to_end(cache_key)
            return cached[2]

    # The prepare passes are CPU-heavy and may be requested concurrently by
    # several preview panes. Serialize cache misses so they do not saturate
    # the shared offload pool and starve lightweight health/status requests.
    with _preview_prepare_lock:
        stat = source_path.stat()
        fingerprint = (stat.st_mtime_ns, stat.st_size)
        with _preview_content_cache_lock:
            cached = _preview_content_cache.get(cache_key)
            if cached and cached[:2] == fingerprint:
                _preview_content_cache.move_to_end(cache_key)
                return cached[2]

        prepared_path = prepare_svg_file_for_render(source_path)
        try:
            content = prepared_path.read_text(encoding="utf-8")
        finally:
            prepared_path.unlink(missing_ok=True)

        with _preview_content_cache_lock:
            _preview_content_cache[cache_key] = (fingerprint[0], fingerprint[1], content)
            _preview_content_cache.move_to_end(cache_key)
            while len(_preview_content_cache) > _PREVIEW_CONTENT_CACHE_LIMIT:
                _preview_content_cache.popitem(last=False)
        return content


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
    ET.parse(svg_path)
    normalize_text_fonts_in_svg(svg_path)
    render_math_in_svg(svg_path)
    tree = ET.parse(svg_path)
    if normalize_inline_scripts(tree.getroot()) + layout_text_flows(tree.getroot()) + reflow_declared_paragraphs(tree.getroot()) + layout_text_groups(tree.getroot()):
        tree.write(svg_path, encoding="unicode")
    embed_icons_in_file(svg_path, settings.icons_dir)
    embed_images_in_svg(svg_path)
    flatten_text_in_svg(svg_path)
    normalize_text_fonts_in_svg(svg_path)
