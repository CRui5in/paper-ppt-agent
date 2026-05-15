from __future__ import annotations

import pytest

from backend.llm import LLMResponse
from backend.orchestrator.research_agent import (
    analyze_paper,
    _extract_manuscript_from_review,
    _figure_token_inventory_block,
    _manuscript_structure_error,
    _manuscript_figure_token_error,
    _run_single_pass_analysis,
    _target_slides_guidance,
)
from backend.parser.paper_model import PaperFigure, PaperSection, ParsedPaper


class _FakeResearchLLM:
    def __init__(self, response: str | list[str]) -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.calls = 0
        self.requests: list[dict] = []

    async def chat(self, messages, model, **kwargs) -> LLMResponse:
        self.requests.append({"messages": messages, "model": model, **kwargs})
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return LLMResponse(content=self.responses[index])


def _four_slide_manuscript(content_body: str | None = None) -> str:
    content = content_body or (
        "**Transformer 的关键主张是完全移除循环和卷积，只用自注意力完成序列转导。**\n\n"
        "- 论文依据：Table 1 shows self-attention has O(1) sequential operations and O(1) maximum path length, compared with O(n) recurrent layers.\n"
        "- 机制解释：因此任意两个 token 可以直接交互，训练并行度提高，长程依赖不再经过多步传播。\n"
        "- So what: This mechanism supports the BLEU gains and lower training cost reported later."
    )
    parts = [
        "<!-- page_type: cover -->\n# Attention Is All You Need\n\nVaswani et al., NeurIPS 2017",
        "<!-- page_type: chapter -->\n# 核心贡献\n\n全注意力架构",
        f"<!-- page_type: content -->\n## 自注意力替代循环\n\n{content}",
        "<!-- page_type: ending -->\n# 谢谢聆听\n\nQ&A",
    ]
    return "\n\n---\n\n".join(parts)


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


def test_extract_review_plain_revised_manuscript_marker():
    original = "## Slide 1: Old\n\nBody"
    review = """## Step 3: Revised Manuscript

## Slide 1: New

Better body

---

## Slide 2: Added

More body
"""

    extracted = _extract_manuscript_from_review(review, original)

    assert extracted.startswith("## Slide 1: New")
    assert "## Step 3" not in extracted


@pytest.mark.asyncio
async def test_non_deep_generates_lightweight_brief_before_manuscript(tmp_path):
    paper = ParsedPaper(
        title="Attention Is All You Need",
        abstract="Transformer removes recurrence for sequence transduction.",
        sections=[
            PaperSection(
                title="Method",
                level=1,
                content="Self-attention uses O(1) sequential operations and improves BLEU.",
            )
        ],
    )
    brief = (
        "## Core problem\n"
        "RNNs serialize sequence computation.\n\n"
        "## Key evidence\n"
        "Table 1: self-attention has O(1) sequential operations."
    )
    llm = _FakeResearchLLM([brief, _four_slide_manuscript()])
    progress: list[tuple[str, float]] = []

    manuscript = await analyze_paper(
        paper,
        llm,
        "fake-model",
        num_pages=4,
        language="zh",
        debug_dir=tmp_path,
        on_progress=lambda message, value: progress.append((message, value)),
    )

    assert manuscript == _four_slide_manuscript()
    assert llm.calls == 2
    assert (tmp_path / "paper_brief.md").read_text(encoding="utf-8") == brief
    prompt = (tmp_path / "research_single_pass_prompt.md").read_text(encoding="utf-8")
    assert "Paper Brief (authoritative substance for content slides)" in prompt
    assert "Table 1: self-attention has O(1) sequential operations" in prompt
    assert not (tmp_path / "research_pass2_prompt.md").exists()
    assert all("Pass 2/4" not in message for message, _ in progress)


