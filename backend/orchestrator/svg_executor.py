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
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from html import escape as html_escape
from inspect import Parameter, signature
from pathlib import Path

from PIL import Image

from backend.config import settings
from backend.generator.svg_critic import CriticConfig, CriticReport, Violation, check_svg
from backend.generator.visual_critic import VisualCriticConfig, visual_check
from backend.llm import LLMMessage, LLMProvider, LLMResponse
from backend.orchestrator.deck_plan import (
    build_deck_plan,
    deck_plan_markdown,
    slide_plan_markdown,
)
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
TEXT_BLOCK_RE = re.compile(r"<text\b(?P<attrs>[^>]*)>(?P<body>.*?)</text>", re.IGNORECASE | re.DOTALL)
TEXT_X_ATTR_RE = re.compile(r'\bx=(["\'])(?P<value>[^"\']+)\1', re.IGNORECASE)
TEXT_ANCHOR_ATTR_RE = re.compile(r"\btext-anchor\s*=", re.IGNORECASE)
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

PROMPT_PATH = Path(__file__).parent / "prompts" / "executor.md"

# Default number of static critic checks per page, including the first
# post-generation check. 0 disables review entirely.
DEFAULT_MAX_CRITIC_ATTEMPTS = 0

# Max prior page exchanges kept in the conversation (sliding window).
# Each page generates 1 user + 1 assistant exchange plus bounded repair rounds.
# Keeping 2 pages of context balances style consistency vs. token cost.
MAX_PRIOR_PAGES_IN_CONTEXT = 2

# Initial response plus bounded same-page retries when no SVG can be extracted.
MAX_SVG_EXTRACTION_ATTEMPTS = 3
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
    if page_type in STRUCTURAL_PAGE_TYPES:
        guidance = (
            "- Page role boundary: render this as a minimal structural page. "
            "Use the title, an optional short subtitle/key phrase, and sparse "
            "native SVG decoration only. Do not add paper figures, charts, "
            "multi-column evidence blocks, or detailed bullet grids.\n"
            "- Chrome rule: if page numbering is visible, place it in the "
            "footer only. Do not add top-left section/chapter/page counter "
            "labels."
        )
        if page_type == "chapter":
            guidance += (
                "\n- Chapter consistency: reuse the same chapter-divider layout "
                "for every chapter page in this deck; only change the chapter "
                "number, title, and optional subtitle/key phrase."
            )
        return guidance
    return (
        "- Page role boundary: render this as a content page. Do not use a "
        "chapter-divider layout, oversized chapter numerals, or transition "
        "slide treatment.\n"
        "- Chrome rule: if page numbering is visible, place it in the footer "
        "only. Do not add top-left section/chapter/page counter labels."
    )


