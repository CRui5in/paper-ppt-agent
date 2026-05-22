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

import base64
import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from backend.config import settings

_TEMPLATES_ROOT = settings.templates_dir / "layouts"
_INDEX_PATH = _TEMPLATES_ROOT / "layouts_index.json"
_USER_INDEX_PATH = _TEMPLATES_ROOT / "user_templates.json"

# Regex to extract viewBox from an SVG root element.
_VIEWBOX_RE = re.compile(r'viewBox=["\']([^"\']+)["\']')
_SOURCE_DERIVED_MODES = {"agent", "direct"}
_SVG_TEMPLATE_FIELDS = ("cover_svg", "chapter_svg", "content_svg", "ending_svg", "toc_svg")
_DATA_IMAGE_HREF_RE = re.compile(
    r'(?P<prefix>\b(?:href|xlink:href)=["\'])'
    r'data:image/(?P<mime>png|jpe?g|webp);base64,(?P<data>[^"\']+)'
    r'(?P<suffix>["\'])',
    re.IGNORECASE,
)
_DIRTY_IMPORT_PATTERNS = (
    re.compile(r"<\s*(?:clipPath|mask)\b", re.IGNORECASE),
    re.compile(r"\b(?:clip-path|mask)\s*=", re.IGNORECASE),
    re.compile(r"\bmatrix\s*\(", re.IGNORECASE),
    re.compile(r"data:image/[^;]+;base64,", re.IGNORECASE),
    re.compile(r'x=["\'][^"\']*\s+[^"\']*\s+[^"\']*["\']', re.IGNORECASE),
)


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
    import_mode: str = "builtin"
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
    template_dir: Path | None = None
    viewbox: str = ""
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


def _parse_viewbox(svg_text: str) -> str:
    """Extract the root SVG viewBox, if present."""
    match = _VIEWBOX_RE.search(svg_text or "")
    return match.group(1).strip() if match else ""


def _read_template_manifest(template_dir: Path) -> dict:
    path = template_dir / "manifest.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _template_import_mode(template_dir: Path, *, source: str) -> str:
    if source != "user":
        return "builtin"
    manifest = _read_template_manifest(template_dir)
    import_mode = manifest.get("import_mode")
    if import_mode in {"direct", "agent", "classic", "llm"}:
        return "llm" if import_mode == "classic" else str(import_mode)
    if manifest.get("directTemplate"):
        return "direct"
    if manifest.get("agentTemplateOutputs"):
        return "agent"
    return "llm"


def _is_clean_generation_template(template_dir: Path | None) -> bool:
    if template_dir is None:
        return False
    manifest = _read_template_manifest(template_dir)
    return bool(manifest.get("clean_generation_ready"))


def _is_dirty_user_skeleton(svg_text: str) -> bool:
    """Detect source-PPT SVGs that should not be used as strict skeletons."""
    if not svg_text:
        return False
    score = sum(1 for pattern in _DIRTY_IMPORT_PATTERNS if pattern.search(svg_text))
    if len(svg_text) > 12000 and "data:image/" in svg_text:
        score += 1
    return score >= 2


