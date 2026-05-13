"""Icon Decoration Round — Phase 2 of the Strategist pipeline.

Runs after the base design spec (Phase 1) is produced.
Analyzes each page's semantic role, searches for matching icons,
then asks the LLM to produce a sparse icon inventory with explicit placement
metadata for pages where an icon can act as a natural layout anchor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from backend.config import settings
from backend.generator.icon_index import get_icon_index
from backend.llm import LLMMessage, LLMProvider
from backend.orchestrator.manuscript import (
    extract_page_type,
    page_title,
    split_manuscript_pages,
    strip_page_type_metadata,
)

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "icon_round.md"
ICON_ROUND_MAX_TOKENS = 4096
ICON_RAG_CANDIDATES_PER_QUERY = 6
FORBIDDEN_ICON_PAGE_TYPES = {"cover", "chapter", "toc", "ending"}
MAX_PAGE_EXCERPT_CHARS = 700

# ── Per-page icon search ────────────────────────────────────────────────────

async def _search_icons_rag(
    queries: list[str],
    lib: str,
    gemini_api_key: str | None = None,
    *,
    k: int = 3,
) -> list[dict]:
    """Search icon candidates for a list of queries using RAG.

    Returns a list of dicts with keys: path, name, lib, category, tags, score, query.
    """
    import os

    old_key = os.environ.get("GEMINI_API_KEY")
    if gemini_api_key:
        os.environ["GEMINI_API_KEY"] = gemini_api_key

    index = get_icon_index()
    if not index.is_available:
        logger.warning("Icon index not available, skipping RAG retrieval")
        return []

    seen_paths: set[str] = set()
    candidates: list[dict] = []

    for query in queries:
        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(index.search, query, lib=lib, k=k),
                timeout=30,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning("Icon search timed out for query: %s", query)
            break
        for r in results:
            path = str(r.get("path") or "")
            if path in seen_paths or not _icon_asset_exists(path):
                continue
            seen_paths.add(path)
            r["query"] = query
            candidates.append(r)

    # Restore key
    if gemini_api_key:
        if old_key is not None:
            os.environ["GEMINI_API_KEY"] = old_key
        else:
            os.environ.pop("GEMINI_API_KEY", None)

    return candidates


# ── Offline concept-to-icon mapping ────────────────────────────────────────

OFFLINE_ICON_PALETTE: list[tuple[str, list[str]]] = [
    ("warning|risk|danger|failure|error|limitation", ["alert-triangle", "circle-exclamation", "alert-circle", "ban", "bug"]),
    ("insight|idea|key|thesis|turning point|contribution", ["lightbulb", "sparkles", "bulb", "brain"]),
    ("framework|architecture|model|module|system|pipeline", ["puzzle", "component", "cube", "layers", "stack"]),
    ("method|process|approach|stage|step|algorithm", ["route", "git-branch", "arrow-right", "settings", "cog"]),
    ("result|metric|performance|score|evaluation|experiment", ["chart-bar", "chart-line", "chart-pie", "activity", "target"]),
    ("evidence|data|dataset|ablation|table", ["flask", "microscope", "clipboard", "database"]),
    ("vision|perception|observation|attention|detection", ["eye", "search", "crosshairs"]),
    ("safety|robust|protection|security|reliability", ["shield", "lock", "key"]),
    ("people|user|human|team|collaboration", ["users", "user", "accessibility"]),
    ("future|next|direction|implication|conclusion", ["rocket", "flag", "trophy", "book", "file-text"]),
    ("growth|increase|improve|gain|rise", ["arrow-trend-up", "chart-line"]),
    ("decline|decrease|reduce|loss|drop", ["arrow-trend-down"]),
    ("success|complete|achieve|check|done", ["circle-checkmark", "circle-check", "badge-check"]),
    ("efficiency|speed|fast|optimize|performance", ["bolt", "zap", "gauge"]),
    ("communication|message|chat|feedback", ["comment", "message", "mail"]),
    ("global|world|international|region", ["globe", "world", "map-pin"]),
    ("time|deadline|schedule|clock|duration", ["clock", "calendar", "hourglass"]),
    ("money|finance|cost|budget|price", ["dollar", "currency-dollar", "coin"]),
]


def _search_icons_offline(
    page_info: dict,
    lib: str,
) -> list[dict]:
    """Match page title/purpose against concept patterns to find icons."""
    text = " ".join(
        [
            str(page_info.get("title", "")),
            str(page_info.get("purpose", "")),
            str(page_info.get("excerpt", "")),
        ]
    ).lower()
    seen: set[str] = set()
    candidates: list[dict] = []

    for pattern, names in OFFLINE_ICON_PALETTE:
        if not any(kw in text for kw in pattern.split("|")):
            continue
        for name in names:
            path = f"{lib}/{name}"
            if path in seen or not _icon_asset_exists(path):
                continue
            seen.add(path)
            candidates.append({
                "path": path,
                "name": name,
                "lib": lib,
                "category": pattern.split("|")[0],
                "tags": [],
                "score": 0.5,
            })

    return candidates


# ── Page info parsing ───────────────────────────────────────────────────────

def _parse_design_outline_pages(design_spec: str) -> dict[int, dict]:
    """Extract per-page hints from design spec Section IX Content Outline."""
    pages: dict[int, dict] = {}
    ix_match = re.search(
        r"(?ims)^#+\s*IX\.?\s*Content\s+Outline(.*?)(?=^#+\s*[XV]+\.?\s|\Z)",
        design_spec,
    )
    if not ix_match:
        return pages

    ix_text = ix_match.group(1)

    # Match patterns like "#### Slide 01 - Title" or "#### Page 01 - Title"
    for m in re.finditer(
        r"(?im)(?:slide|page)\s+0*(\d+)\s*[-–—:]\s*(.+?)(?:\n|$)",
        ix_text,
    ):
        page_num = int(m.group(1))
        title_line = m.group(2).strip()

        # Extract layout type from the block following this heading
        # Look for "Layout:" or "布局:" in the next few lines
        block_end = ix_text.find("\n####", m.end())
        if block_end == -1:
            block = ix_text[m.end():]
        else:
            block = ix_text[m.end():block_end]

        layout_match = re.search(r"(?im)(?:layout|布局)\s*[:：]\s*(.+?)(?:\n|$)", block)
        layout_type = layout_match.group(1).strip() if layout_match else ""

        purpose_match = re.search(r"(?im)(?:purpose|purpose|类型|type)\s*[:：]\s*(.+?)(?:\n|$)", block)
        purpose = purpose_match.group(1).strip() if purpose_match else ""

        pages[page_num] = {
            "page_num": page_num,
            "title": title_line,
            "purpose": purpose,
            "layout_type": layout_type,
            "outline_block": block.strip(),
        }

    return pages


def _plain_excerpt(page_content: str, *, limit: int = MAX_PAGE_EXCERPT_CHARS) -> str:
    text = strip_page_type_metadata(page_content)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"[*_#>\[\]()`]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _page_density_flags(page_content: str, layout_type: str = "") -> dict[str, bool | int]:
    visible = strip_page_type_metadata(page_content)
    lines = [line for line in visible.splitlines() if line.strip()]
    bullet_count = sum(1 for line in lines if re.match(r"\s*[-*+]\s+", line))
    has_table = "|" in visible and re.search(r"(?m)^\s*\|.+\|\s*$", visible) is not None
    has_paper_figure = "[[FIG:" in visible or "<image" in visible.lower()
    layout_lower = layout_type.lower()
    return {
        "chars": len(visible),
        "bullets": bullet_count,
        "has_table": has_table,
        "has_paper_figure": has_paper_figure,
        "layout_mentions_data": any(
            token in layout_lower
            for token in ("table", "chart", "figure", "data", "plot", "grid")
        ),
    }


def _eligibility_note(page_type: str, flags: dict[str, bool | int]) -> str:
    if page_type in FORBIDDEN_ICON_PAGE_TYPES:
        return f"FORCED_NONE: {page_type} pages must not use icons."
    if flags.get("has_paper_figure"):
        return "AVOID: page already carries a paper figure; use an icon only for a tiny labeled callout."
    if flags.get("has_table") or flags.get("layout_mentions_data"):
        return "CAUTION: dense/data layout; prefer None unless an icon anchors a KPI or warning callout."
    if int(flags.get("bullets") or 0) >= 6 or int(flags.get("chars") or 0) > 1300:
        return "CAUTION: text-dense page; icon must replace visual clutter, not add to it."
    return "ELIGIBLE: use an icon when it anchors a process step, KPI, limitation, or key contribution."


def _parse_page_info(design_spec: str, manuscript: str) -> list[dict]:
    """Build per-page icon planning context from design spec and manuscript.

    The manuscript is authoritative for page count and page type. This keeps
    structural slides (cover/chapter/toc/ending) out of the icon pipeline even
    if the design outline is loose or localized.
    """
    outline_by_page = _parse_design_outline_pages(design_spec)
    pages: list[dict] = []
    for page_num, page_content in enumerate(split_manuscript_pages(manuscript), start=1):
        outline = outline_by_page.get(page_num, {})
        page_type = extract_page_type(page_content)
        layout_type = str(outline.get("layout_type", ""))
        flags = _page_density_flags(page_content, layout_type)
        pages.append(
            {
                "page_num": page_num,
                "page_type": page_type,
                "title": str(outline.get("title") or page_title(page_content)),
                "purpose": str(outline.get("purpose", "")),
                "layout_type": layout_type,
                "excerpt": _plain_excerpt(page_content),
                "flags": flags,
                "eligibility": _eligibility_note(page_type, flags),
            }
        )
    return pages


# ── Icon asset validation ───────────────────────────────────────────────────

def _icon_asset_exists(icon_path: str) -> bool:
    """Check if an icon SVG file exists on disk."""
    if "/" not in icon_path:
        return False
    lib, name = icon_path.split("/", 1)
    return (settings.icons_dir / lib / f"{name}.svg").exists()


# ── Build per-page candidates table ─────────────────────────────────────────

def _format_candidates_table(
    pages: list[dict],
    per_page_candidates: dict[int, list[dict]],
) -> str:
    """Format per-page context and icon candidates as compact markdown."""
    lines: list[str] = []

    for page in pages:
        pn = page["page_num"]
        title = page["title"]
        candidates = per_page_candidates.get(pn, [])

        lines.append(f"**Page {pn:02d} [{page.get('page_type', 'content')}]** — {title}")
        if page.get("layout_type"):
            lines.append(f"- Layout: {page['layout_type']}")
        if page.get("purpose"):
            lines.append(f"- Purpose: {page['purpose']}")
        lines.append(f"- Eligibility: {page['eligibility']}")
        if page.get("excerpt"):
            lines.append(f"- Page content excerpt: {page['excerpt']}")
        if candidates:
            lines.append("| Icon Path | Category | Query | Score |")
            lines.append("|-----------|----------|-------|-------|")
            for c in candidates[:8]:
                cat = c.get("category", "-")
                query = str(c.get("query") or "-")[:80]
                score = c.get("score")
                score_text = f"{float(score):.3f}" if isinstance(score, (int, float)) else "-"
                lines.append(f"| `{c['path']}` | {cat} | {query} | {score_text} |")
        else:
            lines.append("*(no strong shortlist candidates; use None unless the full catalog has a clearly better structural match)*")
        lines.append("")

    return "\n".join(lines)


def _collect_icon_catalog(lib: str) -> list[dict]:
    index = get_icon_index()
    icons: list[dict] = []
    if index.is_available:
        icons = index.get_all_icons(lib)
    if icons:
        return icons

    lib_dir = settings.icons_dir / lib
    if not lib_dir.exists():
        return []
    return [
        {
            "path": f"{lib}/{svg.stem}",
            "name": svg.stem,
            "lib": lib,
            "category": "",
            "tags": [],
        }
        for svg in sorted(lib_dir.glob("*.svg"))
    ]


def _format_full_icon_catalog(lib: str) -> str:
    """Format the selected library as a compact exact-path catalog.

    This is intentionally a plain list: the icon round is already isolated
    from manuscript/layout generation, so it can spend attention on the icon
    inventory without distracting the main strategist prompt.
    """
    icons = _collect_icon_catalog(lib)
    if not icons:
        return f"## Full Selectable Icon Catalog\n\nNo local icons found for `{lib}`."

    paths = [f"`{icon['path']}`" for icon in icons if icon.get("path")]
    lines = [
        f"## Full Selectable Icon Catalog ({lib}, {len(paths)} icons)",
        "",
        "Use exact paths from this catalog. Prefer the per-page shortlist when it is semantically strong; use the full catalog when the shortlist misses a better natural metaphor.",
        "",
    ]
    for i in range(0, len(paths), 8):
        lines.append(", ".join(paths[i : i + 8]))
    return "\n".join(lines)


# ── Parse LLM output ────────────────────────────────────────────────────────

def _parse_icon_assignments(llm_output: str) -> dict[int, dict[str, str]]:
    """Parse the LLM's icon assignment table into structured assignments."""
    assignments: dict[int, dict[str, str]] = {}

    for raw_line in llm_output.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or "---" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2 or not re.fullmatch(r"0*\d+", cells[0]):
            continue
        page_num = int(cells[0])
        icon = cells[1].strip().strip("`")
        if not re.fullmatch(
            r"(?:chunk|tabler-filled|tabler-outline)/[^`\s|]+|None",
            icon,
        ):
            continue
        assignments[page_num] = {
            "icon": icon,
            "role": cells[2] if len(cells) > 2 else "",
            "anchor": cells[3] if len(cells) > 3 else "",
            "placement": cells[4] if len(cells) > 4 else "",
            "size": cells[5] if len(cells) > 5 else "",
            "reason": cells[6] if len(cells) > 6 else "",
        }

    # Backward-compatible parser for the old 3-column table.
    if not assignments:
        for m in re.finditer(
            r"\|\s*0*(\d+)\s*\|\s*`?((?:chunk|tabler-filled|tabler-outline)/[^`\s|]+|None)`?\s*\|",
            llm_output,
        ):
            page_num = int(m.group(1))
            assignments[page_num] = {
                "icon": m.group(2).strip(),
                "role": "",
                "anchor": "",
                "placement": "",
                "size": "",
                "reason": "",
            }

    return assignments


