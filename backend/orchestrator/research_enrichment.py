"""External research enrichment: arxiv, Semantic Scholar, web search.

Populates a ResearchContext with supplementary findings that get injected into
Pass 1 of the deep analysis. Each source is independently configurable, errors
are captured (not silently swallowed) and surfaced both to the LLM (so it can
reason about gaps) and to the frontend (so users see why a switch produced no
results).

Design notes — see review in conversation history:
- Single source-of-truth implementation (no duplicate MCP server).
- Default OFF; only the master toggle's first activation defaults to academic
  free sources.
- Errors are recorded on `ctx.errors` rather than silently dropped.
- Findings are deduplicated and the original paper is filtered out.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from backend.api.schemas import ResearchConfig

if TYPE_CHECKING:
    from backend.orchestrator.research_agent import ResearchContext

logger = logging.getLogger(__name__)


@dataclass
class ResearchFinding:
    """A single enrichment finding from any source."""

    source: str  # "arxiv" | "semantic_scholar" | "web"
    title: str
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    citation_count: int | None = None
    url: str = ""
    relevance_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "title": self.title,
            "abstract": self.abstract,
            "authors": list(self.authors),
            "year": self.year,
            "citation_count": self.citation_count,
            "url": self.url,
            "relevance_note": self.relevance_note,
        }


@dataclass
class EnrichmentStats:
    """Per-source counts and errors, surfaced to the frontend."""

    arxiv_found: int = 0
    arxiv_error: str | None = None
    scholar_found: int = 0
    scholar_error: str | None = None
    web_found: int = 0
    web_error: str | None = None
    web_provider: str | None = None
    total_findings: int = 0
    filtered_findings: int = 0
    findings: list[ResearchFinding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "total_findings": self.total_findings,
            "filtered_findings": self.filtered_findings,
        }
        def _block(found: int, error: str | None, provider: str | None = None, items: list[ResearchFinding] | None = None) -> dict[str, Any]:
            block: dict[str, Any] = {"found": found}
            if error:
                block["error"] = error
            if provider:
                block["provider"] = provider
            if items:
                block["findings"] = [f.to_dict() for f in items]
            return block

        if self.arxiv_found or self.arxiv_error is not None:
            arxiv_items = [f for f in self.findings if f.source == "arxiv"]
            out["arxiv"] = _block(self.arxiv_found, self.arxiv_error, items=arxiv_items if arxiv_items else None)
        if self.scholar_found or self.scholar_error is not None:
            scholar_items = [f for f in self.findings if f.source == "semantic_scholar"]
            out["semantic_scholar"] = _block(self.scholar_found, self.scholar_error, items=scholar_items if scholar_items else None)
        if self.web_found or self.web_error is not None or self.web_provider:
            web_items = [f for f in self.findings if f.source == "web"]
            out["web"] = _block(self.web_found, self.web_error, self.web_provider, items=web_items if web_items else None)
        return out


# ── Public API ────────────────────────────────────────────────────────────────


async def enrich_context(
    ctx: "ResearchContext",
    *,
    paper_title: str = "",
    paper_abstract: str = "",
    config: ResearchConfig | None = None,
) -> EnrichmentStats:
    """Populate ResearchContext from configured external APIs.

    Returns per-source stats so the caller can surface them via the progress
    channel. The pipeline still functions when every source is disabled or
    fails — `ctx.findings` simply stays empty and Pass 1 sees an empty
    enrichment block.
    """
    stats = EnrichmentStats()
    if config is None:
        return stats

    if config.arxiv_search_enabled and (paper_title or paper_abstract):
        await _enrich_arxiv(ctx, stats, paper_title, paper_abstract, config.max_results_per_source)

    if config.semantic_scholar_enabled and paper_title:
        await _enrich_semantic_scholar(ctx, stats, paper_title, config.max_results_per_source, config.semantic_scholar_api_key)

    if config.web_search_enabled:
        await _enrich_web_search(ctx, stats, paper_title, config)

    # Dedupe + drop the paper itself.
    ctx.findings = _dedupe_findings(ctx.findings, paper_title)
    if config.relevance_filter:
        ctx.findings = _filter_relevant_findings(ctx.findings, paper_title, paper_abstract)
    stats.total_findings = len(ctx.findings)
    stats.filtered_findings = stats.total_findings
    stats.findings = list(ctx.findings)

    return stats


# ── arxiv ─────────────────────────────────────────────────────────────────────


async def _enrich_arxiv(
    ctx: "ResearchContext",
    stats: EnrichmentStats,
    paper_title: str,
    abstract: str,
    max_results: int,
) -> None:
    query = _build_arxiv_query(paper_title, abstract)
    if query is None:
        # Most common cause: a Chinese-only title with no extractable English/technical
        # terms. We deliberately skip rather than send a useless query.
        stats.arxiv_error = "no_extractable_terms"
        ctx.queries_used.append("arxiv: <skipped: no extractable terms>")
        logger.info("arxiv enrichment skipped — no extractable terms in title")
        return

    ctx.queries_used.append(f"arxiv: {query}")
    try:
        import arxiv  # type: ignore[import-not-found]

        client = arxiv.Client(page_size=max_results)
        search = arxiv.Search(query=query, max_results=max_results)

        added = 0
        for result in client.results(search):
            ctx.findings.append(
                ResearchFinding(
                    source="arxiv",
                    title=str(result.title),
                    abstract=str(result.summary or "").strip(),
                    authors=[a.name for a in result.authors][:5],
                    year=result.published.year if result.published else None,
                    url=str(result.pdf_url or result.entry_id or ""),
                )
            )
            added += 1

        stats.arxiv_found = added
        logger.info("arxiv enrichment: %d papers for query '%s'", added, query[:60])

    except ImportError:
        stats.arxiv_error = "package_missing"
        logger.info("arxiv package not installed; skipping arxiv enrichment")
    except Exception as e:  # noqa: BLE001
        stats.arxiv_error = f"{type(e).__name__}: {e}"
        logger.warning("arxiv enrichment failed: %s", e)


# ── Semantic Scholar ──────────────────────────────────────────────────────────


async def _enrich_semantic_scholar(
    ctx: "ResearchContext",
    stats: EnrichmentStats,
    paper_title: str,
    max_results: int,
    api_key: str | None = None,
) -> None:
    ctx.queries_used.append(f"semantic_scholar: {paper_title[:80]}")
    try:
        from semanticscholar import AsyncSemanticScholar  # type: ignore[import-not-found]

        sch = AsyncSemanticScholar(api_key=api_key or None, timeout=60)
        results = await sch.search_paper(
            paper_title,
            limit=max_results,
            fields=["title", "authors", "abstract", "citationCount", "year", "externalIds"],
        )

        added = 0
        for paper in results:
            authors = [getattr(a, "name", "") for a in (getattr(paper, "authors", []) or [])][:5]
            arxiv_id = ""
            external = getattr(paper, "externalIds", None) or {}
            if isinstance(external, dict):
                arxiv_id = external.get("ArXiv", "") or ""

            ctx.findings.append(
                ResearchFinding(
                    source="semantic_scholar",
                    title=str(getattr(paper, "title", "") or ""),
                    abstract=str(getattr(paper, "abstract", "") or "").strip(),
                    authors=[a for a in authors if a],
                    year=getattr(paper, "year", None),
                    citation_count=getattr(paper, "citationCount", None),
                    url=f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "",
                )
            )
            added += 1

        stats.scholar_found = added
        logger.info("Semantic Scholar enrichment: %d candidates for '%s'", added, paper_title[:50])

    except ImportError:
        stats.scholar_error = "package_missing"
        logger.info("semanticscholar package not installed; skipping")
    except Exception as e:  # noqa: BLE001
        stats.scholar_error = f"{type(e).__name__}: {e}"
        logger.warning("Semantic Scholar enrichment failed: %s", e)


# ── Web search ────────────────────────────────────────────────────────────────


async def _enrich_web_search(
    ctx: "ResearchContext",
    stats: EnrichmentStats,
    paper_title: str,
    config: ResearchConfig,
) -> None:
    if not paper_title:
        stats.web_error = "no_title"
        return

    tavily_key = (config.tavily_api_key or "").strip()
    serpapi_key = (config.serpapi_key or "").strip()
    provider = getattr(config, "web_search_provider", "tavily") or "tavily"

    if provider == "tavily" and not tavily_key:
        stats.web_error = "no_api_key"
        ctx.queries_used.append("web: <skipped: no api key>")
        return
    if provider == "serpapi" and not serpapi_key:
        stats.web_error = "no_api_key"
        ctx.queries_used.append("web: <skipped: no api key>")
        return

    # Focus on critique / followup / limitation discussions — that's where web
    # outranks arxiv. Generic "paper analysis" queries return SEO sludge.
    query = f'"{paper_title}" (critique OR limitation OR follow-up OR discussion)'
    ctx.queries_used.append(f"web: {query}")

    try:
        import httpx  # type: ignore[import-not-found]

        if provider == "tavily":
            stats.web_provider = "tavily"
            results = await _tavily_search(httpx, query, tavily_key, config.max_results_per_source)
        else:
            stats.web_provider = "serpapi"
            results = await _serpapi_search(httpx, query, serpapi_key, config.max_results_per_source)

        for r in results:
            ctx.findings.append(
                ResearchFinding(
                    source="web",
                    title=r.get("title") or r.get("url", ""),
                    abstract=r.get("content", "").strip(),
                    url=r.get("url", ""),
                )
            )
        stats.web_found = len(results)
        logger.info("Web enrichment (%s): %d results", stats.web_provider, len(results))

    except ImportError:
        stats.web_error = "httpx_missing"
    except Exception as e:  # noqa: BLE001
        stats.web_error = f"{type(e).__name__}: {e}"
        logger.warning("Web search enrichment failed: %s", e)


async def _tavily_search(httpx_mod: Any, query: str, api_key: str, max_results: int) -> list[dict[str, str]]:
    async with httpx_mod.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={"query": query, "api_key": api_key, "max_results": max_results, "include_images": False},
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": str(r.get("content", ""))[:500],
            }
            for r in data.get("results", [])
        ]


async def _serpapi_search(httpx_mod: Any, query: str, api_key: str, max_results: int) -> list[dict[str, str]]:
    async with httpx_mod.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://serpapi.com/search",
            params={"engine": "google", "q": query, "api_key": api_key, "num": max_results},
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("link", ""),
                "content": str(r.get("snippet", ""))[:500],
            }
            for r in data.get("organic_results", [])
        ]


# ── Helpers ───────────────────────────────────────────────────────────────────


_ASCII_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,}")
_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "for", "in", "on", "to", "with", "from",
    "by", "is", "are", "was", "were", "be", "been", "being", "that", "this",
    "these", "those", "it", "its", "as", "at", "which", "but", "not", "no",
    "nor", "so", "if", "than", "too", "very", "can", "will", "just", "should",
    "now", "via", "using", "based", "towards", "toward", "via",
    "paper", "method", "learning", "network", "net", "fine", "grained",
    "refine", "refinement", "model", "models", "approach",
})


def _build_arxiv_query(title: str, abstract: str) -> str | None:
    """Build a query that has a real chance of returning useful results.

    Strategy: prefer multi-word capitalized noun phrases or distinctive ASCII
    tokens from title + first sentence of abstract. If we cannot extract at
    least 2 meaningful tokens, return None — sending Chinese strings or single
    common words to arxiv produces zero hits and burns the user's time.
    """
    seed = (title or "").strip()
    if abstract:
        first_sentence = abstract.strip().split(".")[0]
        seed = f"{seed} {first_sentence}"

    tokens: list[str] = []
    seen: set[str] = set()
    for match in _ASCII_TOKEN.findall(seed):
        lower = match.lower()
        if lower in _STOP_WORDS:
            continue
        if lower in seen:
            continue
        seen.add(lower)
        tokens.append(match)
        if len(tokens) >= 6:
            break

    if len(tokens) < 2:
        return None
    return " ".join(tokens[:6])


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def _dedupe_findings(findings: list[ResearchFinding], paper_title: str) -> list[ResearchFinding]:
    """Drop near-duplicates and the paper being analyzed."""
    paper_norm = _normalize_title(paper_title)
    seen: set[str] = set()
    out: list[ResearchFinding] = []
    for f in findings:
        norm = _normalize_title(f.title)
        if not norm or norm == paper_norm or norm in seen:
            continue
        seen.add(norm)
        out.append(f)

    # Stable sort by citation count (None last). arxiv has no citation count
    # so it falls to the bottom — that's intentional, S2 results carry the
    # impact signal.
    out.sort(key=lambda f: (-(f.citation_count or 0), f.source))
    return out


def _filter_relevant_findings(
    findings: list[ResearchFinding],
    paper_title: str,
    paper_abstract: str,
) -> list[ResearchFinding]:
    terms = _relevance_terms(paper_title, paper_abstract)
    if len(terms) < 2:
        return findings

    out: list[ResearchFinding] = []
    for finding in findings:
        haystack = _normalize_title(f"{finding.title} {finding.abstract}")
        matched = [term for term in terms if term in haystack]
        if len(matched) < 2:
            continue
        finding.relevance_note = "Matched: " + ", ".join(matched[:5])
        out.append(finding)
    return out


def _relevance_terms(title: str, abstract: str) -> list[str]:
    seed = f"{title} {abstract}".lower()
    terms: list[str] = []
    seen: set[str] = set()
    for match in _ASCII_TOKEN.findall(seed):
        term = match.lower()
        if term in _STOP_WORDS or len(term) < 3 or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= 12:
            break
    return terms
