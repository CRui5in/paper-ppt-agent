"""Exercise generation entry points with model responses and inspect saved artifacts."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import xml.etree.ElementTree as ET

from backend.llm import LLMResponse
from backend.orchestrator import research_agent, svg_executor
from backend.generator.svg_finalize.finalize import finalize_project
from backend.generator.svg_finalize.render_ready import prepare_svg_file_content_for_render
from backend.generator.svg_to_pptx.converter import convert_svg_to_slide_shapes
from backend.parser.paper_model import ParsedPaper

MANUSCRIPT = '\n\n---\n\n'.join((
    '<!-- page_type: cover -->\n# A study',
    '<!-- page_type: toc -->\n# Contents\n- Method',
    '<!-- page_type: content -->\n# Method\nThe method normalizes its inputs.',
    '<!-- page_type: ending -->\n# Thank you',
))
MODEL_SVG = r'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
<rect x="0" y="0" width="1280" height="720" fill="#fff"/>
<g data-textbox="84 180 500 100">
<text font-size="18"><tspan x="84" y="200">The input is normalized.</tspan></text>
<text font-size="18"><tspan x="84" y="240">The result is accumulated.</tspan></text>
</g>
<g data-latex="\sum_{i=1}^{n} x_i^2" data-math-box="84 320 500 100" font-size="30"/>
</svg>'''
NS = '{http://www.w3.org/2000/svg}'


class ManuscriptEntryPointTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_generation_sends_content_driven_writing_rules(self):
        provider = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse(content=MANUSCRIPT)))
        result = await research_agent.analyze_paper(ParsedPaper(title='A study'), provider, 'test-model', num_pages=4)
        self.assertEqual(result, MANUSCRIPT)
        messages = provider.chat.call_args.args[0]
        self.assertIn(research_agent.WRITING_PROMPT.read_text(encoding='utf-8'), messages[0].content)
        self.assertNotIn('Every `content` slide must include', messages[1].content)
        self.assertIn('Choose its opening, structure', messages[1].content)

    async def test_deep_generation_passes_use_the_same_writing_rules(self):
        provider = SimpleNamespace(chat=AsyncMock(side_effect=[
            LLMResponse(content='SOURCE: the method normalizes its inputs.'),
            LLMResponse(content='Cover, contents, method, closing.'),
            LLMResponse(content=MANUSCRIPT),
            LLMResponse(content='QUALITY_CHECK_PASSED\n' + MANUSCRIPT),
        ]))
        result = await research_agent.analyze_paper(ParsedPaper(title='A study'), provider, 'test-model', num_pages=4, enable_deep_research=True)
        self.assertEqual(result, MANUSCRIPT)
        self.assertEqual(provider.chat.await_count, 4)
        for call in provider.chat.call_args_list:
            self.assertIn(research_agent.WRITING_PROMPT.read_text(encoding='utf-8'), call.args[0][0].content)


class SvgGenerationEntryPointTests(unittest.IsolatedAsyncioTestCase):
    async def exercise_mode(self, mode):
        with TemporaryDirectory() as directory:
            project = Path(directory)
            provider = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse(content=MODEL_SVG)))
            kwargs = {'max_critic_attempts': 0, 'enable_visual_critic': False}
            generator = svg_executor.generate_svg_pages
            if mode != 'sequential':
                generator = svg_executor.generate_svg_pages_parallel
                kwargs['mode'] = mode
            results = [result async for result in generator(
                '', '<!-- page_type: content -->\n# Method\nInputs and normalization.',
                project, provider, 'test-model', **kwargs,
            )]
            self.assertEqual(len(results), 1)
            self.assertEqual(provider.chat.await_count, 1)
            saved = project / 'svg_output/slide_001.svg'
            self.assertTrue(saved.is_file())
            root = ET.parse(saved).getroot()
            self.assertTrue(list(root.iter(NS + 'path')))
            preview = ET.fromstring(prepare_svg_file_content_for_render(saved))
            self.assertEqual([(e.get('x'), e.get('y')) for e in preview.iter(NS + 'text')], [('84', '200'), ('84', '240')])
            finalize_project(project)
            final = project / 'svg_final/slide_001.svg'
            self.assertTrue(final.is_file())
            xml, _, _ = convert_svg_to_slide_shapes(final)
            self.assertIn('The input is normalized.', xml)
            self.assertIn('<a:custGeom>', xml)

    async def test_sequential_generation_preview_finalize_and_native_export(self):
        await self.exercise_mode('sequential')

    async def test_page_parallel_generation_preview_finalize_and_native_export(self):
        await self.exercise_mode('page_parallel')


if __name__ == '__main__':
    unittest.main()
