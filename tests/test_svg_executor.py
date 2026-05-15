from __future__ import annotations

import pytest

from backend.generator.svg_critic import CriticReport
from backend.generator.visual_critic import VisualCheckOutcome
from backend.llm import LLMResponse
from backend.llm.types import ProviderInfo
from backend.orchestrator import svg_executor
from backend.orchestrator.svg_executor import generate_svg_pages
from backend.usage.tracker import current_usage_context


class _FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.calls = 0
        self.message_snapshots = []

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.message_snapshots.append(list(args[0]))
        response = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return LLMResponse(content=response)


class _DeepSeekLLM(_FakeLLM):
    def get_provider_info(self) -> ProviderInfo:
        return ProviderInfo(name="deepseek", display_name="DeepSeek")


def _svg(body: str) -> str:
    return (
        '```svg\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
        '<rect width="1280" height="720" fill="#fff"/>'
        f"{body}</svg>\n```"
    )


def test_chapter_parallel_groups_make_cover_and_ending_standalone() -> None:
    groups = svg_executor._chapter_parallel_groups(
        [
            {"page_num": 1, "page_type": "cover"},
            {"page_num": 2, "page_type": "chapter"},
            {"page_num": 3, "page_type": "content"},
            {"page_num": 4, "page_type": "chapter"},
            {"page_num": 5, "page_type": "content"},
            {"page_num": 6, "page_type": "ending"},
        ]
    )

    assert [[item["page_num"] for item in group] for group in groups] == [
        [1],
        [2, 3],
        [4, 5],
        [6],
    ]


def test_extract_page_design_block_stops_before_next_part_heading() -> None:
    design_spec = """
### IX. Content Outline

### Part 1: Problem

#### Slide 04 - Content
- **Page Type**: content
- **Title**: Current chapter content

### Part 2: Method

#### Slide 05 - Chapter
- **Page Type**: chapter
- **Title**: Next chapter
"""

    block = svg_executor._extract_page_design_block(design_spec, 4)

    assert "Current chapter content" in block
    assert "Part 2" not in block
    assert "Next chapter" not in block
    assert svg_executor._extract_page_section_heading(design_spec, 4) == "### Part 1: Problem"
    assert svg_executor._extract_page_section_heading(design_spec, 5) == "### Part 2: Method"


def test_template_skeleton_placeholders_use_chapter_index_not_page_number() -> None:
    pages = svg_executor.split_manuscript_pages(
        "<!-- page_type: cover -->\n# Cover\n\n---\n\n"
        "<!-- page_type: chapter -->\n# 第一章\n**背景**\n\n---\n\n"
        "<!-- page_type: content -->\n**内容页**\n\n---\n\n"
        "<!-- page_type: chapter -->\n# 第二章\n**方法**"
    )
    variables = svg_executor._template_vars_by_page(pages)

    resolved = svg_executor._resolve_template_skeleton_placeholders(
        "<svg><text>{{CHAPTER_NUM}}</text><text>0{{CHAPTER_NUM}}</text>"
        "<text>{{CHAPTER_TITLE}}</text><text>{{CHAPTER_DESC}}</text></svg>",
        variables[4],
    )

    assert "<text>02</text>" in resolved
    assert "<text>002</text>" not in resolved
    assert "第二章" in resolved
    assert "方法" in resolved
    assert "04" not in resolved


def test_structural_design_sanitizer_preserves_planner_style_fields() -> None:
    block = """
#### Slide 02 - Chapter
- **Page Type**: chapter
- **Layout**: dark radial divider with centered title and thin gold accent
- **Style Family**: structural.chapter-divider
- **Typography**: large title, small subtitle
- **Content Elements**: 核心问题 cards and paper figure
"""

    sanitized = svg_executor._sanitize_structural_page_design_block(block, "chapter")

    assert "dark radial divider" in sanitized
    assert "structural.chapter-divider" in sanitized
    assert "large title" in sanitized
    assert "cards and paper figure" not in sanitized
    assert sanitized.count("核心问题") == 1