def _validate_icon_assignments(
    parsed: dict[int, dict[str, str]],
    pages: list[dict],
) -> dict[int, dict[str, str]]:
    """Normalize assignments, enforce structural bans, and remove bad paths."""
    page_by_num = {int(page["page_num"]): page for page in pages}
    validated: dict[int, dict[str, str]] = {}

    for page_num, page in page_by_num.items():
        incoming = dict(parsed.get(page_num, {}))
        icon = incoming.get("icon", "None").strip().strip("`") or "None"
        if page.get("page_type") in FORBIDDEN_ICON_PAGE_TYPES:
            icon = "None"
        elif icon != "None" and not _icon_asset_exists(icon):
            logger.warning(
                "Icon round: removing non-existent icon %s for page %d",
                icon,
                page_num,
            )
            icon = "None"

        incoming["icon"] = icon
        incoming.setdefault("role", "")
        incoming.setdefault("anchor", "")
        incoming.setdefault("placement", "")
        incoming.setdefault("size", "")
        incoming.setdefault("reason", "")
        incoming["page_type"] = str(page.get("page_type") or "content")
        validated[page_num] = incoming

    return validated


# ── Merge results into design spec ──────────────────────────────────────────

def _merge_icon_results(
    design_spec: str,
    icon_assignments: dict[int, dict[str, str]],
    icon_library: str,
) -> str:
    """Merge icon round results into the design spec.

    1. Replace/insert Section VI with icon inventory.
    2. Add `Icon:` lines to Section IX per-page entries.
    """
    text = design_spec

    # 1. Build Section VI content
    icon_rows: list[str] = []
    for pn in sorted(icon_assignments.keys()):
        assignment = icon_assignments[pn]
        icon = assignment.get("icon", "None")
        if icon and icon != "None":
            role = assignment.get("role") or "semantic anchor"
            anchor = assignment.get("anchor") or "nearest related content block"
            placement = assignment.get("placement") or "reserve a slot before laying out text"
            size = assignment.get("size") or "32x32px"
            icon_rows.append(
                f"| {pn:02d} | `{icon}` | {role} | {anchor} | {placement} | {size} |"
            )

    if icon_rows:
        vi_content = (
            f"### VI. Icon Usage Specification\n\n"
            f"- **Library**: `{icon_library}` (one library for entire deck)\n"
            f"- Icons are semantic layout anchors, not decorative accents.\n"
            f"- Cover, chapter/transition, TOC, and ending pages must not use icons.\n"
            f"- For assigned slides, reserve the icon slot first, then fit text and charts around it.\n"
            f"- Placement targets: process nodes, KPI/result callouts, warnings/limitations, key contribution cards.\n"
            f"- Placeholder: `<use data-icon=\"lib/name\" x=\"\" y=\"\" width=\"48\" height=\"48\" fill=\"\"/>`\n\n"
            f"| Slide | Icon Path | Role | Anchor | Placement | Size |\n"
            f"|-------|-----------|------|--------|-----------|------|\n"
            + "\n".join(icon_rows)
            + "\n"
        )
    else:
        vi_content = (
            "### VI. Icon Usage Specification\n\n"
            "No icons assigned. Do not use `<use data-icon/>` in any slide.\n"
            "Cover, chapter/transition, TOC, and ending pages are always icon-free.\n"
        )

    # Replace existing Section VI or insert before Section VII
    vi_pattern = r"(?ims)^#{1,4}\s*VI\.?\s*Icon\s+Usage.*?(?=^#{1,4}\s*VII\.?\s|\Z)"
    if re.search(vi_pattern, text):
        text = re.sub(vi_pattern, vi_content.rstrip(), text, flags=re.MULTILINE)
    else:
        # Insert before Section VII
        vii_pattern = r"(?m)^#{1,4}\s*VII\.?\s"
        if re.search(vii_pattern, text):
            text = re.sub(vii_pattern, vi_content.rstrip() + "\n\n### VII. ", text, count=1)

    # 2. Add Icon: lines to Section IX
    ix_pattern = r"(?ims)^#+\s*IX\.?\s*Content\s+Outline(.*?)(?=^#+\s*[XV]+\.?\s|\Z)"
    ix_match = re.search(ix_pattern, text)
    if ix_match and icon_assignments:
        ix_text = ix_match.group(1)
        for pn, icon in sorted(icon_assignments.items()):
            if icon.get("icon") == "None" or not icon.get("icon"):
                continue
            ix_text = _merge_icon_into_outline_page(ix_text, pn, icon)

        text = text[:ix_match.start()] + "### IX. Content Outline" + ix_text + text[ix_match.end():]

    return text


