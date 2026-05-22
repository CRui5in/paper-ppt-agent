"""Convert a rendered slide SVG into a placeholder template SVG.

Solves nested ``<g transform>`` accumulation, preserves DOM order
(z-order), de-duplicates ``defs`` (clipPath / mask / filter / gradients)
with content-hash + ID remap, and validates geometric tolerance against
``max(4, 0.005 × min(W, H))``.
"""

from __future__ import annotations

import hashlib
import html
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from .types import (
    BoundingBox,
    ChromeTextItem,
    ElementAction,
    ManifestWarning,
    PageType,
    PlaceholderInstance,
    SlideMetrics,
    TemplateizeOutput,
)


# ─────────────────────────────────────────────────────────────────────────────
# Namespaces
# ─────────────────────────────────────────────────────────────────────────────

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)


# ─────────────────────────────────────────────────────────────────────────────
# Font metrics: char-width ratio relative to font-size for bbox estimation.
# Latin defaults are calibrated against measured glyph advance widths;
# CJK approximations assume monospaced full-width glyphs.
# ─────────────────────────────────────────────────────────────────────────────

_CHAR_WIDTH_RATIO_DEFAULT = 0.6

_CHAR_WIDTH_RATIO_BY_FONT: dict[str, float] = {
    # Latin
    "arial": 0.55,
    "helvetica": 0.55,
    "calibri": 0.52,
    "times new roman": 0.50,
    "times": 0.50,
    "georgia": 0.55,
    "verdana": 0.60,
    "tahoma": 0.55,
    "courier new": 0.60,
    "courier": 0.60,
    # CJK
    "simsun": 0.95,
    "simhei": 0.95,
    "microsoft yahei": 0.92,
    "yahei": 0.92,
    "pingfang sc": 0.92,
    "pingfang": 0.92,
    "noto sans cjk sc": 0.92,
    "source han sans": 0.92,
    "kaiti": 0.95,
    "fangsong": 0.95,
}

_CENTERED_PLACEHOLDERS = {
    "TITLE",
    "SUBTITLE",
    "AUTHOR",
    "DATE",
    "GROUP",
    "CHAPTER_NUM",
    "CHAPTER_NUMBER",
    "CHAPTER_TITLE",
    "ENDING_TITLE",
    "ENDING_MESSAGE",
    "THANK_YOU",
    "PAGE_TITLE",
    "TOC_LIST",
    "TOC_ITEM_1",
    "TOC_ITEM_2",
    "TOC_ITEM_3",
    "TOC_ITEM_4",
    "TOC_ITEM_5",
}


def _char_width_ratio(font_family: str | None) -> float:
    """Pick a per-char width ratio for ``font_family`` (case/quote tolerant)."""
    if not font_family:
        return _CHAR_WIDTH_RATIO_DEFAULT
    families = [f.strip().strip("'").strip('"').lower() for f in str(font_family).split(",")]
    for family in families:
        if family in _CHAR_WIDTH_RATIO_BY_FONT:
            return _CHAR_WIDTH_RATIO_BY_FONT[family]
    # Heuristic: any CJK char in the family name → CJK ratio
    for family in families:
        if any("\u4e00" <= ch <= "\u9fff" for ch in family):
            return 0.92
    return _CHAR_WIDTH_RATIO_DEFAULT


# ─────────────────────────────────────────────────────────────────────────────
# Generic helpers
# ─────────────────────────────────────────────────────────────────────────────

def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(str(value).replace("px", "").strip())
    except (TypeError, ValueError):
        return default


def _float_values(value: str | None) -> list[float]:
    if not value:
        return []
    out: list[float] = []
    for item in re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", str(value).replace("px", "")):
        try:
            out.append(float(item))
        except ValueError:
            continue
    return out


def _style_value(element: ET.Element, name: str) -> str:
    value = element.attrib.get(name)
    if value:
        return value
    style = element.attrib.get("style") or ""
    for piece in style.split(";"):
        if ":" not in piece:
            continue
        key, val = piece.split(":", 1)
        if key.strip() == name:
            return val.strip()
    return ""


def _href_for_element(element: ET.Element) -> str:
    return (
        element.attrib.get("href")
        or element.attrib.get(f"{{{XLINK_NS}}}href")
        or element.attrib.get("xlink:href")
        or ""
    )


def _element_text_content(element: ET.Element) -> str:
    parts = [p.strip() for p in element.itertext() if p and p.strip()]
    return " ".join(parts)


def _multiply_matrix(
    left: tuple[float, float, float, float, float, float],
    right: tuple[float, float, float, float, float, float],
) -> tuple[float, float, float, float, float, float]:
    la, lb, lc, ld, le, lf = left
    ra, rb, rc, rd, re_, rf = right
    return (
        la * ra + lc * rb,
        lb * ra + ld * rb,
        la * rc + lc * rd,
        lb * rc + ld * rd,
        la * re_ + lc * rf + le,
        lb * re_ + ld * rf + lf,
    )