def test_template_vars_extract_cover_author_and_venue() -> None:
    pages = svg_executor.split_manuscript_pages(
        "<!-- page_type: cover -->\n"
        "# VFR: A Lightweight Visibility-aware Fine-grained Refinement\n\n"
        "### ACM MM '26\n\n"
        "#### Anonymous Author(s)"
    )

    variables = svg_executor._template_vars_by_page(pages)[1]

    assert variables["SOURCE"] == "ACM MM '26"
    assert variables["JOURNAL"] == "ACM MM '26"
    assert variables["VENUE"] == "ACM MM '26"
    assert variables["AUTHOR"] == "Anonymous Author(s)"
    assert variables["AUTHORS"] == "Anonymous Author(s)"
    assert variables["BRAND_LABEL"] == "ACM MM '26"


def test_minimal_cover_content_preserves_author_metadata() -> None:
    content = (
        "<!-- page_type: cover -->\n"
        "# VFR: A Lightweight Visibility-aware Fine-grained Refinement\n\n"
        "### ACM MM '26\n\n"
        "#### Anonymous Author(s)\n\n"
        "2026\n\n"
        "- 面向实时多人姿态估计的轻量化可见性 refinement"
    )

    compact = svg_executor._minimal_structural_page_content(content, "cover")

    assert "ACM MM '26" in compact
    assert "Anonymous Author(s)" in compact
    assert "2026" in compact
    assert "轻量化可见性 refinement" in compact


@pytest.mark.asyncio
async def test_generate_svg_pages_adds_deepseek_executor_guidance(
    workspace_tmp,
) -> None:
    manuscript = "# Page One\n\n- Mechanism\n- Evidence"
    llm = _DeepSeekLLM([_svg('<text x="100" y="100" font-size="24">ok</text>')])

    pages = [
        page_num
        async for page_num, _ in generate_svg_pages(
            "# Design",
            manuscript,
            workspace_tmp,
            llm,
            "deepseek-v4-pro",
            detail_level="very_high",
        )
    ]

    assert pages == [1]
    initial_prompt = llm.message_snapshots[0][1].content
    assert "Detail Level Guidelines" in initial_prompt
    assert "preserve depth without overcrowding" in initial_prompt


@pytest.mark.asyncio
async def test_generate_svg_pages_parallel_preserves_page_numbers_and_structural_policy(
    workspace_tmp,
) -> None:
    manuscript = (
        "<!-- page_type: cover -->\n"
        "# Cover\n\n"
        "[[FIG:fig_001_p1]] - Figure 1 overview\n\n"
        "---\n\n"
        "# Results\n\n"
        "[[FIG:fig_001_p1]] - Figure 1 overview"
    )
    figure_inventory = [
        {
            "path": "../sources/images/fig_001_p1.png",
            "caption": "Figure 1. Overview.",
            "natural_width": 1200,
            "natural_height": 800,
        }
    ]
    llm = _FakeLLM([_svg('<text x="100" y="100" font-size="24">ok</text>')])

    pages = [
        page_num
        async for page_num, _ in svg_executor.generate_svg_pages_parallel(
            "# Design",
            manuscript,
            workspace_tmp,
            llm,
            "fake-model",
            mode="page_parallel",
            parallel_concurrency=2,
            figure_inventory=figure_inventory,
            max_critic_attempts=0,
        )
    ]

    assert sorted(pages) == [1, 2]
    assert (workspace_tmp / "parallel_executor_context.md").exists()
    output_names = {
        path.name for path in (workspace_tmp / "svg_output").glob("*.svg")
    }
    assert any(name.startswith("01_") for name in output_names)
    assert any(name.startswith("02_") for name in output_names)

    prompts = [snapshot[-1].content for snapshot in llm.message_snapshots]
    cover_prompt = next(prompt for prompt in prompts if "Generate page 1/2" in prompt)
    content_prompt = next(prompt for prompt in prompts if "Generate page 2/2" in prompt)
    assert "Structural page paper-figure policy" in cover_prompt
    assert "PAPER FIGURE" not in cover_prompt
    assert "fig_001_p1.png" not in cover_prompt
    assert "PAPER FIGURE" in content_prompt
    assert "fig_001_p1.png" in content_prompt


