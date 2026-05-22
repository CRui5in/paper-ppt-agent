"""Utility functions for SVG-to-PPTX conversion.

Coordinate mapping, color parsing, font handling, and unit conversion.
"""

from __future__ import annotations

import re
import math

# 1 SVG px = 9525 EMU (at 96 DPI)
EMU_PER_PX = 9525
# 1 px font = 0.75 pt = 75 hundredths of a point
FONT_PX_TO_HUNDREDTHS_PT = 75
# DrawingML angles in 60000ths of a degree
ANGLE_UNIT = 60000

# Named SVG colors (subset of most common)
NAMED_COLORS = {
    "black": "000000", "white": "FFFFFF", "red": "FF0000",
    "green": "008000", "blue": "0000FF", "yellow": "FFFF00",
    "cyan": "00FFFF", "magenta": "FF00FF", "gray": "808080",
    "grey": "808080", "orange": "FFA500", "purple": "800080",
    "navy": "000080", "teal": "008080", "silver": "C0C0C0",
    "maroon": "800000", "olive": "808000", "lime": "00FF00",
    "aqua": "00FFFF", "fuchsia": "FF00FF", "transparent": "000000",
}


def px_to_emu(px: float) -> int:
    """Convert SVG pixels to EMU."""
    return round(px * EMU_PER_PX)


