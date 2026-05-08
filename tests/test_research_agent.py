from __future__ import annotations

from backend.orchestrator.research_agent import (
    _extract_manuscript_from_review,
    _manuscript_structure_error,
    _target_slides_guidance,
)


def test_extract_review_pass_keeps_original_manuscript_when_report_is_prepended():
    original = """## Slide 1: Clean Start

Body

---

## Slide 2: Clean End

Body
"""
    review = """## Step 2: Consolidated Assessment

Consensus Scores: 35/35

---

## Step 3: Revised Manuscript

QUALITY_CHECK_PASSED

---

## Slide Manuscript (Unchanged)

---

## Slide 1: Echoed Start

Body
"""

    assert _extract_manuscript_from_review(review, original) == original


def test_extract_review_revised_manuscript_after_marker():
    original = "## Slide 1: Old\n\nBody"
    review = """## Review

Needs revision.

## Final Slide Manuscript

## Slide 1: New

Better body

---

## Slide 2: Added

More body
"""

    extracted = _extract_manuscript_from_review(review, original)

    assert extracted.startswith("## Slide 1: New")
    assert "## Review" not in extracted


def test_manuscript_structure_requires_default_budget_and_closing_ending():
    parts = ["<!-- page_type: cover -->\n# Title"]
    for chapter in range(1, 4):
        parts.append(f"<!-- page_type: chapter -->\n# Chapter {chapter}")
        for slide in range(1, 5):
            parts.append(
                f"<!-- page_type: content -->\n## {chapter}.{slide} Content\n\n- point"
            )
    parts.append("<!-- page_type: content -->\n## Extra Content\n\n- point")
    parts.append("<!-- page_type: ending -->\n# 谢谢聆听\n\nQ&A")

    assert _manuscript_structure_error("\n\n---\n\n".join(parts), None) is None


def test_manuscript_structure_rejects_summary_as_ending():
    parts = ["<!-- page_type: cover -->\n# Title"]
    parts.extend(f"<!-- page_type: chapter -->\n# Chapter {i}" for i in range(1, 4))
    parts.extend(f"<!-- page_type: content -->\n## Content {i}" for i in range(1, 14))
    parts.append("<!-- page_type: ending -->\n# 总结与展望\n\n- Key takeaway")

    error = _manuscript_structure_error("\n\n---\n\n".join(parts), None)

    assert error == "ending slide must be a closing/thanks page"


def test_target_slide_guidance_expands_for_very_high_detail():
    guidance = _target_slides_guidance(None, "very_high")

    assert "Produce exactly 26 slides" in guidance
    assert "content 21" in guidance
