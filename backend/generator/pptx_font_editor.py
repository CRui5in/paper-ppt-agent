"""PPTX font editor — replaces fonts in text runs by element type.

Opens a .pptx file, traverses all slides, classifies each text run as
heading/body and Western/CJK, then replaces the font family accordingly.

Uses lxml for fast XML manipulation directly on the PPTX zip contents.
No python-pptx parsing overhead — works on raw slide XML.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from lxml import etree


# ── Namespaces ───────────────────────────────────────────────────────────────

NSMAP = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}

# ── Font size threshold ──────────────────────────────────────────────────────
# The `sz` attribute in <a:rPr> is in "hundredths of a point".
# 22pt = 2200 — runs with size >= this are treated as headings.
HEADING_FONT_SIZE_THRESHOLD = 2200

# Default font size when not specified in run/paragraph properties
DEFAULT_FONT_SIZE = 1800  # 18pt

# ── CJK detection ────────────────────────────────────────────────────────────
# Matches CJK Unified Ideographs, Compatibility Ideographs, and common ranges.
_CJK_RE = re.compile(
    r"[一-鿿豈-﫿㐀-䶿　-〿]"
)


def _is_cjk_text(text: str) -> bool:
    """Return True if the text is predominantly CJK characters."""
    if not text:
        return False
    cjk_count = len(_CJK_RE.findall(text))
    total_alpha = len(re.findall(r"[\w]", text))
    # Consider it CJK if CJK chars are >= 30% of total alphanumeric chars,
    # or if there are at least 2 CJK chars.
    return cjk_count >= 2 or (total_alpha > 0 and cjk_count / total_alpha >= 0.3)


def _get_font_size(rpr: etree._Element) -> int | None:
    """Extract font size from <a:rPr sz="2400"> or parent <a:pPr sz="2400">."""
    sz = rpr.get("sz")
    if sz is not None:
        try:
            return int(sz)
        except ValueError:
            pass
    return None


def _get_typeface(element: etree._Element, tag: str) -> str | None:
    """Get <a:latin typeface="..."> or <a:ea typeface="..."> value."""
    ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    child = element.find(f"{{{ns}}}{tag}")
    if child is not None:
        return child.get("typeface")
    return None


def _set_typeface(rpr: etree._Element, tag: str, font: str) -> None:
    """Set or create <a:latin>, <a:ea>, <a:cs> sub-element with given typeface."""
    ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    child = rpr.find(f"{{{ns}}}{tag}")
    if child is not None:
        child.set("typeface", font)
    else:
        new_elem = etree.SubElement(rpr, f"{{{ns}}}{tag}")
        new_elem.set("typeface", font)


class FontReplaceConfig:
    """Configuration for which fonts to use per element/text type."""

    def __init__(
        self,
        western_heading: str | None = None,
        western_body: str | None = None,
        cjk_heading: str | None = None,
        cjk_body: str | None = None,
    ):
        self.western_heading = western_heading
        self.western_body = western_body
        self.cjk_heading = cjk_heading
        self.cjk_body = cjk_body

    def pick_font(self, is_heading: bool, is_cjk: bool) -> str | None:
        if is_heading:
            return self.cjk_heading if is_cjk else self.western_heading
        return self.cjk_body if is_cjk else self.western_body


class FontEditResult:
    """Result of a font replacement operation."""

    def __init__(self):
        self.total_runs = 0
        self.heading_runs = 0
        self.body_runs = 0
        self.cjk_runs = 0
        self.western_runs = 0
        self.fonts_replaced = 0
        self.slides_modified = 0


def _process_slide_xml(slide_xml: str, config: FontReplaceConfig, threshold: int) -> tuple[str, int]:
    """Process a single slide XML string, replacing fonts.

    Returns (modified_xml, replacement_count).
    """
    # Text body <p:txBody> is in the presentation namespace,
    # while <a:p>, <a:pPr>, <a:defRPr>, <a:r>, <a:rPr>, <a:t>, etc.
    # are in the drawing namespace.
    ns_a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    ns_p = "http://schemas.openxmlformats.org/presentationml/2006/main"

    tree = etree.fromstring(slide_xml.encode("utf-8"))
    replacement_count = 0

    # Walk each text body → paragraph → run.
    # This ensures we always know the parent paragraph for font size fallback.
    for tx_body in tree.iter(f"{{{ns_p}}}txBody"):
        for p in tx_body.iter(f"{{{ns_a}}}p"):
            # Determine paragraph-level default font size from
            # <a:pPr><a:defRPr sz="2800"/> — this is how python-pptx
            # stores font size for paragraph styles.
            ppr_font_size: int | None = None
            ppr = p.find(f"{{{ns_a}}}pPr")
            if ppr is not None:
                def_rpr = ppr.find(f"{{{ns_a}}}defRPr")
                if def_rpr is not None:
                    ppr_font_size = _get_font_size(def_rpr)

            for run in p.iter(f"{{{ns_a}}}r"):
                # Find text content
                t_elem = run.find(f"{{{ns_a}}}t")
                if t_elem is None or t_elem.text is None:
                    continue
                text = t_elem.text
                if not text.strip():
                    continue

                # Get or create <a:rPr>
                rpr = run.find(f"{{{ns_a}}}rPr")
                if rpr is None:
                    rpr = etree.Element(f"{{{ns_a}}}rPr")
                    run.insert(0, rpr)

                # Determine font size: run-level first, then paragraph-level
                font_size = _get_font_size(rpr)
                if font_size is None:
                    font_size = ppr_font_size or DEFAULT_FONT_SIZE

                is_heading = font_size >= threshold
                is_cjk = _is_cjk_text(text)

                target_font = config.pick_font(is_heading, is_cjk)
                if target_font is None:
                    continue

                if not is_cjk:
                    _set_typeface(rpr, "latin", target_font)
                else:
                    _set_typeface(rpr, "ea", target_font)
                    _set_typeface(rpr, "latin", target_font)

                replacement_count += 1

    return etree.tostring(tree, encoding="utf-8", xml_declaration=True).decode("utf-8"), replacement_count


def replace_fonts_in_pptx(
    pptx_path: Path,
    config: FontReplaceConfig,
    output_path: Path | None = None,
    heading_threshold: int = HEADING_FONT_SIZE_THRESHOLD,
) -> tuple[Path, FontEditResult]:
    """Replace fonts in a PPTX file based on element type and text type.

    Processes ALL XML files that contain text:
    - ppt/slides/slide*.xml (slide content)
    - ppt/slideMasters/slideMaster*.xml (master layouts — headers, footers, logos)
    - ppt/slideLayouts/slideLayout*.xml (layout placeholders)

    Args:
        pptx_path: Path to the source .pptx file.
        config: Font replacement configuration (4 target fonts).
        output_path: Path for the modified .pptx. If None, overwrites the original.
        heading_threshold: Font size threshold (in hundredths of a point)
            above which text is considered a heading.

    Returns:
        Tuple of (output_path, FontEditResult).
    """
    if output_path is None:
        output_path = pptx_path

    result = FontEditResult()

    with tempfile.TemporaryDirectory(prefix="pptx_font_edit_") as tmp:
        tmp_dir = Path(tmp)

        with zipfile.ZipFile(pptx_path, "r") as zf:
            zf.extractall(tmp_dir)

        # Process ALL XML files that contain text runs
        targets = []
        for subdir in ("ppt/slides", "ppt/slideMasters", "ppt/slideLayouts"):
            dir_path = tmp_dir / subdir
            if dir_path.exists():
                targets.extend(sorted(dir_path.glob("*.xml")))

        if not targets:
            raise ValueError("No slide XML files found in PPTX")

        for xml_file in targets:
            original = xml_file.read_text(encoding="utf-8")
            modified, count = _process_slide_xml(original, config, heading_threshold)
            if count > 0:
                xml_file.write_text(modified, encoding="utf-8")
                result.fonts_replaced += count
                result.slides_modified += 1

        # Repackage as PPTX
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in sorted(path for path in tmp_dir.rglob("*") if path.is_file()):
                arc_name = file_path.relative_to(tmp_dir)
                zf.write(file_path, arc_name)

    return output_path, result


# ── SVG font replacement ─────────────────────────────────────────────────────

SVG_NS = "http://www.w3.org/2000/svg"

# SVG font-size threshold for headings (in px or pt — same unit as the SVG)
SVG_HEADING_FONT_SIZE_THRESHOLD = 20  # >= 20px/pt = heading


def _parse_svg_font_size(size_str: str | None) -> float:
    """Parse SVG font-size string like '24', '16px', '1.2em' to a float."""
    if not size_str:
        return 14.0
    s = size_str.strip().lower()
    if s.endswith("px"):
        return float(s[:-2])
    if s.endswith("pt"):
        return float(s[:-2])
    if s.endswith("em"):
        return float(s[:-2]) * 16  # rough conversion
    try:
        return float(s)
    except ValueError:
        return 14.0


def replace_fonts_in_svg(svg_content: str, config: FontReplaceConfig, threshold: float = SVG_HEADING_FONT_SIZE_THRESHOLD) -> tuple[str, int]:
    """Replace font-family in an SVG string based on element type and text type.

    Modifies <text font-family="..."> attributes.
    Returns (modified_svg, replacement_count).
    """
    try:
        tree = etree.fromstring(svg_content.encode("utf-8"))
    except Exception:
        return svg_content, 0

    replacement_count = 0

    for text_elem in tree.iter(f"{{{SVG_NS}}}text"):
        # Get text content
        text = "".join(t_elem.text or "" for t_elem in text_elem.iter(f"{{{SVG_NS}}}tspan"))
        if not text:
            text = text_elem.text or ""
        if not text.strip():
            continue

        # Get font size
        fs_str = text_elem.get("font-size")
        fs = _parse_svg_font_size(fs_str)

        is_heading = fs >= threshold
        is_cjk = _is_cjk_text(text)

        target_font = config.pick_font(is_heading, is_cjk)
        if target_font is None:
            continue

        text_elem.set("font-family", target_font)
        replacement_count += 1

        # Also update child tspan elements
        for tspan in text_elem.iter(f"{{{SVG_NS}}}tspan"):
            tspan.set("font-family", target_font)

    return etree.tostring(tree, encoding="utf-8", xml_declaration=True).decode("utf-8"), replacement_count


def replace_fonts_in_svg_dir(
    svg_dir: Path,
    config: FontReplaceConfig,
    threshold: float = SVG_HEADING_FONT_SIZE_THRESHOLD,
) -> int:
    """Replace fonts in all SVG files in a directory.

    Returns total number of replacements made.
    """
    total = 0
    if not svg_dir.exists():
        return total

    for svg_file in sorted(svg_dir.glob("*.svg")):
        content = svg_file.read_text(encoding="utf-8")
        modified, count = replace_fonts_in_svg(content, config, threshold)
        if count > 0:
            svg_file.write_text(modified, encoding="utf-8")
            total += count

    return total