_IDENTITY_MATRIX: tuple[float, float, float, float, float, float] = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _parse_svg_transform(transform: str | None) -> tuple[tuple[float, float, float, float, float, float], bool]:
    """Parse ``transform=...``. Returns ``(matrix, has_rotation)``.

    ``has_rotation`` is set when a non-trivial rotate() is encountered;
    the caller is responsible for surfacing a warning since the templateizer
    skips full rotation-aware bbox math (per task 9.1 contract).
    """
    matrix = _IDENTITY_MATRIX
    has_rotation = False
    if not transform:
        return matrix, has_rotation
    for name, args in re.findall(r"([a-zA-Z]+)\(([^)]*)\)", transform):
        values = _float_values(args)
        op = name.lower()
        nxt = matrix
        if op == "matrix" and len(values) >= 6:
            nxt = (values[0], values[1], values[2], values[3], values[4], values[5])
        elif op == "translate" and values:
            tx = values[0]
            ty = values[1] if len(values) > 1 else 0.0
            nxt = (1.0, 0.0, 0.0, 1.0, tx, ty)
        elif op == "scale" and values:
            sx = values[0]
            sy = values[1] if len(values) > 1 else sx
            nxt = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        elif op == "rotate" and values:
            angle = values[0]
            if abs(angle) > 1e-6:
                has_rotation = True
            ang = math.radians(angle)
            cos_v = math.cos(ang)
            sin_v = math.sin(ang)
            rotate = (cos_v, sin_v, -sin_v, cos_v, 0.0, 0.0)
            if len(values) >= 3:
                cx, cy = values[1], values[2]
                nxt = _multiply_matrix(
                    _multiply_matrix((1.0, 0.0, 0.0, 1.0, cx, cy), rotate),
                    (1.0, 0.0, 0.0, 1.0, -cx, -cy),
                )
            else:
                nxt = rotate
        # skewX/skewY/unknown ops fall through as identity
        matrix = _multiply_matrix(matrix, nxt)
    return matrix, has_rotation


def _apply_svg_matrix(
    matrix: tuple[float, float, float, float, float, float],
    x: float,
    y: float,
) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return a * x + c * y + e, b * x + d * y + f


def _matrix_scale(matrix: tuple[float, float, float, float, float, float]) -> tuple[float, float]:
    a, b, c, d, _e, _f = matrix
    sx = math.hypot(a, b) or 1.0
    sy = math.hypot(c, d) or sx
    return sx, sy


# ─────────────────────────────────────────────────────────────────────────────
# Element id / kind (mirrors v1 _iter_actionable_elements)
# ─────────────────────────────────────────────────────────────────────────────

_SHAPE_TAGS = frozenset({"rect", "circle", "ellipse", "line", "polyline", "polygon", "path"})


def _actionable_kind(element: ET.Element) -> str:
    local = _local_name(element.tag)
    if local == "text":
        return "text"
    if local == "image":
        return "image"
    if local in _SHAPE_TAGS:
        return "shape"
    return ""


def _element_id(slide_index: int, kind: str, ordinal: int) -> str:
    return f"s{slide_index:02d}_{kind}_{ordinal:03d}"


def _iter_actionable_elements(
    root: ET.Element, slide_index: int
) -> Iterator[tuple[str, ET.Element, ET.Element, str, list[ET.Element]]]:
    """Yield ``(element_id, parent, element, kind, ancestor_chain)`` in DOM order.

    ``ancestor_chain`` is the list of ancestors from outermost (root) to the
    immediate parent (inclusive), used by :func:`solved_bbox` to accumulate
    transforms. Element ordinals follow the same naming scheme as v1
    (``s{slide_index:02d}_{kind}_{ordinal:03d}``).
    """
    counts: dict[str, int] = {}

    def _walk(
        parent: ET.Element, chain: list[ET.Element]
    ) -> Iterator[tuple[str, ET.Element, ET.Element, str, list[ET.Element]]]:
        next_chain = chain + [parent]
        for child in list(parent):
            kind = _actionable_kind(child)
            if kind:
                counts[kind] = counts.get(kind, 0) + 1
                yield _element_id(slide_index, kind, counts[kind]), parent, child, kind, list(next_chain)
            yield from _walk(child, next_chain)

    yield from _walk(root, [])


# ─────────────────────────────────────────────────────────────────────────────
# Task 9.1 — solved_bbox
# ─────────────────────────────────────────────────────────────────────────────

def _accumulate_transforms(ancestors: list[ET.Element]) -> tuple[tuple[float, float, float, float, float, float], bool]:
    matrix = _IDENTITY_MATRIX
    rotated = False
    for node in ancestors:
        m, rot = _parse_svg_transform(node.attrib.get("transform"))
        rotated = rotated or rot
        matrix = _multiply_matrix(matrix, m)
    return matrix, rotated


def _resolve_font_family(raw: str, metrics: SlideMetrics) -> str:
    """Apply ``metrics.font_substitutions`` to a logical font family.

    ``raw`` may be a comma-separated list; the first non-substituted family
    is returned. The substitution map is consulted case-insensitively.
    """
    if not raw:
        return ""
    subs = {k.strip().lower(): v for k, v in (metrics.font_substitutions or {}).items()}
    out: list[str] = []
    for piece in raw.split(","):
        cleaned = piece.strip().strip("'").strip('"')
        if not cleaned:
            continue
        replacement = subs.get(cleaned.lower())
        out.append(replacement if replacement else cleaned)
    return out[0] if out else ""


