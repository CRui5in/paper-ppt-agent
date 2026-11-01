"""SVG Executor agent: generates SVG page code from design spec.

The executor runs a page-by-page generation loop. Each page is checked by
the static :mod:`backend.generator.svg_critic` before being accepted. If
the critic finds violations, a targeted repair prompt is fed back to the
LLM (bounded retries, with slightly lower temperature on each retry) so
that regeneration is *informed* rather than blind.
"""

from __future__ import annotations

import asyncio
import re
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from html import escape as html_escape
from inspect import Parameter, signature
from pathlib import Path

from PIL import Image

from backend.config import settings
from backend.generator.project_manager import get_svg_files, slide_index_from_svg_path
from backend.generator.svg_critic import CriticConfig, CriticReport, Violation, check_svg
from backend.generator.visual_critic import VisualCriticConfig, visual_check
from backend.llm import LLMMessage, LLMProvider, LLMResponse
from backend.orchestrator.deck_plan import (
    build_deck_plan,
    deck_generation_context_markdown,
    deck_plan_markdown,
    slide_plan_markdown,
)
from backend.orchestrator.manuscript import (
    extract_page_type,
    split_manuscript_pages,
    strip_page_type_metadata,
)
from backend.usage.tracker import reset_usage_context, set_usage_context

# `[[FIG:fig_007_p9_page]]` style tokens emitted by the research agent.
FIG_TOKEN_RE = re.compile(r"\[\[FIG:([A-Za-z0-9_\-]+)\]\]")
IMAGE_HREF_RE = re.compile(r"<image\b[^>]*\bhref=[\"']([^\"']+)[\"']", re.IGNORECASE)
IMAGE_TAG_RE = re.compile(r"<image\b(?P<attrs>[^>]*)/?>", re.IGNORECASE | re.DOTALL)
SVG_ATTR_RE = re.compile(r"([\w:\-]+)\s*=\s*([\"'])(.*?)\2", re.IGNORECASE | re.DOTALL)
DATA_ICON_RE = re.compile(
    r"<use\b(?=[^>]*\bdata-icon=([\"'])([^\"']+)\1)[^>]*(?:/>|>\s*</use>)",
    re.IGNORECASE | re.DOTALL,
)
PSEUDO_ICON_BADGE_TEXT = frozenset({"P", "Δ", "!", "G", "?", "i", "I", "✓", "×"})
STRUCTURAL_CONTENT_LABEL_RE = re.compile(
    r"(?i)(核心问题|本章看点|本章关注|本节关注|三个问题|主要结果|结果亮点|"
    r"模型规格|关键目标|贡献|讲者提示|presenter\s+note|chapter\s+highlights|key\s+points)"
)
STRUCTURAL_LIST_LINE_RE = re.compile(r"^\s*(?:[-*•]|\d+[\.)、])\s+\S+")
FIGURE_LABEL_RE = re.compile(
    r"\b(fig(?:ure)?|table)\s*\.?\s*(\d+)\b|([图表])\s*(\d+)",
    re.IGNORECASE,
)
TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}")
TOC_TEMPLATE_PLACEHOLDER_RE = re.compile(
    r"\{\{TOC_ITEM_\d+(?:_(?:TITLE|DESC))?\}\}"
)
NON_TOC_TEMPLATE_PLACEHOLDER_RE = re.compile(
    r"\{\{(?!TOC_ITEM_\d+(?:_(?:TITLE|DESC))?\}\})[A-Z0-9_]+\}\}"
)
TEXT_BLOCK_RE = re.compile(r"<text\b(?P<attrs>[^>]*)>(?P<body>.*?)</text>", re.IGNORECASE | re.DOTALL)
TEXT_X_ATTR_RE = re.compile(r'\bx=(["\'])(?P<value>[^"\']+)\1', re.IGNORECASE)
TEXT_ANCHOR_ATTR_RE = re.compile(r"\btext-anchor\s*=", re.IGNORECASE)
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
CLIP_RECT_RE = re.compile(
    r"<clipPath\b[^>]*\bid=([\"'])(?P<id>[^\"']+)\1[^>]*>.*?"
    r"<rect\b(?P<attrs>[^>]*)/?>.*?</clipPath>",
    re.IGNORECASE | re.DOTALL,
)

PROMPT_PATH = Path(__file__).parent / "prompts" / "executor.md"

# Default number of static critic checks per page, including the first
# post-generation check. Paragraph capacity is checked independently of review.
DEFAULT_MAX_CRITIC_ATTEMPTS = 0

# Maximum number of prior same-type SVGs used as continuity references.
MAX_PRIOR_PAGES_IN_CONTEXT = 2

# Initial response plus bounded same-page retries when no SVG can be extracted.
MAX_SVG_EXTRACTION_ATTEMPTS = 3
SVG_GENERATION_MAX_TOKENS = max(8192, int(settings.svg_generation_max_tokens or 16384))
SVG_GENERATION_MAX_TOKENS_CEILING = max(
    SVG_GENERATION_MAX_TOKENS,
    int(settings.svg_generation_max_tokens_ceiling or 32768),
)
SVG_REPAIR_MAX_TOKENS = max(4096, int(settings.svg_repair_max_tokens or 8192))
MAX_DESIGN_SECTION_CHARS = 1800
STRUCTURAL_PAGE_TYPES = frozenset({"cover", "chapter", "toc", "ending"})

CriticMetadata = dict[str, object]
CriticCallback = Callable[
    [int, int, CriticReport, str | None, str | None, CriticMetadata | None],
    Awaitable[None],
]
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


def _drop_paper_figure_tokens_for_structural_page(page_content: str) -> str:
    """Remove automatic paper-figure tokens from structural pages.

    This only blocks paper figures selected by the analysis pipeline. It does
    not ban generic images, user-applied image search assets, or template
    background images.
    """
    return FIG_TOKEN_RE.sub("", page_content)


def _page_role_guidance(page_type: str) -> str:
    descriptions = {
        "cover": "Compose the opening page from the supplied title, metadata, and unlabeled abstract ornament, with a focal point and typography suited to the deck's visual identity.",
        "toc": "Present the complete ordered chapter/list items as the table of contents.",
        "chapter": "Use the common chapter-divider identity with the planned chapter index, title, and subtitle.",
        "ending": "Compose the planned closing message in the deck's visual identity.",
    }
    return descriptions.get(page_type, "Develop the page's explanation and evidence into a readable visual composition.")


def _factual_rendering_guardrails(page_type: str) -> str:
    if page_type in STRUCTURAL_PAGE_TYPES:
        return ""
    return (
        "\n## Information Fidelity\n"
        "- Render the current page's information completely, with exact values, "
        "units, notation, and comparison conditions.\n"
        "- Encode quantitative comparisons with consistent scales and precise data "
        "labels. Keep retrieval and provenance metadata in speaker notes.\n"
    )


def _layout_generation_guardrails(page_type: str, has_paper_figure: bool) -> str:
    if page_type in STRUCTURAL_PAGE_TYPES:
        return (
            "\n## Page Composition\n"
            "- Compose the planned title, metadata, agenda, or closing message using "
            "the deck's structural-page visual identity.\n"
        )
    figure_note = (
        "- Fit the complete assigned figure at its natural aspect ratio and a readable "
        "visible size. Arrange direct annotations, captions, and explanation around it; "
        "choose placement from its role in the argument.\n"
        if has_paper_figure else
        "- Build the explanation from native SVG geometry and type where useful. "
        "A diagram, chart, comparison, or open text composition can carry the page.\n"
    )
    return (
        "\n## Page Composition\n"
        "- Choose a visual relationship and reading order for this material. Treat the "
        "planned layout family as a starting idea, with freedom inside explicit template "
        "boundaries. Use paths, connectors, spatial grouping, and direct labels when "
        "they make the relationship visible.\n"
        "- Match the deck's palette, typography, and recurring chrome. Size regions "
        "from their content, align related elements, and use whitespace deliberately.\n"
        f"{figure_note}"
        "- Put each continuous body paragraph with inline tspan emphasis in a "
        "`<g data-role=\"body\" data-textbox=\"x y w h\">` for measured wrapping. "
        "Diagram labels, headings, and data labels may use directly positioned text.\n"
        "- Check text capacity, label collisions, connector endpoints, and table column "
        "widths before returning the SVG. Keep essential content clear of the footer.\n"
    )


def _paper_figure_keys(figures: list[dict]) -> set[str]:
    keys: set[str] = set()
    for fig in figures:
        path = str(fig.get("path") or "")
        key = Path(path).stem if path else ""
        if key:
            keys.add(key)
    return keys


def _remove_paper_reference_lines(content: str, disallowed_keys: set[str]) -> str:
    if not disallowed_keys:
        return content
    kept: list[str] = []
    for line in content.splitlines():
        if "[PAPER FIGURE" in line and any(key in line for key in disallowed_keys):
            continue
        kept.append(line)
    return "\n".join(kept)


def _caption_signature(caption: str) -> str:
    return re.sub(r"\s+", " ", (caption or "").strip().lower())


def _figure_slide_score(fig: dict) -> float:
    """Rank same-caption figure/table candidates for on-slide readability."""
    quality = float(fig.get("quality_score") or 0.0)
    width = int(fig.get("natural_width") or 0)
    height = int(fig.get("natural_height") or 0)
    ratio = float(fig.get("aspect_ratio") or (width / height if width and height else 0.0))
    flags = set(fig.get("review_flags") or [])
    caption = str(fig.get("caption") or "")
    label = _extract_figure_label(caption)

    score = quality * 10.0
    if flags:
        score -= min(len(flags), 4) * 0.8
    if label and label[0] == "table":
        # Tables usually read better in a compact, wide crop on a 16:9 slide.
        score += min(ratio, 5.0) * 0.6
        if height:
            score -= min(height, 1200) / 1200.0
    elif ratio:
        score += 0.4 if 0.7 <= ratio <= 4.8 else -0.4
    if width < 240 or height < 120:
        score -= 2.0
    return score


def _optimize_figure_choices(
    used_figures: list[dict],
    figure_inventory: list[dict] | None,
) -> tuple[list[dict], list[str]]:
    """Swap selected figures for better same-caption candidates before prompting.

    PDF extraction can yield multiple crops for the same caption. The generation
    prompt should not force a large, awkward crop when a more slide-friendly
    sibling exists.
    """
    if not used_figures or not figure_inventory:
        return used_figures, []

    by_caption: dict[str, list[dict]] = {}
    for fig in figure_inventory:
        signature = _caption_signature(str(fig.get("caption") or ""))
        if signature:
            by_caption.setdefault(signature, []).append(fig)

    optimized: list[dict] = []
    notes: list[str] = []
    for fig in used_figures:
        signature = _caption_signature(str(fig.get("caption") or ""))
        candidates = [
            candidate
            for candidate in by_caption.get(signature, [])
            if int(candidate.get("page_number") or -1) == int(fig.get("page_number") or -2)
        ]
        if len(candidates) < 2:
            optimized.append(fig)
            continue
        best = max(candidates, key=_figure_slide_score)
        current_key = Path(str(fig.get("path") or "")).stem
        best_key = Path(str(best.get("path") or "")).stem
        if best_key and best_key != current_key:
            notes.append(
                f"{current_key}: replaced with slide-friendlier same-caption crop {best_key}"
            )
            optimized.append(best)
        else:
            optimized.append(fig)
    return optimized, notes


def _limit_figure_count_for_layout(
    used_figures: list[dict],
    *,
    page_type: str,
) -> tuple[list[dict], list[str]]:
    """Keep figure-heavy slides readable without special-casing page numbers."""
    if not used_figures:
        return used_figures, []
    if page_type in STRUCTURAL_PAGE_TYPES:
        return [], [f"Removed {len(used_figures)} paper figure assignment(s) from structural page"]
    if len(used_figures) <= 2:
        return used_figures, []
    kept = used_figures[:2]
    omitted = used_figures[2:]
    notes = [
        "Limited paper figures to the 2 most central assignments so the slide can "
        "preserve readable text and caption spacing."
    ]
    if omitted:
        notes.append(
            "Omitted figures on this slide: "
            + ", ".join(Path(str(fig.get("path") or "")).stem for fig in omitted if fig.get("path"))
        )
    return kept, notes


def _figures_from_page_references(
    page_content: str,
    figure_inventory: list[dict] | None,
) -> list[dict]:
    """Resolve current-page figure/table labels to real inventory images."""
    if not figure_inventory:
        return []

    labels: list[tuple[str, str]] = []
    seen_labels: set[tuple[str, str]] = set()
    for match in FIGURE_LABEL_RE.finditer(page_content):
        if match.group(1):
            kind_raw = match.group(1).lower()
            kind = "table" if kind_raw == "table" else "figure"
            label = (kind, match.group(2))
        else:
            kind = "figure" if match.group(3) == "图" else "table"
            label = (kind, match.group(4))
        if label not in seen_labels:
            seen_labels.add(label)
            labels.append(label)
    if not labels:
        return []

    by_label = _figure_label_inventory(figure_inventory)
    figures: list[dict] = []
    seen: set[str] = set()
    for label in labels:
        fig = by_label.get(label)
        if not fig:
            continue
        stem = Path(str(fig.get("path") or "")).stem
        if stem in seen:
            continue
        seen.add(stem)
        figures.append(fig)
    return figures


def _figures_from_design_spec_for_page(
    design_spec: str,
    page_num: int,
    figure_inventory: list[dict] | None,
) -> list[dict]:
    """Extract figure assignments from design_spec Section VIII for a given page.

    Parses the Image Resource List table and matches the Page column against
    page_num.  Returns matching entries from figure_inventory.
    """
    if not figure_inventory or not design_spec:
        return []

    # Extract Section VIII content
    section_match = re.search(
        r"(?is)#+\s*VIII\b.*?(?=^#+\s*[IVXLCDM]+\.|\Z)",
        design_spec,
    )
    if not section_match:
        return []
    section = section_match.group(0)

    # Build a set of filenames assigned to this page from the table rows.
    # Two known formats:
    #   Format A: | filename | ... | page_num | ... |          (numeric Page column)
    #   Format B: | filename | ... | Slide N: description |    (Placement Notes column)
    assigned_stems: set[str] = set()
    for row_match in re.finditer(r"(?m)^\s*\|\s*(\S+)\s*\|(.+)$", section):
        stem = row_match.group(1).strip()
        row_text = row_match.group(2)
        # Try Format A: look for a standalone page number in the last column(s)
        cells = [c.strip() for c in row_text.split("|")]
        found = False
        for cell in cells:
            # Check for "Slide N" or "page N" patterns (Format B)
            slide_refs = re.findall(r"[Ss]lide\s+(\d+)", cell)
            if slide_refs:
                if page_num in (int(s) for s in slide_refs):
                    assigned_stems.add(stem)
                    found = True
                    break
            # Check for bare page number (Format A)
            if re.fullmatch(r"\d+", cell):
                if int(cell) == page_num:
                    assigned_stems.add(stem)
                    found = True
                    break
            # Check for comma/space separated page numbers
            tokens = re.split(r"[,，\s]+", cell)
            nums = set()
            for t in tokens:
                t = t.strip()
                if t.isdigit():
                    nums.add(int(t))
            if page_num in nums:
                assigned_stems.add(stem)
                found = True
                break

    if not assigned_stems:
        return []

    # Match stems against figure_inventory
    inventory_by_stem: dict[str, dict] = {}
    for fig in figure_inventory:
        fig_stem = Path(str(fig.get("path") or "")).stem
        if fig_stem:
            inventory_by_stem[fig_stem] = fig

    figures: list[dict] = []
    seen: set[str] = set()
    for stem in assigned_stems:
        fig = inventory_by_stem.get(stem)
        if fig and stem not in seen:
            seen.add(stem)
            figures.append(fig)
    return figures


def _figure_label_inventory(
    figure_inventory: list[dict],
) -> dict[tuple[str, str], dict]:
    candidates: dict[tuple[str, str], list[dict]] = {}
    for fig in figure_inventory:
        label = _extract_figure_label(str(fig.get("caption") or ""))
        if label:
            candidates.setdefault(label, []).append(fig)
    return {
        label: sorted(figs, key=lambda fig: _figure_label_rank(fig, label), reverse=True)[0]
        for label, figs in candidates.items()
    }


