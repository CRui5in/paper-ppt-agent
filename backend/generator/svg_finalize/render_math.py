"""Typeset explicit mathematical expressions as SVG paths in their layout boxes."""

from __future__ import annotations

from functools import lru_cache
import hashlib
import math
from pathlib import Path
import threading
import xml.etree.ElementTree as ET

_math_lock = threading.Lock()


@lru_cache(maxsize=256)
def _math_path(expression: str, font_size: float) -> tuple[str, tuple[float, float, float, float]]:
    # TextPath shares Matplotlib's font caches across render workers.
    with _math_lock:
        from matplotlib.font_manager import FontProperties
        from matplotlib.path import Path as MplPath
        from matplotlib.textpath import TextPath

        prop = FontProperties(math_fontfamily="stix")
        path = TextPath((0, 0), f"${expression}$", size=font_size, prop=prop, usetex=False)
        box = path.get_extents()
        bounds = (float(box.x0), float(box.y0), float(box.width), float(box.height))
        if not all(math.isfinite(v) for v in bounds) or box.width <= 0 or box.height <= 0:
            raise ValueError("The expression has no drawable mathematical content")
        commands = []
        names = {MplPath.MOVETO: "M", MplPath.LINETO: "L", MplPath.CURVE3: "Q", MplPath.CURVE4: "C"}
        for vertices, code in path.iter_segments(simplify=False):
            if code == MplPath.CLOSEPOLY:
                commands.append("Z")
            elif code in names:
                commands.append(names[code] + " ".join(f"{float(v):.6g}" for v in vertices))
        return " ".join(commands), bounds


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
            d, (left, bottom, natural_width, natural_height) = _math_path(expression, font_size)
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
        ET.SubElement(element, namespace + "path", {
            "d": d,
            "fill": element.get("fill", "#1A365D"),
            "transform": f"matrix({scale:.9g} 0 0 {-scale:.9g} {tx:.9g} {ty:.9g})",
        })
        element.set("data-math-rendered", signature)
        count += 1
    return count


def render_math_in_svg(svg_path: Path) -> int:
    tree = ET.parse(svg_path)
    count = render_math_in_tree(tree.getroot())
    if count:
        tree.write(svg_path, encoding="unicode")
    return count
