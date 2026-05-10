"""Strategist agent: produces a design specification from a manuscript."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from backend.config import CANVAS_FORMATS, DESIGN_STYLES, settings
from backend.generator.icon_index import get_icon_index
from backend.llm import LLMMessage, LLMProvider, LLMResponse
from backend.orchestrator.manuscript import (
    count_manuscript_pages,
    format_page_inventory,
    page_inventory,
)
from backend.orchestrator.provider_guidance import (
    deepseek_strategy_guidance,
    is_deepseek_provider,
)

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "strategist.md"
DESIGN_SPEC_MAX_TOKENS = 24576
MAX_DESIGN_SPEC_ATTEMPTS = 2

# Number of icon candidates to pre-select via RAG
ICON_CANDIDATE_COUNT = 30
# Number of search queries to extract from manuscript
ICON_QUERY_COUNT = 8
OFFLINE_ICON_CANDIDATE_COUNT = 28

OFFLINE_ICON_PALETTE: list[tuple[str, str, list[str]]] = [
    ("warning / failure mode", "occlusion risk, noisy feature, error propagation, invalid assumption", ["alert-triangle", "circle-exclamation", "alert-circle", "ban", "bug"]),
    ("insight / key idea", "turning point, core thesis, design intuition, important takeaway", ["lightbulb", "sparkles", "bulb", "brain"]),
    ("framework / architecture", "model block, stacked decoder, module composition, hierarchy", ["puzzle", "component", "cube", "layers", "stack", "binary-tree"]),
    ("method / process", "pipeline, stage transition, iterative update, tuning or regulation", ["route", "git-branch", "arrow-right", "settings", "cog", "sliders"]),
    ("result / metric", "quantitative result, AP gain, trade-off, target objective", ["chart-bar", "chart-line", "chart-pie", "activity", "target"]),
    ("evidence / experiment", "ablation, table result, experimental support, dataset evidence", ["flask", "microscope", "clipboard", "database"]),
    ("visibility / perception", "visible/occluded evidence, observation, localization, attention", ["eye", "search", "crosshairs"]),
    ("robustness / safety", "reliability, protection from bad updates, gated trust", ["shield", "lock", "key"]),
    ("people / pose", "person, crowd, keypoint, human pose, accessibility", ["users", "user", "accessibility"]),
    ("future / contribution", "implication, next step, contribution summary, closing message", ["rocket", "flag", "trophy", "book", "file-text"]),
]


def _extract_icon_queries(manuscript: str) -> list[str]:
    """Extract semantic queries for icon search from manuscript content.

    Parses page titles and key concepts from the slide-structured manuscript
    to generate targeted icon search queries.
    """
    queries: list[str] = []

    # Extract page titles (## headings)
    for m in re.finditer(r"^##\s+(.+)$", manuscript, re.MULTILINE):
        title = m.group(1).strip()
        # Clean up numbering like "1. " or "Page 1: "
        title = re.sub(r"^\d+[\.\):\s]+", "", title).strip()
        if title and len(title) > 2:
            queries.append(title)

    # Extract bold concepts (**text**)
    for m in re.finditer(r"\*\*([^*]{3,50})\*\*", manuscript):
        concept = m.group(1).strip()
        if concept not in queries:
            queries.append(concept)

    return queries[:ICON_QUERY_COUNT]


async def _retrieve_icon_candidates(
    manuscript: str,
    lib: str,
    gemini_api_key: str | None = None,
) -> str:
    """Retrieve icon candidates for the chosen library using RAG.

    Returns a formatted string listing candidate icons for injection
    into the strategist prompt.  Wrapped in asyncio.to_thread() so that
    the blocking Gemini embedding call does not prevent task cancellation.
    """
    import os

    # Temporarily set GEMINI_API_KEY if provided via frontend config
    old_key = os.environ.get("GEMINI_API_KEY")
    if gemini_api_key:
        os.environ["GEMINI_API_KEY"] = gemini_api_key

    index = get_icon_index()
    if not index.is_available:
        logger.warning("Icon index not available, skipping RAG retrieval")
        return ""

    queries = _extract_icon_queries(manuscript)
    if not queries:
        return ""

    # Collect candidates from all queries — run blocking search in a thread
    # so that asyncio cancellation can interrupt it.
    seen_paths: set[str] = set()
    candidates: list[dict] = []

    for query in queries:
        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(index.search, query, lib=lib, k=5),
                timeout=30,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning("Icon search timed out or was cancelled for query: %s", query)
            break
        for r in results:
            path = str(r.get("path") or "")
            if path in seen_paths:
                continue
            if not _icon_asset_exists(path):
                logger.warning("Skipping stale icon RAG candidate with missing local asset: %s", path)
                continue
            seen_paths.add(path)
            candidates.append(r)

    # Sort by score descending, take top N
    candidates.sort(key=lambda x: x["score"], reverse=True)
    candidates = candidates[:ICON_CANDIDATE_COUNT]

    # Restore original key
    if gemini_api_key:
        if old_key is not None:
            os.environ["GEMINI_API_KEY"] = old_key
        else:
            os.environ.pop("GEMINI_API_KEY", None)

    if not candidates:
        return ""

    # Format as a compact table for prompt injection
    lines = [
        f"\n## Available Icon Candidates ({lib} library, {len(candidates)} icons)",
        "",
        "Semantic-searched from manuscript content. Use only when justified by clear design purpose.",
        "",
        "| # | Icon Path | Category | Tags |",
        "|---|-----------|----------|------|",
    ]

    for i, c in enumerate(candidates, 1):
        tags = ", ".join(c.get("tags", [])[:5])
        cat = c.get("category", "-")
        lines.append(f"| {i} | `{c['path']}` | {cat} | {tags} |")

    lines.append("")
    lines.append(
        "Use the icon path with the `<use data-icon=\"...\"/>` placeholder syntax. "
        "Example: `<use data-icon=\"chart-bar\" x=\"100\" y=\"200\" width=\"32\" height=\"32\" fill=\"#0076A8\"/>`"
    )
    lines.append("")

    return "\n".join(lines)


def _icon_asset_exists(icon_path: str) -> bool:
    if "/" not in icon_path:
        return False
    lib, name = icon_path.split("/", 1)
    return (settings.icons_dir / lib / f"{name}.svg").exists()


def _offline_icon_candidates_block(lib: str) -> str:
    """Provide a compact verified palette when semantic icon RAG is disabled."""
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for semantic_role, use_when, names in OFFLINE_ICON_PALETTE:
        for name in names:
            path = f"{lib}/{name}"
            if path in seen or not _icon_asset_exists(path):
                continue
            seen.add(path)
            rows.append((path, semantic_role, use_when))
            if len(rows) >= OFFLINE_ICON_CANDIDATE_COUNT:
                break
        if len(rows) >= OFFLINE_ICON_CANDIDATE_COUNT:
            break

    if not rows:
        return (
            f"\n## Available Icon Candidates ({lib} library, offline fallback)\n\n"
            "No verified local icons were found for this library. Prefer `Icon: None` "
            "unless a specific local icon path is known to exist.\n"
        )

    lines = [
        f"\n## Available Icon Candidates ({lib} library, offline fallback, {len(rows)} verified icons)",
        "",
        "Semantic RAG is disabled, so use this small verified palette instead of inventing icon names.",
        "Choose icons sparingly; most slides should still use `Icon: None`.",
        "",
        "| # | Icon Path | Role | Use when |",
        "|---|-----------|------|----------|",
    ]
    for i, (path, semantic_role, use_when) in enumerate(rows, 1):
        lines.append(f"| {i} | `{path}` | {semantic_role} | {use_when} |")
    lines.append("")
    lines.append(
        "Use icon paths exactly as shown, with the library prefix, in Section VI and Section IX."
    )
    return "\n".join(lines)


def _icon_usage_policy_block(icon_library: str) -> str:
    return (
        "\n## Icon Usage: ENABLED — restrained semantic mode\n"
        "Icons are enabled, but they must remain sparse and purposeful. "
        "Use icons only when they clarify the slide's structure or meaning; do not add icons merely because the switch is on.\n\n"
        "Rules:\n"
        "- Target only high-value placements: chapter dividers, process steps, KPI/result highlights, warnings/failure modes, limitation cards, or future-direction cards.\n"
        "- Avoid icon use on dense technical/data slides unless the icon labels a clear process step or callout.\n"
        "- Never use icons as ordinary bullet prefixes, repeated decoration, filler, or background texture.\n"
        "- Keep to one icon library for the whole deck and use explicit `<use data-icon=\"...\"/>` placeholders.\n"
        "- In Section VI, list only icons with a concrete justification; leave slides unlisted when no icon is needed.\n"
        "- In Section IX Content Outline, add an `Icon: ` line only for slides that should actually render an icon.\n"
        f"- Selected icon library: `{icon_library}`. Icon paths should include this library prefix, e.g. `{icon_library}/name`.\n"
        "- Choose only icon paths that appear in the candidate table below, unless a local asset with that exact path is explicitly known to exist.\n"
        "\nVisual role separation:\n"
        "- `Icon` means a real library icon only. Use `Icon: None` on ordinary content pages unless the icon has a clear semantic job.\n"
        "- `Card Marker` means structural labeling only: prefer `numbered` or `none`. Do not use single-letter/symbol badges such as `P`, `Δ`, `!`, or `G` as fake icons.\n"
        "- `Micro Visual` means a tiny mechanism diagram, not an icon. Prefer forms such as `distribution-bins`, `residual-arrow`, `error-growth`, `gate-slider`, `stage-flow`, `mini-chart`, or `none`.\n"
        "- For dense technical explanation cards, choose numbered markers or micro visuals over decorative icons.\n"
        "- In Section IX, add `Card Marker:` and/or `Micro Visual:` only when they materially guide the Executor; otherwise omit them or use `none`.\n"
    )


def _extract_design_spec_section(content: str, roman: str) -> str:
    pattern = rf"(?ims)^#+\s*{re.escape(roman)}\.\s+.*?(?=^#+\s*[IVXLCDM]+\.\s+|\Z)"
    match = re.search(pattern, content)
    return match.group(0) if match else ""


def _design_spec_validation_error(
    content: str,
    *,
    expected_page_count: int | None = None,
    expected_inventory: list[dict[str, str | int]] | None = None,
) -> str | None:
    text = content.strip()
    if len(text) < 1200:
        return f"design_spec.md is too short ({len(text)} characters)"

    required = {
        "I": "Project Information",
        "II": "Canvas Specification",
        "III": "Visual Theme",
        "IX": "Content Outline",
        "XI": "Technical Constraints",
    }
    for roman, title in required.items():
        pattern = rf"(?im)^#+\s*{roman}\.\s+.*{re.escape(title)}"
        if not re.search(pattern, text):
            return f"design_spec.md is missing section {roman}. {title}"

    if expected_page_count is not None:
        count_match = re.search(r"(?im)\bPage Count\s*[:：]\s*(\d+)\b", text)
        if count_match and int(count_match.group(1)) != expected_page_count:
            return (
                f"Page Count is {count_match.group(1)}, expected {expected_page_count}"
            )

        outline = _extract_design_spec_section(text, "IX")
        page_nums = [
            int(match.group(1))
            for match in re.finditer(
                r"(?i)\b(?:page|slide)\s*0*(\d+)\b", outline
            )
        ]
        if page_nums:
            if max(page_nums) > expected_page_count:
                return (
                    f"Content Outline references page {max(page_nums)}, "
                    f"expected no more than {expected_page_count}"
                )
            if len(set(page_nums)) != expected_page_count:
                return (
                    f"Content Outline lists {len(set(page_nums))} unique pages, "
                    f"expected {expected_page_count}"
                )

    if expected_inventory:
        outline = _extract_design_spec_section(text, "IX")
        missing_types = []
        for item in expected_inventory:
            page_num = item["page"]
            page_type = str(item["type"])
            page_pattern = rf"(?is)\b(?:page|slide)\s*0*{page_num}\b(.+?)(?=\b(?:page|slide)\s*0*\d+\b|\Z)"
            page_match = re.search(page_pattern, outline)
            if page_match and page_type not in page_match.group(0).lower():
                missing_types.append(f"{page_num}:{page_type}")
        if missing_types:
            return "Content Outline page types do not match manuscript: " + ", ".join(missing_types[:5])

    missing_icons = sorted(
        {
            match.group(1).strip()
            for match in re.finditer(
                r"`((?:chunk|tabler-filled|tabler-outline)/[^`]+)`",
                text,
            )
            if not match.group(1).strip().endswith("/name")
            and not _icon_asset_exists(match.group(1).strip())
        }
    )
    if missing_icons:
        return (
            "Design spec references missing local icon assets: "
            + ", ".join(missing_icons[:8])
            + ". Choose exact paths from the provided icon candidate table, or use Icon: None."
        )
    return None


def _language_constraint(language: str) -> str:
    normalized = language.strip().lower()
    if normalized == "zh":
        return "All slide titles, labels, bullets, and annotations must be in Simplified Chinese except proper nouns."
    if normalized == "en":
        return "All slide titles, labels, bullets, and annotations must be in English."
    if normalized == "bilingual":
        return (
            "Page titles and core bullets may include both Chinese and English, but each line must stay readable and deliberate."
        )
    return (
        f"Treat `{language}` as a literal target-language request and keep all visible slide text fully in {language}, "
        "except proper nouns that must remain in their original form."
    )


async def create_design_spec(
    manuscript: str,
    llm: LLMProvider,
    model: str,
    *,
    canvas_format: str = "ppt169",
    style: str = "academic",
    language: str = "en",
    detail_level: str = "normal",
    icon_library: str = "chunk",
    style_overrides: dict | None = None,
    enable_icon: bool = False,
    enable_icon_rag: bool = False,
    gemini_api_key: str | None = None,
    figure_inventory: list[dict] | None = None,
    debug_dir: Path | None = None,
    template_context: str | None = None,
) -> str:
    """Generate a design specification from a manuscript.

    Args:
        manuscript: Slide-structured manuscript markdown.
        llm: LLM provider instance.
        model: Model ID to use.
        canvas_format: Canvas format key.
        style: Design style key.
        language: Output language.
        detail_level: Requested content depth and density target.
        icon_library: Icon library to use (chunk/tabler-filled/tabler-outline).

    Returns:
        Design specification markdown (design_spec.md content).
    """
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    # Load the design spec reference template
    ref_path = settings.templates_dir / "design_spec_reference.md"
    ref_template = ""
    if ref_path.exists():
        ref_template = ref_path.read_text(encoding="utf-8")

    fmt = CANVAS_FORMATS.get(canvas_format, CANVAS_FORMATS["ppt169"])
    style_info = DESIGN_STYLES.get(style, DESIGN_STYLES["academic"])

    # Count pages in manuscript
    page_count = count_manuscript_pages(manuscript)
    inventory = page_inventory(manuscript)
    inventory_block = format_page_inventory(manuscript)
    enforce_page_types = "<!--" in manuscript and "page_type" in manuscript

    user_parts = [
        f"## Manuscript\n\n{manuscript}",
        f"\n## Manuscript Page Inventory\n\n{inventory_block}",
        f"\n## Canvas Format: {fmt['name']} ({fmt['ratio']}), viewBox: `{fmt['viewbox']}`",
        f"\n## Design Style: {style_info['name']}",
        f"\n## Page Count: {page_count}",
        f"\n## Language: {language}",
        f"\n## Detail Level: {detail_level}",
        f"\n## Pre-resolved Confirmations",
        f"- Canvas Format: {fmt['name']}",
        f"- Page Count: {page_count}",
        f"- Audience: Academic/Research community",
        f"- Style: {style_info['name']}",
    ]

    if template_context:
        # When a template is active, the template's color scheme and
        # typography take precedence.  Only inject the style name for
        # content-strategy guidance (academic vs consulting vs tech),
        # but skip primary/accent color so they don't conflict.
        user_parts.extend([
            "- Color scheme & Typography: defined by the selected template (see Template Reference below)",
            "\n## Hard Constraints",
            "- The template's color scheme, typography, and page structure MUST take precedence over any style defaults.",
            f"- Page contract: exactly {page_count} pages; page N in Section IX must match manuscript page N.",
            "- Do not add, remove, or reorder cover, chapter/transition, content, or ending pages.",
            f"- The visible slide language must be `{language}`.",
            f"- {_language_constraint(language)}",
            "- Detail level `normal` should keep pages concise, `high` should allow moderately denser explanatory content, and `very_high` should accommodate richer explanations and fuller evidence coverage without becoming unreadable.",
        ])
        user_parts.append(f"\n{template_context}")
    else:
        user_parts.extend([
            f"- Primary Color: {style_info['primary']}",
            f"- Accent Color: {style_info['accent']}",
            f"- Typography: Sans-serif (Inter/Arial for body, bold for headings)",
            "\n## Hard Constraints",
            "- Respect the selected design style. Do not silently fall back to a default academic theme when another style is selected.",
            f"- Page contract: exactly {page_count} pages; page N in Section IX must match manuscript page N.",
            "- Do not add, remove, or reorder cover, chapter/transition, content, or ending pages.",
            f"- The visible slide language must be `{language}`.",
            f"- {_language_constraint(language)}",
            "- Detail level `normal` should keep pages concise, `high` should allow moderately denser explanatory content, and `very_high` should accommodate richer explanations and fuller evidence coverage without becoming unreadable.",
        ])

    if is_deepseek_provider(llm, model):
        user_parts.append("\n" + deepseek_strategy_guidance(detail_level))

    # Icon policy: when enabled, Phase 1 skips icon detail — Phase 2 (icon_round) decides
    if not enable_icon:
        user_parts.append(
            "\n## Icon Usage: DISABLED\n"
            "Do NOT use any `<use data-icon=\"...\"/>` elements in any slide. "
            "Use plain SVG shapes (circles, rects, paths) for all visual elements instead."
        )
    else:
        # Phase 1: just tell strategist that icons will be planned separately
        user_parts.append(
            f"\n## Icon Usage: ENABLED (Phase 2)\n"
            f"Icons are enabled but will be planned in a separate phase.\n"
            f"In Section VI, write: `Icon library: {icon_library} — inventory TBD.`\n"
            f"In Section IX, do NOT add `Icon:` lines yet — they will be added later.\n"
            f"Do not use `<use data-icon/>` placeholders in the content outline."
        )

    # Inject actual image dimensions so the design spec has correct ratios
    if figure_inventory:
        fig_lines = ["\n## Available Paper Figures (actual dimensions)"]
        fig_lines.append("")
        fig_lines.append("Use these EXACT dimensions and ratios in Section VIII Image Resource List.")
        fig_lines.append("Do NOT fabricate dimensions — use the actual values below.")
        fig_lines.append("")
        fig_lines.append("| Filename | Actual Dimensions | Ratio | Page | Caption |")
        fig_lines.append("|----------|-------------------|-------|------|---------|")
        for fig in figure_inventory:
            w = fig.get("natural_width", 0)
            h = fig.get("natural_height", 0)
            ratio = fig.get("aspect_ratio", 0)
            page = fig.get("page_number", "?")
            path = fig.get("path", "")
            name = Path(path).stem if path else "?"
            cap = (fig.get("caption") or "")[:60]
            if w and h:
                fig_lines.append(f"| {name} | {w}x{h} | {ratio:.2f} | p{page} | {cap} |")
        fig_lines.append("")
        fig_lines.append(
            "**Important**: When placing these images in SVG, the `width/height` ratio MUST match "
            "the actual ratio above. For example, if actual dimensions are 974x269 (ratio 3.62), "
            "use width=500 height=138 (500/3.62≈138)."
        )
        user_parts.append("\n".join(fig_lines))

    if style_overrides:
        override_lines = ["\n## Style Overrides (must override defaults)"]
        palette = style_overrides.get("palette") if isinstance(style_overrides, dict) else None
        font = style_overrides.get("font") if isinstance(style_overrides, dict) else None
        font_heading = style_overrides.get("font_heading") if isinstance(style_overrides, dict) else None
        font_body = style_overrides.get("font_body") if isinstance(style_overrides, dict) else None
        cjk_heading = style_overrides.get("cjk_heading") if isinstance(style_overrides, dict) else None
        cjk_body = style_overrides.get("cjk_body") if isinstance(style_overrides, dict) else None
        density = style_overrides.get("density") if isinstance(style_overrides, dict) else None
        if palette:
            try:
                colors = ", ".join(str(c) for c in palette if c)
            except TypeError:
                colors = ""
            if colors:
                override_lines.append(
                    f"- Palette: {colors} — use these as the primary / accent / background colors "
                    f"in every slide's color system. Do NOT fall back to the default style colors."
                )
        if font_heading or font_body or cjk_heading or cjk_body:
            if font_heading:
                override_lines.append(
                    f"- Western heading font-family: `{font_heading}` — use this for Western (Latin) heading/title text."
                )
            if font_body:
                override_lines.append(
                    f"- Western body font-family: `{font_body}` — use this for Western (Latin) body/paragraph text."
                )
            if cjk_heading:
                override_lines.append(
                    f"- CJK heading font-family: `{cjk_heading}` — use this for CJK (Chinese/Japanese/Korean) heading/title text."
                )
            if cjk_body:
                override_lines.append(
                    f"- CJK body font-family: `{cjk_body}` — use this for CJK (Chinese/Japanese/Korean) body/paragraph text."
                )
        elif font:
            override_lines.append(
                f"- Font-family: `{font}` — use this family for every text element throughout."
            )
        if density:
            override_lines.append(
                f"- Layout density: `{density}` — respect this target spacing/whitespace aesthetic."
            )
        user_parts.append("\n".join(override_lines))

    if ref_template:
        user_parts.append(
            f"\n## Design Spec Reference Template\n\n"
            f"Follow this template structure exactly:\n\n{ref_template}"
        )

    user_parts.append(
        "\n\nGenerate the complete design_spec.md following the template structure. "
        "All 11 sections (I through XI) must be present. In Section IX, list each page once with its page type."
    )

    base_messages = [
        LLMMessage.system(system_prompt),
        LLMMessage.user("\n".join(user_parts)),
    ]

    # Save full prompt for debugging
    if debug_dir:
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
            prompt_file = debug_dir / "strategist_prompt.md"
            parts = []
            for msg in base_messages:
                parts.append(f"--- ROLE: {msg.role} ---\n\n{msg.content}")
            prompt_file.write_text("\n\n".join(parts), encoding="utf-8")
        except Exception:
            pass

    last_error = ""
    for attempt in range(1, MAX_DESIGN_SPEC_ATTEMPTS + 1):
        messages = list(base_messages)
        if last_error:
            messages.append(
                LLMMessage.user(
                    "The previous design_spec.md response was invalid: "
                    f"{last_error}. Regenerate the complete design_spec.md now. "
                    "Do not return an empty response. Include all required sections I through XI."
                )
            )

        response: LLMResponse = await llm.chat(
            messages,
            model,
            temperature=0.25 if attempt > 1 else 0.4,
            max_tokens=DESIGN_SPEC_MAX_TOKENS,
        )
        content = response.content.strip()
        error = _design_spec_validation_error(
            content,
            expected_page_count=page_count,
            expected_inventory=inventory if enforce_page_types else None,
        )
        if error is None:
            # Phase 2: Icon Decoration Round (if enabled)
            if enable_icon:
                from backend.orchestrator.icon_round import run_icon_round

                logger.info("Running Icon Decoration Round (Phase 2)")
                content = await run_icon_round(
                    content,
                    manuscript,
                    icon_library,
                    llm,
                    model,
                    enable_icon_rag=enable_icon_rag,
                    gemini_api_key=gemini_api_key,
                    debug_dir=debug_dir,
                )
            return content
        last_error = error

    raise RuntimeError(f"Invalid design specification from strategist: {last_error}")