@pytest.mark.asyncio
async def test_chapter_parallel_passes_previous_layout_brief_within_group(
    workspace_tmp,
) -> None:
    manuscript = (
        "<!-- page_type: chapter -->\n"
        "# 第一章\n\n"
        "---\n\n"
        "<!-- page_type: content -->\n"
        "# 内容页\n\n"
        "- 要点"
    )
    first_svg = _svg(
        '<rect x="80" y="120" width="520" height="180" rx="18" fill="#123456"/>'
        '<text x="96" y="172" font-family="Source Han Sans SC" font-size="28">chapter</text>'
    )
    second_svg = _svg('<text x="100" y="100" font-size="24">content</text>')
    llm = _FakeLLM([first_svg, second_svg])

    pages = [
        page_num
        async for page_num, _ in svg_executor.generate_svg_pages_parallel(
            "# Design",
            manuscript,
            workspace_tmp,
            llm,
            "fake-model",
            mode="chapter_parallel",
            max_critic_attempts=0,
        )
    ]

    assert pages == [1, 2]
    first_prompt = next(prompt[-1].content for prompt in llm.message_snapshots if "Generate page 1/2" in prompt[-1].content)
    second_prompt = next(prompt[-1].content for prompt in llm.message_snapshots if "Generate page 2/2" in prompt[-1].content)
    assert "Previous Page Layout Continuity" not in first_prompt
    assert "Previous Page Layout Continuity" in second_prompt
    assert "Source Han Sans SC" in second_prompt
    assert "rect x=80 y=120 w=520 h=180" in second_prompt
    assert "do not copy its text" in second_prompt


@pytest.mark.asyncio
async def test_generate_svg_pages_retries_same_page_after_svg_extraction_failure(
    workspace_tmp,
) -> None:
    manuscript = "# Page One\n\nBody\n\n---\n\n# Page Two\n\nBody"
    valid_svg = (
        '```svg\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
        '<rect width="1280" height="720" fill="#fff"/>'
        '<text x="100" y="100" font-size="24">ok</text></svg>\n```'
    )
    llm = _FakeLLM(
        [
            "not svg",
            "still not svg",
            valid_svg,
            valid_svg,
        ]
    )

    pages = [
        page_num
        async for page_num, _ in generate_svg_pages(
            "# Design", manuscript, workspace_tmp, llm, "fake-model"
        )
    ]

    assert pages == [1, 2]
    assert llm.calls == 4
    assert (workspace_tmp / "svg_output" / "01_page_one.svg").exists()
    retry_prompt = llm.message_snapshots[1][-1].content
    assert "## Generation Validation Report" in retry_prompt
    assert "No complete `<svg ...>...</svg>` block could be extracted." in retry_prompt
    assert "Regenerate page 1/2 only" in retry_prompt
    assert "# Page One" in retry_prompt


@pytest.mark.asyncio
async def test_generate_svg_pages_fails_after_bounded_same_page_retries(
    workspace_tmp,
) -> None:
    manuscript = "# Page One\n\nBody\n\n---\n\n# Page Two\n\nBody"
    llm = _FakeLLM(["not svg"])

    with pytest.raises(
        RuntimeError,
        match=r"Failed to generate parseable SVG for page 1/2 .* after 3 attempts",
    ):
        [
            page_num
            async for page_num, _ in generate_svg_pages(
                "# Design", manuscript, workspace_tmp, llm, "fake-model"
            )
        ]

    assert llm.calls == 3
    assert not list((workspace_tmp / "svg_output").glob("*.svg"))