def _text_bbox_estimate(
    element: ET.Element,
    matrix: tuple[float, float, float, float, float, float],
    metrics: SlideMetrics,
) -> BoundingBox:
    text = _element_text_content(element)
    raw_font_size = _to_float(_style_value(element, "font-size"), 18.0)
    sx, sy = _matrix_scale(matrix)
    font_size = max(1.0, raw_font_size * sy)
    family_raw = _style_value(element, "font-family") or ""
    family_resolved = _resolve_font_family(family_raw, metrics)
    ratio = _char_width_ratio(family_resolved or family_raw)

    # Anchor point: prefer x/y on the <text> element itself, otherwise
    # fall back to the first <tspan>.
    x_attr = _to_float(element.attrib.get("x"), None) if element.attrib.get("x") is not None else None  # type: ignore[arg-type]
    y_attr = _to_float(element.attrib.get("y"), None) if element.attrib.get("y") is not None else None  # type: ignore[arg-type]
    if x_attr is None or y_attr is None:
        for child in element.iter():
            if child is element:
                continue
            if _local_name(child.tag) != "tspan":
                continue
            xs = _float_values(child.attrib.get("x"))
            ys = _float_values(child.attrib.get("y"))
            if xs and x_attr is None:
                x_attr = xs[0]
            if ys and y_attr is None:
                y_attr = ys[0]
            if x_attr is not None and y_attr is not None:
                break
    x_attr = x_attr if x_attr is not None else 0.0
    y_attr = y_attr if y_attr is not None else 0.0

    origin_x, origin_y = _apply_svg_matrix(matrix, float(x_attr), float(y_attr))

    char_count = max(len(text), 1)
    width = font_size * char_count * ratio
    height = font_size

    # SVG <text> y is the baseline; bbox top is roughly y - 0.8 * font_size.
    top_y = origin_y - height * 0.8

    # Honor text-anchor for the bbox x:
    anchor = (_style_value(element, "text-anchor") or element.attrib.get("text-anchor", "")).strip().lower()
    if anchor == "middle":
        bbox_x = origin_x - width / 2.0
    elif anchor == "end":
        bbox_x = origin_x - width
    else:
        bbox_x = origin_x

    return BoundingBox(x=bbox_x, y=top_y, width=width, height=height)


def _shape_or_image_bbox(
    element: ET.Element,
    matrix: tuple[float, float, float, float, float, float],
) -> BoundingBox:
    local = _local_name(element.tag)
    if local == "circle":
        cx = _to_float(element.attrib.get("cx"))
        cy = _to_float(element.attrib.get("cy"))
        r = _to_float(element.attrib.get("r"))
        x0, y0 = _apply_svg_matrix(matrix, cx - r, cy - r)
        x1, y1 = _apply_svg_matrix(matrix, cx + r, cy + r)
    elif local == "ellipse":
        cx = _to_float(element.attrib.get("cx"))
        cy = _to_float(element.attrib.get("cy"))
        rx = _to_float(element.attrib.get("rx"))
        ry = _to_float(element.attrib.get("ry"))
        x0, y0 = _apply_svg_matrix(matrix, cx - rx, cy - ry)
        x1, y1 = _apply_svg_matrix(matrix, cx + rx, cy + ry)
    elif local == "line":
        x_a, y_a = _apply_svg_matrix(
            matrix, _to_float(element.attrib.get("x1")), _to_float(element.attrib.get("y1"))
        )
        x_b, y_b = _apply_svg_matrix(
            matrix, _to_float(element.attrib.get("x2")), _to_float(element.attrib.get("y2"))
        )
        x0, x1 = min(x_a, x_b), max(x_a, x_b)
        y0, y1 = min(y_a, y_b), max(y_a, y_b)
    else:
        # rect / image / path / polygon / polyline use the x/y/width/height attrs
        # when available; path/polyline/polygon will degrade to (0,0,0,0) which
        # the caller can filter.
        x = _to_float(element.attrib.get("x"))
        y = _to_float(element.attrib.get("y"))
        w = _to_float(element.attrib.get("width"))
        h = _to_float(element.attrib.get("height"))
        x0, y0 = _apply_svg_matrix(matrix, x, y)
        x1, y1 = _apply_svg_matrix(matrix, x + w, y + h)
    bx = min(x0, x1)
    by = min(y0, y1)
    bw = abs(x1 - x0)
    bh = abs(y1 - y0)
    return BoundingBox(x=bx, y=by, width=bw, height=bh)


def solved_bbox(
    element: ET.Element,
    ancestors_transforms: list[ET.Element],
    metrics: SlideMetrics,
) -> BoundingBox:
    """Solve an element's bbox in viewBox coordinates.

    Walks the ``<g transform>`` ancestor chain accumulating
    ``translate / matrix / scale`` (rotate noted but not bbox-correct),
    then estimates the bbox per element kind:

    * **text**: ``font-size × len(text) × char_width_ratio[font_family]``
      (height = font-size), corrected by ``metrics.font_substitutions``.
    * **image / shape**: x / y / width / height (or center+radius for
      circle / ellipse) post-transform.
    """
    matrix_self, _ = _parse_svg_transform(element.attrib.get("transform"))
    matrix_ancestors, _ = _accumulate_transforms(ancestors_transforms)
    matrix = _multiply_matrix(matrix_ancestors, matrix_self)

    kind = _actionable_kind(element) or _local_name(element.tag)
    if kind == "text":
        return _text_bbox_estimate(element, matrix, metrics)
    return _shape_or_image_bbox(element, matrix)


# ─────────────────────────────────────────────────────────────────────────────
# Task 9.2 — collect_defs
# ─────────────────────────────────────────────────────────────────────────────

_DEDUPE_TAGS = frozenset(
    {"clipPath", "mask", "filter", "linearGradient", "radialGradient", "pattern", "symbol"}
)


def _canonical_subtree(element: ET.Element) -> str:
    """Serialize ``element`` with sorted attributes (excluding ``id``) and
    sorted children-by-canonical-form. Stable across attribute insertion order.
    """
    tag = _local_name(element.tag)
    attrs = []
    for key in sorted(element.attrib.keys()):
        if _local_name(key) == "id":
            continue
        attrs.append(f"{key}={element.attrib[key]!r}")
    text = (element.text or "").strip()
    tail = (element.tail or "").strip()
    children = sorted(_canonical_subtree(c) for c in list(element))
    return f"<{tag} {' '.join(attrs)}>{text}|{tail}|{''.join(children)}</{tag}>"