def _figure_label_rank(fig: dict, label: tuple[str, str]) -> tuple[float, float, float, float]:
    kind, number = label
    caption = str(fig.get("caption") or "")
    official = _caption_starts_with_label(caption, kind, number)
    method = str(fig.get("extraction_method") or "")
    flags = {str(flag) for flag in (fig.get("review_flags") or [])}
    quality = _safe_float(fig.get("quality_score"), 0.0)
    area = _safe_float(fig.get("natural_width"), 0.0) * _safe_float(
        fig.get("natural_height"),
        0.0,
    )
    method_score = 1.0 if method in {"embedded_image", "vector_page"} else 0.0
    flag_score = 0.0 if flags.intersection({"low_graphic_coverage", "page_level_fallback"}) else 1.0
    return (official, method_score, flag_score, quality + min(area / 1_000_000.0, 1.0))


def _caption_starts_with_label(caption: str, kind: str, number: str) -> float:
    if kind == "table":
        pattern = rf"^\s*(?:table|表)\s*\.?\s*{re.escape(number)}\s*[:：|]"
    else:
        pattern = rf"^\s*(?:fig(?:ure)?|图)\s*\.?\s*{re.escape(number)}\s*[:：|]"
    return 1.0 if re.search(pattern, caption, re.IGNORECASE) else 0.0


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _icon_from_inventory_table(design_spec: str, page_num: int) -> dict | None:
    """Look up icon from Section VI inventory table."""
    vi_pattern = r"(?ims)^#+\s*VI\.?\s*Icon\s+Usage.*?(?=^#+\s*VII\.?\s|\Z)"
    vi_match = re.search(vi_pattern, design_spec)
    if not vi_match:
        return None
    vi_text = vi_match.group(0)

    for raw_line in vi_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or "---" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2 or not re.fullmatch(r"0*\d+", cells[0]):
            continue
        if int(cells[0]) != page_num:
            continue
        icon_name = cells[1].strip().strip("`")
        if not icon_name or icon_name.lower() in {"none", "null", "n/a"}:
            return None
        role = cells[2] if len(cells) > 2 else ""
        anchor = cells[3] if len(cells) > 3 else ""
        placement = cells[4] if len(cells) > 4 else ""
        size_text = cells[5] if len(cells) > 5 else ""
        note = "; ".join(
            part
            for part in (
                f"role: {role}" if role else "",
                f"anchor: {anchor}" if anchor else "",
                f"placement: {placement}" if placement else "",
                f"size: {size_text}" if size_text else "",
            )
            if part
        )
        return {
            "name": icon_name,
            "size": _icon_size_from_text(size_text, default=40),
            "color": _icon_color_from_text(size_text, default="#2563EB"),
            "note": note,
            "role": role,
            "anchor": anchor,
            "placement": placement,
            "reason": "",
        }

    # Match table rows like | 03 | `chunk/target` | ... |
    row_pattern = rf"\|\s*0*{page_num}\s*\|\s*`([^`]+)`\s*\|"
    row_match = re.search(row_pattern, vi_text)
    if not row_match:
        return None
    icon_name = row_match.group(1).strip()
    if not icon_name or icon_name.lower() in {"none", "null", "n/a"}:
        return None
    return {"name": icon_name, "size": 40, "color": "#2563EB", "note": ""}


