"""Multi-source merger.

Takes an ordered list of :class:`~backend.parser.source_model.Source` items,
parses each in isolation (see :mod:`backend.parser.dispatcher`), and folds the
results into a single :class:`~backend.parser.paper_model.ParsedPaper` that the
rest of the pipeline consumes unchanged.

Design goals (see plan Layer 2/3):

* **Zero downstream changes** — ``build_provider_memory``, ``analyze_paper``,
  ``_build_figure_inventory`` keep reading ``paper.to_markdown()`` /
  ``paper.all_figures()`` / ``paper.title`` etc. The merger just produces a
  valid ParsedPaper.
* **Figure-id isolation** — each source parses into its own
  ``sources/<source_id>/`` subdir, but ``PaperFigure.fig_id`` is derived from
  the file *stem* (``fig_001_p1``), which can collide across two PDFs. The
  merger renames every figure file to ``<source_id>_<stem>`` and rewrites
  ``fig.path`` so the ``[[FIG:<source_id>_fig_001_p1]]`` tokens stay globally
  unique.
* **Attribution** — each source's content is wrapped under a level-1
  "group" section whose title carries the source label, so the merged
  ``to_markdown()`` is self-documenting about where each block came from.
* **Fault tolerance** — one source failing (bad URL, corrupt PDF) must not
  abort the whole run; the failure is recorded as a placeholder section and
  surfaced via ``parse_status`` on the source for the frontend.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from backend.runtime import aoffload
from backend.runtime.resource_gates import heavy_stage_slot

from .dispatcher import parse_source
from .paper_model import PaperFigure, PaperSection, ParsedPaper
from .source_model import Source

logger = logging.getLogger(__name__)

ProgressCb = Callable[[Source, str], Awaitable[None]]


def _rename_figure(fig: PaperFigure, source_id: str, source_dir: Path) -> PaperFigure:
    """Prefix a figure's filename/path/fig_id with ``source_id``.

    ``fig.path`` always lives inside ``source_dir`` (the parser wrote it
    there). We rename the file on disk so the stem — and therefore
    ``fig_id`` — becomes ``<source_id>_<old_stem>``.
    """
    old_path = Path(fig.path)
    try:
        new_name = f"{source_id}_{old_path.name}"
        new_path = old_path.parent / new_name
        if old_path.exists() and old_path != new_path:
            shutil.move(str(old_path), str(new_path))
        else:
            new_path = old_path  # nothing to rename; keep stem unique anyway
    except OSError:
        new_path = old_path
    return replace(fig, path=new_path)


def _gather_figures(sections: list[PaperSection]) -> list[PaperFigure]:
    figs: list[PaperFigure] = []
    for sec in sections:
        figs.extend(sec.figures)
    return figs


def _wrap_source_sections(
    src: Source,
    paper: ParsedPaper,
) -> list[PaperSection]:
    """Wrap a source's sections under an attribution group heading."""
    # Attribute metadata baked into the group title so it survives the flatten
    # into ``to_markdown()``.
    attribution = f"{src.label} (source: {src.kind})"
    group = PaperSection(title=attribution, level=1, content="", figures=[], tables=[])
    # Demote the source's own top-level (level 1) sections to level 2 so they
    # nest cleanly under the group heading. Levels are clamped to 2..4.
    adjusted: list[PaperSection] = [group]
    for sec in paper.sections:
        new_level = max(2, min(sec.level + 1, 4)) if sec.level >= 1 else 2
        adjusted.append(replace(sec, level=new_level))
    if not paper.sections:
        # No sections (e.g. empty text) — leave the group heading alone.
        pass
    return adjusted


def _merge_failed_section(src: Source) -> list[PaperSection]:
    """Placeholder block for a source that could not be parsed."""
    msg = src.parse_error or "unknown error"
    group = PaperSection(
        title=f"{src.label} (source: {src.kind}) — failed to parse",
        level=1,
        content=f"[This source could not be parsed and was skipped: {msg}]",
    )
    return [group]