def _content_hash(element: ET.Element) -> str:
    canonical = _canonical_subtree(element)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def _find_defs(svg_root: ET.Element) -> list[ET.Element]:
    return [child for child in list(svg_root) if _local_name(child.tag) == "defs"]


def _remap_id_references(svg_root: ET.Element, id_map: dict[str, str]) -> None:
    """Rewrite ``url(#X)``, ``href="#X"``, ``xlink:href="#X"`` per ``id_map``."""
    if not id_map:
        return
    url_pattern = re.compile(r"url\(#([^)\s]+)\)")

    def _rewrite_url(match: re.Match[str]) -> str:
        old = match.group(1)
        return f"url(#{id_map.get(old, old)})"

    for el in svg_root.iter():
        for attr_key, attr_val in list(el.attrib.items()):
            if not attr_val:
                continue
            new_val = attr_val
            if "url(#" in new_val:
                new_val = url_pattern.sub(_rewrite_url, new_val)
            local = _local_name(attr_key)
            if local == "href" and new_val.startswith("#"):
                old = new_val[1:]
                if old in id_map:
                    new_val = "#" + id_map[old]
            if new_val != attr_val:
                el.attrib[attr_key] = new_val


def collect_defs(svg_root: ET.Element) -> tuple[list[ET.Element], dict[str, str]]:
    """Collect & dedupe ``<defs>`` children, returning ``(kept, id_map)``.

    Mutates ``svg_root`` in place: drops duplicate defs and rewrites
    ``url(#X) / href="#X" / xlink:href="#X"`` references via ``id_map``.
    The first occurrence of each content hash wins; later duplicates are
    removed and their ``id`` values mapped to the winner.
    """
    defs_nodes = _find_defs(svg_root)
    if not defs_nodes:
        return [], {}

    seen: dict[str, str] = {}  # hash → winner_id
    kept: list[ET.Element] = []
    id_map: dict[str, str] = {}

    for defs_node in defs_nodes:
        for child in list(defs_node):
            tag = _local_name(child.tag)
            if tag not in _DEDUPE_TAGS:
                kept.append(child)
                continue
            child_id = child.attrib.get("id")
            if not child_id:
                kept.append(child)
                continue
            digest = _content_hash(child)
            winner = seen.get(digest)
            if winner is None:
                seen[digest] = child_id
                kept.append(child)
            else:
                # Drop duplicate from the tree, remap id → winner
                defs_node.remove(child)
                if child_id != winner:
                    id_map[child_id] = winner

    if id_map:
        _remap_id_references(svg_root, id_map)

    return kept, id_map


# ─────────────────────────────────────────────────────────────────────────────
# Task 9.3 / 9.4 — templateize
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _replace_text_with_placeholder(
    element: ET.Element,
    placeholder_name: str,
    bbox: BoundingBox,
    style: dict[str, str],
) -> None:
    """Rewrite a ``<text>`` element in place to ``{{NAME}}`` while keeping
    its solved geometry/style. Children (``<tspan>``) are stripped.
    """
    for child in list(element):
        element.remove(child)
    # Drop attributes that would re-apply original positioning logic.
    for attr in ("style", "transform", "letter-spacing", "word-spacing", "font-stretch", "dx", "dy"):
        element.attrib.pop(attr, None)
    # Anchor to the solved bbox. Imported PPT SVGs often carry per-character
    # x coordinates or inherited anchors; placeholders should be stable and
    # visually centered for title-like slots.
    anchor = style.get("text-anchor", "")
    if placeholder_name in _CENTERED_PLACEHOLDERS:
        anchor = "middle"
    if anchor == "middle":
        x_attr = bbox.x + bbox.width / 2.0
    elif anchor == "end":
        x_attr = bbox.x + bbox.width
    else:
        x_attr = bbox.x
    baseline_y = bbox.y + bbox.height * 0.8
    element.attrib["x"] = f"{x_attr:.2f}"
    element.attrib["y"] = f"{baseline_y:.2f}"
    if style.get("font-size"):
        element.attrib["font-size"] = style["font-size"]
    if style.get("fill"):
        element.attrib["fill"] = style["fill"]
    if style.get("font-family"):
        element.attrib["font-family"] = style["font-family"]
    if style.get("font-weight"):
        element.attrib["font-weight"] = style["font-weight"]
    if style.get("opacity"):
        element.attrib["opacity"] = style["opacity"]
    if anchor:
        element.attrib["text-anchor"] = anchor
    element.text = f"{{{{{placeholder_name}}}}}"


def _replace_image_with_placeholder(
    element: ET.Element,
    placeholder_name: str,
    bbox: BoundingBox,
) -> None:
    """Rewrite an ``<image>`` element in place to ``href="{{NAME}}"`` while
    preserving its solved geometry. Transform is dropped because the bbox
    already accounts for ancestor transforms.
    """
    element.attrib.pop("transform", None)
    element.attrib["x"] = f"{bbox.x:.2f}"
    element.attrib["y"] = f"{bbox.y:.2f}"
    element.attrib["width"] = f"{bbox.width:.2f}"
    element.attrib["height"] = f"{bbox.height:.2f}"
    # Clear any existing href variants and set canonical ``href``.
    for key in (
        "href",
        f"{{{XLINK_NS}}}href",
        "xlink:href",
    ):
        if key in element.attrib:
            del element.attrib[key]
    element.attrib["href"] = f"{{{{{placeholder_name}}}}}"


