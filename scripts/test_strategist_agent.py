"""Regression tests: python -m unittest discover -s scripts -p test_strategist_agent.py -v"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from backend.llm import LLMResponse, TokenUsage
from backend.orchestrator import strategist_agent as strategist


MANUSCRIPT = "\n\n---\n\n".join(
    f"<!-- page_type: {kind} -->\n# Slide {page}\nSource evidence for slide {page}."
    for page, kind in enumerate(("cover", "toc", "ending"), start=1)
)
SECTIONS = (
    ("I", "Project Information"),
    ("II", "Canvas Specification"),
    ("III", "Visual Theme"),
    ("IV", "Typography System"),
    ("V", "Layout Principles"),
    ("VI", "Icon Usage"),
    ("VII", "Visualization Reference List"),
    ("VIII", "Image Resource List"),
)
TITLES = ("Cover", "Agenda", "Thanks")
INTRO = "\n\n".join(
    f"## {number}. {title}\n"
    "Use a 1280x720 canvas, a white background, navy headings, and readable body text. "
    "Keep shared margins, spacing, and typography consistent across every page. "
    "Use the manuscript as the source for titles, evidence, and page order."
    for number, title in SECTIONS
) + "\n\n## IX. Content Outline"
PAGES = [
    f"#### Slide {page:02d} - {TITLES[page - 1]}\n"
    f"- **Type**: {kind}\n"
    "- **Style Family**: structural.standard\n"
    "- **Layout Family**: centered-title\n"
    "- **Density**: low\n"
    "- **Content**: Complete page plan."
    for page, kind in enumerate(("cover", "toc", "ending"), start=1)
]
NOTES = "## X. Speaker Notes Requirements\nUse short explanations supported by the manuscript."
CONSTRAINTS = (
    "## XI. Technical Constraints Reminder\n"
    "Use inline SVG attributes and tspan for text wrapping. Do not use foreignObject."
)
COMPLETE = "\n\n".join((INTRO, *PAGES, NOTES, CONSTRAINTS))


def response(text: str, reason: str = "stop") -> LLMResponse:
    return LLMResponse(
        content=text,
        usage=TokenUsage(prompt_tokens=100, completion_tokens=200),
        raw={"choices": [{"finish_reason": reason}]},
    )


class StrategistGenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.budgets = patch.multiple(
            strategist.settings,
            design_spec_max_tokens=32768,
            deepseek_design_spec_max_tokens=131072,
        )
        self.budgets.start()
        self.addCleanup(self.budgets.stop)

    async def generate(self, responses, **kwargs):
        provider = SimpleNamespace(chat=AsyncMock(side_effect=responses))
        result = await strategist.create_design_spec(
            MANUSCRIPT, provider, kwargs.pop("model", "deepseek-flash"), **kwargs,
        )
        return result, provider.chat

    async def test_complete_response_uses_deepseek_budget_in_one_call(self):
        result, chat = await self.generate([response(COMPLETE)])
        self.assertEqual(result, COMPLETE)
        self.assertEqual(chat.await_count, 1)
        self.assertEqual(chat.call_args.kwargs["max_tokens"], 131072)

    async def test_other_models_keep_existing_budget(self):
        result, chat = await self.generate([response(COMPLETE)], model="gpt-5.4")
        self.assertEqual(result, COMPLETE)
        self.assertEqual(chat.call_args.kwargs["max_tokens"], 32768)

    async def test_truncated_page_is_replaced_and_remaining_pages_are_completed(self):
        partial = "\n\n".join((INTRO, PAGES[0], "#### Slide 02 - Slide 2\n- cut off mid-word"))
        tail = "\n\n".join((*PAGES[1:], NOTES, CONSTRAINTS))
        with TemporaryDirectory() as directory:
            result, chat = await self.generate(
                [response(partial, "length"), response(tail)], debug_dir=Path(directory),
            )
            debug = Path(directory) / "strategist_response_attempt_1_continuation_1.md"
            self.assertTrue(debug.exists())
            self.assertIn("max_tokens=131072", debug.read_text(encoding="utf-8"))
        self.assertEqual(result, COMPLETE)
        self.assertEqual(chat.await_count, 2)
        messages = chat.call_args.args[0]
        self.assertEqual(messages[-2].role, "assistant")
        self.assertEqual(messages[-2].content, partial)
        self.assertIn("Start with this heading: #### Slide 02", messages[-1].content)

    async def test_multiple_continuations_preserve_completed_pages(self):
        partial1 = "\n\n".join((INTRO, PAGES[0], "#### Slide 02 - Slide 2\nIncomplete"))
        partial2 = "\n\n".join((PAGES[1], "#### Slide 03 - Slide 3\nStill incomplete"))
        tail = "\n\n".join((PAGES[2], NOTES, CONSTRAINTS))
        result, chat = await self.generate([
            response(partial1, "length"), response(partial2, "length"), response(tail),
        ])
        self.assertEqual(result, COMPLETE)
        self.assertEqual(chat.await_count, 3)

    async def test_length_response_with_all_headings_is_still_completed(self):
        partial = COMPLETE.replace("Do not use foreignObject.", "Do not use foreign")
        result, chat = await self.generate([response(partial, "length"), response(CONSTRAINTS)])
        self.assertEqual(result, COMPLETE)
        self.assertEqual(chat.await_count, 2)

    async def test_truncated_subsection_is_rewritten_without_losing_section_heading(self):
        subsection = "### SVG rules\nUse tspan for line breaks."
        partial = COMPLETE + "\n\n### SVG rules\nUse ts"
        result, _ = await self.generate([response(partial, "length"), response(subsection)])
        self.assertEqual(result, COMPLETE + "\n\n" + subsection)

    async def test_empty_continuation_does_not_accept_truncated_content(self):
        provider = SimpleNamespace(chat=AsyncMock(side_effect=[
            response(COMPLETE, "length"), response(""), response(COMPLETE),
        ]))
        result = await strategist.create_design_spec(MANUSCRIPT, provider, "deepseek-flash")
        self.assertEqual(result, COMPLETE)
        self.assertEqual(provider.chat.await_count, 3)
        self.assertIn("empty response", provider.chat.call_args.args[0][-1].content)

    async def test_empty_thinking_only_response_retries_generation(self):
        result, chat = await self.generate([response("", "length"), response(COMPLETE)])
        self.assertEqual(result, COMPLETE)
        self.assertEqual(chat.await_count, 2)
        self.assertFalse(any(message.role == "assistant" for message in chat.call_args.args[0]))

    async def test_missing_page_is_not_hidden_by_complete_final_sections(self):
        incomplete = "\n\n".join((INTRO, PAGES[0], PAGES[2], NOTES, CONSTRAINTS))
        result, chat = await self.generate([response(incomplete), response(COMPLETE)])
        self.assertEqual(result, COMPLETE)
        self.assertIn("missing slide headings for pages [2]", chat.call_args.args[0][-1].content)

    async def test_nontruncation_validation_errors_keep_existing_retry(self):
        incomplete = COMPLETE[:COMPLETE.index("## XI.")]
        result, chat = await self.generate([response(incomplete), response(COMPLETE)])
        self.assertEqual(result, COMPLETE)
        self.assertIn("missing section XI", chat.call_args.args[0][-1].content)

    async def test_repeated_full_document_on_continuation_is_rejected(self):
        partial = "\n\n".join((INTRO, PAGES[0], "#### Slide 02 - Slide 2\nIncomplete"))
        result, chat = await self.generate([
            response(partial, "length"), response(COMPLETE), response(COMPLETE),
        ])
        self.assertEqual(result, COMPLETE)
        self.assertEqual(chat.await_count, 3)
        self.assertIn("duplicate numbered sections", chat.call_args.args[0][-1].content)

    async def test_repeated_truncation_stops_with_actionable_error(self):
        provider = SimpleNamespace(chat=AsyncMock(return_value=response(COMPLETE, "length")))
        with self.assertRaisesRegex(RuntimeError, "truncated at the output token limit"):
            await strategist.create_design_spec(MANUSCRIPT, provider, "deepseek-flash")
        self.assertEqual(provider.chat.await_count, 6)

    async def test_page_types_are_aligned_after_continuation(self):
        wrong_cover = PAGES[0].replace("Cover", "Opening").replace("**Type**: cover", "**Type**: content")
        partial = "\n\n".join((INTRO, wrong_cover, "#### Slide 02 - Slide 2\nIncomplete"))
        tail = "\n\n".join((*PAGES[1:], NOTES, CONSTRAINTS))
        result, _ = await self.generate([response(partial, "length"), response(tail)])
        self.assertEqual(result, COMPLETE.replace("Cover", "Opening"))


class StrategistValidationTests(unittest.TestCase):
    def test_deepseek_current_legacy_and_namespaced_models(self):
        with patch.object(strategist.settings, "deepseek_design_spec_max_tokens", 65536):
            for model in ("deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro", "deepseek-ai/deepseek-v4-flash"):
                with self.subTest(model=model):
                    self.assertEqual(strategist._design_spec_max_tokens(model), 65536)

    def test_outline_does_not_count_cross_references_as_pages(self):
        text = "\n\n".join((INTRO, PAGES[0] + "\nSee Slide 02 and Slide 03.", NOTES, CONSTRAINTS))
        error = strategist._design_spec_validation_error(text, expected_page_count=3)
        self.assertIn("missing slide headings for pages [2, 3]", error)

    def test_duplicate_and_unexpected_pages_are_rejected(self):
        for pages, expected in (
            ([*PAGES, PAGES[1]], "duplicate slide headings"),
            ([*PAGES, "#### Slide 04 - Extra\nUnplanned slide."], "unexpected pages [4]"),
        ):
            with self.subTest(expected=expected):
                text = "\n\n".join((INTRO, *pages, NOTES, CONSTRAINTS))
                error = strategist._design_spec_validation_error(text, expected_page_count=3)
                self.assertIn(expected, error)

    def test_continuation_handles_content_without_headings(self):
        prefix, instruction = strategist._design_spec_continuation("An incomplete opening")
        self.assertEqual(prefix, "")
        self.assertIn("Section I", instruction)

    def test_finish_reason_supports_sdk_response_objects(self):
        result = LLMResponse("", raw=SimpleNamespace(choices=[SimpleNamespace(finish_reason="length")]))
        self.assertEqual(strategist._response_finish_reason(result), "length")


if __name__ == "__main__":
    unittest.main()
