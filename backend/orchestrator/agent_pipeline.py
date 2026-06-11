"""Independent Claude Code / Codex powered PPT generation pipeline.

This module intentionally does not use ``backend.llm.create_provider``.  Agent
generation is a separate orchestration path: the backend prepares a workspace
and an output contract, starts the selected coding agent runtime, streams any
SVG artifacts the agent writes, then reuses deterministic finalize/export code.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import sys
import threading
import tomllib
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal
from xml.etree import ElementTree as ET

from backend.config import CANVAS_FORMATS, PROJECT_ROOT, settings
from backend.generator.project_manager import get_notes, get_svg_files, init_project
from backend.generator.svg_finalize import finalize_project
from backend.generator.svg_to_pptx import create_pptx
from backend.generator.template_agent import _events_from_sdk_message
from backend.orchestrator.manuscript import auto_slide_range, page_type_budget, page_type_budget_guidance
from backend.runtime import aoffload, arun, awrite_text
from backend.runtime.resource_gates import heavy_stage_slot

logger = logging.getLogger(__name__)


@dataclass
class AgentProgressEvent:
    stage: str
    status: Literal["started", "progress", "complete", "error"]
    message: str = ""
    progress: float = 0.0
    data: dict | None = None


@dataclass
class _AgentUsageTotals:
    runtime: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    total_cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0
    seen_message_ids: set[str] | None = None
    seen_result_ids: set[str] | None = None
    pending_input_tokens: int = 0
    pending_output_tokens: int = 0
    pending_cache_read_input_tokens: int = 0
    pending_cache_creation_input_tokens: int = 0


@dataclass
class _ClaudeResponseOutcome:
    feedback: str | None = None
    hit_turn_limit: bool = False
    interrupted: bool = False


_RUNTIME_DEFAULT = "claude_code"
_AGENT_SKILL_NAME = "paper-ppt-generate"
_RESEARCH_SKILL_NAME = "paper-ppt-research"
_DEEP_RESEARCH_SKILL_NAME = "paper-ppt-deep-research"
_SVG_SCAN_SECONDS = 1.0
_AGENT_IDLE_NOTICE_SECONDS = 60.0
_AGENT_INTERRUPT_POLL_SECONDS = 0.1
_AGENT_IDLE_MESSAGE = (
    "Agent has not produced new activity for a while. "
    "You can pause it or send guidance to continue."
)
_AGENT_AUTHORING_SCRIPT_SUFFIXES = {
    ".bat",
    ".cmd",
    ".js",
    ".mjs",
    ".cjs",
    ".ps1",
    ".py",
    ".rb",
    ".sh",
    ".ts",
}
_AGENT_AUTHORING_SCAN_EXCLUDED_ROOTS = {
    "agent_feedback",
    "exports",
    "notes",
    "research",
    "skills",
    "source_assets",
    "sources",
    "svg_final",
    "svg_output",
    "templates",
}

# Sentinel marking end of the persistent session message stream.
_SESSION_DONE = object()


class _TransientSvgPreviewError(RuntimeError):
    """Raised when an Agent SVG looks like an in-progress file write."""


async def agent_runtime_status(runtime: str) -> dict[str, Any]:
    """Return whether an Agent runtime is usable before a job is queued."""
    runtime = runtime or _RUNTIME_DEFAULT
    if runtime == "claude_code":
        from backend.generator.template_agent import claude_code_environment_status

        return {"runtime": runtime, **claude_code_environment_status()}
    if runtime != "codex":
        return {
            "runtime": runtime,
            "available": False,
            "message": f"Unsupported Agent runtime: {runtime}",
        }

    try:
        from openai_codex import AppServerConfig, AsyncCodex
    except ImportError as exc:
        return {
            "runtime": runtime,
            "available": False,
            "sdk_available": False,
            "message": (
                "Codex Agent runtime requires the `openai-codex` Python SDK. "
                "Run `uv sync` after updating dependencies, then start the backend again."
            ),
            "error": str(exc) or exc.__class__.__name__,
        }

    _install_codex_jsonrpc_noise_filter()

    try:
        codex_config = AppServerConfig(
            cwd=str(PROJECT_ROOT),
            env={},
            codex_bin=str(settings.codex_bin) if settings.codex_bin else None,
        )
        async with AsyncCodex(config=codex_config) as codex:
            account = await codex.account(refresh_token=False)
            account_obj = getattr(account, "account", None)
            if account_obj is None:
                return {
                    "runtime": runtime,
                    "available": False,
                    "sdk_available": True,
                    "authenticated": False,
                    "message": (
                        "Codex is not logged in. Sign in with the Codex CLI, or start a "
                        "ChatGPT OAuth/device-code login through the Codex SDK before running Agent mode."
                    ),
                    "supports_chatgpt_oauth": True,
                }
            return {
                "runtime": runtime,
                "available": True,
                "sdk_available": True,
                "authenticated": True,
                **agent_runtime_defaults(runtime),
                "requires_openai_auth": bool(getattr(account, "requires_openai_auth", False)),
                "message": "Codex SDK is available and authenticated.",
            }
    except Exception as exc:  # noqa: BLE001 - surface runtime/auth issues
        return {
            "runtime": runtime,
            "available": False,
            "sdk_available": True,
            "authenticated": False,
            "message": f"Codex runtime is not ready: {exc}",
            "error": str(exc) or exc.__class__.__name__,
            "supports_chatgpt_oauth": True,
        }


def agent_runtime_defaults(runtime: str) -> dict[str, str]:
    """Best-effort defaults used by the selected Agent runtime."""
    if (runtime or _RUNTIME_DEFAULT) != "codex":
        return {}
    defaults: dict[str, str] = {}
    model = _codex_default_model()
    effort = _codex_default_reasoning_effort()
    if model:
        defaults["model"] = model
    if effort:
        defaults["reasoning_effort"] = effort
    return defaults


class PersistentAgentSession:
    """Keeps a Claude Code or Codex agent client alive across multiple
    user interactions within a single generation job.

    The session broadcasts every SDK message through an ``on_message``
    callback.  During initial generation the callback pushes to a bridge
    queue so the pipeline generator can yield events.  After the pipeline
    returns the callback is switched to ``session_manager.record_event``
    so feedback turns are broadcast directly to WebSocket subscribers.
    """

    def __init__(
        self,
        project_dir: Path,
        runtime: str,
        agent_config: dict[str, Any],
        *,
        resume_session_id: str | None = None,
        job_id: str = "",
    ) -> None:
        self._project_dir = project_dir
        self._runtime = runtime
        self._agent_config = agent_config
        self._resume_session_id = resume_session_id
        self._job_id = job_id

        # Cross-thread prompt injection (main thread -> worker thread).
        self._prompt_queue: queue.Queue[str | None] = queue.Queue()
        # Message bridge (worker thread -> caller async loop).
        self._bridge: asyncio.Queue[Any] = asyncio.Queue(maxsize=128)
        self._cancel_event = threading.Event()
        self._interrupt_event = threading.Event()
        self._worker_loop_holder: dict[str, asyncio.AbstractEventLoop] = {}
        self._ready = threading.Event()
        self._session_id: str | None = resume_session_id
        self._active = True
        self._accepting_feedback = False
        self._worker_thread: threading.Thread | None = None
        self._caller_loop: asyncio.AbstractEventLoop | None = None
        # Callback invoked for every SDK message.  Can be swapped at
        # runtime (e.g. from bridge-queue mode to direct-record mode).
        self.on_message: Callable[[Any], None] | None = None
        # Usage tracking
        self._usage_totals = _AgentUsageTotals(runtime=runtime)
        configured_model = agent_config.get("model")
        if isinstance(configured_model, str) and configured_model.strip():
            self._usage_totals.model = configured_model.strip()

    # -- Public API (called from the main async loop) ----------------------

    async def start(self, initial_prompt: str) -> None:
        """Launch the worker thread and send the first prompt."""
        self._caller_loop = asyncio.get_running_loop()
        self._worker_thread = threading.Thread(
            target=self._worker,
            name=f"agent-session-{self._runtime}",
            daemon=True,
        )
        self._worker_thread.start()
        ready = await asyncio.to_thread(
            self._ready.wait,
            max(1, int(settings.agent_runtime_ready_timeout or 30)),
        )
        if not ready:
            self._cancel_event.set()
            raise TimeoutError(
                f"Agent runtime '{self._runtime}' did not become ready within "
                f"{settings.agent_runtime_ready_timeout}s"
            )
        if self._usage_totals.model:
            self._emit(_agent_usage_progress_event(self._usage_totals))
        self._accepting_feedback = True
        self._prompt_queue.put(initial_prompt)

    async def send_message(self, prompt: str, *, interrupt_current: bool = False) -> None:
        """Inject a prompt and optionally interrupt an in-flight response."""
        if not self.can_accept_feedback:
            raise RuntimeError("Agent session is not accepting feedback.")
        self._prompt_queue.put(prompt)
        if interrupt_current:
            self._interrupt_event.set()

    async def interrupt(self) -> None:
        """Request a soft interruption of the current agent turn.

        Unlike ``close()``, this keeps the SDK session alive so a follow-up
        prompt can continue the conversation.
        """
        self._interrupt_event.set()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def can_accept_feedback(self) -> bool:
        thread_alive = self._worker_thread is None or self._worker_thread.is_alive()
        return self._active and self._accepting_feedback and thread_alive

    @session_id.setter
    def session_id(self, value: str | None) -> None:
        self._session_id = value

    def __aiter__(self) -> PersistentAgentSession:
        return self

    async def __anext__(self) -> Any:
        while True:
            item = await self._bridge.get()
            if item is _SESSION_DONE:
                raise StopAsyncIteration
            if isinstance(item, BaseException):
                raise item
            return item

    async def close(self) -> None:
        """Signal the worker to stop and wait for it to finish."""
        self._active = False
        self._cancel_event.set()
        self._prompt_queue.put(None)  # unblock the worker
        if self._worker_thread and self._worker_thread.is_alive():
            await asyncio.to_thread(self._worker_thread.join, 10)

    def set_bridge_mode(self) -> None:
        """Route messages to the bridge queue (for pipeline generator)."""
        self.on_message = None  # default: use _put_threadsafe -> bridge

    def set_direct_mode(self) -> None:
        """Route messages directly via session_manager.record_event."""
        from backend.session.progress import payloads_from_progress_event

        def _record(message: Any) -> None:
            from backend.api.endpoints.generate import session_manager

            job = session_manager.get_job(self._job_id)
            if job is None:
                return
            if isinstance(message, AgentProgressEvent):
                for payload, updates in payloads_from_progress_event(
                    self._job_id, job, message
                ):
                    session_manager.record_event(self._job_id, payload, **updates)
                return
            usage_event = _update_agent_usage_totals(self._usage_totals, message)
            if usage_event is not None:
                for payload, updates in payloads_from_progress_event(
                    self._job_id, job, usage_event
                ):
                    session_manager.record_event(self._job_id, payload, **updates)
            # Also record agent events
            if self._runtime == "codex":
                item_event = _codex_item_progress_event(message, str(getattr(message, "method", "") or ""))
                if item_event is not None:
                    for payload, updates in payloads_from_progress_event(
                        self._job_id, job, item_event
                    ):
                        session_manager.record_event(self._job_id, payload, **updates)
            else:
                for sdk_event in _agent_sdk_events_from_message(message):
                    msg = str(sdk_event.get("message") or "").strip()
                    if msg:
                        evt = AgentProgressEvent(
                            "agent", "progress", msg, 0.12,
                            data={"runtime": "claude_code", "agent_event": sdk_event},
                        )
                        for payload, updates in payloads_from_progress_event(
                            self._job_id, job, evt
                        ):
                            session_manager.record_event(
                                self._job_id, payload, **updates
                            )

        self.on_message = _record

    # -- Internal worker thread --------------------------------------------

    def _emit(self, message: Any) -> None:
        """Route a message through the callback or bridge queue."""
        if self.on_message is not None:
            try:
                self.on_message(message)
            except Exception:
                pass
        else:
            # Default: push to bridge queue for pipeline generator.
            loop = self._caller_loop
            if loop is None or loop.is_closed():
                return

            async def _put() -> None:
                await self._bridge.put(message)

            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            try:
                if current_loop is loop:
                    loop.create_task(_put())
                else:
                    asyncio.run_coroutine_threadsafe(_put(), loop)
            except RuntimeError:
                pass

    def _worker(self) -> None:
        if sys.platform == "win32":
            loop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._worker_loop_holder["loop"] = loop
        self._ready.set()
        try:
            loop.run_until_complete(self._drive())
        finally:
            loop.close()

    async def _drive(self) -> None:
        if self._runtime == "codex":
            await self._drive_codex()
        else:
            await self._drive_claude()

    async def _drive_claude(self) -> None:
        # Let Claude run to natural completion by default. Live feedback is
        # still picked up after assistant/tool-result messages and interrupts
        # the current response at a safe boundary.
        try:
            from claude_agent_sdk import ClaudeSDKClient

            options = self._build_claude_options()
            async with ClaudeSDKClient(options=options) as client:
                first_prompt: str | None = None
                # Wait for the initial prompt.
                while self._active:
                    try:
                        first_prompt = self._prompt_queue.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    break
                if not first_prompt or self._cancel_event.is_set():
                    return

                # -- First chunk: process the initial prompt.
                await client.query(first_prompt)
                self._accepting_feedback = True
                outcome = await self._receive_claude_response(
                    client,
                    interrupt_on_feedback=True,
                )
                if self._cancel_event.is_set():
                    return
                live_feedback = outcome.feedback
                continue_work = outcome.hit_turn_limit
                paused = outcome.interrupted
                if paused:
                    self._emit(_agent_paused_event())

                # -- Continuation / live feedback loop --------------------
                while self._active:
                    if paused:
                        user_msg = await self._async_wait_for_user_message(timeout=1.0)
                        if self._cancel_event.is_set():
                            return
                        if user_msg is None:
                            continue
                        await client.query(_live_feedback_prompt(user_msg))
                        outcome = await self._receive_claude_response(
                            client,
                            interrupt_on_feedback=True,
                        )
                        if self._cancel_event.is_set():
                            return
                        live_feedback = outcome.feedback
                        continue_work = outcome.hit_turn_limit
                        paused = outcome.interrupted
                        if paused:
                            self._emit(_agent_paused_event())
                        continue

                    if live_feedback is not None:
                        await client.query(_live_feedback_prompt(live_feedback))
                        outcome = await self._receive_claude_response(
                            client,
                            interrupt_on_feedback=True,
                        )
                        if self._cancel_event.is_set():
                            return
                        live_feedback = outcome.feedback
                        continue_work = outcome.hit_turn_limit
                        paused = outcome.interrupted
                        if paused:
                            self._emit(_agent_paused_event())
                        continue

                    # 1. Check for queued user messages.
                    user_msg = self._drain_user_message()
                    if user_msg is not None:
                        # User sent feedback — process it as a new turn.
                        await client.query(_live_feedback_prompt(user_msg))
                        outcome = await self._receive_claude_response(
                            client,
                            interrupt_on_feedback=True,
                        )
                        if self._cancel_event.is_set():
                            return
                        live_feedback = outcome.feedback
                        continue_work = outcome.hit_turn_limit
                        paused = outcome.interrupted
                        if paused:
                            self._emit(_agent_paused_event())
                        continue

                    if not continue_work:
                        self._active = False
                        break

                    # 2. Claude hit a configured turn limit — send continuation prompt.
                    await client.query(
                        "Continue your work from where you left off. "
                        "Do not repeat completed steps. "
                        "After each major milestone (e.g. completing a slide), "
                        "briefly note what you just finished."
                    )
                    outcome = await self._receive_claude_response(
                        client,
                        interrupt_on_feedback=True,
                    )
                    if self._cancel_event.is_set():
                        return
                    live_feedback = outcome.feedback
                    continue_work = outcome.hit_turn_limit
                    paused = outcome.interrupted
                    if paused:
                        self._emit(_agent_paused_event())

        except BaseException as exc:
            if _is_false_success_error(exc):
                return
            self._emit(exc)
        finally:
            self._accepting_feedback = False
            self._emit(_SESSION_DONE)

    async def _receive_claude_response(
        self,
        client: Any,
        *,
        interrupt_on_feedback: bool = False,
    ) -> _ClaudeResponseOutcome:
        """Drain one Claude response, optionally cutting it short for feedback.

        Claude's SDK requires the current response stream to be drained after
        ``interrupt()`` before a new ``query()`` can be sent.  We therefore
        interrupt as soon as feedback is observed after an emitted SDK message,
        keep draining the interrupted ResultMessage, and return the latest
        queued feedback for the caller to submit as the next turn.
        """
        pending_feedback: str | None = None
        interrupt_sent = False
        hit_turn_limit = False
        soft_interrupted = False
        async def _interrupt_when_requested() -> None:
            nonlocal interrupt_sent, pending_feedback, soft_interrupted
            while self._active or self._cancel_event.is_set():
                if self._cancel_event.is_set():
                    try:
                        await client.interrupt()
                    except Exception:
                        logger.exception("Failed to cancel Claude session")
                    return
                if not self._interrupt_event.is_set():
                    await asyncio.sleep(_AGENT_INTERRUPT_POLL_SECONDS)
                    continue
                self._interrupt_event.clear()
                pending_feedback = self._drain_user_message() or pending_feedback
                if interrupt_sent:
                    return
                interrupt_sent = True
                try:
                    await client.interrupt()
                    soft_interrupted = True
                except Exception:
                    interrupt_sent = False
                    logger.exception("Failed to interrupt Claude session")
                return

        interrupt_watcher = asyncio.create_task(_interrupt_when_requested())
        idle_timeout = _agent_turn_idle_timeout()
        iterator = client.receive_response().__aiter__()
        try:
            while True:
                try:
                    message = await _next_agent_stream_item(iterator, idle_timeout)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    try:
                        await client.interrupt()
                    except Exception:
                        logger.exception("Failed to interrupt timed-out Claude session")
                    raise TimeoutError(
                        f"Agent turn produced no activity for {idle_timeout}s."
                    ) from None
                if self._cancel_event.is_set():
                    return _ClaudeResponseOutcome(feedback=pending_feedback)

                self._capture_session_id(message)

                if (
                    (soft_interrupted or self._cancel_event.is_set())
                    and _is_feedback_interrupt_result(message)
                ):
                    continue

                self._emit(message)
                if _is_claude_turn_limit_result(message):
                    hit_turn_limit = True

                if not interrupt_on_feedback or pending_feedback is not None:
                    continue

                if not _claude_message_allows_live_feedback_interrupt(message):
                    continue

                queued_feedback = self._drain_user_message()
                if queued_feedback is None:
                    continue

                pending_feedback = queued_feedback
                if message.__class__.__name__ == "ResultMessage":
                    continue
                if not interrupt_sent:
                    interrupt_sent = True
                    try:
                        await client.interrupt()
                        soft_interrupted = True
                    except Exception:
                        interrupt_sent = False
                        logger.exception("Failed to interrupt Claude session for live feedback")
        finally:
            if not interrupt_watcher.done():
                interrupt_watcher.cancel()
            await asyncio.gather(interrupt_watcher, return_exceptions=True)

        if soft_interrupted and pending_feedback is None:
            pending_feedback = self._drain_user_message()

        return _ClaudeResponseOutcome(
            feedback=pending_feedback,
            hit_turn_limit=hit_turn_limit and pending_feedback is None,
            interrupted=soft_interrupted and pending_feedback is None,
        )

    def _drain_user_message(self) -> str | None:
        """Return the most recent queued user message, or None."""
        msg: str | None = None
        while True:
            try:
                candidate = self._prompt_queue.get_nowait()
            except queue.Empty:
                break
            if candidate is None:
                self._active = False
                return None
            msg = candidate  # keep the latest
        return msg

    async def _async_wait_for_user_message(self, timeout: float) -> str | None:
        """Async wait up to *timeout* seconds for a user message."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while self._active:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            # Use run_in_executor to do a blocking queue.get without
            # blocking the event loop.
            try:
                msg = await asyncio.get_event_loop().run_in_executor(
                    None, self._prompt_queue.get, True, min(remaining, 1.0)
                )
            except queue.Empty:
                continue
            if msg is None:
                self._active = False
                return None
            return msg
        return None

    async def _drive_codex(self) -> None:
        try:
            try:
                from openai_codex import (
                    AppServerConfig,
                    AsyncCodex,
                    ApprovalMode,
                    SkillInput,
                    TextInput,
                )
                from openai_codex.generated.v2_all import (
                    DangerFullAccessSandboxPolicy,
                    ReasoningEffort,
                    SandboxPolicy,
                )
                from openai_codex.types import SandboxMode
            except ImportError as exc:
                raise RuntimeError(
                    "Codex Agent runtime requires the `openai-codex` Python SDK. "
                    "Run `uv sync` after updating dependencies, then start the backend again."
                ) from exc

            _install_codex_jsonrpc_noise_filter()

            effort_value = str(
                self._agent_config.get("reasoning_effort")
                or _codex_default_reasoning_effort()
                or "high"
            )
            try:
                effort = ReasoningEffort(effort_value)
            except ValueError:
                effort = ReasoningEffort.high
            model = self._agent_config.get("model") or _codex_default_model() or None
            self._usage_totals.model = model or "codex-default"
            self._emit(_agent_usage_progress_event(self._usage_totals))
            skill_path = str(
                (self._project_dir / "skills" / _AGENT_SKILL_NAME).resolve()
            )
            codex_config = AppServerConfig(
                cwd=str(self._project_dir),
                env=_agent_runtime_env(
                    self._project_dir,
                    self._agent_config.get("_research_env"),
                ),
                codex_bin=str(settings.codex_bin) if settings.codex_bin else None,
            )
            sandbox_policy = SandboxPolicy(
                root=DangerFullAccessSandboxPolicy(type="dangerFullAccess")
            )
            self._emit(
                AgentProgressEvent(
                    "agent",
                    "progress",
                    "Codex app-server is ready; starting Agent thread...",
                    0.12,
                    data={"runtime": "codex", "model": model, "reasoning_effort": effort.value},
                )
            )
            async with AsyncCodex(config=codex_config) as codex:
                thread_kwargs = {
                    "approval_mode": ApprovalMode.auto_review,
                    "cwd": str(self._project_dir),
                    "developer_instructions": _codex_developer_instructions(self._project_dir),
                    "model": model,
                    "sandbox": SandboxMode.danger_full_access,
                    "config": {"model_reasoning_effort": effort.value},
                }
                if self._resume_session_id:
                    thread = await codex.thread_resume(self._resume_session_id, **thread_kwargs)
                    self._resume_session_id = None
                else:
                    thread = await codex.thread_start(**thread_kwargs)
                self._session_id = thread.id
                self._emit(
                    AgentProgressEvent(
                        "agent",
                        "progress",
                        "Codex Agent thread started; waiting for initial prompt...",
                        0.12,
                        data={"runtime": "codex", "thread_id": thread.id},
                    )
                )
                first_turn = True
                while self._active:
                    try:
                        prompt = self._prompt_queue.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    if prompt is None:
                        break
                    if self._cancel_event.is_set():
                        break
                    # Start a new turn.
                    self._emit(
                        AgentProgressEvent(
                            "agent",
                            "progress",
                            "Submitting Codex Agent turn...",
                            0.12,
                            data={"runtime": "codex", "thread_id": thread.id},
                        )
                    )
                    turn_prompt = prompt if first_turn else _live_feedback_prompt(prompt)
                    first_turn = False
                    turn = await thread.turn(
                        [SkillInput(name=_AGENT_SKILL_NAME, path=skill_path), TextInput(turn_prompt)],
                        approval_mode=ApprovalMode.auto_review,
                        cwd=str(self._project_dir),
                        effort=effort,
                        model=model,
                        sandbox_policy=sandbox_policy,
                    )
                    self._emit(
                        AgentProgressEvent(
                            "agent",
                            "progress",
                            "Codex Agent turn submitted; streaming events...",
                            0.12,
                            data={"runtime": "codex", "thread_id": thread.id},
                        )
                    )
                    self._accepting_feedback = True
                    turn_interrupted = False
                    async def _interrupt_codex_when_requested() -> None:
                        nonlocal turn_interrupted
                        while self._active or self._cancel_event.is_set():
                            if self._cancel_event.is_set():
                                await turn.interrupt()
                                return
                            if not self._interrupt_event.is_set():
                                await asyncio.sleep(_AGENT_INTERRUPT_POLL_SECONDS)
                                continue
                            self._interrupt_event.clear()
                            await turn.interrupt()
                            turn_interrupted = True
                            self._emit(_agent_paused_event())
                            return

                    interrupt_watcher = asyncio.create_task(_interrupt_codex_when_requested())
                    try:
                        async for event in _iter_agent_stream_with_timeout(
                            turn.stream(),
                            _agent_turn_idle_timeout(),
                        ):
                            if self._cancel_event.is_set():
                                await turn.interrupt()
                                break
                            self._emit(event)
                            queued_feedback = self._drain_user_message()
                            if queued_feedback is not None:
                                await turn.steer(TextInput(_live_feedback_prompt(queued_feedback)))
                    except TimeoutError:
                        try:
                            await turn.interrupt()
                        except Exception:
                            logger.exception("Failed to interrupt timed-out Codex turn")
                        raise
                    finally:
                        self._accepting_feedback = False
                        if not interrupt_watcher.done():
                            interrupt_watcher.cancel()
                        await asyncio.gather(interrupt_watcher, return_exceptions=True)
                    if turn_interrupted:
                        self._accepting_feedback = True
                        continue
                    # A finished Codex turn completes generation. Feedback sent
                    # after export resumes this saved thread in a new run.
                    self._active = False
                    break
        except BaseException as exc:
            self._emit(exc)
        finally:
            self._accepting_feedback = False
            self._emit(_SESSION_DONE)

    # -- Helpers -----------------------------------------------------------

    def _build_claude_options(self, max_turns: int | None = None) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        agent_config = self._agent_config
        allowed_tools = [
            "Read", "Write", "Edit", "MultiEdit", "Grep", "Glob",
            "LS", "Bash", "TodoWrite", "Task", "Agent",
        ]
        session_opts: dict[str, Any] = {}
        if self._resume_session_id:
            session_opts["resume"] = self._resume_session_id
            # Only use resume for the first query; subsequent queries
            # are in the same client session.
            self._resume_session_id = None
        effective_max_turns = max_turns if max_turns is not None else (
            int(agent_config["max_turns"])
            if agent_config.get("max_turns")
            else None
        )
        return ClaudeAgentOptions(
            tools={"type": "preset", "preset": "claude_code"},
            system_prompt={"type": "preset", "preset": "claude_code"},
            cwd=str(self._project_dir),
            add_dirs=[
                str(self._project_dir),
                str(settings.icons_dir),
                str(settings.templates_dir),
            ],
            allowed_tools=allowed_tools,
            disallowed_tools=["Bash(rm *)", "Bash(del *)", "Bash(Remove-Item *)"],
            permission_mode="bypassPermissions",
            include_partial_messages=False,
            max_buffer_size=16 * 1024 * 1024,
            max_turns=effective_max_turns,
            setting_sources=(
                ["user", "project", "local"]
                if agent_config.get("load_project_settings", True)
                else ["user"]
            ),
            model=agent_config.get("model") or None,
            skills=[_AGENT_SKILL_NAME, _RESEARCH_SKILL_NAME, _DEEP_RESEARCH_SKILL_NAME],
            env=_agent_runtime_env(
                self._project_dir,
                agent_config.get("_research_env"),
                agent_config,
            ),
            hooks=_agent_research_gate_hooks(self._project_dir),
            **session_opts,
        )

    def _capture_session_id(self, message: Any) -> None:
        kind = message.__class__.__name__
        if kind == "SystemMessage":
            subtype = getattr(message, "subtype", None)
            if subtype == "init":
                data = getattr(message, "data", None) or {}
                sid = data.get("session_id")
                if isinstance(sid, str) and sid:
                    self._session_id = sid


