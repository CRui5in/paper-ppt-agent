"""Typeset explicit mathematical expressions as SVG paths in their layout boxes."""

from __future__ import annotations

from functools import lru_cache
import hashlib
import math
from pathlib import Path
import threading
import xml.etree.ElementTree as ET

_math_lock = threading.Lock()
_MATH_RENDER_VERSION = 5


@lru_cache(maxsize=32)
def _cjk_math_font(characters: str) -> str:
    from matplotlib.font_manager import fontManager
    from matplotlib.ft2font import FT2Font
    from ..svg_to_pptx.font_mapping import parse_font_family

    # Require the actual glyphs instead of relying on a font's family name.
    preferred = parse_font_family('Arial')['ea']
    fonts = sorted(fontManager.ttflist, key=lambda font: (font.name != preferred, font.style != 'normal', font.weight != 400))
    for font in fonts:
        try:
            face = FT2Font(font.fname)
            probes = set(characters) | {'一', '二'}
            glyphs = [face.get_char_index(ord(character)) for character in probes]
            if all(glyphs) and len(set(glyphs)) == len(glyphs):
                return font.name
        except (OSError, RuntimeError):
            continue
    raise ValueError(f'No installed font contains the mathematical text glyphs: {characters}')


@lru_cache(maxsize=256)
def _math_path(expression: str, font_size: float) -> tuple[str, tuple[float, float, float, float]]:
    from .mathjax import math_artwork
    with _math_lock:
        shapes, bounds = math_artwork(expression, font_size)
    if not all(math.isfinite(value) for value in bounds) or bounds[2] <= 0 or bounds[3] <= 0:
        raise ValueError("The expression has no drawable mathematical content")
    return " ".join(shape[0] for shape in shapes), bounds


def render_math_in_tree(root: ET.Element) -> int:
    """Replace <g data-latex=... data-math-box='x y w h'> with vector geometry.

    Expressions remain attached as metadata. Unsupported notation raises a
    rendering error instead of silently displaying a different expression.
    """
    count = 0
    namespace = root.tag.split("}", 1)[0] + "}" if root.tag.startswith("{") else ""
    for element in list(root.iter()):
        expression = element.get("data-latex")
        if expression is None:
            continue
        signature = hashlib.sha256(repr((
            _MATH_RENDER_VERSION,
            expression, element.get("data-math-box"),
            element.get("font-size"), element.get("fill"),
        )).encode()).hexdigest()[:16]
        if element.get("data-math-rendered") == signature:
            continue
        if element.tag.rsplit("}", 1)[-1] != "g":
            raise ValueError("Mathematical expressions require an SVG group")
        try:
            values = [float(v) for v in element.get("data-math-box", "").split()]
            if len(values) != 4 or not all(math.isfinite(v) for v in values):
                raise ValueError("Expected x y width height")
            x, y, width, height = values
            font_size = float(element.get("font-size", "28"))
            if width <= 0 or height <= 0 or not math.isfinite(font_size) or font_size <= 0:
                raise ValueError("Math box and font size must be positive")
            _, (left, bottom, natural_width, natural_height) = _math_path(expression, font_size)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Cannot typeset expression {expression!r}: {exc}") from exc
        scale = min(1.0, width / natural_width, height / natural_height)
        tx = x - left * scale
        ty = y + (height - natural_height * scale) / 2 + (bottom + natural_height) * scale
        element.text = None
        for child in list(element):
            element.remove(child)
        title = ET.SubElement(element, namespace + "title")
        title.text = expression
        from .mathjax import math_artwork
        shapes, _ = math_artwork(expression, font_size)
        ink = element.get("fill", "#1A365D")
        for geometry, fill, stroke, stroke_width, extras in shapes:
            attributes = {
                "d": geometry,
                "fill": ink if fill == "currentColor" else fill,
                "transform": f"matrix({scale:.9g} 0 0 {-scale:.9g} {tx:.9g} {ty:.9g})",
            }
            if stroke != "none":
                attributes.update(stroke=ink if stroke == "currentColor" else stroke,
                                  **{"stroke-width": f"{stroke_width:.9g}"})
                attributes.update(extras)
            ET.SubElement(element, namespace + "path", attributes)
        element.set("data-math-rendered", signature)
        count += 1
    return count


def math_content_bounds(element: ET.Element) -> tuple[float, float, float, float]:
    """Return the occupied equation area, excluding unused math-box space."""
    x, y, width, height = [float(value) for value in element.get('data-math-box', '').split()]
    _, (_, _, natural_width, natural_height) = _math_path(element.get('data-latex', ''), float(element.get('font-size', '28')))
    scale = min(1.0, width/natural_width, height/natural_height)
    return x, y+(height-natural_height*scale)/2, natural_width*scale, natural_height*scale


def render_math_in_svg(svg_path: Path) -> int:
    tree = ET.parse(svg_path)
    count = render_math_in_tree(tree.getroot())
    if count:
        tree.write(svg_path, encoding="unicode")
    return count
