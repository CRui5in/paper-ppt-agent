from __future__ import annotations

import pytest

from backend.llm import LLMResponse
from backend.llm.types import ProviderInfo
from backend.orchestrator import strategist_agent


class _FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    async def chat(self, messages, model, **kwargs) -> LLMResponse:
        self.calls.append({"messages": messages, "model": model, **kwargs})
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return LLMResponse(content=self.responses[index])


class _DeepSeekLLM(_FakeLLM):
    def get_provider_info(self) -> ProviderInfo:
        return ProviderInfo(name="deepseek", display_name="DeepSeek")


def _valid_design_spec() -> str:
    filler = "\n".join(f"- Layout rule {i}: keep the academic system consistent." for i in range(80))
    return f"""# Test Design Spec

## I. Project Information
- Project name: Test
- Page Count: 1

## II. Canvas Specification
- Canvas: 1280x720

## III. Visual Theme
- Theme: academic light

## IV. Typography System
- Heading/body scale

## V. Layout Principles
{filler}

## IX. Content Outline
- Page 1: content — title cover

## XI. Technical Constraints Reminder
- Return valid SVG only
"""


@pytest.mark.asyncio
async def test_create_design_spec_retries_empty_response() -> None:
    llm = _FakeLLM(["", _valid_design_spec()])

    spec = await strategist_agent.create_design_spec(
        "# Title\n\nBody",
        llm,
        "fake-model",
        canvas_format="ppt169",
        style="academic",
        language="zh",
        detail_level="very_high",
    )

    assert "## I. Project Information" in spec
    assert len(llm.calls) == 2
    assert llm.calls[0]["max_tokens"] == strategist_agent.DESIGN_SPEC_MAX_TOKENS
    retry_prompt = llm.calls[1]["messages"][-1].content
    assert "previous design_spec.md response was invalid" in retry_prompt


@pytest.mark.asyncio
async def test_create_design_spec_fails_after_invalid_retries() -> None:
    llm = _FakeLLM(["", "too short"])

    with pytest.raises(RuntimeError, match="Invalid design specification"):
        await strategist_agent.create_design_spec(
            "# Title\n\nBody",
            llm,
            "fake-model",
        )

    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_create_design_spec_adds_deepseek_strategy_guidance() -> None:
    llm = _DeepSeekLLM([_valid_design_spec()])

    await strategist_agent.create_design_spec(
        "# Title\n\n- Mechanism\n- Evidence",
        llm,
        "deepseek-v4-pro",
        detail_level="very_high",
    )

    user_prompt = llm.calls[0]["messages"][-1].content
    assert "Detail Level Guidelines" in user_prompt
    assert "preserve the manuscript's analytical depth" in user_prompt


@pytest.mark.asyncio
async def test_create_design_spec_adds_restrained_icon_policy_when_enabled() -> None:
    llm = _FakeLLM([_valid_design_spec()])

    await strategist_agent.create_design_spec(
        "# Title\n\n- Step one\n- Key result",
        llm,
        "fake-model",
        enable_icon=True,
        enable_icon_rag=False,
        icon_library="tabler-outline",
    )

    user_prompt = llm.calls[0]["messages"][-1].content
    assert "Icon Usage: ENABLED — restrained semantic mode" in user_prompt
    assert "Use icons only when they clarify" in user_prompt
    assert "Never use icons as ordinary bullet prefixes" in user_prompt
    assert "Selected icon library: `tabler-outline`" in user_prompt
    assert "offline fallback" in user_prompt
    assert "`tabler-outline/alert-triangle`" in user_prompt
    assert "Visual role separation" in user_prompt
    assert "Card Marker" in user_prompt
    assert "Micro Visual" in user_prompt


@pytest.mark.asyncio
async def test_create_design_spec_disables_icons_when_switch_off() -> None:
    llm = _FakeLLM([_valid_design_spec()])

    await strategist_agent.create_design_spec(
        "# Title\n\nBody",
        llm,
        "fake-model",
        enable_icon=False,
    )

    user_prompt = llm.calls[0]["messages"][-1].content
    assert "Icon Usage: DISABLED" in user_prompt
    assert "Do NOT use any `<use data-icon" in user_prompt


def test_design_spec_validation_rejects_outline_page_drift() -> None:
    bad = _valid_design_spec().replace(
        "- Page 1: content — title cover",
        "- Page 1: cover — title\n- Page 2: content — extra",
    )

    error = strategist_agent._design_spec_validation_error(
        bad,
        expected_page_count=1,
    )

    assert error is not None
    assert "references page 2" in error


def test_design_spec_validation_rejects_page_type_drift() -> None:
    bad = _valid_design_spec().replace(
        "- Page 1: content — title cover",
        "- Page 1: cover — invented cover",
    )

    error = strategist_agent._design_spec_validation_error(
        bad,
        expected_page_count=1,
        expected_inventory=[{"page": 1, "type": "content", "title": "Real content"}],
    )

    assert error is not None
    assert "page types do not match" in error


def test_design_spec_validation_rejects_missing_icon_asset() -> None:
    bad = _valid_design_spec().replace(
        "## IX. Content Outline\n- Page 1: content — title cover",
        (
            "## IX. Content Outline\n"
            "- Page 1: content — title cover\n"
            "- **Icon**: `chunk/alert-triangle`"
        ),
    )

    error = strategist_agent._design_spec_validation_error(bad)

    assert error is not None
    assert "missing local icon assets" in error
    assert "chunk/alert-triangle" in error


@pytest.mark.asyncio
async def test_icon_rag_candidates_skip_missing_local_assets(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeIndex:
        is_available = True

        def search(self, query: str, *, lib: str, k: int) -> list[dict]:
            return [
                {"path": "chunk/scale", "score": 0.99, "category": "metric", "tags": ["scale"]},
                {"path": "chunk/ruler", "score": 0.88, "category": "metric", "tags": ["measure"]},
            ]

    monkeypatch.setattr(strategist_agent, "get_icon_index", lambda: _FakeIndex())
    monkeypatch.setattr(strategist_agent, "_icon_asset_exists", lambda path: path == "chunk/ruler")

    block = await strategist_agent._retrieve_icon_candidates("## Measurement\n\n**scale**", "chunk")

    assert "`chunk/ruler`" in block
    assert "chunk/scale" not in block


def test_offline_icon_candidates_are_verified_and_semantic() -> None:
    block = strategist_agent._offline_icon_candidates_block("chunk")

    assert "offline fallback, 28 verified icons" in block
    assert "Use when" in block
    assert "`chunk/circle-exclamation`" in block
    assert "`chunk/sliders`" in block
    assert "gate" in block