def parse_svg_length(value: object, default: float = 0.0) -> float:
    """Parse a generic SVG numeric/length value.

    Accepts plain numbers as well as values with units like px, pt, em, or %.
    Percentages are returned as their numeric component so "0%" becomes 0.0
    and "50%" becomes 50.0. This is primarily used to avoid export-time
    crashes when a downstream step emits unit-suffixed values.
    """
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return default

    match = re.match(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", text)
    if not match:
        return default

    try:
        return float(match.group(0))
    except ValueError:
        return default


def parse_svg_ratio(value: object, default: float = 0.0) -> float:
    """Parse an SVG ratio-like value.

    Plain decimals are returned as-is. Percentages are normalized to 0..1.
    For example, "50%" becomes 0.5 and "0%" becomes 0.0.
    """
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return default

    number = parse_svg_length(text, default)
    if text.endswith("%"):
        return number / 100.0
    return number


def parse_style_attribute(style: object) -> dict[str, str]:
    """Parse an inline SVG/CSS style attribute into a property map."""
    if style is None:
        return {}
    text = str(style).strip()
    if not text:
        return {}

    result: dict[str, str] = {}
    for declaration in text.split(";"):
        if ":" not in declaration:
            continue
        key, value = declaration.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            result[key] = value
    return result


def get_style_attr(elem: object, attr: str, default: str = "") -> str:
    """Read an SVG attribute, falling back to inline CSS style."""
    getter = getattr(elem, "get", None)
    if not callable(getter):
        return default
    value = getter(attr)
    if value is not None:
        return value
    return parse_style_attribute(getter("style")).get(attr, default)


def font_px_to_half_pts(px: float) -> int:
    """Convert font size in px to hundredths of a point."""
    return round(px * FONT_PX_TO_HUNDREDTHS_PT)


def parse_hex_color(color_str: str) -> str | None:
    """Parse a color string to 6-digit hex (without #).

    Supports: #RGB, #RRGGBB, rgb(r,g,b), named colors.
    Returns None for 'none' or unrecognized values.
    """
    if not color_str:
        return None
    color_str = color_str.strip()

    if color_str.lower() == "none":
        return None

    # Named colors
    if color_str.lower() in NAMED_COLORS:
        return NAMED_COLORS[color_str.lower()]

    # #RGB or #RRGGBB
    if color_str.startswith("#"):
        hex_part = color_str[1:]
        if len(hex_part) == 3:
            return "".join(c * 2 for c in hex_part).upper()
        if len(hex_part) == 6:
            return hex_part.upper()
        return None

    # rgb(r, g, b)
    match = re.match(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", color_str)
    if match:
        r, g, b = int(match.group(1)), int(match.group(2)), int(match.group(3))
        return f"{r:02X}{g:02X}{b:02X}"

    return None


def resolve_url_id(url_str: str) -> str | None:
    """Extract ID from url(#someId) reference."""
    match = re.match(r"url\(#([^)]+)\)", url_str.strip())
    return match.group(1) if match else None


def xml_escape(text: str) -> str:
    """Escape special XML characters."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def is_cjk_char(ch: str) -> bool:
    """Check if a character is CJK (Chinese/Japanese/Korean)."""
    cp = ord(ch)
    return any(
        start <= cp <= end
        for start, end in [
            (0x4E00, 0x9FFF),    # CJK Unified Ideographs
            (0x3040, 0x309F),    # Hiragana
            (0x30A0, 0x30FF),    # Katakana
            (0xAC00, 0xD7AF),    # Hangul Syllables
            (0x3400, 0x4DBF),    # CJK Extension A
            (0x20000, 0x2A6DF),  # CJK Extension B
            (0xFF00, 0xFFEF),    # Fullwidth Forms
        ]
    )


def select_ppt_font_family(text: str, font_stack: str) -> str:
    """Pick one concrete typeface from a CSS font-family stack.

    .. deprecated:: Use :func:`font_mapping.parse_font_family` for new code,
        which returns separate Latin/EA typefaces.  This wrapper returns only
        the Latin typeface for backward compatibility.
    """
    from backend.generator.svg_to_pptx.font_mapping import resolve_latin_font

    return resolve_latin_font(font_stack)


def split_font_stack(font_stack: str) -> list[str]:
    """Split a CSS font-family stack without keeping quote characters."""
    families: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for ch in font_stack:
        if ch in {"'", '"'}:
            quote = None if quote == ch else ch if quote is None else quote
            continue
        if ch == "," and quote is None:
            family = "".join(current).strip()
            if family:
                families.append(family)
            current = []
            continue
        current.append(ch)
    family = "".join(current).strip()
    if family:
        families.append(family)
    return families


def estimate_text_width(text: str, font_size: float, bold: bool = False) -> float:
    """Estimate text width in pixels."""
    width = 0.0
    for ch in text:
        if is_cjk_char(ch):
            width += font_size
        elif ch == " ":
            width += font_size * 0.35
        else:
            width += font_size * 0.62
    if bold:
        width *= 1.05
    return width


def parse_transform(transform_str: str) -> tuple[float, float, float, float, float]:
    """Parse SVG transform attribute.

    Returns:
        (translate_x, translate_y, scale_x, scale_y, angle_degrees)
    """
    dx, dy = 0.0, 0.0
    sx, sy = 1.0, 1.0
    angle = 0.0

    if not transform_str:
        return dx, dy, sx, sy, angle

    # translate(x, y) or translate(x)
    for m in re.finditer(r"translate\(\s*([^,\s]+)(?:\s*[,\s]\s*([^)\s]+))?\s*\)", transform_str):
        dx += float(m.group(1))
        dy += float(m.group(2) or 0)

    # scale(x, y) or scale(x)
    for m in re.finditer(r"scale\(\s*([^,\s]+)(?:\s*[,\s]\s*([^)\s]+))?\s*\)", transform_str):
        sx *= float(m.group(1))
        sy *= float(m.group(2) or m.group(1))

    # rotate(angle)
    for m in re.finditer(r"rotate\(\s*([^,\s)]+)", transform_str):
        angle += float(m.group(1))

    return dx, dy, sx, sy, angle


Matrix = tuple[float, float, float, float, float, float]


def identity_matrix() -> Matrix:
    return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def multiply_matrices(left: Matrix, right: Matrix) -> Matrix:
    """Compose two SVG affine matrices, returning left * right."""
    a1, b1, c1, d1, e1, f1 = left
    a2, b2, c2, d2, e2, f2 = right
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def transform_point(matrix: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return a * x + c * y + e, b * x + d * y + f


def parse_transform_matrix(transform_str: str) -> Matrix:
    """Parse an SVG transform list into a full affine matrix.

    SVG transform commands are composed in list order. For a point column
    vector, that means the accumulated matrix is post-multiplied by each
    command matrix: ``translate(...) scale(...)`` becomes ``T * S``.
    """
    if not transform_str:
        return identity_matrix()

    matrix = identity_matrix()
    for name, raw_args in re.findall(r"([a-zA-Z]+)\(([^)]*)\)", transform_str):
        values = [
            float(part)
            for part in re.findall(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", raw_args)
        ]
        op = name.lower()
        if op == "matrix" and len(values) >= 6:
            op_matrix: Matrix = tuple(values[:6])  # type: ignore[assignment]
        elif op == "translate":
            tx = values[0] if values else 0.0
            ty = values[1] if len(values) > 1 else 0.0
            op_matrix = (1.0, 0.0, 0.0, 1.0, tx, ty)
        elif op == "scale":
            sx = values[0] if values else 1.0
            sy = values[1] if len(values) > 1 else sx
            op_matrix = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        elif op == "rotate":
            angle = math.radians(values[0] if values else 0.0)
            cos_a = math.cos(angle)
            sin_a = math.sin(angle)
            rotate_matrix: Matrix = (cos_a, sin_a, -sin_a, cos_a, 0.0, 0.0)
            if len(values) >= 3:
                cx, cy = values[1], values[2]
                op_matrix = multiply_matrices(
                    multiply_matrices((1.0, 0.0, 0.0, 1.0, cx, cy), rotate_matrix),
                    (1.0, 0.0, 0.0, 1.0, -cx, -cy),
                )
            else:
                op_matrix = rotate_matrix
        elif op == "skewx" and values:
            op_matrix = (1.0, 0.0, math.tan(math.radians(values[0])), 1.0, 0.0, 0.0)
        elif op == "skewy" and values:
            op_matrix = (1.0, math.tan(math.radians(values[0])), 0.0, 1.0, 0.0, 0.0)
        else:
            continue
        matrix = multiply_matrices(matrix, op_matrix)
    return matrix


def matrix_to_legacy_components(matrix: Matrix) -> tuple[float, float, float, float]:
    """Return translation and axis-aligned scales for legacy converters."""
    a, _b, _c, d, e, f = matrix
    return e, f, a, d