@pytest.mark.asyncio
async def test_deep_research_does_not_generate_lightweight_brief(tmp_path):
    paper = ParsedPaper(title="Paper", abstract="Abstract")
    llm = _FakeResearchLLM(
        [
            "Deep analysis with concrete evidence.",
            "Narrative plan.",
            _four_slide_manuscript(),
            "QUALITY_CHECK_PASSED\n\n" + _four_slide_manuscript(),
        ]
    )
    progress: list[tuple[str, float]] = []

    manuscript = await analyze_paper(
        paper,
        llm,
        "fake-model",
        num_pages=4,
        enable_deep_research=True,
        debug_dir=tmp_path,
        on_progress=lambda message, value: progress.append((message, value)),
    )

    assert manuscript == _four_slide_manuscript()
    assert llm.calls == 4
    assert not (tmp_path / "paper_brief.md").exists()
    assert (tmp_path / "research_pass2_prompt.md").exists()
    assert any("Pass 2/4" in message for message, _ in progress)


@pytest.mark.asyncio
async def test_single_pass_shallow_manuscript_gets_one_depth_rewrite(tmp_path):
    shallow = _four_slide_manuscript(
        "**Transformer is important.**\n\n"
        "- It improves sequence modeling."
    )
    revised = _four_slide_manuscript()
    llm = _FakeResearchLLM([shallow, revised])
    paper = ParsedPaper(title="Paper", abstract="Abstract")

    manuscript = await _run_single_pass_analysis(
        paper,
        llm,
        "fake-model",
        num_pages=4,
        paper_brief="Table 1 shows O(1) sequential operations and BLEU gains.",
        debug_dir=tmp_path,
    )

    assert manuscript == revised
    assert llm.calls == 2
    assert (tmp_path / "research_depth_rewrite_prompt.md").exists()


@pytest.mark.asyncio
async def test_single_pass_content_rich_manuscript_skips_depth_rewrite(tmp_path):
    manuscript = _four_slide_manuscript()
    llm = _FakeResearchLLM(manuscript)
    paper = ParsedPaper(title="Paper", abstract="Abstract")

    result = await _run_single_pass_analysis(
        paper,
        llm,
        "fake-model",
        num_pages=4,
        paper_brief="Table 1 shows O(1) sequential operations and BLEU gains.",
        debug_dir=tmp_path,
    )

    assert result == manuscript
    assert llm.calls == 1
    assert not (tmp_path / "research_depth_rewrite_prompt.md").exists()


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


def test_manuscript_structure_rejects_content_blocks_on_chapter_page():
    parts = ["<!-- page_type: cover -->\n# Title"]
    parts.append(
        "<!-- page_type: chapter -->\n"
        "# 1. 问题与目标\n\n"
        "**核心问题**\n\n"
        "- vanilla attention 的二次复杂度限制超长上下文\n"
        "- agent 工作流需要更长上下文\n\n"
        "**本章看点**\n\n"
        "- 能否高效稳定部署 1M tokens"
    )
    for idx in range(1, 13):
        parts.append(f"<!-- page_type: content -->\n## Content {idx}\n\n- point")
    parts.append("<!-- page_type: ending -->\n# 谢谢聆听\n\nQ&A")

    error = _manuscript_structure_error("\n\n---\n\n".join(parts), None)

    assert error is not None
    assert "chapter" in error
    assert "bullet or numbered-list content" in error or "核心问题" in error


def test_manuscript_structure_rejects_trailing_orphan_chapter():
    parts = ["<!-- page_type: cover -->\n# Title"]
    for chapter in range(1, 4):
        parts.append(f"<!-- page_type: chapter -->\n# Chapter {chapter}")
        for slide in range(1, 5):
            parts.append(
                f"<!-- page_type: content -->\n## {chapter}.{slide} Content\n\n- point"
            )
    parts.append("<!-- page_type: chapter -->\n# Chapter 4")
    parts.append("<!-- page_type: ending -->\n# 谢谢聆听\n\nQ&A")

    error = _manuscript_structure_error("\n\n---\n\n".join(parts), None)

    assert error is not None
    assert "chapter slide" in error
    assert "introduces only 0" in error


