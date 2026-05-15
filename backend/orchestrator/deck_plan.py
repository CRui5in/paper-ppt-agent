"""Structured deck plan derived from manuscript and design_spec.

The manuscript remains the source of truth for slide order and page type.
The design spec remains the source of truth for visual intent. This module
turns both prose artifacts into a compact, explicit contract for SVG
generation so parallel pages do not have to re-infer identity, chapter
numbering, style family, or density from scratch.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from backend.orchestrator.manuscript import (
    extract_page_type,
    page_title,
    split_manuscript_pages,
    strip_page_type_metadata,
)


@dataclass(frozen=True)
class SlidePlan:
    page_num: int
    total_pages: int
    page_type: str
    title: str
    subtitle: str
    chapter_index: int | None
    chapter_label: str
    chapter_title: str
    chapter_subtitle: str
    style_family: str
    layout_family: str
    density: str
    design_block: str


@dataclass(frozen=True)
class DeckPlan:
    slides: tuple[SlidePlan, ...]
    canonical_chapter_contract: str

    @property
    def by_page(self) -> dict[int, SlidePlan]:
        return {slide.page_num: slide for slide in self.slides}


def _extract_page_design_block(design_spec: str, page_num: int) -> str:
    pattern = (
        rf"(?ims)^#+\s*(?:slide|page)\s*0*{page_num}\b.*?"
        rf"(?=^#+\s*(?:slide|page)\s*0*\d+\b"
        rf"|^#+\s*(?:part|chapter|section)\s+\d+\b"
        rf"|^#+\s*[IVXLCDM]+\.\s+|\Z)"
    )
    match = re.search(pattern, design_spec)
    return match.group(0).strip() if match else ""


def _extract_page_title_parts(page_content: str) -> tuple[str, str]:
    visible = strip_page_type_metadata(page_content).strip()
    headings = list(re.finditer(r"(?m)^#{1,6}\s+(.+?)\s*$", visible))
    title = headings[0].group(1).strip() if headings else page_title(page_content)
    subtitle = headings[1].group(1).strip() if len(headings) > 1 else ""
    if not subtitle:
        bold = re.search(r"(?m)^\*\*(.+?)\*\*\s*$", visible)
        if bold:
            subtitle = bold.group(1).strip()
    return title, subtitle


def _field(block: str, *labels: str) -> str:
    if not block:
        return ""
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?im)^\s*[-*]?\s*(?:\*\*)?(?:{label_pattern})(?:\*\*)?\s*[:：]\s*(.+?)\s*$",
        block,
    )
    return match.group(1).strip() if match else ""


def _slug(value: str, fallback: str) -> str:
    text = value.strip().lower()
    if not text:
        return fallback
    text = re.sub(r"`|\*\*", "", text)
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text).strip("-")
    if not text:
        return fallback
    return text[:48]


def _layout_family(page_type: str, block: str) -> str:
    explicit = _field(block, "Layout Family", "Layout Type", "Layout")
    if explicit:
        return _slug(explicit, f"{page_type}.standard")
    if page_type == "chapter":
        return "structural.chapter-divider"
    if page_type in {"cover", "toc", "ending"}:
        return f"structural.{page_type}"
    return "content.standard"


def _style_family(page_type: str, block: str, layout_family: str) -> str:
    explicit = _field(block, "Style Family", "Visual Family", "Design Family")
    if explicit:
        return _slug(explicit, layout_family)
    if page_type == "chapter":
        return "structural.chapter-divider"
    if page_type in {"cover", "toc", "ending"}:
        return f"structural.{page_type}"
    return f"content.{layout_family}"


def _density(page_type: str, block: str, detail_level: str) -> str:
    explicit = _field(block, "Density", "Content Density", "Information Density")
    if explicit:
        return explicit
    if page_type in {"cover", "chapter", "toc", "ending"}:
        return "minimal structural"
    if detail_level == "very_high":
        return "very_high: full but readable; use the content area with meaningful callouts, diagrams, annotations, or evidence blocks instead of leaving large unused regions"
    if detail_level == "high":
        return "high: moderately dense, with clear evidence and explanation blocks"
    return "normal: concise and readable"


def _redact_current_title(block: str, title: str, subtitle: str) -> str:
    redacted = block
    for value, token in ((title, "{{CHAPTER_TITLE}}"), (subtitle, "{{CHAPTER_SUBTITLE}}")):
        if value:
            redacted = redacted.replace(value, token)
    redacted = re.sub(r"(?i)\b(?:slide|page)\s*0*\d+\b", "Slide {{PAGE_NUM}}", redacted)
    return redacted.strip()


def build_deck_plan(
    manuscript: str,
    design_spec: str,
    *,
    detail_level: str = "normal",
) -> DeckPlan:
    pages = split_manuscript_pages(manuscript)
    total_pages = len(pages)
    slides: list[SlidePlan] = []
    chapter_index = 0
    current_chapter_title = ""
    current_chapter_subtitle = ""
    first_chapter_contract = ""

    for page_num, page in enumerate(pages, start=1):
        page_type = extract_page_type(page)
        title, subtitle = _extract_page_title_parts(page)
        block = _extract_page_design_block(design_spec, page_num)

        if page_type == "chapter":
            chapter_index += 1
            current_chapter_title = title
            current_chapter_subtitle = subtitle
            if not first_chapter_contract and block:
                first_chapter_contract = _redact_current_title(block, title, subtitle)

        chapter_label = f"{chapter_index:02d}" if chapter_index and page_type != "cover" else ""
        layout_family = _layout_family(page_type, block)
        style_family = _style_family(page_type, block, layout_family)
        density = _density(page_type, block, detail_level)

        slides.append(
            SlidePlan(
                page_num=page_num,
                total_pages=total_pages,
                page_type=page_type,
                title=title,
                subtitle=subtitle,
                chapter_index=chapter_index if chapter_index else None,
                chapter_label=chapter_label,
                chapter_title=current_chapter_title,
                chapter_subtitle=current_chapter_subtitle,
                style_family=style_family,
                layout_family=layout_family,
                density=density,
                design_block=block,
            )
        )

    return DeckPlan(
        slides=tuple(slides),
        canonical_chapter_contract=first_chapter_contract,
    )


def deck_plan_markdown(plan: DeckPlan) -> str:
    lines = [
        "# Structured Deck Plan",
        "",
        "This is the authoritative generation contract derived from manuscript.md and design_spec.md.",
        "Follow it over loose prose when page identity, chapter numbering, style family, or density conflicts.",
        "",
        "## Numbering Contract",
        "",
        "- Footer page number is always the slide page number.",
        "- Visible chapter/section number is the planned chapter index, not the slide page number.",
        "- Do not derive chapter numbers from `Generate page N/Total`.",
        "",
    ]

    if plan.canonical_chapter_contract:
        lines.extend(
            [
                "## Canonical Chapter Divider Family",
                "",
                "Use this as style/layout intent for every chapter page. Treat title, subtitle, page number, and chapter number as variables supplied by the current slide plan.",
                "",
                plan.canonical_chapter_contract,
                "",
            ]
        )

    lines.extend(
        [
            "## Slide Matrix",
            "",
            "| Slide | Type | Footer | Chapter | Style Family | Layout Family | Density | Title |",
            "| ----- | ---- | ------ | ------- | ------------ | ------------- | ------- | ----- |",
        ]
    )
    for slide in plan.slides:
        chapter = slide.chapter_label or "-"
        title = slide.title.replace("|", "/")
        density = slide.density.replace("|", "/")
        lines.append(
            f"| {slide.page_num} | {slide.page_type} | {slide.page_num}/{slide.total_pages} | "
            f"{chapter} | {slide.style_family} | {slide.layout_family} | {density} | {title} |"
        )
    return "\n".join(lines).strip()


def slide_plan_markdown(plan: DeckPlan, page_num: int) -> str:
    slide = plan.by_page.get(page_num)
    if slide is None:
        return ""

    lines = [
        f"### Slide {slide.page_num:02d} Plan",
        f"- Page type: {slide.page_type}",
        f"- Footer page number: {slide.page_num}/{slide.total_pages}",
        f"- Title: {slide.title}",
        f"- Subtitle: {slide.subtitle or '(none)'}",
        f"- Style family: {slide.style_family}",
        f"- Layout family: {slide.layout_family}",
        f"- Density target: {slide.density}",
    ]
    if slide.chapter_label:
        lines.extend(
            [
                f"- Current chapter index: {slide.chapter_label}",
                f"- Current chapter title: {slide.chapter_title}",
                f"- Current chapter subtitle: {slide.chapter_subtitle or '(none)'}",
                "- If a visible chapter/section number is needed, render the chapter index above, never the footer page number.",
            ]
        )
    if slide.page_type == "chapter":
        lines.append(
            "- Chapter divider contract: use the canonical chapter divider family; only title, subtitle, and chapter index may change."
        )
    elif slide.page_type == "content":
        lines.append(
            "- Content page contract: do not use chapter-divider treatment, oversized chapter numerals, or transition-page layout."
        )
    return "\n".join(lines)
