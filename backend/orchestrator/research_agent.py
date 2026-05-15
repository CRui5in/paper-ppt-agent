"""Research agent: multi-pass deep analysis of academic papers for slide manuscripts.

Architecture:
    Pass 1 — Deep Reading: structured critical analysis of the paper.
             Optional external enrichment (related papers, citations, web
             discussions) is injected here so the LLM can position the paper
             against existing literature and sharpen the gap analysis.
    Pass 2 — Narrative Arc: design a story-driven slide plan.
    Pass 3 — Manuscript: generate the actual slide manuscript.
    Pass 4 — Self-Review: evaluate quality and revise if needed.

Deep research is opt-in. Without it, the agent first writes a lightweight
paper brief and then writes the manuscript from that brief; external
enrichment can still be injected as context.
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
    strip_page_type_metadata,
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
DEEPSEEK_RESEARCH_MAX_TOKENS = DEEPSEEK_MAX_TOKENS
QUALITY_THRESHOLD = 28  # out of 35 (7 dimensions × 5 points each)
MAX_MANUSCRIPT_ATTEMPTS = 3
SINGLE_PASS_SYSTEM_PROMPT = (
    "You write slide-structured manuscripts from academic papers. "
    "Extract the paper's problem, method, evidence, and takeaway; turn them into "
    "a clear slide sequence. Output only the manuscript, separated by standalone "
    "`---` lines."
)
LIGHTWEIGHT_BRIEF_SYSTEM_PROMPT = (
    "You are preparing a compact paper brief for a slide writer. Extract concrete "
    "paper substance: problem, mechanism, equations, figures/tables, experiments, "
    "numbers, limitations, and implications. Do not write slides yet."
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
_STRUCTURAL_PAGE_TYPES = frozenset({"cover", "chapter", "toc", "ending"})
_STRUCTURAL_LIST_RE = re.compile(r"(?m)^\s*(?:[-*•]|\d+[\.)、])\s+\S+")
_STRUCTURAL_LABEL_RE = re.compile(
    r"(?i)(核心问题|本章看点|本章关注|本节关注|三个问题|主要结果|结果亮点|"
    r"模型规格|关键目标|贡献|方法要点|实验看点|chapter\s+highlights|key\s+points)"
)
_EVIDENCE_MARKER_RE = re.compile(
    r"(?i)(\d|%|table|figure|fig\.?|equation|formula|experiment|result|metric|"
    r"dataset|baseline|sota|bleu|rouge|accuracy|f1|auc|ap\b|loss|latency|"
    r"ablation|complexity|parameter|token|图|表|公式|实验|结果|指标|数据集|"
    r"基线|消融|复杂度|参数|训练|推理|精度|召回|误差)"
)
_EXPLANATION_MARKER_RE = re.compile(
    r"(?i)(because|therefore|so that|this means|this shows|this suggests|"
    r"implies|enables|reduces|increases|compared|mechanism|design choice|"
    r"assumption|limitation|trade[- ]?off|why|because of|原因|因此|所以|"
    r"意味着|说明|表明|显示|支撑|导致|使得|解决|降低|提升|相比|机制|"
    r"设计|假设|局限|权衡|启示|关键在于)"
)
_DEPTH_CHAR_THRESHOLDS = {
    "normal": 70,
    "high": 105,
    "very_high": 130,
}


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


async def _run_lightweight_paper_brief(
    paper: ParsedPaper,
    llm: LLMProvider,
    model: str,
    *,
    instruction: str = "",
    language: str = "en",
    detail_level: str = "normal",
    enrichment_block: str = "",
    is_deepseek: bool = False,
    debug_dir: Path | None = None,
) -> str:
    """Extract a compact, evidence-first brief for the non-deep manuscript path."""
    user_parts = [
        f"## Paper Content\n\n{paper.to_markdown()}",
        f"\n## Target Language\n\n{language}\n\n{_language_guidance(language)}",
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
        "\n\n## Brief Requirements\n\n"
        "Output Markdown only. Keep it compact but concrete; do not write the slide manuscript yet.\n"
        "Include these sections:\n"
        "1. Core problem and why it matters.\n"
        "2. Main method/mechanism, including important equations or architecture choices.\n"
        "3. Key evidence: exact metrics, datasets, table/figure references, or qualitative findings.\n"
        "4. Figure/table/formula inventory: which extracted paper figures or formulas are worth using and why.\n"
        "5. Limitations, assumptions, and implications.\n"
        "6. Slide substance inventory: reusable claim/evidence/so-what bullets for content slides.\n\n"
        "Avoid generic phrases such as 'the paper proposes a new method' unless you name the method, mechanism, and evidence."
    )

    messages = [
        LLMMessage.system(LIGHTWEIGHT_BRIEF_SYSTEM_PROMPT),
        LLMMessage.user("\n".join(user_parts)),
    ]
    _debug_write_messages(debug_dir, "paper_brief_prompt.md", messages)
    response = await llm.chat(
        messages,
        model,
        temperature=0.25,
        max_tokens=DEEPSEEK_RESEARCH_MAX_TOKENS if is_deepseek else None,
    )
    brief = response.content.strip()
    _debug_write_text(debug_dir, "paper_brief.md", brief)
    return brief


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
    paper_brief: str = "",
    is_deepseek: bool = False,
    debug_dir: Path | None = None,
) -> str:
    """Generate a slide manuscript for the non-deep mode."""
    user_parts = [
        f"## Paper Content\n\n{paper.to_markdown()}",
        f"\n## Target Language\n\n{language}\n\n{_language_guidance(language)}",
        f"\n## Target Slides\n\n{_target_slides_guidance(num_pages, detail_level)}",
        f"\n## Detail Level\n\n{detail_level}\n\n{DETAIL_GUIDANCE.get(detail_level, DETAIL_GUIDANCE['normal'])}",
    ]
    if paper_brief:
        user_parts.append(
            "\n## Paper Brief (authoritative substance for content slides)\n\n"
            f"{paper_brief}"
        )
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
        "\n\n## Internal Structure Planning Contract\n\n"
        "Before writing the manuscript, internally allocate the deck into coherent "
        "chapter groups. A chapter/transition slide is allowed only when it introduces "
        "at least 2 following `content` slides. If a topic has only 1 content slide, "
        "do not create a chapter divider for it; merge that topic into a neighboring "
        "chapter and make the slide `content`. Never put labels such as `核心问题` / "
        "`本章看点`, bullets, figures, or evidence blocks on `chapter` slides."
    )
    user_parts.append(
        "\n\n## Content Depth Contract\n\n"
        "Every `content` slide must include: (1) a clear claim sentence, preferably bold; "
        "(2) paper-grounded evidence such as a metric, dataset, table/figure/formula reference, "
        "architecture detail, ablation finding, or mechanism detail; and (3) a short explanation "
        "of why that evidence matters. For `high` and `very_high`, prefer 3-5 concrete content "
        "blocks per content slide when readable. Do not invent evidence; if the paper lacks a "
        "number, use mechanism-level evidence from the paper brief."
    )
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
            depth_feedback = _manuscript_depth_feedback(response_content, detail_level)
            if not depth_feedback:
                return response_content

            rewrite_messages = list(base_messages)
            rewrite_messages.extend(
                [
                    LLMMessage.assistant(response_content),
                    LLMMessage.user(
                        _depth_retry_prompt(depth_feedback, num_pages, detail_level)
                    ),
                ]
            )
            _debug_write_messages(
                debug_dir,
                "research_depth_rewrite_prompt.md",
                rewrite_messages,
            )
            rewrite_response = await llm.chat(
                rewrite_messages,
                model,
                temperature=0.35,
                max_tokens=DEEPSEEK_MAX_TOKENS if is_deepseek else None,
            )
            revised = rewrite_response.content
            _debug_write_text(debug_dir, "research_depth_rewrite_response.md", revised)
            revised_error = _manuscript_validation_error(
                revised,
                paper,
                num_pages,
                detail_level,
            )
            if not revised_error:
                return revised
            _debug_write_text(
                debug_dir,
                "research_depth_rewrite_rejected.txt",
                revised_error,
            )
            return response_content

    logger.warning("Single-pass manuscript structure invalid after retry; continuing: %s", last_error)
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
        structural_content_error = _structural_page_content_error(
            page,
            index,
            page_type,
        )
        if structural_content_error:
            return structural_content_error

    if missing_meta:
        return "missing page_type metadata on slides " + ", ".join(missing_meta[:8])

    if num_pages:
        expected_count = _expected_slide_count(num_pages, detail_level)
        if len(pages) != expected_count:
            return f"expected {expected_count} slides, got {len(pages)}"

        expected_budget = page_type_budget(num_pages, detail_level)
        if seen["cover"] != expected_budget["cover"]:
            return f"expected {expected_budget['cover']} cover slide(s), got {seen['cover']}"
        if seen["ending"] != expected_budget["ending"]:
            return f"expected {expected_budget['ending']} ending slide(s), got {seen['ending']}"
        if expected_budget["content"] > 0 and seen["content"] < 1:
            return "expected at least 1 content slide"
    else:
        min_pages, max_pages = auto_slide_range(detail_level)
        if not min_pages <= len(pages) <= max_pages:
            return f"expected {min_pages}-{max_pages} slides, got {len(pages)}"
        if seen["cover"] != 1:
            return f"expected 1 cover slide, got {seen['cover']}"
        if seen["ending"] != 1:
            return f"expected 1 ending slide, got {seen['ending']}"
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

    chapter_group_error = _chapter_group_structure_error(pages)
    if chapter_group_error:
        return chapter_group_error
    return None


def _structural_page_content_error(
    page: str,
    index: int,
    page_type: str,
) -> str | None:
    if page_type not in _STRUCTURAL_PAGE_TYPES:
        return None

    visible = strip_page_type_metadata(page)
    if "[[FIG:" in visible:
        return f"slide {index} ({page_type}) is structural but contains a paper figure token"

    if page_type in {"cover", "chapter"} and _STRUCTURAL_LIST_RE.search(visible):
        return (
            f"slide {index} ({page_type}) is structural but contains bullet or "
            "numbered-list content"
        )

    non_heading_lines = [
        line.strip()
        for line in visible.splitlines()
        if line.strip()
        and not line.lstrip().startswith("#")
        and not line.strip().startswith("<!--")
    ]
    non_heading_text = "\n".join(non_heading_lines)
    if page_type in {"cover", "chapter"} and _STRUCTURAL_LABEL_RE.search(non_heading_text):
        return (
            f"slide {index} ({page_type}) is structural but contains content-block "
            "labels such as 核心问题/本章看点"
        )

    if page_type == "cover" and len(non_heading_lines) > 2:
        return "cover slide must contain only title, optional subtitle, source/authors, and at most one thesis line"
    if page_type == "chapter" and len(non_heading_lines) > 2:
        return "chapter slide must contain only title and at most 1-2 orientation phrases"
    return None


def _chapter_group_structure_error(pages: list[str]) -> str | None:
    if len(pages) < 12:
        return None

    groups: list[tuple[int, int]] = []
    current_chapter_page: int | None = None
    current_content_count = 0
    for index, page in enumerate(pages, start=1):
        page_type = extract_page_type(page)
        if page_type == "chapter":
            if current_chapter_page is not None:
                groups.append((current_chapter_page, current_content_count))
            current_chapter_page = index
            current_content_count = 0
        elif page_type == "content" and current_chapter_page is not None:
            current_content_count += 1
        elif page_type == "ending" and current_chapter_page is not None:
            groups.append((current_chapter_page, current_content_count))
            current_chapter_page = None
            current_content_count = 0

    if current_chapter_page is not None:
        groups.append((current_chapter_page, current_content_count))

    for chapter_page, content_count in groups:
        if content_count < 2:
            return (
                f"chapter slide {chapter_page} introduces only {content_count} "
                "content slide(s); merge it into a neighboring section or add "
                "at least 2 following content slides"
            )
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


def _visible_body_lines(page: str) -> list[str]:
    visible = strip_page_type_metadata(page)
    lines: list[str] = []
    for line in visible.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--") or stripped.startswith("[[FIG:"):
            continue
        if stripped.lstrip().startswith("#"):
            continue
        lines.append(stripped)
    return lines


def _page_heading(page: str) -> str:
    visible = strip_page_type_metadata(page)
    match = re.search(r"(?m)^#{1,6}\s+(.+?)\s*$", visible)
    if match:
        return match.group(1).strip()
    return "Untitled"


def _content_depth_issues(page: str, detail_level: str) -> list[str]:
    visible = strip_page_type_metadata(page)
    body_lines = _visible_body_lines(page)
    body_text = " ".join(body_lines)
    compact_text = re.sub(r"[\s*_`#>\-\[\]():：，。,.]+", "", body_text)
    threshold = _DEPTH_CHAR_THRESHOLDS.get(detail_level, _DEPTH_CHAR_THRESHOLDS["normal"])

    has_claim = bool(re.search(r"\*\*[^*]{10,}\*\*", visible)) or any(
        len(line.lstrip("-*•0123456789.、) ")) >= 24
        and not line.lstrip().startswith(("-", "*", "•"))
        for line in body_lines[:3]
    )
    has_evidence = bool(_FIG_TOKEN_RE.search(visible) or _EVIDENCE_MARKER_RE.search(visible))
    has_explanation = bool(_EXPLANATION_MARKER_RE.search(visible))

    issues: list[str] = []
    if not has_claim:
        issues.append("missing a clear claim sentence")
    if not has_evidence:
        issues.append("missing paper-grounded evidence")
    if not has_explanation:
        issues.append("missing explanation / so-what")
    if len(compact_text) < threshold:
        issues.append(f"too little slide substance ({len(compact_text)} chars)")
    return issues if len(issues) >= 2 else []


def _manuscript_depth_feedback(manuscript: str, detail_level: str = "normal") -> str | None:
    shallow: list[str] = []
    content_count = 0
    for index, page in enumerate(split_manuscript_pages(manuscript), start=1):
        if extract_page_type(page) != "content":
            continue
        content_count += 1
        issues = _content_depth_issues(page, detail_level)
        if issues:
            title = _page_heading(page)
            shallow.append(
                f"- Slide {index} `{title}`: " + "; ".join(issues[:3])
            )

    if not shallow:
        return None

    listed = "\n".join(shallow[:6])
    more = f"\n- ... and {len(shallow) - 6} more shallow content slide(s)" if len(shallow) > 6 else ""
    return (
        f"{len(shallow)} of {content_count} content slide(s) look too shallow.\n"
        f"{listed}{more}"
    )


def _depth_retry_prompt(
    feedback: str,
    num_pages: int | None,
    detail_level: str = "normal",
) -> str:
    return (
        "The manuscript structure is valid, but several content slides are too shallow:\n\n"
        f"{feedback}\n\n"
        "Rewrite the full slide manuscript once. Preserve slide count, slide order, "
        "page_type metadata, cover/chapter/ending minimalism, and valid FIG tokens. "
        "Only strengthen content slides: add paper-grounded claim/evidence/so-what "
        "substance from the Paper Brief and Paper Content. Do not invent metrics or "
        "figures. If a slide lacks numeric evidence, use concrete mechanism details, "
        "equations, assumptions, or experimental logic from the paper instead.\n"
        f"{page_type_budget_guidance(num_pages, detail_level)}"
    )


def _structure_retry_prompt(
    error: str,
    num_pages: int | None,
    detail_level: str = "normal",
) -> str:
    return (
        "The previous manuscript did not match the slide structure contract: "
        f"{error}.\n\n"
        "Regenerate the full slide manuscript only. First rebuild the chapter plan internally, then write the manuscript.\n"
        "If the error mentions paper figure tokens, replace invalid tokens with exact tokens from the Valid Paper Figure Tokens list, or omit the real figure when no listed token matches.\n"
        "Structural-page rules are strict: cover/chapter/ending slides must not contain bullet lists, numbered question lists, KPI/result blocks, paper figures, or labeled sections such as `核心问题` / `本章看点`. "
        "All chapter slides must use the same manuscript shape: one chapter title plus an optional short subtitle/orientation phrase only; put all detailed questions and evidence on following content slides.\n"
        "If a chapter divider would introduce only 0 or 1 content slide, remove that divider from the chapter plan and merge the topic into a neighboring chapter. "
        "If a planned chapter slide contains `核心问题`, `本章看点`, bullets, figures, or evidence, it is not a chapter slide; rewrite that material as a `content` slide and keep chapter dividers minimal.\n"
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
            False, generate a lightweight paper brief before the manuscript.
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
        logger.info("Research lightweight mode: paper brief...")
        if on_progress:
            on_progress("Preparing paper brief", 0.20)
        paper_brief = await _run_lightweight_paper_brief(
            paper,
            llm,
            model,
            instruction=instruction,
            language=language,
            detail_level=detail_level,
            enrichment_block=enrichment_block,
            is_deepseek=is_deepseek,
            debug_dir=debug_dir,
        )
        logger.info("Research lightweight brief complete (%d chars)", len(paper_brief))
        if on_progress:
            on_progress("Generating manuscript from brief", 0.24)
        manuscript = await _run_single_pass_analysis(
            paper,
            llm,
            model,
            instruction=instruction,
            num_pages=num_pages,
            language=language,
            detail_level=detail_level,
            enrichment_block=enrichment_block,
            paper_brief=paper_brief,
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
        "Follow the narrative arc plan and the information aesthetics principles. "
        "Before writing, internally verify the chapter plan: every chapter divider "
        "must introduce at least 2 following content slides; topics with only 1 "
        "content slide must be merged into a neighboring chapter instead of getting "
        "their own divider. Keep chapter slides minimal; detailed labels such as "
        "`核心问题` / `本章看点`, bullets, figures, metrics, and evidence belong on "
        "`content` slides."
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
    elif final_error:
        logger.warning("Final manuscript validation failed after retries; continuing: %s", final_error)
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
