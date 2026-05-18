"""Detect repeated page chrome (headers / footers / page numbers) in SVGs.

Normalizes text, accumulates per-text occurrence positions across all
slide SVGs, then classifies each candidate into
``remove`` / ``keep`` / ``replace_with_placeholder`` according to the
priority rules in ``design.md`` §chrome_detector and Requirement 5.5.
"""

from __future__ import annotations

import math
import re
import statistics
import string
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

from .types import ChromeTextItem, ElementActionLabel, SlideMetrics

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CANVAS_W = 1280
_DEFAULT_CANVAS_H = 720
_DEFAULT_FONT_SIZE = 12.0
_CHAR_WIDTH_RATIO = 0.6
_POSITION_STABLE_THRESHOLD = 0.01

# CJK punctuation: U+3000-U+303F (CJK symbols and punctuation) and
# U+FF00-U+FFEF (halfwidth/fullwidth forms). Per task spec we treat the
# whole range as "punctuation-like" for the purpose of skipping
# digit/punct-only chrome candidates.
_CJK_PUNCT_CHARS = "".join(chr(c) for c in range(0x3000, 0x3040)) + "".join(
    chr(c) for c in range(0xFF00, 0xFFF0)
)
_DECIMAL_PUNCT_CHARS = frozenset(
    string.digits + string.punctuation + _CJK_PUNCT_CHARS + " \t\u00a0"
)

_WHITESPACE_RE = re.compile(r"\s+")
_TRANSLATE_RE = re.compile(
    r"translate\s*\(\s*([-+]?[\d.eE]+)\s*(?:[,\s]\s*([-+]?[\d.eE]+))?\s*\)"
)
_MATRIX_RE = re.compile(
    r"matrix\s*\(\s*"
    r"([-+]?[\d.eE]+)\s*[,\s]\s*"
    r"([-+]?[\d.eE]+)\s*[,\s]\s*"
    r"([-+]?[\d.eE]+)\s*[,\s]\s*"
    r"([-+]?[\d.eE]+)\s*[,\s]\s*"
    r"([-+]?[\d.eE]+)\s*[,\s]\s*"
    r"([-+]?[\d.eE]+)\s*\)"
)
_FONT_SIZE_STYLE_RE = re.compile(r"font-size\s*:\s*([-+]?[\d.]+)")
_NUMERIC_PREFIX_RE = re.compile(r"^[-+]?[\d.]+")


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def normalize_text(text: str | None) -> str:
    """Strip surrounding whitespace and collapse internal whitespace runs.

    Returns ``""`` for ``None``, empty, or whitespace-only input. Used both
    as the chrome-aggregation key and for ``preserve_texts`` matching.
    """
    if not text:
        return ""
    stripped = text.strip()
    if not stripped:
        return ""
    return _WHITESPACE_RE.sub(" ", stripped)


def iter_text_elements(
    svg_path: Path,
) -> Iterator[tuple[str, float, float, float, float]]:
    """Yield ``(text, x, y, width, height)`` for every ``<text>`` in *svg_path*.

    * ``text`` is the concatenation of the element's text and its
      descendants (including ``<tspan>`` children) before normalization.
    * ``(x, y)`` resolves the element's ``x``/``y`` attributes plus any
      translation extracted from a ``transform`` attribute (``translate(...)``
      and ``matrix(a,b,c,d,e,f)`` translations are summed; rotation/scale
      components are ignored).
    * ``width`` / ``height`` are estimated from ``font-size`` and character
      count (``font-size × len(text) × 0.6`` for width, ``font-size`` for
      height); good enough for chrome stability checks.
    """
    try:
        tree = ET.parse(str(svg_path))
    except (ET.ParseError, OSError):
        return
    root = tree.getroot()
    for elem in root.iter():
        if not _is_text_tag(elem.tag):
            continue
        raw_text = "".join(elem.itertext())
        if not raw_text:
            continue
        x = _parse_float(elem.get("x"), 0.0)
        y = _parse_float(elem.get("y"), 0.0)
        tx, ty = _parse_translation(elem.get("transform"))
        font_size = _resolve_font_size(elem)
        char_count = max(1, len(raw_text.strip()))
        width = max(1.0, font_size * char_count * _CHAR_WIDTH_RATIO)
        height = max(1.0, font_size)
        yield raw_text, x + tx, y + ty, width, height


# ---------------------------------------------------------------------------
# Internal parsing helpers
# ---------------------------------------------------------------------------


def _is_text_tag(tag: object) -> bool:
    if not isinstance(tag, str):
        return False
    # Strip XML namespace if present: "{http://...}text".
    return tag.rsplit("}", 1)[-1] == "text"


def _parse_float(raw: str | None, default: float) -> float:
    if raw is None:
        return default
    raw = raw.strip()
    if not raw:
        return default
    match = _NUMERIC_PREFIX_RE.match(raw)
    if not match:
        return default
    try:
        return float(match.group(0))
    except ValueError:
        return default


def _parse_translation(transform: str | None) -> tuple[float, float]:
    """Sum translation components from a (possibly compound) ``transform``."""
    if not transform:
        return 0.0, 0.0
    tx = ty = 0.0
    for match in _TRANSLATE_RE.finditer(transform):
        tx += float(match.group(1))
        if match.group(2):
            ty += float(match.group(2))
    for match in _MATRIX_RE.finditer(transform):
        tx += float(match.group(5))
        ty += float(match.group(6))
    return tx, ty


