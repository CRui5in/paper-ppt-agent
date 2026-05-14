"""Provider-specific orchestration guidance."""

from __future__ import annotations

from backend.llm import LLMProvider


def is_deepseek_provider(llm: LLMProvider, model: str) -> bool:
    """Return true when the active request is routed to DeepSeek."""
    model_id = (model or "").lower()
    if model_id.startswith("deepseek"):
        return True

    provider_name = str(getattr(llm, "_provider_name", "") or "").lower()
    if provider_name == "deepseek":
        return True

    base_url = str(getattr(llm, "_base_url", "") or "").lower()
    if "api.deepseek.com" in base_url:
        return True

    try:
        info = llm.get_provider_info()
    except Exception:
        return False
    return getattr(info, "name", "").lower() == "deepseek"


def deepseek_research_guidance(detail_level: str) -> str:
    if detail_level != "very_high":
        return (
            "## DeepSeek Calibration\n\n"
            "Preserve concrete paper details in the final answer. Avoid collapsing method, "
            "evidence, or result slides into generic labels."
        )
    return (
        "## DeepSeek Calibration\n\n"
        "For this `very_high` deck, keep the final manuscript analytically dense but still "
        "slide-ready:\n"
        "- For each substantive slide, include mechanism, evidence/data, and implication "
        "when the paper provides them.\n"
        "- Structural pages are exempt from this density target: keep cover, "
        "chapter/transition, and ending slides minimal, with no supporting "
        "bullet lists, metric blocks, or evidence sections.\n"
        "- Method slides should name the actual modules, training/inference flow, "
        "objective functions, routing decisions, or architectural constraints instead "
        "of only high-level labels.\n"
        "- Result slides should include the key metric, comparison baseline, and what "
        "the number proves.\n"
        "- Avoid tag-only or slogan-only bullets. Prefer 3-5 information-bearing bullets "
        "or equivalent structured blocks per content slide."
    )


def deepseek_strategy_guidance(detail_level: str) -> str:
    if detail_level != "very_high":
        return (
            "## Detail Level Guidelines\n\n"
            "Convert manuscript substance into concrete layout plans. Do not reduce rich "
            "slides to decorative tags or short labels."
        )
    return (
        "## Detail Level Guidelines\n\n"
        "For `very_high`, the design spec must preserve the manuscript's analytical "
        "depth while keeping layouts readable:\n"
        "- In section IX, each content page must list the concrete content blocks to "
        "render, not just a layout name. Do not apply this content-block rule to "
        "cover, chapter/transition, TOC, or ending pages.\n"
        "- For cover/chapter/ending pages, list only the title and at most one short "
        "subtitle/key phrase. Do not create question lists, KPI rows, cards, or "
        "`核心问题` / `本章看点` blocks for structural pages.\n"
        "- Preserve mechanism/evidence/implication from the manuscript. If a page has "
        "a method flow, result table, ablation, or limitation, the spec must say how "
        "that information appears visually.\n"
        "- Use tags only as small supporting labels. Do not make many capsule tags the "
        "main content.\n"
        "- Leave enough padding and wrapping room; prefer fewer, richer blocks over "
        "many tiny fragments near card edges."
    )


def deepseek_executor_guidance(detail_level: str) -> str:
    if detail_level != "very_high":
        return (
            "## Detail Level Guidelines\n\n"
            "Keep the SVG faithful to the manuscript. Do not collapse content into a "
            "few generic tags when there is room for concise explanatory text."
        )
    return (
        "## Detail Level Guidelines\n\n"
        "For `very_high`, preserve depth without overcrowding:\n"
        "- Each substantive slide should render the manuscript's core mechanism, "
        "evidence/data, and implication when present.\n"
        "- Avoid label-only slides and excessive capsule tags. Use tags only for "
        "meaningful categories, legends, or process states.\n"
        "- Prefer 3-5 readable content blocks with wrapped text over many tiny labels.\n"
        "- Keep all text inside safe margins with generous internal padding; never put "
        "text flush against card or pill edges.\n"
        "- If a real paper figure is unavailable or disallowed, redraw the idea with "
        "native SVG rather than repeating or inventing an image."
    )
