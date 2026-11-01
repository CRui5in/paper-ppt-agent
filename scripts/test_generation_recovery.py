"""Exercise explicit continuation without repeating completed model work."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from backend.api.endpoints import recovery
from backend.api.schemas import ResumeGenerationRequest
from backend.config import settings
from backend.llm import LLMResponse
from backend.orchestrator import pipeline, svg_executor
from backend.orchestrator.checkpoints import GenerationCheckpoints
from backend.parser.paper_model import ParsedPaper, PaperFigure, PaperSection
from backend.runtime.job_event_monitor import write_generation_request
from backend.session.manager import SessionManager
from backend.workers.generation_worker import _request_from_payload


SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720"><text x="80" y="160" font-size="28">A saved page</text></svg>'
MANUSCRIPT = '\n\n---\n\n'.join(f'<!-- page_type: content -->\n# Page {i}' for i in range(1, 4))


class CheckpointTests(unittest.TestCase):
    def test_paper_roundtrip_and_tamper_detection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / 'sources/figure.png'
            image.parent.mkdir()
            image.write_bytes(b'original image')
            paper = ParsedPaper('Title', sections=[PaperSection('Method', 1, figures=[PaperFigure(image)])])
            checkpoints = GenerationCheckpoints(root)
            checkpoints.save_paper(paper)
            loaded = GenerationCheckpoints(root).load_paper()
            self.assertEqual(loaded.all_figures()[0].path, image)
            image.write_bytes(b'changed image')
            with self.assertRaisesRegex(ValueError, 'changed or is missing'):
                checkpoints.load_paper()


class ResumePageTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_reuses_completed_and_initial_pages_in_all_modes(self):
        for mode in ('sequential', 'page_parallel', 'chapter_parallel'):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                root = Path(directory)
                calls = 0
                async def first_generation(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls > 1:
                        await asyncio.Event().wait()
                    return LLMResponse(content=SVG)
                llm = SimpleNamespace(chat=AsyncMock(side_effect=first_generation))
                generator = svg_executor.generate_svg_pages if mode == 'sequential' else svg_executor.generate_svg_pages_parallel
                kwargs = {} if mode == 'sequential' else {'mode': mode, 'parallel_concurrency': 1}
                first = generator('', MANUSCRIPT, root, llm, 'test', **kwargs)
                self.assertEqual((await anext(first))[0], 1)
                await first.aclose()
                # Simulate a raw response saved immediately before a render failure.
                initial = root / 'svg_initial/slide_002.svg'
                initial.write_text(SVG, encoding='utf8')
                original = (root / 'svg_output/slide_001.svg').read_bytes()
                llm.chat.reset_mock()
                llm.chat.side_effect = None
                llm.chat.return_value = LLMResponse(content=SVG)
                results = [item async for item in generator('', MANUSCRIPT, root, llm, 'test', resume=True, **kwargs)]
                self.assertEqual(sorted(page for page, _ in results), [1, 2, 3])
                self.assertEqual(llm.chat.await_count, 1)
                self.assertEqual((root / 'svg_output/slide_001.svg').read_bytes(), original)

    async def test_retry_regenerates_only_unfinished_pages_and_retains_failed_source(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'svg_output').mkdir()
            (root / 'svg_initial').mkdir()
            output = root / 'svg_output/slide_001.svg'
            output.write_text(SVG, encoding='utf8')
            GenerationCheckpoints(root).save_page(1, output)
            invalid = '<svg><!-- invalid -- comment --></svg>'
            (root / 'svg_initial/slide_002.svg').write_text(invalid, encoding='utf8')
            llm = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse(content=SVG)))
            with self.assertRaisesRegex(RuntimeError, 'Invalid XML'):
                _ = [item async for item in svg_executor.generate_svg_pages('', MANUSCRIPT, root, llm, 'test', resume=True)]
            self.assertEqual(llm.chat.await_count, 0)
            _ = [item async for item in svg_executor.generate_svg_pages('', MANUSCRIPT, root, llm, 'test', resume=True, retry_failed_pages=True)]
            self.assertEqual(llm.chat.await_count, 2)
            self.assertEqual(next((root / 'checkpoints/attempts').glob('*.svg')).read_text(encoding='utf8'), invalid)

    async def test_full_pipeline_resume_skips_parse_research_strategy_after_failure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            llm = SimpleNamespace(chat=AsyncMock(side_effect=[LLMResponse(content=SVG), ConnectionError('lost upstream')]))
            request = pipeline.GenerationRequest(root/'paper.pdf', 'pdf', 'test', 'model', 'secret')
            parse = AsyncMock(return_value=ParsedPaper('A paper'))
            research = AsyncMock(return_value=MANUSCRIPT)
            strategy = AsyncMock(return_value='# Design')
            def export(files, path, **kwargs):
                path.write_bytes(b'test export')
            with patch.object(settings, 'workspaces_dir', root), \
                 patch('backend.llm.create_provider', return_value=llm), \
                 patch('backend.parser.pdf_parser.PDFParser.parse', parse), \
                 patch.object(pipeline.research_agent, 'analyze_paper', research), \
                 patch.object(pipeline.strategist_agent, 'create_design_spec', strategy), \
                 patch('backend.generator.svg_to_pptx.create_pptx', export):
                with self.assertRaisesRegex(ConnectionError, 'lost upstream'):
                    _ = [event async for event in pipeline.run_pipeline(request)]
                project = next(root.glob('paper_ppt_*'))
                old = (project/'svg_output/slide_001.svg').read_bytes()
                llm.chat = AsyncMock(return_value=LLMResponse(content=SVG))
                events = [event async for event in pipeline.run_pipeline(replace(request, resume_project_dir=str(project)))]
                self.assertEqual(events[-1].stage, 'export')
                self.assertEqual(llm.chat.await_count, 2)
                self.assertEqual(parse.await_count, 1)
                self.assertEqual(research.await_count, 1)
                self.assertEqual(strategy.await_count, 1)
                self.assertEqual((project/'svg_output/slide_001.svg').read_bytes(), old)


class RecoveryApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime_patch = patch.object(settings, 'runtime_dir', self.root)
        self.runtime_patch.start()
        self.manager = SessionManager()
        self.manager_patch = patch.object(recovery, 'session_manager', self.manager)
        self.manager_patch.start()
        self.parent = self.manager.create_job('session')
        self.workspace = self.root/'workspace'
        self.workspace.mkdir()
        self.manager.update_job(self.parent.id, status='error', error='Original error', project_dir=str(self.workspace), provider='deepseek')
        request = pipeline.GenerationRequest(self.root/'paper.pdf', 'pdf', 'deepseek', 'deepseek-flash', 'test-only-key', job_id=self.parent.id)
        await write_generation_request(self.parent.id, request)

    async def asyncTearDown(self):
        await self.manager.flush_now()
        if self.manager._flush_task:
            self.manager._flush_task.cancel()
            await asyncio.gather(self.manager._flush_task, return_exceptions=True)
        self.manager_patch.stop()
        self.runtime_patch.stop()
        self.temporary.cleanup()

    async def test_resume_is_durable_keeps_parameters_and_blocks_duplicate(self):
        scheduler = SimpleNamespace(submit=AsyncMock(return_value=0))
        with patch.object(recovery, 'get_scheduler', return_value=scheduler):
            response = await recovery.resume_generation(self.parent.id, ResumeGenerationRequest(feedback='Use fewer labels'))
            request_path = self.root/'job_requests'/f'{response.job_id}.json'
            saved = _request_from_payload(json.loads(request_path.read_text(encoding='utf8')))
            self.assertEqual(saved.api_key, 'test-only-key')
            self.assertEqual(saved.model, 'deepseek-flash')
            self.assertEqual(saved.resume_project_dir, str(self.workspace))
            self.assertEqual(saved.resume_feedback, 'Use fewer labels')
            self.assertEqual(self.parent.status, 'error')
            with self.assertRaises(HTTPException) as caught:
                await recovery.resume_generation(self.parent.id, ResumeGenerationRequest())
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(scheduler.submit.await_count, 1)
            await self.manager.flush_now()
            restored = SessionManager()
            self.assertEqual(restored.get_job(response.job_id).parent_job_id, self.parent.id)

    async def test_deleting_parent_preserves_workspace_used_by_continuation(self):
        child = self.manager.create_job('session')
        self.manager.update_job(child.id, project_dir=str(self.workspace), parent_job_id=self.parent.id)
        with patch.object(settings, 'workspaces_dir', self.root):
            self.manager.delete_job(self.parent.id, delete_files=True)
            self.assertTrue(self.workspace.is_dir())


if __name__ == '__main__':
    unittest.main()