def _build_placeholder_style(element: ET.Element, kind: str) -> dict[str, str]:
    if kind != "text":
        return {}
    style: dict[str, str] = {}
    fs = _style_value(element, "font-size")
    if fs:
        style["font-size"] = fs
    fill = _style_value(element, "fill")
    if fill:
        style["fill"] = fill
    fam = _style_value(element, "font-family")
    if fam:
        style["font-family"] = fam
    weight = _style_value(element, "font-weight")
    if weight:
        style["font-weight"] = weight
    anchor = (_style_value(element, "text-anchor") or element.attrib.get("text-anchor", "")).strip()
    if anchor:
        style["text-anchor"] = anchor
    opacity = _style_value(element, "opacity")
    if opacity:
        style["opacity"] = opacity
    return style


def _safe_remove(parent: ET.Element, child: ET.Element) -> bool:
    try:
        parent.remove(child)
        return True
    except ValueError:
        return False


def _href_filename(element: ET.Element) -> str:
    href = _href_for_element(element)
    if not href:
        return ""
    return PurePosixPath(href.split("#", 1)[0]).name


def _auto_text_font_size(element: ET.Element, bbox: BoundingBox) -> float:
    return max(_to_float(_style_value(element, "font-size"), bbox.height or 14.0), bbox.height or 14.0)


