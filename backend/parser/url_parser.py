"""Parser for URL / webpage sources.

Pipeline:

1. **Fetch** the raw HTML via ``httpx`` (follow redirects, HTML content-type
   check) — mirrors the reader in
   :func:`backend.orchestrator.research_enrichment._open_web_results`.
2. **Extract** main content with ``trafilatura`` (run on the offload pool since
   it's CPU-bound lxml work). Falls back to the stdlib HTML stripper in
   ``research_enrichment`` when trafilatura is unavailable or returns nothing.
3. **Structure** the extracted markdown into sections via the same heuristic
   as :class:`~backend.parser.markdown_parser.MarkdownParser`.

The fetch step is split out (:func:`fetch_url_html`) so the API layer can
validate a URL *immediately* on add (before any generation) and return a
useful error to the user instead of failing 20 minutes into a pipeline run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from backend.config import settings
from backend.runtime import aoffload

from .base import PaperParser
from .markdown_parser import _markdown_to_paper
from .paper_model import ParsedPaper

logger = logging.getLogger(__name__)

# User-Agent kept identical to the research reader so sites that already
# tolerate the agent keep doing so.
_USER_AGENT = "PaperPPTAgent/1.0 research reader"
_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


class UrlFetchError(Exception):
    """Raised when a URL cannot be fetched or has no extractable text.

    Carries a short ``reason`` the API layer surfaces verbatim to the user.
    """


@dataclass
class UrlFetchResult:
    """Outcome of fetching + extracting one URL."""

    url: str
    final_url: str            # after redirects
    title: str                # <title> or best-effort
    markdown: str             # extracted main content
    html_bytes: int           # size of the downloaded payload
    method: str               # "trafilatura" | "fallback"


def _is_html(content_type: str) -> bool:
    ct = (content_type or "").lower().split(";")[0].strip()
    return any(ct.startswith(t) for t in _HTML_CONTENT_TYPES)


def _extract_title(html: str) -> str:
    import re

    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    # Collapse whitespace and unescape the most common entities.
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    return title[:200]


def _trafilatura_extract(html: str, url: str) -> str | None:
    """Run trafilatura on the offload pool. Returns None if unavailable/empty."""
    try:
        import trafilatura  # type: ignore[import-not-found]
    except Exception:
        return None

    def _do() -> str | None:
        return trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_links=False,
            include_tables=True,
            include_images=False,
            favor_recall=True,
        )

    return _do()


def _fallback_extract(html: str) -> str:
    """Stdlib HTML→text used when trafilatura is missing or yields nothing."""
    try:
        from backend.orchestrator.research_enrichment import _html_to_text
    except Exception:  # pragma: no cover — defensive
        from html.parser import HTMLParser

        class _Stripper(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self._skip = 0
                self._chunks: list[str] = []

            def handle_starttag(self, tag: str, attrs: list) -> None:
                if tag.lower() in {"script", "style", "noscript", "svg"}:
                    self._skip += 1

            def handle_endtag(self, tag: str) -> None:
                if self._skip and tag.lower() in {"script", "style", "noscript", "svg"}:
                    self._skip -= 1

            def handle_data(self, data: str) -> None:
                if not self._skip:
                    self._chunks.append(data)

        s = _Stripper()
        s.feed(html)
        text = " ".join(s._chunks)
    else:
        text = _html_to_text(html)

    import re

    return re.sub(r"\s+\n", "\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


async def fetch_url_html(url: str) -> UrlFetchResult:
    """Fetch + extract one URL. Raises :class:`UrlFetchError` on failure.

    Used both by :class:`UrlParser.parse` and by the ``/api/sources/{sid}/url``
    endpoint (which validates a URL on add).
    """
    import httpx

    if not url.startswith(("http://", "https://")):
        raise UrlFetchError("URL must start with http:// or https://")

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=settings.url_fetch_timeout,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            resp = await client.get(url)
    except httpx.TimeoutException as exc:
        raise UrlFetchError(
            f"Timed out after {settings.url_fetch_timeout:.0f}s while fetching the page."
        ) from exc
    except httpx.HTTPError as exc:
        raise UrlFetchError(f"Could not reach the page: {exc}") from exc

    if resp.status_code >= 400:
        raise UrlFetchError(f"The page returned HTTP {resp.status_code}.")

    content_type = str(resp.headers.get("content-type", ""))
    if not _is_html(content_type):
        raise UrlFetchError(
            "The URL did not return an HTML page "
            f"(content-type was '{content_type.split(';')[0]}')."
        )

    html = resp.text
    if len(resp.content) > settings.url_fetch_max_bytes:
        # Keep the head only; trafilatura handles truncated markup acceptably.
        html = html[: settings.url_fetch_max_bytes]

    title = _extract_title(html) or urlparse(str(resp.url)).netloc or url

    md = await aoffload(_trafilatura_extract, html, str(resp.url))
    method = "trafilatura"
    if not md or not md.strip():
        fallback = await aoffload(_fallback_extract, html)
        if not fallback:
            raise UrlFetchError(
                "Could not extract readable text from this page "
                "(it may be a JavaScript-rendered app with no static content)."
            )
        md = fallback
        method = "fallback"

    return UrlFetchResult(
        url=url,
        final_url=str(resp.url),
        title=title,
        markdown=md,
        html_bytes=len(resp.content),
        method=method,
    )


class UrlParser(PaperParser):
    """Parse a URL source into a :class:`ParsedPaper`.

    ``file_path`` carries the source URL as a string-encoded path (the API
    layer writes ``url=<the url>`` into the Source). We resolve the URL via
    :func:`fetch_url_html` and then structure the extracted markdown.
    """

    async def parse(self, file_path: Path, output_dir: Path) -> ParsedPaper:
        url = self._url_from_path(file_path)
        return await self.parse_url(url, output_dir)

    async def parse_url(self, url: str, output_dir: Path) -> ParsedPaper:
        result = await fetch_url_html(url)
        output_dir.mkdir(parents=True, exist_ok=True)
        # Persist the extracted markdown so the workspace is self-contained
        # (the agent path and debug tooling can re-read it without re-fetching).
        (output_dir / "page.md").write_text(
            f"# {result.title}\n\nSource: {result.final_url}\n\n{result.markdown}\n",
            encoding="utf-8",
        )
        paper = await aoffload(_markdown_to_paper, result.markdown, output_dir)
        # Prefer the page <title> for display when extraction produced only
        # body sections.
        if not paper.title or paper.title == "Markdown document":
            paper.title = result.title or urlparse(url).netloc or "Webpage"
        paper.source_type = "url"  # type: ignore[assignment]
        return paper

    @staticmethod
    def _url_from_path(file_path: Path) -> str:
        raw = str(file_path)
        # The API layer stores the URL verbatim; tolerate the ``url=`` prefix
        # we use in Source encoding as well as a bare URL.
        if raw.startswith("url="):
            return raw[len("url=") :]
        return raw
