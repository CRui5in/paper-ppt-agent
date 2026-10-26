"""Strategist agent: produces a design specification from a manuscript."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from backend.config import CANVAS_FORMATS, DESIGN_STYLES, settings
from backend.llm import LLMMessage, LLMProvider, LLMResponse
from backend.orchestrator.manuscript import (
    count_manuscript_pages,
    format_page_inventory,
    page_inventory,
)
logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "strategist.md"
MAX_DESIGN_SPEC_ATTEMPTS = 2
MAX_DESIGN_SPEC_CONTINUATIONS = 2


def _design_spec_max_tokens(model: str) -> int:
    name = model.lower().rsplit("/", 1)[-1]
    if name == "deepseek-flash" or name.startswith("deepseek-v4-"):
        return settings.deepseek_design_spec_max_tokens
    return settings.design_spec_max_tokens


def _design_spec_continuation(content: str) -> tuple[str, str]:
    """Keep complete blocks and regenerate the final, possibly cut-off block."""
    headings = list(re.finditer(r"(?m)^#{1,6}[ \t]+[^\r\n]+", content))
    if not headings:
        return "", "Start with Section I."
    last = headings[-1]
    return content[:last.start()].rstrip(), f"Start with this heading: {last.group(0)}"

def _format_chapter_map(inventory: list[dict[str, str | int]]) -> str:
    """Render an explicit chapter-to-slide map from the manuscript contract."""
    lines: list[str] = []
    current_title = ""
    current_page: int | None = None
    current_items: list[str] = []
    opening_items: list[str] = []

    def flush() -> None:
        nonlocal current_title, current_page, current_items
        if current_page is None:
            return
        if current_items:
            covered = "; ".join(current_items)
        else:
            covered = "(no following content slides)"
        lines.append(f"- Chapter slide {current_page}: {current_title} -> {covered}")
        current_title = ""
        current_page = None
        current_items = []

    for item in inventory:
        page = int(item["page"])
        page_type = str(item["type"])
        title = str(item["title"])
        if page_type == "chapter":
            flush()
            current_page = page
            current_title = title
            current_items = []
        elif page_type == "content":
            entry = f"Slide {page} {title}"
            if current_page is None:
                opening_items.append(entry)
            else:
                current_items.append(entry)
        elif page_type == "ending":
            flush()

    flush()
    if opening_items:
        lines.insert(
            0,
            "- Opening content before first chapter: " + "; ".join(opening_items),
        )
    if not lines:
        return "No explicit chapter divider slides; keep content sequence continuous."
    return "\n".join(lines)


def _extract_design_spec_section(content: str, roman: str) -> str:
    pattern = rf"(?ims)^#+\s*{re.escape(roman)}\.\s+.*?(?=^#+\s*[IVXLCDM]+\.\s+|\Z)"
    match = re.search(pattern, content)
    return match.group(0) if match else ""


def _has_required_design_spec_section(text: str, roman: str, title: str) -> bool:
    pattern = rf"(?im)^#+\s*{roman}\.\s+.*{re.escape(title)}"
    if re.search(pattern, text):
        return True

    # LLMs sometimes rename sections to equivalent titles.  Accept common
    # aliases so that a valid design spec is not rejected on a wording variant.
    _SECTION_FALLBACKS: dict[str, str] = {
        "I": (
            r"Project\s+Information|Project\s+Overview|Project\s+Details|"
            r"Project\s+Summary|项目信息|项目概况|项目概览|项目详情|项目概述"
        ),
        "XI": (
            r"Technical|Constraints?|Requirements?|Guidelines?|Notes?|"
            r"Specification|Implementation|Execution|Rendering|SVG|Output|"
            r"技术|约束|限制|要求|规范|实现|执行|渲染|输出"
        ),
    }
    fallback = _SECTION_FALLBACKS.get(roman)
    if fallback:
        return bool(
            re.search(
                rf"(?im)^#+\s*{roman}\.\s+.*(?:{fallback})",
                text,
            )
        )
    return False


_PAGE_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "cover": ("cover", "封面", "封面页", "标题页"),
    "chapter": ("chapter", "transition", "section", "章节", "章节页", "章标题", "过渡", "过渡页", "转场"),
    "toc": ("toc", "agenda", "outline", "目录", "大纲"),
    "content": ("content", "body", "main", "内容", "内容页", "正文", "主体", "普通页"),
    "ending": ("ending", "closing", "thanks", "q&a", "结束", "结束页", "致谢", "感谢", "问答"),
}
_PAGE_TYPE_WORD_PATTERN = "|".join(
    re.escape(alias)
    for aliases in _PAGE_TYPE_ALIASES.values()
    for alias in sorted(aliases, key=len, reverse=True)
)
_PAGE_TYPE_LABEL_PATTERN = (
    r"(?:page\s*)?type|page\s*category|slide\s*type|类型|页型|页面类型|幻灯片类型"
)


def _response_finish_reason(response: LLMResponse) -> str:
    raw = response.raw
    choices = None
    if isinstance(raw, dict):
        choices = raw.get("choices")
    elif raw is not None:
        choices = getattr(raw, "choices", None)
    if not choices:
        return ""
    choice = choices[0]
    if isinstance(choice, dict):
        return str(choice.get("finish_reason") or "")
    return str(getattr(choice, "finish_reason", "") or "")


def _write_strategy_attempt_debug(
    debug_dir: Path | None,
    *,
    attempt: int,
    response: LLMResponse,
    validation_error: str | None,
    continuation: int = 0,
    max_tokens: int | None = None,
) -> None:
    if debug_dir is None:
        return
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        usage = response.usage
        header = [
            f"attempt={attempt}",
            f"continuation={continuation}",
            f"max_tokens={max_tokens}",
            f"chars={len(response.content or '')}",
            f"finish_reason={_response_finish_reason(response)}",
        ]
        if usage is not None:
            header.extend(
                [
                    f"prompt_tokens={usage.prompt_tokens}",
                    f"completion_tokens={usage.completion_tokens}",
                    f"total_tokens={usage.total_tokens}",
                ]
            )
        if validation_error:
            header.append(f"validation_error={validation_error}")
        suffix = f"_continuation_{continuation}" if continuation else ""
        path = debug_dir / f"strategist_response_attempt_{attempt}{suffix}.md"
        path.write_text(
            "<!-- " + "; ".join(header) + " -->\n\n" + (response.content or ""),
            encoding="utf-8",
        )
    except Exception:
        pass


def _outline_block_mentions_page_type(block: str, page_type: str) -> bool:
    aliases = _PAGE_TYPE_ALIASES.get(page_type, (page_type,))
    for alias in aliases:
        escaped = re.escape(alias)
        if alias.isascii():
            if re.search(rf"(?i)(?<![a-z0-9_]){escaped}(?![a-z0-9_])", block):
                return True
        elif alias in block:
            return True
    return False


def _insert_outline_page_type(block: str, page_num: int | str, page_type: str) -> str:
    first_line_end = block.find("\n")
    if first_line_end < 0:
        first_line = block
        rest = ""
    else:
        first_line = block[:first_line_end]
        rest = block[first_line_end:]

    if first_line.lstrip().startswith("|"):
        page_ref_pattern = rf"(?i)\b((?:page|slide)\s*0*{page_num})\b"
        table_line = re.sub(
            page_ref_pattern,
            rf"\1 (type: {page_type})",
            first_line,
            count=1,
        )
        if table_line != first_line:
            return table_line + rest

    return f"{first_line}\n- **Type**: {page_type}{rest}"


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
        if not _has_required_design_spec_section(text, roman, title):
            return f"design_spec.md is missing section {roman}. {title}"

    section_numbers = re.findall(r"(?im)^#+[ \t]*([IVXLCDM]+)\.[ \t]+", text)
    if len(section_numbers) != len(set(section_numbers)):
        return "design_spec.md contains duplicate numbered sections"

    if expected_inventory is not None:
        expected_pages = {int(item["page"]) for item in expected_inventory}
    elif expected_page_count is not None:
        expected_pages = set(range(1, expected_page_count + 1))
    else:
        return None

    outline = _extract_design_spec_section(text, "IX")
    page_numbers = [
        int(number) for number in re.findall(
            r"(?im)^#+[ \t]*(?:page|slide)[ \t]+0*(\d+)\b", outline,
        )
    ]
    actual_pages = set(page_numbers)
    if missing := sorted(expected_pages - actual_pages):
        return f"design_spec.md Section IX is missing slide headings for pages {missing}"
    if extra := sorted(actual_pages - expected_pages):
        return f"design_spec.md Section IX contains unexpected pages {extra}"
    if len(page_numbers) != len(actual_pages):
        return "design_spec.md Section IX contains duplicate slide headings"

    return None


def _outline_page_numbers(content: str) -> set[int]:
    outline = _extract_design_spec_section(content, "IX")
    return {
        int(match.group(1))
        for match in re.finditer(r"(?i)\b(?:page|slide)\s*0*(\d+)\b", outline)
    }


def _align_content_outline_page_types(
    content: str,
    expected_inventory: list[dict[str, str | int]] | None,
) -> str:
    """Repair outline page-type labels when the page contract is otherwise intact."""
    if not expected_inventory:
        return content

    outline = _extract_design_spec_section(content, "IX")
    if not outline:
        return content

    repaired = outline
    for item in expected_inventory:
        page_num = item["page"]
        expected_type = str(item["type"])
        line_pattern = rf"(?im)^(\s*[-*]?\s*(?:page|slide)\s*0*{page_num}\s*[:：\-–—]\s*)(?:\*\*)?(?:{_PAGE_TYPE_WORD_PATTERN})(?:\*\*)?"
        repaired = re.sub(line_pattern, rf"\g<1>{expected_type}", repaired, count=1)

        block_pattern = rf"(?is)(\b(?:page|slide)\s*0*{page_num}\b.+?)(?=\b(?:page|slide)\s*0*\d+\b|\Z)"
        block_match = re.search(block_pattern, repaired)
        if not block_match:
            continue
        block = block_match.group(1)
        if _outline_block_mentions_page_type(block, expected_type):
            continue

        typed_block = re.sub(
            rf"(?im)^(\s*(?:[-*]\s*)?(?:\*\*)?(?:{_PAGE_TYPE_LABEL_PATTERN})(?:\*\*)?\s*[:：]\s*)(?:{_PAGE_TYPE_WORD_PATTERN})",
            rf"\g<1>{expected_type}",
            block,
            count=1,
        )
        if typed_block == block:
            typed_block = _insert_outline_page_type(block, page_num, expected_type)
        if typed_block != block:
            start, end = block_match.span(1)
            repaired = repaired[:start] + typed_block + repaired[end:]

    if repaired == outline:
        return content
    start = content.find(outline)
    if start < 0:
        return content
    return content[:start] + repaired + content[start + len(outline):]


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
    style_overrides: dict | None = None,
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
        detail_level: Compatibility parameter. It controls upstream page and context
            budgets, not design density.
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
    chapter_map_block = _format_chapter_map(inventory)
    enforce_page_types = "<!--" in manuscript and "page_type" in manuscript

    user_parts = [
        f"## Manuscript\n\n{manuscript}",
        f"\n## Manuscript Page Inventory\n\n{inventory_block}",
        f"\n## Manuscript Chapter Map (authoritative)\n\n{chapter_map_block}",
        f"\n## Canvas Format: {fmt['name']} ({fmt['ratio']}), viewBox: `{fmt['viewbox']}`",
        f"\n## Design Style: {style_info['name']}",
        f"\n## Page Count: {page_count}",
        f"\n## Language: {language}",
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
            "- For decks over 30 pages, keep Section IX concise but list every page exactly once; never stop before the final manuscript page.",
            "- Do not add, remove, or reorder cover, chapter/transition, content, or ending pages.",
            "- Treat the Manuscript Chapter Map as authoritative: do not invent, rename, split, merge, or move chapter divider slides in Section IX.",
            "- Keep cover/chapter/ending pages distinct from content pages: cover pages may include title metadata and a modest context/accent treatment, while chapter/ending pages stay minimal dividers/closers.",
            "- Do not assign paper figures, dense article-content blocks, multi-column evidence layouts, or detailed result sections to cover/chapter/ending pages.",
            "- If a chapter/ending manuscript page contains extra body text, summarize it into at most one subtitle/key phrase in Section IX; do not create cards, KPI strips, question lists, mini-agendas, or `核心问题` / `本章看点` blocks on those pages.",
            "- Keep every chapter page on the same divider layout; only chapter number, title, and optional subtitle/key phrase may change.",
            "- Keep content pages as content pages: do not style them as chapter dividers or add oversized chapter/section counters.",
            "- Put any page number in the footer only; do not create top-left administrative labels or mixed section/page counters.",
            "- In Section IX, every page must include `Style Family:`, `Layout Family:`, and `Density:` fields. All chapter pages must share the same Style Family.",
            "- Use reusable content layout families such as `figure-left-text-right`, `text-left-figure-right`, `two-column-evidence`, `three-card-grid`, `process-flow-with-evidence`, `comparison-table-callout`, or `full-width-chart-with-notes` unless the manuscript truly requires another.",
            "- TOC pages are title plus the full ordered chapter/list items only; do not plan side summaries, stats, charts, or process blocks.",
            "- Derive each page's `Density:` from that page's manuscript, visual share, and argument structure. Do not infer density from any global detail setting.",
            "- In Section IX, separate footer page numbering from chapter index. Chapter labels use the planned chapter index, never the slide page number.",
            f"- The visible slide language must be `{language}`.",
            f"- {_language_constraint(language)}",
        ])
        user_parts.append(f"\n{template_context}")
    else:
        user_parts.extend([
            f"- Primary Color: {style_info['primary']}",
            f"- Accent Color: {style_info['accent']}",
            "- Typography: use a CJK-compatible sans-serif stack by default: "
            "`Source Han Sans SC`, `Noto Sans CJK SC`, `Noto Sans SC`, "
            "`PingFang SC`, `Microsoft YaHei`, `Arial`, `sans-serif`; "
            "use heavier weights for headings.",
            "\n## Hard Constraints",
            "- Respect the selected design style. Do not silently fall back to a default academic theme when another style is selected.",
            f"- Page contract: exactly {page_count} pages; page N in Section IX must match manuscript page N.",
            "- For decks over 30 pages, keep Section IX concise but list every page exactly once; never stop before the final manuscript page.",
            "- Do not add, remove, or reorder cover, chapter/transition, content, or ending pages.",
            "- Treat the Manuscript Chapter Map as authoritative: do not invent, rename, split, merge, or move chapter divider slides in Section IX.",
            "- Keep cover/chapter/ending pages distinct from content pages: cover pages may include title metadata and a modest context/accent treatment, while chapter/ending pages stay minimal dividers/closers.",
            "- Do not assign paper figures, dense article-content blocks, multi-column evidence layouts, or detailed result sections to cover/chapter/ending pages.",
            "- If a chapter/ending manuscript page contains extra body text, summarize it into at most one subtitle/key phrase in Section IX; do not create cards, KPI strips, question lists, mini-agendas, or `核心问题` / `本章看点` blocks on those pages.",
            "- Keep every chapter page on the same divider layout; only chapter number, title, and optional subtitle/key phrase may change.",
            "- Keep content pages as content pages: do not style them as chapter dividers or add oversized chapter/section counters.",
            "- Put any page number in the footer only; do not create top-left administrative labels or mixed section/page counters.",
            "- In Section IX, every page must include `Style Family:`, `Layout Family:`, and `Density:` fields. All chapter pages must share the same Style Family.",
            "- Use reusable content layout families such as `figure-left-text-right`, `text-left-figure-right`, `two-column-evidence`, `three-card-grid`, `process-flow-with-evidence`, `comparison-table-callout`, or `full-width-chart-with-notes` unless the manuscript truly requires another.",
            "- TOC pages are title plus the full ordered chapter/list items only; do not plan side summaries, stats, charts, or process blocks.",
            "- Derive each page's `Density:` from that page's manuscript, visual share, and argument structure. Do not infer density from any global detail setting.",
            "- In Section IX, separate footer page numbering from chapter index. Chapter labels use the planned chapter index, never the slide page number.",
            f"- The visible slide language must be `{language}`.",
            f"- {_language_constraint(language)}",
        ])

    user_parts.append(
        "\n## Icon Usage: DISABLED\n"
        "Provider generation does not add icon decoration. Do NOT use any "
        "`<use data-icon=\"...\"/>` elements in any slide. Use plain SVG "
        "shapes, charts, diagrams, text, and local image assets instead. "
        "Never use emoji, colored emoji glyphs, Unicode symbols, dingbats, "
        "stars, checkmarks, arrows, warning marks, punctuation, letters, or "
        "initials as icon substitutes."
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
        "All 11 sections (I through XI) must be present. In Section IX, list each page once with its page type, "
        "using a `#### Slide NN - Title` heading for every page. "
        "Define shared design rules once in Sections I-VIII; reference them briefly in each page plan "
        "instead of repeating the manuscript, palette, typography, or full SVG markup. "
        "Reserve output space for the final pages and Sections X-XI."
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
    last_nonempty_error = ""
    max_tokens = _design_spec_max_tokens(model)
    for attempt in range(1, MAX_DESIGN_SPEC_ATTEMPTS + 1):
        attempt_messages = list(base_messages)
        if last_error:
            attempt_messages.append(
                LLMMessage.user(
                    "The previous design_spec.md response was invalid: "
                    f"{last_error}. Regenerate the complete design_spec.md now. "
                    "Do not return an empty response. Include all required sections I through XI. "
                    "Keep shared rules and per-page plans concise so the entire deck fits."
                )
            )

        messages = list(attempt_messages)
        content = ""
        prefix = ""
        for continuation in range(MAX_DESIGN_SPEC_CONTINUATIONS + 1):
            response: LLMResponse = await llm.chat(
                messages,
                model,
                temperature=0.25 if attempt > 1 or continuation else 0.4,
                max_tokens=max_tokens,
            )
            chunk = response.content.strip()
            if chunk:
                content = f"{prefix}\n\n{chunk}".strip() if prefix else chunk
            error = _design_spec_validation_error(
                content,
                expected_page_count=page_count,
                expected_inventory=inventory if enforce_page_types else None,
            )
            truncated = _response_finish_reason(response) == "length"
            if truncated:
                # Even a response containing XI can end halfway through a rule.
                error = f"design_spec.md was truncated at the output token limit (max_tokens={max_tokens})"
            elif not chunk:
                error = "design_spec.md generation returned an empty response"
            _write_strategy_attempt_debug(
                debug_dir,
                attempt=attempt,
                response=response,
                validation_error=error,
                continuation=continuation,
                max_tokens=max_tokens,
            )
            if error is None:
                if enforce_page_types:
                    content = _align_content_outline_page_types(content, inventory)
                return content
            if not truncated or not chunk or continuation == MAX_DESIGN_SPEC_CONTINUATIONS:
                break

            prefix, restart = _design_spec_continuation(content)
            logger.info(
                "Design spec attempt %s reached its token limit; continuing from the final block (%s/%s)",
                attempt, continuation + 1, MAX_DESIGN_SPEC_CONTINUATIONS,
            )
            messages = [
                *attempt_messages,
                LLMMessage.assistant(content),
                LLMMessage.user(
                    "The response was cut off at the output token limit. "
                    "The application has kept all complete blocks before your final Markdown heading. "
                    f"{restart}\n"
                    "Rewrite that final block in full, then finish every remaining page and section through XI. "
                    "Return only this replacement tail, without repeating earlier blocks, a preamble, or code fences. "
                    "Use the authoritative manuscript page inventory; do not skip pages to reach Sections X-XI. "
                    "Keep page plans concise and refer to the shared design rules."
                ),
            ]
        last_error = error
        if content:
            last_nonempty_error = error

    detail = last_error
    if last_nonempty_error and last_nonempty_error != last_error:
        detail = f"{last_error}; previous non-empty attempt failed: {last_nonempty_error}"
    raise RuntimeError(f"Invalid design specification from strategist: {detail}")