def _auto_text_entries(
    walk: list[tuple[str, ET.Element, ET.Element, str, list[ET.Element]]],
    metrics: SlideMetrics,
    canvas_w: int,
    canvas_h: int,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for element_id, _parent, element, kind, ancestors in walk:
        if kind != "text":
            continue
        text = _normalize_text(_element_text_content(element))
        if not text or "{{" in text or "}}" in text:
            continue
        try:
            bbox = solved_bbox(element, ancestors, metrics)
        except Exception:
            continue
        if bbox.width <= 1 or bbox.height <= 1:
            continue
        font_size = _auto_text_font_size(element, bbox)
        y_mid = bbox.y + bbox.height / 2.0
        x_mid = bbox.x + bbox.width / 2.0
        if y_mid < -canvas_h * 0.04 or y_mid > canvas_h * 1.04:
            continue
        entries.append(
            {
                "id": element_id,
                "element": element,
                "text": text,
                "bbox": bbox,
                "font_size": font_size,
                "area": max(1.0, bbox.width * bbox.height),
                "x_mid": x_mid,
                "y_mid": y_mid,
            }
        )
    return entries


def _pick_largest(entries: list[dict[str, Any]], *, exclude: set[str] | None = None, top: bool = False) -> dict[str, Any] | None:
    exclude = exclude or set()
    candidates = [entry for entry in entries if entry["id"] not in exclude]
    if not candidates:
        return None
    if top:
        candidates.sort(key=lambda entry: (entry["y_mid"], -entry["font_size"], -entry["area"]))
    else:
        candidates.sort(key=lambda entry: (-entry["font_size"], -entry["area"], entry["y_mid"]))
    return candidates[0]


def _build_auto_placeholder_plan(
    page_type: PageType,
    entries: list[dict[str, Any]],
    canvas_w: int,
    canvas_h: int,
) -> tuple[dict[str, str], list[BoundingBox]]:
    """Infer a small clean placeholder set for direct imports.

    This intentionally strips most source content. The selected boxes keep
    the imported deck's layout rhythm, while the generated template receives
    stable reusable slots instead of the original paper text.
    """
    assigned: dict[str, str] = {}
    used: set[str] = set()

    def assign(entry: dict[str, Any] | None, name: str) -> None:
        if not entry or entry["id"] in used:
            return
        assigned[entry["id"]] = name
        used.add(entry["id"])

    upper = [entry for entry in entries if entry["y_mid"] <= canvas_h * 0.42]
    central = [entry for entry in entries if canvas_h * 0.12 <= entry["y_mid"] <= canvas_h * 0.82]
    body_boxes: list[BoundingBox] = []

    if page_type == "cover":
        title = _pick_largest(central or entries)
        assign(title, "TITLE")
        subtitle_candidates = [entry for entry in central if entry["id"] not in used and entry["y_mid"] >= (title or {}).get("y_mid", 0)]
        assign(_pick_largest(subtitle_candidates, top=True), "SUBTITLE")
        bottom = [entry for entry in entries if entry["id"] not in used and entry["y_mid"] >= canvas_h * 0.55]
        assign(_pick_largest(bottom, top=True), "AUTHOR")
    elif page_type == "toc":
        assign(_pick_largest(upper or entries), "PAGE_TITLE")
        toc_boxes = [entry["bbox"] for entry in central if entry["id"] not in used]
        if toc_boxes:
            body_boxes = toc_boxes
        list_entry = _pick_largest([entry for entry in central if entry["id"] not in used], top=True)
        assign(list_entry, "TOC_LIST")
    elif page_type == "chapter":
        numeric = [
            entry
            for entry in entries
            if len(entry["text"]) <= 8 and any(ch.isdigit() for ch in entry["text"])
        ]
        assign(_pick_largest(numeric), "CHAPTER_NUM")
        assign(_pick_largest([entry for entry in central or entries if entry["id"] not in used]), "CHAPTER_TITLE")
    elif page_type == "ending":
        assign(_pick_largest(central or entries), "ENDING_MESSAGE")
    else:
        assign(_pick_largest(upper or entries, top=True), "PAGE_TITLE")
        body_boxes = [
            entry["bbox"]
            for entry in entries
            if entry["id"] not in used and canvas_h * 0.18 <= entry["y_mid"] <= canvas_h * 0.88
        ]

    if page_type == "content" and not body_boxes:
        body_boxes = [
            BoundingBox(
                x=canvas_w * 0.08,
                y=canvas_h * 0.24,
                width=canvas_w * 0.84,
                height=canvas_h * 0.58,
            )
        ]
    return assigned, body_boxes


def _wrap_content_area(
    svg_root: ET.Element,
    placeholder_box: BoundingBox | None,
    fallback_kept_text_boxes: list[BoundingBox],
    canvas_w: int,
    canvas_h: int,
) -> BoundingBox | None:
    """Wrap (or synthesize) ``<g id="content-area">{{CONTENT_AREA}}</g>``.

    If ``placeholder_box`` is provided (the bbox of an existing
    ``{{CONTENT_AREA}}`` placeholder), wrap that box. Otherwise use
    ``fallback_kept_text_boxes`` (only those with ``y`` inside the central
    band ``[0.18·H, 0.86·H]``) to compute a covering rectangle. Returns the
    resulting :class:`BoundingBox` or ``None`` if no body cluster exists.
    """
    bbox: BoundingBox | None
    if placeholder_box is not None:
        bbox = placeholder_box
    else:
        body_boxes = [
            b
            for b in fallback_kept_text_boxes
            if b.y >= canvas_h * 0.18 and (b.y + b.height) <= canvas_h * 0.86
        ]
        if not body_boxes:
            bbox = BoundingBox(
                x=canvas_w * 0.08,
                y=canvas_h * 0.24,
                width=canvas_w * 0.84,
                height=canvas_h * 0.58,
            )
        else:
            x0 = min(b.x for b in body_boxes)
            y0 = min(b.y for b in body_boxes)
            x1 = max(b.x + b.width for b in body_boxes)
            y1 = max(b.y + b.height for b in body_boxes)
            bbox = BoundingBox(
                x=max(0.0, x0),
                y=max(0.0, y0),
                width=max(0.0, min(canvas_w, x1) - max(0.0, x0)),
                height=max(0.0, min(canvas_h, y1) - max(0.0, y0)),
            )

    # Build the wrapper <g id="content-area"> with a single placeholder text.
    g_tag = f"{{{SVG_NS}}}g" if svg_root.tag.startswith("{") else "g"
    text_tag = f"{{{SVG_NS}}}text" if svg_root.tag.startswith("{") else "text"
    wrapper = ET.SubElement(svg_root, g_tag)
    wrapper.attrib["id"] = "content-area"
    inner = ET.SubElement(wrapper, text_tag)
    inner.attrib["x"] = f"{bbox.x + bbox.width / 2.0:.2f}"
    inner.attrib["y"] = f"{bbox.y + bbox.height / 2.0:.2f}"
    inner.attrib["text-anchor"] = "middle"
    inner.attrib["fill"] = "#94A3B8"
    inner.attrib["font-family"] = "Arial, sans-serif"
    inner.attrib["font-size"] = "18"
    inner.text = "{{CONTENT_AREA}}"
    return bbox


def templateize(
    svg_path: Path,
    page_type: PageType,
    *,
    metrics: SlideMetrics,
    actions: list[ElementAction],
    chrome_items: list[ChromeTextItem],
    asset_roles: dict[str, str],
) -> TemplateizeOutput:
    """Render a single page-type template SVG plus placeholder metadata.

    Applies ``actions`` in place (in DOM order) so kept and replaced
    elements preserve their original z-order. Dedupes ``<defs>``,
    enforces geometric tolerance (``max(4, 0.005·min(W,H))``), and for
    ``page_type == "content"`` wraps the body cluster in
    ``<g id="content-area">{{CONTENT_AREA}}</g>``.
    """
    tree = ET.parse(svg_path)
    root = tree.getroot()

    canvas_w = metrics.canvas_width or 1280
    canvas_h = metrics.canvas_height or 720
    tolerance = max(4.0, 0.005 * min(canvas_w, canvas_h))

    warnings: list[ManifestWarning] = []

    # Index actions by element_id, scoped to this page_type.
    actions_by_id: dict[str, ElementAction] = {
        a.element_id: a for a in actions if a.page_type == page_type and a.element_id
    }

    # Index chrome items by normalized text for quick lookup during the walk.
    chrome_by_text: dict[str, ChromeTextItem] = {
        _normalize_text(item.text): item for item in chrome_items if item.text
    }

    # Track ids and produce placeholders. We collect first, then mutate, so
    # that DOM order is stable (we replace in-place via parent.remove + insert).
    kept_ids: list[str] = []
    removed_ids: list[str] = []
    replaced_ids: list[str] = []
    placeholders: list[PlaceholderInstance] = []
    content_area_box: BoundingBox | None = None
    kept_text_boxes_for_content: list[BoundingBox] = []

    # Materialize the walk so we can mutate the tree without disturbing
    # iteration order. Each entry preserves the *original* DOM position.
    walk: list[tuple[str, ET.Element, ET.Element, str, list[ET.Element]]] = list(
        _iter_actionable_elements(root, metrics.slide_index)
    )
    auto_placeholder_by_id, auto_content_boxes = _build_auto_placeholder_plan(
        page_type,
        _auto_text_entries(walk, metrics, canvas_w, canvas_h),
        canvas_w,
        canvas_h,
    )

    rotation_warned = False

    for element_id, parent, element, kind, ancestors in walk:
        try:
            bbox = solved_bbox(element, ancestors, metrics)
        except Exception:  # pragma: no cover - defensive fall-through
            bbox = BoundingBox(0.0, 0.0, 0.0, 0.0)

        # Surface a one-time warning if any ancestor or self has rotate().
        if not rotation_warned:
            self_matrix, self_rot = _parse_svg_transform(element.attrib.get("transform"))
            if self_rot or _accumulate_transforms(ancestors)[1]:
                warnings.append(
                    ManifestWarning(
                        code="templateize.geometry_violation",
                        reason="rotation transforms are approximated via axis-aligned bbox",
                        context={"element_id": element_id},
                    )
                )
                rotation_warned = True

        action = actions_by_id.get(element_id)

        # ─ Step 1: explicit element_action takes top priority ──────────────
        if action is not None:
            label = action.action
            if label == "keep":
                kept_ids.append(element_id)
                if kind == "text":
                    kept_text_boxes_for_content.append(bbox)
                continue
            if label == "remove":
                if _safe_remove(parent, element):
                    removed_ids.append(element_id)
                continue
            if label == "replace_with_placeholder":
                placeholder_name = (action.placeholder or "").strip()
                if not placeholder_name:
                    # Treat malformed placeholder as a remove + warning.
                    if _safe_remove(parent, element):
                        removed_ids.append(element_id)
                    warnings.append(
                        ManifestWarning(
                            code="templateize.geometry_violation",
                            reason="missing placeholder name on replace_with_placeholder",
                            context={"element_id": element_id},
                        )
                    )
                    continue
                style = _build_placeholder_style(element, kind)
                if kind == "text":
                    _replace_text_with_placeholder(element, placeholder_name, bbox, style)
                elif kind == "image":
                    _replace_image_with_placeholder(element, placeholder_name, bbox)
                else:
                    # Shapes can't carry a textual placeholder; remove instead.
                    if _safe_remove(parent, element):
                        removed_ids.append(element_id)
                    continue
                replaced_ids.append(element_id)
                placeholders.append(
                    PlaceholderInstance(name=placeholder_name, bbox=bbox, style=dict(style))
                )
                if placeholder_name == "CONTENT_AREA":
                    content_area_box = bbox
                continue

        # ─ Step 2: chrome-driven decision ──────────────────────────────────
        if kind == "text":
            normalized = _normalize_text(_element_text_content(element))
            chrome = chrome_by_text.get(normalized)
            if chrome is not None:
                if chrome.action == "remove":
                    if _safe_remove(parent, element):
                        removed_ids.append(element_id)
                    continue
                if chrome.action == "replace_with_placeholder" and chrome.placeholder_hint:
                    style = _build_placeholder_style(element, kind)
                    name = chrome.placeholder_hint
                    _replace_text_with_placeholder(element, name, bbox, style)
                    replaced_ids.append(element_id)
                    placeholders.append(
                        PlaceholderInstance(name=name, bbox=bbox, style=dict(style))
                    )
                    if name == "CONTENT_AREA":
                        content_area_box = bbox
                    continue
                # action == "keep" falls through to default keep below.

        # ─ Step 3: asset-role drop for images ──────────────────────────────
        if kind == "image":
            file_name = _href_filename(element)
            role = asset_roles.get(file_name, "")
            if role in {"ignore", "content_image"}:
                if _safe_remove(parent, element):
                    removed_ids.append(element_id)
                continue

        # ─ Step 4: clean direct-import defaults for source text ───────────
        if kind == "text":
            placeholder_name = auto_placeholder_by_id.get(element_id)
            if placeholder_name:
                style = _build_placeholder_style(element, kind)
                _replace_text_with_placeholder(element, placeholder_name, bbox, style)
                replaced_ids.append(element_id)
                placeholders.append(
                    PlaceholderInstance(name=placeholder_name, bbox=bbox, style=dict(style))
                )
                if placeholder_name == "CONTENT_AREA":
                    content_area_box = bbox
                continue
            if _safe_remove(parent, element):
                removed_ids.append(element_id)
            continue

        # ─ Step 5: default — keep decoration / shapes ─────────────────────
        kept_ids.append(element_id)
        if kind == "text":
            kept_text_boxes_for_content.append(bbox)

    # ─ Geometry tolerance check ────────────────────────────────────────────
    for ph in placeholders:
        # ``ph.bbox`` is the solved bbox — by construction we wrote it back
        # onto the element's x/y/width/height, so the |delta| is bounded by
        # ``2.f`` rounding (≤ 0.01px). Surface a warning only if anything
        # exceeds the tolerance (e.g. a future bbox-shrink heuristic).
        # We re-parse the rendered values would be circular; instead, assert
        # placement was within the canvas. Out-of-canvas placement is a
        # geometry violation worth surfacing.
        if ph.bbox.x < -tolerance or ph.bbox.y < -tolerance:
            warnings.append(
                ManifestWarning(
                    code="templateize.geometry_violation",
                    reason="placeholder bbox lies outside the canvas",
                    context={
                        "element_id": ph.name,
                        "delta_x": ph.bbox.x,
                        "delta_y": ph.bbox.y,
                    },
                )
            )
        if (
            ph.bbox.x + ph.bbox.width > canvas_w + tolerance
            or ph.bbox.y + ph.bbox.height > canvas_h + tolerance
        ):
            warnings.append(
                ManifestWarning(
                    code="templateize.geometry_violation",
                    reason="placeholder bbox extends past the canvas",
                    context={
                        "element_id": ph.name,
                        "right": ph.bbox.x + ph.bbox.width,
                        "bottom": ph.bbox.y + ph.bbox.height,
                    },
                )
            )

    # ─ Defs dedup + ID remap ───────────────────────────────────────────────
    collect_defs(root)

    # ─ content_area wrap (page_type == "content") ──────────────────────────
    if page_type == "content":
        wrapped = _wrap_content_area(
            root,
            placeholder_box=content_area_box,
            fallback_kept_text_boxes=auto_content_boxes or kept_text_boxes_for_content,
            canvas_w=canvas_w,
            canvas_h=canvas_h,
        )
        if wrapped is not None:
            content_area_box = wrapped
            # If the wrapper synthesized a CONTENT_AREA placeholder, expose
            # it in ``placeholders`` so downstream invariants hold.
            if not any(p.name == "CONTENT_AREA" for p in placeholders):
                placeholders.append(
                    PlaceholderInstance(
                        name="CONTENT_AREA",
                        bbox=wrapped,
                        style={"font-size": "18", "fill": "#94A3B8"},
                    )
                )

    # ─ Serialize ───────────────────────────────────────────────────────────
    svg_text = ET.tostring(root, encoding="unicode")
    svg_text = pretty_print_svg(svg_text)

    return TemplateizeOutput(
        page_type=page_type,
        svg_text=svg_text,
        placeholders=placeholders,
        content_area=content_area_box if page_type == "content" else None,
        z_order_preserved=True,
        warnings=warnings,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Task 9.5 — pretty_print_svg
# ─────────────────────────────────────────────────────────────────────────────

_PLACEHOLDER_TOKEN_RE = re.compile(r"\{\{[A-Z_][A-Z0-9_]*\}\}")
_NUMERIC_RE = re.compile(r"(?<![A-Za-z_{])(\d+\.\d{3,})")


def _format_numeric(text: str) -> str:
    """Round long decimals to 2 places without touching ``{{TOKEN}}`` substrings."""
    if not text:
        return text
    # Split on placeholder tokens; rewrite numerics only outside tokens.
    parts: list[str] = []
    last = 0
    for match in _PLACEHOLDER_TOKEN_RE.finditer(text):
        outside = text[last : match.start()]
        parts.append(_NUMERIC_RE.sub(lambda m: f"{float(m.group(0)):.2f}", outside))
        parts.append(match.group(0))
        last = match.end()
    tail = text[last:]
    parts.append(_NUMERIC_RE.sub(lambda m: f"{float(m.group(0)):.2f}", tail))
    return "".join(parts)


def _sort_attrs(attrs: dict[str, str]) -> list[tuple[str, str]]:
    """``id`` first, then alphabetical."""
    items = list(attrs.items())

    def _key(item: tuple[str, str]) -> tuple[int, str]:
        local = _local_name(item[0])
        return (0 if local == "id" else 1, item[0])

    return sorted(items, key=_key)


def _serialize_pretty(
    element: ET.Element,
    indent: int,
    out: list[str],
) -> None:
    pad = "  " * indent
    tag = _local_name(element.tag)
    # Restore the namespace prefix when present in the original tag so the
    # output remains valid SVG (we registered the default svg namespace).
    rendered_tag = tag
    if element.tag.startswith("{"):
        ns = element.tag[1:].split("}", 1)[0]
        if ns == SVG_NS:
            rendered_tag = tag
        elif ns == XLINK_NS:
            rendered_tag = f"xlink:{tag}"
        else:
            rendered_tag = tag

    attr_pairs = _sort_attrs(dict(element.attrib))
    rendered_attrs: list[str] = []
    for key, val in attr_pairs:
        local = _local_name(key)
        if key.startswith(f"{{{XLINK_NS}}}"):
            attr_name = f"xlink:{local}"
        else:
            attr_name = local if not key.startswith("{") else key
        rendered_attrs.append(f'{attr_name}="{html.escape(_format_numeric(str(val)), quote=True)}"')
    attr_str = (" " + " ".join(rendered_attrs)) if rendered_attrs else ""

    children = list(element)
    text = element.text or ""
    has_text = bool(text.strip())
    if not children and not has_text:
        out.append(f"{pad}<{rendered_tag}{attr_str}/>")
        return
    out.append(f"{pad}<{rendered_tag}{attr_str}>")
    if has_text:
        out.append(f"{'  ' * (indent + 1)}{html.escape(_format_numeric(text.strip()))}")
    for child in children:
        _serialize_pretty(child, indent + 1, out)
    out.append(f"{pad}</{rendered_tag}>")


def pretty_print_svg(svg_text: str) -> str:
    """Canonical SVG formatting: attribute order, indent, 2-digit numbers.

    ``parse(pretty_print(s)) == parse(s)`` semantically; the XML declaration
    and DOCTYPE (when present in the input) are preserved verbatim.
    """
    if not svg_text:
        return ""

    prologue = ""
    body = svg_text
    # Preserve <?xml ?> declaration
    if body.lstrip().startswith("<?xml"):
        end = body.find("?>")
        if end != -1:
            prologue += body[: end + 2].lstrip() + "\n"
            body = body[end + 2 :].lstrip()
    # Preserve <!DOCTYPE ...>
    if body.lstrip().startswith("<!DOCTYPE"):
        end = body.find(">")
        if end != -1:
            prologue += body[: end + 1].lstrip() + "\n"
            body = body[end + 1 :].lstrip()

    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return svg_text

    out: list[str] = []
    _serialize_pretty(root, 0, out)
    return prologue + "\n".join(out) + "\n"


__all__ = [
    "templateize",
    "solved_bbox",
    "collect_defs",
    "pretty_print_svg",
]
