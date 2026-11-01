"""Separate visible SVG budgets from reasoning and preserve response failure causes."""

import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from backend.llm.provider_openai import OpenAIProvider
from backend.llm.types import LLMMessage, LLMResponse, TokenUsage
from backend.orchestrator.svg_executor import _chat_svg_generation
from backend.usage.tracker import reset_usage_context, set_usage_context


class SvgCompletionBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_retains_finish_reason_even_when_gateway_omits_usage(self):
        provider = OpenAIProvider('test-only')
        try:
            for usage in (None, SimpleNamespace(prompt_tokens=7000, completion_tokens=16384,
                                               completion_tokens_details=SimpleNamespace(reasoning_tokens=16384))):
                response = SimpleNamespace(choices=[SimpleNamespace(finish_reason='length', message=SimpleNamespace(content=''))], usage=usage)
                provider._create_chat_completion = AsyncMock(return_value=response)
                with patch('backend.llm.provider_openai.usage_tracker.record'):
                    result = await provider.chat([LLMMessage.user('Draw one page')], 'test-model', max_tokens=16384)
                self.assertEqual(result.finish_reason, 'length')
                self.assertEqual(result.reasoning_tokens, 16384 if usage else 0)
        finally:
            await provider._client.close()

    async def test_deepseek_thinking_and_direct_output_get_distinct_total_budgets(self):
        for artifact_mode, thinking, expected in (
            ('default', True, None), ('default', False, 16384), ('disabled', True, 16384),
        ):
            with self.subTest(artifact_mode=artifact_mode, thinking=thinking):
                settings = {'thinking_enabled': thinking, 'reasoning_effort': 'max'}
                original = copy.deepcopy(settings)
                provider = OpenAIProvider('test-only', provider_name='deepseek', base_url='https://api.deepseek.com',
                                          artifact_thinking_mode=artifact_mode, deepseek_settings=settings)
                context = set_usage_context(stage='generation')
                try:
                    total = provider.artifact_max_tokens('deepseek-flash', 16384)
                    self.assertEqual(total, expected)
                    kwargs = provider._build_chat_kwargs([LLMMessage.user('Draw one page')], 'deepseek-flash',
                                                        temperature=.3, max_tokens=total)
                    if expected is None:
                        self.assertNotIn('max_tokens', kwargs)
                        self.assertEqual(kwargs['reasoning_effort'], 'max')
                        self.assertEqual(kwargs['extra_body']['thinking'], {'type': 'enabled'})
                    else:
                        self.assertEqual(kwargs['max_tokens'], 16384)
                        self.assertEqual(kwargs['extra_body']['thinking'], {'type': 'disabled'})
                    self.assertEqual(settings, original)
                finally:
                    reset_usage_context(context)
                    await provider._client.close()

    async def test_thinking_request_passes_native_budget_on_its_first_call(self):
        control = OpenAIProvider('test-only', provider_name='deepseek', base_url='https://api.deepseek.com', artifact_thinking_mode='default',
                                 deepseek_settings={'thinking_enabled': True, 'reasoning_effort': 'max'})
        provider = SimpleNamespace(artifact_max_tokens=control.artifact_max_tokens,
            chat=AsyncMock(return_value=LLMResponse('<svg/>', finish_reason='stop', reasoning_tokens=21000)))
        try:
            with TemporaryDirectory() as directory:
                await _chat_svg_generation(provider, [], 'deepseek-flash', temperature=.3, max_tokens=16384,
                    project_dir=Path(directory), debug_prefix='executor', page_num=1, attempt=1)
                self.assertIsNone(provider.chat.call_args.kwargs['max_tokens'])
                self.assertEqual(provider.chat.await_count, 1)
        finally:
            await control._client.close()

    async def test_truncated_reasoning_empty_and_partial_answers_report_their_cause(self):
        for content, finish_reason, reasoning, expected in (
            ('', 'length', 16384, 'total output limit'),
            ('<svg><text>unfinished', 'length', 12000, 'total output limit'),
            ('', 'stop', 9000, 'empty final response'),
        ):
            with self.subTest(content=content, finish_reason=finish_reason), TemporaryDirectory() as directory:
                provider = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse(content,
                    usage=TokenUsage(7000, 16384), finish_reason=finish_reason, reasoning_tokens=reasoning)))
                with self.assertRaisesRegex(RuntimeError, expected):
                    await _chat_svg_generation(provider, [], 'test-model', temperature=.3, max_tokens=16384,
                        project_dir=Path(directory), debug_prefix='executor', page_num=1, attempt=1)
                self.assertEqual(provider.chat.await_count, 1)
                saved = (Path(directory)/'debug/executor_page_01_response_attempt_1.md').read_text(encoding='utf8')
                self.assertIn(f'finish_reason={finish_reason}', saved)
                self.assertIn(f'reasoning_tokens={reasoning}', saved)
                if content:
                    self.assertIn(content, saved)


if __name__ == '__main__':
    unittest.main()
