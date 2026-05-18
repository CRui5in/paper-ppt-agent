"""Template manager: loads and provides template context for the pipeline.

Templates are stored in ``assets/templates/layouts/<template_id>/``.  Each
template directory contains:

- ``design_spec.md``  — full design specification
- ``01_cover.svg``    — cover page template
- ``02_chapter.svg``  — chapter/section page template
- ``03_content.svg``  — content page template
- ``04_ending.svg``   — ending page template
- ``02_toc.svg``      — table of contents (optional)

The manager exposes helpers to:
- list available templates
- load a template's design spec and SVG skeletons
- extract the content-area boundary from a template SVG
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from backend.config import settings

_TEMPLATES_ROOT = settings.templates_dir / "layouts"
_INDEX_PATH = _TEMPLATES_ROOT / "layouts_index.json"
_USER_INDEX_PATH = _TEMPLATES_ROOT / "user_templates.json"

# Regex to extract viewBox from an SVG root element.
_VIEWBOX_RE = re.compile(r'viewBox=["\']([^"\']+)["\']')


@dataclass
class TemplateInfo:
    """Lightweight descriptor for a template."""

    template_id: str
    label: str = ""
    summary: str = ""
    tone: str = ""
    theme_mode: str = ""
    category: str = ""
    keywords: list[str] = field(default_factory=list)
    source: str = "builtin"
    editable: bool = False
    slide_count: int = 0
    has_cover: bool = False
    has_chapter: bool = False
    has_content: bool = False
    has_ending: bool = False
    has_toc: bool = False


@dataclass
class TemplateContent:
    """Full loaded template content."""

    info: TemplateInfo
    design_spec: str = ""
    cover_svg: str = ""
    chapter_svg: str = ""
    content_svg: str = ""
    ending_svg: str = ""
    toc_svg: str = ""
    content_area: dict[str, int] = field(default_factory=dict)


def _parse_content_area_from_svg(svg_text: str) -> dict[str, int] | None:
    """Try to find a ``<g id="content-area">`` or ``{{CONTENT_AREA}}`` region."""
    # Look for a content-area group with explicit coordinates.
    m = re.search(
        r'<g\s+id=["\']content-area["\']\s*>',
        svg_text,
        re.IGNORECASE,
    )
    if m:
        # Try to find a rect child as the boundary.
        after = svg_text[m.end(): m.end() + 500]
        rect_m = re.search(
            r'<rect\s+[^>]*x=["\'](\d+)["\'][^>]*y=["\'](\d+)["\']'
            r'[^>]*width=["\'](\d+)["\'][^>]*height=["\'](\d+)["\']',
            after,
        )
        if rect_m:
            return {
                "x": int(rect_m.group(1)),
                "y": int(rect_m.group(2)),
                "width": int(rect_m.group(3)),
                "height": int(rect_m.group(4)),
            }
    return None


def list_templates() -> list[TemplateInfo]:
    """Return descriptors for all installed templates."""
    if not _INDEX_PATH.exists():
        return []
    data = json.loads(_INDEX_PATH.read_text(encoding="utf-8"))
    results: list[TemplateInfo] = []
    categories = data.get("categories", {})
    layouts = data.get("layouts", {})

    # Build category lookup.
    cat_map: dict[str, str] = {}
    for cat_id, cat_data in categories.items():
        for lid in cat_data.get("layouts", []):
            cat_map[lid] = cat_id

    for tid, meta in layouts.items():
        tdir = _TEMPLATES_ROOT / tid
        if not tdir.is_dir():
            continue
        results.append(
            TemplateInfo(
                template_id=tid,
                label=meta.get("label", tid),
                summary=meta.get("summary", ""),
                tone=meta.get("tone", ""),
                theme_mode=meta.get("themeMode", ""),
                category=cat_map.get(tid, ""),
                keywords=meta.get("keywords", []),
                source="builtin",
                editable=False,
                slide_count=meta.get("slideCount", 0),
                has_cover=(tdir / "01_cover.svg").exists(),
                has_chapter=(tdir / "02_chapter.svg").exists(),
                has_content=(tdir / "03_content.svg").exists(),
                has_ending=(tdir / "04_ending.svg").exists(),
                has_toc=(tdir / "02_toc.svg").exists(),
            )
        )

    # Append user-imported templates.
    if _USER_INDEX_PATH.exists():
        try:
            user_data = json.loads(_USER_INDEX_PATH.read_text(encoding="utf-8"))
            for tid, meta in user_data.get("templates", {}).items():
                tdir = _TEMPLATES_ROOT / tid
                if not tdir.is_dir():
                    continue
                results.append(
                    TemplateInfo(
                        template_id=tid,
                        label=meta.get("label", tid),
                        summary=meta.get("summary", ""),
                        tone=meta.get("tone", ""),
                        theme_mode=meta.get("themeMode", ""),
                        category="user-imported",
                        keywords=meta.get("keywords", []),
                        source="user",
                        editable=True,
                        slide_count=meta.get("slideCount", 0),
                        has_cover=(tdir / "01_cover.svg").exists(),
                        has_chapter=(tdir / "02_chapter.svg").exists(),
                        has_content=(tdir / "03_content.svg").exists(),
                        has_ending=(tdir / "04_ending.svg").exists(),
                        has_toc=(tdir / "02_toc.svg").exists(),
                    )
                )
        except (json.JSONDecodeError, OSError):
            pass

    return results


def load_template(template_id: str) -> TemplateContent | None:
    """Load a template by ID.  Returns *None* if not found."""
    tdir = _TEMPLATES_ROOT / template_id
    if not tdir.is_dir():
        return None

    info: TemplateInfo
    # Try to get metadata from built-in index first, then user index.
    meta: dict = {}
    if _INDEX_PATH.exists():
        data = json.loads(_INDEX_PATH.read_text(encoding="utf-8"))
        meta = data.get("layouts", {}).get(template_id, {})
    if not meta and _USER_INDEX_PATH.exists():
        try:
            user_data = json.loads(_USER_INDEX_PATH.read_text(encoding="utf-8"))
            meta = user_data.get("templates", {}).get(template_id, {})
        except (json.JSONDecodeError, OSError):
            pass

    if meta:
        info = TemplateInfo(
            template_id=template_id,
            label=meta.get("label", template_id),
            summary=meta.get("summary", ""),
            tone=meta.get("tone", ""),
            theme_mode=meta.get("themeMode", ""),
            keywords=meta.get("keywords", []),
            source="user" if template_id.startswith("user_") else "builtin",
            editable=template_id.startswith("user_"),
            slide_count=meta.get("slideCount", 0),
            has_cover=(tdir / "01_cover.svg").exists(),
            has_chapter=(tdir / "02_chapter.svg").exists(),
            has_content=(tdir / "03_content.svg").exists(),
            has_ending=(tdir / "04_ending.svg").exists(),
            has_toc=(tdir / "02_toc.svg").exists(),
        )
    else:
        info = TemplateInfo(
            template_id=template_id,
            source="user" if template_id.startswith("user_") else "builtin",
            editable=template_id.startswith("user_"),
            has_cover=(tdir / "01_cover.svg").exists(),
            has_chapter=(tdir / "02_chapter.svg").exists(),
            has_content=(tdir / "03_content.svg").exists(),
            has_ending=(tdir / "04_ending.svg").exists(),
            has_toc=(tdir / "02_toc.svg").exists(),
        )

    def _read(name: str) -> str:
        p = tdir / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    content_svg = _read("03_content.svg")
    content_area = _parse_content_area_from_svg(content_svg) or {
        "x": 40, "y": 100, "width": 1200, "height": 520,
    }

    return TemplateContent(
        info=info,
        design_spec=_read("design_spec.md"),
        cover_svg=_read("01_cover.svg"),
        chapter_svg=_read("02_chapter.svg"),
        content_svg=content_svg,
        ending_svg=_read("04_ending.svg"),
        toc_svg=_read("02_toc.svg"),
        content_area=content_area,
    )


def build_template_context_for_strategist(template: TemplateContent) -> str:
    """Build a text block to inject into the Strategist prompt."""
    lines = [
        "## Template Reference",
        f"- Template ID: {template.info.template_id}",
        f"- Label: {template.info.label}",
        f"- Theme: {template.info.theme_mode}",
        f"- Tone: {template.info.tone}",
        f"- Content area boundary: x={template.content_area['x']}, "
        f"y={template.content_area['y']}, "
        f"width={template.content_area['width']}, "
        f"height={template.content_area['height']}",
        "",
        "The design spec MUST inherit the template's color scheme, typography, "
        "and page structure.  Cover/chapter/ending pages MUST follow the "
        "template SVG structure.  Content pages MUST respect the content area boundary.",
    ]
    return "\n".join(lines)


def build_template_context_for_executor(template: TemplateContent) -> str:
    """Build a text block to inject into the Executor prompt for each page."""
    ca = template.content_area
    return (
        "## Template Constraints\n"
        f"- Content area boundary: x={ca['x']}, y={ca['y']}, "
        f"width={ca['width']}, height={ca['height']}\n"
        "- All content elements MUST be placed within the content area.\n"
        "- Text must not extend beyond content area boundaries.\n"
        "- When a Template Skeleton is provided for a page, you MUST use it as "
        "the starting point. Replace {{PLACEHOLDER}} tokens with actual content. "
        "Preserve ALL decorative elements (gradients, glow effects, grid lines, "
        "accent bars, decorative shapes). Do NOT rewrite the skeleton from scratch.\n"
        "- For content pages without a skeleton, follow the skeleton's color scheme "
        "and layout style from the content page skeleton reference.\n"
    )


def build_template_skeletons(template: TemplateContent) -> dict[str, str]:
    """Extract page-type SVG skeletons for per-page injection."""
    skeletons: dict[str, str] = {}
    if template.cover_svg:
        skeletons["cover"] = template.cover_svg
    if template.chapter_svg:
        skeletons["chapter"] = template.chapter_svg
    if template.content_svg:
        skeletons["content"] = template.content_svg
    if template.ending_svg:
        skeletons["ending"] = template.ending_svg
    if template.toc_svg:
        skeletons["toc"] = template.toc_svg
    return skeletons
