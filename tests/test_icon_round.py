from __future__ import annotations

import pytest

from backend.llm import LLMResponse
from backend.orchestrator import icon_round


class _FakeLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def chat(self, messages, model, **kwargs) -> LLMResponse:
        self.calls.append({"messages": messages, "model": model, **kwargs})
        return LLMResponse(content=self.response)


def _design_spec() -> str:
    return """# Test Design Spec

### I. Project Information

### VI. Icon Usage Specification

Icon library: chunk -- inventory TBD.

### VII. Image Resource List

None.

### IX. Content Outline

#### Slide 01 - Opening
- **Type**: cover
- **Layout**: title only

#### Slide 02 - Result Delta
- **Type**: content
- **Layout**: KPI callout with supporting bullets
- **Purpose**: highlight the target objective and metric change

#### Slide 03 - Method
- **Type**: chapter
- **Layout**: section divider

#### Slide 04 - Thanks
- **Type**: ending
- **Layout**: closing

### XI. Technical Constraints Reminder
"""


def _manuscript() -> str:
    return """<!-- page_type: cover -->
# Opening

---

<!-- page_type: content -->
# Result Delta

- Target objective improves the measured score.
- Key result needs one clear callout.

---

<!-- page_type: transition -->
# Method

---

<!-- page_type: ending -->
# Thanks
"""


def test_parse_icon_assignments_allows_page_word_in_reason() -> None:
    parsed = icon_round._parse_icon_assignments(
        """| Page | Icon | Role | Anchor | Placement | Size | Reason |
|------|------|------|--------|-----------|------|--------|
| 02 | `chunk/target` | KPI/result anchor | metric label | left of result label | 32x32px | this content page has a direct target metaphor |"""
    )

    assert parsed[2]["icon"] == "chunk/target"
    assert parsed[2]["role"] == "KPI/result anchor"
    assert "content page" in parsed[2]["reason"]


@pytest.mark.asyncio
async def test_icon_round_forces_structural_pages_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _FakeLLM(
        """| Page | Icon | Role | Anchor | Placement | Size | Reason |
|------|------|------|--------|-----------|------|--------|
| 01 | chunk/alert-triangle | forbidden | cover title | corner | 40x40px | model tried to decorate the cover |
| 02 | chunk/target | KPI/result anchor | result callout title | left of result label before metric text | 32x32px | target icon directly labels the objective |
| 03 | chunk/lightbulb | forbidden | chapter title | beside title | 40x40px | model tried to decorate chapter |
| 04 | chunk/rocket | forbidden | closing title | corner | 40x40px | model tried to decorate ending |"""
    )
    monkeypatch.setattr(icon_round, "_icon_asset_exists", lambda _path: True)
    monkeypatch.setattr(
        icon_round,
        "_format_full_icon_catalog",
        lambda lib: f"## Full Selectable Icon Catalog\n\n`{lib}/target`",
    )

    updated = await icon_round.run_icon_round(
        _design_spec(),
        _manuscript(),
        "chunk",
        llm,
        "fake-model",
        enable_icon_rag=False,
    )

    assert "| 02 | `chunk/target` | KPI/result anchor | result callout title | left of result label before metric text | 32x32px |" in updated
    assert "role: KPI/result anchor; anchor: result callout title; placement: left of result label before metric text; size: 32x32px" in updated
    assert "Decorative accent" not in updated
    assert "chunk/alert-triangle" not in updated
    assert "chunk/lightbulb" not in updated
    assert "chunk/rocket" not in updated

    prompt = llm.calls[0]["messages"][0].content
    assert "Forbidden page types" in prompt
    assert "Full Selectable Icon Catalog" in prompt
    assert "Reserve the icon's visual slot before text" in prompt
