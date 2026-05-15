from __future__ import annotations

from backend.orchestrator.deck_plan import (
    build_deck_plan,
    deck_plan_markdown,
    slide_plan_markdown,
)


def test_deck_plan_separates_chapter_index_from_page_number() -> None:
    manuscript = (
        "<!-- page_type: cover -->\n# Cover\n\n---\n\n"
        "<!-- page_type: chapter -->\n# 背景与动机\n\n## 为什么需要新模型\n\n---\n\n"
        "<!-- page_type: content -->\n# 当前挑战\n\n- point\n\n---\n\n"
        "<!-- page_type: chapter -->\n# 模型架构\n\n## 编码器与解码器\n\n---\n\n"
        "<!-- page_type: content -->\n# 编码器\n\n- point"
    )
    design_spec = """
## IX. Content Outline

#### Slide 02 - Chapter
- **Page Type**: chapter
- **Title**: 背景与动机
- **Style Family**: structural.chapter-divider
- **Layout Family**: section-divider-dark
- **Density**: minimal

#### Slide 04 - Chapter
- **Page Type**: chapter
- **Title**: 模型架构
- **Style Family**: structural.chapter-divider
- **Layout Family**: section-divider-dark
- **Density**: minimal
"""

    plan = build_deck_plan(manuscript, design_spec, detail_level="very_high")

    assert plan.by_page[2].chapter_label == "01"
    assert plan.by_page[4].chapter_label == "02"
    assert plan.by_page[2].style_family == plan.by_page[4].style_family

    context = slide_plan_markdown(plan, 2)
    assert "Footer page number: 2/5" in context
    assert "Current chapter index: 01" in context
    assert "never the footer page number" in context


def test_deck_plan_markdown_includes_authoritative_numbering_contract() -> None:
    manuscript = "<!-- page_type: chapter -->\n# 第一章"
    spec = "#### Slide 01\n- **Page Type**: chapter\n- **Layout**: dark divider"

    text = deck_plan_markdown(build_deck_plan(manuscript, spec))

    assert "Visible chapter/section number is the planned chapter index" in text
    assert "Do not derive chapter numbers from `Generate page N/Total`" in text