def _structural_page_override_block(page_type: str) -> str:
    if page_type not in STRUCTURAL_PAGE_TYPES:
        return ""
    if page_type == "cover":
        return (
            "\n## Structural Page Override\n"
            "- This override is stronger than any design-spec content-elements list for this page.\n"
            "- Render the cover as a lightweight title/meta slide: title, optional subtitle, "
            "available authors/source/venue/date, a few short context/thesis lines, "
            "template chrome, and sparse background decoration.\n"
            "- Do not render extracted paper figures or turn the cover into a detailed "
            "background, contribution, metric, result, or evidence slide.\n"
        )
    consistency = (
        "\n- For chapter pages, keep the same divider layout across all chapter "
        "slides. Vary only the section number, title, and optional subtitle/key phrase."
        if page_type == "chapter"
        else ""
    )
    return (
        "\n## Structural Page Override\n"
        "- This override is stronger than any manuscript body text or design-spec "
        "content-elements list for this page.\n"
        "- Render only the structural role: title, optional short subtitle/key phrase, "
        "template chrome, and sparse background decoration.\n"
        "- Do not render content sections named `核心问题`, `本章看点`, `主要结果`, "
        "question lists, KPI strips, cards/panels, figures, charts, or mini-agendas "
        "on chapter/ending pages."
        f"{consistency}\n"
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
    """For each paper figure, recommend layout based on its aspect ratio.

    When multiple images share a page, compute a height budget so the total
    stays within the content area.
    """
    if not used_figures:
        return ""
    ca_y = _DEFAULT_CONTENT_AREA["y"]
    ca_w = _DEFAULT_CONTENT_AREA["width"]
    ca_h = _DEFAULT_CONTENT_AREA["height"]
    ca_bottom = ca_y + ca_h

    lines = ["## Image Layout Recommendations"]

    # Collect image metadata
    fig_meta: list[dict] = []
    for fig in used_figures:
        path = fig.get("path") or ""
        meta: dict = {"stem": Path(path).stem if path else "unknown"}
        try:
            img_path = Path(path)
            if img_path.exists():
                with Image.open(img_path) as img:
                    w, h = img.size
                    meta["w"], meta["h"], meta["ratio"] = w, h, w / h
        except Exception:
            pass
        fig_meta.append(meta)

    # Single image: original logic, capped to content area
    if len(fig_meta) == 1:
        m = fig_meta[0]
        r = m.get("ratio")
        if r is not None:
            img_h_at_full_w = int(ca_w / r)
            if r > 1.2 and img_h_at_full_w <= ca_h:
                # Top-bottom: image fits at full width
                img_w, img_h = ca_w, img_h_at_full_w
                txt_h = ca_h - img_h - 20
                lines.append(
                    f"- {m['stem']}: ratio={r:.2f}, "
                    f"recommended=top-bottom, "
                    f"image={img_w}x{img_h}, text_area={ca_w}x{txt_h}"
                )
            else:
                # Left-right: either ratio <= 1.2, or image too tall at full width
                img_h, img_w = ca_h, int(ca_h * r)
                txt_w = ca_w - img_w - 20
                # If text area too narrow (<280px), cap image to 70% height
                if txt_w < 280:
                    img_h = int(ca_h * 0.7)
                    img_w = int(img_h * r)
                    txt_w = ca_w - img_w - 20
                if r > 1.2:
                    lines.append(
                        f"- {m['stem']}: ratio={r:.2f}, "
                        f"recommended=left-right (image too tall for top-bottom), "
                        f"image={img_w}x{img_h}, text_area={txt_w}x{ca_h}"
                    )
                else:
                    lines.append(
                        f"- {m['stem']}: ratio={r:.2f}, "
                        f"recommended=left-right, "
                        f"image={img_w}x{img_h}, text_area={txt_w}x{ca_h}"
                    )
        else:
            lines.append(f"- {m['stem']}: unable to read dimensions")

    # Multiple images: compute shared height budget
    else:
        n = len(fig_meta)
        text_reserve = 150
        available = ca_h - text_reserve - 20 * n
        per_img_max = max(80, available // n) if n > 0 else ca_h

        lines.append(
            f"Multi-image page ({n} images): "
            f"each image max {per_img_max}px height, "
            f"content area bottom = y{ca_bottom}. "
            f"After images, reserve at least {text_reserve}px for text."
        )

        running_y = ca_y
        for m in fig_meta:
            r = m.get("ratio")
            if r is not None:
                if r > 1.2:
                    img_w = ca_w
                    img_h = min(int(ca_w / r), per_img_max)
                else:
                    img_h = min(ca_h, per_img_max)
                    img_w = int(img_h * r)
                lines.append(
                    f"- {m['stem']}: ratio={r:.2f}, "
                    f"image={img_w}x{img_h}, "
                    f"y_start={running_y}, y_end={running_y + img_h}"
                )
                running_y += img_h + 20
            else:
                lines.append(f"- {m['stem']}: unable to read dimensions")
                running_y += per_img_max + 20

        remaining = ca_bottom - running_y
        lines.append(
            f"Text starts at y={running_y}, "
            f"max height={remaining}px, "
            f"must not exceed y={ca_bottom}"
        )

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
    template_vars = _template_vars_by_page(pages)
    deck_plan = build_deck_plan(
        manuscript,
        design_spec,
        detail_level=detail_level,
    )
    deck_plan_block = deck_plan_markdown(deck_plan)
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
    if is_deepseek_provider(llm, model):
        extra_sections.append(deepseek_executor_guidance(detail_level))
    if template_context:
        extra_sections.append(template_context)
    extra_block = "\n\n" + "\n\n".join(extra_sections) if extra_sections else ""
    conversation: list[LLMMessage] = [
        LLMMessage.system(system_prompt),
        LLMMessage.user(
            f"## Design Specification\n\n{design_spec}\n\n"
            f"## Structured Deck Plan (authoritative)\n\n{deck_plan_block}\n\n"
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
        _max_context_msgs = MAX_PRIOR_PAGES_IN_CONTEXT * max(1, max_critic_attempts) * 2
        _beyond_preamble = len(conversation) - _preamble_len
        if _beyond_preamble > _max_context_msgs:
            _trim = _beyond_preamble - _max_context_msgs
            conversation[:] = conversation[:_preamble_len] + conversation[_preamble_len + _trim:]

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
        page_role_guidance = _page_role_guidance(page_type)
        structural_override = _structural_page_override_block(page_type)
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
                skeleton_svg = _resolve_template_skeleton_placeholders(
                    skeleton_svg,
                    template_vars.get(page_num),
                )
                skeleton_block = (
                    _template_skeleton_instruction(page_type)
                    +
                    f"```svg\n{skeleton_svg}\n```"
                )

        slide_plan_block = slide_plan_markdown(deck_plan, page_num)
        conversation.append(
            LLMMessage.user(
                f"## Current Structured Slide Plan\n\n{slide_plan_block}\n\n"
                f"## Page {page_num}/{len(pages)}: {page_name}\n\n"
                f"{rewritten_content}\n\n"
                f"## Runtime Reminders\n"
                f"- Style preset: {style}\n"
                f"- Language: {language}\n"
                f"- Detail level: {detail_level}\n"
                f"- Page type: {page_type}. Use this type only for template selection; do not render metadata comments.\n"
                f"- Keep all visible text in the requested language.\n"
                f"{page_role_guidance}\n"
                f"{structural_override}"
                f"- {char_budget}"
                f"{structural_figure_policy}\n\n"
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
                report: CriticReport | None = None
                report_metadata: CriticMetadata = {"source": "static"}
                event_attempt = critic_attempt

                if max_critic_attempts > 0:
                    report = _merge_reports(
                        check_svg(svg_content, critic_config),
                        _validate_paper_figure_refs(
                            svg_content,
                            allowed_figures=used_figures,
                            used_paper_figures=used_paper_figures,
                        ),
                        _validate_icon_refs(svg_content, required_icon=required_icon),
                    )
                    report_metadata = {"source": "static"}
                    event_attempt = critic_attempt
                    if not report.passed and critic_attempt >= max_critic_attempts:
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
                    + "\n\nReturn the complete corrected SVG only, "
                    "wrapped in a ```svg code block."
                )
                conversation.append(LLMMessage.user(repair_prompt_text))
                repair_temp = max(0.1, 0.3 - 0.1 * event_attempt)
                snapshot = set_usage_context(
                    stage="repair", page=page_num, attempt=event_attempt
                )
                try:
                    response = await llm.chat(
                        conversation, model, temperature=repair_temp, max_tokens=16384
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


def _extract_design_section(design_spec: str, roman: str) -> str:
    pattern = rf"(?ims)^#+\s*{re.escape(roman)}\.\s+.*?(?=^#+\s*[IVXLCDM]+\.\s+|\Z)"
    match = re.search(pattern, design_spec)
    return match.group(0).strip() if match else ""


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


def _sanitize_structural_page_design_block(
    block: str,
    page_type: str,
) -> str:
    if page_type not in STRUCTURAL_PAGE_TYPES or not block.strip():
        return block

    first_line = next((line.strip() for line in block.splitlines() if line.strip()), "")
    if not first_line.startswith("#"):
        first_line = f"#### Structural {page_type.title()} Page"

    preserved: list[str] = []
    forbidden = (
        re.compile(
            r"(?i)(\[\[FIG:|paper figure|extracted figure|dense article|"
            r"detailed evidence|detailed result|multi-column evidence)"
        )
        if page_type == "cover"
        else re.compile(
            r"(?i)(\[\[FIG:|paper figure|chart|table|metric|kpi|核心问题|本章看点|"
            r"question list|mini-agenda|agenda grid|evidence|bullet grid)"
        )
    )
    allowed_label = re.compile(
        r"(?i)^\s*[-*]\s*(?:\*\*)?"
        r"(page type|type|title|subtitle|layout|layout family|style family|"
        r"visual family|visual treatment|background|composition|typography|"
        r"color|palette|accent|motif|chrome|footer|spacing|template|"
        r"metadata|authors|source|venue|date|context|ornament)"
        r"(?:\*\*)?\s*[:：]"
    )
    for raw_line in block.splitlines()[1:]:
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if forbidden.search(stripped):
            continue
        if allowed_label.search(stripped):
            preserved.append(line)

    if page_type == "chapter":
        layout = (
            "Consistent minimal chapter divider; same layout across all chapter pages; "
            "only chapter number, title, and optional subtitle/key phrase may change."
        )
    elif page_type == "cover":
        layout = (
            "Lightweight cover page; title, optional subtitle, available metadata, "
            "and a modest context/accent treatment."
        )
    else:
        layout = f"Minimal structural {page_type} page; title plus optional short subtitle/key phrase only."
    lines = [
        first_line,
        "",
        f"- **Page Type**: {page_type}",
        f"- **Structural Role**: {layout}",
    ]
    if preserved:
        lines.extend(["", "### Preserved Planner Style Fields", *preserved])
    lines.extend(
        [
            "",
            (
                "- **Content Boundary**: title, optional subtitle, paper metadata, and "
                "a few short context/thesis lines. No extracted paper figures or dense "
                "article-content sections."
                if page_type == "cover"
                else "- **Content Boundary**: title and at most one short subtitle/key phrase. "
                "No cards, KPI strips, question lists, mini-agendas, paper figures, charts, "
                "or labeled blocks such as `核心问题` / `本章看点`."
            ),
        ]
    )
    return "\n".join(lines)


def _template_vars_by_page(pages: list[str]) -> dict[int, dict[str, str]]:
    result: dict[int, dict[str, str]] = {}
    chapter_index = 0
    current_chapter = {"num": "", "title": "", "desc": ""}
    toc_items: list[str] = []
    fallback_items: list[str] = []

    for page in pages:
        page_type = extract_page_type(page)
        title, _subtitle = _extract_page_title_parts(page)
        if not title:
            continue
        if page_type == "chapter":
            toc_items.append(title)
        elif page_type not in {"cover", "toc", "ending"}:
            fallback_items.append(title)
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
            "GROUP": "",
            "BRAND_LABEL": "",
            "COVER_QUOTE": "",
            "ENDING_TITLE": title,
            "ENDING_MESSAGE": subtitle,
        }
        for idx in range(5):
            variables[f"TOC_ITEM_{idx + 1}"] = toc_items[idx] if idx < len(toc_items) else ""
        if page_type == "cover":
            variables.update(_extract_cover_metadata(page, title, subtitle))
        result[page_num] = variables
    return result


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
    return (
        f"\n\n## Template Skeleton ({page_type} page)\n"
        "Use this SVG as your already data-bound starting point. If any "
        "placeholder token remains, replace it with the current slide plan value. "
        "Preserve ALL decorative elements, gradients, structural chrome, and local "
        "`assets/...` image hrefs. Do NOT invent image paths or swap template "
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
        _extract_design_section(design_spec, roman)
        for roman in ("I", "II", "III", "IV", "V", "VI")
    ]
    compact = "\n\n".join(section for section in sections if section)
    return compact or design_spec[:12000]


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
    detail_level: str,
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
        f"- Language: {language}\n"
        f"- Detail level: {detail_level}\n\n"
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
    if is_structural_page:
        page_design_block = _sanitize_structural_page_design_block(
            page_design_block,
            page_type,
        )

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
    char_budget = _char_budget_block(rewritten_content)
    img_layout = _figure_layout_guidance(used_figures)
    page_role_guidance = _page_role_guidance(page_type)
    structural_override = _structural_page_override_block(page_type)
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
            skeleton_svg = _resolve_template_skeleton_placeholders(
                skeleton_svg,
                template_vars,
            )
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
    page_design_section = (
        f"\n\n## Page Design Contract\n\n{page_design_block}"
        if page_design_block
        else ""
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
            f"- Selected detail level: {detail_level}\n"
            f"- Generate page {page_num}/{total_pages} independently, but match the global design contract exactly.\n"
            f"- All visible SVG text must follow the selected language unless a proper noun must stay in its original form.\n"
            f"- Use the Typography System from the global design contract as the single source of truth for fonts and size hierarchy."
            f"{extra_block}"
            f"{slide_plan_section}"
            f"{page_design_section}\n\n"
            f"{previous_layout_section}\n\n"
            f"## Page {page_num}/{total_pages}: {page_name}\n\n"
            f"{rewritten_content}\n\n"
            f"## Runtime Reminders\n"
            f"- Style preset: {style}\n"
            f"- Language: {language}\n"
            f"- Detail level: {detail_level}\n"
            f"- Page type: {page_type}. Use this type only for template selection; do not render metadata comments.\n"
            f"- Keep all visible text in the requested language.\n"
            f"{page_role_guidance}\n"
            f"{structural_override}"
            f"- {char_budget}"
            f"{structural_figure_policy}\n\n"
            f"{figure_guidance}\n\n"
            f"{icon_guidance}\n\n"
            f"{img_layout}\n\n"
            f"## Pre-Generation Boundary Check\n"
            f"Before writing SVG, mentally plan y-coordinates top to bottom:\n"
            f"- Content area: y=100 to y=620 (520px). This is a hard limit.\n"
            f"- Track cumulative height as you place each element.\n"
            f"- If total exceeds 520px: shrink images, reduce font size, or cut content.\n"
            f"- Footer (y=660+) is reserved for page number only -- no content there.\n\n"
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

    snapshot = set_usage_context(stage="generation", page=page_num, attempt=1)
    try:
        response: LLMResponse = await llm.chat(
            conversation,
            model,
            temperature=0.3,
            max_tokens=16384,
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
                    total_pages=total_pages,
                    page_name=page_name,
                    page_content=str(page_input["page_content"]),
                    attempt=extraction_attempt,
                )
            )
        )
        snapshot = set_usage_context(
            stage="generation",
            page=page_num,
            attempt=extraction_attempt,
        )
        try:
            response = await llm.chat(
                conversation,
                model,
                temperature=0.2,
                max_tokens=16384,
            )
        finally:
            reset_usage_context(snapshot)
        svg_content = _extract_svg(response.content)

    if not svg_content:
        conversation.append(LLMMessage.assistant(response.content))
        raise RuntimeError(
            f"Failed to generate parseable SVG for page {page_num}/{total_pages} "
            f"({page_name}) after {MAX_SVG_EXTRACTION_ATTEMPTS} attempts"
        )

    conversation.append(LLMMessage.assistant(f"```svg\n{svg_content}\n```"))

    best_svg = svg_content
    visual_attempts = 0
    critic_attempt = 1
    max_critic_attempts = max(0, int(max_critic_attempts))
    visual_qa_max_attempts = max(0, int(visual_qa_max_attempts or 0))

    while True:
        report: CriticReport | None = None
        report_metadata: CriticMetadata = {"source": "static"}
        event_attempt = critic_attempt

        if max_critic_attempts > 0:
            report = _merge_reports(
                check_svg(svg_content, critic_config),
                _validate_paper_figure_refs(
                    svg_content,
                    allowed_figures=used_figures,
                    used_paper_figures={},
                ),
                _validate_icon_refs(svg_content, required_icon=required_icon),
            )
            if not report.passed and critic_attempt >= max_critic_attempts:
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
            + "\n\nReturn the complete corrected SVG only, wrapped in a ```svg code block."
        )
        conversation.append(LLMMessage.user(repair_prompt_text))
        repair_temp = max(0.1, 0.3 - 0.1 * event_attempt)
        snapshot = set_usage_context(stage="repair", page=page_num, attempt=event_attempt)
        try:
            response = await llm.chat(
                conversation,
                model,
                temperature=repair_temp,
                max_tokens=16384,
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

    svg_path = svg_output_dir / f"{page_num:02d}_{page_name}.svg"
    svg_path.write_text(best_svg, encoding="utf-8")
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
    try:
        (project_dir / "deck_plan.md").write_text(deck_plan_block, encoding="utf-8")
    except OSError:
        pass
    base_context = _parallel_global_design_context(design_spec)
    base_context = f"{base_context}\n\n## Structured Deck Plan (authoritative)\n\n{deck_plan_block}"
    if template_context:
        base_context = f"{base_context}\n\n## Template Reference\n\n{template_context}"
    if is_deepseek_provider(llm, model):
        base_context = f"{base_context}\n\n{deepseek_executor_guidance(detail_level)}"

    try:
        (project_dir / "parallel_executor_context.md").write_text(
            _build_parallel_context_document(
                design_spec=design_spec,
                pages=pages,
                style=style,
                language=language,
                detail_level=detail_level,
                mode=mode,
                deck_plan_block=deck_plan_block,
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
        "- Preserve the page content below; do not invent a different slide.\n"
        "- Return one complete SVG document, wrapped in a ```svg code block.\n"
        "- The SVG must start with `<svg` and end with `</svg>`.\n\n"
        f"## Page Content To Render\n\n{page_content}\n\n"
        f"## Retry Attempt\n\n{attempt}"
    )
