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
