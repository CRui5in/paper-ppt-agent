"""Research agent: multi-pass deep analysis of academic papers for slide manuscripts.

Architecture:
    Pass 1 — Deep Reading: structured critical analysis of the paper.
             Optional external enrichment (related papers, citations, web
             discussions) is injected here so the LLM can position the paper
             against existing literature and sharpen the gap analysis.
    Pass 2 — Narrative Arc: design a story-driven slide plan.
    Pass 3 — Manuscript: generate the actual slide manuscript.
    Pass 4 — Self-Review: evaluate quality and revise if needed.

Deep research is opt-in. Without it, the agent writes the manuscript directly
from the full parsed paper; external enrichment can still be injected as
context. Compact paper memory is retained only as a fallback/debug source when
a provider rejects the full context.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from backend.llm import LLMMessage, LLMProvider, LLMResponse
from backend.orchestrator.provider_guidance import is_deepseek_provider
from backend.orchestrator.manuscript import (
    auto_slide_range,
    extract_page_type,
    normalize_manuscript_slide_delimiters,
    page_type_budget,
    page_type_budget_guidance,
    split_manuscript_pages,
    strip_page_type_metadata,
)
from backend.orchestrator.provider_memory import ProviderMemory
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
MAX_MANUSCRIPT_ATTEMPTS = 3
SINGLE_PASS_SYSTEM_PROMPT = (
    "You write slide-structured manuscripts from research source corpora. "
    "Extract the sources' problems, methods, evidence, agreements, differences, "
    "and takeaways; turn them into "
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
_INTERNAL_EVIDENCE_ID_RE = re.compile(r"`?\bs\d{2,}[ct]\d{2,}\b`?", re.IGNORECASE)
_SOURCE_CITATION_RE = re.compile(
    r"<!--\s*sources?\s*:\s*([^>]+?)\s*-->",
    re.IGNORECASE,
)
_SOURCE_REF_RE = re.compile(r"\bS\d+\b", re.IGNORECASE)


def _is_multi_source_corpus(paper: ParsedPaper) -> bool:
    return paper.source_type == "mixed" and len(paper.source_manifest) > 1


def _multi_source_contract(paper: ParsedPaper) -> str:
    if not _is_multi_source_corpus(paper):
        return ""
    rows = []
    for index, source in enumerate(paper.source_manifest, 1):
        ref = str(source.get("ref") or f"S{index}")
        label = str(source.get("label") or "Untitled source")
        title = str(source.get("title") or "").strip()
        kind = str(source.get("kind") or "unknown")
        title_suffix = f" — {title}" if title and title != label else ""
        rows.append(f"- {ref}: {label}{title_suffix} ({kind})")
    return (
        "## Multi-Source Synthesis Contract\n\n"
        "Treat the entries below as peer research inputs, not as one main paper plus "
        "attachments. Build the deck around a cross-source question and synthesize "
        "agreements, differences, complementary evidence, and limitations. Do not let "
        "source order determine emphasis. Every successfully parsed source must affect "
        "the narrative, and at least one content slide must combine evidence from two "
        "or more sources.\n\n"
        "At the end of every content slide, add one invisible provenance comment using "
        "only the source references that materially support that slide, for example "
        "`<!-- sources: S1, S3 -->`. These comments are required for coverage validation "
        "but are not visible slide text.\n\n"
        + "\n".join(rows)
    )


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


def _sanitize_internal_evidence_markers(manuscript: str) -> str:
    def replacement(match: re.Match[str]) -> str:
        token = match.group(0).strip("`").lower()
        return "paper table" if "t" in token else "paper passage"

    cleaned = _INTERNAL_EVIDENCE_ID_RE.sub(replacement, manuscript)
    cleaned = re.sub(
        r"(?:对应|基于|见|来源于)?\s*表\s+paper table",
        "对应论文表格",
        cleaned,
    )
    cleaned = re.sub(
        r"(?:对应|基于|见|来源于)?\s*段落\s+paper passage",
        "对应论文段落",
        cleaned,
    )
    return cleaned


def _is_context_window_error(exc: BaseException) -> bool:
    text = f"{exc.__class__.__name__}: {exc}".lower()
    context_terms = (
        "context_length",
        "context length",
        "context window",
        "maximum context",
        "max context",
        "too many tokens",
        "tokens exceed",
        "input tokens",
        "prompt tokens",
        "request too large",
        "payload too large",
        "string too long",
    )
    return any(term in text for term in context_terms)


async def _repair_manuscript_structure_if_needed(
    manuscript: str,
    source_text: str,
    llm: LLMProvider,
    model: str,
    *,
    language: str,
    detail_level: str,
    is_deepseek: bool,
    paper: ParsedPaper,
    num_pages: int | None,
    debug_dir: Path | None,
    debug_prefix: str,
) -> str:
    initial_error = _manuscript_validation_error(
        manuscript,
        paper,
        num_pages,
        detail_level,
    )
    if not initial_error:
        return manuscript

    current = manuscript
    current_error = initial_error
    for repair_attempt in range(1, 3):
        attempt_prefix = (
            debug_prefix if repair_attempt == 1 else f"{debug_prefix}_attempt{repair_attempt}"
        )
        messages = [
            LLMMessage.system(
                "You repair only the machine-readable structure of a slide manuscript. "
                "Preserve the visible content and evidence as closely as possible. Fix "
                "page separators, page_type metadata, required structural roles, page "
                "count, ordering, and invalid FIG tokens. Output only the complete "
                "repaired manuscript."
            ),
            LLMMessage.user(
                f"## Validation Error\n{current_error}\n\n"
                f"## Target Language\n{language}\n\n"
                f"## Target Slides\n{_target_slides_guidance(num_pages, detail_level)}\n\n"
                "## Repair Rules\n"
                "- Slide 1 is cover and slide 2 is the mandatory table of contents.\n"
                "- Preserve the existing visible wording; do not perform content-quality edits.\n"
                "- Do not add new claims, figures, metrics, or source attributions.\n"
                "- Use only exact FIG tokens available in the source working memory.\n\n"
                f"## Source Working Memory (for FIG token validation only)\n{source_text}\n\n"
                f"## Manuscript To Repair\n{current}"
            ),
        ]
        _debug_write_messages(debug_dir, f"{attempt_prefix}_prompt.md", messages)
        response = await llm.chat(
            messages,
            model,
            temperature=0.15,
            max_tokens=DEEPSEEK_MAX_TOKENS if is_deepseek else None,
        )
        _debug_write_text(debug_dir, f"{attempt_prefix}_response.md", response.content)
        current = normalize_manuscript_slide_delimiters(response.content)
        current_error = _manuscript_validation_error(
            current,
            paper,
            num_pages,
            detail_level,
        )
        if not current_error:
            return current

        _debug_write_text(debug_dir, f"{attempt_prefix}_rejected.txt", current_error)

    deterministic = _coerce_extra_slides_if_safe(
        current,
        paper,
        num_pages,
        detail_level,
    )
    if deterministic is not None:
        _debug_write_text(debug_dir, f"{debug_prefix}_deterministic_response.md", deterministic)
        return deterministic

    raise ValueError(
        "Slide manuscript structure repair failed: "
        f"initial error: {initial_error}; repair error: {current_error}"
    )


def _coerce_extra_slides_if_safe(
    manuscript: str,
    paper: ParsedPaper,
    num_pages: int | None,
    detail_level: str,
) -> str | None:
    if not num_pages:
        return None
    expected_count = _expected_slide_count(num_pages, detail_level)
    pages = split_manuscript_pages(manuscript)
    if len(pages) <= expected_count:
        return None

    while len(pages) > expected_count:
        merge_pair = _extra_slide_merge_pair(pages)
        if merge_pair is None:
            return None
        target_index, source_index = merge_pair
        pages[target_index] = _merge_extra_slide(
            pages[target_index],
            pages[source_index],
        )
        del pages[source_index]

    candidate = "\n\n---\n\n".join(page.strip() for page in pages if page.strip())
    if _manuscript_validation_error(candidate, paper, num_pages, detail_level):
        return None
    return candidate


def _extra_slide_merge_pair(pages: list[str]) -> tuple[int, int] | None:
    if len(pages) <= 3:
        return None
    last_content = len(pages) - 1
    if extract_page_type(pages[-1]) == "ending":
        last_content -= 1
    for idx in range(last_content, 1, -1):
        if (
            extract_page_type(pages[idx]) == "content"
            and extract_page_type(pages[idx - 1]) == "content"
        ):
            return (idx - 1, idx)
    for idx in range(2, max(last_content, 2)):
        if (
            extract_page_type(pages[idx]) == "content"
            and extract_page_type(pages[idx + 1]) == "content"
        ):
            return (idx, idx + 1)
    return None


def _merge_extra_slide(target: str, source: str) -> str:
    source_visible = strip_page_type_metadata(source).strip()
    source_visible = re.sub(r"(?m)^#{1,3}\s+", "### ", source_visible)
    if not source_visible:
        return target
    return (
        target.rstrip()
        + "\n\n### Additional Material From Merged Slide\n\n"
        + source_visible
    ).strip()


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
    paper_markdown: str | None = None,
    repair_source_markdown: str | None = None,
    debug_dir: Path | None = None,
) -> str:
    """Generate a slide manuscript for the non-deep mode."""
    paper_context = paper_markdown or paper.to_markdown()
    corpus_label = "Source Corpus Working Memory" if _is_multi_source_corpus(paper) else "Paper Working Memory"
    user_parts = [
        f"## {corpus_label}\n\n{paper_context}",
        f"\n## Target Language\n\n{language}\n\n{_language_guidance(language)}",
        f"\n## Target Slides\n\n{_target_slides_guidance(num_pages, detail_level)}",
    ]
    multi_source_contract = _multi_source_contract(paper)
    if multi_source_contract:
        user_parts.append(f"\n{multi_source_contract}")
    figure_inventory = _figure_token_inventory_block(paper)
    if figure_inventory:
        user_parts.append(f"\n{figure_inventory}")
    if enrichment_block:
        user_parts.append(f"\n{enrichment_block}")
    if instruction:
        user_parts.append(f"\n## User Instruction\n\n{instruction}")
    user_parts.append(
        "\n\n## Internal Structure Planning Contract\n\n"
        "Before writing the manuscript, internally allocate the deck into coherent "
        "chapter groups based on your understanding of the source corpus, not a "
        "fixed template. Chapter titles should reflect the paper's real conceptual "
        "blocks, argument stages, mechanisms, evidence clusters, or contribution "
        "logic. Slide 2 is the mandatory table of contents and must list the final "
        "chapter titles in narrative order. Keep chapter dividers concise and put "
        "detailed evidence on content slides."
    )
    user_parts.append(
        "\n\n## Content Depth Contract\n\n"
        "Every `content` slide must include: (1) a clear claim sentence, preferably bold; "
        "(2) paper-grounded evidence appropriate to the discipline, such as a metric, dataset, "
        "quotation, case, observation, archival source, theorem, table/figure/formula reference, "
        "method detail, comparison, or reasoning step; and (3) a short explanation of why that "
        "evidence matters. Choose the number of content blocks from the current page's argument "
        "and visual needs. Do not invent evidence and do not force numeric evidence "
        "onto qualitative, theoretical, historical, or interpretive work."
    )
    user_parts.append(
        "\n\n## Exact Evidence Coverage Contract\n\n"
        "When the paper provides concrete headline evidence, preserve representative exact "
        "values in the manuscript instead of replacing them with generic phrases such as "
        "'strong performance', 'competitive results', or 'significant improvement'. Cover "
        "the main evidence streams that support the deck's story: problem-defining data "
        "distributions, method/efficiency costs, benchmark headline numbers, ablation "
        "deltas, and important limitations. For table-heavy papers, choose a compact set "
        "of exact values across the major benchmark or experiment groups rather than "
        "copying the whole table or omitting the numbers."
    )
    user_parts.append(
        "\n\n## Visible Source Anchor Contract\n\n"
        "Do not expose internal evidence-card IDs or retrieval IDs such as `s20t03`, "
        "`s22c011`, or similar `s##c##` / `s##t##` markers in the manuscript. "
        "Use human-readable anchors such as paper section names, public table/figure "
        "labels, or `paper table` / `paper figure` when the exact label is unavailable."
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
        response_content = normalize_manuscript_slide_delimiters(response.content)
        _debug_write_text(
            debug_dir,
            "research_single_pass_response.md"
            if attempt == 1
            else f"research_single_pass_response_attempt{attempt}.md",
            response.content,
        )
        if response_content != response.content.strip():
            _debug_write_text(
                debug_dir,
                "research_single_pass_response_normalized.md"
                if attempt == 1
                else f"research_single_pass_response_attempt{attempt}_normalized.md",
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

    logger.warning("Single-pass manuscript structure invalid after retry; repairing: %s", last_error)
    return await _repair_manuscript_structure_if_needed(
        response_content,
        repair_source_markdown or paper_context,
        llm,
        model,
        language=language,
        detail_level=detail_level,
        is_deepseek=is_deepseek,
        paper=paper,
        num_pages=num_pages,
        debug_dir=debug_dir,
        debug_prefix="research_single_pass_structure_repair",
    )


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
    if not pages:
        return "no parseable slides found"

    seen = {"cover": 0, "toc": 0, "chapter": 0, "content": 0, "ending": 0}
    missing_meta = []
    for index, page in enumerate(pages, start=1):
        if not re.search(r"(?im)^\s*<!--\s*page_type\s*:", page):
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
        if seen["cover"] != expected_budget["cover"]:
            return f"expected {expected_budget['cover']} cover slide(s), got {seen['cover']}"
        if seen["toc"] != expected_budget["toc"]:
            return f"expected {expected_budget['toc']} table-of-contents slide(s), got {seen['toc']}"
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
        if seen["toc"] != 1:
            return f"expected 1 table-of-contents slide, got {seen['toc']}"
        if seen["ending"] != 1:
            return f"expected 1 ending slide, got {seen['ending']}"
        if seen["content"] < 1:
            return "expected at least 1 content slide"

    if seen["cover"] and extract_page_type(pages[0]) != "cover":
        return "cover slide must be slide 1"
    if seen["toc"] and (len(pages) < 2 or extract_page_type(pages[1]) != "toc"):
        return "table-of-contents slide must be slide 2"
    if seen["ending"] and extract_page_type(pages[-1]) != "ending":
        return "ending slide must be the final slide"
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


def _manuscript_source_coverage_error(
    manuscript: str,
    paper: ParsedPaper,
) -> str | None:
    if not _is_multi_source_corpus(paper):
        return None

    valid_refs = {
        str(source.get("ref") or f"S{index}").upper()
        for index, source in enumerate(paper.source_manifest, 1)
        if str(source.get("status") or "ok") == "ok"
    }
    if len(valid_refs) < 2:
        return None

    citation_groups = []
    for raw_group in _SOURCE_CITATION_RE.findall(manuscript):
        refs = {ref.upper() for ref in _SOURCE_REF_RE.findall(raw_group)}
        valid_group = refs.intersection(valid_refs)
        if valid_group:
            citation_groups.append(valid_group)

    used_refs = set().union(*citation_groups) if citation_groups else set()
    required_count = len(valid_refs)
    missing = sorted(valid_refs - used_refs)
    errors: list[str] = []
    if len(used_refs) < required_count:
        errors.append(
            f"multi-source coverage is {len(used_refs)}/{len(valid_refs)}; "
            f"cite at least {required_count} sources using invisible "
            "`<!-- sources: S1, S2 -->` comments"
        )
        if missing:
            errors.append("missing source references: " + ", ".join(missing))
    if citation_groups and not any(len(group) >= 2 for group in citation_groups):
        errors.append(
            "no content slide combines multiple sources; at least one provenance "
            "comment must cite two or more materially used sources"
        )
    elif not citation_groups:
        errors.append("no multi-source provenance comments were found")
    return "; ".join(errors) or None


def _manuscript_validation_error(
    manuscript: str,
    paper: ParsedPaper,
    num_pages: int | None,
    detail_level: str = "normal",
) -> str | None:
    structure_error = _manuscript_structure_error(manuscript, num_pages, detail_level)
    figure_error = _manuscript_figure_token_error(manuscript, paper)
    source_error = _manuscript_source_coverage_error(manuscript, paper)
    errors = [error for error in (structure_error, figure_error, source_error) if error]
    return "; ".join(errors) or None


def _structure_retry_prompt(
    error: str,
    num_pages: int | None,
    detail_level: str = "normal",
) -> str:
    return (
        "The previous manuscript could not be parsed by the slide pipeline: "
        f"{error}.\n\n"
        "Regenerate the full slide manuscript only. Preserve the visible content and evidence as closely as possible.\n"
        "If the error mentions paper figure tokens, replace invalid tokens with exact tokens from the Valid Paper Figure Tokens list, or omit the real figure when no listed token matches.\n"
        "If the error mentions multi-source coverage, revise the actual narrative so the missing sources contribute evidence, then add accurate invisible `<!-- sources: S1, S2 -->` comments to content slides. Do not add source references without using their evidence.\n"
        "Use valid standalone slide separators and explicit page_type metadata. Slide 1 must be cover, slide 2 must be the mandatory table of contents, and the final slide must be ending when the page budget requires it.\n"
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
    provider_memory: ProviderMemory | None = None,
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
        detail_level: Controls automatic page range and context/summary budgets.
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
    compact_paper_md = provider_memory.compact_markdown if provider_memory else paper_md

    # External enrichment is injected into Pass 1 specifically — that's where
    # related-work context actually changes the analysis (gap framing,
    # contribution delta). Injecting later just decorates the manuscript.
    enrichment_block = ""
    if research_context and (research_context.has_enrichment or research_context.errors):
        enrichment_block = research_context.enrichment_block_for_pass1()
        logger.info("Research: Pass 1 enrichment block (%d chars)", len(enrichment_block))

    if not enable_deep_research:
        context_meta: dict[str, object] = {
            "mode": "full_multi_source_no_truncation" if _is_multi_source_corpus(paper) else "full_first",
            "manuscript_selected": "full",
            "manuscript_reason": "complete_source_corpus",
            "full_chars": len(paper_md),
            "compact_chars": len(compact_paper_md),
            "compact_available": bool(compact_paper_md.strip()),
            "compact_truncated": "[Compact paper memory truncated here.]" in compact_paper_md,
        }
        multi_source_contract = _multi_source_contract(paper)
        repair_source = "\n\n".join(
            part
            for part in (paper_md if multi_source_contract else "", multi_source_contract, _figure_token_inventory_block(paper))
            if part
        )
        logger.info(
            "Research normal mode context: manuscript=%s (full=%d chars, selected=%d chars, compact=%d chars)",
            context_meta["manuscript_selected"],
            len(paper_md),
            len(paper_md),
            len(compact_paper_md),
        )
        _debug_write_text(
            debug_dir,
            "lightweight_context_selection.json",
            json.dumps(context_meta, ensure_ascii=False, indent=2),
        )
        if on_progress:
            on_progress("Generating manuscript from full paper", 0.22)
        try:
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
                paper_markdown=paper_md,
                repair_source_markdown=repair_source,
                debug_dir=debug_dir,
            )
        except Exception as exc:
            if _is_multi_source_corpus(paper) and _is_context_window_error(exc):
                raise ValueError(
                    "The complete multi-source corpus exceeds the selected model's "
                    "context window. No automatic truncation was applied; use a model "
                    "with a larger context window or remove sources explicitly."
                ) from exc
            if (
                not _is_context_window_error(exc)
                or not compact_paper_md.strip()
                or compact_paper_md == paper_md
            ):
                raise
            logger.warning(
                "Full-paper normal research context rejected; retrying with compact fallback: %s",
                exc,
            )
            context_meta.update(
                {
                    "manuscript_selected": "compact",
                    "manuscript_reason": "provider_rejected_full_context",
                    "fallback_error": str(exc),
                }
            )
            _debug_write_text(
                debug_dir,
                "lightweight_context_selection.json",
                json.dumps(context_meta, ensure_ascii=False, indent=2),
            )
            if on_progress:
                on_progress("Full paper exceeded provider context; retrying compact fallback", 0.22)
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
                paper_markdown=compact_paper_md,
                repair_source_markdown=repair_source,
                debug_dir=debug_dir,
            )
        context_meta["manuscript_chars"] = len(
            compact_paper_md
            if context_meta["manuscript_selected"] == "compact"
            else paper_md
        )
        _debug_write_text(
            debug_dir,
            "lightweight_context_selection.json",
            json.dumps(context_meta, ensure_ascii=False, indent=2),
        )
        manuscript = _sanitize_internal_evidence_markers(manuscript)
        _debug_write_text(debug_dir, "research_final_manuscript.md", manuscript)
        return manuscript

    # ── Pass 1: Deep Reading ───────────────────────────────────────────────
    logger.info("Research Pass 1: Deep reading...")
    pass1_system = PASS1_PROMPT.read_text(encoding="utf-8")

    content_label = "Source Corpus Content" if _is_multi_source_corpus(paper) else "Paper Content"
    pass1_user_parts = [f"## {content_label}\n\n{paper_md}"]
    multi_source_contract = _multi_source_contract(paper)
    if multi_source_contract:
        pass1_user_parts.append(f"\n{multi_source_contract}")
    if enrichment_block:
        pass1_user_parts.append(f"\n{enrichment_block}")
    if instruction:
        pass1_user_parts.append(f"\n## User Instruction\n\n{instruction}")
    pass1_user_parts.append(
        "\n\nAnalyze this source corpus following the structured format above. Be specific and insightful. "
        "When supplementary related-work context is provided, use it to ground the gap analysis "
        "in concrete prior art rather than vague claims."
    )

    pass1_messages = [
        LLMMessage.system(pass1_system),
        LLMMessage.user("\n".join(pass1_user_parts)),
    ]
    _debug_write_messages(debug_dir, "research_pass1_prompt.md", pass1_messages)
    try:
        pass1_response = await llm.chat(
            pass1_messages,
            model,
            temperature=0.4,
            max_tokens=DEEPSEEK_MAX_TOKENS if is_deepseek else None,
        )
    except Exception as exc:
        if _is_multi_source_corpus(paper) and _is_context_window_error(exc):
            raise ValueError(
                "The complete multi-source corpus exceeds the selected model's "
                "context window during deep analysis. No automatic truncation was "
                "applied; use a model with a larger context window or remove sources "
                "explicitly."
            ) from exc
        if (
            not _is_context_window_error(exc)
            or not compact_paper_md.strip()
            or compact_paper_md == paper_md
        ):
            raise
        logger.warning("Full-paper Pass 1 context rejected; retrying compact fallback: %s", exc)
        fallback_parts = [part.replace(paper_md, compact_paper_md) for part in pass1_user_parts]
        fallback_messages = [
            LLMMessage.system(pass1_system),
            LLMMessage.user("\n".join(fallback_parts)),
        ]
        _debug_write_messages(debug_dir, "research_pass1_prompt_compact_fallback.md", fallback_messages)
        _debug_write_text(debug_dir, "research_pass1_context_fallback.txt", str(exc))
        pass1_response = await llm.chat(
            fallback_messages,
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
        f"## Deep Analysis of the Source Corpus\n\n{deep_analysis}",
        f"\n## Target Slides\n\n{_target_slides_guidance(num_pages, detail_level)}",
    ]
    if multi_source_contract:
        pass2_user_parts.append(f"\n{multi_source_contract}")
    pass2_user_parts.append(
        "\n\nDesign the narrative arc for this source corpus presentation. Choose the best narrative strategy "
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
    ]
    if multi_source_contract:
        pass3_user_parts.append(f"\n{multi_source_contract}")
    figure_inventory = _figure_token_inventory_block(paper)
    if figure_inventory:
        pass3_user_parts.append(f"\n{figure_inventory}")
    if instruction:
        pass3_user_parts.append(f"\n## User Instruction\n\n{instruction}")
    # NOTE: enrichment_block is intentionally injected only into Pass 1 above.
    # Pass 3 sees the deep_analysis (which already absorbed the enrichment),
    # so re-injecting here would just burn context for no benefit.
    pass3_user_parts.append(
        "\n\nGenerate the complete slide manuscript now. Use `---` to separate slides. "
        "Follow the narrative arc plan and the information aesthetics principles. "
        "Make the chapter plan come from the source corpus's real content logic, not a fixed "
        "template. Slide 2 must be the table of contents "
        "and must use the same final chapter titles. Keep chapter slides concise; detailed "
        "evidence belongs on content slides."
    )
    pass3_user_parts.append(
        "\n\nExact evidence coverage is mandatory: preserve representative source values "
        "for the paper's main evidence streams, including problem-defining distributions, "
        "method/efficiency costs, benchmark headline numbers, ablation deltas, and stated "
        "limitations. Do not replace concrete table values with generic performance claims."
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
        manuscript = normalize_manuscript_slide_delimiters(pass3_response.content)
        _debug_write_text(
            debug_dir,
            "research_pass3_response.md"
            if attempt == 1
            else f"research_pass3_response_attempt{attempt}.md",
            pass3_response.content,
        )
        if manuscript != pass3_response.content.strip():
            _debug_write_text(
                debug_dir,
                "research_pass3_response_normalized.md"
                if attempt == 1
                else f"research_pass3_response_attempt{attempt}_normalized.md",
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
        logger.warning("Pass 3 manuscript structure invalid after retry; repairing: %s", last_structure_error)
        manuscript = await _repair_manuscript_structure_if_needed(
            manuscript,
            "\n\n".join(
                part
                for part in (paper_md if multi_source_contract else "", multi_source_contract, figure_inventory)
                if part
            ),
            llm,
            model,
            language=language,
            detail_level=detail_level,
            is_deepseek=is_deepseek,
            paper=paper,
            num_pages=num_pages,
            debug_dir=debug_dir,
            debug_prefix="research_pass3_structure_repair",
        )
    logger.info("Research Pass 3 complete (%d chars)", len(manuscript))
    if on_progress:
        on_progress("Pass 3/4 — Manuscript", 0.25)

    # ── Pass 4: Self-Evaluation & Revision ─────────────────────────────────
    logger.info("Research Pass 4: Self-evaluation...")
    pass4_system = PASS4_PROMPT.read_text(encoding="utf-8")
    pass4_source_context = paper_md

    pass4_user_parts = [
        f"## Slide Manuscript to Evaluate\n\n{manuscript}",
        f"\n## Source Corpus (authoritative)\n\n{pass4_source_context}",
        f"\n## Original Deep Analysis\n\n{deep_analysis[:5000]}",
        f"\n## Narrative Plan\n\n{narrative_plan[:2000]}",
        f"\n## Target Language\n\n{language}",
    ]
    if figure_inventory:
        pass4_user_parts.append(f"\n{figure_inventory}")
    if multi_source_contract:
        pass4_user_parts.append(f"\n{multi_source_contract}")
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
    try:
        pass4_response = await llm.chat(
            pass4_messages,
            model,
            temperature=0.3,
            max_tokens=DEEPSEEK_MAX_TOKENS if is_deepseek else None,
        )
    except Exception as exc:
        if _is_multi_source_corpus(paper) and _is_context_window_error(exc):
            raise ValueError(
                "The complete multi-source corpus exceeds the selected model's "
                "context window during quality review. No automatic truncation was "
                "applied; use a model with a larger context window or remove sources "
                "explicitly."
            ) from exc
        if (
            not _is_context_window_error(exc)
            or not compact_paper_md.strip()
            or compact_paper_md == paper_md
        ):
            raise
        logger.warning("Full-paper Pass 4 context rejected; retrying compact fallback: %s", exc)
        fallback_parts = [
            part.replace(pass4_source_context, compact_paper_md)
            for part in pass4_user_parts
        ]
        pass4_messages = [
            LLMMessage.system(pass4_system),
            LLMMessage.user("\n".join(fallback_parts)),
        ]
        _debug_write_messages(debug_dir, "research_pass4_prompt_compact_fallback.md", pass4_messages)
        _debug_write_text(debug_dir, "research_pass4_context_fallback.txt", str(exc))
        pass4_response = await llm.chat(
            pass4_messages,
            model,
            temperature=0.3,
            max_tokens=DEEPSEEK_MAX_TOKENS if is_deepseek else None,
        )
    _debug_write_text(debug_dir, "research_pass4_response.md", pass4_response.content)
    final_output = normalize_manuscript_slide_delimiters(
        _extract_manuscript_from_review(pass4_response.content, manuscript)
    )
    final_error = _manuscript_validation_error(final_output, paper, num_pages, detail_level)
    manuscript_error = _manuscript_validation_error(manuscript, paper, num_pages, detail_level)
    if final_error and not manuscript_error:
        logger.warning("Pass 4 changed manuscript structure; keeping Pass 3 output: %s", final_error)
        final_output = manuscript
    elif final_error:
        logger.warning("Final manuscript validation failed after retries; repairing: %s", final_error)
        final_output = await _repair_manuscript_structure_if_needed(
            final_output,
            "\n\n".join(
                part
                for part in (paper_md if multi_source_contract else "", multi_source_contract, figure_inventory)
                if part
            ),
            llm,
            model,
            language=language,
            detail_level=detail_level,
            is_deepseek=is_deepseek,
            paper=paper,
            num_pages=num_pages,
            debug_dir=debug_dir,
            debug_prefix="research_final_structure_repair",
        )
    final_output = _sanitize_internal_evidence_markers(final_output)
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