def _icon_note_fields(note: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for match in re.finditer(
        r"(?is)\b(role|anchor|placement|size|reason|color)\s*:\s*"
        r"(.*?)(?=;\s*\b(?:role|anchor|placement|size|reason|color)\s*:|$)",
        note,
    ):
        fields[match.group(1).lower()] = match.group(2).strip(" ;.-")
    return fields


def _icon_size_from_text(text: str, *, default: int = 40) -> int:
    size_match = re.search(r"(\d{1,3})\s*[x×]\s*(\d{1,3})\s*px?", text, re.IGNORECASE)
    if size_match:
        return max(int(size_match.group(1)), int(size_match.group(2)))
    single_match = re.search(r"\b(\d{2,3})\s*px\b", text, re.IGNORECASE)
    if single_match:
        return int(single_match.group(1))
    return default


def _icon_color_from_text(text: str, *, default: str = "#2563EB") -> str:
    color_match = re.search(r"`(#[0-9A-Fa-f]{3,8})`|(#[0-9A-Fa-f]{3,8})", text)
    if color_match:
        return color_match.group(1) or color_match.group(2)
    return default


def _icon_from_design_spec_for_page(design_spec: str, page_num: int) -> dict | None:
    """Recover a slide-level icon assignment from the design spec.

    Looks in Section IX first (Icon: line), then Section VI inventory table.
    """
    return None
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
            fields = _icon_note_fields(note)
            size_text = fields.get("size", note)
            color_text = fields.get("color", note)

            return {
                "name": icon_name,
                "size": _icon_size_from_text(size_text, default=40),
                "color": _icon_color_from_text(color_text, default="#2563EB"),
                "note": note,
                "role": fields.get("role", ""),
                "anchor": fields.get("anchor", ""),
                "placement": fields.get("placement", ""),
                "reason": fields.get("reason", ""),
            }

    # Secondary: Section VI inventory table
    return _icon_from_inventory_table(design_spec, page_num)


def _icon_guidance_block(icon_assignment: dict | None) -> str:
    """Constrain icon rendering to explicit design-spec placeholders."""
    if not icon_assignment:
        return (
            "## Icon Guidance\n"
            "- No icon is assigned. Do not add icon placeholders, emoji, Unicode "
            "symbols, punctuation badges, or single-character glyphs as icons.\n"
            "- Use numbered step markers only for ordered steps; otherwise use "
            "plain labels, shapes, lines, or small mechanism diagrams."
        )

    name = str(icon_assignment["name"])
    size = int(icon_assignment.get("size") or 40)
    color = str(icon_assignment.get("color") or "#2563EB")
    role = str(icon_assignment.get("role") or "semantic layout anchor")
    anchor = str(icon_assignment.get("anchor") or "the nearest related content block")
    placement = str(
        icon_assignment.get("placement")
        or "reserve a small slot before laying out text and charts"
    )
    reason = str(icon_assignment.get("reason") or "").strip()
    return (
        "## Icon Guidance\n"
        f"- The design spec assigns this slide exactly one semantic icon: `{name}`.\n"
        f"- Icon role: {role}.\n"
        f"- Anchor it to: {anchor}.\n"
        f"- Natural placement: {placement}.\n"
        + (f"- Why it belongs: {reason}.\n" if reason else "")
        + "- Reserve the icon slot before placing body text, charts, or cards; then fit surrounding content around that slot.\n"
        "- Render it with a real icon placeholder so the finalizer can embed the "
        "icon library asset. Do not redraw it with inline `<path>`, `<polygon>`, "
        "or decorative geometry.\n"
        f"- Use this form with double quotes: "
        f"`<use data-icon=\"{name}\" x=\"...\" y=\"...\" width=\"{size}\" "
        f"height=\"{size}\" fill=\"{color}\"/>`.\n"
        "- Keep it visually integrated with that anchor. Do not float it in an unrelated corner, use it as wallpaper, or overlay it on charts/figures.\n"
        "- Keep it sparse and purposeful; use no other icon placeholders on this slide.\n"
        "- Do not add emoji, symbols, punctuation badges, or single-character glyphs as extra icons."
    )


def _figure_guidance_block(
    used: list[dict],
    rejected: list[str] | None = None,
    *,
    source: str = "manuscript",
) -> str:
    """Expose assigned assets with their actual captions and dimensions."""
    lines = ["## Paper Figure Assets"]
    if not used:
        lines.append("- Assigned paper figures: none.")
    for fig in used:
        path = str(fig.get("path") or "")
        caption = str(fig.get("caption") or "").strip().replace("\n", " ")
        width, height = int(fig.get("natural_width") or 0), int(fig.get("natural_height") or 0)
        if not (width and height):
            try:
                with Image.open(path) as image:
                    width, height = image.size
            except (OSError, ValueError):
                pass
        dimensions = f"; {width}x{height}px; ratio {width / height:.4f}" if width and height else ""
        lines.append(f'- Assigned href: "{path}"{dimensions}; caption: {caption}')
    if rejected:
        lines.append("- Unavailable references: " + "; ".join(rejected))
    return "\n".join(lines)


def _dynamic_text_layout_block(
    manuscript_page: str,
    page_design_block: str,
    *,
    has_paper_figure: bool,
) -> str:
    """Keep paragraph measurement local to the designer's chosen regions."""
    return (
        "## Paragraph Layout\n"
        "Size paragraph boxes with their neighboring visuals. Write continuous "
        "text with inline emphasis; font metrics and the declared box width "
        "determine wrapping. Reserve line-height, padding, and caption space."
    )


def _write_generation_response_debug(
    project_dir: Path,
    *,
    prefix: str,
    page_num: int,
    attempt: int,
    content: str,
    max_tokens: int | None = None,
    note: str | None = None,
) -> None:
    try:
        debug_dir = project_dir / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        response_file = debug_dir / f"{prefix}_page_{page_num:02d}_response_attempt_{attempt}.md"
        header = ""
        if max_tokens is not None or note:
            header = (
                f"<!-- max_tokens={max_tokens if max_tokens is not None else ''}; "
                f"note={note or ''} -->\n\n"
            )
        response_file.write_text(header + content, encoding="utf-8")
    except OSError:
        pass


def _is_max_token_limit_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    token_terms = (
        "max_tokens",
        "max_completion_tokens",
        "max output",
        "output token",
        "completion token",
        "maximum context",
        "context length",
        "context window",
        "tokens exceed",
        "too many tokens",
    )
    limit_terms = (
        "exceed",
        "exceeded",
        "greater than",
        "maximum",
        "too large",
        "invalid",
        "limit",
        "not support",
        "unsupported",
    )
    return any(term in text for term in token_terms) and any(term in text for term in limit_terms)


def _completion_near_limit(response: LLMResponse, requested_max_tokens: int) -> bool:
    usage = response.usage
    if usage is None or requested_max_tokens <= 0:
        return False
    return usage.completion_tokens >= int(requested_max_tokens * 0.96)


def _svg_response_looks_truncated(content: str) -> bool:
    text = content.strip()
    if "</svg>" in text:
        return False
    return "<svg" in text or text.endswith((">", "/>", "</g>", "</text>", "</rect>"))


def _next_higher_svg_max_tokens(current: int) -> int:
    if current >= SVG_GENERATION_MAX_TOKENS_CEILING:
        return current
    return min(SVG_GENERATION_MAX_TOKENS_CEILING, max(current + 2048, current * 2))


def _next_lower_svg_max_tokens(current: int) -> int:
    if current <= 4096:
        return current
    return max(4096, current // 2)


async def _chat_svg_generation(
    llm: LLMProvider,
    conversation: list[LLMMessage],
    model: str,
    *,
    temperature: float,
    max_tokens: int,
    project_dir: Path,
    debug_prefix: str,
    page_num: int,
    attempt: int,
) -> tuple[LLMResponse, int]:
    requested_max_tokens = max_tokens
    while True:
        snapshot = set_usage_context(
            stage="generation",
            page=page_num,
            attempt=attempt,
        )
        try:
            response = await llm.chat(
                conversation,
                model,
                temperature=temperature,
                max_tokens=requested_max_tokens,
            )
        except BaseException as exc:
            reset_usage_context(snapshot)
            if _is_max_token_limit_error(exc):
                lowered = _next_lower_svg_max_tokens(requested_max_tokens)
                _write_generation_response_debug(
                    project_dir,
                    prefix=debug_prefix,
                    page_num=page_num,
                    attempt=attempt,
                    content=str(exc),
                    max_tokens=requested_max_tokens,
                    note=f"max token limit rejected; retrying with {lowered}",
                )
                if lowered < requested_max_tokens:
                    requested_max_tokens = lowered
                    continue
            raise
        else:
            reset_usage_context(snapshot)
            _write_generation_response_debug(
                project_dir,
                prefix=debug_prefix,
                page_num=page_num,
                attempt=attempt,
                content=response.content,
                max_tokens=requested_max_tokens,
            )
            return response, requested_max_tokens


def _normalize_obvious_short_text_lines(svg: str, page_design_block: str) -> str:
    """Wrap overlong text without merging intentional line breaks."""
    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        return svg
    namespace = _namespace_uri(root.tag)
    if namespace:
        ET.register_namespace("", namespace)
    from backend.generator.svg_finalize.render_math import render_math_in_tree
    from backend.generator.svg_text_layout import reflow_declared_paragraphs, layout_text_groups, normalize_inline_scripts

    math_rendered = render_math_in_tree(root) + normalize_inline_scripts(root)
    paragraphs_reflowed = reflow_declared_paragraphs(root) + layout_text_groups(root)
    regions = _merged_text_regions(root, page_design_block)
    if not regions:
        return ET.tostring(root, encoding="unicode") if math_rendered or paragraphs_reflowed else svg

    changed = _wrap_overlong_text_elements(root, regions)

    if not changed and not math_rendered and not paragraphs_reflowed:
        return svg
    return ET.tostring(root, encoding="unicode")


def _merged_text_regions(root: ET.Element, page_design_block: str) -> list[dict[str, float]]:
    regions = _page_design_regions(page_design_block)
    regions.extend(_svg_declared_text_regions(root))
    regions.extend(_svg_rect_text_regions(root))
    return _dedupe_regions(regions)


def _dedupe_regions(regions: list[dict[str, float]]) -> list[dict[str, float]]:
    deduped: list[dict[str, float]] = []
    seen: set[tuple[int, int, int, int]] = set()
    for region in regions:
        key = (
            round(region["x"]),
            round(region["y"]),
            round(region["w"]),
            round(region["h"]),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(region)
    return deduped


def _svg_declared_text_regions(root: ET.Element) -> list[dict[str, float]]:
    regions: list[dict[str, float]] = []
    for element in root.iter():
        region = _parse_textbox_attr(element)
        if region is not None:
            regions.append(region)
    return regions


def _parse_textbox_attr(element: ET.Element) -> dict[str, float] | None:
    raw = (
        element.attrib.get("data-textbox")
        or element.attrib.get("data-box")
        or element.attrib.get("data-region")
        or ""
    )
    if not raw:
        return None
    named = {
        match.group(1).lower(): float(match.group(2))
        for match in re.finditer(r"\b(x|y|w|h|width|height)\s*[:=]\s*(-?\d+(?:\.\d+)?)", raw)
    }
    if named:
        w = named.get("w", named.get("width"))
        h = named.get("h", named.get("height"))
        x = named.get("x")
        y = named.get("y")
        if x is not None and y is not None and w is not None and h is not None:
            if w >= 80 and h >= 20:
                return {"x": x, "y": y, "w": w, "h": h}
            return None
    values = [float(match.group(0)) for match in NUMBER_RE.finditer(raw)]
    if len(values) >= 4 and values[2] >= 80 and values[3] >= 20:
        return {"x": values[0], "y": values[1], "w": values[2], "h": values[3]}
    return None


def _svg_rect_text_regions(root: ET.Element) -> list[dict[str, float]]:
    regions: list[dict[str, float]] = []
    for element in root.iter():
        if _local_name(element.tag) != "rect":
            continue
        x = _svg_float_attr(element, "x")
        y = _svg_float_attr(element, "y")
        w = _svg_float_attr(element, "width")
        h = _svg_float_attr(element, "height")
        if w < 160 or h < 36:
            continue
        if w > 1220 and h > 560:
            continue
        pad = min(18.0, max(8.0, min(w, h) * 0.08))
        regions.append({"x": x + pad, "y": y + pad, "w": w - pad * 2, "h": h - pad * 2})
    return regions


def _wrap_overlong_text_elements(root: ET.Element, regions: list[dict[str, float]]) -> bool:
    parent_map = {child: parent for parent in root.iter() for child in list(parent)}
    changed = False
    for element in list(root.iter()):
        if _local_name(element.tag) != "text":
            continue
        if element.attrib.get("text-anchor") or "transform" in element.attrib:
            continue
        if _has_positioned_text_children(element):
            continue
        text = _element_text(element)
        if len(text) < 4:
            continue
        parent = parent_map.get(element)
        if parent is None:
            continue
        x = _svg_float_attr(element, "x")
        y = _svg_float_attr(element, "y")
        region = _explicit_or_best_text_region(element, parent_map, regions)
        if region is None:
            continue
        available = region["x"] + region["w"] - x - 6
        if available < 80:
            continue
        font_size = _svg_font_size(element)
        if _estimate_text_width(text, font_size) <= available * 0.98:
            continue

        fragments = _styled_text_fragments(element)
        tokens = _styled_tokens(fragments, available, font_size)
        lines = _pack_styled_tokens(tokens, available, font_size)
        if len(lines) <= 1:
            continue

        line_height = _text_line_height(element, font_size)
        bottom = region["y"] + region["h"]
        if y + line_height * (len(lines) - 1) + font_size * 0.25 > bottom:
            fit_height = max(font_size, bottom - y)
            desired = line_height * (len(lines) - 1) + font_size
            if desired > 0:
                scale = max(0.82, min(1.0, fit_height / desired))
                if scale < 0.995:
                    font_size *= scale
                    element.attrib["font-size"] = _format_svg_number(font_size)
                    tokens = _styled_tokens(fragments, available, font_size)
                    lines = _pack_styled_tokens(tokens, available, font_size)
                    line_height = _text_line_height(element, font_size)

        if _replace_text_element_with_lines(parent, element, lines, line_height):
            changed = True
    return changed


def _has_positioned_text_children(element: ET.Element) -> bool:
    for child in element.iter():
        if child is element:
            continue
        if any(attr in child.attrib for attr in ("x", "y", "dx", "dy", "baseline-shift")):
            return True
    return False


def _explicit_or_best_text_region(
    element: ET.Element,
    parent_map: dict[ET.Element, ET.Element],
    regions: list[dict[str, float]],
) -> dict[str, float] | None:
    current: ET.Element | None = element
    while current is not None:
        explicit = _parse_textbox_attr(current)
        if explicit is not None:
            return explicit
        current = parent_map.get(current)
    return _best_region_for_text(
        _svg_float_attr(element, "x"),
        _svg_float_attr(element, "y"),
        regions,
    )


def _styled_text_fragments(element: ET.Element) -> list[tuple[str, dict[str, str]]]:
    from backend.generator.svg_text_layout import paragraph_fragments

    return paragraph_fragments(element)


def _styled_tokens(
    fragments: list[tuple[str, dict[str, str]]],
    available: float,
    font_size: float,
) -> list[tuple[str, dict[str, str]]]:
    tokens: list[tuple[str, dict[str, str]]] = []
    token_re = re.compile(r"\s+|[A-Za-z0-9_./×+\-~≈=<>%]+|[\u4e00-\u9fff]+|[^\s]")
    for text, attrs in fragments:
        for match in token_re.finditer(text):
            token = match.group(0)
            if not token:
                continue
            if not token.isspace() and _estimate_text_width(token, font_size) > available:
                tokens.extend((char, attrs) for char in token)
            else:
                tokens.append((token, attrs))
    return tokens


def _pack_styled_tokens(
    tokens: list[tuple[str, dict[str, str]]],
    available: float,
    font_size: float,
) -> list[list[tuple[str, dict[str, str]]]]:
    lines: list[list[tuple[str, dict[str, str]]]] = []
    current: list[tuple[str, dict[str, str]]] = []
    for token in tokens:
        text = token[0]
        if text.isspace() and not current:
            continue
        candidate = _trim_styled_tokens(current + [token])
        if current and _styled_tokens_width(candidate, font_size) > available:
            trimmed = _trim_styled_tokens(current)
            if trimmed:
                lines.append(trimmed)
            current = [] if text.isspace() else [token]
        else:
            current.append(token)
    trimmed = _trim_styled_tokens(current)
    if trimmed:
        lines.append(trimmed)
    return lines


def _trim_styled_tokens(
    tokens: list[tuple[str, dict[str, str]]],
) -> list[tuple[str, dict[str, str]]]:
    trimmed = list(tokens)
    while trimmed and trimmed[0][0].isspace():
        trimmed.pop(0)
    while trimmed and trimmed[-1][0].isspace():
        trimmed.pop()
    return trimmed


def _styled_tokens_width(tokens: list[tuple[str, dict[str, str]]], font_size: float) -> float:
    return _estimate_text_width("".join(text for text, _ in tokens), font_size)


def _text_line_height(element: ET.Element, font_size: float) -> float:
    value = element.attrib.get("data-line-height") or element.attrib.get("line-height")
    match = NUMBER_RE.search(str(value))
    if match:
        return max(font_size * 1.05, float(match.group(0)))
    return font_size * 1.36


def _replace_text_element_with_lines(
    parent: ET.Element,
    element: ET.Element,
    lines: list[list[tuple[str, dict[str, str]]]],
    line_height: float,
) -> bool:
    children = list(parent)
    try:
        index = children.index(element)
    except ValueError:
        return False
    base_y = _svg_float_attr(element, "y")
    base_attrs = dict(element.attrib)
    parent.remove(element)
    for line_index, line_tokens in enumerate(lines):
        attrs = dict(base_attrs)
        attrs["y"] = _format_svg_number(base_y + line_height * line_index)
        if len(lines) > 1:
            attrs.pop("id", None)
        line_element = ET.Element(element.tag, attrs)
        _populate_text_line(line_element, line_tokens)
        parent.insert(index + line_index, line_element)
    return True


def _populate_text_line(
    element: ET.Element,
    tokens: list[tuple[str, dict[str, str]]],
) -> None:
    last_child: ET.Element | None = None
    tspan_tag = _qualified_child_tag(element, "tspan")
    for text, attrs in tokens:
        if not text:
            continue
        if attrs:
            child = ET.SubElement(element, tspan_tag, attrs)
            child.text = text
            last_child = child
        elif last_child is None:
            element.text = (element.text or "") + text
        else:
            last_child.tail = (last_child.tail or "") + text


def _qualified_child_tag(parent: ET.Element, local_name: str) -> str:
    namespace = _namespace_uri(parent.tag)
    return f"{{{namespace}}}{local_name}" if namespace else local_name


def _page_design_regions(page_design_block: str) -> list[dict[str, float]]:
    regions: list[dict[str, float]] = []
    pattern = re.compile(
        r"(?i)\bx\s*=\s*(?P<x>-?\d+(?:\.\d+)?)\s+"
        r"y\s*=\s*(?P<y>-?\d+(?:\.\d+)?)\s+"
        r"w\s*=\s*(?P<w>\d+(?:\.\d+)?)\s+"
        r"h\s*=\s*(?P<h>\d+(?:\.\d+)?)"
    )
    for match in pattern.finditer(page_design_block):
        region = {key: float(match.group(key)) for key in ("x", "y", "w", "h")}
        if region["w"] >= 120 and region["h"] >= 40:
            regions.append(region)
    return regions


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _namespace_uri(tag: str) -> str | None:
    if tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", 1)[0]
    return None


def _svg_float_attr(element: ET.Element, name: str) -> float:
    value = element.attrib.get(name, "")
    match = NUMBER_RE.search(value)
    return float(match.group(0)) if match else 0.0


def _svg_font_size(element: ET.Element) -> float:
    value = element.attrib.get("font-size", "")
    match = NUMBER_RE.search(value)
    return float(match.group(0)) if match else 18.0


def _text_style_key(element: ET.Element) -> tuple[str, ...]:
    return (
        element.attrib.get("font-size", ""),
        element.attrib.get("font-family", ""),
        element.attrib.get("font-weight", ""),
        element.attrib.get("fill", ""),
        element.attrib.get("class", ""),
    )


def _can_merge_text_nodes(left: ET.Element, right: ET.Element) -> bool:
    if left.attrib.get("text-anchor") or right.attrib.get("text-anchor"):
        return False
    if "transform" in left.attrib or "transform" in right.attrib:
        return False
    if _text_style_key(left) != _text_style_key(right):
        return False
    x_delta = abs(_svg_float_attr(left, "x") - _svg_float_attr(right, "x"))
    if x_delta > 2:
        return False
    font_size = max(_svg_font_size(left), _svg_font_size(right), 1.0)
    y_delta = _svg_float_attr(right, "y") - _svg_float_attr(left, "y")
    return font_size * 0.85 <= y_delta <= font_size * 1.8


def _rewrap_text_run(
    parent: ET.Element,
    run: list[ET.Element],
    regions: list[dict[str, float]],
) -> bool:
    text_parts = [(node.text or "").strip() for node in run]
    if any(not part for part in text_parts):
        return False
    first = run[0]
    x = _svg_float_attr(first, "x")
    y = _svg_float_attr(first, "y")
    font_size = _svg_font_size(first)
    region = _best_region_for_text(x, y, regions)
    if region is None:
        return False
    available = region["x"] + region["w"] - x - 10
    if available < 160:
        return False
    widths = [_estimate_text_width(part, font_size) for part in text_parts]
    if max(widths) > available * 0.92:
        return False
    if sum(widths) / len(widths) > available * 0.68:
        return False
    merged = _join_text_lines(text_parts)
    rewrapped = _wrap_text_to_width(merged, available, font_size)
    if not rewrapped or len(rewrapped) > len(run):
        return False
    if len(rewrapped) == len(run) and rewrapped == text_parts:
        return False

    line_height = _svg_float_attr(run[1], "y") - _svg_float_attr(run[0], "y")
    for idx, node in enumerate(run):
        if idx < len(rewrapped):
            node.text = rewrapped[idx]
            node.attrib["y"] = _format_svg_number(y + line_height * idx)
        else:
            try:
                parent.remove(node)
            except ValueError:
                pass
    return True


def _rewrap_inline_emphasis_runs(root: ET.Element, regions: list[dict[str, float]]) -> bool:
    """Move simple inline text segments onto better lines without flattening style."""
    parent_map = {child: parent for parent in root.iter() for child in list(parent)}
    by_parent: dict[ET.Element, list[ET.Element]] = {}
    for element in root.iter():
        if _local_name(element.tag) != "text":
            continue
        if not _is_reflowable_inline_text(element):
            continue
        parent = parent_map.get(element)
        if parent is None:
            continue
        by_parent.setdefault(parent, []).append(element)

    changed = False
    for parent, elements in by_parent.items():
        lines = _visual_text_lines(elements)
        for paragraph in _paragraph_lines(lines, regions):
            if _rewrap_inline_paragraph(paragraph, regions):
                changed = True
    return changed


def _is_reflowable_inline_text(element: ET.Element) -> bool:
    if element.attrib.get("text-anchor") or "transform" in element.attrib:
        return False
    text = _element_text(element)
    if not text or "\n" in text or len(text) > 180:
        return False
    for child in element.iter():
        if child is element:
            continue
        if any(attr in child.attrib for attr in ("x", "y", "dx", "dy", "baseline-shift")):
            return False
    return True


def _visual_text_lines(elements: list[ET.Element]) -> list[list[ET.Element]]:
    ordered = sorted(elements, key=lambda node: (_svg_float_attr(node, "y"), _svg_float_attr(node, "x")))
    lines: list[list[ET.Element]] = []
    for element in ordered:
        y = _svg_float_attr(element, "y")
        font_size = _svg_font_size(element)
        placed = False
        for line in lines:
            base_y = _svg_float_attr(line[0], "y")
            if abs(y - base_y) <= max(4, font_size * 0.35):
                line.append(element)
                placed = True
                break
        if not placed:
            lines.append([element])
    return [sorted(line, key=lambda node: _svg_float_attr(node, "x")) for line in lines]


def _paragraph_lines(
    lines: list[list[ET.Element]],
    regions: list[dict[str, float]],
) -> list[list[list[ET.Element]]]:
    paragraphs: list[list[list[ET.Element]]] = []
    current: list[list[ET.Element]] = []
    previous_y: float | None = None
    previous_font = 0.0
    previous_region: dict[str, float] | None = None
    previous_was_label = False
    for line in lines:
        text = _line_text(line)
        x = _svg_float_attr(line[0], "x")
        y = _svg_float_attr(line[0], "y")
        font = _svg_font_size(line[0])
        region = _best_region_for_text(x, y, regions)
        is_label = _line_is_standalone_label(line)
        starts_new_bullet = text.startswith(("•", "-", "·")) and bool(current)
        region_changed = previous_region is not None and region is not previous_region
        y_gap = (
            previous_y is not None
            and y - previous_y > max(previous_font, font, 1.0) * 2.0
        )
        font_changed = previous_font and abs(previous_font - font) > 2
        if current and (
            starts_new_bullet
            or region_changed
            or y_gap
            or font_changed
            or previous_was_label
            or is_label
        ):
            paragraphs.append(current)
            current = []
        current.append(line)
        previous_y = y
        previous_font = font
        previous_region = region
        previous_was_label = is_label
    if current:
        paragraphs.append(current)
    return paragraphs


def _rewrap_inline_paragraph(
    paragraph: list[list[ET.Element]],
    regions: list[dict[str, float]],
) -> bool:
    if not paragraph:
        return False
    segments = [node for line in paragraph for node in line]
    if len(segments) < 2:
        return False
    first = segments[0]
    start_x = _svg_float_attr(first, "x")
    start_y = _svg_float_attr(first, "y")
    font_size = _svg_font_size(first)
    region = _best_region_for_text(start_x, start_y, regions)
    if region is None:
        return False
    right = region["x"] + region["w"] - 10
    available = right - start_x
    if available < 160:
        return False

    line_widths = [_line_visual_width(line, font_size) for line in paragraph]
    if len(paragraph) == 1 and max(line_widths, default=0) > available * 0.72:
        return False
    if len(paragraph) > 1 and not _paragraph_has_wasteful_break(paragraph, right, font_size):
        return False

    line_height = _paragraph_line_height(paragraph, font_size)
    continuation_x = _continuation_x(paragraph, start_x)
    current_x = start_x
    current_y = start_y
    previous_text = ""
    placements: list[tuple[ET.Element, float, float]] = []
    for index, node in enumerate(segments):
        text = _element_text(node)
        width = _estimate_text_width(text, font_size)
        gap = 0.0 if index == 0 else _inline_segment_gap(previous_text, text, font_size)
        candidate_x = current_x + gap
        line_start_x = start_x if not _is_continuation_context(placements, paragraph) else continuation_x
        if candidate_x > line_start_x and candidate_x + width > right:
            current_y += line_height
            current_x = continuation_x
            candidate_x = current_x
        if current_y > start_y + line_height * (len(paragraph) + 1):
            return False
        placements.append((node, candidate_x, current_y))
        current_x = candidate_x + width
        previous_text = text

    if len({round(y, 2) for _, _, y in placements}) > len(paragraph):
        return False

    changed = False
    for node, x, y in placements:
        old_x = _svg_float_attr(node, "x")
        old_y = _svg_float_attr(node, "y")
        if abs(old_x - x) > 1 or abs(old_y - y) > 1:
            node.attrib["x"] = _format_svg_number(x)
            node.attrib["y"] = _format_svg_number(y)
            changed = True
    return changed


def _paragraph_has_wasteful_break(
    paragraph: list[list[ET.Element]],
    right: float,
    font_size: float,
) -> bool:
    for line_index in range(len(paragraph) - 1):
        line = paragraph[line_index]
        next_line = paragraph[line_index + 1]
        if not line or not next_line:
            continue
        line_right = _line_right_edge(line, font_size)
        first_next = next_line[0]
        first_next_width = _estimate_text_width(_element_text(first_next), font_size)
        if line_right + first_next_width <= right - 4:
            return True
    return False


def _paragraph_line_height(paragraph: list[list[ET.Element]], font_size: float) -> float:
    ys = sorted({_svg_float_attr(line[0], "y") for line in paragraph if line})
    deltas = [ys[idx + 1] - ys[idx] for idx in range(len(ys) - 1) if ys[idx + 1] > ys[idx]]
    if deltas:
        return min(max(deltas[0], font_size * 1.1), font_size * 1.7)
    return font_size * 1.45


def _continuation_x(paragraph: list[list[ET.Element]], start_x: float) -> float:
    first_text = _line_text(paragraph[0]) if paragraph else ""
    if first_text.startswith(("•", "-", "·")):
        return start_x + 16
    return start_x


def _is_continuation_context(
    placements: list[tuple[ET.Element, float, float]],
    paragraph: list[list[ET.Element]],
) -> bool:
    if not placements or not paragraph:
        return False
    first_text = _line_text(paragraph[0])
    if not first_text.startswith(("•", "-", "·")):
        return False
    first_y = placements[0][2]
    return placements[-1][2] > first_y


def _line_text(line: list[ET.Element]) -> str:
    return "".join(_element_text(node) for node in line).strip()


def _line_is_standalone_label(line: list[ET.Element]) -> bool:
    if not line:
        return False
    text = _line_text(line)
    if not text or text.startswith(("•", "-", "·")):
        return False
    if len(text) > 24:
        return False
    if any(_looks_like_emphasis_color(node.attrib.get("fill", "")) for node in line):
        return False
    bold_nodes = [
        node
        for node in line
        if "bold" in str(node.attrib.get("font-weight", "")).lower()
        or str(node.attrib.get("font-weight", "")).isdigit()
        and int(str(node.attrib.get("font-weight", "0"))) >= 600
    ]
    return len(bold_nodes) == len(line)


def _looks_like_emphasis_color(fill: str) -> bool:
    value = fill.strip().lower()
    if not value.startswith("#"):
        return False
    neutral_or_heading = {
        "#111",
        "#111111",
        "#222",
        "#222222",
        "#2d3436",
        "#2d3748",
        "#333",
        "#333333",
        "#1a365d",
        "#1a3c6d",
        "#1a5ad7",
    }
    if value in neutral_or_heading:
        return False
    if len(value) == 4:
        value = "#" + "".join(ch * 2 for ch in value[1:])
    if len(value) != 7:
        return False
    try:
        r = int(value[1:3], 16)
        g = int(value[3:5], 16)
        b = int(value[5:7], 16)
    except ValueError:
        return False
    return max(r, g, b) - min(r, g, b) >= 45


def _element_text(element: ET.Element) -> str:
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def _line_visual_width(line: list[ET.Element], font_size: float) -> float:
    if not line:
        return 0.0
    left = min(_svg_float_attr(node, "x") for node in line)
    return _line_right_edge(line, font_size) - left


def _line_right_edge(line: list[ET.Element], font_size: float) -> float:
    return max(
        _svg_float_attr(node, "x") + _estimate_text_width(_element_text(node), font_size)
        for node in line
    )


def _inline_segment_gap(previous: str, current: str, font_size: float) -> float:
    if not previous or not current:
        return 0.0
    prev = previous[-1]
    curr = current[0]
    if prev.isspace() or curr.isspace():
        return 0.0
    if ("\u4e00" <= prev <= "\u9fff") or ("\u4e00" <= curr <= "\u9fff"):
        return 0.0
    if curr in "，。；：、,.!?;:)]）】":
        return 0.0
    return font_size * 0.3


def _best_region_for_text(
    x: float,
    y: float,
    regions: list[dict[str, float]],
) -> dict[str, float] | None:
    candidates = [
        region
        for region in regions
        if region["x"] - 4 <= x <= region["x"] + region["w"]
        and region["y"] - 36 <= y <= region["y"] + region["h"] + 36
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda region: abs(x - region["x"]) + abs(y - region["y"]))


def _estimate_text_width(text: str, font_size: float) -> float:
    width = 0.0
    for char in text:
        code = ord(char)
        if char.isspace():
            width += font_size * 0.35
        elif 0x4E00 <= code <= 0x9FFF or 0x3040 <= code <= 0x30FF or 0xAC00 <= code <= 0xD7AF:
            width += font_size
        elif char in "，。；：、,.!?;:()（）[]【】":
            width += font_size * 0.55
        else:
            width += font_size * 0.58
    return width


def _join_text_lines(lines: list[str]) -> str:
    text = "".join(
        part if idx == 0 or _starts_with_cjk_or_punct(part) else f" {part}"
        for idx, part in enumerate(lines)
    )
    return re.sub(r"\s+", " ", text).strip()


def _starts_with_cjk_or_punct(text: str) -> bool:
    if not text:
        return False
    char = text[0]
    return "\u4e00" <= char <= "\u9fff" or char in "，。；：、,.!?;:)]）】"


def _wrap_text_to_width(text: str, width: float, font_size: float) -> list[str]:
    chunks = re.findall(r"[A-Za-z0-9_./×+\-]+|[\u4e00-\u9fff]+|[^\s]", text)
    lines: list[str] = []
    current = ""
    for chunk in chunks:
        pieces = [chunk]
        if _estimate_text_width(chunk, font_size) > width:
            pieces = list(chunk)
        for piece in pieces:
            candidate = _append_text_chunk(current, piece)
            if current and _estimate_text_width(candidate, font_size) > width:
                lines.append(current)
                current = piece
            else:
                current = candidate
    if current:
        lines.append(current)
    return lines


def _append_text_chunk(current: str, chunk: str) -> str:
    if not current:
        return chunk
    if _starts_with_cjk_or_punct(chunk):
        return current + chunk
    if "\u4e00" <= current[-1] <= "\u9fff":
        return current + chunk
    return current + " " + chunk


def _format_svg_number(value: float) -> str:
    rounded = round(value, 2)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def _figure_layout_guidance(used_figures: list[dict]) -> str:
    """Supply image geometry; the designer decides composition."""
    if not used_figures:
        return ""
    lines = ["## Assigned Image Geometry"]
    for fig in used_figures:
        path = Path(str(fig.get("path") or ""))
        try:
            with Image.open(path) as image:
                width, height = image.size
            lines.append(f"- {path.stem}: {width}x{height}px; aspect ratio {width / height:.4f}.")
        except (OSError, ValueError):
            lines.append(f"- {path.stem}: dimensions unavailable.")
    lines.append("Fit complete figures at readable sizes within the page's assigned regions.")
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


def _svg_attrs(attr_text: str) -> dict[str, str]:
    return {match.group(1): match.group(3) for match in SVG_ATTR_RE.finditer(attr_text or "")}


def _svg_number(value: str | None) -> float | None:
    if value is None:
        return None
    match = NUMBER_RE.search(str(value))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _clip_rects(svg_content: str) -> dict[str, tuple[float, float, float, float]]:
    rects: dict[str, tuple[float, float, float, float]] = {}
    for match in CLIP_RECT_RE.finditer(svg_content):
        attrs = _svg_attrs(match.group("attrs"))
        x = _svg_number(attrs.get("x")) or 0.0
        y = _svg_number(attrs.get("y")) or 0.0
        width = _svg_number(attrs.get("width"))
        height = _svg_number(attrs.get("height"))
        if width is not None and height is not None:
            rects[match.group("id")] = (x, y, width, height)
    return rects


def _clip_id(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"url\(#([^)]+)\)", value)
    return match.group(1) if match else None


def _intersection_area(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x_overlap = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    y_overlap = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    return x_overlap * y_overlap


def _validate_paper_figure_geometry(
    svg_content: str,
    *,
    allowed_figures: list[dict],
) -> CriticReport:
    allowed_by_key = {
        Path(str(fig.get("path") or "")).stem: fig
        for fig in allowed_figures
        if fig.get("path")
    }
    clip_rects = _clip_rects(svg_content)
    violations: list[Violation] = []

    for tag_match in IMAGE_TAG_RE.finditer(svg_content):
        attrs = _svg_attrs(tag_match.group("attrs"))
        href = attrs.get("href") or attrs.get("xlink:href") or ""
        key = _paper_figure_key_from_href(href)
        if key is None or key not in allowed_by_key:
            continue

        x = _svg_number(attrs.get("x"))
        y = _svg_number(attrs.get("y"))
        width = _svg_number(attrs.get("width"))
        height = _svg_number(attrs.get("height"))
        if None in (x, y, width, height):
            violations.append(
                Violation(
                    rule="paper_figure_missing_geometry",
                    severity="error",
                    detail=(
                        f'Paper figure "{key}" must include numeric x, y, width, '
                        "and height so layout can be verified."
                    ),
                )
            )
            continue

        assert x is not None and y is not None and width is not None and height is not None
        fig = allowed_by_key[key]
        natural_w = float(fig.get("natural_width") or 0)
        natural_h = float(fig.get("natural_height") or 0)
        if natural_w > 0 and natural_h > 0 and height > 0:
            expected_ratio = natural_w / natural_h
            actual_ratio = width / height
            if abs(actual_ratio / expected_ratio - 1.0) > 0.06:
                violations.append(
                    Violation(
                        rule="paper_figure_aspect_ratio_changed",
                        severity="error",
                        detail=(
                            f'Paper figure "{key}" rendered ratio {actual_ratio:.2f} '
                            f"does not match source ratio {expected_ratio:.2f}. "
                            "Preserve the source aspect ratio."
                        ),
                    )
                )

        if width <= 0 or height <= 0:
            violations.append(
                Violation(
                    rule="paper_figure_invalid_size",
                    severity="error",
                    detail=f'Paper figure "{key}" has non-positive rendered size.',
                )
            )
            continue

        right = x + width
        bottom = y + height
        if x < -1 or y < 60 or right > 1260 or bottom > 640 or width > 1220 or height > 540:
            violations.append(
                Violation(
                    rule="paper_figure_out_of_bounds",
                    severity="error",
                    detail=(
                        f'Paper figure "{key}" box is x={x:.0f}, y={y:.0f}, '
                        f"w={width:.0f}, h={height:.0f}, extending to "
                        f"x={right:.0f}, y={bottom:.0f}. Keep the full visible "
                        "paper figure within the slide canvas/content band; do "
                        "not hide source-image overflow off-canvas."
                    ),
                )
            )

        clip = attrs.get("clip-path")
        clip_key = _clip_id(clip)
        if clip_key and clip_key in clip_rects:
            image_box = (x, y, width, height)
            clip_box = clip_rects[clip_key]
            visible = _intersection_area(image_box, clip_box)
            image_area = width * height
            visible_fraction = visible / image_area if image_area else 0.0
            if visible_fraction < 0.72:
                violations.append(
                    Violation(
                        rule="paper_figure_overcropped",
                        severity="error",
                        detail=(
                            f'Paper figure "{key}" is clipped to only '
                            f"{visible_fraction:.0%} of its rendered image box. "
                            "Resize the image to the visible frame or redraw the "
                            "desired subpanel with native SVG."
                        ),
                    )
                )

    return CriticReport(passed=not violations, violations=violations)


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


def _generation_static_report(
    svg_content: str,
    *,
    critic_config: CriticConfig,
    allowed_figures: list[dict],
    used_paper_figures: dict[str, int],
    required_icon: str | None,
    include_general_svg_checks: bool,
    page_content: str = "",
) -> CriticReport:
    source_report = _validate_source_table_values(svg_content, page_content)
    if not include_general_svg_checks:
        report = check_svg(svg_content, critic_config)
        violations = [v for v in report.violations if v.rule in {
            "paragraph_capacity", "out_of_bounds", "text_overflow_in_container",
            "filter_requires_raster_fallback", "forbidden_element", "xml_parse",
            "text_line_overlap", "text_overlap", "shape_covers_text",
        }]
        return _merge_reports(CriticReport(passed=not violations, violations=violations), source_report)
    reports = [
        source_report,
        _validate_paper_figure_refs(
            svg_content,
            allowed_figures=allowed_figures,
            used_paper_figures=used_paper_figures,
        ),
        _validate_paper_figure_geometry(
            svg_content,
            allowed_figures=allowed_figures,
        ),
        _validate_icon_refs(svg_content, required_icon=required_icon),
    ]
    if include_general_svg_checks:
        reports.insert(0, check_svg(svg_content, critic_config))
    return _merge_reports(*reports)


def _validate_source_table_values(svg_content: str, page_content: str) -> CriticReport:
    """Keep numeric records selected into the manuscript's tables visible."""
    from decimal import Decimal
    import unicodedata

    def values(text: str) -> dict:
        text = unicodedata.normalize('NFKC', text).replace('−', '-')
        result = {}
        pattern = r'(?<![A-Za-z0-9_.])[-+]?\d+(?:[, \u2009\u202f]\d{3})*(?:\.\d+)?(?:[eE][-+]?\d+)?(?![A-Za-z0-9_.])'
        for match in re.finditer(pattern, text):
            raw = match.group()
            result[Decimal(re.sub(r'[,\s]', '', raw))] = raw
        return result

    rows = []
    in_table = False
    for line in page_content.splitlines():
        if re.fullmatch(r'\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*', line):
            in_table = True
        elif in_table and '|' in line:
            rows.append(line)
        else:
            in_table = False
    expected = values('\n'.join(rows))
    if not expected:
        return CriticReport(passed=True)
    try:
        root = ET.fromstring(svg_content)
    except ET.ParseError:
        return CriticReport(passed=True)
    visible = []
    for element in root.iter():
        if _local_tag(element.tag) == 'text':
            visible.append(''.join(element.itertext()))
        if element.get('data-latex'):
            visible.append(element.get('data-latex'))
    present = values('\n'.join(visible))
    missing = [raw for value, raw in expected.items() if value not in present]
    if not missing:
        return CriticReport(passed=True)
    return CriticReport(passed=False, violations=[Violation(
        rule='source_table_values_missing', severity='error',
        detail='Selected source-table values are missing from the visible slide: ' + ', '.join(missing) +
               '. Recompose the table or chart to retain the complete selected records and their labels.',
    )])


def _static_attempt_limit_for_report(
    max_critic_attempts: int,
    report: CriticReport | None,
) -> int:
    # Basic geometry gets one repair pass; remaining findings are reported
    # on the page without aborting generation of the rest of the deck.
    return max(1, int(max_critic_attempts))


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


def _archive_repair_svg(
    repair_archive_dir: Path,
    *,
    page_num: int,
    attempt: int,
    label: str,
    svg_content: str,
) -> str | None:
    """Persist an SVG snapshot for review before/after repair."""
    try:
        repair_archive_dir.mkdir(parents=True, exist_ok=True)
        archive_filename = f"p{page_num:02d}_attempt{attempt}_{label}.svg"
        archive_path = repair_archive_dir / archive_filename
        archive_path.write_text(svg_content, encoding="utf-8")
        return f"svg_archive/repair/{archive_filename}"
    except OSError:
        return None


def _archive_visual_render(
    repair_archive_dir: Path,
    *,
    page_num: int,
    attempt: int,
    media_type: str | None,
    image_bytes: bytes | None,
) -> str | None:
    if not image_bytes:
        return None
    ext = ".jpg" if media_type == "image/jpeg" else ".png"
    try:
        repair_archive_dir.mkdir(parents=True, exist_ok=True)
        archive_filename = f"p{page_num:02d}_visual_attempt{attempt}{ext}"
        archive_path = repair_archive_dir / archive_filename
        archive_path.write_bytes(image_bytes)
        return f"svg_archive/repair/{archive_filename}"
    except OSError:
        return None


def _visual_outcome_metadata(
    visual_outcome,
    repair_archive_dir: Path | None = None,
    *,
    page_num: int | None = None,
    attempt: int | None = None,
) -> CriticMetadata:
    metadata: CriticMetadata = {
        "source": "visual",
        "rendered": bool(getattr(visual_outcome, "rendered", False)),
    }
    if repair_archive_dir is not None and page_num is not None and attempt is not None:
        rendered_image_path = _archive_visual_render(
            repair_archive_dir,
            page_num=page_num,
            attempt=attempt,
            media_type=getattr(visual_outcome, "media_type", None),
            image_bytes=getattr(visual_outcome, "rendered_image_bytes", None),
        )
        if rendered_image_path:
            metadata["rendered_image_path"] = rendered_image_path
    for attr in ("media_type", "skipped_reason", "raw_response_excerpt"):
        value = getattr(visual_outcome, attr, None)
        if value:
            metadata[attr] = value
    return metadata


async def _emit_critic(
    on_critic: CriticCallback | None,
    page_num: int,
    attempt: int,
    report: CriticReport,
    repair_prompt: str | None,
    archive_path: str | None,
    metadata: CriticMetadata | None,
) -> None:
    if on_critic is None:
        return
    try:
        params = signature(on_critic).parameters.values()
        supports_varargs = any(param.kind == Parameter.VAR_POSITIONAL for param in params)
        positional_params = [
            param
            for param in params
            if param.kind in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
        ]
    except (TypeError, ValueError):
        supports_varargs = True
        positional_params = []

    if supports_varargs or len(positional_params) >= 6:
        await on_critic(page_num, attempt, report, repair_prompt, archive_path, metadata)
    else:
        await on_critic(page_num, attempt, report, repair_prompt, archive_path)  # type: ignore[misc]


def _load_existing_svg_history(
    svg_output_dir: Path,
    pages: list[str],
) -> dict[int, tuple[str, str, str]]:
    history: dict[int, tuple[str, str, str]] = {}
    project_dir = svg_output_dir.parent
    for fallback_index, svg_path in enumerate(get_svg_files(project_dir, "output"), start=1):
        page_num = slide_index_from_svg_path(svg_path, fallback_index)
        if page_num is None or page_num < 1 or page_num > len(pages) or page_num in history:
            continue
        try:
            svg_content = svg_path.read_text(encoding="utf-8")
        except OSError:
            continue
        page = pages[page_num - 1]
        history[page_num] = (
            _classify_page_type(page),
            _make_page_name(page_num, page),
            svg_content,
        )
    return history


def _write_generated_svg(svg_output_dir: Path, page_num: int, svg_content: str) -> Path:
    """Atomically replace every filename variant representing one slide."""
    target = svg_output_dir / f"slide_{page_num:03d}.svg"
    temporary = svg_output_dir / f".__slide_{page_num:03d}_{uuid.uuid4().hex}.svg"
    temporary.write_text(svg_content, encoding="utf-8")
    try:
        for existing in svg_output_dir.glob("*.svg"):
            if existing == temporary:
                continue
            if slide_index_from_svg_path(existing) == page_num:
                existing.unlink(missing_ok=True)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _same_type_svg_history_message(
    history: dict[int, tuple[str, str, str]],
    *,
    current_page_num: int,
    current_page_type: str,
) -> str:
    if current_page_type not in {"chapter", "content"}:
        return ""
    references = [
        (page_num, title, svg)
        for page_num, (page_type, title, svg) in sorted(history.items())
        if page_num < current_page_num and page_type == current_page_type
    ][-MAX_PRIOR_PAGES_IN_CONTEXT:]
    if not references:
        return ""

    label = "Chapter" if current_page_type == "chapter" else "Content"
    parts = [
        f"## Prior {label} Page SVG References",
        "",
        f"Use these prior {current_page_type} pages only for typography, spacing, "
        "color, shape language, and layout continuity. The current manuscript and "
        "Page Design Contract remain authoritative. Do not copy old facts or text.",
    ]
    for page_num, title, svg in references:
        parts.extend(
            [
                "",
                f"### Slide {page_num:02d}: {title}",
                "```svg",
                svg,
                "```",
            ]
        )
    return "\n".join(parts)


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
    slide_contexts: dict[int, str] | None = None,
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
    template_vars = _template_vars_by_page(pages)
    deck_plan = build_deck_plan(
        manuscript,
        design_spec,
        detail_level=detail_level,
    )
    deck_plan_block = deck_plan_markdown(deck_plan)
    generation_context_block = deck_generation_context_markdown(deck_plan)
    compact_design_spec = _parallel_global_design_context(design_spec)
    try:
        (project_dir / "deck_plan.md").write_text(deck_plan_block, encoding="utf-8")
    except OSError:
        pass
    svg_output_dir = project_dir / "svg_output"
    svg_output_dir.mkdir(parents=True, exist_ok=True)
    repair_archive_dir = project_dir / "svg_archive" / "repair"
    used_paper_figures: dict[str, int] = {}
    if max_critic_attempts is None:
        max_critic_attempts = DEFAULT_MAX_CRITIC_ATTEMPTS
    max_critic_attempts = max(0, int(max_critic_attempts))
    visual_qa_max_attempts = max(0, int(visual_qa_max_attempts or 0))

    extra_sections = []
    if extra_instruction:
        extra_sections.append(extra_instruction)
    if template_context:
        extra_sections.append(template_context)
    extra_block = "\n\n" + "\n\n".join(extra_sections) if extra_sections else ""
    base_conversation: list[LLMMessage] = [
        LLMMessage.system(system_prompt),
        LLMMessage.user(
            f"## Compact Global Design Specification\n\n{compact_design_spec}\n\n"
            f"## Deck Generation Context\n\n{generation_context_block}\n\n"
            f"## SVG Technical Standards\n\n{standards}\n\n"
            f"## Fixed Runtime Configuration\n\n"
            f"- Selected style preset: {style}\n"
            f"- Selected language: {language}\n"
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
        for msg in base_conversation:
            parts.append(f"--- ROLE: {msg.role} ---\n\n{msg.content}")
        prompt_file.write_text("\n\n".join(parts), encoding="utf-8")
    except Exception:
        pass

    generated_history = _load_existing_svg_history(
        svg_output_dir,
        pages,
    )

    for i, page_content in enumerate(pages):
        page_num = i + 1
        if target_pages is not None and page_num not in target_pages:
            continue

        page_name = _make_page_name(page_num, page_content)
        page_type = _classify_page_type(page_content)
        conversation = list(base_conversation)
        continuity_message = _same_type_svg_history_message(
            generated_history,
            current_page_num=page_num,
            current_page_type=page_type,
        )
        if continuity_message:
            conversation.append(LLMMessage.user(continuity_message))
        is_structural_page = page_type in STRUCTURAL_PAGE_TYPES
        visible_page_content = strip_page_type_metadata(page_content)
        if is_structural_page:
            visible_page_content = _drop_paper_figure_tokens_for_structural_page(
                visible_page_content
            )
            visible_page_content = _minimal_structural_page_content(
                visible_page_content,
                page_type,
            )
        rewritten_content, used_figures, rejected_figures = _resolve_fig_tokens(
            visible_page_content,
            figure_inventory,
        )
        figure_source = "manuscript"
        if not used_figures and not is_structural_page:
            referenced_figures = _figures_from_page_references(
                visible_page_content,
                figure_inventory,
            )
            if referenced_figures:
                figure_source = "manuscript_reference"
                used_figures = referenced_figures
                reference_lines = "\n".join(
                    _paper_figure_reference_line(fig) for fig in referenced_figures
                )
                rewritten_content = (
                    f"{rewritten_content}\n\n"
                    "Paper figure references resolved from this slide manuscript:\n"
                    f"{reference_lines}"
                )
        # Fallback: if manuscript has no figure tokens or text references,
        # check design_spec Section VIII for explicit page assignments.
        if not used_figures and not is_structural_page and design_spec:
            spec_figures = _figures_from_design_spec_for_page(
                design_spec, page_num, figure_inventory,
            )
            if spec_figures:
                figure_source = "design_spec"
                used_figures = spec_figures
                reference_lines = "\n".join(
                    _paper_figure_reference_line(fig) for fig in spec_figures
                )
                rewritten_content = (
                    f"{rewritten_content}\n\n"
                    "Paper figure assignment from design_spec Image Resource List:\n"
                    f"{reference_lines}"
                )
        used_figures, optimized_figure_notes = _optimize_figure_choices(
            used_figures,
            figure_inventory,
        )
        rejected_figures.extend(optimized_figure_notes)
        if optimized_figure_notes and used_figures:
            rewritten_content += (
                "\n\nOptimized paper figure assignment for generation "
                "(use these instead of any earlier figure assignment lines):\n"
                + "\n".join(_paper_figure_reference_line(fig) for fig in used_figures)
            )
        used_figures, limited_figure_notes = _limit_figure_count_for_layout(
            used_figures,
            page_type=page_type,
        )
        rejected_figures.extend(limited_figure_notes)
        if limited_figure_notes and used_figures:
            rewritten_content += (
                "\n\nFigure budget note:\n"
                + "\n".join(f"- {note}" for note in limited_figure_notes)
                + "\n"
                + "\n".join(_paper_figure_reference_line(fig) for fig in used_figures)
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
        img_layout = _figure_layout_guidance(used_figures)
        page_role_guidance = _page_role_guidance(page_type)
        factual_guardrails = _factual_rendering_guardrails(page_type)
        layout_guardrails = _layout_generation_guardrails(
            page_type,
            has_paper_figure=bool(used_figures),
        )
        structural_figure_policy = (
            "\n- Structural page paper-figure policy: do not place extracted "
            "paper figures (`../sources/images/fig_*.png`) on cover, chapter, "
            "TOC, or ending pages. Use native SVG decoration or template images "
            "instead.\n"
            if is_structural_page
            else ""
        )

        # Build skeleton injection block if template skeletons are available
        skeleton_block = ""
        if template_skeletons:
            skeleton_svg = template_skeletons.get(page_type)
            if skeleton_svg:
                skeleton_svg = _prepare_template_skeleton(
                    page_type,
                    skeleton_svg,
                    template_vars.get(page_num),
                )
                if skeleton_svg:
                    skeleton_block = (
                        _template_skeleton_instruction(page_type)
                        +
                        f"```svg\n{skeleton_svg}\n```"
                    )

        slide_plan_block = slide_plan_markdown(deck_plan, page_num)
        page_design_block = _extract_page_design_block(design_spec, page_num)
        text_layout = _dynamic_text_layout_block(
            rewritten_content,
            page_design_block,
            has_paper_figure=bool(used_figures),
        )
        page_design_section = (
            f"## Page Design Contract\n\n{page_design_block}\n\n"
            if page_design_block
            else ""
        )
        slide_memory = (slide_contexts or {}).get(page_num, "").strip()
        slide_memory_section = (
            f"\n\n{slide_memory}\n\n"
            if slide_memory
            else "\n\n"
        )
        conversation.append(
            LLMMessage.user(
                f"## Current Structured Slide Plan\n\n{slide_plan_block}\n\n"
                f"{slide_memory_section}"
                f"{page_design_section}"
                f"## Page {page_num}/{len(pages)}: {page_name}\n\n"
                f"{rewritten_content}\n\n"
                f"## Runtime Reminders\n"
                f"- Style preset: {style}\n"
                f"- Language: {language}\n"
                f"- Page type: {page_type}. Use this type only for template selection; do not render metadata comments.\n"
                f"- Keep all visible text in the requested language.\n"
                f"{page_role_guidance}\n"
                f"{factual_guardrails}"
                f"{layout_guardrails}"
                f"{text_layout}\n"
                f"{structural_figure_policy}\n\n"
                f"{figure_guidance}\n\n"
                f"{icon_guidance}\n\n"
                f"{img_layout}\n\n"
                f"## Output Complexity Budget\n"
                f"- Keep the SVG compact and editable; target under {SVG_GENERATION_MAX_TOKENS} output tokens.\n"
                f"- Prefer grouped primitives, reusable styles, and concise text. Avoid huge decorative path dumps or duplicate off-canvas elements.\n\n"
                f"## Layout Budget\n"
                f"- Follow the design specification's canvas, content boundaries, and footer clearance.\n"
                f"- Resolve fit through composition, region sizes, and concise wording while preserving substantive content.\n\n"
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

        generation_max_tokens = SVG_GENERATION_MAX_TOKENS
        response, generation_max_tokens = await _chat_svg_generation(
            llm,
            conversation,
            model,
            temperature=0.3,
            max_tokens=generation_max_tokens,
            project_dir=project_dir,
            debug_prefix="executor",
            page_num=page_num,
            attempt=1,
        )

        svg_content = _extract_svg(response.content)
        for extraction_attempt in range(2, MAX_SVG_EXTRACTION_ATTEMPTS + 1):
            if svg_content:
                break
            if (
                _completion_near_limit(response, generation_max_tokens)
                and _svg_response_looks_truncated(response.content)
            ):
                generation_max_tokens = _next_higher_svg_max_tokens(generation_max_tokens)
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
            response, generation_max_tokens = await _chat_svg_generation(
                llm,
                conversation,
                model,
                temperature=0.2,
                max_tokens=generation_max_tokens,
                project_dir=project_dir,
                debug_prefix="executor",
                page_num=page_num,
                attempt=extraction_attempt,
            )
            svg_content = _extract_svg(response.content)

        if svg_content:
            svg_content = _normalize_obvious_short_text_lines(svg_content, page_design_block)
            conversation.append(LLMMessage.assistant(f"```svg\n{svg_content}\n```"))

            best_svg = svg_content
            visual_attempts = 0
            critic_attempt = 1
            static_attempt_limit = _static_attempt_limit_for_report(
                max_critic_attempts,
                None,
            )
            while True:
                report: CriticReport | None = None
                report_metadata: CriticMetadata = {"source": "static"}
                event_attempt = critic_attempt

                if static_attempt_limit > 0:
                    report = _generation_static_report(
                        svg_content,
                        critic_config=critic_config,
                        allowed_figures=used_figures,
                        used_paper_figures=used_paper_figures,
                        required_icon=required_icon,
                        include_general_svg_checks=max_critic_attempts > 0,
                        page_content=page_content,
                    )
                    static_attempt_limit = _static_attempt_limit_for_report(
                        max_critic_attempts,
                        report,
                    )
                    if report.passed and max_critic_attempts <= 0:
                        report = None
                    report_metadata = {"source": "static"}
                    event_attempt = critic_attempt
                    if report is not None and not report.passed and critic_attempt > static_attempt_limit:
                        archive_rel = _archive_repair_svg(
                            repair_archive_dir,
                            page_num=page_num,
                            attempt=event_attempt,
                            label="unrepaired",
                            svg_content=svg_content,
                        )
                        if archive_rel:
                            report_metadata = {
                                **report_metadata,
                                "before_archive_path": archive_rel,
                            }
                        await _emit_critic(
                            on_critic,
                            page_num,
                            event_attempt,
                            report,
                            None,
                            archive_rel,
                            report_metadata,
                        )
                        best_svg = svg_content
                        break

                # Visual QA has its own budget. A static critic budget of zero
                # disables only static checks; it does not disable visual QA.
                if report is None or report.passed:
                    if report is not None:
                        await _emit_critic(
                            on_critic,
                            page_num,
                            event_attempt,
                            report,
                            None,
                            None,
                            report_metadata,
                        )
                    if not enable_visual_critic or visual_attempts >= visual_qa_max_attempts:
                        best_svg = svg_content
                        break
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
                    visual_metadata = _visual_outcome_metadata(
                        visual_outcome,
                        repair_archive_dir,
                        page_num=page_num,
                        attempt=visual_attempts,
                    )
                    event_attempt = visual_attempts
                    if visual_outcome.report.passed:
                        await _emit_critic(
                            on_critic,
                            page_num,
                            event_attempt,
                            visual_outcome.report,
                            None,
                            None,
                            visual_metadata,
                        )
                        best_svg = svg_content
                        break
                    if not visual_outcome.rendered:
                        best_svg = svg_content
                        break
                    report = visual_outcome.report
                    report_metadata = visual_metadata

                if report is None:
                    best_svg = svg_content
                    break

                # Archive pre-repair SVG for before/after comparison.
                before_archive_rel = _archive_repair_svg(
                    repair_archive_dir,
                    page_num=page_num,
                    attempt=event_attempt,
                    label="before",
                    svg_content=svg_content,
                )

                repair_prompt_text = (
                    report.to_prompt_block()
                    + "\n\nWhen fixing paper-figure violations, resize the image "
                    "to the visible frame, preserve the source aspect ratio, keep "
                    "the full rendered image inside x=0..1260 and y=80..640, and "
                    "remove over-cropping clip paths. Do not swap to an unlisted "
                    "paper figure href.\n\nReturn the complete corrected SVG only, "
                    "wrapped in a ```svg code block."
                )
                conversation.append(LLMMessage.user(repair_prompt_text))
                repair_temp = max(0.1, 0.3 - 0.1 * event_attempt)
                snapshot = set_usage_context(
                    stage="repair", page=page_num, attempt=event_attempt
                )
                try:
                    response = await llm.chat(
                        conversation,
                        model,
                        temperature=repair_temp,
                        max_tokens=SVG_REPAIR_MAX_TOKENS,
                    )
                finally:
                    reset_usage_context(snapshot)

                repaired = _extract_svg(response.content)
                after_archive_rel: str | None = None
                if repaired:
                    after_archive_rel = _archive_repair_svg(
                        repair_archive_dir,
                        page_num=page_num,
                        attempt=event_attempt,
                        label="after",
                        svg_content=repaired,
                    )
                    event_metadata: CriticMetadata = dict(report_metadata)
                    if before_archive_rel:
                        event_metadata["before_archive_path"] = before_archive_rel
                    if after_archive_rel:
                        event_metadata["after_archive_path"] = after_archive_rel
                    await _emit_critic(
                        on_critic,
                        page_num,
                        event_attempt,
                        report,
                        repair_prompt_text,
                        before_archive_rel,
                        event_metadata,
                    )
                    svg_content = repaired
                    best_svg = repaired
                    conversation.append(
                        LLMMessage.assistant(f"```svg\n{repaired}\n```")
                    )
                    if on_svg_update is not None:
                        await on_svg_update(page_num, repaired)
                    if report_metadata.get("source") == "static":
                        critic_attempt += 1
                    else:
                        # Visual QA attempts are counted when the image is
                        # inspected, so visual repairs do not spend static
                        # critic budget.
                        pass
                else:
                    event_metadata = dict(report_metadata)
                    if before_archive_rel:
                        event_metadata["before_archive_path"] = before_archive_rel
                    await _emit_critic(
                        on_critic,
                        page_num,
                        event_attempt,
                        report,
                        repair_prompt_text,
                        before_archive_rel,
                        event_metadata,
                    )
                    conversation.append(LLMMessage.assistant(response.content))
                    break

            _write_generated_svg(svg_output_dir, page_num, best_svg)
            generated_history[page_num] = (
                page_type,
                page_name,
                best_svg,
            )
            for href in IMAGE_HREF_RE.findall(best_svg):
                key = _paper_figure_key_from_href(href)
                if key is not None:
                    used_paper_figures.setdefault(key, page_num)
            yield page_num, best_svg
        else:
            conversation.append(LLMMessage.assistant(response.content))
            failure_message = (
                f"Failed to generate parseable SVG for page {page_num}/{len(pages)} "
                f"({page_name}) after {MAX_SVG_EXTRACTION_ATTEMPTS} attempts"
            )
            raise RuntimeError(failure_message)


def _extract_design_section(design_spec: str, roman: str) -> str:
    pattern = rf"(?ims)^#+\s*{re.escape(roman)}\.\s+.*?(?=^#+\s*[IVXLCDM]+\.\s+|\Z)"
    match = re.search(pattern, design_spec)
    return match.group(0).strip() if match else ""


def _compact_prompt_block(text: str, limit: int, *, label: str = "context") -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[: max(0, limit - 120)].rstrip()
    return f"{head}\n\n[{label} compacted to {limit} characters for provider working-memory mode.]"


def _extract_page_design_block(design_spec: str, page_num: int) -> str:
    pattern = (
        rf"(?ims)^#+\s*(?:slide|page)\s*0*{page_num}\b.*?"
        rf"(?=^#+\s*(?:slide|page)\s*0*\d+\b"
        rf"|^#+\s*(?:part|chapter|section)\s+\d+\b"
        rf"|^#+\s*[IVXLCDM]+\.\s+|\Z)"
    )
    match = re.search(pattern, design_spec)
    return match.group(0).strip() if match else ""


def _extract_page_section_heading(design_spec: str, page_num: int) -> str:
    slide_pattern = rf"(?im)^#+\s*(?:slide|page)\s*0*{page_num}\b.*$"
    slide_match = re.search(slide_pattern, design_spec)
    if not slide_match:
        return ""
    section_pattern = r"(?im)^#+\s*(?:part|chapter|section)\s+\d+\b.*$"
    section_matches = list(re.finditer(section_pattern, design_spec[: slide_match.start()]))
    return section_matches[-1].group(0).strip() if section_matches else ""


def _extract_page_title_parts(page_content: str) -> tuple[str, str]:
    visible = strip_page_type_metadata(page_content).strip()
    headings = list(re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", visible))
    title = headings[0].group(1).strip() if headings else ""
    subtitle = headings[1].group(1).strip() if len(headings) > 1 else ""
    bolds = [m.group(1).strip() for m in re.finditer(r"(?m)^\*\*(.+?)\*\*\s*$", visible)]
    if not title and bolds:
        title = bolds[0]
        bolds = bolds[1:]
    if not subtitle and bolds:
        subtitle = bolds[0]
    if not title:
        first_line = next((line.strip() for line in visible.splitlines() if line.strip()), "")
        title = re.sub(r"^[#*\-\s]+|[#*\-\s]+$", "", first_line) or "Untitled"
    return title, subtitle


def _clean_structural_phrase(text: str) -> str:
    phrase = text.strip()
    phrase = re.sub(r"^\*\*(.+?)\*\*$", r"\1", phrase).strip()
    phrase = re.sub(r"^[#*\-\s]+|[#*\-\s]+$", "", phrase).strip()
    return phrase


def _structural_visible_phrases(page_content: str) -> list[str]:
    visible = strip_page_type_metadata(page_content).strip()
    phrases: list[str] = []
    for line in visible.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", stripped)
        candidate = heading.group(1) if heading else stripped
        if STRUCTURAL_LIST_LINE_RE.match(stripped) or "[[FIG:" in stripped:
            continue
        cleaned = _clean_structural_phrase(candidate)
        if cleaned:
            phrases.append(cleaned)
    return phrases


def _looks_like_publication_source(text: str) -> bool:
    lower = text.lower()
    if re.search(r"\b(?:19|20)\d{2}\b", text) and re.search(r"\b(?:conference|journal|workshop|symposium|proceedings)\b", lower):
        return True
    source_tokens = (
        "acm",
        "ieee",
        "cvpr",
        "iccv",
        "eccv",
        "neurips",
        "nips",
        "iclr",
        "icml",
        "aaai",
        "ijcai",
        "acl",
        "emnlp",
        "naacl",
        "siggraph",
        "kdd",
        "www",
        "chi",
        "uist",
        "arxiv",
        "journal",
        "conference",
        "transactions",
        "proceedings",
        "letters",
    )
    return any(token in lower for token in source_tokens)


def _looks_like_author_line(text: str) -> bool:
    lower = text.lower()
    if any(token in lower for token in ("author", "anonymous", "anon.", "et al", "presenter", "speaker", "作者")):
        return True
    if "," in text or ";" in text or "，" in text or "、" in text or "等" in text or " and " in lower or " & " in text:
        return True
    return bool(re.search(r"\b[A-Z][A-Za-z.-]+(?:\s+[A-Z][A-Za-z.-]+){1,}\b", text))


def _looks_like_date_line(text: str) -> bool:
    return bool(re.fullmatch(r".*(?:19|20)\d{2}(?:[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?)?.*", text.strip()))


def _extract_cover_metadata(page_content: str, title: str, subtitle: str) -> dict[str, str]:
    phrases = _structural_visible_phrases(page_content)
    source = subtitle if subtitle and _looks_like_publication_source(subtitle) else ""
    author = ""
    date = ""

    for phrase in phrases[1:]:
        if phrase in {title, subtitle}:
            continue
        if not date and _looks_like_date_line(phrase) and not _looks_like_publication_source(phrase):
            date = phrase
            continue
        if not source and _looks_like_publication_source(phrase):
            source = phrase
            continue
        if not author and _looks_like_author_line(phrase):
            author = phrase

    brand_label = source
    return {
        "AUTHOR": author,
        "AUTHORS": author,
        "PRESENTER": author,
        "SOURCE": source,
        "JOURNAL": source,
        "VENUE": source,
        "CONFERENCE": source,
        "DATE": date,
        "BRAND_LABEL": brand_label,
        "COVER_QUOTE": "",
    }


def _cover_context_phrases(
    page_content: str,
    title: str,
    subtitle: str,
    metadata_values: set[str],
    *,
    max_lines: int = 3,
) -> list[str]:
    visible = strip_page_type_metadata(page_content).strip()
    phrases: list[str] = []
    seen = {value for value in {title, subtitle, *metadata_values} if value}
    for line in visible.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        if "[[FIG:" in stripped or "<image" in stripped.lower():
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", stripped)
        candidate = heading.group(1) if heading else stripped
        candidate = re.sub(r"^\s*(?:[-*•]|\d+[\.)、])\s+", "", candidate).strip()
        candidate = _clean_structural_phrase(candidate)
        if not candidate or candidate in seen or len(candidate) > 160:
            continue
        phrases.append(candidate)
        seen.add(candidate)
        if len(phrases) >= max_lines:
            break
    return phrases


def _minimal_structural_page_content(page_content: str, page_type: str) -> str:
    """Reduce structural manuscript pages to the text they are allowed to render."""
    visible = strip_page_type_metadata(page_content).strip()
    title, subtitle = _extract_page_title_parts(visible)
    if page_type == "toc":
        toc_items: list[str] = []
        for line in visible.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("<!--") or "[[FIG:" in stripped:
                continue
            if re.match(r"^#{1,6}\s+(.+?)\s*$", stripped):
                continue
            if not STRUCTURAL_LIST_LINE_RE.match(stripped):
                continue
            item = _clean_structural_phrase(stripped)
            if item and item != title and item not in toc_items:
                toc_items.append(item)
        lines = [f"# {title or '目录'}"]
        lines.extend(f"- {item}" for item in toc_items[:8])
        return "\n\n".join(lines)

    if not subtitle:
        title_seen = False
        for line in visible.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("<!--"):
                continue
            heading = re.match(r"^#{1,6}\s+(.+?)\s*$", stripped)
            if heading:
                if not title_seen:
                    title_seen = True
                    continue
                candidate = _clean_structural_phrase(heading.group(1))
            else:
                if STRUCTURAL_LIST_LINE_RE.match(stripped):
                    continue
                if "[[FIG:" in stripped:
                    continue
                if STRUCTURAL_CONTENT_LABEL_RE.search(stripped):
                    continue
                candidate = _clean_structural_phrase(stripped)
            if not candidate or candidate == title:
                continue
            if len(candidate) <= 120:
                subtitle = candidate
                break

    lines = [f"# {title or 'Untitled'}"]
    if subtitle and subtitle != title:
        lines.append(f"## {subtitle}")
    if page_type == "cover":
        cover_meta = _extract_cover_metadata(page_content, title, subtitle)
        source = cover_meta.get("SOURCE", "")
        author = cover_meta.get("AUTHOR", "")
        date = cover_meta.get("DATE", "")
        if source and source not in {title, subtitle}:
            lines.append(f"### {source}")
        if author and author not in {title, subtitle, source}:
            lines.append(f"### {author}")
        if date and date not in {title, subtitle, source, author}:
            lines.append(f"### {date}")
        for phrase in _cover_context_phrases(
            page_content,
            title,
            subtitle,
            {source, author, date},
        ):
            lines.append(phrase)
    return "\n\n".join(lines)



def _template_vars_by_page(pages: list[str]) -> dict[int, dict[str, str]]:
    result: dict[int, dict[str, str]] = {}
    chapter_index = 0
    current_chapter = {"num": "", "title": "", "desc": ""}
    toc_items: list[tuple[str, str]] = []
    fallback_items: list[tuple[str, str]] = []

    for page in pages:
        if extract_page_type(page) != "toc":
            continue
        toc_items = _toc_items_from_page(page)
        if toc_items:
            break

    for page in pages:
        page_type = extract_page_type(page)
        title, subtitle = _extract_page_title_parts(page)
        if not title:
            continue
        if page_type == "chapter" and not toc_items:
            toc_items.append((title, subtitle))
        elif page_type not in {"cover", "toc", "ending"}:
            fallback_items.append((title, subtitle))
    if not toc_items:
        toc_items = fallback_items

    for page_num, page in enumerate(pages, 1):
        page_type = extract_page_type(page)
        title, subtitle = _extract_page_title_parts(page)
        if page_type == "chapter":
            chapter_index += 1
            current_chapter = {
                "num": f"{chapter_index:02d}",
                "title": title,
                "desc": subtitle,
            }

        variables = {
            "PAGE_NUM": str(page_num),
            "PAGE_NUM_PADDED": f"{page_num:02d}",
            "TOTAL_PAGES": str(len(pages)),
            "PAGE_TITLE": title,
            "PAGE_SUBTITLE": subtitle,
            "TITLE": title,
            "SUBTITLE": subtitle,
            "SECTION_NUM": current_chapter["num"],
            "SECTION_NAME": current_chapter["title"],
            "CHAPTER_NUM": current_chapter["num"],
            "CHAPTER_NUMBER": current_chapter["num"],
            "CHAPTER_TITLE": current_chapter["title"],
            "CHAPTER_DESC": current_chapter["desc"],
            "CHAPTER_SUBTITLE": current_chapter["desc"],
            "AUTHOR": "",
            "AUTHORS": "",
            "PRESENTER": "",
            "SOURCE": "",
            "JOURNAL": "",
            "VENUE": "",
            "CONFERENCE": "",
            "DATE": "",
            "BRAND_LABEL": "",
            "COVER_QUOTE": "",
            "ENDING_TITLE": title,
            "ENDING_MESSAGE": subtitle,
        }
        variables["TOC_ITEM_COUNT"] = str(len(toc_items))
        for idx in range(max(12, len(toc_items))):
            item_title = toc_items[idx][0] if idx < len(toc_items) else ""
            item_desc = toc_items[idx][1] if idx < len(toc_items) else ""
            variables[f"TOC_ITEM_{idx + 1}"] = item_title
            variables[f"TOC_ITEM_{idx + 1}_TITLE"] = item_title
            variables[f"TOC_ITEM_{idx + 1}_DESC"] = item_desc
        if page_type == "cover":
            variables.update(_extract_cover_metadata(page, title, subtitle))
        result[page_num] = variables
    return result


def _toc_items_from_page(page: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for raw_line in page.splitlines():
        line = re.sub(r"^\s*[-*•]\s*", "", raw_line).strip()
        match = re.match(r"^\s*\d+[\.\)、]\s*(.+)$", line)
        if not match:
            continue
        body = match.group(1).strip()
        bold = re.match(r"\*\*(.+?)\*\*\s*[:：]?\s*(.*)$", body)
        if bold:
            title = _plain_inline_text(bold.group(1))
            desc = _plain_inline_text(bold.group(2))
        else:
            parts = re.split(r"\s*[:：]\s*", body, maxsplit=1)
            title = _plain_inline_text(parts[0])
            desc = _plain_inline_text(parts[1]) if len(parts) > 1 else ""
        if title:
            items.append((title, desc))
    return items


def _plain_inline_text(value: str) -> str:
    text = re.sub(r"[*_`]+", "", value or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n:：")


def _prepare_template_skeleton(
    page_type: str,
    skeleton_svg: str,
    variables: dict[str, str] | None,
) -> str:
    raw = _normalize_template_placeholder_text_slots(skeleton_svg)
    if page_type == "toc":
        item_count = _safe_int((variables or {}).get("TOC_ITEM_COUNT"))
        capacity = _toc_template_capacity(raw)
        if item_count and capacity and capacity < item_count:
            return ""
        raw = _strip_non_toc_template_content(raw)
    return _resolve_template_skeleton_placeholders(raw, variables)


def _safe_int(value: str | None) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _toc_template_capacity(skeleton_svg: str) -> int:
    slots = [
        int(match.group(1))
        for match in re.finditer(
            r"\{\{TOC_ITEM_(\d+)(?:_(?:TITLE|DESC))?\}\}",
            skeleton_svg,
        )
    ]
    return max(slots, default=0)


def _resolve_template_skeleton_placeholders(
    skeleton_svg: str,
    variables: dict[str, str] | None,
) -> str:
    skeleton_svg = _normalize_template_placeholder_text_slots(skeleton_svg)
    if not variables:
        return skeleton_svg
    resolved = skeleton_svg
    # Some legacy templates write 0{{CHAPTER_NUM}} expecting an unpadded
    # single digit. Replace that compound form first so chapter 02 does not
    # become 002.
    for key in ("CHAPTER_NUM", "SECTION_NUM", "PAGE_NUM"):
        value = variables.get(key, "")
        if value:
            resolved = resolved.replace(f"0{{{{{key}}}}}", html_escape(value, quote=True))

    for key, value in variables.items():
        resolved = resolved.replace(f"{{{{{key}}}}}", html_escape(value, quote=True))
    return resolved


def _strip_non_toc_template_content(skeleton_svg: str) -> str:
    if not NON_TOC_TEMPLATE_PLACEHOLDER_RE.search(skeleton_svg):
        return skeleton_svg
    try:
        root = ET.fromstring(skeleton_svg)
    except ET.ParseError:
        return skeleton_svg
    namespace = _namespace_uri(root.tag)
    if namespace:
        ET.register_namespace("", namespace)

    parent_map = {child: parent for parent in root.iter() for child in list(parent)}
    placeholder_nodes: list[ET.Element] = []
    placeholder_points: list[tuple[float, float]] = []
    for element in root.iter():
        local = _local_name(element.tag)
        serialized = ET.tostring(element, encoding="unicode")
        if not NON_TOC_TEMPLATE_PLACEHOLDER_RE.search(serialized):
            continue
        if local == "text":
            placeholder_nodes.append(element)
            placeholder_points.append(
                (_svg_float_attr(element, "x"), _svg_float_attr(element, "y"))
            )

    removals: set[ET.Element] = set()
    for node in placeholder_nodes:
        candidate = node
        parent = parent_map.get(node)
        while parent is not None and parent is not root:
            serialized = ET.tostring(parent, encoding="unicode")
            if TOC_TEMPLATE_PLACEHOLDER_RE.search(serialized):
                break
            if _local_name(parent.tag) == "g":
                candidate = parent
            parent = parent_map.get(parent)
        removals.add(candidate)

    region = _expanded_point_region(placeholder_points)
    if region is not None:
        for element in list(root.iter()):
            if element in removals or element is root:
                continue
            if _element_in_non_toc_region(element, region):
                removals.add(element)

    for element in removals:
        parent = parent_map.get(element)
        if parent is None:
            continue
        try:
            parent.remove(element)
        except ValueError:
            pass
    return ET.tostring(root, encoding="unicode")


def _expanded_point_region(
    points: list[tuple[float, float]],
) -> tuple[float, float, float, float] | None:
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (
        max(0.0, min(xs) - 220.0),
        max(90.0, min(ys) - 120.0),
        min(1280.0, max(xs) + 220.0),
        min(640.0, max(ys) + 100.0),
    )


def _element_in_non_toc_region(
    element: ET.Element,
    region: tuple[float, float, float, float],
) -> bool:
    if TOC_TEMPLATE_PLACEHOLDER_RE.search(ET.tostring(element, encoding="unicode")):
        return False
    bbox = _simple_element_bbox(element)
    if bbox is None:
        return False
    x, y, w, h = bbox
    if w >= 1200 or h >= 600:
        return False
    if y < 90 or y + h > 650:
        return False
    rx1, ry1, rx2, ry2 = region
    bx2 = x + w
    by2 = y + h
    return not (bx2 < rx1 or x > rx2 or by2 < ry1 or y > ry2)


def _simple_element_bbox(element: ET.Element) -> tuple[float, float, float, float] | None:
    local = _local_name(element.tag)
    if local in {"rect", "image"}:
        return (
            _svg_float_attr(element, "x"),
            _svg_float_attr(element, "y"),
            _svg_float_attr(element, "width"),
            _svg_float_attr(element, "height"),
        )
    if local == "line":
        x1 = _svg_float_attr(element, "x1")
        y1 = _svg_float_attr(element, "y1")
        x2 = _svg_float_attr(element, "x2")
        y2 = _svg_float_attr(element, "y2")
        return (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
    if local == "circle":
        r = _svg_float_attr(element, "r")
        return (
            _svg_float_attr(element, "cx") - r,
            _svg_float_attr(element, "cy") - r,
            r * 2,
            r * 2,
        )
    if local == "text":
        x = _svg_float_attr(element, "x")
        y = _svg_float_attr(element, "y")
        font_size = _svg_font_size(element)
        width = max(20.0, _estimate_text_width(_element_text(element), font_size))
        return (x, y - font_size, width, font_size * 1.3)
    return None


def _normalize_template_placeholder_text_slots(skeleton_svg: str) -> str:
    """Collapse imported PPT per-character x lists on placeholder text nodes."""

    def _replace(match: re.Match) -> str:
        attrs = match.group("attrs")
        body = match.group("body")
        if not TEMPLATE_PLACEHOLDER_RE.search(body):
            return match.group(0)
        x_match = TEXT_X_ATTR_RE.search(attrs)
        if not x_match:
            return match.group(0)
        values = [float(raw) for raw in NUMBER_RE.findall(x_match.group("value"))]
        if len(values) < 2:
            return match.group(0)
        center_x = (values[0] + values[-1]) / 2
        attrs = TEXT_X_ATTR_RE.sub(f'x="{center_x:.2f}"', attrs, count=1)
        if not TEXT_ANCHOR_ATTR_RE.search(attrs):
            attrs = f'{attrs} text-anchor="middle"'
        return f"<text{attrs}>{body}</text>"

    return TEXT_BLOCK_RE.sub(_replace, skeleton_svg)


def _template_skeleton_instruction(page_type: str) -> str:
    structural_note = (
        " For structural pages, keep role-matching chrome and omit template "
        "content placeholders that do not belong to this page role."
        if page_type in STRUCTURAL_PAGE_TYPES
        else ""
    )
    return (
        f"\n\n## Template Skeleton ({page_type} page)\n"
        "Use this SVG as your already data-bound starting point. If any "
        "placeholder token remains, replace it with the current slide plan value. "
        "Preserve ALL decorative elements, gradients, structural chrome, and local "
        "`assets/...` image hrefs."
        f"{structural_note} Do NOT invent image paths or swap template "
        "images for `../sources/images/...` files. Do NOT rewrite from scratch. "
        "Every `<image>` element in the skeleton is structural brand chrome; keep "
        "its href, x/y, width/height, transform, opacity, and preserveAspectRatio "
        "unless the current slide plan explicitly says to replace that exact image. "
        "Preserve path/polygon transforms and orientation exactly; do not mirror, "
        "flip, rotate, or relocate corner decorations. "
        "When replacing long text in imported PPT text slots, keep the text inside "
        "the original slot by using a single x value, `text-anchor=\"middle\"`, "
        "line breaks, or a smaller font size; do not keep per-character x lists "
        "from the placeholder. Do NOT recompute chapter/section numbers from the "
        "page number.\n\n"
    )


def _parallel_global_design_context(design_spec: str) -> str:
    sections = [
        _compact_prompt_block(
            _extract_design_section(design_spec, roman),
            MAX_DESIGN_SECTION_CHARS,
            label=f"design section {roman}",
        )
        for roman in ("I", "II", "III", "IV", "V")
    ]
    compact = "\n\n".join(section for section in sections if section)
    return compact or design_spec


def _parallel_page_inventory(pages: list[str]) -> str:
    lines = ["| Page | Type | Title |", "| ---- | ---- | ----- |"]
    for idx, page in enumerate(pages, 1):
        page_type = extract_page_type(page)
        title = _make_page_name(idx, page).replace("_", " ")
        lines.append(f"| {idx} | {page_type} | {title} |")
    return "\n".join(lines)


def _build_parallel_context_document(
    *,
    design_spec: str,
    pages: list[str],
    style: str,
    language: str,
    mode: str,
    deck_plan_block: str = "",
) -> str:
    page_blocks = []
    for idx, _page in enumerate(pages, 1):
        block = _extract_page_design_block(design_spec, idx)
        if block:
            section_heading = _extract_page_section_heading(design_spec, idx)
            page_blocks.append(
                f"{section_heading}\n\n{block}" if section_heading else block
            )
    return (
        "# Parallel SVG Executor Context\n\n"
        "This derived context is used only by parallel generation modes. "
        "The canonical design_spec.md remains unchanged for sequential generation.\n\n"
        "## Runtime\n\n"
        f"- Generation mode: {mode}\n"
        f"- Style preset: {style}\n"
        f"- Language: {language}\n\n"
        "## Global Design Contract\n\n"
        f"{_parallel_global_design_context(design_spec)}\n\n"
        "## Page Inventory\n\n"
        f"{_parallel_page_inventory(pages)}\n\n"
        "## Structured Deck Plan\n\n"
        f"{deck_plan_block}\n\n"
        "## Page Contracts\n\n"
        + "\n\n---\n\n".join(page_blocks)
    )


def _compact_paint(value: str | None) -> str:
    value = (value or "").strip()
    if not value or value.lower() in {"none", "transparent"}:
        return ""
    if value.startswith("url("):
        return "gradient"
    return value[:32]


def _svg_num_attr(elem: ET.Element, name: str) -> float | None:
    raw = (elem.get(name) or "").strip()
    if not raw:
        return None
    match = re.match(r"^-?\d+(?:\.\d+)?", raw)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _fmt_num(value: float | None) -> str:
    if value is None:
        return "?"
    return str(int(round(value)))


def _svg_layout_style_brief(
    svg_content: str,
    *,
    page_num: int,
    page_type: str,
) -> str:
    """Extract a compact, content-light layout summary from a generated SVG."""
    try:
        root = ET.fromstring(svg_content)
    except ET.ParseError:
        return ""

    colors: Counter[str] = Counter()
    fonts: list[str] = []
    sizes: Counter[str] = Counter()
    anchors: Counter[str] = Counter()
    boxes: list[str] = []
    images: list[str] = []

    for elem in root.iter():
        tag = _local_tag(elem.tag)
        for attr in ("fill", "stroke"):
            paint = _compact_paint(elem.get(attr))
            if paint:
                colors[paint] += 1

        if tag in {"text", "tspan"}:
            family = (elem.get("font-family") or "").strip()
            if family and family not in fonts:
                fonts.append(family)
            size = (elem.get("font-size") or "").strip()
            if size:
                sizes[size] += 1
            anchor = (elem.get("text-anchor") or "start").strip()
            anchors[anchor] += 1

        if tag == "rect" and len(boxes) < 8:
            x = _svg_num_attr(elem, "x")
            y = _svg_num_attr(elem, "y")
            width = _svg_num_attr(elem, "width")
            height = _svg_num_attr(elem, "height")
            if width is None or height is None:
                continue
            area = width * height
            is_background = width >= 1200 and height >= 650 and (x in (None, 0.0)) and (y in (None, 0.0))
            if area >= 6000 and not is_background:
                fill = _compact_paint(elem.get("fill")) or "none"
                radius = _svg_num_attr(elem, "rx") or _svg_num_attr(elem, "ry")
                boxes.append(
                    f"rect x={_fmt_num(x)} y={_fmt_num(y)} w={_fmt_num(width)} "
                    f"h={_fmt_num(height)} r={_fmt_num(radius)} fill={fill}"
                )

        if tag == "image" and len(images) < 4:
            images.append(
                f"image x={_fmt_num(_svg_num_attr(elem, 'x'))} "
                f"y={_fmt_num(_svg_num_attr(elem, 'y'))} "
                f"w={_fmt_num(_svg_num_attr(elem, 'width'))} "
                f"h={_fmt_num(_svg_num_attr(elem, 'height'))}"
            )

    lines = [
        f"- Previous generated page: {page_num} ({page_type}).",
        "- Use this as layout/style continuity only; do not copy its text, page number, section label, or image assignment.",
    ]
    if colors:
        lines.append("- Dominant paints: " + ", ".join(color for color, _ in colors.most_common(8)))
    if fonts:
        lines.append("- Font families used: " + "; ".join(fonts[:4]))
    if sizes:
        lines.append("- Text scale used: " + ", ".join(size for size, _ in sizes.most_common(8)))
    if anchors:
        lines.append("- Text anchors: " + ", ".join(f"{anchor}={count}" for anchor, count in anchors.most_common(4)))
    if boxes:
        lines.append("- Major layout boxes: " + " | ".join(boxes[:6]))
    if images:
        lines.append("- Image slots: " + " | ".join(images))
    return "\n".join(lines)


def _prepare_parallel_page_inputs(
    *,
    page_num: int,
    total_pages: int,
    page_content: str,
    design_spec: str,
    figure_inventory: list[dict] | None,
    claimed_paper_figures: set[str],
    template_vars: dict[str, str] | None = None,
    slide_plan: str = "",
    slide_context: str = "",
) -> dict:
    page_name = _make_page_name(page_num, page_content)
    page_type = _classify_page_type(page_content)
    is_structural_page = page_type in STRUCTURAL_PAGE_TYPES
    visible_page_content = strip_page_type_metadata(page_content)
    if is_structural_page:
        visible_page_content = _drop_paper_figure_tokens_for_structural_page(
            visible_page_content
        )
        visible_page_content = _minimal_structural_page_content(
            visible_page_content,
            page_type,
        )

    rewritten_content, used_figures, rejected_figures = _resolve_fig_tokens(
        visible_page_content,
        figure_inventory,
    )
    figure_source = "manuscript"
    if not used_figures and not is_structural_page:
        referenced_figures = _figures_from_page_references(
            visible_page_content,
            figure_inventory,
        )
        if referenced_figures:
            figure_source = "manuscript_reference"
            used_figures = referenced_figures
            reference_lines = "\n".join(
                _paper_figure_reference_line(fig) for fig in referenced_figures
            )
            rewritten_content = (
                f"{rewritten_content}\n\n"
                "Paper figure references resolved from this slide manuscript:\n"
                f"{reference_lines}"
            )
    # Fallback: if manuscript has no figure tokens or text references,
    # check design_spec Section VIII for explicit page assignments.
    if not used_figures and not is_structural_page and design_spec:
        spec_figures = _figures_from_design_spec_for_page(
            design_spec, page_num, figure_inventory,
        )
        if spec_figures:
            figure_source = "design_spec"
            used_figures = spec_figures
            reference_lines = "\n".join(
                _paper_figure_reference_line(fig) for fig in spec_figures
            )
            rewritten_content = (
                f"{rewritten_content}\n\n"
                "Paper figure assignment from design_spec Image Resource List:\n"
                f"{reference_lines}"
            )

    used_figures, optimized_figure_notes = _optimize_figure_choices(
        used_figures,
        figure_inventory,
    )
    rejected_figures.extend(optimized_figure_notes)
    if optimized_figure_notes and used_figures:
        rewritten_content += (
            "\n\nOptimized paper figure assignment for generation "
            "(use these instead of any earlier figure assignment lines):\n"
            + "\n".join(_paper_figure_reference_line(fig) for fig in used_figures)
        )
    used_figures, limited_figure_notes = _limit_figure_count_for_layout(
        used_figures,
        page_type=page_type,
    )
    rejected_figures.extend(limited_figure_notes)
    if limited_figure_notes and used_figures:
        rewritten_content += (
            "\n\nFigure budget note:\n"
            + "\n".join(f"- {note}" for note in limited_figure_notes)
            + "\n"
            + "\n".join(_paper_figure_reference_line(fig) for fig in used_figures)
        )

    duplicate_keys = _paper_figure_keys(used_figures).intersection(claimed_paper_figures)
    if duplicate_keys:
        used_figures = [
            fig
            for fig in used_figures
            if Path(str(fig.get("path") or "")).stem not in duplicate_keys
        ]
        rewritten_content = _remove_paper_reference_lines(
            rewritten_content,
            duplicate_keys,
        )
        rejected_figures.extend(
            f"{key}: already reserved for an earlier slide" for key in sorted(duplicate_keys)
        )
    claimed_paper_figures.update(_paper_figure_keys(used_figures))

    page_design_block = _extract_page_design_block(design_spec, page_num)

    return {
        "page_num": page_num,
        "total_pages": total_pages,
        "page_name": page_name,
        "page_type": page_type,
        "is_structural_page": is_structural_page,
        "page_content": page_content,
        "rewritten_content": rewritten_content,
        "used_figures": used_figures,
        "rejected_figures": rejected_figures,
        "figure_source": figure_source,
        "page_design_block": page_design_block,
        "template_vars": template_vars or {},
        "slide_plan": slide_plan,
        "slide_context": slide_context,
    }


async def _generate_parallel_page(
    page_input: dict,
    *,
    base_context: str,
    standards: str,
    project_dir: Path,
    llm: LLMProvider,
    model: str,
    style: str,
    language: str,
    detail_level: str,
    extra_instruction: str,
    critic_config: CriticConfig | None,
    on_critic: CriticCallback | None,
    on_svg_update: SvgUpdateCallback | None,
    enable_visual_critic: bool,
    max_critic_attempts: int,
    visual_qa_max_attempts: int,
    visual_critic_config: VisualCriticConfig | None,
    template_skeletons: dict[str, str] | None,
) -> tuple[int, str]:
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    page_num = int(page_input["page_num"])
    total_pages = int(page_input["total_pages"])
    page_name = str(page_input["page_name"])
    page_type = str(page_input["page_type"])
    rewritten_content = str(page_input["rewritten_content"])
    used_figures = list(page_input["used_figures"])
    rejected_figures = list(page_input["rejected_figures"])
    figure_source = str(page_input["figure_source"])
    page_design_block = str(page_input["page_design_block"] or "")
    template_vars = dict(page_input.get("template_vars") or {})
    slide_plan = str(page_input.get("slide_plan") or "").strip()
    slide_context = str(page_input.get("slide_context") or "").strip()
    previous_layout_style = str(page_input.get("previous_layout_style") or "").strip()
    is_structural_page = bool(page_input["is_structural_page"])

    svg_output_dir = project_dir / "svg_output"
    svg_output_dir.mkdir(parents=True, exist_ok=True)
    repair_archive_dir = project_dir / "svg_archive" / "repair"

    figure_guidance = _figure_guidance_block(
        used_figures,
        rejected_figures,
        source=figure_source,
    )
    icon_assignment = _icon_from_design_spec_for_page(
        page_design_block,
        page_num,
    ) or _icon_from_design_spec_for_page(base_context, page_num)
    required_icon = str(icon_assignment["name"]) if icon_assignment is not None else None
    icon_guidance = _icon_guidance_block(icon_assignment)
    img_layout = _figure_layout_guidance(used_figures)
    page_role_guidance = _page_role_guidance(page_type)
    factual_guardrails = _factual_rendering_guardrails(page_type)
    layout_guardrails = _layout_generation_guardrails(
        page_type,
        has_paper_figure=bool(used_figures),
    )
    structural_figure_policy = (
        "\n- Structural page paper-figure policy: do not place extracted "
        "paper figures (`../sources/images/fig_*.png`) on cover, chapter, "
        "TOC, or ending pages. Use native SVG decoration or template images "
        "instead.\n"
        if is_structural_page
        else ""
    )

    skeleton_block = ""
    if template_skeletons:
        skeleton_svg = template_skeletons.get(page_type)
        if skeleton_svg:
            skeleton_svg = _prepare_template_skeleton(
                page_type,
                skeleton_svg,
                template_vars,
            )
            if skeleton_svg:
                skeleton_block = (
                    _template_skeleton_instruction(page_type)
                    +
                    f"```svg\n{skeleton_svg}\n```"
                )

    slide_plan_section = (
        f"\n\n## Current Structured Slide Plan\n\n{slide_plan}"
        if slide_plan
        else ""
    )
    slide_context_section = (
        f"\n\n{slide_context}"
        if slide_context
        else ""
    )
    page_design_section = (
        f"\n\n## Page Design Contract\n\n{page_design_block}"
        if page_design_block
        else ""
    )
    text_layout = _dynamic_text_layout_block(
        rewritten_content,
        page_design_block,
        has_paper_figure=bool(used_figures),
    )
    previous_layout_section = (
        "\n\n## Previous Page Layout Continuity (same chapter)\n\n"
        f"{previous_layout_style}\n"
        "If the previous page type differs from the current page type, borrow only "
        "its typography, spacing rhythm, and visual vocabulary; keep the current "
        "page's structure and content contract."
        if previous_layout_style
        else ""
    )
    extra_block = f"\n\n{extra_instruction}" if extra_instruction else ""
    conversation: list[LLMMessage] = [
        LLMMessage.system(system_prompt),
        LLMMessage.user(
            f"## Parallel Generation Global Design Contract\n\n{base_context}\n\n"
            f"## SVG Technical Standards\n\n{standards}\n\n"
            f"## Fixed Runtime Configuration\n\n"
            f"- Selected style preset: {style}\n"
            f"- Selected language: {language}\n"
            f"- Generate page {page_num}/{total_pages} independently, but match the global design contract exactly.\n"
            f"- All visible SVG text must follow the selected language unless a proper noun must stay in its original form.\n"
            f"- Use the Typography System from the global design contract as the single source of truth for fonts and size hierarchy."
            f"{extra_block}"
            f"{slide_plan_section}"
            f"{slide_context_section}"
            f"{page_design_section}\n\n"
            f"{previous_layout_section}\n\n"
            f"## Page {page_num}/{total_pages}: {page_name}\n\n"
            f"{rewritten_content}\n\n"
            f"## Runtime Reminders\n"
            f"- Style preset: {style}\n"
            f"- Language: {language}\n"
            f"- Page type: {page_type}. Use this type only for template selection; do not render metadata comments.\n"
            f"- Keep all visible text in the requested language.\n"
            f"{page_role_guidance}\n"
            f"{factual_guardrails}"
            f"{layout_guardrails}"
            f"{text_layout}\n"
            f"{structural_figure_policy}\n\n"
            f"{figure_guidance}\n\n"
            f"{icon_guidance}\n\n"
            f"{img_layout}\n\n"
            f"## Output Complexity Budget\n"
            f"- Keep the SVG compact and editable; target under {SVG_GENERATION_MAX_TOKENS} output tokens.\n"
            f"- Prefer grouped primitives, reusable styles, and concise text. Avoid huge decorative path dumps or duplicate off-canvas elements.\n\n"
            f"## Layout Budget\n"
            f"- Follow the design specification's canvas, content boundaries, and footer clearance.\n"
            f"- Resolve fit through composition, region sizes, and concise wording while preserving substantive content.\n\n"
            f"Generate the complete SVG code for this page. "
            f"Output ONLY the SVG code, wrapped in ```svg code block."
            f"{skeleton_block}"
        ),
    ]

    try:
        debug_dir = project_dir / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = debug_dir / f"parallel_page_{page_num:02d}_prompt.md"
        prompt_file.write_text(
            "\n\n".join(f"--- ROLE: {msg.role} ---\n\n{msg.content}" for msg in conversation),
            encoding="utf-8",
        )
    except OSError:
        pass

    generation_max_tokens = SVG_GENERATION_MAX_TOKENS
    response, generation_max_tokens = await _chat_svg_generation(
        llm,
        conversation,
        model,
        temperature=0.3,
        max_tokens=generation_max_tokens,
        project_dir=project_dir,
        debug_prefix="parallel",
        page_num=page_num,
        attempt=1,
    )

    svg_content = _extract_svg(response.content)
    for extraction_attempt in range(2, MAX_SVG_EXTRACTION_ATTEMPTS + 1):
        if svg_content:
            break
        if (
            _completion_near_limit(response, generation_max_tokens)
            and _svg_response_looks_truncated(response.content)
        ):
            generation_max_tokens = _next_higher_svg_max_tokens(generation_max_tokens)
        conversation.append(LLMMessage.assistant(response.content))
        conversation.append(
            LLMMessage.user(
                _build_svg_extraction_retry_prompt(
                    page_num=page_num,
                    total_pages=total_pages,
                    page_name=page_name,
                    page_content=str(page_input["page_content"]),
                    attempt=extraction_attempt,
                )
            )
        )
        response, generation_max_tokens = await _chat_svg_generation(
            llm,
            conversation,
            model,
            temperature=0.2,
            max_tokens=generation_max_tokens,
            project_dir=project_dir,
            debug_prefix="parallel",
            page_num=page_num,
            attempt=extraction_attempt,
        )
        svg_content = _extract_svg(response.content)

    if not svg_content:
        conversation.append(LLMMessage.assistant(response.content))
        failure_message = (
            f"Failed to generate parseable SVG for page {page_num}/{total_pages} "
            f"({page_name}) after {MAX_SVG_EXTRACTION_ATTEMPTS} attempts"
        )
        raise RuntimeError(failure_message)

    svg_content = _normalize_obvious_short_text_lines(svg_content, page_design_block)
    conversation.append(LLMMessage.assistant(f"```svg\n{svg_content}\n```"))

    best_svg = svg_content
    visual_attempts = 0
    critic_attempt = 1
    max_critic_attempts = max(0, int(max_critic_attempts))
    visual_qa_max_attempts = max(0, int(visual_qa_max_attempts or 0))
    static_attempt_limit = _static_attempt_limit_for_report(
        max_critic_attempts,
        None,
    )

    while True:
        report: CriticReport | None = None
        report_metadata: CriticMetadata = {"source": "static"}
        event_attempt = critic_attempt

        if static_attempt_limit > 0:
            report = _generation_static_report(
                svg_content,
                critic_config=critic_config,
                allowed_figures=used_figures,
                used_paper_figures={},
                required_icon=required_icon,
                include_general_svg_checks=max_critic_attempts > 0,
                page_content=str(page_input["page_content"]),
            )
            static_attempt_limit = _static_attempt_limit_for_report(
                max_critic_attempts,
                report,
            )
            if report.passed and max_critic_attempts <= 0:
                report = None
            if report is not None and not report.passed and critic_attempt > static_attempt_limit:
                archive_rel = _archive_repair_svg(
                    repair_archive_dir,
                    page_num=page_num,
                    attempt=event_attempt,
                    label="unrepaired",
                    svg_content=svg_content,
                )
                if archive_rel:
                    report_metadata = {
                        **report_metadata,
                        "before_archive_path": archive_rel,
                    }
                await _emit_critic(
                    on_critic,
                    page_num,
                    event_attempt,
                    report,
                    None,
                    archive_rel,
                    report_metadata,
                )
                best_svg = svg_content
                break

        if report is None or report.passed:
            if report is not None:
                await _emit_critic(
                    on_critic,
                    page_num,
                    event_attempt,
                    report,
                    None,
                    None,
                    report_metadata,
                )
            if not enable_visual_critic or visual_attempts >= visual_qa_max_attempts:
                best_svg = svg_content
                break
            visual_attempts += 1
            snapshot = set_usage_context(
                stage="visual_qa",
                page=page_num,
                attempt=visual_attempts,
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
            visual_metadata = _visual_outcome_metadata(visual_outcome)
            event_attempt = visual_attempts
            if visual_outcome.report.passed:
                await _emit_critic(
                    on_critic,
                    page_num,
                    event_attempt,
                    visual_outcome.report,
                    None,
                    None,
                    visual_metadata,
                )
                best_svg = svg_content
                break
            if not visual_outcome.rendered:
                best_svg = svg_content
                break
            report = visual_outcome.report
            report_metadata = visual_metadata

        if report is None:
            best_svg = svg_content
            break

        before_archive_rel = _archive_repair_svg(
            repair_archive_dir,
            page_num=page_num,
            attempt=event_attempt,
            label="before",
            svg_content=svg_content,
        )
        repair_prompt_text = (
            report.to_prompt_block()
            + "\n\nWhen fixing paper-figure violations, resize the image "
            "to the visible frame, preserve the source aspect ratio, keep "
            "the full rendered image inside x=0..1260 and y=80..640, and "
            "remove over-cropping clip paths. Do not swap to an unlisted "
            "paper figure href.\n\nReturn the complete corrected SVG only, wrapped in a ```svg code block."
        )
        conversation.append(LLMMessage.user(repair_prompt_text))
        repair_temp = max(0.1, 0.3 - 0.1 * event_attempt)
        snapshot = set_usage_context(stage="repair", page=page_num, attempt=event_attempt)
        try:
            response = await llm.chat(
                conversation,
                model,
                temperature=repair_temp,
                max_tokens=SVG_REPAIR_MAX_TOKENS,
            )
        finally:
            reset_usage_context(snapshot)

        repaired = _extract_svg(response.content)
        if repaired:
            after_archive_rel = _archive_repair_svg(
                repair_archive_dir,
                page_num=page_num,
                attempt=event_attempt,
                label="after",
                svg_content=repaired,
            )
            event_metadata: CriticMetadata = dict(report_metadata)
            if before_archive_rel:
                event_metadata["before_archive_path"] = before_archive_rel
            if after_archive_rel:
                event_metadata["after_archive_path"] = after_archive_rel
            await _emit_critic(
                on_critic,
                page_num,
                event_attempt,
                report,
                repair_prompt_text,
                before_archive_rel,
                event_metadata,
            )
            svg_content = repaired
            best_svg = repaired
            conversation.append(LLMMessage.assistant(f"```svg\n{repaired}\n```"))
            if on_svg_update is not None:
                await on_svg_update(page_num, repaired)
            if report_metadata.get("source") == "static":
                critic_attempt += 1
        else:
            event_metadata = dict(report_metadata)
            if before_archive_rel:
                event_metadata["before_archive_path"] = before_archive_rel
            await _emit_critic(
                on_critic,
                page_num,
                event_attempt,
                report,
                repair_prompt_text,
                before_archive_rel,
                event_metadata,
            )
            conversation.append(LLMMessage.assistant(response.content))
            break

    _write_generated_svg(svg_output_dir, page_num, best_svg)
    return page_num, best_svg


def _chapter_parallel_groups(page_inputs: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current: list[dict] = []
    standalone_types = {"cover", "toc", "ending"}
    for item in page_inputs:
        page_type = item["page_type"]
        if page_type in standalone_types:
            if current:
                groups.append(current)
                current = []
            groups.append([item])
        elif page_type == "chapter":
            if current:
                groups.append(current)
            current = [item]
        elif current:
            current.append(item)
        else:
            current = [item]
    if current:
        groups.append(current)
    return groups


async def generate_svg_pages_parallel(
    design_spec: str,
    manuscript: str,
    project_dir: Path,
    llm: LLMProvider,
    model: str,
    *,
    mode: str = "chapter_parallel",
    parallel_concurrency: int = 3,
    style: str = "academic",
    language: str = "en",
    detail_level: str = "normal",
    extra_instruction: str = "",
    target_pages: set[int] | None = None,
    critic_config: CriticConfig | None = None,
    on_critic: CriticCallback | None = None,
    on_svg_update: SvgUpdateCallback | None = None,
    figure_inventory: list[dict] | None = None,
    slide_contexts: dict[int, str] | None = None,
    enable_visual_critic: bool = False,
    max_critic_attempts: int = DEFAULT_MAX_CRITIC_ATTEMPTS,
    visual_qa_max_attempts: int = 1,
    visual_critic_config: VisualCriticConfig | None = None,
    template_context: str | None = None,
    template_skeletons: dict[str, str] | None = None,
) -> AsyncIterator[tuple[int, str]]:
    """Generate SVG pages with independent page or chapter parallelism."""
    standards_path = settings.references_dir / "shared-standards-essential.md"
    if not standards_path.exists():
        standards_path = settings.references_dir / "shared-standards.md"
    standards = standards_path.read_text(encoding="utf-8") if standards_path.exists() else ""

    pages = split_manuscript_pages(manuscript)
    template_vars = _template_vars_by_page(pages)
    deck_plan = build_deck_plan(
        manuscript,
        design_spec,
        detail_level=detail_level,
    )
    deck_plan_block = deck_plan_markdown(deck_plan)
    generation_context_block = deck_generation_context_markdown(deck_plan)
    try:
        (project_dir / "deck_plan.md").write_text(deck_plan_block, encoding="utf-8")
    except OSError:
        pass
    base_context = _parallel_global_design_context(design_spec)
    base_context = (
        f"{base_context}\n\n## Deck Generation Context\n\n"
        f"{generation_context_block}"
    )
    if template_context:
        base_context = f"{base_context}\n\n## Template Reference\n\n{template_context}"
    try:
        (project_dir / "parallel_executor_context.md").write_text(
            _build_parallel_context_document(
                design_spec=design_spec,
                pages=pages,
                style=style,
                language=language,
                mode=mode,
                deck_plan_block=generation_context_block,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass

    claimed_paper_figures: set[str] = set()
    page_inputs: list[dict] = []
    for i, page_content in enumerate(pages):
        page_num = i + 1
        if target_pages is not None and page_num not in target_pages:
            continue
        page_inputs.append(
            _prepare_parallel_page_inputs(
                page_num=page_num,
                total_pages=len(pages),
                page_content=page_content,
                design_spec=design_spec,
                figure_inventory=figure_inventory,
                claimed_paper_figures=claimed_paper_figures,
                template_vars=template_vars.get(page_num),
                slide_plan=slide_plan_markdown(deck_plan, page_num),
                slide_context=(slide_contexts or {}).get(page_num, ""),
            )
        )

    concurrency = max(1, min(int(parallel_concurrency or 1), 8, len(page_inputs) or 1))
    queue: asyncio.Queue[tuple[int, str] | BaseException] = asyncio.Queue()
    semaphore = asyncio.Semaphore(concurrency)

    async def _run_one(item: dict) -> None:
        async with semaphore:
            try:
                queue.put_nowait(
                    await _generate_parallel_page(
                        item,
                        base_context=base_context,
                        standards=standards,
                        project_dir=project_dir,
                        llm=llm,
                        model=model,
                        style=style,
                        language=language,
                        detail_level=detail_level,
                        extra_instruction=extra_instruction,
                        critic_config=critic_config,
                        on_critic=on_critic,
                        on_svg_update=on_svg_update,
                        enable_visual_critic=enable_visual_critic,
                        max_critic_attempts=max_critic_attempts,
                        visual_qa_max_attempts=visual_qa_max_attempts,
                        visual_critic_config=visual_critic_config,
                        template_skeletons=template_skeletons,
                    )
                )
            except BaseException as exc:
                queue.put_nowait(exc)

    async def _run_group(group: list[dict]) -> None:
        previous_layout_style = ""
        for item in group:
            try:
                page_input = (
                    {**item, "previous_layout_style": previous_layout_style}
                    if previous_layout_style
                    else item
                )
                page_num, svg_content = await _generate_parallel_page(
                    page_input,
                    base_context=base_context,
                    standards=standards,
                    project_dir=project_dir,
                    llm=llm,
                    model=model,
                    style=style,
                    language=language,
                    detail_level=detail_level,
                    extra_instruction=extra_instruction,
                    critic_config=critic_config,
                    on_critic=on_critic,
                    on_svg_update=on_svg_update,
                    enable_visual_critic=enable_visual_critic,
                    max_critic_attempts=max_critic_attempts,
                    visual_qa_max_attempts=visual_qa_max_attempts,
                    visual_critic_config=visual_critic_config,
                    template_skeletons=template_skeletons,
                )
                queue.put_nowait(
                    (page_num, svg_content)
                )
                previous_layout_style = _svg_layout_style_brief(
                    svg_content,
                    page_num=page_num,
                    page_type=str(item["page_type"]),
                )
            except BaseException as exc:
                queue.put_nowait(exc)
                return

    if mode == "page_parallel":
        tasks = [asyncio.create_task(_run_one(item)) for item in page_inputs]
    else:
        tasks = [
            asyncio.create_task(_run_group(group))
            for group in _chapter_parallel_groups(page_inputs)
        ]

    remaining = len(page_inputs)
    try:
        while remaining:
            item = await queue.get()
            if isinstance(item, BaseException):
                for task in tasks:
                    task.cancel()
                raise item
            remaining -= 1
            yield item
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()


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
        "- Start from a clean SVG document instead of continuing or patching the previous response; it may have been truncated.\n"
        "- Do not spend tokens on hidden reasoning, analysis, explanations, or empty output. Emit visible SVG code immediately.\n"
        "- Preserve the page content below; do not invent a different slide.\n"
        "- Keep the SVG concise: prefer native text/rect/path groups, avoid excessive decorative paths, and stay well below the token limit.\n"
        "- Return one complete SVG document, wrapped in a ```svg code block.\n"
        "- The SVG must start with `<svg` and end with `</svg>`.\n\n"
        f"## Page Content To Render\n\n{page_content}\n\n"
        f"## Retry Attempt\n\n{attempt}"
    )
