"""SVG Executor agent: generates SVG page code from design spec.

The executor runs a page-by-page generation loop. Each page is checked by
the static :mod:`backend.generator.svg_critic` before being accepted. If
the critic finds violations, a targeted repair prompt is fed back to the
LLM (bounded retries, with slightly lower temperature on each retry) so
that regeneration is *informed* rather than blind.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from PIL import Image

from backend.config import settings
from backend.generator.svg_critic import CriticConfig, CriticReport, Violation, check_svg
from backend.generator.visual_critic import VisualCriticConfig, visual_check
from backend.llm import LLMMessage, LLMProvider, LLMResponse
from backend.orchestrator.manuscript import (
    extract_page_type,
    split_manuscript_pages,
    strip_page_type_metadata,
)
from backend.orchestrator.provider_guidance import (
    deepseek_executor_guidance,
    is_deepseek_provider,
)
from backend.usage.tracker import reset_usage_context, set_usage_context

# `[[FIG:fig_007_p9_page]]` style tokens emitted by the research agent.
FIG_TOKEN_RE = re.compile(r"\[\[FIG:([A-Za-z0-9_\-]+)\]\]")
IMAGE_HREF_RE = re.compile(r"<image\b[^>]*\bhref=[\"']([^\"']+)[\"']", re.IGNORECASE)
DATA_ICON_RE = re.compile(
    r"<use\b(?=[^>]*\bdata-icon=([\"'])([^\"']+)\1)[^>]*(?:/>|>\s*</use>)",
    re.IGNORECASE | re.DOTALL,
)
PSEUDO_ICON_BADGE_TEXT = frozenset({"P", "Δ", "!", "G", "?", "i", "I", "✓", "×"})
FIGURE_LABEL_RE = re.compile(
    r"\b(fig(?:ure)?|table)\s*\.?\s*(\d+)\b|([图表])\s*(\d+)",
    re.IGNORECASE,
)

PROMPT_PATH = Path(__file__).parent / "prompts" / "executor.md"

# Default number of static critic checks per page, including the first
# post-generation check. A value of 3 allows two repair calls.
DEFAULT_MAX_CRITIC_ATTEMPTS = 3

# Max prior page exchanges kept in the conversation (sliding window).
# Each page generates 1 user + 1 assistant exchange plus bounded repair rounds.
# Keeping 2 pages of context balances style consistency vs. token cost.
MAX_PRIOR_PAGES_IN_CONTEXT = 2

# Initial response plus bounded same-page retries when no SVG can be extracted.
MAX_SVG_EXTRACTION_ATTEMPTS = 3

CriticCallback = Callable[[int, int, CriticReport, str | None, str | None], Awaitable[None]]
SvgUpdateCallback = Callable[[int, str], Awaitable[None]]


def _resolve_fig_tokens(
    page_content: str,
    figure_inventory: list[dict] | None,
) -> tuple[str, list[dict], list[str]]:
    """Replace `[[FIG:id]]` tokens with explicit real-figure references."""
    if not figure_inventory:
        return page_content, [], []

    by_id = _figure_alias_map(figure_inventory)
    used: list[dict] = []
    seen: set[str] = set()
    rejected: list[str] = []

    def _replace(match: re.Match) -> str:
        fig_id = match.group(1)
        fig = by_id.get(fig_id)
        if fig is None:
            return f"[[MISSING_FIG:{fig_id}]]"
        resolved_id = Path(str(fig.get("path") or "")).stem or fig_id
        line = _line_containing(page_content, match.start())
        mismatch = _figure_label_mismatch(line, str(fig.get("caption") or ""))
        if mismatch:
            rejected.append(f"{fig_id}: {mismatch}")
            return f"[[REJECTED_FIG:{fig_id} — {mismatch}]]"
        if resolved_id not in seen:
            seen.add(resolved_id)
            used.append(fig)
        return _paper_figure_reference_line(fig, resolved_id)

    return FIG_TOKEN_RE.sub(_replace, page_content), used, rejected


def _figure_alias_map(figure_inventory: list[dict]) -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    for fig in figure_inventory:
        path = str(fig.get("path") or "")
        if path:
            stem = Path(path).stem
            by_id[stem] = fig
            label = _extract_figure_label(str(fig.get("caption") or ""))
            if label:
                kind, number = label
                aliases = {f"{kind}{number}", f"{kind}_{number}"}
                if kind == "figure":
                    aliases.update({f"fig{number}", f"fig_{number}"})
                for alias in aliases:
                    by_id.setdefault(alias, fig)
    return by_id


def _paper_figure_reference_line(fig: dict, resolved_id: str | None = None) -> str:
    resolved_id = resolved_id or Path(str(fig.get("path") or "")).stem
    path = fig.get("path") or ""
    cap = (fig.get("caption") or "").strip().replace("\n", " ")
    if len(cap) > 160:
        cap = cap[:157] + "..."
    return f"[PAPER FIGURE — id={resolved_id}, href=\"{path}\", caption: {cap}]"


def _figures_from_design_spec_for_page(
    design_spec: str,
    page_num: int,
    figure_inventory: list[dict] | None,
) -> list[dict]:
    """Recover slide image assignments from the design spec as a safety net."""
    if not figure_inventory:
        return []
    page_pattern = (
        rf"(?ims)^#+\s*(?:slide|page)\s*0*{page_num}\b.*?"
        rf"(?=^#+\s*(?:slide|page)\s*0*\d+\b|\Z)"
    )
    page_match = re.search(page_pattern, design_spec)
    if not page_match:
        return []
    block = page_match.group(0)
    image_ids = re.findall(r"(?im)^\s*-\s*\*\*Image\*\*\s*:\s*`([^`]+)`", block)
    if not image_ids:
        return []
    by_id = _figure_alias_map(figure_inventory)
    figures: list[dict] = []
    seen: set[str] = set()
    for image_id in image_ids:
        fig = by_id.get(image_id.strip())
        if not fig:
            continue
        stem = Path(str(fig.get("path") or "")).stem
        if stem in seen:
            continue
        seen.add(stem)
        figures.append(fig)
    return figures


def _icon_from_inventory_table(design_spec: str, page_num: int) -> dict | None:
    """Look up icon from Section VI inventory table."""
    vi_pattern = r"(?ims)^#+\s*VI\.?\s*Icon\s+Usage.*?(?=^#+\s*VII\.?\s|\Z)"
    vi_match = re.search(vi_pattern, design_spec)
    if not vi_match:
        return None
    vi_text = vi_match.group(0)
    # Match table rows like | 03 | `chunk/target` | ... |
    row_pattern = rf"\|\s*0*{page_num}\s*\|\s*`([^`]+)`\s*\|"
    row_match = re.search(row_pattern, vi_text)
    if not row_match:
        return None
    icon_name = row_match.group(1).strip()
    if not icon_name or icon_name.lower() in {"none", "null", "n/a"}:
        return None
    return {"name": icon_name, "size": 40, "color": "#2563EB", "note": ""}


def _icon_from_design_spec_for_page(design_spec: str, page_num: int) -> dict | None:
    """Recover a slide-level icon assignment from the design spec.

    Looks in Section IX first (Icon: line), then Section VI inventory table.
    """
    # Primary: Section IX Icon: line
    page_pattern = (
        rf"(?ims)^#+\s*(?:slide|page)\s*0*{page_num}\b.*?"
        rf"(?=^#+\s*(?:slide|page)\s*0*\d+\b|\Z)"
    )
    page_match = re.search(page_pattern, design_spec)
    if page_match:
        block = page_match.group(0)
        icon_match = re.search(
            r"(?im)^\s*-\s*\*\*Icon\*\*\s*:\s*`([^`]+)`([^\n]*)",
            block,
        )
        if icon_match:
            icon_name = icon_match.group(1).strip()
            if not icon_name or icon_name.lower() in {"none", "null", "n/a"}:
                return None

            note = icon_match.group(2).strip()
            size = 40
            size_match = re.search(r"(\d{1,3})\s*[x×]\s*(\d{1,3})\s*px?", note, re.IGNORECASE)
            if size_match:
                size = max(int(size_match.group(1)), int(size_match.group(2)))

            color = "#2563EB"
            color_match = re.search(r"`(#[0-9A-Fa-f]{3,8})`|(#[0-9A-Fa-f]{3,8})", note)
            if color_match:
                color = color_match.group(1) or color_match.group(2)

            return {"name": icon_name, "size": size, "color": color, "note": note}

    # Secondary: Section VI inventory table
    return _icon_from_inventory_table(design_spec, page_num)


def _icon_guidance_block(icon_assignment: dict | None) -> str:
    """Constrain icon rendering to explicit design-spec placeholders."""
    if not icon_assignment:
        return (
            "## Icon Guidance\n"
            "- This slide has no explicit design-spec icon assignment. Do not add "
            "`<use data-icon=\"...\"/>` placeholders or decorative icons.\n"
            "- Do not simulate icons with standalone letter/symbol badges inside "
            "small squares or circles, such as `P`, `Δ`, `!`, `G`, `?`, or `i`.\n"
            "- If cards need structure, use plain numbered markers only when the "
            "design spec asks for `Card Marker: numbered`; otherwise use title text "
            "and spacing.\n"
            "- If a technical concept needs a visual cue, draw a micro diagram that "
            "shows structure, such as distribution bins, residual arrows, error "
            "growth, gate sliders, stage flow, or mini charts."
        )

    name = str(icon_assignment["name"])
    size = int(icon_assignment.get("size") or 40)
    color = str(icon_assignment.get("color") or "#2563EB")
    return (
        "## Icon Guidance\n"
        f"- The design spec assigns this slide exactly one semantic icon: `{name}`.\n"
        "- Render it with a real icon placeholder so the finalizer can embed the "
        "icon library asset. Do not redraw it with inline `<path>`, `<polygon>`, "
        "or decorative geometry.\n"
        f"- Use this form with double quotes: "
        f"`<use data-icon=\"{name}\" x=\"...\" y=\"...\" width=\"{size}\" "
        f"height=\"{size}\" fill=\"{color}\"/>`.\n"
        "- Keep it sparse and purposeful; use no other icon placeholders on this slide.\n"
        "- Do not add extra standalone letter/symbol badges as fake icons in cards."
    )


def _figure_guidance_block(
    used: list[dict],
    rejected: list[str] | None = None,
    *,
    source: str = "manuscript",
) -> str:
    """Constrain real paper-figure hrefs without limiting native SVG visuals."""
    rejected = rejected or []
    if not used:
        lines = [
            "## Paper Figure Guidance\n"
            "- This slide does not contain an explicit paper-figure token. "
            "Do not invent a paper-figure `<image href>` path. This restriction "
            "applies only to extracted paper figures; native SVG diagrams, charts, "
            "and visual treatments remain available."
        ]
        for item in rejected:
            lines.append(
                f"- Rejected paper figure token: {item}. Do not use its href; "
                "summarize the idea with native SVG or omit the image."
            )
        return "\n".join(lines)

    lines = ["## Paper Figure Guidance"]
    if source == "design_spec":
        lines.append(
            "- The design spec explicitly assigns the following paper figure(s) to this slide. "
            "Include them unless the slide would become unreadable; preserve the listed aspect ratio."
        )
    for fig in used:
        path = fig.get("path") or ""
        cap = (fig.get("caption") or "").strip().replace("\n", " ")
        if len(cap) > 160:
            cap = cap[:157] + "..."

        dim_info = ""
        w = int(fig.get("natural_width") or 0)
        h = int(fig.get("natural_height") or 0)
        if w > 0 and h > 0:
            dim_info = f" actual dimensions: {w}x{h} (ratio {w / h:.2f});"
        else:
            try:
                img_path = Path(path)
                if img_path.exists():
                    with Image.open(img_path) as img:
                        w, h = img.size
                        ratio = w / h
                        dim_info = f" actual dimensions: {w}x{h} (ratio {ratio:.2f});"
            except Exception:
                pass

        lines.append(
            f"- Allowed paper figure href: \"{path}\";{dim_info} caption: {cap}"
        )
    lines.append(
        "Use only the listed hrefs for extracted paper figures. Never substitute "
        "a different paper-figure href, reuse one from another slide, or invent "
        "a paper-figure path. This does not restrict native SVG visuals."
    )
    return "\n".join(lines)


# -- Character budget & image layout helpers ----------------------------------

# Content area defaults for PPT 16:9 (may be overridden by design_spec)
_DEFAULT_CONTENT_AREA = {"x": 40, "y": 100, "width": 1200, "height": 520}


def _estimate_capacity(width: int, height: int, font_size: int = 16) -> int:
    """Estimate max characters that fit in a content area."""
    line_height = int(font_size * 1.3)
    max_lines = max(1, height // line_height)
    # Average char width: blend of CJK (1.0) and Latin (0.55), assume 0.75
    avg_char_w = font_size * 0.75
    chars_per_line = max(1, int(width / avg_char_w))
    return max_lines * chars_per_line


# Width presets for common layout scenarios (content area = 1200×520)
_LAYOUT_WIDTHS = {
    "full": 1200,          # full-width body text
    "half_left": 560,      # left column in two-column
    "half_right": 560,     # right column in two-column
    "card": 480,           # card content area
    "card_narrow": 380,    # narrow card (3-column)
}

# Regex to identify structural elements in manuscript Markdown
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
_BULLET_RE = re.compile(r"^[\s]*[-*•]\s+(.+)$", re.MULTILINE)


def _char_budget_block(manuscript_page: str) -> str:
    """Return a per-element character budget guide for the page.

    Instead of a single whole-page estimate, this analyses the manuscript
    structure (headings, bullets, paragraphs) and tells the LLM how many
    characters fit per line for each common layout scenario.
    """
    text = manuscript_page.strip()
    est_chars = len(text)

    # Whole-page capacity for density warning
    total_capacity = _estimate_capacity(
        _DEFAULT_CONTENT_AREA["width"],
        _DEFAULT_CONTENT_AREA["height"],
    )
    ratio = est_chars / total_capacity if total_capacity else 0

    # Per-layout character-per-line estimates at common font sizes
    lines = []
    lines.append("## Character Budget Per Line")
    lines.append("Use these limits to decide when to wrap text. "
                 "Do NOT wrap prematurely when >40% of line width is unused.")
    lines.append("")
    lines.append("| Layout | Width | font 16 cpl | font 18 cpl | font 22 cpl |")
    lines.append("|--------|-------|-------------|-------------|-------------|")
    for name, w in _LAYOUT_WIDTHS.items():
        cpl16 = int(w / (16 * 0.75))
        cpl18 = int(w / (18 * 0.75))
        cpl22 = int(w / (22 * 0.75))
        lines.append(f"| {name} | {w}px | {cpl16} | {cpl18} | {cpl22} |")

    # Count structural elements for a concrete hint
    headings = _HEADING_RE.findall(text)
    bullets = _BULLET_RE.findall(text)
    if headings or bullets:
        lines.append("")
        lines.append(f"Page structure: {len(headings)} heading(s), "
                     f"{len(bullets)} bullet(s), ~{est_chars} total chars.")

    # Density warning
    if ratio > 0.8:
        lines.append("")
        lines.append(
            f"⚠ **Density warning**: ~{est_chars} chars / ~{total_capacity} capacity "
            f"({ratio:.0%}). Consider splitting across multiple slides or condensing."
        )
    else:
        lines.append("")
        lines.append(
            f"Density: ~{est_chars} chars / ~{total_capacity} capacity ({ratio:.0%})."
        )

    return "\n".join(lines)


def _figure_layout_guidance(used_figures: list[dict]) -> str:
    """For each paper figure, recommend layout based on its aspect ratio."""
    if not used_figures:
        return ""
    ca_w = _DEFAULT_CONTENT_AREA["width"]
    ca_h = _DEFAULT_CONTENT_AREA["height"]
    lines = ["## Image Layout Recommendations"]
    for fig in used_figures:
        path = fig.get("path") or ""
        try:
            img_path = Path(path)
            if img_path.exists():
                with Image.open(img_path) as img:
                    w, h = img.size
                    r = w / h
                    if r > 1.2:
                        layout = "top-bottom"
                        img_w, img_h = ca_w, int(ca_w / r)
                        txt_area = f"{ca_w}x{ca_h - img_h - 20}"
                    else:
                        layout = "left-right"
                        img_h, img_w = ca_h, int(ca_h * r)
                        txt_area = f"{ca_w - img_w - 20}x{ca_h}"
                    lines.append(
                        f"- {Path(path).stem}: ratio={r:.2f}, "
                        f"recommended={layout}, "
                        f"image={img_w}x{img_h}, text_area={txt_area}"
                    )
                    continue
        except Exception:
            pass
        lines.append(f"- {Path(path).stem}: unable to read dimensions")
    return "\n".join(lines)


def _line_containing(text: str, offset: int) -> str:
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    if end == -1:
        end = len(text)
    return text[start:end]


def _extract_figure_label(text: str) -> tuple[str, str] | None:
    match = FIGURE_LABEL_RE.search(text)
    if not match:
        return None
    if match.group(1):
        kind_raw = match.group(1).lower()
        kind = "table" if kind_raw == "table" else "figure"
        return kind, match.group(2)
    kind = "figure" if match.group(3) == "图" else "table"
    return kind, match.group(4)


def _figure_label_mismatch(reference_line: str, caption: str) -> str | None:
    requested = _extract_figure_label(reference_line)
    actual = _extract_figure_label(caption)
    if not requested or not actual:
        return None
    if requested != actual:
        req_kind, req_num = requested
        actual_kind, actual_num = actual
        return (
            f"requested {req_kind} {req_num}, but inventory caption is "
            f"{actual_kind} {actual_num}"
        )
    return None


def _paper_figure_key_from_href(href: str) -> str | None:
    if href.startswith("data:"):
        return None
    normalized = href.replace("\\", "/")
    stem = Path(normalized).stem
    if "/sources/images/" in normalized or stem.startswith("fig_"):
        return stem
    return None


def _validate_paper_figure_refs(
    svg_content: str,
    *,
    allowed_figures: list[dict],
    used_paper_figures: dict[str, int],
) -> CriticReport:
    allowed_keys = {
        Path(str(fig.get("path") or "")).stem
        for fig in allowed_figures
        if fig.get("path")
    }
    hrefs = IMAGE_HREF_RE.findall(svg_content)
    paper_keys = [
        key for href in hrefs if (key := _paper_figure_key_from_href(href)) is not None
    ]
    violations: list[Violation] = []

    for key in sorted(set(paper_keys)):
        if key not in allowed_keys:
            violations.append(
                Violation(
                    rule="paper_figure_not_allowed",
                    severity="error",
                    detail=(
                        f'Paper figure "{key}" is not allowed for this slide. '
                        "Remove it or replace it with one of the current page's "
                        "explicitly allowed paper-figure hrefs."
                    ),
                )
            )
        if paper_keys.count(key) > 1:
            violations.append(
                Violation(
                    rule="paper_figure_duplicate_on_slide",
                    severity="error",
                    detail=(
                        f'Paper figure "{key}" appears multiple times on this slide. '
                        "Use it once at most, or replace repeated copies with native SVG."
                    ),
                )
            )
        previous_page = used_paper_figures.get(key)
        if previous_page is not None:
            violations.append(
                Violation(
                    rule="paper_figure_reused_from_previous_slide",
                    severity="error",
                    detail=(
                        f'Paper figure "{key}" was already used on slide {previous_page}. '
                        "Do not repeat extracted paper images across slides; redraw the "
                        "idea with native SVG or choose a different explicitly allowed figure."
                    ),
                )
            )

    return CriticReport(passed=not violations, violations=violations)


def _validate_icon_refs(
    svg_content: str,
    *,
    required_icon: str | None,
) -> CriticReport:
    placeholders = [
        {"quote": match.group(1), "name": match.group(2)}
        for match in DATA_ICON_RE.finditer(svg_content)
    ]
    names = [item["name"] for item in placeholders]
    violations: list[Violation] = []

    for item in placeholders:
        if item["quote"] != '"':
            violations.append(
                Violation(
                    rule="icon_placeholder_quote_unsupported",
                    severity="error",
                    detail=(
                        f'Icon placeholder "{item["name"]}" uses single quotes. '
                        "Use double quotes so the icon finalizer can embed it."
                    ),
                )
            )

    if required_icon:
        if required_icon not in names:
            violations.append(
                Violation(
                    rule="required_icon_missing",
                    severity="error",
                    detail=(
                        f'This slide is assigned icon "{required_icon}" in the design spec, '
                        "but the SVG does not contain the required "
                        f'`<use data-icon="{required_icon}" .../>` placeholder. '
                        "Do not redraw the icon manually with inline paths."
                    ),
                )
            )
        for name in sorted(set(names)):
            if name != required_icon:
                violations.append(
                    Violation(
                        rule="unassigned_icon_placeholder",
                        severity="error",
                        detail=(
                            f'Icon placeholder "{name}" is not assigned to this slide. '
                            f'Use only "{required_icon}" or remove the extra placeholder.'
                        ),
                    )
                )
    elif names:
        for name in sorted(set(names)):
            violations.append(
                Violation(
                    rule="unassigned_icon_placeholder",
                    severity="error",
                    detail=(
                        f'Icon placeholder "{name}" is not assigned to this slide. '
                        "Remove it; restrained icon mode allows icons only on slides "
                        "with an explicit design-spec Icon line."
                    ),
                )
            )

    if not required_icon:
        violations.extend(_pseudo_icon_badge_violations(svg_content))

    return CriticReport(passed=not violations, violations=violations)


def _pseudo_icon_badge_violations(svg_content: str) -> list[Violation]:
    try:
        root = ET.fromstring(svg_content)
    except ET.ParseError:
        return []

    small_rects: list[tuple[float, float, float, float]] = []
    small_circles: list[tuple[float, float, float]] = []
    for elem in root.iter():
        tag = _local_tag(elem.tag)
        if tag == "rect":
            x = _float_attr(elem, "x")
            y = _float_attr(elem, "y")
            w = _float_attr(elem, "width")
            h = _float_attr(elem, "height")
            if 24 <= w <= 72 and 24 <= h <= 72:
                small_rects.append((x, y, w, h))
        elif tag == "circle":
            r = _float_attr(elem, "r")
            if 10 <= r <= 36:
                small_circles.append((_float_attr(elem, "cx"), _float_attr(elem, "cy"), r))

    violations: list[Violation] = []
    for elem in root.iter():
        if _local_tag(elem.tag) != "text":
            continue
        text = "".join(elem.itertext()).strip()
        if text not in PSEUDO_ICON_BADGE_TEXT:
            continue
        x = _float_attr(elem, "x")
        y = _float_attr(elem, "y")
        font_size = _float_attr(elem, "font-size", 18.0)
        inside_rect = any(
            rx <= x <= rx + rw and ry <= y <= ry + rh + font_size * 0.4
            for rx, ry, rw, rh in small_rects
        )
        inside_circle = any(
            abs(x - cx) <= r * 0.75 and abs(y - (cy + font_size * 0.35)) <= r
            for cx, cy, r in small_circles
        )
        if inside_rect or inside_circle:
            violations.append(
                Violation(
                    rule="pseudo_icon_badge_not_allowed",
                    severity="error",
                    detail=(
                        f'Standalone badge "{text}" looks like a fake icon, but this '
                        "slide has `Icon: None`. Replace it with a numbered card marker "
                        "only if requested, or with a micro diagram such as distribution "
                        "bins, residual arrows, error growth, a gate slider, or stage flow."
                    ),
                )
            )
    return violations


def _local_tag(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _float_attr(elem: ET.Element, name: str, default: float = 0.0) -> float:
    try:
        return float(elem.get(name, default))
    except (TypeError, ValueError):
        return default


def _merge_reports(*reports: CriticReport) -> CriticReport:
    violations: list[Violation] = []
    canvas = None
    for report in reports:
        violations.extend(report.violations)
        canvas = canvas or report.canvas
    return CriticReport(
        passed=all(report.passed for report in reports),
        violations=violations,
        canvas=canvas,
    )


async def generate_svg_pages(
    design_spec: str,
    manuscript: str,
    project_dir: Path,
    llm: LLMProvider,
    model: str,
    *,
    style: str = "academic",
    language: str = "en",
    detail_level: str = "normal",
    extra_instruction: str = "",
    target_pages: set[int] | None = None,
    critic_config: CriticConfig | None = None,
    on_critic: CriticCallback | None = None,
    on_svg_update: SvgUpdateCallback | None = None,
    figure_inventory: list[dict] | None = None,
    enable_visual_critic: bool = False,
    max_critic_attempts: int = DEFAULT_MAX_CRITIC_ATTEMPTS,
    visual_qa_max_attempts: int = 1,
    visual_critic_config: VisualCriticConfig | None = None,
    template_context: str | None = None,
    template_skeletons: dict[str, str] | None = None,
) -> AsyncIterator[tuple[int, str]]:
    """Generate SVG code for each slide page sequentially."""
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    standards_path = settings.references_dir / "shared-standards-essential.md"
    if not standards_path.exists():
        standards_path = settings.references_dir / "shared-standards.md"
    standards = ""
    if standards_path.exists():
        standards = standards_path.read_text(encoding="utf-8")

    pages = split_manuscript_pages(manuscript)
    svg_output_dir = project_dir / "svg_output"
    svg_output_dir.mkdir(parents=True, exist_ok=True)
    repair_archive_dir = project_dir / "svg_archive" / "repair"
    used_paper_figures: dict[str, int] = {}
    max_critic_attempts = max(
        1,
        int(max_critic_attempts or DEFAULT_MAX_CRITIC_ATTEMPTS),
    )
    visual_qa_max_attempts = max(0, int(visual_qa_max_attempts or 0))

    extra_sections = []
    if extra_instruction:
        extra_sections.append(extra_instruction)
    if is_deepseek_provider(llm, model):
        extra_sections.append(deepseek_executor_guidance(detail_level))
    if template_context:
        extra_sections.append(template_context)
    extra_block = "\n\n" + "\n\n".join(extra_sections) if extra_sections else ""
    conversation: list[LLMMessage] = [
        LLMMessage.system(system_prompt),
        LLMMessage.user(
            f"## Design Specification\n\n{design_spec}\n\n"
            f"## SVG Technical Standards\n\n{standards}\n\n"
            f"## Fixed Runtime Configuration\n\n"
            f"- Selected style preset: {style}\n"
            f"- Selected language: {language}\n"
            f"- Selected detail level: {detail_level}\n"
            f"- Do not replace the requested style with another preset.\n"
            f"- All visible SVG text must follow the selected language unless a proper noun must stay in its original form.\n\n"
            f"Total pages to generate: {len(pages)}\n\n"
            f"You will generate SVG code for each page sequentially. "
            f"I will provide the content for each page one at a time."
            f"{extra_block}"
        ),
        LLMMessage.assistant(
            "Understood. I have the design specification and technical constraints. "
            "Please provide the content for page 1."
        ),
    ]

    # Save full initial prompt for debugging
    try:
        debug_dir = project_dir / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = debug_dir / "executor_prompt.md"
        parts = []
        for msg in conversation:
            parts.append(f"--- ROLE: {msg.role} ---\n\n{msg.content}")
        prompt_file.write_text("\n\n".join(parts), encoding="utf-8")
    except Exception:
        pass

    # Track how many page exchanges we've appended beyond the preamble
    # (system + design-spec user + ack assistant = 3 preamble messages).
    _preamble_len = len(conversation)

    for i, page_content in enumerate(pages):
        page_num = i + 1
        if target_pages is not None and page_num not in target_pages:
            continue

        # Sliding window: trim old page exchanges, keeping only the most
        # recent ones to avoid unbounded context growth.  Each page
        # produces up to max_critic_attempts * 2 messages
        # (user prompt + assistant SVG per round).
        _max_context_msgs = MAX_PRIOR_PAGES_IN_CONTEXT * max_critic_attempts * 2
        _beyond_preamble = len(conversation) - _preamble_len
        if _beyond_preamble > _max_context_msgs:
            _trim = _beyond_preamble - _max_context_msgs
            conversation[:] = conversation[:_preamble_len] + conversation[_preamble_len + _trim:]

        page_name = _make_page_name(page_num, page_content)
        page_type = _classify_page_type(page_content)
        visible_page_content = strip_page_type_metadata(page_content)
        rewritten_content, used_figures, rejected_figures = _resolve_fig_tokens(
            visible_page_content,
            figure_inventory,
        )
        figure_source = "manuscript"
        if not used_figures:
            design_spec_figures = _figures_from_design_spec_for_page(
                design_spec,
                page_num,
                figure_inventory,
            )
            if design_spec_figures:
                figure_source = "design_spec"
                used_figures = design_spec_figures
                fallback_refs = "\n".join(
                    _paper_figure_reference_line(fig) for fig in design_spec_figures
                )
                rewritten_content = (
                    f"{rewritten_content}\n\n"
                    "Design spec image assignment recovered for this slide:\n"
                    f"{fallback_refs}"
                )
        figure_guidance = _figure_guidance_block(
            used_figures,
            rejected_figures,
            source=figure_source,
        )
        icon_assignment = _icon_from_design_spec_for_page(design_spec, page_num)
        required_icon = (
            str(icon_assignment["name"]) if icon_assignment is not None else None
        )
        icon_guidance = _icon_guidance_block(icon_assignment)
        char_budget = _char_budget_block(rewritten_content)
        img_layout = _figure_layout_guidance(used_figures)

        # Build skeleton injection block if template skeletons are available
        skeleton_block = ""
        if template_skeletons:
            skeleton_svg = template_skeletons.get(page_type)
            if skeleton_svg:
                skeleton_block = (
                    f"\n\n## Template Skeleton ({page_type} page)\n"
                    f"Use this SVG as your starting point. Replace {{{{PLACEHOLDER}}}} tokens "
                    f"with actual content below. Preserve ALL decorative elements, gradients, "
                    f"and structural chrome. Do NOT rewrite from scratch.\n\n"
                    f"```svg\n{skeleton_svg}\n```"
                )

        conversation.append(
            LLMMessage.user(
                f"## Page {page_num}/{len(pages)}: {page_name}\n\n"
                f"{rewritten_content}\n\n"
                f"## Runtime Reminders\n"
                f"- Style preset: {style}\n"
                f"- Language: {language}\n"
                f"- Detail level: {detail_level}\n"
                f"- Page type: {page_type}. Use this type only for template selection; do not render metadata comments.\n"
                f"- Keep all visible text in the requested language.\n"
                f"- {char_budget}\n\n"
                f"{figure_guidance}\n\n"
                f"{icon_guidance}\n\n"
                f"{img_layout}\n\n"
                f"Generate the complete SVG code for this page. "
                f"Output ONLY the SVG code, wrapped in ```svg code block."
                f"{skeleton_block}"
            )
        )
        try:
            debug_dir = project_dir / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            prompt_file = debug_dir / f"executor_page_{page_num:02d}_prompt.md"
            parts = [f"--- ROLE: {msg.role} ---\n\n{msg.content}" for msg in conversation]
            prompt_file.write_text("\n\n".join(parts), encoding="utf-8")
        except OSError:
            pass

        snapshot = set_usage_context(stage="generation", page=page_num, attempt=1)
        try:
            response: LLMResponse = await llm.chat(
                conversation, model, temperature=0.3, max_tokens=16384
            )
        finally:
            reset_usage_context(snapshot)

        svg_content = _extract_svg(response.content)
        for extraction_attempt in range(2, MAX_SVG_EXTRACTION_ATTEMPTS + 1):
            if svg_content:
                break
            conversation.append(LLMMessage.assistant(response.content))
            conversation.append(
                LLMMessage.user(
                    _build_svg_extraction_retry_prompt(
                        page_num=page_num,
                        total_pages=len(pages),
                        page_name=page_name,
                        page_content=page_content,
                        attempt=extraction_attempt,
                    )
                )
            )
            snapshot = set_usage_context(
                stage="generation", page=page_num, attempt=extraction_attempt
            )
            try:
                response = await llm.chat(
                    conversation, model, temperature=0.2, max_tokens=16384
                )
            finally:
                reset_usage_context(snapshot)
            svg_content = _extract_svg(response.content)

        if svg_content:
            conversation.append(LLMMessage.assistant(f"```svg\n{svg_content}\n```"))

            best_svg = svg_content
            visual_attempts = 0
            critic_attempt = 1
            while True:
                report = _merge_reports(
                    check_svg(svg_content, critic_config),
                    _validate_paper_figure_refs(
                        svg_content,
                        allowed_figures=used_figures,
                        used_paper_figures=used_paper_figures,
                    ),
                    _validate_icon_refs(svg_content, required_icon=required_icon),
                )
                # Archive pre-repair SVG on first violation detection
                first_archive: str | None = None
                if not report.passed:
                    try:
                        repair_archive_dir.mkdir(parents=True, exist_ok=True)
                        archive_filename = f"p{page_num:02d}_attempt{critic_attempt}.svg"
                        archive_path = repair_archive_dir / archive_filename
                        archive_path.write_text(svg_content, encoding="utf-8")
                        first_archive = f"svg_archive/repair/{archive_filename}"
                    except OSError:
                        pass

                # When the static critic is satisfied, run visual critic
                # passes if enabled. Visual issues become the next repair
                # prompt, bounded by the static critic budget.
                if report.passed:
                    if on_critic is not None:
                        await on_critic(
                            page_num,
                            critic_attempt,
                            report,
                            None,
                            first_archive,
                        )
                    if enable_visual_critic and visual_attempts < visual_qa_max_attempts:
                        visual_attempts += 1
                        snapshot = set_usage_context(
                            stage="visual_qa", page=page_num, attempt=visual_attempts
                        )
                        try:
                            visual_outcome = await visual_check(
                                svg_content,
                                llm=llm,
                                model=model,
                                page_num=page_num,
                                page_title=page_name,
                                style=style,
                                config=visual_critic_config,
                            )
                        finally:
                            reset_usage_context(snapshot)
                        if on_critic is not None:
                            await on_critic(
                                page_num,
                                critic_attempt,
                                visual_outcome.report,
                                None,
                                None,
                            )
                        if (
                            visual_outcome.rendered
                            and not visual_outcome.report.passed
                        ):
                            report = visual_outcome.report
                            # fall through to the repair prompt below
                        else:
                            best_svg = svg_content
                            break
                    else:
                        best_svg = svg_content
                        break

                if critic_attempt >= max_critic_attempts:
                    best_svg = svg_content
                    break

                # Archive pre-repair SVG for before/after comparison
                archive_rel: str | None = None
                try:
                    repair_archive_dir.mkdir(parents=True, exist_ok=True)
                    archive_filename = f"p{page_num:02d}_attempt{critic_attempt + 1}.svg"
                    archive_path = repair_archive_dir / archive_filename
                    archive_path.write_text(svg_content, encoding="utf-8")
                    archive_rel = f"svg_archive/repair/{archive_filename}"
                except OSError:
                    pass

                repair_prompt_text = (
                    report.to_prompt_block()
                    + "\n\nReturn the complete corrected SVG only, "
                    "wrapped in a ```svg code block."
                )
                if on_critic is not None:
                    await on_critic(
                        page_num,
                        critic_attempt,
                        report,
                        repair_prompt_text,
                        archive_rel or first_archive,
                    )
                conversation.append(LLMMessage.user(repair_prompt_text))
                repair_temp = max(0.1, 0.3 - 0.1 * critic_attempt)
                snapshot = set_usage_context(
                    stage="repair", page=page_num, attempt=critic_attempt + 1
                )
                try:
                    response = await llm.chat(
                        conversation, model, temperature=repair_temp, max_tokens=16384
                    )
                finally:
                    reset_usage_context(snapshot)

                repaired = _extract_svg(response.content)
                if repaired:
                    svg_content = repaired
                    best_svg = repaired
                    conversation.append(
                        LLMMessage.assistant(f"```svg\n{repaired}\n```")
                    )
                    if on_svg_update is not None:
                        await on_svg_update(page_num, repaired)
                else:
                    conversation.append(LLMMessage.assistant(response.content))
                    break
                critic_attempt += 1

            svg_path = svg_output_dir / f"{page_num:02d}_{page_name}.svg"
            svg_path.write_text(best_svg, encoding="utf-8")
            for href in IMAGE_HREF_RE.findall(best_svg):
                key = _paper_figure_key_from_href(href)
                if key is not None:
                    used_paper_figures.setdefault(key, page_num)
            yield page_num, best_svg
        else:
            conversation.append(LLMMessage.assistant(response.content))
            raise RuntimeError(
                f"Failed to generate parseable SVG for page {page_num}/{len(pages)} "
                f"({page_name}) after {MAX_SVG_EXTRACTION_ATTEMPTS} attempts"
            )


def _classify_page_type(page_content: str) -> str:
    """Classify a page from manuscript metadata only."""
    return extract_page_type(page_content)


def _make_page_name(num: int, content: str) -> str:
    """Generate a clean filename from page content."""
    match = re.match(r"^##?\s+(.+)$", content, re.MULTILINE)
    if match:
        name = match.group(1).strip()
        name = re.sub(r"[^\w\s-]", "", name)
        name = re.sub(r"\s+", "_", name)
        return name[:40].lower()
    return f"page_{num}"


def _extract_svg(text: str) -> str | None:
    """Extract SVG content from LLM response."""
    match = re.search(r"```(?:svg|xml)?\s*\n(.*?)\n```", text, re.DOTALL)
    if match:
        svg = match.group(1).strip()
        if svg.startswith("<svg"):
            return svg

    match = re.search(r"(<svg[^>]*>.*?</svg>)", text, re.DOTALL)
    if match:
        return match.group(1).strip()

    return None


def _build_svg_extraction_retry_prompt(
    *,
    page_num: int,
    total_pages: int,
    page_name: str,
    page_content: str,
    attempt: int,
) -> str:
    """Build structured feedback when the model output is not parseable SVG."""
    return (
        "## Generation Validation Report\n\n"
        f"The previous response for page {page_num}/{total_pages} ({page_name}) "
        "did not contain a parseable complete SVG document.\n\n"
        "## Failure\n"
        "- No complete `<svg ...>...</svg>` block could be extracted.\n"
        "- The current page has not been generated yet.\n\n"
        "## Regeneration Instructions\n"
        f"- Regenerate page {page_num}/{total_pages} only; do not move to another page.\n"
        "- Preserve the page content below; do not invent a different slide.\n"
        "- Return one complete SVG document, wrapped in a ```svg code block.\n"
        "- The SVG must start with `<svg` and end with `</svg>`.\n\n"
        f"## Page Content To Render\n\n{page_content}\n\n"
        f"## Retry Attempt\n\n{attempt}"
    )