def _resolve_font_size(elem: ET.Element) -> float:
    raw = elem.get("font-size")
    if raw:
        match = _NUMERIC_PREFIX_RE.match(raw.strip())
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                pass
    style = elem.get("style") or ""
    match = _FONT_SIZE_STYLE_RE.search(style)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return _DEFAULT_FONT_SIZE


def _is_decimal_or_punct_only(text: str) -> bool:
    """Return True when *text* consists solely of digits/punctuation/whitespace.

    Considers ASCII digits + punctuation, common CJK punctuation
    (U+3000-U+303F), and halfwidth/fullwidth forms (U+FF00-U+FFEF).
    """
    if not text:
        return True
    return all(ch in _DECIMAL_PUNCT_CHARS for ch in text)


def _stddev(values: list[float]) -> float:
    if len(values) <= 1:
        return 0.0
    return statistics.pstdev(values)


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return statistics.fmean(values)


# ---------------------------------------------------------------------------
# Public detect()
# ---------------------------------------------------------------------------


def detect(
    svg_files: list[Path],
    metrics: list[SlideMetrics],
    *,
    preserve_texts: list[str],
    placeholder_hints: dict[str, dict[str, str]],
) -> list[ChromeTextItem]:
    """Return chrome candidates with action and stability annotations.

    A text element is a chrome candidate when, after normalization, it
    appears on ``≥ max(2, ceil(slide_count * 0.4))`` distinct slides and
    is not composed solely of digits and punctuation.

    Action priority (Requirement 5.5):
        1. ``replace_with_placeholder`` — text matches a
           ``placeholder_hints[page_type][name]`` original text;
        2. ``keep`` — text is in *preserve_texts*;
        3. ``remove`` — chrome with ``position_stable_chrome=True``;
        4. ``keep`` — chrome candidate but unstable position.
    """
    slide_count = len(svg_files)
    if slide_count == 0:
        return []

    occurrences: dict[
        str, list[tuple[int, float, float, float, float]]
    ] = defaultdict(list)

    for idx, svg_path in enumerate(svg_files):
        canvas_w, canvas_h, slide_index = _resolve_canvas(metrics, idx)
        if canvas_w <= 0:
            canvas_w = _DEFAULT_CANVAS_W
        if canvas_h <= 0:
            canvas_h = _DEFAULT_CANVAS_H

        for raw_text, x, y, w, h in iter_text_elements(svg_path):
            text = normalize_text(raw_text)
            if not text:
                continue
            if _is_decimal_or_punct_only(text):
                continue
            occurrences[text].append(
                (
                    slide_index,
                    x / canvas_w,
                    y / canvas_h,
                    w / canvas_w,
                    h / canvas_h,
                )
            )

    threshold = max(2, math.ceil(slide_count * 0.4))

    preserve_set = {
        normalized
        for raw in preserve_texts or ()
        if (normalized := normalize_text(raw))
    }
    placeholder_lookup = _build_placeholder_lookup(placeholder_hints)

    items: list[ChromeTextItem] = []
    for text, occ_list in occurrences.items():
        page_set = {entry[0] for entry in occ_list}
        if len(page_set) < threshold:
            continue

        xs = [entry[1] for entry in occ_list]
        ys = [entry[2] for entry in occ_list]
        ws = [entry[3] for entry in occ_list]
        hs = [entry[4] for entry in occ_list]

        mean_x = _mean(xs)
        mean_y = _mean(ys)
        mean_w = _mean(ws)
        mean_h = _mean(hs)
        std_x = _stddev(xs)
        std_y = _stddev(ys)
        std_w = _stddev(ws)
        std_h = _stddev(hs)

        position_stable = max(std_x, std_y) <= _POSITION_STABLE_THRESHOLD
        placeholder_hint = placeholder_lookup.get(text)

        action: ElementActionLabel
        if placeholder_hint is not None:
            action = "replace_with_placeholder"
        elif text in preserve_set:
            action = "keep"
        elif position_stable:
            action = "remove"
        else:
            action = "keep"

        items.append(
            ChromeTextItem(
                text=text,
                pages=sorted(page_set),
                page_count=len(page_set),
                position_stable_chrome=position_stable,
                bbox_norm_mean=(mean_x, mean_y, mean_w, mean_h),
                bbox_norm_stddev=(std_x, std_y, std_w, std_h),
                placeholder_hint=placeholder_hint,
                action=action,
            )
        )

    return items


# ---------------------------------------------------------------------------
# detect() helpers
# ---------------------------------------------------------------------------


def _resolve_canvas(
    metrics: list[SlideMetrics], idx: int
) -> tuple[int, int, int]:
    """Return ``(canvas_w, canvas_h, slide_index)`` with safe defaults."""
    if idx < len(metrics):
        m = metrics[idx]
        if m is not None:
            return (
                m.canvas_width or _DEFAULT_CANVAS_W,
                m.canvas_height or _DEFAULT_CANVAS_H,
                m.slide_index,
            )
    return _DEFAULT_CANVAS_W, _DEFAULT_CANVAS_H, idx + 1


def _build_placeholder_lookup(
    placeholder_hints: dict[str, dict[str, str]] | None,
) -> dict[str, str]:
    """``normalized_text → placeholder_name`` (first match wins)."""
    lookup: dict[str, str] = {}
    if not placeholder_hints:
        return lookup
    for names in placeholder_hints.values():
        if not names:
            continue
        for name, original in names.items():
            key = normalize_text(original)
            if key and key not in lookup:
                lookup[key] = name
    return lookup