@pytest.mark.asyncio
async def test_single_pass_invalid_manuscript_continues_after_retries():
    parts = ["<!-- page_type: cover -->\n# Title"]
    for chapter in range(1, 4):
        parts.append(f"<!-- page_type: chapter -->\n# Chapter {chapter}")
        for slide in range(1, 5):
            parts.append(
                f"<!-- page_type: content -->\n## {chapter}.{slide} Content\n\n- point"
            )
    parts.append("<!-- page_type: chapter -->\n# Chapter 4")
    parts.append("<!-- page_type: ending -->\n# 谢谢聆听\n\nQ&A")
    llm = _FakeResearchLLM("\n\n---\n\n".join(parts))
    paper = ParsedPaper(title="Paper", abstract="Abstract")

    manuscript = await _run_single_pass_analysis(paper, llm, "fake-model")

    assert "Chapter 4" in manuscript
    assert llm.calls == 3


def test_manuscript_structure_allows_lightweight_cover_context():
    parts = [
        "<!-- page_type: cover -->\n"
        "# VFR\n\n"
        "## 轻量级可见性感知精炼\n\n"
        "- 任务：实时姿态估计\n"
        "- 结果：67.6% AP"
    ]
    for chapter in range(1, 4):
        parts.append(f"<!-- page_type: chapter -->\n# Chapter {chapter}")
        for slide in range(1, 5):
            parts.append(
                f"<!-- page_type: content -->\n## {chapter}.{slide} Content\n\n- point"
            )
    parts.append("<!-- page_type: content -->\n## Extra Content\n\n- point")
    parts.append("<!-- page_type: ending -->\n# 谢谢聆听\n\nQ&A")

    error = _manuscript_structure_error("\n\n---\n\n".join(parts), None)

    assert error is None


def test_manuscript_structure_rejects_overfull_cover_page():
    parts = [
        "<!-- page_type: cover -->\n"
        "# VFR\n\n"
        "## 轻量级可见性感知精炼\n\n"
        "- 任务：实时姿态估计\n"
        "- 方法：可见性感知 refinement\n"
        "- 机制：质量引导损失\n"
        "- 数据：COCO val2017\n"
        "- 结果：67.6% AP\n"
        "- 消融：模块贡献\n"
        "- 局限：遮挡场景"
    ]
    for chapter in range(1, 4):
        parts.append(f"<!-- page_type: chapter -->\n# Chapter {chapter}")
        for slide in range(1, 5):
            parts.append(
                f"<!-- page_type: content -->\n## {chapter}.{slide} Content\n\n- point"
            )
    parts.append("<!-- page_type: content -->\n## Extra Content\n\n- point")
    parts.append("<!-- page_type: ending -->\n# 谢谢聆听\n\nQ&A")

    error = _manuscript_structure_error("\n\n---\n\n".join(parts), None)

    assert error is not None
    assert "cover" in error
    assert "too much body content" in error


def test_target_slide_guidance_expands_for_very_high_detail():
    guidance = _target_slides_guidance(None, "very_high")

    assert "Choose 22-34 slides" in guidance
    assert "Do not force a fixed chapter count" in guidance


def test_figure_token_inventory_lists_exact_valid_tokens(tmp_path):
    paper = ParsedPaper(
        title="Paper",
        sections=[
            PaperSection(
                title="Method",
                level=1,
                figures=[
                    PaperFigure(
                        path=tmp_path / "fig_001_p3.png",
                        caption="Figure 1. Architecture overview.",
                        natural_width=100,
                        natural_height=50,
                    )
                ],
            )
        ],
    )

    block = _figure_token_inventory_block(paper)

    assert "`[[FIG:fig_001_p3]]`" in block
    assert "Architecture overview" in block
    assert "fig_arch" in block


def test_manuscript_figure_token_error_rejects_semantic_alias(tmp_path):
    paper = ParsedPaper(
        title="Paper",
        sections=[
            PaperSection(
                title="Method",
                level=1,
                figures=[
                    PaperFigure(
                        path=tmp_path / "fig_001_p3.png",
                        caption="Figure 1. Architecture overview.",
                    )
                ],
            )
        ],
    )
    manuscript = "[[FIG:fig_arch]]\n\n[[FIG:fig_001_p3]]"

    error = _manuscript_figure_token_error(manuscript, paper)

    assert error is not None
    assert "[[FIG:fig_arch]]" in error
    assert "[[FIG:fig_001_p3]]" in error