def _is_false_success_error(exc: BaseException) -> bool:
    """Detect the benign ``[WinError 6]`` that the Claude CLI raises on
    Windows when the subprocess handle is closed after a successful run."""
    if isinstance(exc, OSError) and getattr(exc, "winerror", None) == 6:
        return True
    return False


def _agent_turn_idle_timeout() -> float | None:
    timeout = float(getattr(settings, "agent_turn_idle_timeout", 300) or 0)
    return timeout if timeout > 0 else None


async def _next_agent_stream_item(iterator: Any, timeout: float | None) -> Any:
    if timeout is None:
        return await iterator.__anext__()
    return await asyncio.wait_for(iterator.__anext__(), timeout=timeout)


async def _iter_agent_stream_with_timeout(stream: Any, timeout: float | None):
    iterator = stream.__aiter__()
    while True:
        try:
            yield await _next_agent_stream_item(iterator, timeout)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Agent turn produced no activity for {timeout}s."
            ) from None


def _is_claude_turn_limit_result(message: Any) -> bool:
    if message.__class__.__name__ != "ResultMessage":
        return False
    if not bool(getattr(message, "is_error", False)):
        return False
    subtype = str(getattr(message, "subtype", "") or "").lower()
    result = str(getattr(message, "result", "") or "").lower()
    return subtype == "error_max_turns" or "maximum number of turns" in result


def _agent_sdk_events_from_message(message: Any) -> list[dict[str, Any]]:
    events = _events_from_sdk_message(message)
    if not _is_claude_turn_limit_result(message):
        return events
    for event in events:
        if event.get("type") != "result":
            continue
        event["status"] = "running"
        event["message"] = "Agent reached the current turn limit; continuing..."
        data = event.get("data")
        if isinstance(data, dict):
            data["recoverable"] = True
    return events


def _is_feedback_interrupt_result(message: Any) -> bool:
    """Return True for the expected ResultMessage emitted after live feedback
    interrupts a running Claude Code turn."""
    if message.__class__.__name__ != "ResultMessage":
        return False
    subtype = str(getattr(message, "subtype", "") or "").lower()
    if subtype == "error_during_execution":
        return True
    result = str(getattr(message, "result", "") or "").lower()
    return "interrupt" in result or "cancel" in result


def _claude_message_allows_live_feedback_interrupt(message: Any) -> bool:
    """Only inject feedback at natural boundaries.

    Tool-use AssistantMessages mean a tool call is just starting; wait for the
    following tool-result UserMessage so feedback lands after the current tool
    batch and before Claude starts the next reasoning step.
    """
    kind = message.__class__.__name__
    if kind == "ResultMessage":
        return True
    if kind == "UserMessage":
        return any(
            block.__class__.__name__ == "ToolResultBlock"
            or hasattr(block, "tool_use_id")
            for block in (getattr(message, "content", []) or [])
        )
    if kind != "AssistantMessage":
        return False
    content = getattr(message, "content", []) or []
    has_tool_use = any(hasattr(block, "name") for block in content)
    has_text = any(str(getattr(block, "text", "") or "").strip() for block in content)
    return has_text and not has_tool_use


def _live_feedback_prompt(feedback: str) -> str:
    return (
        "The user sent live guidance while you were working.\n\n"
        "First, briefly reply to the user that you received it and summarize "
        "how you will apply it. Then continue the PPT generation task from "
        "the current workspace state without restarting completed work.\n\n"
        f"## Live user guidance\n{feedback.strip()}"
    )


# ---------------------------------------------------------------------------
# Session persistence (agent_session.json)
# ---------------------------------------------------------------------------

def _agent_session_path(project_dir: Path) -> Path:
    return project_dir / "agent_session.json"


def _save_agent_session(project_dir: Path, session_id: str) -> None:
    """Persist the SDK session id so post-completion feedback can resume."""
    path = _agent_session_path(project_dir)
    payload = {"session_id": session_id}
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.warning("Failed to persist agent session id to %s", path)