@pytest.mark.asyncio
async def test_visual_critic_uses_distinct_usage_stage(
    workspace_tmp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manuscript = "# Page One\n\nBody"
    valid_svg = (
        '```svg\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
        '<rect width="1280" height="720" fill="#fff"/>'
        '<text x="100" y="100" font-size="24">ok</text></svg>\n```'
    )
    llm = _FakeLLM([valid_svg])
    seen_contexts: list[dict] = []

    async def fake_visual_check(*args, **kwargs) -> VisualCheckOutcome:
        seen_contexts.append(current_usage_context())
        return VisualCheckOutcome(rendered=True, report=CriticReport(passed=True))

    monkeypatch.setattr(svg_executor, "visual_check", fake_visual_check)

    pages = [
        page_num
        async for page_num, _ in generate_svg_pages(
            "# Design",
            manuscript,
            workspace_tmp,
            llm,
            "fake-model",
            enable_visual_critic=True,
        )
    ]

    assert pages == [1]
    assert len(seen_contexts) == 1
    assert seen_contexts[0]["stage"] == "visual_qa"
    assert seen_contexts[0]["page"] == 1
    assert seen_contexts[0]["attempt"] == 1


def test_resolve_fig_tokens_rejects_mismatched_figure_number() -> None:
    page = "## Analysis\n\n[[FIG:fig_006_p8]] — 图4 跨数据集对比"
    inventory = [
        {
            "path": "../sources/images/fig_006_p8.png",
            "caption": "Figure 3. Training curves of Mario vs fixed template.",
        }
    ]

    rewritten, used, rejected = svg_executor._resolve_fig_tokens(page, inventory)

    assert used == []
    assert "REJECTED_FIG:fig_006_p8" in rewritten
    assert rejected
    assert "requested figure 4" in rejected[0]


def test_resolve_fig_tokens_accepts_short_caption_alias() -> None:
    page = "## Results\n\n[[FIG:fig6]] — Figure 6 ablation"
    inventory = [
        {
            "path": "../sources/images/fig_007_p9_table.png",
            "caption": "Figure 6. Ablation study across settings.",
        }
    ]

    rewritten, used, rejected = svg_executor._resolve_fig_tokens(page, inventory)

    assert rejected == []
    assert used == inventory
    assert "id=fig_007_p9_table" in rewritten
    assert "fig_007_p9_table.png" in rewritten


def test_figures_from_design_spec_for_page_recovers_image_assignment() -> None:
    design_spec = """
## IX. Content Outline

#### Slide 03 — Evidence

- **Page type**: content
- **Image**: `fig_001_p3` — VFR overview

#### Slide 04 — Other

- **Page type**: content
"""
    inventory = [
        {
            "path": "../sources/images/fig_001_p3.png",
            "caption": "Figure 1. VFR overview.",
            "natural_width": 3499,
            "natural_height": 1655,
        }
    ]

    figures = svg_executor._figures_from_design_spec_for_page(design_spec, 3, inventory)

    assert figures == inventory


def test_figure_guidance_uses_inventory_dimensions_for_relative_paths() -> None:
    guidance = svg_executor._figure_guidance_block(
        [
            {
                "path": "../sources/images/fig_001_p3.png",
                "caption": "Figure 1. VFR overview.",
                "natural_width": 3499,
                "natural_height": 1655,
            }
        ],
        source="design_spec",
    )

    assert "design spec explicitly assigns" in guidance
    assert 'Allowed paper figure href: "../sources/images/fig_001_p3.png"' in guidance
    assert "actual dimensions: 3499x1655 (ratio 2.11)" in guidance


def test_icon_from_design_spec_for_page_recovers_assignment() -> None:
    design_spec = """
## IX. Content Outline

#### Slide 03 — Occlusion

- **Page type**: transition
- **Icon**: `chunk/alert-triangle` — positioned left of title, 40×40px, color `#60A5FA`

#### Slide 04 — Content

- **Icon**: None
"""

    icon = svg_executor._icon_from_design_spec_for_page(design_spec, 3)

    assert icon == {
        "name": "chunk/alert-triangle",
        "size": 40,
        "color": "#60A5FA",
        "note": "— positioned left of title, 40×40px, color `#60A5FA`",
        "role": "",
        "anchor": "",
        "placement": "",
        "reason": "",
    }
    assert svg_executor._icon_from_design_spec_for_page(design_spec, 4) is None


def test_icon_from_design_spec_for_page_recovers_layout_metadata() -> None:
    design_spec = """
## IX. Content Outline

#### Slide 02 — Result

- **Icon**: `chunk/target` — role: KPI/result anchor; anchor: result callout title; placement: left of result label before metric text; size: 32x32px; reason: direct objective metaphor
"""

    icon = svg_executor._icon_from_design_spec_for_page(design_spec, 2)

    assert icon is not None
    assert icon["name"] == "chunk/target"
    assert icon["size"] == 32
    assert icon["role"] == "KPI/result anchor"
    assert icon["anchor"] == "result callout title"
    assert icon["placement"] == "left of result label before metric text"
    assert icon["reason"] == "direct objective metaphor"


def test_icon_guidance_requires_data_icon_placeholder() -> None:
    guidance = svg_executor._icon_guidance_block(
        {"name": "chunk/lightbulb", "size": 36, "color": "#F59E0B"}
    )

    assert 'data-icon="chunk/lightbulb"' in guidance
    assert "Do not redraw it with inline `<path>`" in guidance
    assert "double quotes" in guidance
    assert "Reserve the icon slot before placing body text" in guidance
    assert "Do not float it in an unrelated corner" in guidance


def test_icon_guidance_without_assignment_bans_fake_badges() -> None:
    guidance = svg_executor._icon_guidance_block(None)

    assert "no explicit design-spec icon assignment" in guidance
    assert "standalone letter/symbol badges" in guidance
    assert "distribution bins" in guidance


def test_validate_icon_refs_rejects_fake_card_badge_when_icon_none() -> None:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
        '<rect x="94" y="416" width="46" height="46" rx="12"/>'
        '<text x="117" y="447" font-size="22" text-anchor="middle">P</text>'
        '<text x="156" y="435" font-size="19">分布式精修</text>'
        "</svg>"
    )

    report = svg_executor._validate_icon_refs(svg, required_icon=None)

    assert not report.passed
    assert any(v.rule == "pseudo_icon_badge_not_allowed" for v in report.violations)