def _strip_existing_icon_lines(block: str) -> str:
    return re.sub(
        r"(?im)^\s*-\s*\*\*Icon(?:\s+(?:Role|Anchor|Placement|Size|Reason))?\*\*\s*:.*(?:\n|$)",
        "",
        block,
    )


def _icon_outline_lines(assignment: dict[str, str]) -> str:
    icon = assignment["icon"]
    role = assignment.get("role") or "semantic anchor"
    anchor = assignment.get("anchor") or "nearest related content block"
    placement = assignment.get("placement") or "reserve slot before text layout"
    size = assignment.get("size") or "32x32px"
    reason = assignment.get("reason") or "structural cue"
    return (
        f"- **Icon**: `{icon}` — role: {role}; anchor: {anchor}; "
        f"placement: {placement}; size: {size}; reason: {reason}\n"
    )


def _merge_icon_into_outline_page(ix_text: str, page_num: int, assignment: dict[str, str]) -> str:
    lines = _icon_outline_lines(assignment)

    heading_pattern = (
        rf"(?ims)(^#+\s*(?:slide|page)\s*0*{page_num}\b[^\n]*\n)"
        rf"(.*?)(?=^#+\s*(?:slide|page)\s*0*\d+\b|\Z)"
    )
    if re.search(heading_pattern, ix_text):
        def _replace_heading_block(match: re.Match) -> str:
            heading = match.group(1)
            body = _strip_existing_icon_lines(match.group(2)).lstrip("\n")
            return heading + lines + body

        return re.sub(heading_pattern, _replace_heading_block, ix_text, count=1)

    bullet_pattern = rf"(?im)^(\s*[-*]\s*(?:slide|page)\s+0*{page_num}\s*[-–—:].*)$"
    if re.search(bullet_pattern, ix_text):
        return re.sub(
            bullet_pattern,
            lambda match: f"{match.group(1)}\n{lines.rstrip()}",
            ix_text,
            count=1,
        )

    return ix_text


