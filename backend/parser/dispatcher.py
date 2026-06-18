"""Source-kind → parser dispatch.

Each :class:`~backend.parser.source_model.Source` is parsed in isolation into
its own subdirectory (provided by the caller, typically
``project_dir/sources/<source_id>/``). Per-source subdirectories are what
prevent figure filename collisions when multiple PDFs are parsed in the same
session — see :mod:`backend.parser.merger`.

The dispatcher is the single place that maps a ``Source.kind`` to a concrete
:class:`~backend.parser.base.PaperParser`. Adding a new source type means
adding a branch here plus the parser class.
"""

from __future__ import annotations

import logging
from pathlib import Path

from backend.runtime import aoffload, awrite_text

from .base import PaperParser
from .paper_model import ParsedPaper
from .source_model import Source

logger = logging.getLogger(__name__)


def _parser_for(kind: str) -> PaperParser:
    if kind == "pdf":
        from .pdf_parser import PDFParser

        return PDFParser()
    if kind == "latex":
        from .latex_parser import LaTeXParser

        return LaTeXParser()
    if kind == "markdown":
        from .markdown_parser import MarkdownParser

        return MarkdownParser()
    if kind == "text":
        from .text_parser import TextParser

        return TextParser()
    if kind == "url":
        from .url_parser import UrlParser

        return UrlParser()
    raise ValueError(f"Unsupported source kind: {kind!r}")


async def _materialize_source_file(src: Source, out_dir: Path) -> Path:
    """Return a file path the parser can read for non-pdf/latex sources.

    ``PDFParser`` / ``LaTeXParser`` read their own uploaded file directly. The
    lightweight parsers take a ``file_path`` too (uniform interface), so for
    text/markdown/url sources we stage the payload into ``out_dir`` first:

    * ``text`` / ``markdown`` → write ``raw_text`` to ``source.<ext>``.
    * ``url`` → write ``url=<the url>`` marker; :class:`UrlParser` re-fetches.
    * ``pdf`` / ``latex`` → return the original ``file_path`` unchanged.
    """
    if src.kind in ("pdf", "latex"):
        if src.file_path is None:
            raise ValueError(f"{src.kind} source {src.id!r} has no file_path")
        return src.file_path

    out_dir.mkdir(parents=True, exist_ok=True)
    if src.kind == "url":
        marker = out_dir / "source.url"
        await awrite_text(marker, f"url={src.url or ''}", encoding="utf-8")
        return marker
    # text / markdown
    ext = ".md" if src.kind == "markdown" else ".txt"
    target = out_dir / f"source{ext}"
    await awrite_text(target, src.raw_text or "", encoding="utf-8")
    return target


async def parse_source(src: Source, base_dir: Path) -> tuple[Source, ParsedPaper]:
    """Parse one source into ``base_dir / <source_id> /``.

    Returns the (possibly telemetry-updated) source alongside its parsed
    paper so the merger can record ``char_count`` / ``parse_status``.
    """
    out_dir = base_dir / src.id
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        file_path = await _materialize_source_file(src, out_dir)
        parser = _parser_for(src.kind)
        paper = await parser.parse(file_path, out_dir)
        char_count = sum(len(s.content) for s in paper.sections) + len(paper.abstract)
        src.parse_status = "ok"
        src.parse_error = None
        src.char_count = char_count
        return src, paper
    except Exception as exc:  # noqa: BLE001 — surfaced as a failed source
        src.parse_status = "error"
        src.parse_error = str(exc) or exc.__class__.__name__
        src.char_count = 0
        logger.warning("source %s (%s) failed to parse: %s", src.id, src.kind, exc)
        # Re-raise so the merger can apply its per-source fault tolerance.
        raise


def estimate_chars(paper: ParsedPaper) -> int:
    return sum(len(s.content) for s in paper.sections) + len(paper.abstract)


__all__ = ["parse_source", "estimate_chars"]
