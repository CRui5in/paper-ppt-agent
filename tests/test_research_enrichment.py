from __future__ import annotations

from backend.orchestrator.research_enrichment import (
    ResearchFinding,
    _filter_relevant_findings,
)


def test_relevance_filter_drops_web_results_that_only_match_generic_words() -> None:
    title = "VFR: Visibility-aware Fine-grained Refinement for Real-time Multi-person Pose Estimation"
    abstract = "We study occluded keypoint visibility and pose refinement in crowded scenes."
    findings = [
        ResearchFinding(
            source="web",
            title="Learning to Refine with Fine-Grained Natural Language Feedback",
            abstract="This paper studies fine-grained feedback for language models.",
        ),
        ResearchFinding(
            source="web",
            title="Visibility-aware pose estimation under occlusion",
            abstract="A follow-up discussion about occluded keypoints and multi-person pose estimation.",
        ),
    ]

    filtered = _filter_relevant_findings(findings, title, abstract)

    assert [item.title for item in filtered] == [
        "Visibility-aware pose estimation under occlusion"
    ]
    assert filtered[0].relevance_note.startswith("Matched:")
