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
    }
    assert svg_executor._icon_from_design_spec_for_page(design_spec, 4) is None


def test_icon_guidance_requires_data_icon_placeholder() -> None:
    guidance = svg_executor._icon_guidance_block(
        {"name": "chunk/lightbulb", "size": 36, "color": "#F59E0B"}
    )

    assert 'data-icon="chunk/lightbulb"' in guidance
    assert "Do not redraw it with inline `<path>`" in guidance
    assert "double quotes" in guidance


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
            "# Design", manuscript, workspace_tmp, llm, "fake-model"
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
            design_spec, manuscript, workspace_tmp, llm, "fake-model"
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
        )
    ]

    assert len(slides) == 2
    assert "fig_001_p1.png" in slides[0]
    assert "redrawn detail" in slides[1]
    assert llm.calls == 3
    repair_prompt = llm.message_snapshots[2][-1].content
    assert "paper_figure_reused_from_previous_slide" in repair_prompt