def test_classify_page_type_reads_manuscript_metadata() -> None:
    assert svg_executor._classify_page_type("<!-- page_type: cover -->\n## Title") == "cover"
    assert svg_executor._classify_page_type("<!-- page_type: transition -->\n## Method") == "chapter"
    assert svg_executor._classify_page_type("## Slide 3: Ordinary content\n\n- point") == "content"


def test_template_vars_use_second_heading_as_chapter_description() -> None:
    pages = [
        "<!-- page_type: chapter -->\n# 1. 背景与问题\n\n## 从效率到可见性缺口\n\n- extra body"
    ]

    vars_by_page = svg_executor._template_vars_by_page(pages)

    assert vars_by_page[1]["CHAPTER_TITLE"] == "1. 背景与问题"
    assert vars_by_page[1]["CHAPTER_DESC"] == "从效率到可见性缺口"


@pytest.mark.asyncio
async def test_template_skeleton_uses_page_type_metadata_not_design_outline(
    workspace_tmp,
) -> None:
    manuscript = "## Slide 1: Ordinary content\n\n- point"
    design_spec = "## IX. Content Outline\n- Slide 1 - Ordinary content\n- Slide 2 - Method"
    llm = _FakeLLM([_svg('<text x="100" y="100" font-size="24">ok</text>')])

    pages = [
        page_num
        async for page_num, _ in generate_svg_pages(
            design_spec,
            manuscript,
            workspace_tmp,
            llm,
            "fake-model",
            template_skeletons={
                "chapter": '<svg viewBox="0 0 1280 720"><text>{{SECTION_NUM}}</text></svg>',
                "content": '<svg viewBox="0 0 1280 720"><text>{{PAGE_TITLE}}</text></svg>',
            },
        )
    ]

    assert pages == [1]
    prompt = llm.message_snapshots[0][-1].content
    assert "Template Skeleton (content page)" in prompt
    assert "Template Skeleton (chapter page)" not in prompt


@pytest.mark.asyncio
async def test_generate_svg_pages_includes_structured_deck_plan(
    workspace_tmp,
) -> None:
    manuscript = (
        "<!-- page_type: chapter -->\n"
        "# 背景与动机\n\n"
        "## 为什么需要新模型"
    )
    llm = _FakeLLM([_svg('<text x="100" y="100" font-size="24">ok</text>')])

    pages = [
        page_num
        async for page_num, _ in generate_svg_pages(
            "#### Slide 01\n- **Page Type**: chapter\n- **Layout**: dark divider",
            manuscript,
            workspace_tmp,
            llm,
            "fake-model",
            template_skeletons={
                "chapter": '<svg viewBox="0 0 1280 720"><text>01</text></svg>',
            },
        )
    ]

    assert pages == [1]
    prompt = llm.message_snapshots[0][-1].content
    assert "Current Structured Slide Plan" in prompt
    assert "Current chapter index: 01" in prompt
    assert "Do NOT recompute chapter/section numbers from the page number" in prompt


@pytest.mark.asyncio
async def test_generate_svg_pages_minimizes_structural_page_prompt(
    workspace_tmp,
) -> None:
    manuscript = (
        "<!-- page_type: chapter -->\n"
        "# 1. 问题与目标\n\n"
        "**核心问题**\n\n"
        "- vanilla attention 的二次复杂度限制超长上下文\n\n"
        "**本章看点**\n\n"
        "- 能否高效稳定部署 1M tokens"
    )
    llm = _FakeLLM([_svg('<text x="100" y="100" font-size="24">ok</text>')])

    pages = [
        page_num
        async for page_num, _ in generate_svg_pages(
            "# Design",
            manuscript,
            workspace_tmp,
            llm,
            "fake-model",
        )
    ]

    assert pages == [1]
    prompt = llm.message_snapshots[0][-1].content
    assert "Structural Page Override" in prompt
    assert "vanilla attention" not in prompt
    assert "1M tokens" not in prompt