def _page_icon_queries(page: dict) -> list[str]:
    raw_queries = [
        str(page.get("title", "")),
        str(page.get("purpose", "")),
        str(page.get("layout_type", "")),
        str(page.get("excerpt", ""))[:360],
    ]
    queries: list[str] = []
    for query in raw_queries:
        query = re.sub(r"\s+", " ", query).strip()
        if query and query not in queries:
            queries.append(query)
    return queries[:4]


# ── Main entry point ────────────────────────────────────────────────────────

async def run_icon_round(
    design_spec: str,
    manuscript: str,
    icon_library: str,
    llm: LLMProvider,
    model: str,
    *,
    enable_icon_rag: bool = False,
    gemini_api_key: str | None = None,
    debug_dir: Path | None = None,
) -> str:
    """Run the Icon Decoration Round (Strategist Phase 2).

    Args:
        design_spec: Phase 1 design spec output.
        manuscript: Original manuscript (for semantic queries).
        icon_library: Icon library prefix (chunk/tabler-filled/tabler-outline).
        llm: LLM provider.
        model: Model ID.
        enable_icon_rag: Use Gemini RAG for per-page icon search.
        gemini_api_key: API key for Gemini embedding.
        debug_dir: Optional directory for debug artifacts.

    Returns:
        Updated design_spec with icon inventory merged in.
    """
    # 1. Parse page info from Phase 1 design spec
    pages = _parse_page_info(design_spec, manuscript)
    if not pages:
        logger.warning("Icon round: no pages parsed from design spec, skipping")
        return design_spec

    # 2. Search icon candidates per page
    per_page_candidates: dict[int, list[dict]] = {}

    if enable_icon_rag:
        for page in pages:
            if page.get("page_type") in FORBIDDEN_ICON_PAGE_TYPES:
                per_page_candidates[page["page_num"]] = []
                continue
            candidates = await _search_icons_rag(
                _page_icon_queries(page),
                icon_library,
                gemini_api_key,
                k=ICON_RAG_CANDIDATES_PER_QUERY,
            )
            per_page_candidates[page["page_num"]] = candidates
    else:
        for page in pages:
            if page.get("page_type") in FORBIDDEN_ICON_PAGE_TYPES:
                per_page_candidates[page["page_num"]] = []
                continue
            candidates = _search_icons_offline(page, icon_library)
            per_page_candidates[page["page_num"]] = candidates

    # 3. Build prompt and call LLM
    prompt_template = PROMPT_PATH.read_text(encoding="utf-8")
    candidates_table = _format_candidates_table(pages, per_page_candidates)
    prompt_text = (
        prompt_template
        .replace("{per_page_candidates_table}", candidates_table)
        .replace("{full_icon_catalog}", _format_full_icon_catalog(icon_library))
    )

    messages = [
        LLMMessage.system(prompt_text),
        LLMMessage.user(
            "Decide icon assignments for each page. Follow the rules strictly. "
            "Return the table only."
        ),
    ]

    # Save debug
    if debug_dir:
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / "icon_round_prompt.md").write_text(
                f"--- SYSTEM ---\n\n{prompt_text}\n\n--- USER ---\n\n{messages[1].content}",
                encoding="utf-8",
            )
        except Exception:
            pass

    from backend.llm import LLMResponse

    response: LLMResponse = await llm.chat(
        messages,
        model,
        temperature=0.2,
        max_tokens=ICON_ROUND_MAX_TOKENS,
    )

    llm_output = response.content.strip()

    # Save debug output
    if debug_dir:
        try:
            (debug_dir / "icon_round_output.md").write_text(llm_output, encoding="utf-8")
        except Exception:
            pass

    # 4. Parse and validate LLM output
    icon_assignments = _parse_icon_assignments(llm_output)
    validated = _validate_icon_assignments(icon_assignments, pages)

    logger.info("Icon round: assigned icons to %d/%d pages",
                sum(1 for v in validated.values() if v.get("icon") != "None"), len(pages))

    # 5. Merge into design spec
    return _merge_icon_results(design_spec, validated, icon_library)
