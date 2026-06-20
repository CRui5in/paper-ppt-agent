"""Parse URL sources from a locally rendered Chromium page.

The parser deliberately uses one transparent path:

1. Render the URL with the project's Playwright Chromium dependency.
2. Scroll generically so lazy content has a chance to appear.
3. Cache the complete visible page text from ``document.body.innerText``.
4. Structure that exact cached text with the normal Markdown parser.

No third-party reader API, site-specific selector, or main-content guess is
used. The source preview displays the same text that generation consumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from backend.runtime import aoffload
from backend.runtime.web_browser import BrowserRenderError, render_url

from .base import PaperParser
from .markdown_parser import _markdown_to_paper
from .paper_model import ParsedPaper


class UrlFetchError(Exception):
    """Raised when a URL cannot be rendered into visible text."""


@dataclass
class UrlFetchResult:
    url: str
    final_url: str
    title: str
    markdown: str
    html_bytes: int
    method: str
    html: str = ""


async def fetch_url_html(url: str) -> UrlFetchResult:
    """Render a URL and return its complete normalized visible page text."""
    try:
        rendered = await render_url(url)
    except BrowserRenderError as exc:
        raise UrlFetchError(str(exc)) from exc
    title = rendered.title or urlparse(rendered.final_url).netloc or url
    return UrlFetchResult(
        url=url,
        final_url=rendered.final_url,
        title=title,
        markdown=rendered.visible_text,
        html_bytes=len(rendered.html.encode("utf-8")),
        method="playwright-visible-text",
        html=rendered.html,
    )


class UrlParser(PaperParser):
    """Parse cached rendered text, fetching only when no cache exists."""

    async def parse(self, file_path: Path, output_dir: Path) -> ParsedPaper:
        if file_path.exists() and file_path.suffix.lower() in {".md", ".markdown", ".txt"}:
            return await self.parse_cached_text(file_path, output_dir)
        url = self._url_from_path(file_path)
        return await self.parse_url(url, output_dir)

    async def parse_cached_text(self, file_path: Path, output_dir: Path) -> ParsedPaper:
        text = file_path.read_text(encoding="utf-8")
        paper = await aoffload(_markdown_to_paper, text, output_dir)
        if not paper.title or paper.title == "Markdown document":
            paper.title = file_path.stem or "Webpage"
        paper.source_type = "url"  # type: ignore[assignment]
        return paper

    async def parse_url(self, url: str, output_dir: Path) -> ParsedPaper:
        result = await fetch_url_html(url)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "page.txt").write_text(result.markdown, encoding="utf-8")
        (output_dir / "page.html").write_text(result.html, encoding="utf-8")
        paper = await aoffload(_markdown_to_paper, result.markdown, output_dir)
        if not paper.title or paper.title == "Markdown document":
            paper.title = result.title or urlparse(url).netloc or "Webpage"
        paper.source_type = "url"  # type: ignore[assignment]
        return paper

    @staticmethod
    def _url_from_path(file_path: Path) -> str:
        raw = str(file_path)
        return raw[len("url=") :] if raw.startswith("url=") else raw
