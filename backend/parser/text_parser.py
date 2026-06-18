"""Lightweight parser for pasted plain text.

Unlike :class:`~backend.parser.pdf_parser.PDFParser`, this parser does **not**
run academic-paper heuristics (figure caption detection, author extraction,
font-size heading analysis). It just splits the text into sections so the
research agent receives structured, readable input rather than one wall of
text.

Heading heuristics (intentionally simple):

* A line is a *title line* of a block if it is short (≤ 80 chars), does not
  end with sentence punctuation, and is followed by a blank line.
* Markdown ``#`` / ``##`` / ``###`` ATX headings are honoured when present so
  a pasted markdown blob degrades gracefully (the dedicated
  :class:`~backend.parser.markdown_parser.MarkdownParser` is preferred for
  real ``.md`` files because it preserves heading levels exactly).
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.runtime import aoffload

from .base import PaperParser
from .paper_model import PaperSection, ParsedPaper

# Markdown ATX headings — honoured opportunistically so a pasted markdown
# blob still gets reasonable structure even through TextParser.
_ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SENTENCE_END_RE = re.compile(r"[.!?。！？:：]\s*$")
_MAX_TITLE_LEN = 80


def _looks_like_title(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > _MAX_TITLE_LEN:
        return False
    if _SENTENCE_END_RE.search(stripped):
        return False
    # A line that is mostly punctuation / symbols is probably not a heading.
    if sum(ch.isalnum() for ch in stripped) < max(3, len(stripped) // 3):
        return False
    return True


def _split_into_blocks(text: str) -> list[tuple[int, str, str]]:
    """Return ``[(heading_level, title, body), ...]`` for a chunk of text.

    Each block becomes one :class:`PaperSection`. The first block has no
    heading when the text starts with body copy.
    """
    lines = text.splitlines()
    blocks: list[tuple[int, str, str]] = []
    current_level = 0
    current_title = ""
    current_body: list[str] = []

    def flush() -> None:
        body = "\n".join(current_body).strip()
        if body or current_title:
            blocks.append((current_level, current_title, body))
        current_body.clear()

    i = 0
    n = len(lines)
    # ``at_boundary`` is True at the very start and right after a blank line —
    # i.e. when a new block/section could legitimately begin. It's the gate
    # that prevents a body sentence from being misread as a title just because
    # it happens to be short.
    at_boundary = True
    while i < n:
        line = lines[i]
        stripped = line.strip()

        md = _ATX_HEADING_RE.match(stripped) if stripped else None
        if md:
            flush()
            current_level = min(len(md.group(1)), 3)
            current_title = md.group(2).strip()
            at_boundary = False
            i += 1
            continue

        # Blank line ends the current paragraph; remember we're at a boundary
        # but don't emit trailing whitespace into the body.
        if not stripped:
            at_boundary = True
            i += 1
            continue

        # Title heuristic: a short, non-sentence line that sits at a block
        # boundary AND is followed by a blank line is treated as a section
        # heading. Requiring *both* boundaries (preceding blank + following
        # blank) keeps real one-line sentences in the body.
        next_blank = i + 1 >= n or not lines[i + 1].strip()
        if (
            at_boundary
            and next_blank
            and _looks_like_title(stripped)
        ):
            flush()
            current_level = 2
            current_title = stripped
            at_boundary = False
            i += 1
            continue

        current_body.append(line)
        at_boundary = False
        i += 1

    flush()
    return blocks


def _derive_title_and_abstract(blocks: list[tuple[int, str, str]]) -> tuple[str, str]:
    """Pick a reasonable document title + abstract from the parsed blocks."""
    title = ""
    for level, heading, _ in blocks:
        if level == 1 or (level == 0 and heading):
            title = heading
            break
    if not title:
        for level, _, body in blocks:
            if body:
                first_line = body.splitlines()[0].strip()
                title = first_line[:80] if first_line else "Pasted text"
                break
    if not title:
        title = "Pasted text"

    abstract = ""
    for level, _, body in blocks:
        if level == 0 and body:
            # First real paragraph becomes the abstract.
            abstract = body.split("\n\n")[0].strip()
            break
    return title, abstract


def _text_to_paper(text: str, source_type: str, output_dir: Path) -> ParsedPaper:
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not normalized:
        normalized = "(empty)"

    blocks = _split_into_blocks(normalized)

    # If heuristics produced nothing useful, wrap the whole thing in one block
    # so the research agent still gets the content verbatim.
    if not blocks or all((not body and not heading) for _, heading, body in blocks):
        blocks = [(0, "", normalized)]

    sections: list[PaperSection] = []
    for level, heading, body in blocks:
        if not heading and not body:
            continue
        if heading:
            sections.append(
                PaperSection(
                    title=heading or "Section",
                    level=max(1, level) if level else 2,
                    content=body,
                )
            )
        else:
            # Leading untitled block — fold into an "Overview" section so the
            # downstream manuscript has a stable anchor for it.
            sections.append(
                PaperSection(title="Overview", level=1, content=body)
            )

    title, abstract = _derive_title_and_abstract(blocks)

    return ParsedPaper(
        title=title,
        authors=[],
        abstract=abstract,
        sections=sections,
        source_type=source_type,  # type: ignore[arg-type]
        figures_dir=None,
    )


class TextParser(PaperParser):
    """Parse a pasted-plain-text payload into a minimal :class:`ParsedPaper`.

    The abstract :meth:`PaperParser.parse` signature takes ``file_path``; for a
    text source the payload is written to ``output_dir/source.txt`` by the
    caller (the API layer / merger), so we read it back here. This keeps the
    interface uniform across all parsers.
    """

    async def parse(self, file_path: Path, output_dir: Path) -> ParsedPaper:
        text = await aoffload(self._read_text, file_path)
        return await aoffload(_text_to_paper, text, "text", output_dir)

    @staticmethod
    def _read_text(file_path: Path) -> str:
        return Path(file_path).read_text(encoding="utf-8", errors="replace")

    async def parse_text(self, text: str, output_dir: Path) -> ParsedPaper:
        """Convenience entrypoint used when the raw text is already in memory."""
        return await aoffload(_text_to_paper, text, "text", output_dir)
