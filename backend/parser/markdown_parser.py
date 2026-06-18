"""Parser for Markdown and plain-text files.

For ``.md`` files we trust the document's own ATX (``#``) / Setext (``===`` /
``---`` underlines) heading structure and map heading depth to
:class:`~backend.parser.paper_model.PaperSection.level` (clamped to 1–3 so
the merged corpus doesn't end up with level-6 noise).

For ``.txt`` files we delegate to :class:`~backend.parser.text_parser.TextParser`'s
heuristic splitter, since plain text rarely carries reliable structure.

Both paths produce a ``ParsedPaper`` with ``source_type="markdown"``. P0 does
**not** extract inline ``![](image)`` images — they're left inline as text so
the research agent can reference them, but no :class:`PaperFigure` records are
created. (Figure extraction from markdown is a P1 concern.)
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.runtime import aoffload

from .base import PaperParser
from .paper_model import PaperSection, ParsedPaper
from .text_parser import TextParser, _text_to_paper

_ATX_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SETEXT_RE = re.compile(r"^[=\-]{2,}\s*$")
# A fenced code block — its contents must not be scanned for headings.
_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})")


def _parse_markdown_to_blocks(text: str) -> list[tuple[int, str, str]]:
    """Return ``[(level, title, body), ...]`` honouring Markdown structure.

    Setext headings (a line of ``=`` or ``-`` under a text line) map to level 1
    / 2 respectively. ATX headings (``#``) clamp to 1–3.
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

    in_fence = False
    fence_marker = ""
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Toggle fenced code blocks so we never treat code as headings.
        fence = _FENCE_RE.match(line)
        if fence:
            marker = line.strip().startswith("~") and "~~~" or "```"
            if not in_fence:
                in_fence = True
                fence_marker = line.strip()[:3]
            elif line.strip().startswith(fence_marker):
                in_fence = False
                fence_marker = ""
            current_body.append(line)
            i += 1
            continue

        if in_fence:
            current_body.append(line)
            i += 1
            continue

        md = _ATX_RE.match(stripped) if stripped else None
        if md:
            flush()
            current_level = min(len(md.group(1)), 3)
            current_title = md.group(2).strip()
            i += 1
            continue

        # Setext: next line is === or --- and current line is non-empty text.
        if (
            stripped
            and i + 1 < n
            and _SETEXT_RE.match(lines[i + 1].strip())
        ):
            flush()
            current_level = 1 if lines[i + 1].strip().startswith("=") else 2
            current_title = stripped
            i += 2
            continue

        current_body.append(line)
        i += 1

    flush()
    return blocks


def _markdown_to_paper(text: str, output_dir: Path) -> ParsedPaper:
    output_dir.mkdir(parents=True, exist_ok=True)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n") or "(empty)"

    blocks = _parse_markdown_to_blocks(normalized)
    if not blocks or all((not body and not heading) for _, heading, body in blocks):
        blocks = [(0, "", normalized)]

    sections: list[PaperSection] = []
    title = ""
    abstract = ""
    for level, heading, body in blocks:
        if not heading and not body:
            continue
        sec_level = level if level else 2
        sections.append(
            PaperSection(title=heading or "Section", level=sec_level, content=body)
        )
        if not title and heading:
            title = heading
        if not abstract and body and not heading:
            abstract = body.split("\n\n")[0].strip()

    if not title:
        title = "Markdown document"

    return ParsedPaper(
        title=title,
        authors=[],
        abstract=abstract,
        sections=sections,
        source_type="markdown",
        figures_dir=None,
    )


class MarkdownParser(PaperParser):
    """Parse ``.md`` (structured) and ``.txt`` (heuristic) files."""

    async def parse(self, file_path: Path, output_dir: Path) -> ParsedPaper:
        text = await aoffload(self._read_text, file_path)
        lower = Path(file_path).suffix.lower()
        if lower == ".txt":
            # Plain text has no reliable structure — reuse the text heuristic.
            return await aoffload(_text_to_paper, text, "markdown", output_dir)
        return await aoffload(_markdown_to_paper, text, output_dir)

    @staticmethod
    def _read_text(file_path: Path) -> str:
        return Path(file_path).read_text(encoding="utf-8", errors="replace")

    async def parse_text(self, text: str, output_dir: Path) -> ParsedPaper:
        """Parse an in-memory markdown string (used by url source when MD is produced)."""
        return await aoffload(_markdown_to_paper, text, output_dir)