def _load_agent_session_id(project_dir: Path) -> str | None:
    """Load a previously persisted SDK session id for resume."""
    path = _agent_session_path(project_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        sid = data.get("session_id")
        return str(sid) if isinstance(sid, str) and sid else None
    except (OSError, json.JSONDecodeError):
        return None


def _build_agent_contract_repair_prompt(
    project_dir: Path,
    violations: list[str],
) -> str:
    rendered = "\n".join(f"- {item}" for item in violations)
    return f"""The backend rejected the deck's slide-authoring or SVG contract.

Detected violations:
{rendered}

Repair the existing workspace without changing the requested narrative or slide count:
- Read `agent_task.json.agent_policy.slide_authoring_policy`.
- Delete every multi-slide generator program you created.
- Rewrite every affected `svg_output/slide_###.svg` directly, one explicitly named file at a time.
- Do not use a loop, slide list/registry, shared renderer, template expansion, or a program that emits multiple SVGs.
- Keep only deck-level visual tokens shared: palette, typography, margins, and recurring chrome.
- Re-evaluate each slide's claim, evidence, paper figure, layout, whitespace, and Chinese wrapping independently.
- Fix every listed out-of-bounds, card overflow, line collision, or text-capacity violation by changing the layout, container size, line breaks, table columns, or wording. Do not merely shrink all text.
- Remove disallowed SVG constructs such as `<style>`, `class=`, `mask`, `filter`, `clipPath`, `foreignObject`, scripts, event handlers, or remote URLs. Put fill/stroke/font attributes directly on each SVG element instead of using CSS classes.
- Before finishing each slide, verify `text_right <= region_right`, `last_baseline + font_size*0.25 <= region_bottom`, and every table column has enough width for its longest visible cell.
- Preserve exact source numbers and units; remove K/M/B shorthand unless it appears verbatim in the paper.
- Ensure every content slide has a well-designed layout.
- Update `agent_report.json.slide_authoring` to:
  - `mode`: `direct_per_slide`
  - `multi_slide_generator_used`: false
  - `direct_files`: every final `slide_###.svg`
  - `notes`: a concise description of how page-specific compositions were chosen.

Validation and rendering scripts may read all slides, but they must not write or regenerate slide SVGs.
Finish only after the generator files are removed and every final slide is directly authored.
"""


def _write_agent_contract_warnings(project_dir: Path, violations: list[str]) -> None:
    if not violations:
        return
    payload = {
        "status": "exported_with_contract_warnings",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "violations": violations,
    }
    try:
        (project_dir / "agent_contract_warnings.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("Failed to persist Agent contract warnings for %s", project_dir)


def _agent_generation_contract_violations(
    project_dir: Path,
    svg_files: list[Path],
) -> list[str]:
    preflight_violations: list[str] = []
    try:
        _require_agent_report(project_dir)
    except RuntimeError as exc:
        preflight_violations.append(str(exc))
    authorship_violations = _agent_slide_authorship_violations(
        project_dir,
        svg_files=svg_files,
    )
    if preflight_violations:
        authorship_violations = [
            violation
            for violation in authorship_violations
            if violation != "agent_report.json is missing slide_authoring evidence"
        ]
    return [
        *preflight_violations,
        *authorship_violations,
        *_agent_layout_contract_violations(svg_files),
    ]


async def _repair_agent_generation_contracts_if_needed(
    project_dir: Path,
    runtime: str,
    agent_config: dict[str, Any],
) -> Any:
    svg_files = get_svg_files(project_dir, source="output")
    violations = _agent_generation_contract_violations(project_dir, svg_files)
    if not violations:
        return

    yield AgentProgressEvent(
        "generation",
        "progress",
        "Agent generation contract failed; requesting a direct per-slide repair...",
        0.68,
        data={
            "generation_mode": "agent",
            "contract": "direct_per_slide_authoring",
            "violations": violations,
        },
    )
    repair_session = PersistentAgentSession(
        project_dir,
        runtime,
        agent_config,
        resume_session_id=_load_agent_session_id(project_dir),
    )
    repair_session.set_bridge_mode()
    try:
        await repair_session.start(
            _build_agent_contract_repair_prompt(project_dir, violations)
        )
        async for message in repair_session:
            if isinstance(message, AgentProgressEvent):
                yield message
                continue
            usage_event = _update_agent_usage_totals(repair_session._usage_totals, message)
            if usage_event is not None:
                yield usage_event
            if runtime == "codex":
                item_event = _codex_item_progress_event(
                    message,
                    str(getattr(message, "method", "") or ""),
                )
                if item_event is not None:
                    yield item_event
            else:
                for sdk_event in _agent_sdk_events_from_message(message):
                    text = str(sdk_event.get("message") or "").strip()
                    if text:
                        yield AgentProgressEvent(
                            "agent",
                            "progress",
                            text,
                            0.68,
                            data={
                                "runtime": "claude_code",
                                "agent_event": sdk_event,
                            },
                        )
                if message.__class__.__name__ == "ResultMessage" and bool(
                    getattr(message, "is_error", False)
                ):
                    if _is_claude_turn_limit_result(message):
                        continue
                    result = str(getattr(message, "result", "") or "Agent repair failed.")
                    raise RuntimeError(result)
        if repair_session.session_id:
            _save_agent_session(project_dir, repair_session.session_id)
    finally:
        await repair_session.close()

    repaired_svg_files = get_svg_files(project_dir, source="output")
    remaining_authoring = _agent_slide_authorship_violations(
        project_dir,
        svg_files=repaired_svg_files,
    )
    if remaining_authoring:
        rendered = "\n- ".join(remaining_authoring)
        raise RuntimeError(
            "Agent still violated the direct-authoring contract after "
            f"one repair turn:\n- {rendered}"
        )
    remaining_layout = _agent_layout_contract_violations(repaired_svg_files)
    if remaining_layout:
        _write_agent_contract_warnings(project_dir, remaining_layout)
        yield AgentProgressEvent(
            "generation",
            "progress",
            (
                "Agent still has SVG/layout contract warnings after repair; "
                "continuing export with warnings recorded."
            ),
            0.72,
            data={
                "generation_mode": "agent",
                "contract": "svg_layout_quality",
                "warnings": remaining_layout,
                "export_will_continue": True,
            },
        )
        return
    yield AgentProgressEvent(
        "generation",
        "progress",
            "Agent repaired direct authoring and SVG contract violations.",
        0.72,
        data={
            "generation_mode": "agent",
            "contract": "direct_per_slide_authoring",
            "repaired": True,
        },
    )


async def run_agent_pipeline(request: Any):
    """Run the Agent-mode generation path.

    The selected runtime owns paper understanding, research, planning, and SVG
    authoring.  The backend owns workspace setup, artifact validation, live
    preview events, finalization, and PPTX export.
    """

    from backend.generator.template_manager import (
        copy_template_assets_for_project,
        load_template,
    )

    agent_config = dict(getattr(request, "agent_config", None) or {})
    agent_config["_research_env"] = _agent_research_env_from_config(
        getattr(request, "research_config", None)
    )
    runtime = str(agent_config.get("runtime") or _RUNTIME_DEFAULT)
    project_dir = init_project(
        name="paper_ppt_agent",
        canvas_format=request.canvas_format,
        base_dir=settings.workspaces_dir,
    )
    yield AgentProgressEvent(
        "agent",
        "started",
        "Preparing Agent workspace...",
        0.02,
        data={"project_dir": str(project_dir), "runtime": runtime},
    )

    await _prepare_agent_workspace(project_dir, request, agent_config)

    if getattr(request, "template_id", None):
        tmpl = load_template(request.template_id)
        if tmpl:
            await aoffload(_copy_template_reference_for_agent, tmpl, project_dir)
            await aoffload(copy_template_assets_for_project, tmpl, project_dir)
            yield AgentProgressEvent(
                "agent",
                "progress",
                f"Template '{request.template_id}' copied into Agent workspace.",
                0.05,
                data={"template_id": request.template_id},
            )

    total_slides_hint = int(getattr(request, "num_pages", None) or 0)
    yield AgentProgressEvent(
        "generation",
        "started",
        "Agent is generating the deck...",
        0.10,
        data={
            "total_slides": total_slides_hint,
            "generation_mode": "agent",
            "runtime": runtime,
        },
    )

    queue: asyncio.Queue[AgentProgressEvent] = asyncio.Queue()
    stop_scan = asyncio.Event()
    seen_hashes: dict[Path, str] = {}
    completed_pages: set[int] = set()

    async def _scanner() -> None:
        while not stop_scan.is_set():
            await _scan_svg_updates(
                project_dir,
                queue,
                seen_hashes,
                completed_pages,
                total_slides_hint=total_slides_hint,
            )
            try:
                await asyncio.wait_for(stop_scan.wait(), timeout=_SVG_SCAN_SECONDS)
            except asyncio.TimeoutError:
                continue
        await _scan_svg_updates(
            project_dir,
            queue,
            seen_hashes,
            completed_pages,
            total_slides_hint=total_slides_hint,
        )

    # -- Persistent session (replaces one-shot runner_task) --
    job_id = str(getattr(request, "job_id", None) or "")
    resume_session_id = _load_agent_session_id(project_dir)
    session = PersistentAgentSession(
        project_dir,
        runtime,
        agent_config,
        resume_session_id=resume_session_id,
        job_id=job_id,
    )
    if job_id:
        from backend.orchestrator.agent_session_registry import register as registry_register
        registry_register(job_id, session)

    async def _cleanup_live_session() -> None:
        if job_id:
            from backend.orchestrator.agent_session_registry import unregister as registry_unregister
            registry_unregister(job_id)
        await session.close()

    prompt = _build_runtime_prompt(project_dir, request, agent_config)

    # Phase 1: Bridge mode — session emits to bridge queue, drainer
    # converts messages to AgentProgressEvent and pushes to event queue.
    session.set_bridge_mode()

    async def _session_drainer() -> None:
        await session.start(prompt)
        async for message in session:
            if isinstance(message, AgentProgressEvent):
                await queue.put(message)
                continue
            usage_event = _update_agent_usage_totals(session._usage_totals, message)
            if usage_event is not None:
                await queue.put(usage_event)
            if runtime == "codex":
                item_event = _codex_item_progress_event(message, str(getattr(message, "method", "") or ""))
                if item_event is not None:
                    await queue.put(item_event)
            else:
                for sdk_event in _agent_sdk_events_from_message(message):
                    msg = str(sdk_event.get("message") or "").strip()
                    if msg:
                        await queue.put(
                            AgentProgressEvent(
                                "agent",
                                "progress",
                                msg,
                                0.12,
                                data={"runtime": "claude_code", "agent_event": sdk_event},
                            )
                        )
                if message.__class__.__name__ == "ResultMessage" and bool(
                    getattr(message, "is_error", False)
                ):
                    if _is_claude_turn_limit_result(message):
                        continue
                    result = str(getattr(message, "result", "") or "Agent failed.")
                    raise RuntimeError(result)
        # Drainer finished — persist session_id and switch to direct mode
        # so feedback turns broadcast directly to WebSocket subscribers.
        if session.session_id:
            _save_agent_session(project_dir, session.session_id)
        session.set_direct_mode()

    scanner_task = asyncio.create_task(_scanner(), name="agent-svg-scanner")
    drainer_task = asyncio.create_task(_session_drainer(), name=f"agent-session-{runtime}")

    # Yield events from the drainer until it finishes the first response.
    last_activity = asyncio.get_running_loop().time()
    waiting_notified = False
    try:
        while True:
            if drainer_task.done() and queue.empty():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if (
                    not waiting_notified
                    and asyncio.get_running_loop().time() - last_activity >= _AGENT_IDLE_NOTICE_SECONDS
                ):
                    waiting_notified = True
                    yield _agent_waiting_event()
                continue
            last_activity = asyncio.get_running_loop().time()
            waiting_notified = False
            yield event
    except asyncio.CancelledError:
        await _cleanup_live_session()
        drainer_task.cancel()
        raise
    finally:
        stop_scan.set()
        scanner_task.cancel()
        await asyncio.gather(scanner_task, return_exceptions=True)

    try:
        await drainer_task

        # Session is now in direct mode — feedback turns broadcast via
        # session_manager.record_event directly.  Proceed to finalization.
        async for repair_event in _repair_agent_generation_contracts_if_needed(
            project_dir,
            runtime,
            agent_config,
        ):
            yield repair_event

        await _scan_svg_updates(
            project_dir,
            queue,
            seen_hashes,
            completed_pages,
            total_slides_hint=total_slides_hint,
        )
        while not queue.empty():
            yield queue.get_nowait()

        svg_files = get_svg_files(project_dir, source="output")
        _validate_agent_artifacts(project_dir, svg_files, total_slides_hint)
        generated = len(svg_files)
        yield AgentProgressEvent(
            "generation",
            "complete",
            f"Agent generated {generated} slides.",
            0.75,
            data={"total_slides": generated, "generation_mode": "agent"},
        )

        yield AgentProgressEvent("postprocess", "started", "Finalizing SVGs...", 0.78)
        async with heavy_stage_slot():
            stats = await aoffload(finalize_project, project_dir)
        yield AgentProgressEvent(
            "postprocess",
            "complete",
            f"Processed {stats.get('total_files', 0)} files",
            0.88,
        )

        yield AgentProgressEvent("export", "started", "Exporting to PowerPoint...", 0.90)
        final_svg_files = get_svg_files(project_dir, source="final")
        notes = get_notes(project_dir, final_svg_files)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        pptx_path = project_dir / "exports" / f"presentation_{timestamp}.pptx"
        pptx_path.parent.mkdir(parents=True, exist_ok=True)
        async with heavy_stage_slot():
            await aoffload(
                create_pptx,
                final_svg_files,
                pptx_path,
                canvas_format=request.canvas_format,
                notes=notes,
            )

        yield AgentProgressEvent(
            "export",
            "complete",
            "PowerPoint generated!",
            1.0,
            data={"output_path": str(pptx_path)},
        )
    finally:
        await _cleanup_live_session()


async def run_agent_feedback_pipeline(
    *,
    project_dir: Path,
    feedback: str,
    runtime: str,
    total_slides_hint: int = 0,
    session_id: str | None = None,
    job_id: str = "",
) -> Any:
    """Continue an Agent-mode job in the same workspace using user feedback."""

    yield AgentProgressEvent(
        "agent",
        "started",
        "Agent is applying feedback...",
        0.05,
        data={"project_dir": str(project_dir), "runtime": runtime},
    )

    queue: asyncio.Queue[AgentProgressEvent] = asyncio.Queue()
    stop_scan = asyncio.Event()
    seen_hashes: dict[Path, str] = {}
    completed_pages: set[int] = set()
    total_hint = max(total_slides_hint, len(get_svg_files(project_dir, source="output")))

    async def _scanner() -> None:
        while not stop_scan.is_set():
            await _scan_svg_updates(
                project_dir,
                queue,
                seen_hashes,
                completed_pages,
                total_slides_hint=total_hint,
            )
            try:
                await asyncio.wait_for(stop_scan.wait(), timeout=_SVG_SCAN_SECONDS)
            except asyncio.TimeoutError:
                continue
        await _scan_svg_updates(
            project_dir,
            queue,
            seen_hashes,
            completed_pages,
            total_slides_hint=total_hint,
        )

    agent_config = _agent_config_from_existing_task(project_dir, runtime)
    # Use persistent session with resume for context continuity.
    effective_session_id = session_id or _load_agent_session_id(project_dir)
    session = PersistentAgentSession(
        project_dir,
        runtime,
        agent_config,
        resume_session_id=effective_session_id,
    )
    if job_id:
        from backend.orchestrator.agent_session_registry import register as registry_register
        registry_register(job_id, session)

    usage_totals = _AgentUsageTotals(runtime=runtime)
    prompt = _build_feedback_runtime_prompt(project_dir, feedback)

    async def _session_drainer() -> None:
        await session.start(prompt)
        async for message in session:
            if isinstance(message, AgentProgressEvent):
                await queue.put(message)
                continue
            usage_event = _update_agent_usage_totals(usage_totals, message)
            if usage_event is not None:
                await queue.put(usage_event)
            if runtime == "codex":
                item_event = _codex_item_progress_event(message, str(getattr(message, "method", "") or ""))
                if item_event is not None:
                    await queue.put(item_event)
            else:
                for sdk_event in _agent_sdk_events_from_message(message):
                    msg = str(sdk_event.get("message") or "").strip()
                    if msg:
                        await queue.put(
                            AgentProgressEvent(
                                "agent",
                                "progress",
                                msg,
                                0.12,
                                data={"runtime": "claude_code", "agent_event": sdk_event},
                            )
                        )
                if message.__class__.__name__ == "ResultMessage" and bool(
                    getattr(message, "is_error", False)
                ):
                    if _is_claude_turn_limit_result(message):
                        continue
                    result = str(getattr(message, "result", "") or "Agent failed.")
                    raise RuntimeError(result)
        if session.session_id:
            _save_agent_session(project_dir, session.session_id)

    scanner_task = asyncio.create_task(_scanner(), name="agent-feedback-svg-scanner")
    drainer_task = asyncio.create_task(_session_drainer(), name=f"agent-feedback-{runtime}")

    last_activity = asyncio.get_running_loop().time()
    waiting_notified = False
    try:
        while True:
            if drainer_task.done() and queue.empty():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if (
                    not waiting_notified
                    and asyncio.get_running_loop().time() - last_activity >= _AGENT_IDLE_NOTICE_SECONDS
                ):
                    waiting_notified = True
                    yield _agent_waiting_event()
                continue
            last_activity = asyncio.get_running_loop().time()
            waiting_notified = False
            yield event
        await drainer_task
    except asyncio.CancelledError:
        drainer_task.cancel()
        raise
    finally:
        stop_scan.set()
        scanner_task.cancel()
        await asyncio.gather(scanner_task, return_exceptions=True)
        if job_id:
            from backend.orchestrator.agent_session_registry import unregister as registry_unregister
            registry_unregister(job_id)
        await session.close()

    await _scan_svg_updates(
        project_dir,
        queue,
        seen_hashes,
        completed_pages,
        total_slides_hint=total_hint,
    )
    while not queue.empty():
        yield queue.get_nowait()

    async for repair_event in _repair_agent_generation_contracts_if_needed(
        project_dir,
        runtime,
        agent_config,
    ):
        yield repair_event

    svg_files = get_svg_files(project_dir, source="output")
    _validate_agent_artifacts(project_dir, svg_files, total_hint)
    yield AgentProgressEvent(
        "generation",
        "complete",
        f"Agent updated {len(svg_files)} slides.",
        0.75,
        data={"total_slides": len(svg_files), "generation_mode": "agent"},
    )

    yield AgentProgressEvent("postprocess", "started", "Finalizing updated SVGs...", 0.78)
    async with heavy_stage_slot():
        stats = await aoffload(finalize_project, project_dir)
    yield AgentProgressEvent(
        "postprocess",
        "complete",
        f"Processed {stats.get('total_files', 0)} files",
        0.88,
    )

    yield AgentProgressEvent("export", "started", "Exporting updated PowerPoint...", 0.90)
    final_svg_files = get_svg_files(project_dir, source="final")
    notes = get_notes(project_dir, final_svg_files)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pptx_path = project_dir / "exports" / f"presentation_agent_feedback_{timestamp}.pptx"
    pptx_path.parent.mkdir(parents=True, exist_ok=True)
    async with heavy_stage_slot():
        await aoffload(
            create_pptx,
            final_svg_files,
            pptx_path,
            canvas_format=_canvas_format_from_task(project_dir),
            notes=notes,
        )

    yield AgentProgressEvent(
        "export",
        "complete",
        "Updated PowerPoint generated!",
        1.0,
        data={"output_path": str(pptx_path)},
    )


async def _prepare_agent_workspace(project_dir: Path, request: Any, agent_config: dict[str, Any]) -> None:
    sources_dir = project_dir / "sources"
    await aoffload(sources_dir.mkdir, parents=True, exist_ok=True)
    await aoffload((project_dir / "agent_feedback").mkdir, parents=True, exist_ok=True)
    source_target = sources_dir / ("paper.pdf" if request.source_type == "pdf" else "latex_source")
    input_path = Path(request.file_path)
    source_target = await aoffload(
        _copy_agent_source_input,
        input_path,
        source_target,
        sources_dir,
        str(request.source_type),
    )

    await aoffload(_copy_agent_skill, project_dir)
    task = _build_agent_task(project_dir, source_target, request, agent_config)
    await awrite_text(
        project_dir / "agent_task.json",
        json.dumps(task, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    await _prepare_agent_source_assets(project_dir)
    await awrite_text(project_dir / "AGENT_README.md", _agent_readme(), encoding="utf-8")


def _copy_agent_source_input(
    input_path: Path,
    source_target: Path,
    sources_dir: Path,
    source_type: str,
) -> Path:
    if input_path.is_dir():
        if source_target.exists():
            shutil.rmtree(source_target)
        shutil.copytree(input_path, source_target)
        return source_target
    if source_type == "latex":
        source_target = sources_dir / input_path.name
    shutil.copy2(input_path, source_target)
    return source_target


async def _prepare_agent_source_assets(project_dir: Path) -> None:
    """Pre-build the same paper asset pack the Agent skill can regenerate.

    Agent mode remains model-runtime owned, but this gives every runtime a
    deterministic figure manifest before the first prompt so non-multimodal
    agents can reason about figures by caption/location instead of by a folder
    full of anonymous image files.
    """

    script = project_dir / "skills" / _AGENT_SKILL_NAME / "scripts" / "extract_paper_assets.py"
    assets_dir = project_dir / "source_assets"
    if not script.exists():
        return
    python_path = _agent_python_path()
    command = [
        str(python_path),
        str(script),
        "--task",
        "agent_task.json",
        "--out",
        "source_assets",
    ]
    try:
        cp = await arun(
            command,
            timeout=180.0,
            cwd=project_dir,
            env=_agent_runtime_env(project_dir),
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - extraction is advisory
        assets_dir.mkdir(parents=True, exist_ok=True)
        await _write_agent_extraction_error(
            assets_dir,
            {
                "error": str(exc) or exc.__class__.__name__,
                "error_type": exc.__class__.__name__,
                "traceback": traceback.format_exc(),
                "command": command,
                "python_path": str(python_path),
                "script": str(script),
                "cwd": str(project_dir),
                "notes": [
                    "Automatic source asset extraction failed before the Agent runtime.",
                    "The Agent may rerun skills/paper-ppt-generate/scripts/extract_paper_assets.py after inspecting the source.",
                ],
            },
        )
        return

    if cp.returncode != 0:
        assets_dir.mkdir(parents=True, exist_ok=True)
        await _write_agent_extraction_error(
            assets_dir,
            {
                "returncode": cp.returncode,
                "command": command,
                "python_path": str(python_path),
                "script": str(script),
                "cwd": str(project_dir),
                "stdout": cp.stdout[-4000:],
                "stderr": cp.stderr[-4000:],
                "notes": [
                    "Automatic source asset extraction exited non-zero before the Agent runtime.",
                    "The Agent should inspect this error and may rerun the script manually.",
                ],
            },
        )
        return

    (assets_dir / "extraction_error.json").unlink(missing_ok=True)


async def _write_agent_extraction_error(assets_dir: Path, payload: dict[str, Any]) -> None:
    await awrite_text(
        assets_dir / "extraction_error.json",
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _copy_template_reference_for_agent(template: Any, project_dir: Path) -> None:
    """Copy template skeleton files into the Agent workspace.

    ``copy_template_assets_for_project`` only prepares asset files for generated
    SVGs. Agent mode also needs the actual template SVG skeletons and design
    spec so Claude Code/Codex can inspect and preserve the selected template.
    """

    template_dir = getattr(template, "template_dir", None)
    info = getattr(template, "info", None)
    template_id = getattr(info, "template_id", None)
    if not template_dir or not template_id:
        return

    src = Path(template_dir)
    if not src.is_dir():
        return

    dest = project_dir / "templates" / str(template_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)

    metadata = {
        "template_id": template_id,
        "label": getattr(info, "label", "") or template_id,
        "source": getattr(info, "source", ""),
        "import_mode": getattr(info, "import_mode", ""),
        "viewbox": getattr(template, "viewbox", ""),
        "content_area": getattr(template, "content_area", {}),
        "files": sorted(path.name for path in dest.iterdir() if path.is_file()),
    }
    (dest / "template_context.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (project_dir / "templates" / "README.md").write_text(
        "Template references are copied here by template_id. Read the selected "
        "template's design_spec.md and page SVG skeletons before drafting slides.\n",
        encoding="utf-8",
    )


def _build_agent_task(project_dir: Path, source_target: Path, request: Any, agent_config: dict[str, Any]) -> dict[str, Any]:
    fmt = CANVAS_FORMATS.get(request.canvas_format, CANVAS_FORMATS["ppt169"])
    research_cfg = getattr(request, "research_config", None)
    research_payload = research_cfg.model_dump(exclude_none=True) if hasattr(research_cfg, "model_dump") else None
    public_research_payload = _public_agent_research_config(research_payload)
    detail_profile = _agent_detail_profile(getattr(request, "detail_level", "normal"), getattr(request, "num_pages", None))
    return {
        "task": "Generate an academic presentation from the uploaded paper using Agent mode.",
        "source": {
            "type": request.source_type,
            "path": _rel(project_dir, source_target),
        },
        "output_contract": {
            "manuscript": "manuscript.md",
            "design_spec": "design_spec.md",
            "slides_dir": "svg_output",
            "slide_filename": "slide_001.svg, slide_002.svg, ...",
            "notes_dir": "notes",
            "report": "agent_report.json",
        },
        "presentation": {
            "canvas_format": request.canvas_format,
            "viewbox": fmt["viewbox"],
            "width": fmt["width"],
            "height": fmt["height"],
            "language": request.language,
            "target_pages": request.num_pages,
            "style": request.style,
            "template_id": request.template_id,
            "user_instruction": request.instruction,
            "style_overrides": request.style_overrides,
            "detail_profile": detail_profile,
        },
        "agent_policy": {
            "user_instruction_policy": {
                "priority": "highest_product_requirement",
                "technical_wrapper": (
                    "The backend exports through SVG/PPTX artifacts, but that technical wrapper "
                    "is an implementation constraint rather than a reason to ignore the user's "
                    "requested visual direction."
                ),
                "conflict_resolution": (
                    "When user wording conflicts with the SVG output format, preserve the user's "
                    "underlying goal and adapt it into valid SVG/PPTX-compatible artifacts. For "
                    "example, requests such as no SVG, image-only, GPT image, or more beautiful "
                    "should become an image-led, polished visual system inside SVG slide wrappers."
                ),
                "status_language": (
                    "Do not tell the user that the output contract overrides their preference. "
                    "Briefly state the adapted plan in user terms."
                ),
            },
            "do_not_call_backend_provider_models": True,
            "icons_optional": True,
            "generate_slides_in_parallel_when_useful": True,
            "allow_external_research": bool(agent_config.get("allow_external_research")),
            "allow_deep_research": bool(agent_config.get("allow_deep_research")),
            "enable_visual_qa": bool(agent_config.get("enable_visual_qa", True)),
            "external_research_skill": _RESEARCH_SKILL_NAME,
            "deep_research_skill": "paper-ppt-deep-research",
            "icon_policy": _agent_icon_policy(),
            "layout_policy": _agent_layout_policy(),
            "slide_authoring_policy": _agent_slide_authoring_policy(),
            "factual_rendering_policy": _agent_factual_rendering_policy(),
            "source_asset_policy": _agent_source_asset_policy(),
            "subagent_policy": _agent_subagent_policy(request, agent_config, detail_profile),
            "research_policy": _agent_research_policy(research_payload),
            "research_gate": {
                "enforced": True,
                "blocks_authoring_until_complete": True,
                "external_required_outputs": [
                    "research/raw_external_results.json",
                    "research/sources.json",
                    "research/brief.md",
                ],
                "deep_required_outputs": [
                    "research/deep/notes_index.json",
                    "research/deep/brief.md",
                ],
            },
        },
        "research_config": public_research_payload,
        "paths": {
            "workspace": str(project_dir),
            "skill": str(project_dir / "skills" / _AGENT_SKILL_NAME / "SKILL.md"),
            "external_research_skill": str(project_dir / "skills" / _RESEARCH_SKILL_NAME / "SKILL.md"),
            "deep_research_skill": str(project_dir / "skills" / "paper-ppt-deep-research" / "SKILL.md"),
            "python": str(_agent_python_path()),
            "icons": _rel(project_dir, settings.icons_dir),
            "templates": _rel(project_dir, project_dir / "templates"),
            "selected_template": f"templates/{request.template_id}" if request.template_id else None,
            "source_assets": "source_assets",
            "source_asset_extractor": f"skills/{_AGENT_SKILL_NAME}/scripts/extract_paper_assets.py",
            "source_paper_markdown": "source_assets/paper.md",
            "source_figures_json": "source_assets/figures.json",
            "source_figures_markdown": "source_assets/figures.md",
            "source_images": "source_assets/images",
            "feedback_dir": "agent_feedback",
            "external_research_script": f"skills/{_RESEARCH_SKILL_NAME}/scripts/search_research.py",
            "deep_research_notes_script": "skills/paper-ppt-deep-research/scripts/compile_deep_notes.py",
            "research_brief": "research/brief.md",
            "research_sources": "research/sources.json",
            "external_research_summary": "research/external_search_summary.json",
            "deep_research_brief": "research/deep/brief.md",
        },
    }


def _agent_runtime_env(
    project_dir: Path,
    research_env: dict[str, str] | None = None,
    agent_config: dict[str, Any] | None = None,
) -> dict[str, str]:
    python_path = _agent_python_path()
    python_dir = str(python_path.parent)
    existing_path = os.environ.get("PATH") or os.environ.get("Path") or ""
    runtime_path = f"{python_dir}{os.pathsep}{existing_path}" if existing_path else python_dir
    tmp_dir = project_dir / ".codex_tmp"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        tmp_dir = Path(os.environ.get("TEMP") or os.environ.get("TMP") or str(project_dir))
    env = {
        "PAPER_PPT_AGENT_MODE": "1",
        "PAPER_PPT_WORKSPACE": str(project_dir),
        "PAPER_PPT_PYTHON": str(python_path),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "TEMP": str(tmp_dir),
        "TMP": str(tmp_dir),
        "TMPDIR": str(tmp_dir),
        "PATH": runtime_path,
        "Path": runtime_path,
    }
    if research_env:
        env.update(
            {
                key: value
                for key, value in research_env.items()
                if isinstance(key, str) and isinstance(value, str) and value.strip()
            }
        )
    if agent_config:
        base_url = str(agent_config.get("base_url") or "").strip()
        api_key = str(agent_config.get("api_key") or "").strip()
        auth_token = str(agent_config.get("auth_token") or "").strip()
        model = str(agent_config.get("model") or "").strip()
        if base_url:
            env["ANTHROPIC_BASE_URL"] = base_url
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
        if auth_token:
            env["ANTHROPIC_AUTH_TOKEN"] = auth_token
            env["CLAUDE_CODE_OAUTH_TOKEN"] = auth_token
        if model:
            env["ANTHROPIC_MODEL"] = model
    return env


def _agent_research_env_from_config(research_config: Any) -> dict[str, str]:
    research = (
        research_config.model_dump(exclude_none=True)
        if hasattr(research_config, "model_dump")
        else research_config
        if isinstance(research_config, dict)
        else {}
    )
    env: dict[str, str] = {}
    key_map = {
        "semantic_scholar_api_key": "SEMANTIC_SCHOLAR_API_KEY",
        "tavily_api_key": "TAVILY_API_KEY",
        "serpapi_key": "SERPAPI_KEY",
    }
    for source_key, env_key in key_map.items():
        value = research.get(source_key)
        if isinstance(value, str) and value.strip():
            env[env_key] = value.strip()
    return env


def _public_agent_research_config(research_payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not research_payload:
        return None
    secret_fields = {
        "semantic_scholar_api_key",
        "tavily_api_key",
        "serpapi_key",
    }
    return {
        key: value
        for key, value in research_payload.items()
        if key not in secret_fields
    }


def _agent_python_path() -> Path:
    local_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if local_python.exists():
        return local_python.resolve()
    return Path(sys.executable).resolve()


def _codex_config_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home) / "config.toml"
    return Path.home() / ".codex" / "config.toml"


def _codex_cli_config() -> dict[str, Any]:
    try:
        with _codex_config_path().open("rb") as handle:
            parsed = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _codex_default_model() -> str | None:
    model = _codex_cli_config().get("model")
    return model.strip() if isinstance(model, str) and model.strip() else None


def _codex_default_reasoning_effort() -> str | None:
    effort = _codex_cli_config().get("model_reasoning_effort")
    if not isinstance(effort, str):
        return None
    effort = effort.strip()
    return effort if effort in {"low", "medium", "high", "xhigh"} else None


def _codex_developer_instructions(project_dir: Path) -> str:
    return (
        "You are running inside a Paper PPT Agent generation workspace. "
        f"The workspace root is {project_dir}. Treat this directory as the only "
        "authoring root for generated files. Read agent_task.json first, follow "
        "the paper-ppt-generate skill, and write artifacts incrementally to "
        "manuscript.md, design_spec.md, svg_output/, notes/, and agent_report.json. "
        "Prefer the configured Python from PAPER_PPT_PYTHON for PDF/TeX/SVG checks. "
        "Use source_assets/paper.md and source_assets/figures.json for paper text "
        "and figure selection. When invoking shell commands on Windows, keep the "
        "working directory in the workspace and avoid long-running interactive commands. "
        "Author every slide SVG directly as its own file. Never create or run a Python, "
        "JavaScript, shell, PowerShell, or other general-purpose program that generates "
        "multiple slide SVGs, and never use a loop or shared renderer to emit the deck. "
        "Shared colors and typography may be documented, but each slide's layout "
        "and SVG markup must be explicitly authored for that slide."
    )


_CODEX_NOISE_FILTER_INSTALLED = False


def _install_codex_jsonrpc_noise_filter() -> None:
    """Ignore known Windows process-kill chatter on Codex app-server stdout.

    The Python SDK expects stdout from `codex app-server --listen stdio://` to
    contain only JSON-RPC lines. On Windows, process-tree cleanup can leak
    taskkill status lines such as "SUCCESS: The process with PID ... has been
    terminated." into stdout, which otherwise kills the reader thread. This
    compatibility shim is intentionally narrow and only skips those lines.
    The SDK also opens stderr as UTF-8 text; some Windows subprocess messages
    can be locale-encoded. stderr is diagnostic only, so decode it with
    replacement instead of letting the drain thread die.
    """

    global _CODEX_NOISE_FILTER_INSTALLED
    if _CODEX_NOISE_FILTER_INSTALLED:
        return
    try:
        from openai_codex import client as codex_client
        from openai_codex.errors import AppServerError, TransportClosedError
    except Exception:
        return

    def _read_message(self) -> dict[str, Any]:
        if self._proc is None or self._proc.stdout is None:
            raise TransportClosedError("app-server is not running")

        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise TransportClosedError(
                    f"app-server closed stdout. stderr_tail={self._stderr_tail()[:2000]}"
                )

            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                if _is_codex_ignorable_stdout_noise(line):
                    try:
                        self._stderr_lines.append(f"ignored stdout noise: {line.rstrip()}")
                    except Exception:
                        pass
                    continue
                raise AppServerError(f"Invalid JSON-RPC line: {line!r}") from exc

            if not isinstance(message, dict):
                raise AppServerError(f"Invalid JSON-RPC payload: {message!r}")
            return message

    def _start_stderr_drain_thread(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return

        def _drain() -> None:
            stderr = self._proc.stderr
            if stderr is None:
                return
            raw = getattr(stderr, "buffer", None)
            if raw is not None:
                for raw_line in raw:
                    try:
                        line = raw_line.decode("utf-8", errors="replace")
                    except Exception:
                        line = repr(raw_line)
                    self._stderr_lines.append(line.rstrip("\n"))
                return
            while True:
                try:
                    line = stderr.readline()
                except UnicodeDecodeError as exc:
                    self._stderr_lines.append(f"ignored undecodable stderr: {exc}")
                    continue
                if not line:
                    break
                self._stderr_lines.append(line.rstrip("\n"))

        self._stderr_thread = threading.Thread(target=_drain, daemon=True)
        self._stderr_thread.start()

    codex_client.AppServerClient._read_message = _read_message
    codex_client.AppServerClient._start_stderr_drain_thread = _start_stderr_drain_thread
    _CODEX_NOISE_FILTER_INSTALLED = True


def _is_codex_ignorable_stdout_noise(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    return (
        stripped.startswith("SUCCESS: The process with PID ")
        and " has been terminated." in stripped
    )


def _codex_usage_progress_event(
    *,
    model_label: str,
    usage: Any,
    status: str = "running",
) -> AgentProgressEvent:
    total = _event_attr(usage, ("payload", "token_usage", "total")) if usage is not None else None
    context_window = _event_attr(usage, ("payload", "token_usage", "model_context_window")) if usage is not None else None
    data = {
        "model": model_label,
        "input_tokens": _safe_int(_event_attr(total, ("input_tokens",))),
        "output_tokens": _safe_int(_event_attr(total, ("output_tokens",))),
        "cache_read_input_tokens": _safe_int(_event_attr(total, ("cached_input_tokens",))),
        "cache_creation_input_tokens": 0,
        "reasoning_output_tokens": _safe_int(_event_attr(total, ("reasoning_output_tokens",))),
        "total_tokens": _safe_int(_event_attr(total, ("total_tokens",))),
        "model_context_window": _safe_int(context_window) if context_window is not None else None,
    }
    return AgentProgressEvent(
        "agent",
        "progress",
        "Usage update",
        0.12,
        data={
            "runtime": "codex",
            "agent_event": {
                "type": "usage",
                "stage": "agent",
                "status": status,
                "message": "Usage update",
                "data": data,
            },
        },
    )


def _codex_token_usage_values(message: Any) -> dict[str, int] | None:
    if str(getattr(message, "method", "") or "") != "thread/tokenUsage/updated":
        return None
    total = _event_attr(message, ("payload", "token_usage", "total"))
    if total is None:
        return None
    return {
        "input_tokens": _safe_int(_event_attr(total, ("input_tokens",))),
        "output_tokens": _safe_int(_event_attr(total, ("output_tokens",))),
        "cache_read_input_tokens": _safe_int(_event_attr(total, ("cached_input_tokens",))),
        "cache_creation_input_tokens": 0,
        "reasoning_output_tokens": _safe_int(_event_attr(total, ("reasoning_output_tokens",))),
        "total_tokens": _safe_int(_event_attr(total, ("total_tokens",))),
        "model_context_window": _safe_int(
            _event_attr(message, ("payload", "token_usage", "model_context_window"))
        ),
    }


def _agent_usage_progress_event(totals: _AgentUsageTotals) -> AgentProgressEvent:
    data: dict[str, Any] = {
        "model": totals.model,
        "input_tokens": totals.input_tokens,
        "output_tokens": totals.output_tokens,
        "cache_read_input_tokens": totals.cache_read_input_tokens,
        "cache_creation_input_tokens": totals.cache_creation_input_tokens,
        "total_tokens": (
            totals.input_tokens
            + totals.output_tokens
            + totals.cache_read_input_tokens
            + totals.cache_creation_input_tokens
        ),
        "total_cost_usd": round(totals.total_cost_usd, 6),
        "num_turns": totals.num_turns,
        "duration_ms": totals.duration_ms,
    }
    return AgentProgressEvent(
        "agent",
        "progress",
        "Usage update",
        0.12,
        data={
            "runtime": totals.runtime,
            "agent_event": {
                "type": "usage",
                "stage": "agent",
                "status": "running",
                "message": "Usage update",
                "data": data,
            },
        },
    )


def _agent_paused_event() -> AgentProgressEvent:
    return AgentProgressEvent(
        "agent",
        "progress",
        "Agent paused. Send guidance to continue from the current workspace.",
        0.12,
        data={
            "agent_paused": True,
            "agent_event": {
                "type": "status",
                "status": "paused",
                "data": {"agent_paused": True},
            },
        },
    )


def _agent_waiting_event() -> AgentProgressEvent:
    return AgentProgressEvent(
        "agent",
        "progress",
        _AGENT_IDLE_MESSAGE,
        0.12,
        data={
            "agent_waiting": True,
            "agent_event": {
                "type": "status",
                "status": "waiting",
                "data": {"agent_waiting": True},
            },
        },
    )


def _codex_item_progress_event(event: Any, method: str) -> AgentProgressEvent | None:
    if method == "turn/started":
        return AgentProgressEvent(
            "agent",
            "progress",
            "Codex Agent turn started.",
            0.12,
            data={
                "runtime": "codex",
                "method": method,
                "agent_event": {
                    "type": "status",
                    "status": "running",
                    "data": {},
                },
            },
        )
    if method == "error":
        payload = getattr(event, "payload", None)
        error = getattr(payload, "error", None)
        message = str(getattr(error, "message", "") or "Codex Agent error").strip()
        details = str(getattr(error, "additional_details", "") or "").strip()
        will_retry = bool(getattr(payload, "will_retry", False))
        rendered = message if not details else f"{message} ({details[:500]})"
        return AgentProgressEvent(
            "agent",
            "progress",
            rendered[:2000],
            0.12,
            data={
                "runtime": "codex",
                "method": method,
                "agent_event": {
                    "type": "error",
                    "status": "retrying" if will_retry else "error",
                    "data": {
                        "message": message,
                        "additional_details": details,
                        "will_retry": will_retry,
                    },
                },
            },
        )
    if method not in {"item/started", "item/completed"}:
        return None
    item = _codex_event_item(event)
    if item is None:
        return None
    item_type = str(getattr(item, "type", "") or "")
    is_started = method == "item/started"
    if item_type == "agentMessage":
        if is_started:
            return None
        text = str(getattr(item, "text", "") or "").strip()
        if not text:
            return None
        return AgentProgressEvent(
            "agent",
            "progress",
            text[:2000],
            0.12,
            data={
                "runtime": "codex",
                "method": method,
                "agent_event": {
                    "type": "message",
                    "status": "complete",
                    "data": {},
                },
            },
        )

    tool_name = _codex_item_tool_name(item)
    if not tool_name:
        return None

    tool_use_id = str(getattr(item, "id", "") or "")
    item_status = _enum_value(getattr(item, "status", None))
    status = "running" if is_started else ("error" if item_status in {"failed", "declined"} else "complete")
    message = _codex_item_tool_label(item) if is_started else "Tool result"
    data = {
        "tool": tool_name,
        "tool_use_id": tool_use_id,
        "input": _codex_item_tool_input(item),
        "output": _codex_item_tool_output(item),
        "codex_item_type": item_type,
        "codex_status": item_status,
    }
    return AgentProgressEvent(
        "agent",
        "progress",
        message,
        0.12,
        data={
            "runtime": "codex",
            "method": method,
            "agent_event": {
                "type": "tool",
                "status": status,
                "data": data,
            },
        },
    )


def _codex_event_item(event: Any) -> Any | None:
    for path in (
        ("payload", "item", "root"),
        ("payload", "item"),
        ("params", "item", "root"),
        ("params", "item"),
    ):
        current = event
        try:
            for part in path:
                current = getattr(current, part)
        except AttributeError:
            continue
        return current
    return None


def _codex_item_tool_name(item: Any) -> str:
    item_type = str(getattr(item, "type", "") or "")
    if item_type == "commandExecution":
        return "Bash"
    if item_type == "mcpToolCall":
        server = str(getattr(item, "server", "") or "")
        tool = str(getattr(item, "tool", "") or "")
        return f"{server}:{tool}" if server else tool
    if item_type == "dynamicToolCall":
        namespace = str(getattr(item, "namespace", "") or "")
        tool = str(getattr(item, "tool", "") or "")
        return f"{namespace}:{tool}" if namespace else tool
    return ""


def _codex_item_tool_label(item: Any) -> str:
    item_type = str(getattr(item, "type", "") or "")
    if item_type == "commandExecution":
        command = str(getattr(item, "command", "") or "").strip()
        return f"Bash {command}".strip()
    return _codex_item_tool_name(item) or item_type


def _codex_item_tool_input(item: Any) -> dict[str, Any]:
    item_type = str(getattr(item, "type", "") or "")
    if item_type == "commandExecution":
        return {
            "command": str(getattr(item, "command", "") or ""),
            "cwd": str(getattr(item, "cwd", "") or ""),
        }
    arguments = getattr(item, "arguments", None)
    return {"arguments": arguments} if arguments is not None else {}


def _codex_item_tool_output(item: Any) -> dict[str, Any]:
    item_type = str(getattr(item, "type", "") or "")
    if item_type == "commandExecution":
        return {
            "exit_code": getattr(item, "exit_code", None),
            "duration_ms": getattr(item, "duration_ms", None),
            "aggregated_output": str(getattr(item, "aggregated_output", "") or "")[:2000],
        }
    result = getattr(item, "result", None)
    error = getattr(item, "error", None)
    return {"result": result, "error": error}


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _event_attr(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for part in path:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
    return current


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _claude_usage_values(usage: Any) -> dict[str, int] | None:
    if not isinstance(usage, dict):
        return None
    values = {
        "input_tokens": _safe_int(usage.get("input_tokens")),
        "output_tokens": _safe_int(usage.get("output_tokens")),
        "cache_read_input_tokens": _safe_int(usage.get("cache_read_input_tokens")),
        "cache_creation_input_tokens": _safe_int(usage.get("cache_creation_input_tokens")),
    }
    return values if any(values.values()) else None


def _claude_model_usage_values(
    model_usage: Any,
) -> tuple[str | None, dict[str, int] | None, float]:
    if not isinstance(model_usage, dict):
        return None, None, 0.0
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    selected_model: str | None = None
    selected_model_tokens = -1
    total_cost = 0.0
    for model_name, raw_usage in model_usage.items():
        if not isinstance(raw_usage, dict):
            continue
        values = {
            "input_tokens": _safe_int(raw_usage.get("inputTokens")),
            "output_tokens": _safe_int(raw_usage.get("outputTokens")),
            "cache_read_input_tokens": _safe_int(raw_usage.get("cacheReadInputTokens")),
            "cache_creation_input_tokens": _safe_int(raw_usage.get("cacheCreationInputTokens")),
        }
        model_tokens = sum(values.values())
        if isinstance(model_name, str) and model_tokens > selected_model_tokens:
            selected_model = model_name
            selected_model_tokens = model_tokens
        for key, value in values.items():
            totals[key] += value
        cost = raw_usage.get("costUSD")
        if isinstance(cost, (int, float)):
            total_cost += float(cost)
    return selected_model, (totals if any(totals.values()) else None), total_cost


def _add_claude_usage_values(
    totals: _AgentUsageTotals,
    values: dict[str, int],
    *,
    pending: bool,
) -> bool:
    changed = False
    for key, attr, pending_attr in (
        ("input_tokens", "input_tokens", "pending_input_tokens"),
        ("output_tokens", "output_tokens", "pending_output_tokens"),
        ("cache_read_input_tokens", "cache_read_input_tokens", "pending_cache_read_input_tokens"),
        ("cache_creation_input_tokens", "cache_creation_input_tokens", "pending_cache_creation_input_tokens"),
    ):
        value = _safe_int(values.get(key))
        if value <= 0:
            continue
        setattr(totals, attr, getattr(totals, attr) + value)
        if pending:
            setattr(totals, pending_attr, getattr(totals, pending_attr) + value)
        changed = True
    return changed


def _pending_claude_usage_values(totals: _AgentUsageTotals) -> dict[str, int]:
    return {
        "input_tokens": totals.pending_input_tokens,
        "output_tokens": totals.pending_output_tokens,
        "cache_read_input_tokens": totals.pending_cache_read_input_tokens,
        "cache_creation_input_tokens": totals.pending_cache_creation_input_tokens,
    }


def _current_claude_usage_values(totals: _AgentUsageTotals) -> dict[str, int]:
    return {
        "input_tokens": totals.input_tokens,
        "output_tokens": totals.output_tokens,
        "cache_read_input_tokens": totals.cache_read_input_tokens,
        "cache_creation_input_tokens": totals.cache_creation_input_tokens,
    }


def _reset_pending_claude_usage(totals: _AgentUsageTotals) -> None:
    totals.pending_input_tokens = 0
    totals.pending_output_tokens = 0
    totals.pending_cache_read_input_tokens = 0
    totals.pending_cache_creation_input_tokens = 0


def _usage_values_equal(left: dict[str, int], right: dict[str, int]) -> bool:
    return all(_safe_int(left.get(key)) == _safe_int(right.get(key)) for key in left)


async def _scan_svg_updates(
    project_dir: Path,
    queue: asyncio.Queue[AgentProgressEvent],
    seen_hashes: dict[Path, str],
    completed_pages: set[int],
    *,
    total_slides_hint: int,
) -> None:
    svg_files = get_svg_files(project_dir, source="output")
    for ordinal, svg_path in enumerate(svg_files, 1):
        try:
            digest, preview = await aoffload(_prepare_agent_svg_preview, svg_path, project_dir)
        except OSError:
            continue
        except _TransientSvgPreviewError:
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping invalid Agent SVG preview %s: %s", svg_path, exc)
            continue
        if seen_hashes.get(svg_path) == digest:
            continue
        seen_hashes[svg_path] = digest
        page = _slide_number(svg_path, ordinal)
        completed_pages.add(page)
        total = max(total_slides_hint, len(svg_files), max(completed_pages or {0}))
        progress = 0.15 + (len(completed_pages) / max(total, 1)) * 0.55
        await queue.put(
            AgentProgressEvent(
                "generation",
                "progress",
                f"Agent updated slide {page}",
                min(progress, 0.72),
                data={
                    "page": page,
                    "svg": preview,
                    "completed_count": len(completed_pages),
                    "total_slides": total,
                    "generation_mode": "agent",
                },
            )
        )


def _prepare_agent_svg_preview(svg_path: Path, project_dir: Path) -> tuple[str, str]:
    """Read, repair, validate, and embed one live Agent SVG preview.

    Agents often write files directly into ``svg_output``. If the scanner sees
    a file mid-write, skip this tick quietly and retry on the next scan.
    """
    before = svg_path.stat()
    content = svg_path.read_text(encoding="utf-8")
    after = svg_path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise _TransientSvgPreviewError(f"{svg_path.name} changed while reading")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    preview = _embed_svg_preview(content, project_dir)
    _validate_single_svg(svg_path, preview)
    return digest, preview


def _validate_agent_artifacts(project_dir: Path, svg_files: list[Path], total_slides_hint: int) -> None:
    _validate_agent_research_gate(project_dir)
    if not svg_files:
        raise RuntimeError(_empty_agent_output_message(project_dir))
    if total_slides_hint and len(svg_files) < total_slides_hint:
        raise RuntimeError(
            f"Agent produced {len(svg_files)} slides, expected at least {total_slides_hint}."
        )
    for svg_path in svg_files:
        _validate_single_svg(svg_path, svg_path.read_text(encoding="utf-8"))
    report_path = _require_agent_report(project_dir)
    authorship_violations = _agent_slide_authorship_violations(
        project_dir,
        svg_files=svg_files,
    )
    if authorship_violations:
        rendered = "\n- ".join(authorship_violations)
        raise RuntimeError(
            "Agent generation contract failed. Slides must be directly authored:\n"
            f"- {rendered}"
        )
    layout_violations = _agent_layout_contract_violations(svg_files)
    if layout_violations:
        _write_agent_contract_warnings(project_dir, layout_violations)
        logger.warning(
            "Agent export continues with %d SVG/layout contract warning(s) for %s",
            len(layout_violations),
            project_dir,
        )
    _validate_agent_report(project_dir, report_path)


def _require_agent_report(project_dir: Path) -> Path:
    report_path = project_dir / "agent_report.json"
    if report_path.exists():
        return report_path

    raise RuntimeError(
        "Agent mode requires agent_report.json. "
        "The backend will not infer or synthesize missing Agent artifacts."
    )


def _agent_authored_script_paths(project_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    for path in project_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _AGENT_AUTHORING_SCRIPT_SUFFIXES:
            continue
        try:
            relative = path.relative_to(project_dir)
        except ValueError:
            continue
        if relative.parts and relative.parts[0] in _AGENT_AUTHORING_SCAN_EXCLUDED_ROOTS:
            continue
        candidates.append(path)
    return sorted(candidates)


def _script_writes_multiple_slide_svgs(content: str) -> bool:
    lowered = content.lower()
    if "svg_output" not in lowered and not re.search(r"slide[_-]?\d{1,3}\.svg", lowered):
        return False

    writes_files = bool(
        re.search(
            r"(?:write_text|writebytes|open\s*\([^)]*[\"'][wa][bt]?[\"']|"
            r"writefilesync|writefile|out-file|set-content|add-content|"
            r">\s*[^\r\n]*slide[_-])",
            lowered,
        )
    )
    if not writes_files:
        return False

    explicit_slides = set(re.findall(r"slide[_-]?(\d{1,3})\.svg", lowered))
    dynamic_slide_name = bool(
        re.search(r"slide[_-]?\{[^}]+\}\.svg", lowered)
        or re.search(r"slide[_-]?[\"']?\s*\+", lowered)
        or re.search(r"slide[_-]?%0?\d*d?\.svg", lowered)
    )
    iteration = bool(
        re.search(r"\bfor\b[\s\S]{0,240}\b(?:slides?|makers?|range|enumerate)\b", lowered)
        or re.search(r"\bforeach\b[\s\S]{0,240}\bslide", lowered)
        or re.search(r"\.(?:map|foreach)\s*\(", lowered)
    )
    shared_renderer = bool(
        re.search(
            r"(?:def|function)\s+(?:base_svg|render_slide|make_slide|"
            r"write_outputs|generate_deck|build_slide)",
            lowered,
        )
        or "slides =" in lowered
        or "makers =" in lowered
    )
    contains_svg_markup = "<svg" in lowered or "base_svg" in lowered
    return (
        len(explicit_slides) >= 2
        or dynamic_slide_name
        or (iteration and contains_svg_markup)
        or (shared_renderer and contains_svg_markup)
    )


def _agent_slide_authorship_violations(
    project_dir: Path,
    *,
    svg_files: list[Path] | None = None,
) -> list[str]:
    violations: list[str] = []
    for path in _agent_authored_script_paths(project_dir):
        try:
            if path.stat().st_size > 2_000_000:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _script_writes_multiple_slide_svgs(content):
            violations.append(
                f"multi-slide generator detected: {_rel(project_dir, path)}"
            )

    report_path = project_dir / "agent_report.json"
    if not report_path.exists():
        violations.append("agent_report.json is missing slide_authoring evidence")
        return violations
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        violations.append("agent_report.json cannot prove direct per-slide authoring")
        return violations

    slide_authoring = report.get("slide_authoring") if isinstance(report, dict) else None
    if not isinstance(slide_authoring, dict):
        violations.append("agent_report.json.slide_authoring is missing")
        return violations
    if str(slide_authoring.get("mode") or "").strip() != "direct_per_slide":
        violations.append("slide_authoring.mode must be direct_per_slide")
    if slide_authoring.get("multi_slide_generator_used") is not False:
        violations.append("slide_authoring.multi_slide_generator_used must be false")

    expected = {
        path.name
        for path in (svg_files if svg_files is not None else get_svg_files(project_dir, source="output"))
    }
    direct_files_raw = slide_authoring.get("direct_files")
    direct_files = {
        Path(str(item)).name
        for item in direct_files_raw
        if str(item).strip()
    } if isinstance(direct_files_raw, list) else set()
    missing = sorted(expected - direct_files)
    if missing:
        violations.append(
            "slide_authoring.direct_files does not list final slides: "
            + ", ".join(missing)
        )
    return violations


def _agent_layout_contract_violations(svg_files: list[Path]) -> list[str]:
    from backend.generator.svg_critic import check_svg

    hard_rules = {
        "forbidden_class",
        "forbidden_element",
        "content_area_overflow",
        "out_of_bounds",
        "text_line_overlap",
        "text_overflow_in_container",
    }
    violations: list[str] = []
    for svg_path in svg_files:
        try:
            report = check_svg(svg_path.read_text(encoding="utf-8"))
        except OSError:
            continue
        page_findings = [
            violation
            for violation in report.violations
            if violation.rule in hard_rules
            and (violation.severity == "error" or violation.rule == "forbidden_class")
        ]
        grouped: dict[str, list[Any]] = {}
        for violation in page_findings:
            grouped.setdefault(violation.rule, []).append(violation)
        for rule, findings in grouped.items():
            violation = findings[0]
            count_suffix = f" ({len(findings)} occurrences)" if len(findings) > 1 else ""
            violations.append(
                f"{svg_path.name}: {rule}{count_suffix}: {violation.detail}"
            )
    return violations


def _agent_research_gate_requirements(project_dir: Path) -> list[Path]:
    task = _read_agent_task(project_dir)
    policy = task.get("agent_policy") if isinstance(task.get("agent_policy"), dict) else {}
    gate = policy.get("research_gate") if isinstance(policy.get("research_gate"), dict) else {}
    if not gate.get("enforced"):
        return []
    required: list[str] = []
    if policy.get("allow_external_research"):
        required.extend(gate.get("external_required_outputs") or [])
    if policy.get("allow_deep_research"):
        required.extend(gate.get("deep_required_outputs") or [])
    return [project_dir / str(path) for path in required if str(path).strip()]


def _agent_research_gate_requirements_by_phase(project_dir: Path) -> dict[str, list[Path]]:
    task = _read_agent_task(project_dir)
    policy = task.get("agent_policy") if isinstance(task.get("agent_policy"), dict) else {}
    gate = policy.get("research_gate") if isinstance(policy.get("research_gate"), dict) else {}
    if not gate.get("enforced"):
        return {}
    phases: dict[str, list[Path]] = {}
    if policy.get("allow_external_research"):
        phases["external"] = [
            project_dir / str(path)
            for path in gate.get("external_required_outputs") or []
            if str(path).strip()
        ]
    if policy.get("allow_deep_research"):
        phases["deep"] = [
            project_dir / str(path)
            for path in gate.get("deep_required_outputs") or []
            if str(path).strip()
        ]
    return phases


def _agent_payload_has_recorded_limitation(payload: dict[str, Any]) -> bool:
    for key in ("error", "limitations", "errors"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list) and any(str(item).strip() for item in value):
            return True
        if isinstance(value, dict) and value:
            return True
    status = str(payload.get("status") or "").strip().lower()
    return status in {"partial", "limited", "no_results", "no_relevant_results", "unavailable", "timeout"}


def _agent_research_gate_missing(project_dir: Path) -> list[Path]:
    missing: list[Path] = []
    for path in _agent_research_gate_requirements(project_dir):
        try:
            if not path.exists() or path.stat().st_size <= 0:
                missing.append(path)
                continue
            if path.suffix.lower() == ".json":
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    missing.append(path)
                    continue
                normalized = path.as_posix().lower()
                if normalized.endswith("research/raw_external_results.json"):
                    queries = payload.get("queries")
                    if not isinstance(queries, list) or not any(str(query).strip() for query in queries):
                        missing.append(path)
                elif normalized.endswith("research/sources.json"):
                    sources = payload.get("sources")
                    if not isinstance(sources, list) or (
                        not sources and not _agent_payload_has_recorded_limitation(payload)
                    ):
                        missing.append(path)
                elif normalized.endswith("research/deep/notes_index.json"):
                    if not isinstance(payload.get("notes"), list):
                        missing.append(path)
        except (OSError, json.JSONDecodeError):
            missing.append(path)
    return missing


def _agent_research_gate_phase_summary(project_dir: Path) -> str:
    phases = _agent_research_gate_requirements_by_phase(project_dir)
    missing = set(_agent_research_gate_missing(project_dir))
    parts: list[str] = []
    for phase, paths in phases.items():
        missing_in_phase = [path for path in paths if path in missing]
        if phase == "external":
            label = "External research"
            complete_hint = (
                "complete; do not repeat searches just to increase result counts. "
                "Limited or zero relevant results are valid when documented in research/sources.json."
            )
            missing_hint = (
                "missing; choose paper-informed queries, run paper-ppt-research, then write "
                "research/sources.json and research/brief.md. If sources time out or results are sparse, "
                "record the limitation and move on."
            )
        else:
            label = "Deep research"
            complete_hint = "complete; use its synthesis in the deck plan."
            missing_hint = (
                "missing; run paper-ppt-deep-research next and produce focused notes/index plus "
                "research/deep/brief.md before authoring."
            )
        if missing_in_phase:
            rel_missing = ", ".join(_rel(project_dir, path) for path in missing_in_phase)
            parts.append(f"{label}: {missing_hint} Missing: {rel_missing}.")
        else:
            parts.append(f"{label}: {complete_hint}")
    return " ".join(parts)


def _agent_authored_output_paths(project_dir: Path) -> list[Path]:
    outputs = [
        project_dir / "manuscript.md",
        project_dir / "design_spec.md",
        project_dir / "agent_report.json",
    ]
    for directory, pattern in (
        (project_dir / "svg_output", "*.svg"),
        (project_dir / "notes", "*.md"),
    ):
        if directory.exists():
            outputs.extend(directory.glob(pattern))
    return [path for path in outputs if path.exists()]


def _validate_agent_research_gate(project_dir: Path) -> None:
    requirements = _agent_research_gate_requirements(project_dir)
    if not requirements:
        return
    missing = _agent_research_gate_missing(project_dir)
    if missing:
        relative = ", ".join(_rel(project_dir, path) for path in missing)
        raise RuntimeError(
            "Agent research gate is incomplete. Run the required research skills "
            f"before authoring slides: {relative}."
        )
    gate_time = max(path.stat().st_mtime_ns for path in requirements)
    early_outputs = [
        path for path in _agent_authored_output_paths(project_dir)
        if path.stat().st_mtime_ns < gate_time
    ]
    if early_outputs:
        relative = ", ".join(_rel(project_dir, path) for path in early_outputs)
        raise RuntimeError(
            "Agent authored presentation outputs before required research finished. "
            f"Regenerate these files after research: {relative}."
        )


def _agent_research_gate_block_reason(
    project_dir: Path,
    tool_name: str,
    tool_input: dict[str, Any],
) -> str | None:
    missing = _agent_research_gate_missing(project_dir)
    if not missing:
        return None
    path_text = ""
    if tool_name in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        path_text = str(
            tool_input.get("file_path")
            or tool_input.get("path")
            or tool_input.get("notebook_path")
            or ""
        ).replace("\\", "/").lower()
    elif tool_name == "Bash":
        command = str(tool_input.get("command") or "").replace("\\", "/").lower()
        protected_command = command.replace("research/deep/notes", "")
        gated_tokens = (
            "manuscript.md",
            "design_spec.md",
            "agent_report.json",
        )
        is_gated_command = (
            any(token in protected_command for token in gated_tokens)
            or "svg_output" in protected_command
            or bool(re.search(r"(?:^|[\s\"'/])notes(?:[/\s\"']|$)", protected_command))
        )
        if is_gated_command:
            path_text = "svg_output/"
    if not path_text:
        return None
    is_slide_notes_output = (
        bool(re.search(r"(?:^|/)notes/", path_text))
        and "research/deep/notes/" not in path_text
    )
    is_authoring_output = (
        path_text == "manuscript.md"
        or path_text.endswith("/manuscript.md")
        or path_text == "design_spec.md"
        or path_text.endswith("/design_spec.md")
        or path_text == "agent_report.json"
        or path_text.endswith("/agent_report.json")
        or "svg_output" in path_text
        or is_slide_notes_output
    )
    if not is_authoring_output:
        return None
    required = ", ".join(_rel(project_dir, path) for path in missing)
    phase_summary = _agent_research_gate_phase_summary(project_dir)
    return (
        "Research gate is active: do not write manuscript, design, notes, report, "
        "or slide SVG files yet. Complete the remaining research phase(s) first. "
        f"{phase_summary} Required missing files: {required}."
    )


def _agent_batch_authoring_block_reason(
    tool_name: str,
    tool_input: dict[str, Any],
) -> str | None:
    script_path = ""
    candidate_content = ""
    if tool_name in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        script_path = str(
            tool_input.get("file_path")
            or tool_input.get("path")
            or tool_input.get("notebook_path")
            or ""
        )
        chunks = [
            tool_input.get("content"),
            tool_input.get("new_string"),
            tool_input.get("cell_source"),
        ]
        edits = tool_input.get("edits")
        if isinstance(edits, list):
            chunks.extend(
                item.get("new_string")
                for item in edits
                if isinstance(item, dict)
            )
        candidate_content = "\n".join(
            str(chunk)
            for chunk in chunks
            if chunk is not None
        )
        suffix = Path(script_path).suffix.lower()
        if suffix in _AGENT_AUTHORING_SCRIPT_SUFFIXES and _script_writes_multiple_slide_svgs(
            candidate_content
        ):
            return (
                "Do not create a program that generates multiple slide SVGs. "
                "Write each svg_output/slide_###.svg directly and independently, "
                "designing that slide's layout based on its content."
            )

    if tool_name != "Bash":
        return None
    command = str(tool_input.get("command") or "")
    lowered = command.lower().replace("\\", "/")
    invokes_program = bool(
        re.search(r"\b(?:python|python3|node|deno|ruby|pwsh|powershell|bash|sh)\b", lowered)
    )
    generator_name = bool(
        re.search(r"(?:generate|build|make|render)[_-]?(?:deck|slides?)", lowered)
    )
    shell_loop = bool(re.search(r"\b(?:for|foreach)\b", lowered))
    slide_target = "svg_output" in lowered or bool(
        re.search(r"slide[_-]?(?:\d{1,3}|\{|\$|%)", lowered)
    )
    if slide_target and (shell_loop or (invokes_program and generator_name)):
        return (
            "Do not run a multi-slide generator or loop over slide files. "
            "Author one explicitly named slide SVG at a time with direct file edits."
        )
    return None


def _agent_research_gate_hooks(project_dir: Path) -> dict[str, list[Any]]:
    from claude_agent_sdk import HookMatcher

    async def _enforce_agent_authoring_contracts(
        input_data: dict[str, Any],
        _tool_use_id: str | None,
        _context: Any,
    ) -> dict[str, Any]:
        tool_name = str(input_data.get("tool_name") or "")
        tool_input = input_data.get("tool_input") if isinstance(input_data.get("tool_input"), dict) else {}
        reason = _agent_research_gate_block_reason(
            project_dir,
            tool_name,
            tool_input,
        )
        reason = reason or _agent_batch_authoring_block_reason(tool_name, tool_input)
        if not reason:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return {
        "PreToolUse": [
            HookMatcher(
                matcher="Write|Edit|MultiEdit|NotebookEdit|Bash",
                hooks=[_enforce_agent_authoring_contracts],
            )
        ]
    }


def _validate_agent_report(project_dir: Path, report_path: Path) -> None:
    task = _read_agent_task(project_dir)
    policy = task.get("agent_policy") if isinstance(task.get("agent_policy"), dict) else {}
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("agent_report.json is not valid JSON.") from exc

    if not isinstance(report, dict):
        raise RuntimeError("agent_report.json must contain a JSON object.")

    if policy.get("allow_external_research"):
        sources_path = project_dir / "research" / "sources.json"
        external_used = bool(report.get("external_research_used"))
        has_sources = False
        has_recorded_limitation = False
        if sources_path.exists():
            try:
                sources_payload = json.loads(sources_path.read_text(encoding="utf-8"))
                sources = sources_payload.get("sources")
                has_sources = isinstance(sources, list) and bool(sources)
                has_recorded_limitation = _agent_payload_has_recorded_limitation(sources_payload)
            except json.JSONDecodeError:
                has_recorded_limitation = True
        if has_sources and not external_used:
            raise RuntimeError(
                "External research sources exist, but agent_report.json does not mark external_research_used=true."
            )
        if not external_used and not has_sources and not has_recorded_limitation:
            raise RuntimeError(
                "External research was enabled, but paper-ppt-research did not produce research/sources.json with sources or a concrete limitation."
            )

    if policy.get("allow_deep_research"):
        subagents = report.get("subagents")
        if not isinstance(subagents, list) or not subagents:
            raise RuntimeError(
                "Deep research was enabled, but agent_report.json does not list SubAgent usage or skip reasons."
            )
        missing_reason = [
            item for item in subagents
            if isinstance(item, dict)
            and not bool(item.get("used"))
            and not str(item.get("skip_reason") or "").strip()
        ]
        if missing_reason:
            raise RuntimeError(
                "Deep research SubAgent entries must include skip_reason when not used."
            )


def _empty_agent_output_message(project_dir: Path) -> str:
    report_path = project_dir / "agent_report.json"
    report_status = ""
    if report_path.exists():
        try:
            raw_report = json.loads(report_path.read_text(encoding="utf-8"))
            report_status = str(
                raw_report.get("status")
                or raw_report.get("summary")
                or raw_report.get("error")
                or ""
            ).strip()
        except (OSError, json.JSONDecodeError):
            report_status = "agent_report.json exists but could not be parsed"

    known_files = [
        name
        for name in ("manuscript.md", "design_spec.md", "agent_report.json")
        if (project_dir / name).exists()
    ]
    stray_svgs = [
        _relative_path(project_dir, path)
        for path in sorted(project_dir.rglob("*.svg"))
        if "svg_output" not in path.parts
        and "svg_final" not in path.parts
        and not path.name.startswith(".__")
    ][:8]

    details: list[str] = []
    if known_files:
        details.append(f"existing files: {', '.join(known_files)}")
    if report_status:
        details.append(f"agent report: {report_status[:300]}")
    if stray_svgs:
        details.append(f"SVGs found outside svg_output/: {', '.join(stray_svgs)}")

    suffix = f" ({'; '.join(details)})" if details else ""
    return f"Agent mode completed without producing SVG slides in svg_output/.{suffix}"


def _relative_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _update_agent_usage_totals(totals: _AgentUsageTotals, message: Any) -> AgentProgressEvent | None:
    if totals.runtime == "codex":
        changed = False
        if totals.model and totals.seen_message_ids is None:
            totals.seen_message_ids = set()
        if totals.model and totals.seen_message_ids is not None and "codex:model" not in totals.seen_message_ids:
            totals.seen_message_ids.add("codex:model")
            changed = True
        token_values = _codex_token_usage_values(message)
        if token_values is not None:
            totals.input_tokens = token_values["input_tokens"]
            totals.output_tokens = token_values["output_tokens"]
            totals.cache_read_input_tokens = token_values["cache_read_input_tokens"]
            totals.cache_creation_input_tokens = token_values["cache_creation_input_tokens"]
            changed = True
        if not changed:
            return None
        data: dict[str, Any] = {
            "model": totals.model,
            "input_tokens": totals.input_tokens,
            "output_tokens": totals.output_tokens,
            "cache_read_input_tokens": totals.cache_read_input_tokens,
            "cache_creation_input_tokens": totals.cache_creation_input_tokens,
            "total_cost_usd": 0.0,
            "num_turns": totals.num_turns,
            "duration_ms": totals.duration_ms,
        }
        if token_values is not None:
            data["reasoning_output_tokens"] = token_values["reasoning_output_tokens"]
            data["total_tokens"] = token_values["total_tokens"]
            data["model_context_window"] = token_values["model_context_window"]
        return AgentProgressEvent(
            "agent",
            "progress",
            "Usage update",
            0.12,
            data={
                "runtime": totals.runtime,
                "agent_event": {
                    "type": "usage",
                    "stage": "agent",
                    "status": "running",
                    "message": "Usage update",
                    "data": data,
                },
            },
        )

    kind = message.__class__.__name__
    changed = False
    if totals.seen_message_ids is None:
        totals.seen_message_ids = set()
    if kind == "AssistantMessage":
        model = getattr(message, "model", None)
        if isinstance(model, str) and model:
            totals.model = model
            changed = True
        msg_id = getattr(message, "message_id", None) or getattr(message, "uuid", None)
        if isinstance(msg_id, str) and msg_id in totals.seen_message_ids:
            return None
        if isinstance(msg_id, str):
            totals.seen_message_ids.add(msg_id)
        usage_values = _claude_usage_values(getattr(message, "usage", None))
        if usage_values is not None:
            changed = _add_claude_usage_values(totals, usage_values, pending=True) or changed
    elif kind == "ResultMessage":
        if totals.seen_result_ids is None:
            totals.seen_result_ids = set()
        result_id = getattr(message, "uuid", None)
        if not isinstance(result_id, str) or not result_id:
            result_id = ":".join(
                str(getattr(message, name, "") or "")
                for name in ("session_id", "duration_ms", "duration_api_ms", "num_turns", "stop_reason")
            )
        result_seen = result_id in totals.seen_result_ids
        totals.seen_result_ids.add(result_id)

        cost = getattr(message, "total_cost_usd", None)
        if isinstance(cost, (int, float)) and not result_seen:
            totals.total_cost_usd += float(cost)
            changed = True
        num_turns = getattr(message, "num_turns", None)
        if isinstance(num_turns, int) and not result_seen:
            totals.num_turns += num_turns
            changed = True
        duration_ms = getattr(message, "duration_ms", None)
        if isinstance(duration_ms, int) and not result_seen:
            totals.duration_ms += duration_ms
            changed = True
        model_usage = getattr(message, "model_usage", None)
        usage_model, model_usage_values, model_usage_cost = _claude_model_usage_values(model_usage)
        if usage_model and not totals.model:
            totals.model = usage_model
            changed = True
        if not result_seen:
            usage_values = _claude_usage_values(getattr(message, "usage", None)) or model_usage_values
            if usage_values is not None:
                current_values = _current_claude_usage_values(totals)
                if not _usage_values_equal(usage_values, current_values):
                    pending_values = _pending_claude_usage_values(totals)
                    missing_values = {
                        key: max(_safe_int(usage_values.get(key)) - _safe_int(pending_values.get(key)), 0)
                        for key in usage_values
                    }
                    changed = _add_claude_usage_values(totals, missing_values, pending=False) or changed
            if model_usage_cost and cost is None:
                totals.total_cost_usd += model_usage_cost
                changed = True
        _reset_pending_claude_usage(totals)
    if not changed:
        return None
    return AgentProgressEvent(
        "agent",
        "progress",
        "Usage update",
        0.12,
        data={
            "runtime": totals.runtime,
            "agent_event": {
                "type": "usage",
                "stage": "agent",
                "status": "running",
                "message": "Usage update",
                "data": {
                    "model": totals.model,
                    "input_tokens": totals.input_tokens,
                    "output_tokens": totals.output_tokens,
                    "cache_read_input_tokens": totals.cache_read_input_tokens,
                    "cache_creation_input_tokens": totals.cache_creation_input_tokens,
                    "total_tokens": (
                        totals.input_tokens
                        + totals.output_tokens
                        + totals.cache_read_input_tokens
                        + totals.cache_creation_input_tokens
                    ),
                    "total_cost_usd": round(totals.total_cost_usd, 6),
                    "num_turns": totals.num_turns,
                    "duration_ms": totals.duration_ms,
                },
            },
        },
    )


def _validate_single_svg(svg_path: Path, content: str) -> None:
    if "<svg" not in content:
        raise ValueError(f"{svg_path.name} is not an SVG document")
    # Agents sometimes emit HTML comments or whitespace before the <?xml
    # declaration.  Strip everything before the first '<svg' tag so the
    # XML parser does not choke on "XML declaration not at start".
    svg_start = content.find("<svg")
    if svg_start > 0:
        content = content[svg_start:]
    root = ET.fromstring(content)
    tag = root.tag.split("}", 1)[-1].lower()
    if tag != "svg":
        raise ValueError(f"{svg_path.name} root element is not svg")
    if not (root.get("viewBox") or (root.get("width") and root.get("height"))):
        raise ValueError(f"{svg_path.name} must define viewBox or width/height")


def _embed_svg_preview(svg_content: str, project_dir: Path) -> str:
    from backend.generator.svg_finalize.render_ready import prepare_svg_content_for_render

    # Strip leading comments / whitespace before <?xml or <svg so downstream
    # XML parsers do not fail with "declaration not at start of entity".
    svg_start = svg_content.find("<svg")
    if svg_start > 0:
        svg_content = svg_content[svg_start:]
    return prepare_svg_content_for_render(svg_content, project_dir / "svg_output")


def _copy_agent_skill(project_dir: Path) -> None:
    copied_sources: list[Path] = []
    for skill_name in (_AGENT_SKILL_NAME, _RESEARCH_SKILL_NAME, "paper-ppt-deep-research"):
        source = PROJECT_ROOT / "assets" / "agent_skills" / skill_name
        if not source.exists():
            raise FileNotFoundError(f"Agent skill not found: {source}")
        copied_sources.append(source)
        for target in (
            project_dir / "skills" / skill_name,
            project_dir / ".claude" / "skills" / skill_name,
        ):
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)

    source = copied_sources[0]
    references = source / "references"
    if references.exists():
        root_references = project_dir / "references"
        if root_references.exists():
            shutil.rmtree(root_references)
        shutil.copytree(references, root_references)


def _build_runtime_prompt(project_dir: Path, request: Any, agent_config: dict[str, Any]) -> str:
    language = "Chinese" if str(getattr(request, "language", "zh")).lower().startswith("zh") else "English"
    deep = "enabled" if agent_config.get("allow_deep_research") else "disabled"
    external = "enabled" if agent_config.get("allow_external_research") else "disabled"
    review = "enabled" if agent_config.get("enable_visual_qa", True) else "disabled"
    detail_profile = _agent_detail_profile(
        getattr(request, "detail_level", "normal"),
        getattr(request, "num_pages", None),
    )
    skill_path = str(project_dir / "skills" / _AGENT_SKILL_NAME / "SKILL.md")
    extractor_path = f"skills/{_AGENT_SKILL_NAME}/scripts/extract_paper_assets.py"
    return f"""Use the `{_AGENT_SKILL_NAME}` skill to generate the full PowerPoint deck.

Workspace contract:
- First read `agent_task.json`.
- The current working directory is the generated workspace. If you need to open the skill file manually, read `{skill_path}` and follow its output contract.
- Do not call this application's legacy provider pipeline or ask for external model API keys.
- User intent priority: `agent_task.json.presentation.user_instruction` is the highest product requirement after hard export validity. The SVG/PPTX output files are a technical wrapper for this app, not a reason to override the user's requested visual direction. If the user asks for no SVG, image-only, GPT-image-style, or "more beautiful", preserve that goal by making an image-led, polished deck inside valid SVG slide wrappers; do not reply that the output contract wins over the user.
- Use subagents/parallel work when the runtime supports it. When deep research is enabled, SubAgents are required unless the Task/SubAgent tool is unavailable or fails.
- External research: {external}. Deep research: {deep}. Agent post-generation review: {review}.
- Deck budget contract: {detail_profile["slide_count_guidance"]} The detail selector affects only slide count and source/summary retrieval budgets. It does not set per-slide density, bullet count, evidence count, typography, or layout.
- Structural contract: slide 1 is the cover and slide 2 is a mandatory table of contents whose chapter titles match the final chapter plan. Count structural pages separately and use the remaining content-slide budget to choose chapter boundaries.
- Chapter contract: use at most {detail_profile["page_type_budget"]["chapter"]} chapter dividers. Chapters are presentation-level sections, not paper subsection headings; local method parts, result groups, and discussion points belong on content slides.
- Per-slide authoring contract: author every `svg_output/slide_###.svg` directly as a separate file. Do not create or run a Python/JavaScript/TypeScript/shell/PowerShell/batch/Ruby program that generates multiple slides. Do not use loops, a `SLIDES`/maker registry, shared `base_svg`/`title`/card rendering functions, or template expansion to emit the deck. Shared palette, typography, margins, and recurring chrome may be documented, but each slide must have an explicit layout and SVG markup chosen from that slide's claim and evidence. Read-only validation/render/export scripts remain allowed.
- Layout contract: design each slide's spatial layout when generating that slide. Build from aligned rectangular regions first; content slides should normally occupy 65-85% of the content area and avoid empty quadrants or detached bottom callouts. Wrap dynamically from each region's actual width, font size, visual share, and content density. Keep line edges, baselines, captions, and padding inside the final boxes. Size table columns from their longest visible cells.
- Continuity contract: before writing a chapter slide, read at most the two most recent earlier chapter-slide SVGs. Before writing a content slide, read at most the two most recent earlier content-slide SVGs, skipping cover, TOC, chapter, and ending slides. Use them for style continuity only; the current manuscript remains authoritative.
- Factual rendering contract: preserve exact source numbers, notation, precision, and units. Do not abbreviate, invent, or round metrics, chart axes, ticks, dates, sample sizes, or settings. Mark derived values explicitly and show their source inputs/calculation nearby. Evidence-card ids and retrieval snippets are private grounding, not visible slide copy.
- Icon contract: icons are optional decorative/semantic visual aids. Add them only when they make the slide clearer or more polished, such as repeated semantic labels, process steps, comparison dimensions, legends, or diagram nodes. Before using icons, define a small vocabulary in `design_spec.md` (concept -> existing local SVG file -> size/style/color). Use filesystem commands against `agent_task.json.paths.icons` to list and match available `.svg` files directly; do not use icon search scripts, icon metadata files, `index_meta.json`, or vector indexes. Reuse the same icon for the same concept, keep to one icon family/style, and color icons with the template/deck palette. Avoid scattering one-off icons or multi-color icon accents across unrelated slides. Never use letters, initials, ASCII/Unicode symbols, emoji, dingbats, or decorative characters as icon substitutes; omit the icon or use a non-icon shape if no matching asset exists.
- SubAgent contract: do not split work just to satisfy ordinary slide count or budget selection. When deep research is enabled, launch focused research SubAgents if the Task tool is available; use separate readers for background/related work, method, experiments, and critique when the paper is complex. When Agent review is enabled, run one focused review SubAgent after the first full deck is drafted; it should review the whole deck for layout overflow, missing assets, icon-policy violations, and narrative gaps instead of checking pages one by one. If you skip a required deep-research or review SubAgent, write a concrete reason in `agent_report.json.subagents`; the main Agent must merge results and keep final narrative/design consistency.
- External research contract: if external research is enabled, use the `{_RESEARCH_SKILL_NAME}` skill after reading the paper. You, not the backend, must choose search queries from paper understanding before calling its script/API helpers. It is mandatory to produce `research/raw_external_results.json`, `research/sources.json`, and `research/brief.md` before writing any manuscript, design specification, notes, report, or slide SVG. Read `research/external_search_summary.json` before sampling raw results. Sparse or zero directly relevant external results are acceptable when `research/sources.json` records a concrete limitation; do not repeat searches merely to satisfy the gate or increase counts. For web results, open/read relevant pages deeply enough to understand the source; do not cite titles/snippets alone.
- Deep research contract: if deep research is enabled, use the `{_DEEP_RESEARCH_SKILL_NAME}` skill for focused paper-reading passes, SubAgent/task decomposition, and slide-ready synthesis. It is mandatory to produce `research/deep/notes_index.json` and `research/deep/brief.md` before writing any manuscript, design specification, notes, report, or slide SVG.
- Source asset contract: before writing `manuscript.md`, read `source_assets/paper.md` as the complete extracted paper, not merely a summary or figure index, and read `source_assets/figures.md`/`figures.json` if they exist. If they are missing or stale, run `{extractor_path}` with `agent_task.json.paths.python`. Pick paper figures by caption/page/context/dimensions and put them into SVG using the exact `href` values from the manifest, usually `../source_assets/images/<file>`.
- When external or deep research is enabled, a backend research gate rejects authoring writes until all required research artifacts above exist. Write those research artifacts first, then send a user-visible progress reply summarizing what you learned, source limitations, and how this changes the planned deck. Do not merely echo query/API counts.
- Reply language for status/reporting: {language}.
- During generation, periodically check `agent_feedback/` for user guidance files. Treat newer guidance as higher priority; if wording conflicts with technical export constraints, adapt the implementation while preserving the user's underlying goal.
- Work in stages. After completing each major milestone (e.g. finishing a slide, completing the manuscript, or finishing a batch of slides), briefly note what you accomplished. The system will automatically pause and resume your work, allowing the user to provide guidance between stages. When you resume, do NOT repeat completed work — pick up exactly where you left off.
- If the source is a PDF, do not use the Read tool directly on the PDF binary. Use `agent_task.json.paths.python` or `PAPER_PPT_PYTHON` to run Python/PyMuPDF (`fitz`) first, and only report password protection when `doc.needs_pass` is true.
- For PDF figures, rely on `source_assets/figures.json`: it includes figure image hrefs, captions, page numbers, bounding boxes, and nearby text so non-multimodal runtimes can still select meaningful figures.

Required final files:
- `manuscript.md`
- `design_spec.md`
- `svg_output/slide_001.svg`, `svg_output/slide_002.svg`, ...
- optional `notes/slide_001.md`, ...
- `agent_report.json`

When a slide SVG is complete, write it immediately to `svg_output/` so the app can preview it live.
Finish only after all required SVGs validate and `agent_report.json` summarizes the work.
"""


def _build_feedback_runtime_prompt(project_dir: Path, feedback: str) -> str:
    skill_path = str(project_dir / "skills" / _AGENT_SKILL_NAME / "SKILL.md")
    return f"""Use the `{_AGENT_SKILL_NAME}` skill to revise the existing PowerPoint deck in this same workspace.

Workspace contract:
- First read `agent_task.json`, then read `{skill_path}` if needed.
- Read the existing `manuscript.md`, `design_spec.md`, current `svg_output/*.svg`, and the newest files in `agent_feedback/`.
- Apply this latest user feedback: {feedback.strip()}
- Do not call this application's legacy provider pipeline or ask for external model API keys.
- Do not regenerate the whole deck from scratch unless the feedback truly requires structural changes.
- Preserve existing slide order and style unless the feedback asks to change them.
- When a slide SVG is revised, write it immediately to `svg_output/` so the app can preview it live.
- Keep following `agent_task.json.presentation.detail_profile`, `agent_task.json.agent_policy.icon_policy`, and `agent_task.json.agent_policy.subagent_policy`.
- Keep following `agent_task.json.agent_policy.slide_authoring_policy`: revise each affected SVG directly. Do not introduce a generator script, loop, shared renderer, or template expander even when many slides need the same change.
- Keep following `agent_task.json.agent_policy.layout_policy`: revise layout by changing the SVG placement math, not by only adding a post-hoc QA note.
- Keep following `agent_task.json.agent_policy.factual_rendering_policy`: preserve exact source numbers/units, avoid K/M/B shorthand unless source-authored, and label transparent derivations.
- Keep following `agent_task.json.agent_policy.source_asset_policy`: when adding/replacing paper figures, use the exact `href` from `source_assets/figures.json` and preserve the figure's aspect ratio.
- Continue to follow the icon vocabulary in `design_spec.md`: use icons only where they clarify structure or repeated concepts, reuse the same local icon/style/color for the same concept, and omit icons that would be one-off decoration. Never use letters, symbols, emoji, or decorative glyphs as icon substitutes.
- Use `agent_task.json.paths.python` or `PAPER_PPT_PYTHON` for Python/PyMuPDF commands instead of system Python.
- If feedback requires new external research, use the `paper-ppt-research` skill and choose queries after reading the affected slide/paper context. If feedback requires renewed deep paper analysis, use the `paper-ppt-deep-research` skill.
- Use SubAgents/Task for deep research or final whole-deck review when those modes are enabled and supported; otherwise use them only when genuinely useful.
- Update `manuscript.md`, `design_spec.md`, notes, and `agent_report.json` to reflect the revision.

Finish only after the revised SVGs validate and `agent_report.json` summarizes what changed.
"""


def _agent_config_from_existing_task(project_dir: Path, runtime: str) -> dict[str, Any]:
    task = _read_agent_task(project_dir)
    policy = task.get("agent_policy") if isinstance(task.get("agent_policy"), dict) else {}
    return {
        "runtime": runtime,
        "allow_external_research": bool(policy.get("allow_external_research")),
        "allow_deep_research": bool(policy.get("allow_deep_research")),
        "enable_visual_qa": bool(policy.get("enable_visual_qa", True)),
    }


def _canvas_format_from_task(project_dir: Path) -> str:
    task = _read_agent_task(project_dir)
    presentation = task.get("presentation") if isinstance(task.get("presentation"), dict) else {}
    value = presentation.get("canvas_format")
    return str(value or "ppt169")


def _read_agent_task(project_dir: Path) -> dict[str, Any]:
    task_path = project_dir / "agent_task.json"
    try:
        return json.loads(task_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _agent_detail_profile(detail_level: str | None, num_pages: int | None) -> dict[str, Any]:
    normalized = str(detail_level or "normal").strip().lower()
    if normalized not in {"normal", "high", "very_high"}:
        normalized = "normal"
    budget = page_type_budget(num_pages, normalized)
    min_pages, max_pages = auto_slide_range(normalized)
    slide_count_guidance = page_type_budget_guidance(num_pages, normalized)
    base: dict[str, dict[str, Any]] = {
        "normal": {
            "paper_input_char_budget": 36000,
            "summary_token_budget": 8192,
            "evidence_cards_per_page": 3,
            "source_sections_per_page": 1,
        },
        "high": {
            "paper_input_char_budget": 52000,
            "summary_token_budget": 12288,
            "evidence_cards_per_page": 4,
            "source_sections_per_page": 2,
        },
        "very_high": {
            "paper_input_char_budget": 72000,
            "summary_token_budget": 16384,
            "evidence_cards_per_page": 5,
            "source_sections_per_page": 2,
        },
    }
    profile = dict(base[normalized])
    profile.update(
        {
            "target_pages": num_pages,
            "auto_slide_range": [min_pages, max_pages],
            "page_type_budget": budget,
            "slide_count_guidance": slide_count_guidance,
        }
    )
    return profile


def _agent_icon_policy() -> dict[str, Any]:
    return {
        "search_only_after_identifying_a_concrete_need": True,
        "use_icons_only_when_they_clarify": True,
        "filesystem_command_matching_required": True,
        "metadata_or_vector_search_forbidden": True,
        "forbidden_search_sources": [
            "skills/paper-ppt-generate/scripts/search_icons.py",
            "index_meta.json",
            "index.npz",
            "any icon metadata file or vector index",
        ],
        "selection_method": (
            "Use shell/filesystem commands against agent_task.json.paths.icons to "
            "list available .svg filenames and match by concrete concept/name. "
            "Verify the selected SVG file exists before referencing it."
        ),
        "when_to_use": [
            "Use icons as restrained decorative/semantic aids for repeated labels, process steps, comparison dimensions, legend keys, and compact section markers.",
            "Do not add icons as filler decoration to ordinary bullet lists, paragraph cards, dense evidence slides, or places where a label alone is clearer.",
            "Prefer one consistent icon system for a repeated pattern; if only one slide would use an icon, consider omitting it.",
        ],
        "density_limits": {
            "default_icons_per_content_slide": "0-3",
            "maximum_icons_per_slide_unless_diagram": 4,
            "diagram_exception": "More icons are acceptable only when every icon is a node in a structured flow, taxonomy, or legend.",
        },
        "consistency_rules": [
            "Build a small icon vocabulary in design_spec.md before drafting slides: semantic role, icon file, size, stroke/fill style, and color.",
            "Reuse the same icon for the same concept across slides; do not mix filled and outline libraries in one visual system unless the template already does so.",
            "Use a consistent size scale and alignment grid. Icons should support hierarchy, not create new focal points on every slide.",
        ],
        "color_rules": [
            "Use template or deck palette tokens from design_spec.md; icons should usually use primary, muted foreground, or one accent color.",
            "Avoid per-icon rainbow colors. Use different colors only when they encode a real category or status and the legend/pattern is repeated.",
            "Keep icon contrast accessible against its background and avoid saturated accents on low-importance icons.",
        ],
        "allowed": [
            "existing SVG/icon files found in the configured local icon path",
            "template-provided icons or vector assets that actually exist",
            "simple geometric shapes only when they are decorative shapes, not fake icons",
        ],
        "forbidden_substitutes": [
            "single letters or initials such as P, F, A, Q",
            "ASCII punctuation or symbols such as !, ?, +, *, #, >, <, /",
            "Unicode symbols, dingbats, stars, checkmarks, arrows, warning marks, and pictographs used as icons",
            "emoji or colored emoji glyphs",
            "text glyph arrows such as ->, <-, up/down arrow glyphs; use SVG line/path geometry instead",
            "any single-character text used as a visual badge, status mark, icon, or connector",
            "made-up icon filenames or remote icon URLs",
        ],
        "fallback_when_missing": "Omit the icon or use a neutral non-icon shape/card accent; do not add an unrelated icon just to fill space.",
        "reporting": "agent_report.json must mention whether local icons were searched, list any icon assets used, and explain the icon vocabulary or why icons were mostly omitted.",
    }


def _agent_layout_policy() -> dict[str, Any]:
    return {
        "generation_phase": "design_per_slide",
        "region_plan_required": False,
        "layout_designer": "executor_per_slide",
        "layout_design_note": (
            "The executor designs each slide's spatial layout when generating that slide, "
            "not upfront in design_spec.md. The strategist only provides global design system, "
            "layout families, style families, and density targets."
        ),
        "content_area_default": {
            "ppt169": {"x": 40, "y": 100, "w": 1200, "h": 520},
        },
        "occupancy_target": {
            "content_slides": "65-85% of the content area should be meaningfully occupied",
            "structural_slides": "sparse by design; do not force content density",
        },
        "layout_families": [
            "figure-left-text-right",
            "text-left-figure-right",
            "full-bleed-image-overlay",
            "kpi-dashboard",
            "full-width-chart-with-notes",
            "comparison-table-callout",
            "two-column-evidence",
            "three-card-grid",
            "four-quadrant-matrix",
            "process-flow-with-evidence",
            "hero-title-plus-callouts",
            "timeline-horizontal",
            "numbered-steps",
        ],
        "balance_rules": [
            "Use a shared grid and 24-40px gutters between major regions.",
            "Avoid empty quadrants, floating short bullet lists, and bottom callouts detached from the main grid.",
            "For sparse content, enlarge the supported figure, convert bullets into grouped callouts, or draw a simple paper-supported mechanism diagram.",
            "For image+text pages, reserve the figure frame first, preserve aspect ratio inside that frame, and make the text column use comparable vertical rhythm.",
            "Visual depth is required: use filter shadows on cards, gradients for backgrounds/overlays, accent top-bars on cards, and subtle decorative elements. Flat pages without elevation look unfinished.",
            "Every card/panel should have a shadow, an accent element, and proper inner padding — not just a plain flat rect with a stroke border.",
            "Use large bold numbers with small gray labels for KPI/metrics. Use color-coded status indicators. Use accent callout boxes with tinted backgrounds for key takeaways.",
        ],
        "text_wrapping": {
            "mode": "dynamic_per_region",
            "derive_from": "actual region width, font size, visual share, and current-page content",
            "avoid": "conservative early breaks that leave most of a usable line empty",
            "allow": "light raggedness and semantic line breaks",
            "repair": "reflow regions, widen the box, adjust gaps, or tune typography only for actual clipping or overlap",
        },
        "text_capacity_preflight": {
            "required_before_svg": True,
            "horizontal_check": (
                "For every text line, estimate its rendered width and require "
                "text_right <= assigned_region_right - padding."
            ),
            "vertical_check": (
                "Reserve heading, body line-height, caption, and bottom padding before "
                "choosing a card height. Require the last baseline plus descender space "
                "to remain inside the assigned region."
            ),
            "table_check": (
                "Size each table column from its longest visible cell before placing text. "
                "Never let a model/method name collide with the first numeric column."
            ),
            "minimums": [
                "Body text should normally be at least 18px.",
                "A heading plus two body lines normally needs at least 96px including padding.",
                "A heading plus four 18px body lines at 26px leading normally needs at least 156px including padding.",
            ],
            "repair_order": (
                "First widen/reallocate the region, then rewrite/split content, then reduce "
                "font size slightly. Never solve fit by pushing text outside the box."
            ),
        },
        "forbidden": [
            "invented chart axes or ticks used only to fill empty space",
            "paper figure stretching instead of fit-inside-frame aspect preservation",
            "paragraph text placed in a narrow right-edge region without a width budget",
            "bottom cards whose final text baseline falls below the card",
            "table columns positioned without measuring the longest label/cell",
        ],
        "qa_reporting": (
            "agent_report.json should include layout_qa with any slides "
            "repaired for occupancy/wrapping and any remaining limitation."
        ),
    }


def _agent_slide_authoring_policy() -> dict[str, Any]:
    return {
        "mode": "direct_per_slide_authoring",
        "required": [
            "Author each svg_output/slide_###.svg as a separate direct file write or edit.",
            "Design each slide's spatial layout when authoring that slide, based on its claim, evidence, and paper figure.",
            "Choose the composition from that slide's content rather than instantiating a deck-wide page template.",
            "Keep shared deck tokens limited to documented colors, typography, margins, and recurring chrome.",
            "Write SVG with direct inline attributes; do not rely on CSS classes or a `<style>` block.",
        ],
        "forbidden": [
            "Any Python, JavaScript, TypeScript, shell, PowerShell, batch, Ruby, or other program that writes more than one slide SVG.",
            "Loops, slide arrays, maker registries, shared base_svg/title/card rendering functions, or template expansion used to emit multiple slides.",
            "A single script that writes manuscript.md, design_spec.md, agent_report.json, notes, and the complete slide deck in one run.",
            "Treating repeated card grids or a shared page skeleton as evidence that the slide was independently designed.",
            "`<style>` blocks, `class=` attributes, `mask`, `filter`, `clipPath`, `foreignObject`, scripts, event handlers, or remote image/icon URLs in final SVGs.",
        ],
        "allowed_tools": [
            "Scripts for source extraction, research retrieval, read-only SVG inspection, validation, rendering, and PPTX export.",
            "Direct Write/Edit operations for one explicitly named slide SVG at a time.",
            "Copying a small amount of shared visual chrome into each slide while keeping the full page composition explicit.",
        ],
        "repair": (
            "If a multi-slide generator is detected, delete it and directly rewrite every "
            "affected slide SVG one by one. The backend allows one Agent repair turn before "
            "blocking export."
        ),
        "reporting": (
            "agent_report.json must include slide_authoring with mode=direct_per_slide, "
            "multi_slide_generator_used=false, and direct_files listing every final slide SVG."
        ),
    }


def _agent_factual_rendering_policy() -> dict[str, Any]:
    return {
        "source_precision_required": True,
        "rules": [
            "Do not invent metrics, chart ticks, axes, sample sizes, dates, model settings, or causal claims.",
            "Preserve the source number and unit exactly unless the slide explicitly shows a transparent derivation.",
            "Do not compress exact counts into shorthand unless that exact notation appears in the source.",
            "Derived values must be marked as derived and show the source inputs or calculation nearby.",
            "Evidence cards are private grounding; do not print card ids, retrieval snippets, or provenance plumbing as slide content.",
        ],
        "repair_priority": (
            "Fix factual precision before decorative layout. A balanced slide with a rounded "
            "or unsupported number is still incorrect."
        ),
    }


def _agent_source_asset_policy() -> dict[str, Any]:
    return {
        "asset_pack": {
            "paper_markdown": "agent_task.json.paths.source_paper_markdown",
            "figures_json": "agent_task.json.paths.source_figures_json",
            "figures_markdown": "agent_task.json.paths.source_figures_markdown",
            "images_dir": "agent_task.json.paths.source_images",
            "extractor_script": "agent_task.json.paths.source_asset_extractor",
        },
        "must_read_before_manuscript": [
            "source_assets/paper.md as the complete extracted paper, not just a summary",
            "source_assets/figures.md or source_assets/figures.json",
        ],
        "figure_selection_rule": (
            "Choose paper figures by caption, page/location, section/context, and dimensions; "
            "never choose by filename alone."
        ),
        "svg_href_rule": (
            "When adding a paper figure to svg_output/*.svg, use the exact `href` value "
            "from source_assets/figures.json/figures.md, usually ../source_assets/images/<file>."
        ),
        "pdf_rule": (
            "For PDF sources, use the asset pack instead of trying to visually inspect raw PDF images. "
            "It contains rendered figures with caption, page number, bbox, and surrounding context."
        ),
        "latex_archive_rule": (
            "For TeX archives, the extractor unpacks the source and indexes includegraphics entries. "
            "Do not run PDF-style page screenshot extraction over archive images; use the referenced source graphics."
        ),
        "reporting": (
            "agent_report.json should include paper_figures_used with ids/hrefs/captions and note if no suitable paper figure was available."
        ),
    }


def _agent_subagent_policy(request: Any, agent_config: dict[str, Any], detail_profile: dict[str, Any]) -> dict[str, Any]:
    target_pages = int(getattr(request, "num_pages", None) or 0)
    review_enabled = bool(agent_config.get("enable_visual_qa", True))
    return {
        "enabled_when_runtime_supports_task_tool": True,
        "required_when": [
            "allow_deep_research is true",
            "agent_review is enabled",
        ],
        "skip_requires_reason": True,
        "main_agent_responsibility": [
            "own final narrative arc, design_spec.md, consistency, and final SVG integration",
            "merge SubAgent findings instead of pasting independent styles into the deck",
            "resolve user feedback from agent_feedback/ before final export",
        ],
        "use_subagents_for": [
            {
                "scenario": "paper extraction",
                "when": "useful for long/figure-heavy papers, but not required unless deep research is enabled",
                "output": "section-level facts, figures/tables, equations, datasets, metrics, limitations",
            },
            {
                "scenario": "external research",
                "when": "allow_external_research is true",
                "output": "research/brief.md and research/sources.json with only slide-relevant context",
            },
            {
                "scenario": "deep research",
                "when": "allow_deep_research is true",
                "output": "parallel reader notes for background, method, experiments, related work, and critique",
            },
            {
                "scenario": "icon and asset inventory",
                "when": "slides need visual labels, comparison cards, process diagrams, or repeated semantic icons",
                "output": "a checked list of existing local icon filenames mapped to slide concepts",
            },
            {
                "scenario": "slide batch drafting",
                "when": "target_pages >= 8 and parallel drafting is genuinely useful",
                "output": "draft SVGs for separate slide ranges that follow design_spec.md",
            },
            {
                "scenario": "QA",
                "when": "agent_review is enabled; after the first complete deck draft, not per page during generation",
                "output": "whole-deck review findings for layout overflow, missing assets, icon-policy violations, narrative gaps, and slides needing repair",
            },
        ],
        "recommended_parallelism": {
            "target_pages": target_pages,
            "paper_extraction_subagents": 1 if agent_config.get("allow_deep_research") else 0,
            "research_subagents": 3 if agent_config.get("allow_deep_research") else (1 if agent_config.get("allow_external_research") else 0),
            "qa_subagents": 1 if review_enabled else 0,
        },
        "agent_review": {
            "enabled": review_enabled,
            "max_attempts": None,
            "scope": "whole deck after generation",
            "replaces_provider_visual_qa": True,
        },
        "reporting": "agent_report.json must list deep-research and review SubAgent scenarios with used=true/false and a specific reason when skipped.",
    }


def _agent_research_policy(research_payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = research_payload or {}
    web_provider = str(payload.get("web_search_provider") or "tavily")
    return {
        "enabled_sources": {
            "arxiv": bool(payload.get("arxiv_search_enabled")),
            "semantic_scholar": bool(payload.get("semantic_scholar_enabled")),
            "web_search": bool(payload.get("web_search_enabled")),
        },
        "web_search_provider": web_provider,
        "max_results_per_source": int(payload.get("max_results_per_source") or 20),
        "relevance_filter": bool(payload.get("relevance_filter", True)),
        "query_owner": "agent",
        "query_rule": "Read the paper first, then choose search queries from paper-specific understanding; backend does not preselect Agent-mode keywords.",
        "credential_env": {
            "semantic_scholar": "SEMANTIC_SCHOLAR_API_KEY" if payload.get("semantic_scholar_api_key") else None,
            "tavily": "TAVILY_API_KEY" if payload.get("tavily_api_key") else None,
            "serpapi": "SERPAPI_KEY" if payload.get("serpapi_key") else None,
        },
        "agent_may_use_other_sources": True,
        "must_open_and_read_sources": True,
        "minimum_source_depth": (
            "For web results, open the relevant pages and read enough body content "
            "to understand the claim, method, evidence, date, and reliability. "
            "Do not rely on result titles/snippets alone."
        ),
    }


def _agent_readme() -> str:
    return """# Agent PPT Generation Workspace

This workspace is owned by Claude Code or Codex Agent mode.  The backend does
not call provider models here; it watches `svg_output/` for live preview, then
finalizes and exports the deck after the agent completes.
"""


def _slide_number(path: Path, fallback: int) -> int:
    match = re.search(r"(\d+)", path.stem)
    if not match:
        return fallback
    try:
        return max(1, int(match.group(1)))
    except ValueError:
        return fallback


def _rel(base: Path, target: Path) -> str:
    try:
        return target.resolve().relative_to(base.resolve()).as_posix()
    except (OSError, ValueError):
        return str(target)


def _codex_completed_item_text(event: Any) -> str:
    item = _codex_event_item(event)
    if item is None:
        return ""
    item_type = str(getattr(item, "type", "") or "")
    if item_type == "agentMessage":
        return str(getattr(item, "text", "") or "").strip()[:2000]
    if item_type:
        return f"Codex item completed: {item_type}"
    return ""