@pytest.mark.asyncio
async def test_generate_svg_pages_repairs_unallowed_paper_figure_href(
    workspace_tmp,
) -> None:
    manuscript = "# Page One\n\nNo real figure token on this page."
    wrong = _svg(
        '<image href="../sources/images/fig_999.png" x="100" y="100" width="300" height="200"/>'
        '<text x="100" y="360" font-size="24">wrong</text>'
    )
    fixed = _svg(
        '<rect x="100" y="100" width="300" height="200" fill="#e5eef8"/>'
        '<text x="100" y="360" font-size="24">native summary</text>'
    )
    llm = _FakeLLM([wrong, fixed])

    pages = [
        page_num
        async for page_num, svg in generate_svg_pages(
            "# Design",
            manuscript,
            workspace_tmp,
            llm,
            "fake-model",
            max_critic_attempts=2,
        )
        if "native summary" in svg
    ]

    assert pages == [1]
    assert llm.calls == 2
    repair_prompt = llm.message_snapshots[1][-1].content
    assert "paper_figure_not_allowed" in repair_prompt


@pytest.mark.asyncio
async def test_generate_svg_pages_repairs_missing_required_icon_placeholder(
    workspace_tmp,
) -> None:
    manuscript = "# Page One\n\nChapter divider"
    design_spec = """
## IX. Content Outline

#### Slide 01 — Occlusion

- **Page type**: transition
- **Icon**: `chunk/alert-triangle` — positioned left of title, 40×40px, color `#60A5FA`
"""
    wrong = _svg(
        '<!-- Icon: alert-triangle -->'
        '<path d="M120,80 L160,150 L80,150 Z" fill="#60A5FA"/>'
        '<text x="100" y="220" font-size="24">chapter</text>'
    )
    fixed = _svg(
        '<use data-icon="chunk/alert-triangle" x="80" y="80" width="40" height="40" fill="#60A5FA"/>'
        '<text x="100" y="220" font-size="24">chapter</text>'
    )
    llm = _FakeLLM([wrong, fixed])

    slides = [
        svg
        async for _, svg in generate_svg_pages(
            design_spec,
            manuscript,
            workspace_tmp,
            llm,
            "fake-model",
            max_critic_attempts=2,
        )
    ]

    assert len(slides) == 1
    assert 'data-icon="chunk/alert-triangle"' in slides[0]
    assert llm.calls == 2
    initial_prompt = llm.message_snapshots[0][-1].content
    assert 'data-icon="chunk/alert-triangle"' in initial_prompt
    repair_prompt = llm.message_snapshots[1][-1].content
    assert "required_icon_missing" in repair_prompt


@pytest.mark.asyncio
async def test_generate_svg_pages_repairs_cross_slide_reused_paper_figure(
    workspace_tmp,
) -> None:
    manuscript = (
        "# Page One\n\n[[FIG:fig_001_p1]] — Figure 1 overview\n\n---\n\n"
        "# Page Two\n\n[[FIG:fig_001_p1]] — Figure 1 detail"
    )
    figure_inventory = [
        {
            "path": "../sources/images/fig_001_p1.png",
            "caption": "Figure 1. Overview and detail panels.",
        }
    ]
    repeated = _svg(
        '<image href="../sources/images/fig_001_p1.png" x="100" y="100" width="300" height="200"/>'
        '<text x="100" y="360" font-size="24">paper figure</text>'
    )
    fixed_second = _svg(
        '<rect x="100" y="100" width="300" height="200" fill="#e5eef8"/>'
        '<text x="100" y="360" font-size="24">redrawn detail</text>'
    )
    llm = _FakeLLM([repeated, repeated, fixed_second])

    slides = [
        svg
        async for _, svg in generate_svg_pages(
            "# Design",
            manuscript,
            workspace_tmp,
            llm,
            "fake-model",
            figure_inventory=figure_inventory,
            max_critic_attempts=2,
        )
    ]

    assert len(slides) == 2
    assert "fig_001_p1.png" in slides[0]
    assert "redrawn detail" in slides[1]
    assert llm.calls == 3
    repair_prompt = llm.message_snapshots[2][-1].content
    assert "paper_figure_reused_from_previous_slide" in repair_prompt
