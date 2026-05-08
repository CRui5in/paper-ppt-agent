"""Helpers for slide-structured manuscript Markdown."""

from __future__ import annotations

import re

_SLIDE_DELIMITER_RE = re.compile(r"(?m)^\s*---\s*$")
_PAGE_TYPE_RE = re.compile(
    r"(?im)^\s*<!--\s*page_type\s*:\s*(cover|chapter|transition|toc|content|ending)\s*-->\s*"
)
_HEADING_RE = re.compile(r"(?m)^#{1,3}\s+(.+?)\s*$")

PageType = str


def split_manuscript_pages(manuscript: str) -> list[str]:
    """Split manuscript Markdown on standalone slide delimiter lines only."""
    return [page.strip() for page in _SLIDE_DELIMITER_RE.split(manuscript) if page.strip()]


def count_manuscript_pages(manuscript: str) -> int:
    """Return the number of actual slide pages in a manuscript."""
    return len(split_manuscript_pages(manuscript))


def normalize_page_type(page_type: str | None) -> PageType:
    value = (page_type or "").strip().lower()
    if value == "transition":
        return "chapter"
    if value in {"cover", "chapter", "toc", "content", "ending"}:
        return value
    return "content"


def extract_page_type(page_content: str) -> PageType:
    """Read non-visible page type metadata from a manuscript page."""
    match = _PAGE_TYPE_RE.search(page_content)
    if match:
        return normalize_page_type(match.group(1))
    return "content"


def strip_page_type_metadata(page_content: str) -> str:
    """Remove page type metadata before sending content to visual rendering."""
    return _PAGE_TYPE_RE.sub("", page_content, count=1).strip()


def page_title(page_content: str) -> str:
    content = strip_page_type_metadata(page_content)
    match = _HEADING_RE.search(content)
    if match:
        return match.group(1).strip()
    first = next((line.strip() for line in content.splitlines() if line.strip()), "")
    return first[:80] or "Untitled"


def page_type_budget(num_pages: int | None) -> dict[PageType, int]:
    """Return the structural slide budget included in the total slide count."""
    if not num_pages:
        return {"cover": 1, "chapter": 2, "content": 8, "ending": 1}
    if num_pages <= 1:
        return {"cover": 0, "chapter": 0, "content": 1, "ending": 0}
    if num_pages == 2:
        return {"cover": 1, "chapter": 0, "content": 0, "ending": 1}

    chapter_count = 1
    if num_pages >= 14:
        chapter_count = 3
    elif num_pages >= 9:
        chapter_count = 2

    content_count = max(1, num_pages - 2 - chapter_count)
    return {
        "cover": 1,
        "chapter": chapter_count,
        "content": content_count,
        "ending": 1,
    }


def page_type_budget_guidance(num_pages: int | None) -> str:
    budget = page_type_budget(num_pages)
    if num_pages:
        lead = f"Produce exactly {num_pages} slides; the structural pages are included in that total."
    else:
        lead = "Auto-determine the final count, defaulting to 12-15 slides when the paper has enough content."
    return (
        f"{lead}\n"
        f"- Budget: cover {budget['cover']}, chapter/transition {budget['chapter']}, "
        f"content {budget['content']}, ending {budget['ending']}.\n"
        "- Put `<!-- page_type: cover|chapter|content|ending -->` at the top of every slide.\n"
        "- Cover, chapter/transition, and ending pages are real slides, not styling guesses."
    )


def page_inventory(manuscript: str) -> list[dict[str, str | int]]:
    pages = split_manuscript_pages(manuscript)
    return [
        {"page": index, "type": extract_page_type(page), "title": page_title(page)}
        for index, page in enumerate(pages, start=1)
    ]


def format_page_inventory(manuscript: str) -> str:
    lines = []
    for item in page_inventory(manuscript):
        lines.append(f"{item['page']}. [{item['type']}] {item['title']}")
    return "\n".join(lines)