def _externalize_inline_svg_images_for_project(template: TemplateContent, project_dir: Path) -> None:
    """Materialize inline data-URI template images as project-local assets.

    Imported PPTist templates often contain logo images as ``data:image`` URLs.
    The template preview can render those directly, but generation prompts become
    huge and LLMs tend to omit or redraw them.  Rewriting them to stable
    ``assets/...`` hrefs makes imported clean templates behave like built-ins.
    """

    asset_dirs = [project_dir / subdir / "assets" for subdir in ("svg_output", "svg_final")]
    for asset_dir in asset_dirs:
        asset_dir.mkdir(parents=True, exist_ok=True)

    def _replace(match: re.Match[str]) -> str:
        mime = match.group("mime").lower()
        ext = "jpg" if mime in {"jpg", "jpeg"} else mime
        try:
            payload = base64.b64decode(match.group("data"), validate=False)
        except (ValueError, OSError):
            return match.group(0)
        digest = hashlib.sha256(payload).hexdigest()[:16]
        filename = f"template_inline_{digest}.{ext}"
        for asset_dir in asset_dirs:
            target = asset_dir / filename
            if not target.exists():
                target.write_bytes(payload)
        return f"{match.group('prefix')}assets/{filename}{match.group('suffix')}"

    for field_name in _SVG_TEMPLATE_FIELDS:
        svg_text = getattr(template, field_name, "")
        if not svg_text or "data:image/" not in svg_text:
            continue
        setattr(template, field_name, _DATA_IMAGE_HREF_RE.sub(_replace, svg_text))


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
                import_mode="builtin",
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
                import_mode = _template_import_mode(tdir, source="user")
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
                        import_mode=import_mode,
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
        import_mode = _template_import_mode(tdir, source="user" if template_id.startswith("user_") else "builtin")
        info = TemplateInfo(
            template_id=template_id,
            label=meta.get("label", template_id),
            summary=meta.get("summary", ""),
            tone=meta.get("tone", ""),
            theme_mode=meta.get("themeMode", ""),
            keywords=meta.get("keywords", []),
            source="user" if template_id.startswith("user_") else "builtin",
            import_mode=import_mode,
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
            import_mode=_template_import_mode(tdir, source="user" if template_id.startswith("user_") else "builtin"),
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

    cover_svg = _read("01_cover.svg")
    chapter_svg = _read("02_chapter.svg")
    content_svg = _read("03_content.svg")
    ending_svg = _read("04_ending.svg")
    toc_svg = _read("02_toc.svg")
    content_area = _parse_content_area_from_svg(content_svg) or {
        "x": 40, "y": 100, "width": 1200, "height": 520,
    }
    viewbox = next(
        (
            parsed
            for parsed in (
                _parse_viewbox(content_svg),
                _parse_viewbox(cover_svg),
                _parse_viewbox(chapter_svg),
                _parse_viewbox(ending_svg),
                _parse_viewbox(toc_svg),
            )
            if parsed
        ),
        "",
    )

    return TemplateContent(
        info=info,
        template_dir=tdir,
        viewbox=viewbox,
        design_spec=_read("design_spec.md"),
        cover_svg=cover_svg,
        chapter_svg=chapter_svg,
        content_svg=content_svg,
        ending_svg=ending_svg,
        toc_svg=toc_svg,
        content_area=content_area,
    )


def copy_template_assets_for_project(template: TemplateContent, project_dir: Path) -> None:
    """Make template-local assets resolvable from generated SVG files."""
    if template.template_dir is None:
        return
    src = template.template_dir / "assets"
    for subdir in ("svg_output", "svg_final"):
        dest = project_dir / subdir / "assets"
        dest.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
    _externalize_inline_svg_images_for_project(template, project_dir)


def build_template_context_for_strategist(template: TemplateContent) -> str:
    """Build a text block to inject into the Strategist prompt."""
    source_derived = template.info.import_mode in _SOURCE_DERIVED_MODES
    clean_generation_ready = _is_clean_generation_template(template.template_dir)
    if source_derived and clean_generation_ready:
        structure_rule = (
            "This imported template is a clean generation-ready skeleton. Treat "
            "its page geometry, logos, brand chrome, decorative paths, colors, "
            "and placeholder slots as authoritative, the same way built-in "
            "templates are treated. Do not mirror, rotate, redraw, or omit "
            "template decorations."
        )
    elif source_derived:
        structure_rule = (
            "This template was imported in a source-derived mode (Agent or Direct). "
            "Treat its page geometry as reference material only: inherit the color "
            "scheme, typography, and branding cues, but rebuild unsafe page structure "
            "if the raw SVG geometry is noisy or cramped."
        )
    else:
        structure_rule = (
            "The design spec MUST inherit the template's color scheme, typography, "
            "and page structure.  Cover/chapter/ending pages MUST follow the "
            "template SVG structure.  Content pages MUST respect the content area boundary."
        )
    lines = [
        "## Template Reference",
        f"- Template ID: {template.info.template_id}",
        f"- Label: {template.info.label}",
        f"- Theme: {template.info.theme_mode}",
        f"- Tone: {template.info.tone}",
        f"- Native viewBox: `{template.viewbox}`" if template.viewbox else "",
        f"- Content area boundary: x={template.content_area['x']}, "
        f"y={template.content_area['y']}, "
        f"width={template.content_area['width']}, "
        f"height={template.content_area['height']}",
        "",
        structure_rule,
        "If the template's native viewBox differs from the default canvas, preserve "
        "the template coordinate system in the design spec instead of normalizing "
        "it to a preset canvas.",
    ]
    return "\n".join(line for line in lines if line)


def build_template_context_for_executor(template: TemplateContent) -> str:
    """Build a text block to inject into the Executor prompt for each page."""
    ca = template.content_area
    source_derived = template.info.import_mode in _SOURCE_DERIVED_MODES
    clean_generation_ready = _is_clean_generation_template(template.template_dir)
    if source_derived and clean_generation_ready:
        skeleton_rule = (
            "- This imported template is clean and generation-ready. When a "
            "Template Skeleton is provided, use its root viewBox, coordinate "
            "system, logos/images, colors, fonts, decorative paths, transforms, "
            "and structural chrome as authoritative for that page."
        )
        placeholder_rule = (
            "- Replace {{PLACEHOLDER}} tokens with actual content while preserving "
            "the original slot x/y/width/height, text-anchor, fill color, font "
            "weight, and surrounding decorations. Prefer minimal, local edits "
            "over redrawing the skeleton."
        )
        compatibility_rule = (
            "- Preserve compatibility-sensitive imported constructs such as "
            "clipPath, mask, path transforms, and rotated/flipped shapes when "
            "they are part of the clean skeleton. Do not mirror or flip corner "
            "decorations."
        )
        fallback_rule = (
            "- If a page has no skeleton, follow the clean imported design spec, "
            "content area boundary, and neighboring skeleton pages."
        )
    elif source_derived:
        skeleton_rule = (
            "- This template was imported in a source-derived mode (Agent or Direct). "
            "Treat its SVGs as visual style references, not as rigid data-bound skeletons. "
            "Prefer clean rebuilt layout over preserving raw PPT geometry."
        )
        placeholder_rule = (
            "- Replace {{PLACEHOLDER}} tokens with actual content and preserve decorative "
            "elements only when the skeleton is already clean and placeholder-driven. "
            "Prefer minimal, local edits over redrawing the skeleton."
        )
        compatibility_rule = (
            "- If the imported SVG still contains compatibility-sensitive constructs "
            "such as clipPath, mask, or heavy matrix transforms, do not rely on it as "
            "the strict page skeleton; use it only to infer style, spacing, and asset "
            "placement."
        )
        fallback_rule = (
            "- For source-derived templates without a skeleton, rely on the design spec, "
            "the content area boundary, and the template's color scheme instead of "
            "trying to recreate raw PPT geometry."
        )
    else:
        skeleton_rule = (
            "- When a Template Skeleton is provided for a page, use its root viewBox, "
            "coordinate system, colors, fonts, and structural chrome as authoritative "
            "for that page, even if the general canvas or style preset says otherwise."
        )
        placeholder_rule = (
            "- Replace {{PLACEHOLDER}} tokens with actual content and preserve decorative "
            "elements. Prefer minimal, local edits over redrawing the skeleton."
        )
        compatibility_rule = (
            "- If a skeleton contains compatibility-sensitive constructs from the imported "
            "PPT such as clipPath, preserve the visual structure and make only the "
            "smallest needed cleanup for SVG/PPT compatibility."
        )
        fallback_rule = (
            "- For content pages without a skeleton, follow the skeleton's color scheme "
            "and layout style from the content page skeleton reference."
        )
    lines = [
        "## Template Constraints",
        f"- Native viewBox: `{template.viewbox}`" if template.viewbox else "",
        f"- Content area boundary: x={ca['x']}, y={ca['y']}, "
        f"width={ca['width']}, height={ca['height']}",
        "- All content elements MUST be placed within the content area.",
        "- Text must not extend beyond content area boundaries.",
        skeleton_rule,
        placeholder_rule,
        "- Preserve template image href attributes exactly when they refer to local "
        "`assets/...` files; do not rename them or replace them with invented "
        "`../sources/images/...` paths.",
        "- Imported PPT text may use per-character x coordinates. When replacing a "
        "placeholder with longer text, keep it inside the same visual slot by using "
        "a single centered x value, line breaks, or a smaller font size instead of "
        "leaving many old x positions that make characters overlap.",
        compatibility_rule,
        fallback_rule,
    ]
    return "\n".join(line for line in lines if line) + "\n"


def build_template_skeletons(template: TemplateContent) -> dict[str, str]:
    """Extract page-type SVG skeletons for per-page injection."""
    skeletons: dict[str, str] = {}
    clean_generation_ready = _is_clean_generation_template(template.template_dir)
    if template.info.import_mode == "direct" and not clean_generation_ready:
        return skeletons
    source_derived = template.info.import_mode in _SOURCE_DERIVED_MODES
    if template.cover_svg and (clean_generation_ready or not (source_derived and _is_dirty_user_skeleton(template.cover_svg))):
        skeletons["cover"] = template.cover_svg
    if template.chapter_svg and (clean_generation_ready or not (source_derived and _is_dirty_user_skeleton(template.chapter_svg))):
        skeletons["chapter"] = template.chapter_svg
    if template.content_svg and (clean_generation_ready or not (source_derived and _is_dirty_user_skeleton(template.content_svg))):
        skeletons["content"] = template.content_svg
    if template.ending_svg and (clean_generation_ready or not (source_derived and _is_dirty_user_skeleton(template.ending_svg))):
        skeletons["ending"] = template.ending_svg
    if template.toc_svg and (clean_generation_ready or not (source_derived and _is_dirty_user_skeleton(template.toc_svg))):
        skeletons["toc"] = template.toc_svg
    return skeletons
