from __future__ import annotations

from backend.orchestrator.research_agent import _extract_manuscript_from_review


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
