"""Regression checks for runtime errors being mistaken for empty slide exports."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.generator.template_agent import _claude_result_error_message, _codex_turn_error_message
from backend.orchestrator import agent_pipeline as pipeline


class ResultMessage(NS):
    pass


def notification(method, **payload):
    return NS(method=method, payload=NS(**payload))


async def stream(messages):
    for message in messages:
        yield message


class RuntimeErrorTests(unittest.IsolatedAsyncioTestCase):
    async def drive_codex(self, messages):
        with TemporaryDirectory() as directory:
            session = pipeline.PersistentAgentSession(
                Path(directory), "codex", {"model": "test-model"},
                export_gate=pipeline._AgentExportGateConfig("ppt169", 0, "test", 3),
            )
            emitted = []
            session.on_message = emitted.append
            session._prompt_queue.put("Generate slides")
            turn = NS(stream=lambda: stream(messages), interrupt=AsyncMock())
            thread = NS(id="test-thread", turn=AsyncMock(return_value=turn))
            codex = MagicMock()
            codex.__aenter__.return_value = codex
            codex.thread_start = AsyncMock(return_value=thread)
            passed = pipeline._AgentExportGateResult(True, "export", "ok", output_path="test.pptx")
            with patch("openai_codex.AsyncCodex", return_value=codex), \
                    patch.object(pipeline, "_install_codex_jsonrpc_noise_filter"), \
                    patch.object(pipeline, "_agent_runtime_env", return_value={}), \
                    patch.object(pipeline, "_run_agent_export_gate", new_callable=AsyncMock, return_value=passed) as gate:
                await asyncio.wait_for(session._drive_codex(), 3)
            return emitted, gate.await_count, thread.turn.await_count

    async def test_failed_codex_turn_preserves_reason_and_skips_export_retries(self):
        reason = "The 'test-model' model is not supported when using Codex with a ChatGPT account."
        event = notification("turn/completed", turn=NS(status="failed", error=NS(message=reason, additional_details="model_not_found")))
        emitted, exports, turns = await self.drive_codex([event])
        errors = [str(item) for item in emitted if isinstance(item, RuntimeError)]
        self.assertEqual(len(errors), 1)
        self.assertIn(reason, errors[0])
        self.assertIn("model_not_found", errors[0])
        self.assertEqual((exports, turns), (0, 1))

    async def test_terminal_error_notification_stops_before_export(self):
        event = notification("error", error=NS(message="401 token expired", additional_details=None), will_retry=False)
        emitted, exports, turns = await self.drive_codex([event])
        self.assertTrue(any("401 token expired" in str(item) for item in emitted))
        self.assertEqual((exports, turns), (0, 1))

    async def test_retriable_error_can_recover_and_export(self):
        retry = notification("error", error=NS(message="Retrying connection"), will_retry=True)
        done = notification("turn/completed", turn=NS(status="completed", error=None))
        emitted, exports, turns = await self.drive_codex([retry, done])
        self.assertFalse(any(isinstance(item, BaseException) for item in emitted))
        self.assertEqual((exports, turns), (1, 1))

    async def test_incomplete_stream_does_not_run_export(self):
        emitted, exports, _ = await self.drive_codex([])
        self.assertTrue(any("without a completed turn" in str(item) for item in emitted))
        self.assertEqual(exports, 0)

    async def test_claude_error_result_is_not_hidden_by_success_subtype(self):
        result = ResultMessage(subtype="success", is_error=True, result="success", errors=["authentication_failed"], api_error_status=401)
        error = _claude_result_error_message(result)
        self.assertIn("authentication_failed", error)
        self.assertIn("401", error)
        with TemporaryDirectory() as directory:
            session = pipeline.PersistentAgentSession(Path(directory), "claude_code", {})
            session.on_message = lambda _: None
            client = NS(receive_response=lambda: stream([result]), interrupt=AsyncMock())
            with self.assertRaisesRegex(RuntimeError, "authentication_failed"):
                await session._receive_claude_response(client)

    async def test_claude_turn_limit_remains_a_continuation(self):
        result = ResultMessage(subtype="error_max_turns", is_error=True, result="Maximum number of turns")
        with TemporaryDirectory() as directory:
            session = pipeline.PersistentAgentSession(Path(directory), "claude_code", {})
            session.on_message = lambda _: None
            outcome = await session._receive_claude_response(NS(receive_response=lambda: stream([result]), interrupt=AsyncMock()))
            self.assertTrue(outcome.hit_turn_limit)

    async def test_incomplete_claude_stream_is_a_runtime_error(self):
        with TemporaryDirectory() as directory:
            session = pipeline.PersistentAgentSession(Path(directory), "claude_code", {})
            session.on_message = lambda _: None
            with self.assertRaisesRegex(RuntimeError, "without a result"):
                await session._receive_claude_response(NS(receive_response=lambda: stream([]), interrupt=AsyncMock()))

    async def test_direct_feedback_errors_become_banner_events(self):
        with TemporaryDirectory() as directory:
            session = pipeline.PersistentAgentSession(Path(directory), "codex", {}, job_id="test")
            job = NS(status="agent", progress=0.3, slides_completed=0, total_slides=0)
            manager = NS(get_job=lambda _: job, record_event=MagicMock())
            with patch.dict("sys.modules", {"backend.api.endpoints.generate": NS(session_manager=manager)}):
                session.set_direct_mode()
                session._emit(RuntimeError("Codex Agent failed: model_not_found"))
            calls = manager.record_event.call_args_list
            self.assertTrue(any(call.args[1]["type"] == "error" for call in calls))
            self.assertTrue(any(call.kwargs.get("error") == "Codex Agent failed: model_not_found" for call in calls))

    def test_successful_results_do_not_report_errors(self):
        self.assertIsNone(_claude_result_error_message(ResultMessage(subtype="success", is_error=False, result="Done")))
        self.assertIsNone(_codex_turn_error_message(notification("turn/completed", turn=NS(status="completed", error=None))))


if __name__ == "__main__":
    unittest.main()
