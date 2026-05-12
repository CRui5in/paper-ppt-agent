"""Icon Decoration Round — Phase 2 of the Strategist pipeline.

Runs after the base design spec (Phase 1) is produced.
Analyzes each page's semantic role, searches for matching icons,
then asks the LLM to produce a sparse, evenly-distributed icon inventory.
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

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "icon_round.md"
ICON_ROUND_MAX_TOKENS = 4096

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
    text = f"{page_info.get('title', '')} {page_info.get('purpose', '')}".lower()
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

def _parse_page_info(design_spec: str) -> list[dict]:
    """Extract per-page info from design spec Section IX Content Outline.

    Returns list of dicts: page_num, title, purpose, layout_type.
    """
    pages: list[dict] = []

    # Extract Section IX content
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

        pages.append({
            "page_num": page_num,
            "title": title_line,
            "purpose": purpose,
            "layout_type": layout_type,
        })

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
    """Format per-page icon candidates as a compact markdown table."""
    lines: list[str] = []

    for page in pages:
        pn = page["page_num"]
        title = page["title"]
        candidates = per_page_candidates.get(pn, [])

        lines.append(f"**Page {pn:02d}** — {title}")
        if candidates:
            lines.append("| Icon Path | Category |")
            lines.append("|-----------|----------|")
            for c in candidates[:5]:  # max 5 candidates per page
                cat = c.get("category", "-")
                lines.append(f"| `{c['path']}` | {cat} |")
        else:
            lines.append("*(no matching candidates — use None)*")
        lines.append("")

    return "\n".join(lines)


# ── Parse LLM output ────────────────────────────────────────────────────────

def _parse_icon_assignments(llm_output: str) -> dict[int, str]:
    """Parse the LLM's icon assignment table into {page_num: icon_path}.

    icon_path is "lib/name" or "None".
    """
    assignments: dict[int, str] = {}

    for m in re.finditer(
        r"\|\s*0*(\d+)\s*\|\s*`?((?:chunk|tabler-filled|tabler-outline)/[^`\s|]+|None)`?\s*\|",
        llm_output,
    ):
        page_num = int(m.group(1))
        icon = m.group(2).strip()
        assignments[page_num] = icon

    return assignments


# ── Merge results into design spec ──────────────────────────────────────────

def _merge_icon_results(
    design_spec: str,
    icon_assignments: dict[int, str],
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
        icon = icon_assignments[pn]
        if icon and icon != "None":
            icon_rows.append(f"| {pn:02d} | `{icon}` | Decorative accent |")

    if icon_rows:
        vi_content = (
            f"### VI. Icon Usage Specification\n\n"
            f"- **Library**: `{icon_library}` (one library for entire deck)\n"
            f"- Icons are sparse and purposeful; most slides have no icon.\n"
            f"- Placement targets: chapter headers, process steps, KPI highlights.\n"
            f"- Placeholder: `<use data-icon=\"lib/name\" x=\"\" y=\"\" width=\"48\" height=\"48\" fill=\"\"/>`\n\n"
            f"| Slide | Icon Path | Role |\n"
            f"|-------|-----------|------|\n"
            + "\n".join(icon_rows)
            + "\n"
        )
    else:
        vi_content = (
            "### VI. Icon Usage Specification\n\n"
            "No icons assigned. Do not use `<use data-icon/>` in any slide.\n"
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
            if icon == "None" or not icon:
                continue
            # Find the page entry in Section IX
            page_pattern = rf"(?im)((?:slide|page)\s+0*{pn}\s*[-–—:].*?)(\n)(?=\n|\Z|####|\n[-*]|\n\|)"
            # Add Icon: line after the page heading, before the next entry
            def _add_icon_line(m: re.Match) -> str:
                return f"{m.group(1)}\n- **Icon**: `{icon}`\n"
            ix_text = re.sub(page_pattern, _add_icon_line, ix_text, count=1)

        text = text[:ix_match.start()] + "### IX. Content Outline" + ix_text + text[ix_match.end():]

    return text


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
    pages = _parse_page_info(design_spec)
    if not pages:
        logger.warning("Icon round: no pages parsed from design spec, skipping")
        return design_spec

    # 2. Search icon candidates per page
    per_page_candidates: dict[int, list[dict]] = {}

    if enable_icon_rag:
        for page in pages:
            queries = [page["title"]]
            if page["purpose"]:
                queries.append(page["purpose"])
            candidates = await _search_icons_rag(queries, icon_library, gemini_api_key)
            per_page_candidates[page["page_num"]] = candidates
    else:
        for page in pages:
            candidates = _search_icons_offline(page, icon_library)
            per_page_candidates[page["page_num"]] = candidates

    # 3. Build prompt and call LLM
    prompt_template = PROMPT_PATH.read_text(encoding="utf-8")
    candidates_table = _format_candidates_table(pages, per_page_candidates)
    prompt_text = prompt_template.replace("{per_page_candidates_table}", candidates_table)

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

    # 4. Parse LLM output
    icon_assignments = _parse_icon_assignments(llm_output)

    # Validate: remove non-existent icons
    validated: dict[int, str] = {}
    for pn, icon in icon_assignments.items():
        if icon == "None" or not icon:
            validated[pn] = "None"
        elif _icon_asset_exists(icon):
            validated[pn] = icon
        else:
            logger.warning("Icon round: removing non-existent icon %s for page %d", icon, pn)
            validated[pn] = "None"

    logger.info("Icon round: assigned icons to %d/%d pages",
                sum(1 for v in validated.values() if v != "None"), len(pages))

    # 5. Merge into design spec
    return _merge_icon_results(design_spec, validated, icon_library)
