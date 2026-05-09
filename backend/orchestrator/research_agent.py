"""Research agent: multi-pass deep analysis of academic papers for slide manuscripts.

Architecture:
    Pass 1 — Deep Reading: structured critical analysis of the paper.
             Optional external enrichment (related papers, citations, web
             discussions) is injected here so the LLM can position the paper
             against existing literature and sharpen the gap analysis.
    Pass 2 — Narrative Arc: design a story-driven slide plan.
    Pass 3 — Manuscript: generate the actual slide manuscript.
    Pass 4 — Self-Review: evaluate quality and revise if needed.

Deep research is opt-in. Without it, the agent writes the manuscript in a
single pass; external enrichment can still be injected as context.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from backend.llm import LLMMessage, LLMProvider, LLMResponse
from backend.orchestrator.provider_guidance import (
    deepseek_research_guidance,
    is_deepseek_provider,
)
from backend.orchestrator.manuscript import (
    auto_slide_range,
    extract_page_type,
    page_type_budget,
    page_type_budget_guidance,
    split_manuscript_pages,
)
from backend.parser.paper_model import ParsedPaper

if TYPE_CHECKING:
    from backend.orchestrator.research_enrichment import ResearchFinding

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Prompt files for each pass
PASS1_PROMPT = PROMPTS_DIR / "research_pass1_analysis.md"
PASS2_PROMPT = PROMPTS_DIR / "research_pass2_narrative.md"
PASS3_PROMPT = PROMPTS_DIR / "research_pass3_manuscript.md"
PASS4_PROMPT = PROMPTS_DIR / "research_pass4_review.md"

# Legacy single-pass prompt (kept for revise_manuscript backward compat)
LEGACY_PROMPT = PROMPTS_DIR / "research.md"

DEEPSEEK_MAX_TOKENS = 24576
QUALITY_THRESHOLD = 28  # out of 35 (7 dimensions × 5 points each)
MAX_MANUSCRIPT_ATTEMPTS = 2
SINGLE_PASS_SYSTEM_PROMPT = (
    "You write slide-structured manuscripts from academic papers. "
    "Extract the paper's problem, method, evidence, and takeaway; turn them into "
    "a clear slide sequence. Output only the manuscript, separated by standalone "
    "`---` lines."
)

_SLIDE_HEADING_RE = re.compile(
    r"^##\s+(?:slide|幻灯片)\s*\d+\s*[:：].*$",
    re.IGNORECASE,
)
_MANUSCRIPT_MARKER_RE = re.compile(
    r"^##\s+(?:slide\s+manuscript(?:\s*\([^)]*\))?|"
    r"revised\s+slide\s+manuscript|final\s+slide\s+manuscript|"
    r"revised\s+manuscript|final\s+manuscript)\s*$",
    re.IGNORECASE,
)
_REVIEW_HEADING_RE = re.compile(
    r"^##\s+(?:step\s*\d+|assessment|review|quality|evaluation|consensus|issues)\b",
    re.IGNORECASE,
)
_FIG_TOKEN_RE = re.compile(r"\[\[FIG:([A-Za-z0-9_\-]+)\]\]")


def _debug_write_text(debug_dir: Path | None, filename: str, content: str) -> None:
    if debug_dir is None:
        return
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / filename).write_text(content, encoding="utf-8")
    except OSError:
        logger.exception("Failed to write research debug file %s", filename)


def _debug_write_messages(
    debug_dir: Path | None,
    filename: str,
    messages: list[LLMMessage],
) -> None:
    parts = [f"--- ROLE: {msg.role} ---\n\n{msg.content}" for msg in messages]
    _debug_write_text(debug_dir, filename, "\n\n".join(parts))


async def _run_single_pass_analysis(
    paper: ParsedPaper,
    llm: LLMProvider,
    model: str,
    *,
    instruction: str = "",
    num_pages: int | None = None,
    language: str = "en",
    detail_level: str = "normal",
    enrichment_block: str = "",
    is_deepseek: bool = False,
    debug_dir: Path | None = None,
) -> str:
    """Generate a slide manuscript in one LLM call for the non-deep mode."""
    user_parts = [
        f"## Paper Content\n\n{paper.to_markdown()}",
        f"\n## Target Language\n\n{language}\n\n{_language_guidance(language)}",
        f"\n## Target Slides\n\n{_target_slides_guidance(num_pages, detail_level)}",
        f"\n## Detail Level\n\n{detail_level}\n\n{DETAIL_GUIDANCE.get(detail_level, DETAIL_GUIDANCE['normal'])}",
    ]
    figure_inventory = _figure_token_inventory_block(paper)
    if figure_inventory:
        user_parts.append(f"\n{figure_inventory}")
    if enrichment_block:
        user_parts.append(f"\n{enrichment_block}")
    if instruction:
        user_parts.append(f"\n## User Instruction\n\n{instruction}")
    if is_deepseek:
        user_parts.append("\n" + deepseek_research_guidance(detail_level))
    user_parts.append(
        "\n\nProduce the final slide manuscript now. Output only the slide manuscript: "
        "no analysis notes, no quality review, no scoring, no preface. Use standalone "
        "`---` lines only as slide delimiters."
    )
    base_messages = [
        LLMMessage.system(SINGLE_PASS_SYSTEM_PROMPT),
        LLMMessage.user("\n".join(user_parts)),
    ]
    _debug_write_messages(debug_dir, "research_single_pass_prompt.md", base_messages)

    last_error = ""
    response_content = ""
    for attempt in range(1, MAX_MANUSCRIPT_ATTEMPTS + 1):
        messages = list(base_messages)
        if last_error:
            messages.append(
                LLMMessage.user(_structure_retry_prompt(last_error, num_pages, detail_level))
            )
        response = await llm.chat(
            messages,
            model,
            temperature=0.35 if attempt > 1 else 0.45,
            max_tokens=DEEPSEEK_MAX_TOKENS if is_deepseek else None,
        )
        response_content = response.content
        _debug_write_text(
            debug_dir,
            "research_single_pass_response.md"
            if attempt == 1
            else f"research_single_pass_response_attempt{attempt}.md",
            response_content,
        )
        last_error = _manuscript_validation_error(
            response_content,
            paper,
            num_pages,
            detail_level,
        ) or ""
        if not last_error:
            return response_content

    logger.warning("Single-pass manuscript structure invalid after retry: %s", last_error)
    return response_content


# ── Language guidance ───────────────────────────────────────────────────────────


def _language_guidance(language: str) -> str:
    guidance = {
        "zh": (
            "Write all slide titles, bullets, callouts, and presenter-facing content in Simplified Chinese. "
            "Keep paper titles, author names, model names, dataset names, and metric abbreviations in their original form when needed."
        ),
        "en": (
            "Write all slide titles, bullets, callouts, and presenter-facing content in English."
        ),
        "bilingual": (
            "Write slide titles and main bullets in bilingual Chinese and English where useful. "
            "Keep terminology aligned across both languages and avoid mixing untranslated fragments mid-sentence."
        ),
    }
    normalized = language.strip().lower()
    if normalized in guidance:
        return guidance[normalized]
    return (
        "Treat the requested language literally and write all slide titles, bullets, callouts, annotations, "
        f"and presenter-facing content in {language.strip() or 'the requested language'}. "
        "Keep proper nouns, paper titles, dataset names, model names, and metric abbreviations in their original form when needed."
    )


# ── Detail level guidance ──────────────────────────────────────────────────────


DETAIL_GUIDANCE = {
    "normal": (
        "Produce a concise but faithful reading of the paper. Capture the core "
        "problem, method, evidence, and conclusions without overloading each slide."
    ),
    "high": (
        "Read the paper more deeply before writing. Surface the paper's reasoning, "
        "method design choices, assumptions, experimental logic, and non-obvious takeaways. "
        "Slides may be moderately denser when that improves understanding."
    ),
    "very_high": (
        "Perform a thorough reading rather than a surface summary. Explicitly cover the "
        "paper's motivation, mechanism, architecture, training or inference flow, assumptions, "
        "limitations, and the significance of the results. It is acceptable for slides to be "
        "denser and richer so the deck reflects a complete understanding of the paper."
    ),
}


# ── Research context (optional external enrichment) ─────────────────────────────


class ResearchContext:
    """External enrichment findings injected into Pass 1.

    Empty by default — populated by `research_enrichment.enrich_context` when
    the user enables one or more sources. Even when populated, the 4-pass
    pipeline remains the authoritative analysis path; enrichment only sharpens
    Pass 1 (gap analysis, related-work positioning).
    """

    def __init__(self) -> None:
        self.findings: list["ResearchFinding"] = []
        # Errors are surfaced to the LLM (so it can note unavailable sources
        # in the gap analysis) AND to the frontend via the progress channel.
        self.errors: list[str] = []
        # Audit trail of the actual queries we sent — useful when debugging
        # zero-result enrichment runs.
        self.queries_used: list[str] = []

    @property
    def has_enrichment(self) -> bool:
        return bool(self.findings)

    def enrichment_block_for_pass1(self) -> str:
        """Format enrichment as a markdown block injected before Pass 1.

        Pass 1 is where related-work context actually changes the LLM's
        reasoning (gap analysis, contribution framing). Earlier versions of
        this code injected into Pass 2/3, which is too late — by then the
        analysis is fixed and the related work is just decoration.
        """
        if not self.findings and not self.errors:
            return ""

        parts: list[str] = ["## Supplementary Related-Work Context\n"]
        parts.append(
            "Use the entries below to: (a) identify what THIS paper extends, "
            "challenges, or supersedes; (b) sharpen the Pass 1 gap analysis with "
            "concrete prior art; (c) flag if a related paper contradicts THIS "
            "paper's claim. Do NOT copy these abstracts into the manuscript — "
            "they are for your reasoning only.\n"
        )

        # Group by source for readability.
        by_source: dict[str, list["ResearchFinding"]] = {}
        for f in self.findings:
            by_source.setdefault(f.source, []).append(f)

        labels = {
            "arxiv": "### Related Papers (arXiv)",
            "semantic_scholar": "### Cited / Citing Work (Semantic Scholar)",
            "web": "### Web Discussions",
        }
        for source, items in by_source.items():
            parts.append(labels.get(source, f"### {source}"))
            for f in items[:5]:
                meta_bits: list[str] = []
                if f.year:
                    meta_bits.append(str(f.year))
                if f.citation_count is not None:
                    meta_bits.append(f"{f.citation_count} citations")
                if f.authors:
                    head = ", ".join(f.authors[:3])
                    if len(f.authors) > 3:
                        head += " et al."
                    meta_bits.append(head)
                meta = " · ".join(meta_bits)
                abstract = (f.abstract or "").strip()
                if len(abstract) > 600:
                    abstract = abstract[:600].rstrip() + "…"
                parts.append(f"- **{f.title or 'Untitled'}**" + (f" ({meta})" if meta else ""))
                if abstract:
                    parts.append(f"  {abstract}")
                if f.url:
                    parts.append(f"  <{f.url}>")
            parts.append("")

        if self.errors:
            parts.append("### Notes on Unavailable Sources")
            for err in self.errors:
                parts.append(f"- {err}")
            parts.append(
                "\nProceed with the analysis; do not fabricate replacements for "
                "sources that failed to load."
            )

        return "\n".join(parts)


def _expected_slide_count(num_pages: int | None, detail_level: str = "normal") -> int:
    return sum(page_type_budget(num_pages, detail_level).values())


def _manuscript_structure_error(
    manuscript: str,
    num_pages: int | None,
    detail_level: str = "normal",
) -> str | None:
    pages = split_manuscript_pages(manuscript)
    seen = {"cover": 0, "chapter": 0, "content": 0, "ending": 0}
    missing_meta = []
    for index, page in enumerate(pages, start=1):
        if "page_type" not in page:
            missing_meta.append(str(index))
        page_type = extract_page_type(page)
        if page_type in seen:
            seen[page_type] += 1
        else:
            return f"slide {index} has unsupported page_type `{page_type}`"

    if missing_meta:
        return "missing page_type metadata on slides " + ", ".join(missing_meta[:8])

    if num_pages:
        expected_count = _expected_slide_count(num_pages, detail_level)
        if len(pages) != expected_count:
            return f"expected {expected_count} slides, got {len(pages)}"

        expected_budget = page_type_budget(num_pages, detail_level)
        drift = [
            f"{name}: expected {expected_budget[name]}, got {seen.get(name, 0)}"
            for name in expected_budget
            if seen.get(name, 0) != expected_budget[name]
        ]
        if drift:
            return "page type budget mismatch (" + "; ".join(drift) + ")"
    else:
        min_pages, max_pages = auto_slide_range(detail_level)
        if not min_pages <= len(pages) <= max_pages:
            return f"expected {min_pages}-{max_pages} slides, got {len(pages)}"
        if seen["cover"] != 1:
            return f"expected 1 cover slide, got {seen['cover']}"
        if seen["ending"] != 1:
            return f"expected 1 ending slide, got {seen['ending']}"
        if not 3 <= seen["chapter"] <= 5:
            return f"expected 3-5 chapter slides, got {seen['chapter']}"
        if seen["content"] < 1:
            return "expected at least 1 content slide"

    if pages and extract_page_type(pages[-1]) == "ending":
        ending_text = pages[-1].lower()
        closing_markers = (
            "谢谢",
            "thank you",
            "thanks",
            "q&a",
            "questions",
            "致谢",
            "交流",
        )
        if not any(marker in ending_text for marker in closing_markers):
            return "ending slide must be a closing/thanks page"
    return None


def _available_figure_tokens(paper: ParsedPaper) -> dict[str, str]:
    """Return valid FIG token ids mapped to compact captions."""
    tokens: dict[str, str] = {}
    for fig in paper.all_figures():
        if not ParsedPaper._should_include_figure(fig):
            continue
        fig_id = fig.fig_id
        caption = (fig.caption or "").replace("\n", " ").strip()
        if len(caption) > 180:
            caption = caption[:177].rstrip() + "..."
        tokens[fig_id] = caption or "Extracted paper figure"
    return tokens


def _figure_token_inventory_block(paper: ParsedPaper) -> str:
    tokens = _available_figure_tokens(paper)
    if not tokens:
        return ""
    lines = [
        "## Valid Paper Figure Tokens",
        "",
        "Only the exact tokens below may appear in the manuscript. Do not rename them, translate them, or create semantic aliases such as `fig_arch`.",
        "",
        "| Token | Caption |",
        "| ----- | ------- |",
    ]
    for token, caption in tokens.items():
        lines.append(f"| `[[FIG:{token}]]` | {caption} |")
    return "\n".join(lines)


def _manuscript_figure_token_error(manuscript: str, paper: ParsedPaper) -> str | None:
    valid = set(_available_figure_tokens(paper))
    used = _FIG_TOKEN_RE.findall(manuscript)
    if not used or not valid:
        return None
    invalid = sorted({token for token in used if token not in valid})
    if not invalid:
        return None
    sample_valid = ", ".join(f"[[FIG:{token}]]" for token in sorted(valid)[:10])
    return (
        "invalid paper figure token(s): "
        + ", ".join(f"[[FIG:{token}]]" for token in invalid)
        + ". Use only exact tokens from the Valid Paper Figure Tokens list"
        + (f", for example {sample_valid}" if sample_valid else "")
        + "."
    )


def _manuscript_validation_error(
    manuscript: str,
    paper: ParsedPaper,
    num_pages: int | None,
    detail_level: str = "normal",
) -> str | None:
    structure_error = _manuscript_structure_error(manuscript, num_pages, detail_level)
    figure_error = _manuscript_figure_token_error(manuscript, paper)
    if structure_error and figure_error:
        return f"{structure_error}; {figure_error}"
    return structure_error or figure_error


def _structure_retry_prompt(
    error: str,
    num_pages: int | None,
    detail_level: str = "normal",
) -> str:
    return (
        "The previous manuscript did not match the slide structure contract: "
        f"{error}.\n\n"
        "Regenerate the full slide manuscript only.\n"
        "If the error mentions paper figure tokens, replace invalid tokens with exact tokens from the Valid Paper Figure Tokens list, or omit the real figure when no listed token matches.\n"
        f"{page_type_budget_guidance(num_pages, detail_level)}"
    )


# ── Main multi-pass analysis ───────────────────────────────────────────────────


async def analyze_paper(
    paper: ParsedPaper,
    llm: LLMProvider,
    model: str,
    *,
    instruction: str = "",
    num_pages: int | None = None,
    language: str = "en",
    detail_level: str = "normal",
    research_context: ResearchContext | None = None,
    enable_deep_research: bool = False,
    debug_dir: Path | None = None,
    on_progress: Callable[[str, float], None] | None = None,
) -> str:
    """Analyze a paper and produce a slide-structured manuscript via multi-pass.

    Args:
        paper: Parsed paper data.
        llm: LLM provider instance.
        model: Model ID to use.
        instruction: Optional user instruction.
        num_pages: Target number of slides (None = auto).
        language: Target language for visible slide text.
        detail_level: Controls analysis depth (normal/high/very_high).
        research_context: Optional enrichment from external tools.
        enable_deep_research: When True, use the 4-pass deep workflow. When
            False, generate the manuscript with one compatibility call.
        debug_dir: Optional directory for prompt/response audit files.
        on_progress: Optional callback invoked as (message, progress_fraction) after each pass.

    Returns:
        Manuscript markdown with --- page separators.
    """
    is_deepseek = is_deepseek_provider(llm, model)
    paper_md = paper.to_markdown()

    # External enrichment is injected into Pass 1 specifically — that's where
    # related-work context actually changes the analysis (gap framing,
    # contribution delta). Injecting later just decorates the manuscript.
    enrichment_block = ""
    if research_context and (research_context.has_enrichment or research_context.errors):
        enrichment_block = research_context.enrichment_block_for_pass1()
        logger.info("Research: Pass 1 enrichment block (%d chars)", len(enrichment_block))

    if not enable_deep_research:
        logger.info("Research single-pass mode: manuscript generation...")
        if on_progress:
            on_progress("Generating manuscript", 0.24)
        manuscript = await _run_single_pass_analysis(
            paper,
            llm,
            model,
            instruction=instruction,
            num_pages=num_pages,
            language=language,
            detail_level=detail_level,
            enrichment_block=enrichment_block,
            is_deepseek=is_deepseek,
            debug_dir=debug_dir,
        )
        _debug_write_text(debug_dir, "research_final_manuscript.md", manuscript)
        return manuscript

    # ── Pass 1: Deep Reading ───────────────────────────────────────────────
    logger.info("Research Pass 1: Deep reading...")
    pass1_system = PASS1_PROMPT.read_text(encoding="utf-8")

    pass1_user_parts = [
        f"## Paper Content\n\n{paper_md}",
        f"\n## Detail Level\n\n{detail_level}\n\n{DETAIL_GUIDANCE.get(detail_level, DETAIL_GUIDANCE['normal'])}",
    ]
    if enrichment_block:
        pass1_user_parts.append(f"\n{enrichment_block}")
    if instruction:
        pass1_user_parts.append(f"\n## User Instruction\n\n{instruction}")
    if is_deepseek:
        pass1_user_parts.append("\n" + deepseek_research_guidance(detail_level))
    pass1_user_parts.append(
        "\n\nAnalyze this paper following the structured format above. Be specific and insightful. "
        "When supplementary related-work context is provided, use it to ground the gap analysis "
        "in concrete prior art rather than vague claims."
    )

    pass1_messages = [
        LLMMessage.system(pass1_system),
        LLMMessage.user("\n".join(pass1_user_parts)),
    ]
    _debug_write_messages(debug_dir, "research_pass1_prompt.md", pass1_messages)
    pass1_response = await llm.chat(
        pass1_messages,
        model,
        temperature=0.4,
        max_tokens=DEEPSEEK_MAX_TOKENS if is_deepseek else None,
    )
    deep_analysis = pass1_response.content
    _debug_write_text(debug_dir, "research_pass1_response.md", deep_analysis)
    logger.info("Research Pass 1 complete (%d chars)", len(deep_analysis))
    if on_progress:
        on_progress("Pass 1/4 — Deep reading", 0.15)

    # ── Pass 2: Narrative Arc Design ───────────────────────────────────────
    logger.info("Research Pass 2: Narrative arc design...")
    pass2_system = PASS2_PROMPT.read_text(encoding="utf-8")

    pass2_user_parts = [
        f"## Deep Analysis of the Paper\n\n{deep_analysis}",
        f"\n## Target Slides\n\n{_target_slides_guidance(num_pages, detail_level)}",
        f"\n## Detail Level\n\n{detail_level}",
    ]
    pass2_user_parts.append(
        "\n\nDesign the narrative arc for this paper's presentation. Choose the best narrative strategy "
        "and specify each slide's role, core insight, and visual strategy."
    )

    pass2_messages = [
        LLMMessage.system(pass2_system),
        LLMMessage.user("\n".join(pass2_user_parts)),
    ]
    _debug_write_messages(debug_dir, "research_pass2_prompt.md", pass2_messages)
    pass2_response = await llm.chat(
        pass2_messages,
        model,
        temperature=0.5,
        max_tokens=DEEPSEEK_MAX_TOKENS if is_deepseek else None,
    )
    narrative_plan = pass2_response.content
    _debug_write_text(debug_dir, "research_pass2_response.md", narrative_plan)
    logger.info("Research Pass 2 complete (%d chars)", len(narrative_plan))
    if on_progress:
        on_progress("Pass 2/4 — Narrative arc", 0.20)

    # ── Pass 3: Manuscript Generation ──────────────────────────────────────
    logger.info("Research Pass 3: Manuscript generation...")
    pass3_system = PASS3_PROMPT.read_text(encoding="utf-8")

    pass3_user_parts = [
        f"## Deep Analysis\n\n{deep_analysis}",
        f"\n## Narrative Arc Plan\n\n{narrative_plan}",
        f"\n## Target Language\n\n{language}\n\n{_language_guidance(language)}",
        f"\n## Target Slides\n\n{_target_slides_guidance(num_pages, detail_level)}",
        f"\n## Detail Level\n\n{detail_level}\n\n{DETAIL_GUIDANCE.get(detail_level, DETAIL_GUIDANCE['normal'])}",
    ]
    figure_inventory = _figure_token_inventory_block(paper)
    if figure_inventory:
        pass3_user_parts.append(f"\n{figure_inventory}")
    if instruction:
        pass3_user_parts.append(f"\n## User Instruction\n\n{instruction}")
    # NOTE: enrichment_block is intentionally injected only into Pass 1 above.
    # Pass 3 sees the deep_analysis (which already absorbed the enrichment),
    # so re-injecting here would just burn context for no benefit.
    if is_deepseek:
        pass3_user_parts.append("\n" + deepseek_research_guidance(detail_level))
    pass3_user_parts.append(
        "\n\nGenerate the complete slide manuscript now. Use `---` to separate slides. "
        "Follow the narrative arc plan and the information aesthetics principles."
    )

    pass3_base_messages = [
        LLMMessage.system(pass3_system),
        LLMMessage.user("\n".join(pass3_user_parts)),
    ]
    _debug_write_messages(debug_dir, "research_pass3_prompt.md", pass3_base_messages)
    manuscript = ""
    last_structure_error = ""
    for attempt in range(1, MAX_MANUSCRIPT_ATTEMPTS + 1):
        pass3_messages = list(pass3_base_messages)
        if last_structure_error:
            pass3_messages.append(
                LLMMessage.user(
                    _structure_retry_prompt(last_structure_error, num_pages, detail_level)
                )
            )
        pass3_response = await llm.chat(
            pass3_messages,
            model,
            temperature=0.35 if attempt > 1 else 0.5,
            max_tokens=DEEPSEEK_MAX_TOKENS if is_deepseek else None,
        )
        manuscript = pass3_response.content
        _debug_write_text(
            debug_dir,
            "research_pass3_response.md"
            if attempt == 1
            else f"research_pass3_response_attempt{attempt}.md",
            manuscript,
        )
        last_structure_error = _manuscript_validation_error(
            manuscript,
            paper,
            num_pages,
            detail_level,
        ) or ""
        if not last_structure_error:
            break
    if last_structure_error:
        logger.warning("Pass 3 manuscript structure invalid after retry: %s", last_structure_error)
    logger.info("Research Pass 3 complete (%d chars)", len(manuscript))
    if on_progress:
        on_progress("Pass 3/4 — Manuscript", 0.25)

    # ── Pass 4: Self-Evaluation & Revision ─────────────────────────────────
    logger.info("Research Pass 4: Self-evaluation...")
    pass4_system = PASS4_PROMPT.read_text(encoding="utf-8")

    pass4_user_parts = [
        f"## Slide Manuscript to Evaluate\n\n{manuscript}",
        f"\n## Original Deep Analysis\n\n{deep_analysis[:3000]}",  # Truncate to avoid excessive context
        f"\n## Narrative Plan\n\n{narrative_plan[:2000]}",
        f"\n## Target Language\n\n{language}",
        f"\n## Detail Level\n\n{detail_level}",
    ]
    if figure_inventory:
        pass4_user_parts.append(f"\n{figure_inventory}")
    pass4_user_parts.append(
        "\n\nEvaluate the manuscript against the seven dimensions. "
        "If the total score is below 28/35 or any dimension is below 3, "
        "revise the problematic slides and output the complete revised manuscript. "
        "Otherwise, output QUALITY_CHECK_PASSED followed by the unchanged manuscript. "
        "Preserve valid paper figure tokens exactly; never introduce a FIG token that is not in the Valid Paper Figure Tokens list."
    )

    pass4_messages = [
        LLMMessage.system(pass4_system),
        LLMMessage.user("\n".join(pass4_user_parts)),
    ]
    _debug_write_messages(debug_dir, "research_pass4_prompt.md", pass4_messages)
    pass4_response = await llm.chat(
        pass4_messages,
        model,
        temperature=0.3,
        max_tokens=DEEPSEEK_MAX_TOKENS if is_deepseek else None,
    )
    _debug_write_text(debug_dir, "research_pass4_response.md", pass4_response.content)
    final_output = _extract_manuscript_from_review(pass4_response.content, manuscript)
    final_error = _manuscript_validation_error(final_output, paper, num_pages, detail_level)
    manuscript_error = _manuscript_validation_error(manuscript, paper, num_pages, detail_level)
    if final_error and not manuscript_error:
        logger.warning("Pass 4 changed manuscript structure; keeping Pass 3 output: %s", final_error)
        final_output = manuscript
    _debug_write_text(debug_dir, "research_final_manuscript.md", final_output)
    logger.info("Research Pass 4 complete. Final manuscript: %d chars", len(final_output))
    if on_progress:
        on_progress("Pass 4/4 — Quality review", 0.28)

    return final_output


def _extract_manuscript_from_review(review_output: str, original_manuscript: str) -> str:
    """Extract the final manuscript from Pass 4 review output.

    The review may output:
    1. "QUALITY_CHECK_PASSED" followed by the manuscript
    2. A revised manuscript (after the assessment section)
    3. Just the assessment with no manuscript changes needed

    In all cases, we try to extract the manuscript (content after the last `---`
    slide separator pattern, or the full content if it looks like a manuscript).
    """
    # If the review explicitly passed, keep Pass 3's clean manuscript. Some
    # models prepend a scoring report before QUALITY_CHECK_PASSED and then echo
    # the unchanged manuscript; using the original avoids leaking that report
    # into downstream slide splitting.
    if "QUALITY_CHECK_PASSED" in review_output:
        return original_manuscript

    marker_extract = _extract_after_manuscript_marker(review_output)
    if marker_extract:
        return marker_extract

    slide_heading_extract = _extract_from_first_numbered_slide(review_output)
    if slide_heading_extract:
        return slide_heading_extract

    # If the review contains a full revised manuscript (has slide separators)
    if review_output.count("---") >= 2:
        # Try to find where the manuscript starts (after the assessment)
        # Look for the first ## heading followed by --- pattern
        lines = review_output.split("\n")
        manuscript_start = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("## ") and i > 0 and not _REVIEW_HEADING_RE.match(stripped):
                # Check if there's a --- separator within the next 30 lines
                for j in range(i, min(i + 30, len(lines))):
                    if lines[j].strip() == "---":
                        manuscript_start = i
                        break
                if manuscript_start is not None:
                    break

        if manuscript_start is not None:
            return "\n".join(lines[manuscript_start:]).strip()

    # Fallback: if we can't parse the review output, return the original
    logger.warning("Could not extract revised manuscript from review; using original")
    return original_manuscript


def _extract_after_manuscript_marker(text: str) -> str | None:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not _MANUSCRIPT_MARKER_RE.match(line.strip()):
            continue
        start = i + 1
        while start < len(lines) and (
            not lines[start].strip()
            or lines[start].strip() == "---"
            or lines[start].strip() == "QUALITY_CHECK_PASSED"
        ):
            start += 1
        if start < len(lines):
            return "\n".join(lines[start:]).strip()
    return None


def _extract_from_first_numbered_slide(text: str) -> str | None:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _SLIDE_HEADING_RE.match(line.strip()):
            return "\n".join(lines[i:]).strip()
    return None


def _target_slides_guidance(
    num_pages: int | None,
    detail_level: str = "normal",
) -> str:
    delimiter_rule = (
        "Use standalone `---` lines only as slide delimiters; for an exact target, "
        "the delimiter count must be one less than the slide count."
    )
    return f"{page_type_budget_guidance(num_pages, detail_level)}\n{delimiter_rule}"


# ── Legacy single-pass for backward compat (revise pipeline) ────────────────


async def revise_manuscript(
    manuscript: str,
    llm: LLMProvider,
    model: str,
    *,
    feedback_history: list[str],
    language: str = "en",
    detail_level: str = "normal",
    target_pages: list[int] | None = None,
    allow_structure_changes: bool = False,
) -> str:
    """Revise an existing manuscript using user feedback.

    When ``allow_structure_changes`` is false, preserve slide order/count and
    revise only the requested scope. When true, the model may insert, remove,
    or reorder slides to satisfy the feedback.
    """
    system_prompt = LEGACY_PROMPT.read_text(encoding="utf-8")
    target_pages = sorted({page for page in (target_pages or []) if page > 0})

    scope_guidance = (
        "Revise only the requested slide pages. Keep all other slides unchanged unless a "
        "small consistency edit is strictly necessary."
        if target_pages
        else "Revise the full deck while preserving its overall structure unless the feedback requires otherwise."
    )
    structure_guidance = (
        "You MAY change slide count, insert new slides, delete slides, split dense slides, or reorder slides "
        "when that is the best way to satisfy the feedback."
        if allow_structure_changes
        else "You MUST preserve slide count and slide order. Do not insert, delete, or reorder slides."
    )

    feedback_block = "\n\n".join(
        f"### Round {index}\n{feedback.strip()}"
        for index, feedback in enumerate(feedback_history, start=1)
        if feedback.strip()
    )

    user_prompt = (
        f"## Existing Manuscript\n\n{manuscript}\n\n"
        f"## Target Language\n\n{language}\n\n"
        f"## Detail Level\n\n{detail_level}\n\n"
        f"## Requested Scope\n\n"
        f"- Target pages: {', '.join(map(str, target_pages)) if target_pages else 'all pages'}\n"
        f"- Scope rule: {scope_guidance}\n"
        f"- Structure rule: {structure_guidance}\n\n"
        f"## User Feedback History\n\n{feedback_block or 'No feedback provided.'}\n\n"
        "Revise the manuscript and output the full updated slide manuscript only. "
        "Keep `---` separators between slides."
    )

    response: LLMResponse = await llm.chat(
        [LLMMessage.system(system_prompt), LLMMessage.user(user_prompt)],
        model,
        temperature=0.4,
        max_tokens=16384,
    )
    return response.content