def _pick_title_abstract(sources: list[Source], papers: list[ParsedPaper | None]) -> tuple[str, list[str], str]:
    """Title/authors/abstract for the merged corpus.

    Prefer the first primary *pdf/latex* source — those carry reliable
    metadata (real title, authors, abstract). For synthetic corpora (notes,
    pasted text, markdown) the per-source ``paper.title`` is usually just the
    first body line, so prefer the user-chosen source label as the corpus
    title and keep the first parsed abstract.
    """
    for src, paper in zip(sources, papers):
        if paper is None or src.role != "primary":
            continue
        if src.kind in ("pdf", "latex"):
            return paper.title, list(paper.authors), paper.abstract

    # Synthetic corpus: synthesize a readable title from the source list, but
    # preserve the first parsed abstract (still useful for the research agent).
    abstract = ""
    for src, paper in zip(sources, papers):
        if paper is not None and paper.abstract:
            abstract = paper.abstract
            break
    primary = [s for s in sources if s.role == "primary"]
    head = primary or sources
    if head:
        first = head[0]
        if len(head) == 1:
            return first.label, [], abstract
        # Multiple synthetic sources → a generic but honest title.
        return f"{first.label} (+{len(head) - 1} more)", [], abstract
    return "Multi-source document", [], abstract


def _merge_figure_manifests(
    sources: list[Source],
    papers: list[ParsedPaper | None],
    sources_root: Path,
) -> None:
    """Write a single combined ``figure_review.json`` at the sources root.

    Each per-source parse already wrote its own manifest; merging them keeps
    the contract that the root ``figures`` dir has one review file. Best
    effort — never raises.
    """
    records: list[dict] = []
    for src, paper in zip(sources, papers):
        if paper is None:
            continue
        for fig in _gather_figures(paper.sections):
            records.append(
                {
                    "source_id": src.id,
                    "source_label": src.label,
                    "path": str(fig.path),
                    "caption": fig.caption,
                    "page_number": fig.page_number,
                    "extraction_method": fig.extraction_method,
                    "quality_score": fig.quality_score,
                    "review_flags": list(fig.review_flags),
                    "natural_width": fig.natural_width,
                    "natural_height": fig.natural_height,
                    "aspect_ratio": round(fig.aspect_ratio, 4) if fig.aspect_ratio else None,
                }
            )
    if not records:
        return
    try:
        sources_root.mkdir(parents=True, exist_ok=True)
        (sources_root / "figure_review.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


async def merge_sources(
    sources: list[Source],
    project_dir: Path,
    on_progress: ProgressCb | None = None,
) -> tuple[ParsedPaper, list[Source]]:
    """Parse and merge ``sources`` into one :class:`ParsedPaper`.

    Returns the merged paper and the updated source list (each source's
    ``parse_status`` / ``char_count`` is populated).
    """
    if not sources:
        raise ValueError("merge_sources requires at least one source")

    sources_root = project_dir / "sources"
    await aoffload(sources_root.mkdir, parents=True, exist_ok=True)

    # Parse all sources in parallel. The heavy_stage_slot gate is acquired per
    # source so we don't exceed the host-wide CPU budget, but the gather lets
    # lightweight text/markdown/url sources progress alongside a slow PDF.
    async def _one(src: Source) -> tuple[Source, ParsedPaper | None]:
        if on_progress is not None:
            await on_progress(src, "parsing")
        try:
            async with heavy_stage_slot():
                updated, paper = await parse_source(src, sources_root)
        except Exception as exc:  # noqa: BLE001 — dispatcher already set status
            logger.warning("source %s failed during merge: %s", src.id, exc)
            return src, None
        if on_progress is not None:
            await on_progress(updated, "ok" if paper is not None else "error")
        return updated, paper

    results = await asyncio.gather(*[_one(s) for s in sources])
    updated_sources = [r[0] for r in results]
    papers = [r[1] for r in results]

    # Namespace figures per source so [[FIG:id]] tokens stay unique.
    for src, paper in zip(updated_sources, papers):
        if paper is None:
            continue
        source_dir = sources_root / src.id
        renamed: list[PaperFigure] = []
        for sec in paper.sections:
            sec.figures = [_rename_figure(f, src.id, source_dir) for f in sec.figures]
            renamed.extend(sec.figures)

    # Build merged section list in source order.
    merged_sections: list[PaperSection] = []
    for src, paper in zip(updated_sources, papers):
        if paper is None:
            merged_sections.extend(_merge_failed_section(src))
        else:
            merged_sections.extend(_wrap_source_sections(src, paper))

    title, authors, abstract = _pick_title_abstract(updated_sources, papers)

    merged = ParsedPaper(
        title=title,
        authors=authors,
        abstract=abstract,
        sections=merged_sections,
        source_type="mixed",
        figures_dir=sources_root,
    )

    _merge_figure_manifests(updated_sources, papers, sources_root)
    return merged, updated_sources


__all__ = ["merge_sources"]
