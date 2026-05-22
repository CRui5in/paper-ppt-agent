"""Whitelist + per-page allowlist validation for placeholder names.

Rejects ``unknown_name`` / ``not_allowed_for_page_type`` element actions
and de-duplicates same-page same-placeholder occurrences (first wins,
others demote to ``remove``). Mirrors ``design.md`` §placeholder_schema.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover
    from .types import ElementAction


PLACEHOLDER_WHITELIST: frozenset[str] = frozenset({
    "TITLE", "PAGE_TITLE", "SUBTITLE", "AUTHOR", "DATE", "GROUP",
    "CHAPTER_NUM", "CHAPTER_NUMBER", "CHAPTER_TITLE",
    "TOC_LIST", "TOC_ITEM_1", "TOC_ITEM_2", "TOC_ITEM_3", "TOC_ITEM_4", "TOC_ITEM_5",
    "CONTENT_AREA",
    "ENDING_TITLE", "ENDING_MESSAGE",
    "LOGO_HEADER", "LOGO_FOOTER",
})

PAGE_TYPE_ALLOWLIST: dict[str, frozenset[str]] = {
    "cover":   frozenset({"TITLE", "PAGE_TITLE", "SUBTITLE", "AUTHOR", "DATE", "GROUP", "LOGO_HEADER", "LOGO_FOOTER"}),
    "toc":     frozenset({"PAGE_TITLE", "TOC_LIST", "TOC_ITEM_1", "TOC_ITEM_2", "TOC_ITEM_3", "TOC_ITEM_4", "TOC_ITEM_5", "LOGO_HEADER", "LOGO_FOOTER"}),
    "chapter": frozenset({"CHAPTER_NUM", "CHAPTER_NUMBER", "CHAPTER_TITLE", "LOGO_HEADER", "LOGO_FOOTER"}),
    "content": frozenset({"PAGE_TITLE", "CONTENT_AREA", "LOGO_HEADER", "LOGO_FOOTER"}),
    "ending":  frozenset({"ENDING_TITLE", "ENDING_MESSAGE", "LOGO_HEADER", "LOGO_FOOTER"}),
}


@dataclass
class PlaceholderViolation:
    """A single rejected element action with its rejection reason."""

    page_type: str
    placeholder: str
    element_id: str
    reason: Literal["unknown_name", "not_allowed_for_page_type", "duplicate_in_page"]


def validate_actions(
    actions: list["ElementAction"],
    *,
    page_type_for_slide: dict[int, str],
) -> tuple[list["ElementAction"], list[PlaceholderViolation]]:
    """Split ``actions`` into ``(legal, violations)``.

    Rules (in order):
      1. ``action != "replace_with_placeholder"`` → pass through unchanged.
      2. ``placeholder`` is ``None`` → pass through unchanged (acts like
         a remove of an element; nothing to validate against the
         whitelist or allowlist).
      3. ``placeholder`` not in :data:`PLACEHOLDER_WHITELIST` →
         reject (``unknown_name``).
      4. ``placeholder`` not in
         ``PAGE_TYPE_ALLOWLIST[action.page_type]`` →
         reject (``not_allowed_for_page_type``).
      5. Same ``(page_type, placeholder)`` pair: keep first occurrence;
         demote duplicates to ``action="remove"`` with ``placeholder``
         cleared (the demoted action stays in ``legal_actions`` and a
         ``duplicate_in_page`` violation is recorded).

    ``page_type_for_slide`` (``dict[int, str]``, slide_index → page_type)
    is currently unused by the rules above — placeholder allowlists are
    keyed by ``action.page_type`` which already lives on each action.
    The parameter is kept for forward compatibility (e.g. future
    cross-checks against the canonical page-type assignment per slide).
    """
    legal: list["ElementAction"] = []
    violations: list[PlaceholderViolation] = []
    seen: set[tuple[str, str]] = set()

    for action in actions:
        # Rule 1: non-placeholder actions pass through untouched.
        if action.action != "replace_with_placeholder":
            legal.append(action)
            continue

        page_type = action.page_type
        placeholder = action.placeholder

        # Rule 2: missing placeholder → effectively a remove, pass through.
        if placeholder is None:
            legal.append(action)
            continue

        # Rule 3: unknown placeholder name.
        if placeholder not in PLACEHOLDER_WHITELIST:
            violations.append(
                PlaceholderViolation(
                    page_type=page_type,
                    placeholder=placeholder,
                    element_id=action.element_id,
                    reason="unknown_name",
                )
            )
            continue

        # Rule 4: placeholder not allowed for this page type.
        allowed = PAGE_TYPE_ALLOWLIST.get(page_type, frozenset())
        if placeholder not in allowed:
            violations.append(
                PlaceholderViolation(
                    page_type=page_type,
                    placeholder=placeholder,
                    element_id=action.element_id,
                    reason="not_allowed_for_page_type",
                )
            )
            continue

        # Rule 5: dedupe within (page_type, placeholder).
        key = (page_type, placeholder)
        if key in seen:
            legal.append(replace(action, action="remove", placeholder=None))
            violations.append(
                PlaceholderViolation(
                    page_type=page_type,
                    placeholder=placeholder,
                    element_id=action.element_id,
                    reason="duplicate_in_page",
                )
            )
            continue

        seen.add(key)
        legal.append(action)

    return legal, violations
