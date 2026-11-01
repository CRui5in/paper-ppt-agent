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
    def test_repair_cannot_trade_capacity_for_a_new_overlap(self):
        from backend.generator.svg_critic import CriticReport, Violation
        before = CriticReport(False, [Violation('paragraph_capacity', 'error', 'Measured space required: 200px wide and 100px high', bbox=(100,180,200,50))])
        after = CriticReport(False, [Violation('text_overlap', 'error', 'Two labels intersect')])
        self.assertTrue(svg_executor._repair_regressed(before, after))
        clipped = CriticReport(False, [Violation('out_of_bounds', 'error', 'Clipped paragraph') for _ in range(8)])
        improved = CriticReport(False, [Violation('out_of_bounds', 'error', 'One remaining clipped line'), Violation('text_overlap', 'error', 'One overlap')])
        self.assertFalse(svg_executor._repair_regressed(clipped, improved))

    async def test_partial_progress_gets_a_followup_and_regressions_keep_the_previous_page(self):
        source = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720"><g data-textbox="100 180 200 10"><text x="100" y="200" font-size="18">This complete paragraph needs enough vertical space to wrap across several lines.</text></g></svg>'
        partial = source.replace('200 10', '200 40')
        worse = source.replace('200 10', '200 1')
        for mode in ('sequential', 'page_parallel'):
            for candidate in (partial, worse, '{"edits":[{"target":"unknown","remove":true}]}'):
                with self.subTest(mode=mode, candidate=candidate), TemporaryDirectory() as directory:
                    provider = SimpleNamespace(chat=AsyncMock(side_effect=[LLMResponse(content=source), LLMResponse(content=candidate), LLMResponse(content=MODEL_SVG)]))
                    generator = svg_executor.generate_svg_pages if mode == 'sequential' else svg_executor.generate_svg_pages_parallel
                    kwargs = {} if mode == 'sequential' else {'mode': mode}
                    results = [result async for result in generator('', '<!-- page_type: content -->\n# Method', Path(directory), provider, 'test-model', max_critic_attempts=0, enable_visual_critic=False, **kwargs)]
                    self.assertEqual(provider.chat.await_count, 3)
                    self.assertIn('The input is normalized.', results[0][1])
                    if candidate != partial:
                        last_prompt = provider.chat.call_args.args[0][1].content
                        self.assertIn('current SVG is unchanged', last_prompt)
                        self.assertIn('200 10', last_prompt)

    async def test_json_repair_moves_the_reported_occluding_shape(self):
        import json
        source = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720"><text x="100" y="200" font-size="18">Explanation</text><rect x="80" y="175" width="300" height="50" fill="#FFFFFF"/></svg>'
        calls = 0
        async def chat(messages, model, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return LLMResponse(content=source)
            current = svg_executor._extract_svg(messages[1].content)
            root = ET.fromstring(current)
            rect_id = next(root.iter(NS+'rect')).get('data-editor-id')
            return LLMResponse(content=json.dumps({'edits':[{'target':rect_id,'attributes':{'y':'300'}}]}))
        with TemporaryDirectory() as directory:
            provider = SimpleNamespace(chat=chat)
            _results = [result async for result in svg_executor.generate_svg_pages_parallel('', '<!-- page_type: content -->\n# Method', Path(directory), provider, 'test-model', mode='page_parallel', max_critic_attempts=0, enable_visual_critic=False)]
            saved = ET.parse(Path(directory)/'svg_output/slide_001.svg').getroot()
            self.assertEqual(next(saved.iter(NS+'rect')).get('y'), '300')
            self.assertEqual(calls, 2)

    async def test_repairs_receive_compact_math_and_saved_results_are_render_ready(self):
        overfull = MODEL_SVG.replace('84 180 500 100', '84 180 500 10')
        for mode in ('sequential', 'page_parallel'):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                provider = SimpleNamespace(chat=AsyncMock(side_effect=[LLMResponse(content=overfull), LLMResponse(content=MODEL_SVG)]))
                generator = svg_executor.generate_svg_pages if mode == 'sequential' else svg_executor.generate_svg_pages_parallel
                kwargs = {} if mode == 'sequential' else {'mode': mode}
                _results = [result async for result in generator('', '<!-- page_type: content -->\n# Method', Path(directory), provider, 'test-model', max_critic_attempts=0, enable_visual_critic=False, **kwargs)]
                self.assertEqual(provider.chat.await_count, 2)
                messages = provider.chat.call_args.args[0]
                authoring = [message.content for message in messages if '```svg' in message.content]
                self.assertTrue(authoring)
                self.assertTrue(all('data-latex' in text and '<path' not in text for text in authoring))
                saved = ET.parse(Path(directory)/'svg_output/slide_001.svg').getroot()
                self.assertTrue(list(saved.iter(NS+'path')))

    async def test_table_membership_diagnostics_do_not_force_the_entire_manuscript_onto_a_page(self):
        manuscript = '<!-- page_type: content -->\n# Counts\n|Year|Count|\n|---|---|\n|2020|1234.5|\n|2021|0.625|'
        omitted = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720"><text x="100" y="200">2020 1234.5</text></svg>'
        complete = omitted.replace('</svg>', '<text x="100" y="240">2021 0.625</text></svg>')
        for mode in ('sequential', 'page_parallel'):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                provider = SimpleNamespace(chat=AsyncMock(side_effect=[LLMResponse(content=omitted), LLMResponse(content=complete)]))
                generator = svg_executor.generate_svg_pages if mode == 'sequential' else svg_executor.generate_svg_pages_parallel
                kwargs = {} if mode == 'sequential' else {'mode': mode}
                results = [result async for result in generator('', manuscript, Path(directory), provider, 'test-model', max_critic_attempts=0, enable_visual_critic=False, **kwargs)]
                self.assertEqual(len(results), 1)
                self.assertEqual(provider.chat.await_count, 1)
                messages = provider.chat.call_args.args[0]
                self.assertNotIn('source_table_values_missing', '\n'.join(message.content for message in messages if message.role=='user'))

    def test_selected_table_values_are_checked_as_visible_content(self):
        source = '| Year | Count |\n|---|---|\n|2020|1,234.50|\n|2021|0.625|'
        omitted = '<svg xmlns="http://www.w3.org/2000/svg"><text>2020 1234.5</text><rect x="2021" width="0.625"/></svg>'
        report = svg_executor._validate_source_table_values(omitted, source)
        self.assertFalse(report.passed)
        self.assertIn('2021', report.violations[0].detail)
        complete = omitted.replace('</svg>', '<text>2021 0.625</text></svg>')
        self.assertTrue(svg_executor._validate_source_table_values(complete, source).passed)

    async def test_text_collisions_are_repaired_without_style_review(self):
        overlap = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720"><text x="100" y="200" font-size="20">First paragraph</text><text x="100" y="202" font-size="20">Second paragraph</text></svg>'
        with TemporaryDirectory() as directory:
            provider = SimpleNamespace(chat=AsyncMock(side_effect=[LLMResponse(content=overlap), LLMResponse(content=MODEL_SVG)]))
            results = [result async for result in svg_executor.generate_svg_pages_parallel('', '<!-- page_type: content -->\n# Method', Path(directory), provider, 'test-model', mode='page_parallel', max_critic_attempts=0, enable_visual_critic=False)]
            self.assertEqual(len(results), 1)
            self.assertEqual(provider.chat.await_count, 2)

    async def test_native_export_incompatibility_is_repaired_without_style_review(self):
        unsupported = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720"><defs><filter id="shadow"><feDropShadow dx="0" dy="2" stdDeviation="2"/></filter></defs><g filter="url(#shadow)"><rect x="100" y="180" width="300" height="100" fill="#FFFFFF"/><text x="120" y="210">Method</text></g></svg>'
        with TemporaryDirectory() as directory:
            provider = SimpleNamespace(chat=AsyncMock(side_effect=[LLMResponse(content=unsupported), LLMResponse(content=MODEL_SVG)]))
            results = [result async for result in svg_executor.generate_svg_pages_parallel('', '<!-- page_type: content -->\n# Method', Path(directory), provider, 'test-model', mode='page_parallel', max_critic_attempts=0, enable_visual_critic=False)]
            self.assertEqual(len(results), 1)
            self.assertEqual(provider.chat.await_count, 2)
            messages = provider.chat.call_args.args[0]
            self.assertIn('filter_requires_raster_fallback', '\n'.join(message.content for message in messages if message.role == 'user'))

    async def test_measured_capacity_feedback_regenerates_in_both_modes_without_review(self):
        overfull = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
        <g data-role="body" data-textbox="100 180 100 30">
        <text x="100" y="200" font-size="18">A complete paragraph needs enough room to wrap.</text>
        <text x="100" y="228" font-size="18">A second paragraph.</text></g></svg>'''
        for mode in ('sequential', 'page_parallel'):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                provider = SimpleNamespace(chat=AsyncMock(side_effect=[LLMResponse(content=overfull), LLMResponse(content=MODEL_SVG)]))
                generator = svg_executor.generate_svg_pages if mode == 'sequential' else svg_executor.generate_svg_pages_parallel
                kwargs = {} if mode == 'sequential' else {'mode': mode}
                results = [result async for result in generator(
                    '', '<!-- page_type: content -->\n# Method\nTwo paragraphs.', Path(directory), provider, 'test-model',
                    max_critic_attempts=0, enable_visual_critic=False, **kwargs,
                )]
                self.assertEqual(len(results), 1)
                self.assertEqual(provider.chat.await_count, 2)
                messages = provider.chat.call_args.args[0]
                feedback = '\n'.join(str(message.content) for message in messages if message.role == 'user')
                self.assertIn('paragraph_capacity', feedback)
                self.assertIn('Measured space required', feedback)

    async def test_remaining_capacity_findings_are_reported_without_aborting_the_deck(self):
        overfull = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720"><g data-role="body" data-textbox="100 180 50 20"><text x="100" y="200" font-size="18">This paragraph needs several lines of space.</text></g></svg>'
        manuscript = '<!-- page_type: content -->\n# First\n\n---\n\n<!-- page_type: content -->\n# Second'
        for mode in ('sequential', 'page_parallel'):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                provider = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse(content=overfull)))
                critic = AsyncMock()
                generator = svg_executor.generate_svg_pages if mode == 'sequential' else svg_executor.generate_svg_pages_parallel
                kwargs = {} if mode == 'sequential' else {'mode': mode}
                results = [result async for result in generator('', manuscript, Path(directory), provider, 'test-model', max_critic_attempts=0, enable_visual_critic=False, on_critic=critic, **kwargs)]
                self.assertEqual(len(results), 2)
                self.assertEqual(provider.chat.await_count, 4)
                self.assertTrue((Path(directory) / 'svg_output/slide_002.svg').exists())
                self.assertTrue(any(any(v.rule == 'paragraph_capacity' for v in call.args[2].violations) for call in critic.call_args_list))

    async def test_structural_design_brief_reaches_designer_intact(self):
        design = '#### Slide 01 - Opening\n- Type: cover\n- Design brief: Distinctive layered vector composition.\n  Preserve the planner\'s nested design explanation.'
        with TemporaryDirectory() as directory:
            provider = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse(content=MODEL_SVG)))
            async for _ in svg_executor.generate_svg_pages_parallel(design, '<!-- page_type: cover -->\n# A Study', Path(directory), provider, 'test-model', mode='page_parallel', max_critic_attempts=0, enable_visual_critic=False):
                pass
            messages = provider.chat.call_args.args[0]
            user_content = '\n'.join(str(message.content) for message in messages if message.role == 'user')
            self.assertIn('Distinctive layered vector composition.', user_content)
            self.assertIn("Preserve the planner's nested design explanation.", user_content)

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
