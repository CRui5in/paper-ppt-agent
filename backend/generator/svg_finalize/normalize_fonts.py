"""Normalize SVG text fonts for preview/export parity."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from backend.generator.svg_to_pptx.font_mapping import parse_font_family
from backend.generator.svg_to_pptx.utils import is_cjk_char

SVG_NS = "http://www.w3.org/2000/svg"

# Font-size threshold (px/pt) — text at or above this is considered a heading.
_HEADING_FONT_SIZE_THRESHOLD = 20.0


@dataclass
class FontReplaceConfig:
    """Four font targets for post-generation replacement."""

    western_heading: str | None = None
    western_body: str | None = None
    cjk_heading: str | None = None
    cjk_body: str | None = None

    def pick_font(self, is_heading: bool, is_cjk: bool) -> str | None:
        if is_heading:
            return self.cjk_heading if is_cjk else self.western_heading
        return self.cjk_body if is_cjk else self.western_body


def normalize_text_fonts_in_svg(svg_path: Path) -> int:
    """Rewrite SVG text font-family stacks to concrete PowerPoint fonts.

    Browsers can resolve CSS font fallback stacks at preview time, while PPTX
    needs a single typeface. Rewriting the SVG first makes preview and export
    read the same font name.
    """
    ET.register_namespace("", SVG_NS)

    try:
        tree = ET.parse(svg_path)
    except ET.ParseError:
        return 0

    changed = normalize_text_fonts_in_tree(tree.getroot())
    if changed:
        tree.write(str(svg_path), xml_declaration=True, encoding="unicode")
    return changed


def normalize_text_fonts_in_tree(root: ET.Element) -> int:
    """Resolve the same concrete fonts before measuring and rendering text."""
    return _normalize_element(root, inherited_font=None)


def _normalize_element(elem: ET.Element, inherited_font: str | None) -> int:
    font_stack = elem.get("font-family") or inherited_font
    changed = 0

    if _is_text_like(elem):
        text = _text_content(elem)
        has_cjk = any(is_cjk_char(ch) for ch in text)
        fonts = parse_font_family(font_stack or "Arial")
        # Use EA font for CJK text (correct preview), Latin font otherwise
        chosen = fonts["ea"] if has_cjk else fonts["latin"]
        if elem.get("font-family") != chosen:
            elem.set("font-family", chosen)
            changed += 1
        font_stack = chosen

    for child in list(elem):
        changed += _normalize_element(child, font_stack)

    return changed


def _is_text_like(elem: ET.Element) -> bool:
    tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
    return tag in {"text", "tspan"}


def _text_content(elem: ET.Element) -> str:
    parts: list[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in list(elem):
        parts.append(_text_content(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts).strip()


def _parse_font_size(size_str: str | None) -> float:
    """Parse SVG font-size string like '24', '16px', '1.2em' to a float."""
    if not size_str:
        return 14.0
    s = size_str.strip().lower()
    if s.endswith("px"):
        return float(s[:-2])
    if s.endswith("pt"):
        return float(s[:-2])
    if s.endswith("em"):
        return float(s[:-2]) * 16
    try:
        return float(s)
    except ValueError:
        return 14.0


def replace_fonts_in_svg(
    svg_path: Path,
    config: FontReplaceConfig,
    threshold: float = _HEADING_FONT_SIZE_THRESHOLD,
) -> int:
    """Replace font-family in an SVG file by heading/body and CJK/Western.

    Returns the number of text elements whose font was changed.
    """
    ET.register_namespace("", SVG_NS)

    try:
        tree = ET.parse(svg_path)
    except ET.ParseError:
        return 0

    changed = _replace_element_fonts(tree.getroot(), config, threshold, inherited_font=None, inherited_size=None)
    if changed:
        tree.write(str(svg_path), xml_declaration=True, encoding="unicode")
    return changed


def _replace_element_fonts(
    elem: ET.Element,
    config: FontReplaceConfig,
    threshold: float,
    inherited_font: str | None,
    inherited_size: float | None,
) -> int:
    font_stack = elem.get("font-family") or inherited_font
    size_str = elem.get("font-size")
    font_size = _parse_font_size(size_str) if size_str else inherited_size
    changed = 0

    if _is_text_like(elem):
        text = _text_content(elem)
        if text.strip():
            has_cjk = any(is_cjk_char(ch) for ch in text)
            is_heading = (font_size or 14.0) >= threshold
            target = config.pick_font(is_heading, has_cjk)
            if target and elem.get("font-family") != target:
                elem.set("font-family", target)
                changed += 1

    for child in list(elem):
        changed += _replace_element_fonts(child, config, threshold, font_stack, font_size)

    return changed


def replace_fonts_in_svg_dir(
    svg_dir: Path,
    config: FontReplaceConfig,
    threshold: float = _HEADING_FONT_SIZE_THRESHOLD,
) -> int:
    """Replace fonts in all SVG files in a directory. Returns total changes."""
    if not svg_dir.exists():
        return 0
    total = 0
    for svg_file in sorted(svg_dir.glob("*.svg")):
        total += replace_fonts_in_svg(svg_file, config, threshold)
    return total
