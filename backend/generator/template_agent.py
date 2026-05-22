"""Claude Agent-backed collaborator for template import review drafts.

This module intentionally sits beside, not inside, the existing template
import LLM review path. The classic ``/feedback`` flow remains a single LLM
planner call; this runner is an optional long-running agent task that can read
and edit the import workspace with Claude Code/Agent SDK semantics.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import html
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree as ET

from backend.config import settings
from backend.usage.tracker import usage_tracker

AgentConfigMode = Literal["claude_code", "custom"]
AgentStatus = Literal["queued", "running", "complete", "error", "cancelled"]

_EVENT_RING_SIZE = 512
_TEXT_LIMIT = 2000
_AGENT_MAX_BUFFER_SIZE = 16 * 1024 * 1024
_AGENT_RECOVERY_ATTEMPTS = 2


def claude_code_environment_status() -> dict[str, Any]:
    """Return whether the backend can start the Claude Code-backed agent."""
    cli_path = shutil.which("claude")
    sdk_available = True
    sdk_error: str | None = None
    try:
        import claude_agent_sdk  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - report optional dependency state
        sdk_available = False
        sdk_error = str(exc) or exc.__class__.__name__

    available = bool(cli_path) and sdk_available
    if available:
        message = "Claude Code is installed and the Agent SDK is available."
    elif not cli_path and not sdk_available:
        message = "Claude Code CLI and claude-agent-sdk are not available in the backend environment."
    elif not cli_path:
        message = "Claude Code CLI is not installed or not on PATH."
    else:
        message = f"claude-agent-sdk is not available: {sdk_error}"

    return {
        "available": available,
        "cli_path": cli_path,
        "sdk_available": sdk_available,
        "sdk_error": sdk_error,
        "message": message,
    }


@dataclass(slots=True)
class TemplateAgentConfig:
    """Runtime config for one Agent SDK session."""

    mode: AgentConfigMode = "claude_code"
    api_key: str | None = None
    auth_token: str | None = None
    base_url: str | None = None
    model: str | None = None
    custom_model_option: str | None = None
    load_project_settings: bool = True
    # ``None`` means no cap (recommended by Agent SDK overview docs); pass an
    # int to bound a session.
    max_turns: int | None = None
    reply_language: Literal["zh", "en"] = "en"


@dataclass(slots=True)
class TemplateAgentSessionState:
    """Persistent Agent SDK session metadata for one import."""

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    initialized: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    total_cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0
    model_name: str | None = None
    model_usage: dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)


@dataclass
class TemplateAgentJob:
    """In-memory state for an agent job."""

    id: str
    import_id: str
    feedback: str
    config: TemplateAgentConfig
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_state: TemplateAgentSessionState | None = None
    planning: bool = True
    silent: bool = False
    status: AgentStatus = "queued"
    message: str = ""
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    stderr_tail: list[str] = field(default_factory=list)
    last_seq: int = 0
    queues: list[asyncio.Queue[dict[str, Any]]] = field(default_factory=list)
    task: asyncio.Task[None] | None = None
    # ── Usage / cost tracking ───────────────────────────────────────────
    # Per-step token counts deduplicated by AssistantMessage.message_id so
    # parallel tool calls (which share the same id) are counted once. See
    # https://code.claude.com/docs/zh-CN/agent-sdk/cost-tracking
    seen_message_ids: set[str] = field(default_factory=set)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    # Cumulative cost reported by the SDK on each ResultMessage. Each
    # query() call emits its own ResultMessage; we accumulate across calls
    # so the UI sees a running total.
    total_cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0
    model_name: str | None = None
    model_usage: dict[str, Any] = field(default_factory=dict)


class TemplateAgentManager:
    """Small in-process job manager for template-agent sessions."""

    def __init__(self) -> None:
        self._jobs: dict[str, TemplateAgentJob] = {}
        self._session_states: dict[str, TemplateAgentSessionState] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        import_id: str,
        feedback: str,
        config: TemplateAgentConfig,
        *,
        silent: bool = False,
        planning: bool = True,
    ) -> TemplateAgentJob:
        job_id = uuid.uuid4().hex[:12]
        job = TemplateAgentJob(
            id=job_id,
            import_id=import_id,
            feedback=feedback.strip(),
            config=config,
            silent=silent,
        )
        job.session_state = self._load_session_state(import_id)
        self._seed_job_from_session_state(job)
        job.planning = planning
        async with self._lock:
            self._jobs[job_id] = job
        if job.feedback and not silent:
            await self._append_review_message(
                import_id,
                "user",
                job.feedback,
                {
                    "mode": "agent",
                    "agent_job_id": job_id,
                    "planning": planning,
                    "read_only": not planning,
                },
            )
        await self._publish(
            job,
            {
                "type": "status",
                "stage": "queued",
                "status": "queued",
                "message": "Agent task queued.",
            },
        )
        job.task = asyncio.create_task(
            self._run(job),
            name=f"template-agent-{job.id}",
        )
        return job

    def get(self, job_id: str) -> TemplateAgentJob | None:
        return self._jobs.get(job_id)

    async def cancel(self, job_id: str) -> TemplateAgentJob | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.status in {"complete", "error", "cancelled"}:
            return job
        job.status = "cancelled"
        job.message = "Agent task cancelled."
        job.completed_at = time.time()
        job.updated_at = job.completed_at
        task = job.task
        if task is not None and not task.done():
            task.cancel()
        self._save_session_state(job)
        await self._publish(
            job,
            {
                "type": "cancelled",
                "stage": "cancelled",
                "status": "cancelled",
                "message": job.message,
            },
        )
        return job

    def subscribe(self, job_id: str) -> asyncio.Queue[dict[str, Any]]:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=settings.ws_subscriber_queue_size)
        job.queues.append(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        job.queues = [q for q in job.queues if q is not queue]

    def events_after(self, job_id: str, since_seq: int) -> list[dict[str, Any]]:
        job = self._jobs.get(job_id)
        if job is None:
            return []
        if since_seq <= 0:
            return list(job.events)
        return [event for event in job.events if int(event.get("seq", 0) or 0) > since_seq]

    def snapshot(self, job: TemplateAgentJob) -> dict[str, Any]:
        return {
            "type": "snapshot",
            "agent_job_id": job.id,
            "import_id": job.import_id,
            "stage": job.status,
            "status": job.status,
            "message": job.message,
            "error": job.error,
            "last_seq": job.last_seq,
            "ts": time.time(),
        }

    def _session_state_path(self, import_id: str) -> Path:
        return _import_dir(import_id) / "agent_session.json"

    def _load_session_state(self, import_id: str) -> TemplateAgentSessionState:
        state = self._session_states.get(import_id)
        if state is not None:
            return state
        path = self._session_state_path(import_id)
        state = TemplateAgentSessionState()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = None
            if isinstance(raw, dict):
                state.session_id = str(raw.get("session_id") or state.session_id)
                state.initialized = bool(raw.get("initialized"))
                state.input_tokens = int(raw.get("input_tokens") or 0)
                state.output_tokens = int(raw.get("output_tokens") or 0)
                state.cache_read_tokens = int(raw.get("cache_read_tokens") or 0)
                state.cache_creation_tokens = int(raw.get("cache_creation_tokens") or 0)
                state.total_cost_usd = float(raw.get("total_cost_usd") or 0.0)
                state.num_turns = int(raw.get("num_turns") or 0)
                state.duration_ms = int(raw.get("duration_ms") or 0)
                model_name = raw.get("model_name")
                state.model_name = str(model_name) if isinstance(model_name, str) and model_name else None
                model_usage = raw.get("model_usage")
                if isinstance(model_usage, dict):
                    state.model_usage = copy.deepcopy(model_usage)
                updated_at = raw.get("updated_at")
                if isinstance(updated_at, (int, float)):
                    state.updated_at = float(updated_at)
        self._session_states[import_id] = state
        return state

    def _save_session_state(self, job: TemplateAgentJob) -> None:
        if job.session_state is None:
            return
        state = job.session_state
        state.session_id = job.session_id or state.session_id
        state.input_tokens = job.input_tokens
        state.output_tokens = job.output_tokens
        state.cache_read_tokens = job.cache_read_tokens
        state.cache_creation_tokens = job.cache_creation_tokens
        state.total_cost_usd = job.total_cost_usd
        state.num_turns = job.num_turns
        state.duration_ms = job.duration_ms
        state.model_name = job.model_name
        state.model_usage = copy.deepcopy(job.model_usage)
        state.updated_at = time.time()
        self._session_states[job.import_id] = state
        _atomic_write_json(self._session_state_path(job.import_id), _session_state_payload(state))

    def _seed_job_from_session_state(self, job: TemplateAgentJob) -> None:
        if job.session_state is None:
            return
        state = job.session_state
        job.session_id = state.session_id
        job.input_tokens = state.input_tokens
        job.output_tokens = state.output_tokens
        job.cache_read_tokens = state.cache_read_tokens
        job.cache_creation_tokens = state.cache_creation_tokens
        job.total_cost_usd = state.total_cost_usd
        job.num_turns = state.num_turns
        job.duration_ms = state.duration_ms
        job.model_name = state.model_name
        job.model_usage = copy.deepcopy(state.model_usage)

    async def _run(self, job: TemplateAgentJob) -> None:
        job.status = "running"
        job.started_at = time.time()
        job.updated_at = job.started_at
        await self._publish(
            job,
            {
                "type": "status",
                "stage": "starting",
                "status": "running",
                "message": "Starting Claude Agent.",
            },
        )

        try:
            try:
                from claude_agent_sdk import ClaudeAgentOptions
            except Exception as exc:  # noqa: BLE001 - optional runtime dependency
                raise RuntimeError(
                    "Claude Agent SDK is not installed. Install `claude-agent-sdk` "
                    "in the backend environment to use Agent collaboration mode."
                ) from exc

            import_dir = _import_dir(job.import_id)
            review_path = import_dir / "review.json"
            if not review_path.exists():
                raise FileNotFoundError(f"review.json not found for import {job.import_id}")

            if job.planning:
                _seed_agent_template_sources(import_dir)
            _write_agent_context(import_dir, planning=job.planning)
            prompt = _build_prompt(
                job.import_id,
                import_dir,
                job.feedback,
                job.config.reply_language,
                planning=job.planning,
            )
            allowed_tools = (
                [
                    "Read",
                    "Write",
                    "Edit",
                    "MultiEdit",
                    "Grep",
                    "Glob",
                    "LS",
                    "Bash",
                    "TodoWrite",
                ]
                if job.planning
                else ["Read", "Grep", "Glob", "LS"]
            )

            def _options_for_attempt() -> Any:
                resume_session = (
                    job.session_state.session_id
                    if job.session_state is not None and job.session_state.initialized
                    else None
                )
                session_options = {"resume": resume_session} if resume_session else {}
                return ClaudeAgentOptions(
                    tools={
                        "type": "preset",
                        "preset": "claude_code",
                    },
                    system_prompt={
                        "type": "preset",
                        "preset": "claude_code",
                    },
                    cwd=str(import_dir),
                    add_dirs=[str(import_dir)],
                    allowed_tools=allowed_tools,
                    permission_mode="bypassPermissions",
                    include_partial_messages=False,
                    max_buffer_size=_AGENT_MAX_BUFFER_SIZE,
                    # Omit max_turns when None so the SDK runs to natural completion
                    # instead of stopping at the (small) default cap.
                    **({"max_turns": int(job.config.max_turns)} if job.config.max_turns else {}),
                    **session_options,
                    setting_sources=_setting_sources(job.config),
                    model=job.config.model or None,
                    env=_env_for_config(job.config),
                    stderr=lambda line: self._handle_stderr(job, line),
                )

            await self._publish(
                job,
                {
                    "type": "status",
                    "stage": "agent",
                    "status": "running",
                    "message": "Agent running.",
                },
            )
            attempt = 0
            while True:
                attempt += 1
                usage_snapshot = _usage_snapshot(job)
                if attempt > 1:
                    _write_agent_context(import_dir, planning=job.planning)
                attempt_prompt = (
                    prompt
                    if attempt == 1
                    else _build_recovery_prompt(
                        job.import_id,
                        import_dir,
                        job.feedback,
                        job.config.reply_language,
                        planning=job.planning,
                    )
                )
                try:
                    async for message in _iter_client_in_worker_loop(
                        attempt_prompt,
                        _options_for_attempt(),
                    ):
                        # Update usage / cost counters before fanning out events so
                        # the UI sees them on the same tick as the originating frame.
                        self._update_usage(
                            job,
                            message,
                            usage_snapshot=usage_snapshot,
                        )
                        for event in _events_from_sdk_message(message):
                            if event.get("type") == "message":
                                text = str(event.get("message") or "").strip()
                                if text:
                                    # Persist each assistant message individually so
                                    # the frontend dedupe (matches by exact content)
                                    # collapses the streamed bubble into the saved
                                    # one. We used to save a combined blob on
                                    # completion, which produced a duplicate bubble.
                                    await self._append_review_message(
                                        job.import_id,
                                        "assistant",
                                        text,
                                        {
                                            "mode": "agent",
                                            "agent_job_id": job.id,
                                            "planning": job.planning,
                                            "read_only": not job.planning,
                                        },
                                    )
                            await self._publish(job, event)
                        if message.__class__.__name__ == "ResultMessage":
                            self._record_usage(job, usage_snapshot, attempt=attempt)
                            self._save_session_state(job)
                    break
                except Exception as exc:  # noqa: BLE001 - resume recoverable SDK stream breaks
                    if attempt > _AGENT_RECOVERY_ATTEMPTS or not _is_recoverable_agent_error(exc):
                        raise
                    job.message = "Agent stream interrupted; resuming the session."
                    job.updated_at = time.time()
                    await self._publish(
                        job,
                        {
                            "type": "status",
                            "stage": "agent",
                            "status": "running",
                            "message": "Agent stream interrupted; resuming the session.",
                            "data": {
                                "recoverable": True,
                                "attempt": attempt,
                                "session_id": job.session_id,
                                "error": _format_agent_error(exc, [])[:500],
                            },
                        },
                    )

            # Mark the import as if the LLM analysis ran only after a real
            # planning/editing run. The automatic Agent inspection is read-only
            # and should not unlock confirm/templated preview by itself.
            if job.planning:
                await self._mark_llm_satisfied(job.import_id)
                await self._publish(
                    job,
                    {
                        "type": "artifact_updated",
                        "stage": "preview",
                        "status": "complete",
                        "message": "Template artifacts updated.",
                        "data": {
                            "preview_version": time.time(),
                            "changed_page_types": ["cover", "toc", "chapter", "content", "ending"],
                            "warnings": [],
                        },
                    },
                )
            if not job.silent:
                await self._resolve_active_annotations(job.import_id)
            job.status = "complete"
            job.message = "Agent task complete."
            job.completed_at = time.time()
            job.updated_at = job.completed_at
            self._save_session_state(job)
            await self._publish(
                job,
                {
                    "type": "complete",
                    "stage": "complete",
                    "status": "complete",
                    "message": job.message,
                    "data": {"review_changed": review_path.exists()},
                },
            )
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.message = "Agent task cancelled."
            job.completed_at = time.time()
            job.updated_at = job.completed_at
            self._save_session_state(job)
            await self._publish(
                job,
                {
                    "type": "cancelled",
                    "stage": "cancelled",
                    "status": "cancelled",
                    "message": job.message,
                },
            )
            raise
        except Exception as exc:  # noqa: BLE001 - surface all agent failures
            job.status = "error"
            job.error = _format_agent_error(exc, job.stderr_tail)
            job.message = "Agent task failed."
            job.completed_at = time.time()
            job.updated_at = job.completed_at
            self._save_session_state(job)
            await self._append_review_message(
                job.import_id,
                "assistant",
                f"Agent 模式执行失败：{job.error}",
                {
                    "mode": "agent",
                    "agent_job_id": job.id,
                    "planning": job.planning,
                    "read_only": not job.planning,
                    "error": True,
                },
            )
            await self._publish(
                job,
                {
                    "type": "error",
                    "stage": "error",
                    "status": "error",
                    "message": job.error,
                    "error": job.error,
                },
            )

    def _update_usage(
        self,
        job: TemplateAgentJob,
        message: Any,
        *,
        usage_snapshot: dict[str, int] | None = None,
    ) -> None:
        """Roll usage / cost counters from SDK messages onto the job.

        Mirrors the dedup-by-message-id pattern recommended by the cost
        tracking docs (https://code.claude.com/docs/zh-CN/agent-sdk/cost-tracking):
        parallel tool calls share an ``id``, so we count tokens only once
        per unique AssistantMessage.message_id. ResultMessage carries the
        cumulative SDK estimate for the just-finished query() call; we
        accumulate across query() calls in case multiple are run.
        """
        kind = message.__class__.__name__

        if kind == "SystemMessage":
            subtype = getattr(message, "subtype", None)
            if subtype == "init":
                data = getattr(message, "data", None)
                if isinstance(data, dict):
                    session_id = data.get("session_id")
                    if isinstance(session_id, str) and session_id:
                        job.session_id = session_id
                        if job.session_state is not None:
                            job.session_state.session_id = session_id
                            job.session_state.initialized = True
                            self._save_session_state(job)
            return

        if kind == "AssistantMessage":
            msg_id = getattr(message, "message_id", None)
            usage = getattr(message, "usage", None) or {}
            model = getattr(message, "model", None)
            if isinstance(model, str) and model:
                job.model_name = model
            if not isinstance(usage, dict):
                return
            if msg_id and msg_id in job.seen_message_ids:
                return
            if msg_id:
                job.seen_message_ids.add(msg_id)
            # Cast keys defensively; SDK uses snake_case in Python.
            job.input_tokens += int(usage.get("input_tokens") or 0)
            job.output_tokens += int(usage.get("output_tokens") or 0)
            job.cache_read_tokens += int(usage.get("cache_read_input_tokens") or 0)
            job.cache_creation_tokens += int(usage.get("cache_creation_input_tokens") or 0)
            _safe_create_task(self._publish(job, _usage_event(job)))
            return

        if kind == "ResultMessage":
            usage = getattr(message, "usage", None) or {}
            if isinstance(usage, dict) and usage_snapshot is not None:
                if (
                    job.input_tokens == usage_snapshot["input_tokens"]
                    and job.output_tokens == usage_snapshot["output_tokens"]
                    and job.cache_read_tokens == usage_snapshot["cache_read_tokens"]
                    and job.cache_creation_tokens == usage_snapshot["cache_creation_tokens"]
                ):
                    job.input_tokens += int(usage.get("input_tokens") or 0)
                    job.output_tokens += int(usage.get("output_tokens") or 0)
                    job.cache_read_tokens += int(usage.get("cache_read_input_tokens") or 0)
                    job.cache_creation_tokens += int(usage.get("cache_creation_input_tokens") or 0)
                    _safe_create_task(self._publish(job, _usage_event(job)))
            cost = getattr(message, "total_cost_usd", None)
            if isinstance(cost, (int, float)):
                job.total_cost_usd += float(cost)
            num_turns = getattr(message, "num_turns", None)
            if isinstance(num_turns, int):
                job.num_turns += int(num_turns)
            duration_ms = getattr(message, "duration_ms", None)
            if isinstance(duration_ms, int):
                job.duration_ms += int(duration_ms)
            model_usage = getattr(message, "model_usage", None)
            if isinstance(model_usage, dict):
                # Merge per-model entries; later calls add to existing model totals.
                for model, stats in model_usage.items():
                    if not isinstance(stats, dict):
                        continue
                    bucket = job.model_usage.setdefault(model, {})
                    for key, value in stats.items():
                        if isinstance(value, (int, float)):
                            bucket[key] = float(bucket.get(key, 0)) + float(value)
                        else:
                            bucket[key] = value
                # Prefer the model from model_usage if AssistantMessage didn't carry one.
                if not job.model_name:
                    first_model = next(iter(model_usage.keys()), None)
                    if isinstance(first_model, str):
                        job.model_name = first_model

    def _record_usage(
        self,
        job: TemplateAgentJob,
        usage_snapshot: dict[str, int],
        *,
        attempt: int,
    ) -> None:
        """Persist a per-attempt usage record for the logs page."""
        prompt_tokens = (
            job.input_tokens
            + job.cache_read_tokens
            + job.cache_creation_tokens
            - usage_snapshot["input_tokens"]
            - usage_snapshot["cache_read_tokens"]
            - usage_snapshot["cache_creation_tokens"]
        )
        completion_tokens = job.output_tokens - usage_snapshot["output_tokens"]
        duration_ms = job.duration_ms - usage_snapshot["duration_ms"]
        if prompt_tokens <= 0 and completion_tokens <= 0:
            return
        usage_tracker.record(
            provider="anthropic" if job.config.mode == "claude_code" else "custom",
            model=job.model_name or job.config.model or "unknown",
            prompt_tokens=max(0, prompt_tokens),
            completion_tokens=max(0, completion_tokens),
            job_id=job.session_id,
            stage="agent",
            attempt=attempt,
            duration_ms=max(0, duration_ms),
        )

    def _handle_stderr(self, job: TemplateAgentJob, line: str) -> None:
        message = str(line).strip()
        if not message:
            return
        job.stderr_tail.append(message)
        job.stderr_tail = job.stderr_tail[-8:]
        _safe_create_task(
            self._publish(
                job,
                {
                    "type": "stderr",
                    "stage": "agent",
                    "status": "running",
                    "message": message[:_TEXT_LIMIT],
                },
            )
        )

    async def _publish(self, job: TemplateAgentJob, event: dict[str, Any]) -> None:
        job.last_seq += 1
        payload = {
            "agent_job_id": job.id,
            "import_id": job.import_id,
            "seq": job.last_seq,
            "ts": time.time(),
            **event,
        }
        job.message = str(payload.get("message") or job.message or "")
        job.updated_at = time.time()
        job.events.append(payload)
        if len(job.events) > _EVENT_RING_SIZE:
            job.events = job.events[-_EVENT_RING_SIZE:]
        for queue in list(job.queues):
            _offer(queue, payload)

    async def _append_review_message(
        self,
        import_id: str,
        role: str,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        if not content.strip():
            return
        path = _import_dir(import_id) / "review.json"
        if not path.exists():
            return
        try:
            review = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(review, dict):
            return
        messages = review.get("conversation")
        if not isinstance(messages, list):
            messages = []
        messages.append(
            {
                "role": role,
                "content": content.strip(),
                "created_at": time.time(),
                "meta": meta or {},
            }
        )
        review["conversation"] = messages[-60:]
        _atomic_write_json(path, review)

    async def _mark_llm_satisfied(self, import_id: str) -> None:
        """Flag the review's LLM gate as satisfied so preview/confirm work.

        In agent mode there is no classic LLM call, but ``_review_template_context``
        still requires ``review.llm.status == 'complete'`` before allowing the
        downstream pipeline. The agent edits review.json directly, so we mark
        the gate as fulfilled by the agent.
        """
        path = _import_dir(import_id) / "review.json"
        if not path.exists():
            return
        try:
            review = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(review, dict):
            return
        agent_template_dir = _import_dir(import_id) / "agent_template"
        required_svgs = [
            "01_cover.svg",
            "02_toc.svg",
            "02_chapter.svg",
            "03_content.svg",
            "04_ending.svg",
        ]
        templateized = (
            (agent_template_dir / "design_spec.md").exists()
            and all((agent_template_dir / name).exists() for name in required_svgs)
        )
        llm_meta = review.get("llm") if isinstance(review.get("llm"), dict) else {}
        notes = list(llm_meta.get("notes") or []) if isinstance(llm_meta, dict) else []
        if not templateized:
            notes.append(
                "Agent run finished, but the required five SVG files and design_spec.md were not all produced."
            )
        llm_meta = {
            **llm_meta,
            "enabled": True,
            "status": "complete",
            "agent": True,
            "templateized": templateized,
            "templateized_at": time.time() if templateized else llm_meta.get("templateized_at"),
            "notes": notes[-10:],
        }
        review["llm"] = llm_meta
        _atomic_write_json(path, review)

    async def _resolve_active_annotations(self, import_id: str) -> None:
        """Mark current user annotations as handled after a successful user-sent run.

        The Agent sees unresolved annotations in ``agent_context.json`` and should
        the user should not need to click a separate "handled" control after
        sending feedback. Auto/silent inspections do not call this helper.
        """
        path = _import_dir(import_id) / "review.json"
        if not path.exists():
            return
        try:
            review = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(review, dict):
            return
        resolved_at = time.time()
        changed = False

        def resolve_list(value: Any) -> None:
            nonlocal changed
            if not isinstance(value, list):
                return
            for annotation in value:
                if not isinstance(annotation, dict) or annotation.get("resolved"):
                    continue
                annotation["resolved"] = True
                annotation["resolved_at"] = resolved_at
                annotation["resolved_by"] = "agent"
                changed = True

        resolve_list(review.get("annotations"))
        draft = review.get("draft")
        if isinstance(draft, dict):
            resolve_list(draft.get("annotations"))
        if changed:
            _atomic_write_json(path, review)


def _import_dir(import_id: str) -> Path:
    return settings.workspaces_dir / "template_imports" / import_id


# Sentinel object marking the end of the worker-loop message stream.
_WORKER_DONE = object()


async def _iter_client_in_worker_loop(prompt: str, options: Any):
    """Run ``ClaudeSDKClient`` on a dedicated background event loop and
    yield messages back to the caller's loop.

    This works around a Windows-specific issue: when uvicorn runs with
    ``--reload`` (or ``workers > 1``) the worker process is forced onto a
    ``SelectorEventLoop`` which does not support ``create_subprocess_exec``.
    The Claude Agent SDK spawns the ``claude`` CLI via
    ``anyio.open_process`` -> ``asyncio.create_subprocess_exec`` and would
    therefore raise ``NotImplementedError`` on the main loop. We sidestep that
    by running the SDK iteration on a ``ProactorEventLoop`` in a background
    thread and bridging messages through an ``asyncio.Queue`` on the caller's
    loop.
    """
    caller_loop = asyncio.get_running_loop()
    bridge: asyncio.Queue[Any] = asyncio.Queue(maxsize=64)

    cancel_event = threading.Event()
    worker_loop_holder: dict[str, asyncio.AbstractEventLoop] = {}
    ready = threading.Event()

    def _put_threadsafe(item: Any) -> None:
        # Bridge from the worker loop into the caller loop's queue.
        async def _put() -> None:
            await bridge.put(item)

        try:
            asyncio.run_coroutine_threadsafe(_put(), caller_loop).result()
        except RuntimeError:
            # Caller loop is closing; drop the message rather than crash the
            # worker loop.
            pass

    async def _drive() -> None:
        try:
            from claude_agent_sdk import ClaudeSDKClient

            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    if cancel_event.is_set():
                        break
                    _put_threadsafe(message)
        except BaseException as exc:  # noqa: BLE001 - propagate to caller loop
            if _is_false_success_error(exc):
                return
            _put_threadsafe(exc)
        finally:
            _put_threadsafe(_WORKER_DONE)

    def _worker() -> None:
        if sys.platform == "win32":
            # ProactorEventLoop is required for create_subprocess_exec.
            loop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()
        worker_loop_holder["loop"] = loop
        asyncio.set_event_loop(loop)
        ready.set()
        try:
            loop.run_until_complete(_drive())
        finally:
            try:
                # Cancel any tasks left over from the SDK before closing.
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            finally:
                loop.close()

    thread = threading.Thread(
        target=_worker, name="claude-agent-worker", daemon=True
    )
    thread.start()
    ready.wait()

    try:
        while True:
            item = await bridge.get()
            if item is _WORKER_DONE:
                break
            if isinstance(item, BaseException):
                raise item
            yield item
    except asyncio.CancelledError:
        # Propagate cancellation into the worker loop so the SDK can shut down
        # the CLI subprocess cleanly.
        cancel_event.set()
        worker_loop = worker_loop_holder.get("loop")
        if worker_loop is not None:
            for task in asyncio.all_tasks(worker_loop):
                worker_loop.call_soon_threadsafe(task.cancel)
        raise
    finally:
        # Wait briefly so the worker loop can finalize before the thread dies.
        thread.join(timeout=10.0)


def _setting_sources(config: TemplateAgentConfig) -> list[str] | None:
    if config.mode != "claude_code":
        return []
    if not config.load_project_settings:
        return ["user"]
    return ["user", "project", "local"]


def _env_for_config(config: TemplateAgentConfig) -> dict[str, str]:
    env: dict[str, str] = {}
    if config.mode != "custom":
        return env
    if config.api_key:
        env["ANTHROPIC_API_KEY"] = config.api_key
    if config.auth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = config.auth_token
        env["ANTHROPIC_AUTH_TOKEN"] = config.auth_token
    if config.base_url:
        env["ANTHROPIC_BASE_URL"] = config.base_url
    if config.model:
        env["ANTHROPIC_MODEL"] = config.model
    if config.custom_model_option:
        env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = config.custom_model_option
    return env


def _review_snapshot(import_dir: Path) -> str:
    """Build a compact, human-readable snapshot of the current review draft.

    The agent has Read access and can always re-read review.json, but giving
    it a structured snapshot up-front prevents wasted turns on discovery and
    lets the very first reply ground itself in real workspace state.
    """
    review_path = import_dir / "review.json"
    if not review_path.exists():
        return "(review.json not found)"

    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "(review.json could not be read)"
    if not isinstance(review, dict):
        return "(review.json is malformed)"

    schema = review.get("schema_version", 1)
    template_id = review.get("template_id") or "(unset)"
    label = review.get("label") or review.get("draft", {}).get("label") or "(unset)"
    slide_count = review.get("slide_count")

    draft = review.get("draft") if isinstance(review.get("draft"), dict) else {}
    page_selections = draft.get("page_selections") or review.get("page_selections") or {}
    page_types = review.get("page_types") or draft.get("page_types") or {}
    annotations = review.get("annotations") or draft.get("annotations") or []
    assets = review.get("assets") or draft.get("assets") or []
    preserve_texts = draft.get("preserve_texts") or review.get("preserve_texts") or []
    placeholder_hints = draft.get("placeholder_hints") or review.get("placeholder_hints") or []
    element_actions = draft.get("element_actions") or review.get("element_actions") or []
    design_spec_present = bool(draft.get("design_spec") or review.get("design_spec"))
    llm = review.get("llm") if isinstance(review.get("llm"), dict) else {}
    llm_status = llm.get("status") or ("ran" if llm.get("enabled") else "not run")

    lines: list[str] = []
    lines.append(f"- schema_version: {schema}")
    lines.append(f"- template_id: {template_id}")
    lines.append(f"- label: {label}")
    if slide_count is not None:
        lines.append(f"- slide_count: {slide_count}")
    lines.append(f"- design_spec_present: {design_spec_present}")
    lines.append(f"- llm.assist_status: {llm_status}")
    lines.append(f"- annotations: {len(annotations)}")
    lines.append(f"- assets (entries): {len(assets)}")
    lines.append(f"- preserve_texts: {len(preserve_texts)}")
    lines.append(f"- placeholder_hints: {len(placeholder_hints)}")
    lines.append(f"- element_actions: {len(element_actions)}")

    if isinstance(page_selections, dict) and page_selections:
        sel_summary = ", ".join(f"{k}={v}" for k, v in page_selections.items())
        lines.append(f"- page_selections: {sel_summary}")

    if isinstance(page_types, dict) and page_types:
        # Page types are usually keyed by slide index; show a compact summary.
        type_buckets: dict[str, list[str]] = {}
        for slide, ptype in page_types.items():
            type_buckets.setdefault(str(ptype or "unknown"), []).append(str(slide))
        bucket_lines = "; ".join(
            f"{ptype}: {', '.join(items[:8])}{'…' if len(items) > 8 else ''}"
            for ptype, items in type_buckets.items()
        )
        lines.append(f"- page_types: {bucket_lines}")

    if isinstance(annotations, list) and annotations:
        # Show up to 5 unresolved annotations as the most relevant feedback.
        preview: list[str] = []
        for ann in annotations[:5]:
            if not isinstance(ann, dict):
                continue
            slide = ann.get("slide_index", "?")
            note = (ann.get("note") or "").strip().replace("\n", " ")
            if len(note) > 100:
                note = note[:99] + "…"
            resolved = "✓" if ann.get("resolved") else "·"
            preview.append(f"  [{resolved}] slide {slide}: {note}")
        if preview:
            lines.append("- annotations preview:")
            lines.extend(preview)

    return "\n".join(lines)


def _write_agent_context(import_dir: Path, *, planning: bool = True) -> None:
    """Write a lightweight context file so Agent never has to read huge blobs."""
    review_path = import_dir / "review.json"
    if not review_path.exists():
        return
    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(review, dict):
        return

    sidecar_slides: dict[int, dict[str, Any]] = {}
    try:
        sidecar = _read_review_manifest(import_dir)
        if isinstance(sidecar, dict):
            sidecar_slides = {
                int(slide.get("index")): slide
                for slide in list(sidecar.get("slides") or [])
                if isinstance(slide, dict) and slide.get("index")
            }
    except (TypeError, ValueError):
        sidecar_slides = {}

    slides: list[dict[str, Any]] = []
    for raw in list(review.get("slides") or [])[:200]:
        if not isinstance(raw, dict):
            continue
        idx = raw.get("index")
        sidecar_slide = sidecar_slides.get(int(idx)) if isinstance(idx, int) else {}
        sidecar_slide = sidecar_slide or {}
        elements = raw.get("elements") or sidecar_slide.get("elements") or []
        slides.append(
            {
                "index": idx,
                "page_type": raw.get("page_type"),
                "text_samples": list(raw.get("text_samples") or [])[:3],
                "element_count": len(elements),
                "svg_path": f"svg/slide_{int(idx):02d}.svg" if isinstance(idx, int) else None,
            }
        )

    assets: list[dict[str, Any]] = []
    raw_assets = review.get("assets") or []
    if isinstance(raw_assets, dict):
        raw_assets = list(raw_assets.values())
    for raw in list(raw_assets)[:30]:
        if not isinstance(raw, dict):
            continue
        assets.append(
            {
                "asset_id": raw.get("asset_id"),
                "file_name": raw.get("file_name") or raw.get("name"),
                "role": raw.get("role") or raw.get("recommended_role"),
                "recommended_role": raw.get("recommended_role"),
                "page_count": len(raw.get("pages") or []),
                "sample_pages": list(raw.get("pages") or [])[:6],
                "usage_count": raw.get("usage_count"),
                "position_stable": raw.get("position_stable"),
            }
        )

    visual_brief = _build_agent_visual_brief(import_dir, review)
    agent_task = _build_agent_task(import_dir, review, visual_brief, planning=planning)
    _write_agent_design_spec_reference(import_dir)

    context = {
        "schema_version": 2,
        "import_id": review.get("import_id"),
        "template_id": review.get("template_id"),
        "label": review.get("label"),
        "status": review.get("status"),
        "slide_count": review.get("slide_count"),
        "llm": review.get("llm") if isinstance(review.get("llm"), dict) else {},
        "page_type_candidates": review.get("page_type_candidates") or {},
        "draft_summary": _agent_draft_summary(review),
        "annotations": [
            entry
            for entry in list(review.get("annotations") or [])
            if isinstance(entry, dict) and not entry.get("resolved")
        ][:80],
        "slides": slides,
        "asset_summary": assets[:120],
        "agent_task_file": "agent_task.json",
        "visual_brief_file": "agent_visual_brief.json",
        "files": {
            "editable_review": "review.json",
            "design_spec_reference": "agent_design_spec_reference.md",
            "source_svgs": "pptist/agent_source_svg/slide-XXX.svg when available; otherwise work/svg/slide_XX.svg",
            "agent_output_dir": "agent_template/",
        },
    }
    _atomic_write_json(import_dir / "agent_context.json", context)
    _atomic_write_json(import_dir / "agent_task.json", agent_task)
    _atomic_write_json(import_dir / "agent_visual_brief.json", visual_brief)


def _write_agent_design_spec_reference(import_dir: Path) -> None:
    reference_path = settings.templates_dir / "design_spec_reference.md"
    try:
        text = reference_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if not text:
        return
    (import_dir / "agent_design_spec_reference.md").write_text(text, encoding="utf-8")


def _agent_draft_summary(review: dict[str, Any]) -> dict[str, Any]:
    draft = review.get("draft") if isinstance(review.get("draft"), dict) else {}
    return {
        "label": draft.get("label") or review.get("label"),
        "page_selections": draft.get("page_selections") or review.get("page_selections") or {},
        "assets_count": len(draft.get("assets") or review.get("assets") or []),
        "preserve_texts_count": len(draft.get("preserve_texts") or []),
        "placeholder_hints_count": len(draft.get("placeholder_hints") or {}),
        "element_actions_count": len(draft.get("element_actions") or []),
        "design_spec_present": bool(draft.get("design_spec") or review.get("design_spec")),
    }


def _build_agent_task(
    import_dir: Path,
    review: dict[str, Any],
    visual_brief: dict[str, Any],
    *,
    planning: bool = True,
) -> dict[str, Any]:
    pages = []
    for page in list(visual_brief.get("selected_pages") or []):
        if not isinstance(page, dict):
            continue
        orientation = page.get("source_orientation") if isinstance(page.get("source_orientation"), dict) else {}
        pages.append(
            {
                "page_type": page.get("page_type"),
                "slide_index": page.get("slide_index"),
                "source_svg": page.get("source_svg"),
                "source_svg_exists": page.get("source_svg_exists"),
                "agent_template_svg": page.get("agent_template_svg"),
                "agent_template_svg_exists": page.get("agent_template_svg_exists"),
                "source_svg_bytes": page.get("source_svg_bytes"),
                "agent_template_svg_bytes": page.get("agent_template_svg_bytes"),
                "orientation": {
                    "visual_y_axis": orientation.get("visual_y_axis"),
                    "raw_y_axis_reliable": orientation.get("raw_y_axis_reliable"),
                    "negative_y_transform_count": orientation.get("negative_y_transform_count"),
                    "top_prominent": list(orientation.get("top_prominent") or [])[:4],
                    "bottom_prominent": list(orientation.get("bottom_prominent") or [])[:4],
                },
            }
        )
    if not planning:
        return {
            "schema_version": 1,
            "workflow": "read_only_template_inspection",
            "template_contract": _agent_template_contract(import_dir),
            "rules": [
                "Read-only run: do not create, edit, delete, or overwrite any file.",
                "Inspect page_selections, slide summaries, unresolved annotations, and source SVG availability.",
                "page_selections are representative layout picks, not labels for every matching source slide.",
                "Check whether the five selected representative slides fit cover, toc, chapter, content, and ending roles.",
                "Identify obvious dirty original content that the clean template builder should remove.",
                "Give concise guidance and end by asking whether to start templateization.",
            ],
            "pages": pages,
            "required_reads": [
                {
                    "page_type": page.get("page_type"),
                    "slide_index": page.get("slide_index"),
                    "source_svg": page.get("source_svg"),
                    "source_svg_exists": page.get("source_svg_exists"),
                }
                for page in pages
            ],
            "review_updates": [],
        }

    return {
        "schema_version": 1,
        "workflow": "source_derived_minimal_edit",
        "template_contract": _agent_template_contract(import_dir),
        "rules": [
            "Read every source_svg and agent_template_svg listed below before editing.",
            "Edit the seeded agent_template SVGs; do not create a blank/new layout.",
            "Preserve source chrome, logos, backgrounds, bands, and navigation unless the user explicitly asks to change them.",
            "Preserve repeated logos and brand marks that appear in the same nearby position across slides; do not remove them as body content.",
            "Keep every SVG visually upright on a normal top-left-origin 16:9 canvas; do not rely on flipped raw-coordinate interpretations.",
            "Replace reusable content/text with the standard placeholder contract, keeping alignment, spacing, and reading order tidy.",
            "When replacing a source text node with a placeholder, keep that node's x/y, text-anchor, fill, font-family, font-size, font-weight, and surrounding bbox unless the user explicitly asks for a redesign.",
            "Place placeholders in the reusable visual zones; avoid drifting into logos, navigation, or decorative chrome.",
            "Use only these new placeholder names: cover={{TITLE}}, {{SUBTITLE}}, {{AUTHOR}}, {{DATE}}, {{GROUP}}; toc={{TOC_ITEM_1}}..{{TOC_ITEM_5}}; chapter={{CHAPTER_TITLE}}, {{CHAPTER_NUMBER}}; content={{PAGE_TITLE}}, {{CONTENT_AREA}}; ending={{ENDING_TITLE}}, {{ENDING_MESSAGE}}.",
            "Write agent_template/manifest.patch.json with source_kind=pptist_import, clean_generation_ready=true, page_selections, changed page types, and warnings.",
        ],
        "pages": pages,
        "required_reads": [
            {
                "page_type": page.get("page_type"),
                "slide_index": page.get("slide_index"),
                "source_svg": page.get("source_svg"),
                "agent_template_svg": page.get("agent_template_svg"),
            }
            for page in pages
        ],
        "review_updates": [
            "draft.design_spec must be a complete Markdown design_spec.md following agent_design_spec_reference.md sections I through XI",
            "draft.assets/assets only when asset roles change",
            "draft.element_actions only when an element is intentionally removed or replaced",
        ],
    }


def _build_agent_visual_brief(import_dir: Path, review: dict[str, Any]) -> dict[str, Any]:
    selections = (review.get("draft") or {}).get("page_selections") or {}
    pages: list[dict[str, Any]] = []
    required_reads: list[dict[str, Any]] = []
    for page_type in ("cover", "toc", "chapter", "content", "ending"):
        index = selections.get(page_type)
        if not isinstance(index, int) or index <= 0:
            continue
        source = _agent_source_svg_path(import_dir, index)
        source_rel = _relative_path(import_dir, source) if source else None
        template_path = import_dir / "agent_template" / _agent_template_file_for_type(page_type)
        template_rel = _relative_path(import_dir, template_path)
        visual_geometry = _analyze_svg_visual_geometry(source) if source else {}
        pages.append(
            {
                "page_type": page_type,
                "slide_index": index,
                "source_svg": source_rel,
                "source_svg_exists": bool(source and source.exists()),
                "source_svg_bytes": _file_size(source),
                "agent_template_svg": template_rel,
                "agent_template_svg_exists": template_path.exists(),
                "agent_template_svg_bytes": _file_size(template_path),
                "source_orientation": visual_geometry.get("orientation", {}),
                "source_visual_elements": visual_geometry.get("elements", []),
            }
        )
        required_reads.append(
            {
                "page_type": page_type,
                "slide_index": index,
                "source_svg": source_rel,
                "source_svg_exists": bool(source and source.exists()),
                "source_svg_bytes": _file_size(source),
                "agent_template_svg": template_rel,
                "agent_template_svg_exists": template_path.exists(),
                "agent_template_svg_bytes": _file_size(template_path),
                "required_before_template_edit": True,
                "read_expectation": (
                    "Read the complete source_svg and, when it exists, the complete "
                    "agent_template_svg before editing this page type."
                ),
            }
        )
    return {
        "coordinate_contract": (
            "source_svg elements are summarized after applying their own matrix transforms; "
            "top/middle/bottom positions are visual positions. agent_template SVGs should use normal SVG coordinates."
        ),
        "direction_contract": (
            "source_orientation is computed deterministically from inherited SVG transforms. "
            "Use visual_zone/top/bottom and visual_top_down facts for direction; raw SVG y values are not reliable."
        ),
        "selected_pages": pages,
        "required_full_page_reads": required_reads,
        "source_derived_template_contract": _agent_template_contract(import_dir),
    }


def _read_review_manifest(import_dir: Path) -> dict[str, Any]:
    for path in (import_dir / "review_manifest.json", import_dir / "work" / "review_manifest.json"):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(manifest, dict):
            return manifest
    return {}


def _agent_template_contract(import_dir: Path) -> dict[str, Any]:
    path = import_dir / "agent_template" / "source_map.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "mode": "source_derived",
            "status": "not_seeded",
            "baseline_rule": "Agent template SVGs should be edited from copied source slides, not recreated from blank layouts.",
        }
    return data if isinstance(data, dict) else {}


def _ensure_pptist_agent_source_svgs(import_dir: Path) -> None:
    """Materialize upright source SVGs from the saved PPTist deck."""

    deck_path = import_dir / "pptist" / "deck.json"
    try:
        deck = json.loads(deck_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(deck, dict):
        return
    slides = deck.get("slides")
    if not isinstance(slides, list):
        return

    canvas_w = int(round(_floatish(deck.get("width"), 1280))) or 1280
    canvas_h = int(round(_floatish(deck.get("height"), 720))) or 720
    output_dir = import_dir / "pptist" / "agent_source_svg"
    output_dir.mkdir(parents=True, exist_ok=True)
    for idx, slide in enumerate(slides, start=1):
        if not isinstance(slide, dict):
            continue
        svg_text = _pptist_slide_to_agent_source_svg(slide, idx, canvas_w, canvas_h)
        (output_dir / f"slide-{idx:03d}.svg").write_text(svg_text, encoding="utf-8")


def _floatish(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pptist_box(el: dict[str, Any]) -> dict[str, float]:
    return {
        "x": _floatish(el.get("left")),
        "y": _floatish(el.get("top")),
        "width": max(1.0, _floatish(el.get("width"), 1.0)),
        "height": max(1.0, _floatish(el.get("height"), 1.0)),
    }


def _pptist_svg_attr(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _pptist_plain_text(value: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return html.unescape(" ".join(text.split()))


def _pptist_parse_style(style: str | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for chunk in str(style or "").split(";"):
        if ":" not in chunk:
            continue
        key, value = chunk.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            parsed[key] = value
    return parsed


def _pptist_anchor_from_align(value: Any) -> str | None:
    align = str(value or "").strip().lower()
    if align in {"center", "middle"}:
        return "middle"
    if align in {"right", "end"}:
        return "end"
    if align in {"left", "start"}:
        return "start"
    return None


def _pptist_font_size(value: Any, fallback: float) -> float:
    if value is None:
        return fallback
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    if not match:
        return fallback
    return max(6.0, min(120.0, float(match.group(0))))


def _pptist_font_weight(value: Any, fallback: str = "400") -> str:
    raw = str(value or "").strip().lower()
    if raw in {"bold", "bolder"}:
        return "700"
    if raw in {"normal", "regular"}:
        return "400"
    return str(value).strip() if raw else fallback


def _pptist_font_family(value: Any, fallback: str = "Arial") -> str:
    raw = str(value or "").strip().strip("\"'")
    return raw or fallback


class _PptistRichTextParser(HTMLParser):
    def __init__(
        self,
        *,
        default_color: str,
        default_font: str,
        default_weight: str,
        align_hint: str | None,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.default_style = {
            "color": default_color,
            "font-family": default_font or "Arial",
            "font-weight": default_weight,
        }
        self.align_hint = _pptist_anchor_from_align(align_hint)
        self.style_stack: list[dict[str, str]] = []
        self.current_align: str | None = self.align_hint
        self.current_segments: list[tuple[str, dict[str, str]]] = []
        self.lines: list[dict[str, Any]] = []

    def _merged_style(self) -> dict[str, str]:
        merged = dict(self.default_style)
        for style in self.style_stack:
            merged.update(style)
        return merged

    def _finish_line(self) -> None:
        text = "".join(segment for segment, _style in self.current_segments)
        if text.strip():
            first_style = next(
                (style for segment, style in self.current_segments if segment.strip()),
                self._merged_style(),
            )
            self.lines.append(
                {
                    "text": html.unescape(" ".join(text.split())),
                    "anchor": self.current_align or self.align_hint or "start",
                    "color": first_style.get("color") or self.default_style["color"],
                    "font": _pptist_font_family(first_style.get("font-family"), self.default_style["font-family"]),
                    "font_size": _pptist_font_size(first_style.get("font-size"), 0.0),
                    "weight": _pptist_font_weight(first_style.get("font-weight"), self.default_style["font-weight"]),
                }
            )
        self.current_segments = []
        self.current_align = self.align_hint

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value for key, value in attrs}
        style = _pptist_parse_style(attr_map.get("style"))
        tag = tag.lower()
        if tag in {"p", "div"}:
            self._finish_line()
            self.current_align = _pptist_anchor_from_align(style.get("text-align")) or self.align_hint
        self.style_stack.append(style)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"p", "div"}:
            self._finish_line()
        if self.style_stack:
            self.style_stack.pop()

    def handle_data(self, data: str) -> None:
        if data:
            self.current_segments.append((data, self._merged_style()))

    def close(self) -> None:
        super().close()
        self._finish_line()


def _pptist_rich_text_lines(
    content: Any,
    *,
    default_color: str,
    default_font: str,
    default_weight: str = "400",
    align_hint: str | None = None,
) -> list[dict[str, Any]]:
    raw = str(content or "")
    if "<" not in raw or ">" not in raw:
        text = _pptist_plain_text(raw)
        return [
            {
                "text": text,
                "anchor": _pptist_anchor_from_align(align_hint) or "start",
                "color": default_color,
                "font": default_font or "Arial",
                "font_size": 0.0,
                "weight": default_weight,
            }
        ] if text else []
    parser = _PptistRichTextParser(
        default_color=default_color,
        default_font=default_font or "Arial",
        default_weight=default_weight,
        align_hint=align_hint,
    )
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        text = _pptist_plain_text(raw)
        return [
            {
                "text": text,
                "anchor": _pptist_anchor_from_align(align_hint) or "start",
                "color": default_color,
                "font": default_font or "Arial",
                "font_size": 0.0,
                "weight": default_weight,
            }
        ] if text else []
    return parser.lines


def _pptist_outline_attrs(outline: Any) -> str:
    if not isinstance(outline, dict) or outline.get("style") == "none":
        return 'stroke="none"'
    color = outline.get("color") or "#111827"
    width = _floatish(outline.get("width"), 1.0)
    return f'stroke="{_pptist_svg_attr(color)}" stroke-width="{width:.2f}"'


def _pptist_rotation_attr(el: dict[str, Any], box: dict[str, float]) -> str:
    rotate = _floatish(el.get("rotate"))
    flip_h = bool(el.get("flipH"))
    flip_v = bool(el.get("flipV"))
    if not rotate and not flip_h and not flip_v:
        return ""
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    transforms: list[str] = []
    if rotate:
        transforms.append(f"rotate({rotate:.2f} {cx:.2f} {cy:.2f})")
    if flip_h or flip_v:
        sx = -1 if flip_h else 1
        sy = -1 if flip_v else 1
        transforms.append(
            f"translate({cx:.2f} {cy:.2f}) scale({sx} {sy}) translate({-cx:.2f} {-cy:.2f})"
        )
    return f' transform="{" ".join(transforms)}"'


def _pptist_background_svg(slide: dict[str, Any], canvas_w: int, canvas_h: int) -> str:
    background = slide.get("background")
    if not isinstance(background, dict):
        return f'<rect width="{canvas_w}" height="{canvas_h}" fill="#ffffff"/>'
    if background.get("type") == "image" and isinstance(background.get("image"), dict):
        src = background["image"].get("src")
        if src:
            return (
                f'<rect width="{canvas_w}" height="{canvas_h}" fill="#ffffff"/>'
                f'<image href="{_pptist_svg_attr(src)}" x="0" y="0" width="{canvas_w}" height="{canvas_h}" '
                'preserveAspectRatio="xMidYMid slice"/>'
            )
    color = background.get("color") or "#ffffff"
    return f'<rect width="{canvas_w}" height="{canvas_h}" fill="{_pptist_svg_attr(color)}"/>'


def _pptist_text_svg(
    content: Any,
    box: dict[str, float],
    *,
    color: str = "#111827",
    font: str = "Arial",
    weight: str = "400",
    anchor: str = "start",
    align_hint: str | None = None,
    rotate: str = "",
) -> str:
    if not content:
        return ""
    rich_lines = _pptist_rich_text_lines(
        content,
        default_color=color,
        default_font=font,
        default_weight=weight,
        align_hint=align_hint or anchor,
    )[:4]
    if not rich_lines:
        return ""
    fallback_size = max(10.0, min(54.0, box["height"] / max(1, len(rich_lines)) * 0.56))
    for line in rich_lines:
        if not line.get("font_size"):
            line["font_size"] = fallback_size
    first = rich_lines[0]
    anchor = str(first.get("anchor") or anchor or "start")
    font_size = float(first.get("font_size") or fallback_size)
    x = box["x"] + (box["width"] / 2 if anchor == "middle" else box["width"] if anchor == "end" else 0)
    line_heights = [float(line.get("font_size") or font_size) * 1.18 for line in rich_lines]
    total_line_height = sum(line_heights)
    y = box["y"] + max(0.0, (box["height"] - total_line_height) / 2.0) + font_size * 0.86
    tspans = []
    for idx, line in enumerate(rich_lines):
        current_size = float(line.get("font_size") or font_size)
        dy = "0" if idx == 0 else f"{line_heights[idx - 1]:.2f}"
        tspan_attrs = [
            f'x="{x:.2f}"',
            f'dy="{dy}"',
            f'font-size="{current_size:.1f}"',
        ]
        line_color = str(line.get("color") or color)
        if line_color:
            tspan_attrs.append(f'fill="{_pptist_svg_attr(line_color)}"')
        line_font = str(line.get("font") or font or "Arial")
        if line_font:
            tspan_attrs.append(f'font-family="{_pptist_svg_attr(line_font)}, sans-serif"')
        line_weight = str(line.get("weight") or weight or "400")
        if line_weight:
            tspan_attrs.append(f'font-weight="{_pptist_svg_attr(line_weight)}"')
        tspans.append(
            f'<tspan {" ".join(tspan_attrs)}>{html.escape(str(line.get("text") or "")[:220])}</tspan>'
        )
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" text-anchor="{anchor}" '
        f'data-pptist-box="{box["x"]:.2f} {box["y"]:.2f} {box["width"]:.2f} {box["height"]:.2f}" '
        f'fill="{_pptist_svg_attr(str(first.get("color") or color))}" '
        f'font-family="{_pptist_svg_attr(str(first.get("font") or font or "Arial"))}, sans-serif" '
        f'font-size="{font_size:.1f}" font-weight="{_pptist_svg_attr(str(first.get("weight") or weight))}"{rotate}>'
        f'{"".join(tspans)}</text>'
    )


def _pptist_element_to_agent_source_svg(el: dict[str, Any]) -> str:
    box = _pptist_box(el)
    rotate = _pptist_rotation_attr(el, box)
    el_type = el.get("type")
    data_id = _pptist_svg_attr(el.get("id"))
    prefix = f'<g data-pptist-id="{data_id}" data-pptist-type="{_pptist_svg_attr(el_type)}">'
    suffix = "</g>"

    if el_type == "image" and el.get("src"):
        return (
            f'{prefix}<image href="{_pptist_svg_attr(el.get("src"))}" x="{box["x"]:.2f}" y="{box["y"]:.2f}" '
            f'width="{box["width"]:.2f}" height="{box["height"]:.2f}" preserveAspectRatio="xMidYMid meet"{rotate}/>{suffix}'
        )

    if el_type == "shape" and el.get("path"):
        view_box = el.get("viewBox") if isinstance(el.get("viewBox"), list) else [1000, 1000]
        vb_w = max(1.0, _floatish(view_box[0] if len(view_box) > 0 else 1000, 1000))
        vb_h = max(1.0, _floatish(view_box[1] if len(view_box) > 1 else 1000, 1000))
        fill = el.get("fill") or "none"
        opacity = _floatish(el.get("opacity"), 1.0)
        shape = (
            f'<g{rotate}><g transform="translate({box["x"]:.2f} {box["y"]:.2f}) '
            f'scale({box["width"] / vb_w:.6f} {box["height"] / vb_h:.6f})">'
            f'<path d="{_pptist_svg_attr(el.get("path"))}" fill="{_pptist_svg_attr(fill)}" '
            f'{_pptist_outline_attrs(el.get("outline"))} opacity="{opacity:.3f}"/></g></g>'
        )
        text = ""
        raw_text = el.get("text")
        if isinstance(raw_text, dict):
            anchor = "middle" if raw_text.get("align") in {"center", "middle"} else "start"
            text = _pptist_text_svg(
                raw_text.get("content"),
                box,
                color=str(raw_text.get("defaultColor") or "#111827"),
                font=str(raw_text.get("defaultFontName") or "Arial"),
                weight="600",
                anchor=anchor,
                align_hint=str(raw_text.get("align") or ""),
                rotate=rotate,
            )
        return f"{prefix}{shape}{text}{suffix}"

    if el_type == "line":
        start = el.get("start") if isinstance(el.get("start"), list) else [box["x"], box["y"]]
        end = el.get("end") if isinstance(el.get("end"), list) else [box["x"] + box["width"], box["y"]]
        color = el.get("color") or "#111827"
        width = max(1.0, _floatish(el.get("width"), 2.0))
        return (
            f'{prefix}<line x1="{_floatish(start[0]):.2f}" y1="{_floatish(start[1]):.2f}" '
            f'x2="{_floatish(end[0]):.2f}" y2="{_floatish(end[1]):.2f}" '
            f'stroke="{_pptist_svg_attr(color)}" stroke-width="{width:.2f}"{rotate}/>{suffix}'
        )

    if el_type == "text":
        return (
            prefix
            + _pptist_text_svg(
                el.get("content"),
                box,
                color=str(el.get("defaultColor") or "#111827"),
                font=str(el.get("defaultFontName") or "Arial"),
                align_hint=None,
                rotate=rotate,
            )
            + suffix
        )

    return ""


def _pptist_slide_to_agent_source_svg(
    slide: dict[str, Any],
    slide_index: int,
    canvas_w: int,
    canvas_h: int,
) -> str:
    parts = [_pptist_background_svg(slide, canvas_w, canvas_h)]
    for raw in slide.get("elements") or []:
        if isinstance(raw, dict):
            markup = _pptist_element_to_agent_source_svg(raw)
            if markup:
                parts.append(markup)
    body = "\n  ".join(parts)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {canvas_w} {canvas_h}" '
        f'width="{canvas_w}" height="{canvas_h}" data-source="pptist-deck" data-slide-index="{slide_index}">\n'
        f"  {body}\n"
        "</svg>\n"
    )


def _seed_agent_template_sources(import_dir: Path) -> None:
    """Create source-derived Agent template baselines without overwriting edits.

    Agent mode should start from the imported slide SVG geometry. This prevents
    freehand rearrangement: files in ``agent_template/`` already contain the
    selected source pages, so the Agent's job is to edit/placehold the copied
    structure instead of inventing a new layout.
    """
    _ensure_pptist_agent_source_svgs(import_dir)
    review_path = import_dir / "review.json"
    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(review, dict):
        return

    selections = (review.get("draft") or {}).get("page_selections") or {}
    if not isinstance(selections, dict):
        return

    agent_dir = import_dir / "agent_template"
    source_dir = agent_dir / "source_reference"
    agent_dir.mkdir(parents=True, exist_ok=True)
    source_dir.mkdir(parents=True, exist_ok=True)

    assets_src = import_dir / "work" / "assets"
    assets_dest = agent_dir / "assets"
    if assets_src.is_dir() and not assets_dest.exists():
        shutil.copytree(assets_src, assets_dest)

    existing_map = _agent_template_contract(import_dir)
    previous_entries = {
        str(entry.get("page_type")): entry
        for entry in list(existing_map.get("pages") or [])
        if isinstance(entry, dict) and entry.get("page_type")
    }
    pages: list[dict[str, Any]] = []

    for page_type in ("cover", "toc", "chapter", "content", "ending"):
        index = selections.get(page_type)
        if not isinstance(index, int) or index <= 0:
            old = previous_entries.get(page_type)
            if old:
                pages.append(old)
            continue
        source = _agent_source_svg_path(import_dir, index)
        if source is None:
            continue
        target = agent_dir / _agent_template_file_for_type(page_type)
        reference = source_dir / f"{page_type}_slide_{index:02d}.svg"
        source_text = source.read_text(encoding="utf-8")
        source_hash = hashlib.sha1(source_text.encode("utf-8")).hexdigest()

        reference.write_text(source_text, encoding="utf-8")

        created = False
        archived_previous = None
        previous_entry = previous_entries.get(page_type)
        if target.exists() and _agent_svg_source_matches(
            target,
            page_type,
            index,
            source_hash,
            previous_entry=previous_entry,
        ):
            _strip_agent_source_markers_in_file(target)
        elif target.exists():
            archived_previous = _archive_agent_template_file(agent_dir, target, page_type)
        if not target.exists():
            target.write_text(
                _strip_agent_source_markers(_rebase_agent_template_svg(source_text)),
                encoding="utf-8",
            )
            created = True

        pages.append(
            {
                "page_type": page_type,
                "slide_index": index,
                "source_svg": _relative_path(import_dir, source),
                "source_reference_svg": _relative_path(import_dir, reference),
                "agent_template_svg": _relative_path(import_dir, target),
                "source_sha1": source_hash,
                "agent_template_existed": not created,
                "archived_previous": archived_previous,
                "baseline_policy": "edit_source_copy_minimally",
            }
        )

    contract = {
        "schema_version": 1,
        "mode": "source_derived",
        "status": "seeded",
        "created_at": time.time(),
        "baseline_rule": (
            "Agent template SVGs are copied from selected source slides. Preserve source "
            "geometry and chrome; replace content with placeholders by editing the copied "
            "SVGs instead of redrawing layouts from scratch."
        ),
        "pages": pages,
    }
    _atomic_write_json(agent_dir / "source_map.json", contract)


def _rebase_agent_template_svg(svg_text: str) -> str:
    return svg_text.replace("../assets/", "assets/").replace("..\\assets\\", "assets\\")


_AGENT_SOURCE_MARKER_RE = re.compile(r"<!--\s*agent-source\b[\s\S]*?-->\s*", re.IGNORECASE)


def _strip_agent_source_markers(svg_text: str) -> str:
    return _AGENT_SOURCE_MARKER_RE.sub("", svg_text)


def _strip_agent_source_markers_in_file(path: Path) -> None:
    try:
        svg_text = path.read_text(encoding="utf-8")
    except OSError:
        return
    cleaned = _strip_agent_source_markers(svg_text)
    if cleaned != svg_text:
        path.write_text(cleaned, encoding="utf-8")


def _read_agent_source_marker(svg_text: str) -> dict[str, str]:
    match = re.search(r"<!--\s*agent-source\s+([^>]*)-->", svg_text)
    if not match:
        return {}
    return {
        key: value
        for key, value in re.findall(r'([a-zA-Z0-9_-]+)="([^"]*)"', match.group(1))
    }


def _agent_svg_source_matches(
    path: Path,
    page_type: str,
    slide_index: int,
    source_sha1: str,
    *,
    previous_entry: dict[str, Any] | None = None,
) -> bool:
    if previous_entry:
        return (
            previous_entry.get("page_type") == page_type
            and previous_entry.get("slide_index") == slide_index
            and previous_entry.get("source_sha1") == source_sha1
        )
    try:
        marker = _read_agent_source_marker(path.read_text(encoding="utf-8"))
    except OSError:
        return False
    return (
        marker.get("page_type") == page_type
        and marker.get("slide_index") == str(slide_index)
        and marker.get("source_sha1") == source_sha1
    )


def _archive_agent_template_file(agent_dir: Path, path: Path, page_type: str) -> str | None:
    if not path.exists():
        return None
    archive_dir = agent_dir / "archive" / str(int(time.time()))
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived = archive_dir / f"{page_type}_{path.name}"
    counter = 1
    while archived.exists():
        archived = archive_dir / f"{page_type}_{counter}_{path.name}"
        counter += 1
    shutil.move(str(path), str(archived))
    try:
        return archived.relative_to(agent_dir.parent).as_posix()
    except ValueError:
        return archived.as_posix()


def _file_size(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        return path.stat().st_size
    except OSError:
        return None


def _agent_template_file_for_type(page_type: str) -> str:
    return {
        "cover": "01_cover.svg",
        "toc": "02_toc.svg",
        "chapter": "02_chapter.svg",
        "content": "03_content.svg",
        "ending": "04_ending.svg",
    }.get(page_type, f"{page_type}.svg")


def _agent_source_svg_path(import_dir: Path, index: int) -> Path | None:
    for base in (
        import_dir / "pptist" / "agent_source_svg",
        import_dir / "work" / "svg",
        import_dir / "svg",
        import_dir / "work" / "svg_raw",
        import_dir / "svg_raw",
    ):
        for name in (f"slide_{index:02d}.svg", f"slide_{index:03d}.svg", f"slide-{index:02d}.svg", f"slide-{index:03d}.svg"):
            path = base / name
            if path.exists():
                return path
    return None


def _relative_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _analyze_svg_visual_geometry(path: Path, limit: int = 45) -> dict[str, Any]:
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError):
        return {"elements": [], "orientation": {}}
    canvas = _svg_canvas(root)
    records: list[dict[str, Any]] = []
    transform_stats = {
        "element_count": 0,
        "negative_y_transform_count": 0,
        "negative_x_transform_count": 0,
        "non_identity_transform_count": 0,
    }

    def visit(elem: ET.Element, inherited: tuple[float, float, float, float, float, float]) -> None:
        local = _parse_svg_transform(elem.attrib.get("transform"))
        matrix = _multiply_svg_matrix(inherited, local)
        if matrix != (1, 0, 0, 1, 0, 0):
            transform_stats["non_identity_transform_count"] += 1
        if matrix[3] < 0:
            transform_stats["negative_y_transform_count"] += 1
        if matrix[0] < 0:
            transform_stats["negative_x_transform_count"] += 1
        tag = elem.tag.rsplit("}", 1)[-1].lower()
        if tag in {"path", "rect", "image", "text", "polygon", "polyline", "line", "circle", "ellipse"}:
            transform_stats["element_count"] += 1
            bbox = _svg_element_bbox(elem)
            if bbox is not None:
                bbox = _apply_svg_matrix(bbox, matrix)
                width = max(0.0, bbox[2] - bbox[0])
                height = max(0.0, bbox[3] - bbox[1])
                if width > 1 and height > 1:
                    area = width * height
                    text = "".join(elem.itertext()).strip()
                    y_mid = (bbox[1] + bbox[3]) / 2
                    fill = elem.attrib.get("fill")
                    font_size = elem.attrib.get("font-size")
                    text_anchor = elem.attrib.get("text-anchor")
                    if tag == "text":
                        for child in list(elem):
                            child_tag = child.tag.rsplit("}", 1)[-1].lower()
                            if child_tag != "tspan":
                                continue
                            fill = fill or child.attrib.get("fill")
                            font_size = font_size or child.attrib.get("font-size")
                            text_anchor = text_anchor or child.attrib.get("text-anchor")
                    records.append(
                        {
                            "tag": tag,
                            "text": text[:80] if text else None,
                            "fill": fill,
                            "font_size": font_size,
                            "text_anchor": text_anchor,
                            "opacity": elem.attrib.get("opacity") or elem.attrib.get("fill-opacity"),
                            "bbox_px": [round(bbox[0], 1), round(bbox[1], 1), round(width, 1), round(height, 1)],
                            "visual_zone": (
                                "top"
                                if y_mid < canvas[1] * 0.28
                                else "bottom"
                                if y_mid > canvas[1] * 0.72
                                else "middle"
                            ),
                            "area": round(area, 1),
                            "inherited_negative_y": matrix[3] < 0,
                        }
                    )
        for child in list(elem):
            visit(child, matrix)

    visit(root, (1, 0, 0, 1, 0, 0))
    records.sort(key=lambda item: (-float(item["area"]), item["bbox_px"][1], item["bbox_px"][0]))
    elements = [{k: v for k, v in item.items() if v not in (None, "")} for item in records[:limit]]
    top = [item for item in records if item["visual_zone"] == "top"][:6]
    bottom = [item for item in records if item["visual_zone"] == "bottom"][:6]
    return {
        "canvas": {"width": canvas[0], "height": canvas[1]},
        "orientation": {
            "visual_y_axis": "top_down",
            "raw_y_axis_reliable": transform_stats["negative_y_transform_count"] == 0,
            "negative_y_transform_count": transform_stats["negative_y_transform_count"],
            "negative_x_transform_count": transform_stats["negative_x_transform_count"],
            "non_identity_transform_count": transform_stats["non_identity_transform_count"],
            "element_count": transform_stats["element_count"],
            "top_prominent": [
                {"tag": item["tag"], "text": item.get("text"), "bbox_px": item["bbox_px"], "area": item["area"]}
                for item in top
            ],
            "bottom_prominent": [
                {"tag": item["tag"], "text": item.get("text"), "bbox_px": item["bbox_px"], "area": item["area"]}
                for item in bottom
            ],
        },
        "elements": elements,
    }


def _summarize_svg_visual_elements(path: Path, limit: int = 45) -> list[dict[str, Any]]:
    return list(_analyze_svg_visual_geometry(path, limit).get("elements") or [])


def _svg_canvas(root: ET.Element) -> tuple[float, float]:
    view_box = root.attrib.get("viewBox")
    if view_box:
        nums = _numbers(view_box)
        if len(nums) >= 4 and nums[2] > 0 and nums[3] > 0:
            return nums[2], nums[3]
    return float(root.attrib.get("width") or 1350), float(root.attrib.get("height") or 759)


def _svg_element_bbox(elem: ET.Element) -> tuple[float, float, float, float] | None:
    tag = elem.tag.rsplit("}", 1)[-1].lower()
    if tag == "rect" or tag == "image":
        x = float(elem.attrib.get("x") or 0)
        y = float(elem.attrib.get("y") or 0)
        w = float(elem.attrib.get("width") or 0)
        h = float(elem.attrib.get("height") or 0)
        return (x, y, x + w, y + h)
    if tag == "text":
        source_box = _numbers(elem.attrib.get("data-pptist-box") or "")
        if len(source_box) >= 4:
            x, y, w, h = source_box[:4]
            return (x, y, x + max(0.0, w), y + max(0.0, h))
        xs = _numbers(elem.attrib.get("x") or "")
        ys = _numbers(elem.attrib.get("y") or "")
        font_values = _numbers(elem.attrib.get("font-size") or "")
        anchor = (elem.attrib.get("text-anchor") or "").strip().lower()
        for child in list(elem):
            child_tag = child.tag.rsplit("}", 1)[-1].lower()
            if child_tag != "tspan":
                continue
            if not xs:
                xs = _numbers(child.attrib.get("x") or "")
            if not ys:
                ys = _numbers(child.attrib.get("y") or "")
            if not font_values:
                font_values = _numbers(child.attrib.get("font-size") or "")
            if not anchor:
                anchor = (child.attrib.get("text-anchor") or "").strip().lower()
        if not xs:
            xs = [0.0]
        if not ys:
            ys = [0.0]
        font = float(font_values[0]) if font_values else 24.0
        text = "".join(elem.itertext()).strip()
        cjk_count = sum(1 for ch in text if ord(ch) > 255)
        latin_count = max(0, len(text) - cjk_count)
        width = max(font, cjk_count * font + latin_count * font * 0.56)
        origin_x = min(xs)
        if anchor == "middle":
            x0 = origin_x - width / 2
        elif anchor == "end":
            x0 = origin_x - width
        else:
            x0 = origin_x
        return (x0, min(ys) - font * 0.86, x0 + width, min(ys) + font * 0.25)
    if tag in {"path", "polygon", "polyline", "line", "circle", "ellipse"}:
        if tag == "circle":
            cx = float(elem.attrib.get("cx") or 0)
            cy = float(elem.attrib.get("cy") or 0)
            r = float(elem.attrib.get("r") or 0)
            return (cx - r, cy - r, cx + r, cy + r)
        if tag == "ellipse":
            cx = float(elem.attrib.get("cx") or 0)
            cy = float(elem.attrib.get("cy") or 0)
            rx = float(elem.attrib.get("rx") or 0)
            ry = float(elem.attrib.get("ry") or 0)
            return (cx - rx, cy - ry, cx + rx, cy + ry)
        values = _numbers(elem.attrib.get("d") or elem.attrib.get("points") or " ".join([
            elem.attrib.get("x1") or "0",
            elem.attrib.get("y1") or "0",
            elem.attrib.get("x2") or "0",
            elem.attrib.get("y2") or "0",
        ]))
        if len(values) < 2:
            return None
        xs = values[0::2]
        ys = values[1::2]
        return (min(xs), min(ys), max(xs), max(ys))
    return None


def _multiply_svg_matrix(
    left: tuple[float, float, float, float, float, float],
    right: tuple[float, float, float, float, float, float],
) -> tuple[float, float, float, float, float, float]:
    la, lb, lc, ld, le, lf = left
    ra, rb, rc, rd, re_, rf = right
    return (
        la * ra + lc * rb,
        lb * ra + ld * rb,
        la * rc + lc * rd,
        lb * rc + ld * rd,
        la * re_ + lc * rf + le,
        lb * re_ + ld * rf + lf,
    )


def _parse_svg_transform(transform: str | None) -> tuple[float, float, float, float, float, float]:
    if not transform:
        return (1, 0, 0, 1, 0, 0)
    matrix: tuple[float, float, float, float, float, float] = (1, 0, 0, 1, 0, 0)
    for name, args in re.findall(r"([a-zA-Z]+)\(([^)]*)\)", transform):
        nums = _numbers(args)
        op = name.lower()
        next_matrix: tuple[float, float, float, float, float, float] = (1, 0, 0, 1, 0, 0)
        if op == "matrix" and len(nums) >= 6:
            next_matrix = (nums[0], nums[1], nums[2], nums[3], nums[4], nums[5])
        elif op == "translate" and nums:
            next_matrix = (1, 0, 0, 1, nums[0], nums[1] if len(nums) > 1 else 0)
        elif op == "scale" and nums:
            sx = nums[0]
            sy = nums[1] if len(nums) > 1 else sx
            next_matrix = (sx, 0, 0, sy, 0, 0)
        matrix = _multiply_svg_matrix(matrix, next_matrix)
    return matrix


def _parse_svg_matrix(transform: str | None) -> tuple[float, float, float, float, float, float]:
    return _parse_svg_transform(transform)


def _apply_svg_matrix(
    bbox: tuple[float, float, float, float],
    matrix: tuple[float, float, float, float, float, float],
) -> tuple[float, float, float, float]:
    a, b, c, d, e, f = matrix
    points = [(bbox[0], bbox[1]), (bbox[2], bbox[1]), (bbox[2], bbox[3]), (bbox[0], bbox[3])]
    tx = [(a * x + c * y + e, b * x + d * y + f) for x, y in points]
    xs = [p[0] for p in tx]
    ys = [p[1] for p in tx]
    return (min(xs), min(ys), max(xs), max(ys))


def _numbers(value: str) -> list[float]:
    return [
        float(item)
        for item in re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", value or "")
    ]


def _language_directive(reply_language: str = "en") -> str:
    if (reply_language or "").lower().startswith("zh"):
        return (
            "HARD REQUIREMENT: 所有自然语言输出必须使用简体中文。"
            "这包括中途状态说明、思考进度摘要、工具执行后的解释、最终总结、Markdown 标题和列表。"
            "不要用英文句子回复用户，除非是文件名、字段名、代码、命令或原文引用。"
        )
    return (
        "HARD REQUIREMENT: All natural-language output must be in English. "
        "This includes interim status updates, tool-result explanations, final summaries, "
        "Markdown headings, and lists. Use other languages only for file names, field names, "
        "code, commands, or direct source quotes."
    )


def _build_prompt(
    import_id: str,
    import_dir: Path,
    feedback: str,
    reply_language: str = "en",
    *,
    planning: bool = True,
) -> str:
    language_directive = _language_directive(reply_language)

    snapshot = _review_snapshot(import_dir)

    if not planning:
        return f"""You are Paper PPT Agent's template-import reviewer.

Output language:
{language_directive}

User request:
{feedback}

Workspace:
{import_dir}

Live snapshot:
{snapshot}

Read-only inspection contract:
- This run is guidance only. Do not create, edit, delete, move, overwrite, or register any file.
- Do not write ``review.json``, ``agent_template/``, SVG, manifest, design spec, or summary files.
- Do not run Bash. Use only file-reading tools.
- The user may still be deciding page roles, so do not mark the template as complete and do not apply changes.

Read order:
1. Read ``agent_context.json``.
2. Read ``agent_task.json``.
3. Read ``pptist/deck.json`` if it exists and you need element-level context.
4. Read source SVGs only if ``agent_task.json`` lists them and they exist.

Inspection goals:
- Check whether the five selected representative page roles are reasonable for cover, toc, chapter, content, and ending.
- Important: page role assignments choose one representative reusable layout per role; they are not tags for every chapter/content slide in the uploaded deck.
- Point out missing or suspicious assignments, duplicate role usage, and pages that still contain obvious original body content or screenshots.
- Notice repeated logos, brand marks, headers, footers, and corner decorations that should be preserved during templateization.
- Use unresolved annotations as user intent and mention any annotation that changes your recommendation.
- Tell the user whether anything needs adjustment, then ask whether to start templateization.

Output discipline:
- Keep chat concise and actionable: no file-reading narration, no long tables, and no more than three bullets unless the user asks for detail.
- Do not paste full JSON, SVG, or deck contents.
- End with a clear next step: adjust a representative page role, add an annotation, or start templateization.
"""

    return f"""You are Paper PPT Agent's template-import editor.

Output language:
{language_directive}

User request:
{feedback}

Workspace:
{import_dir}

Live snapshot:
{snapshot}

Execution gate:
- The user explicitly started templateization. You may edit the import workspace to create a clean template preview.
- Do not confirm/register the template or edit global template directories.

Read order:
1. Read ``agent_context.json``.
2. Read ``agent_task.json``. This is the compact source of truth for the five selected pages.
3. Read ``agent_design_spec_reference.md`` before writing or revising any design spec.
4. For template SVG edits, read every ``source_svg`` and ``agent_template_svg`` listed in
   ``agent_task.json.required_reads``.
5. Read ``agent_visual_brief.json`` only when you need extra visual geometry details.

Editing contract:
- The backend has seeded ``agent_template/*.svg`` from the selected source slides.
- Edit those seeded SVG files in place; source metadata lives in ``agent_template/source_map.json``.
- Do not create a new/blank layout and do not rearrange the page from scratch.
- Preserve source chrome: logos, backgrounds, bands, corner shapes, headers, footers,
  navigation bars, and decorative paths unless the user explicitly asked to change them.
- Preserve any logo/brand image or mark that repeats in roughly the same nearby position
  across multiple source slides, even if it looks like a small content image.
- Keep the visual direction upright and consistent. If a raw SVG uses flipped transforms,
  preserve how it looks in PowerPoint/PPTist, not the misleading raw coordinate direction.
- Replace reusable content only with this standard placeholder contract:
  cover: ``{{{{TITLE}}}}``, ``{{{{SUBTITLE}}}}``, ``{{{{AUTHOR}}}}``, ``{{{{DATE}}}}``, ``{{{{GROUP}}}}``;
  toc: ``{{{{TOC_ITEM_1}}}}`` through ``{{{{TOC_ITEM_5}}}}``;
  chapter: ``{{{{CHAPTER_TITLE}}}}`` and ``{{{{CHAPTER_NUMBER}}}}``;
  content: ``{{{{PAGE_TITLE}}}}`` and ``{{{{CONTENT_AREA}}}}``;
  ending: ``{{{{ENDING_TITLE}}}}`` and ``{{{{ENDING_MESSAGE}}}}``.
- Place placeholders in the same visual zones as the original content: keep alignment,
  spacing, and reading order tidy; avoid piling tokens together or drifting into chrome.
- For text placeholders, do not invent left-to-right positions. Replace the original
  matching text node in place and preserve its x/y, ``text-anchor``, fill color,
  font family, font size, font weight, and bbox unless the user explicitly asks
  for a redesign.
- Use ``source_orientation`` from ``agent_task.json`` for top/bottom direction; raw SVG
  y coordinates may be flipped by transforms.
- Update ``review.json`` only for high-level decisions: design_spec, asset roles,
  element_actions, and resolved annotations.
- Always create or update a complete design spec. Put the same Markdown in
  ``review.json`` at ``draft.design_spec`` and in ``agent_template/design_spec.md``.
  It must follow ``agent_design_spec_reference.md`` sections I through XI, adapted
  to this imported reusable template pack.
- Also write ``agent_template/summary.md`` with a brief change summary and any remaining warnings.
- Also write ``agent_template/manifest.patch.json`` with ``source_kind: "pptist_import"``,
  ``clean_generation_ready: true``, current page selections, changed page types, and warnings.
- Do not confirm/register the template or edit global template directories.

Output discipline:
- Keep chat concise.
- Do not paste full SVG/JSON/command output.
- When done, say which files/fields changed and what to inspect in preview.
"""


def _build_recovery_prompt(
    import_id: str,
    import_dir: Path,
    feedback: str,
    reply_language: str = "en",
    *,
    planning: bool = True,
) -> str:
    language_directive = _language_directive(reply_language)
    snapshot = _review_snapshot(import_dir)
    if not planning:
        return f"""Resume the same read-only Paper PPT Agent template inspection after an SDK stream interruption.

Output language:
{language_directive}

Original user feedback:
{feedback}

Import id:
{import_id}

Workspace:
{import_dir}

Current review snapshot:
{snapshot}

Continue as a read-only reviewer. Do not create, edit, delete, move, overwrite,
or register any file. Read ``agent_context.json`` first and ``agent_task.json``
second, then give concise guidance about the five representative page roles,
dirty content risks, repeated logos/brand marks to preserve, annotations, and
whether the user should start templateization. Do not produce long tables or
file-reading narration.
"""
    return f"""Resume the same Paper PPT Agent template import task after an SDK stream interruption.

Output language:
{language_directive}
Before every visible assistant message, silently check that it follows the required language.

Reason for this resume:
The previous Agent SDK stream was interrupted by transport/output limits, not by a user
cancellation. Continue from the existing session and the current files on disk.

Original user feedback:
{feedback}

Import id:
{import_id}

Workspace:
{import_dir}

Current review snapshot:
{snapshot}

Continue the task by reading the current ``agent_context.json`` first,
``agent_task.json`` second, and ``agent_design_spec_reference.md`` before any
design-spec edits. If the remaining work includes creating or revising
``agent_template/`` SVGs, read every ``source_svg`` and ``agent_template_svg`` in
``agent_task.json.required_reads`` before editing. Avoid printing large JSON, SVG,
command output, or file contents in chat;
summarize instead. If you use Bash, redirect potentially large output to a temporary
file and inspect targeted snippets only.

Finish by editing ``review.json`` and ``agent_template/design_spec.md`` as needed,
then summarize what changed and what the user should inspect in the preview pane.
"""


def _format_agent_error(exc: BaseException, stderr_tail: list[str]) -> str:
    cls = exc.__class__.__name__
    message = str(exc).strip()
    if message:
        text = f"{cls}: {message}"
    else:
        text = f"{cls}: {repr(exc)}"

    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        cause_message = str(cause).strip() or repr(cause)
        text = f"{text}\nCause: {cause.__class__.__name__}: {cause_message}"

    if stderr_tail:
        stderr = "\n".join(stderr_tail[-5:])
        text = f"{text}\nClaude stderr:\n{stderr}"

    return text[:_TEXT_LIMIT]


def _events_from_sdk_message(message: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    kind = message.__class__.__name__

    # StreamEvent carries partial deltas of an in-flight assistant message.
    # We intentionally suppress them: the SDK still emits the final
    # AssistantMessage with the full text, so forwarding partials only
    # produces duplicate / overlapping rows in the UI.
    if kind == "StreamEvent":
        return events

    if kind == "AssistantMessage":
        for block in getattr(message, "content", []) or []:
            block_kind = block.__class__.__name__
            if hasattr(block, "text"):
                text = str(getattr(block, "text") or "").strip()
                if text:
                    events.append(
                        {
                            "type": "message",
                            "stage": "agent",
                            "status": "running",
                            "message": text[:_TEXT_LIMIT],
                            "data": {"block": block_kind},
                        }
                    )
            elif hasattr(block, "name"):
                tool_name = str(getattr(block, "name") or "tool")
                tool_input = getattr(block, "input", None)
                tool_use_id = getattr(block, "id", None)
                events.append(
                    {
                        "type": "tool",
                        "stage": "tool",
                        "status": "running",
                        "message": f"Using {tool_name}",
                        "data": {
                            "tool": tool_name,
                            "input": _compact_jsonish(tool_input),
                            "tool_use_id": tool_use_id,
                        },
                    }
                )
    elif kind == "UserMessage":
        # The SDK echoes tool execution results back through a synthetic
        # UserMessage that carries one or more ToolResultBlocks. Forward each
        # as a ``tool`` event with ``status=complete`` (or ``error``) so the
        # UI can flip the matching running row to its done state.
        for block in getattr(message, "content", []) or []:
            block_kind = block.__class__.__name__
            if block_kind != "ToolResultBlock" and not hasattr(block, "tool_use_id"):
                continue
            tool_use_id = getattr(block, "tool_use_id", None)
            is_error = bool(getattr(block, "is_error", False))
            content = getattr(block, "content", None)
            # ``content`` can be a string or a list of dict-like blocks.
            if isinstance(content, list):
                snippets: list[str] = []
                for entry in content:
                    if isinstance(entry, dict):
                        snippets.append(str(entry.get("text") or entry.get("content") or ""))
                    else:
                        snippets.append(str(entry))
                content_text = "\n".join(s for s in snippets if s)
            else:
                content_text = str(content or "")
            events.append(
                {
                    "type": "tool",
                    "stage": "tool",
                    "status": "error" if is_error else "complete",
                    "message": "Tool result",
                    "data": {
                        "tool_use_id": tool_use_id,
                        "is_error": is_error,
                        "output_preview": content_text[:_TEXT_LIMIT // 4],
                    },
                }
            )
    elif kind == "ResultMessage":
        subtype = str(getattr(message, "subtype", "") or "").lower()
        result = getattr(message, "result", None)
        is_false_success = (
            subtype == "success" or str(result or "").strip().lower() == "success"
        )
        is_error = bool(getattr(message, "is_error", False)) and not is_false_success
        events.append(
            {
                "type": "result",
                "stage": "result",
                "status": "error" if is_error else "complete",
                "message": (str(result or "Agent produced a result.")[:_TEXT_LIMIT]),
                "data": {
                    "is_error": is_error,
                    "subtype": subtype,
                    "duration_ms": getattr(message, "duration_ms", None),
                    "num_turns": getattr(message, "num_turns", None),
                    "total_cost_usd": getattr(message, "total_cost_usd", None),
                },
            }
        )
    elif kind == "SystemMessage":
        subtype = str(getattr(message, "subtype", "system"))
        events.append(
            {
                "type": "system",
                "stage": "system",
                "status": "running",
                "message": subtype,
                "data": _compact_jsonish(getattr(message, "data", None)),
            }
        )
    else:
        text = _message_text(message)
        if text:
            events.append(
                {
                    "type": "message",
                    "stage": "agent",
                    "status": "running",
                    "message": text[:_TEXT_LIMIT],
                    "data": {"message_type": kind},
                }
            )
    return events


def _is_false_success_error(exc: BaseException) -> bool:
    text = str(exc).strip().lower()
    return text == "claude code returned an error result: success"


def _is_recoverable_agent_error(exc: BaseException) -> bool:
    text = str(exc).strip().lower()
    return any(
        marker in text
        for marker in (
            "failed to decode json",
            "json message exceeded maximum buffer size",
            "maximum buffer size",
            "reached maximum number of turns",
        )
    )


def _message_text(message: Any) -> str:
    if hasattr(message, "text"):
        return str(getattr(message, "text") or "").strip()
    if hasattr(message, "content"):
        content = getattr(message, "content")
        if isinstance(content, str):
            return content.strip()
    return ""


def _usage_snapshot(job: TemplateAgentJob) -> dict[str, int]:
    return {
        "input_tokens": job.input_tokens,
        "output_tokens": job.output_tokens,
        "cache_read_tokens": job.cache_read_tokens,
        "cache_creation_tokens": job.cache_creation_tokens,
        "duration_ms": job.duration_ms,
    }


def _usage_event(job: TemplateAgentJob) -> dict[str, Any]:
    """Build a ``usage`` event payload reflecting the current job totals."""
    return {
        "type": "usage",
        "stage": "agent",
        "status": "running" if job.status == "running" else job.status,
        "message": "Usage update",
        "data": {
            "session_id": job.session_id,
            "model": job.model_name,
            "input_tokens": job.input_tokens,
            "output_tokens": job.output_tokens,
            "cache_read_input_tokens": job.cache_read_tokens,
            "cache_creation_input_tokens": job.cache_creation_tokens,
            "total_cost_usd": round(job.total_cost_usd, 6) if job.total_cost_usd else 0.0,
            "num_turns": job.num_turns,
            "duration_ms": job.duration_ms,
            "model_usage": job.model_usage or None,
        },
    }


def _session_state_payload(state: TemplateAgentSessionState) -> dict[str, Any]:
    return {
        "session_id": state.session_id,
        "initialized": state.initialized,
        "input_tokens": state.input_tokens,
        "output_tokens": state.output_tokens,
        "cache_read_tokens": state.cache_read_tokens,
        "cache_creation_tokens": state.cache_creation_tokens,
        "total_cost_usd": round(state.total_cost_usd, 6) if state.total_cost_usd else 0.0,
        "num_turns": state.num_turns,
        "duration_ms": state.duration_ms,
        "model_name": state.model_name,
        "model_usage": state.model_usage or None,
        "updated_at": state.updated_at,
    }


def _compact_jsonish(value: Any) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        encoded = str(value)
    if len(encoded) <= 1200:
        try:
            return json.loads(encoded)
        except json.JSONDecodeError:
            return encoded
    return encoded[:1197] + "..."


def _compact_assistant_text(parts: list[str]) -> str:
    text = "\n\n".join(part.strip() for part in parts if part.strip())
    if len(text) <= 4000:
        return text
    return text[-4000:]


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _offer(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
    try:
        queue.put_nowait(event)
        return
    except asyncio.QueueFull:
        pass
    try:
        queue.get_nowait()
    except asyncio.QueueEmpty:
        return
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        pass


def _safe_create_task(coro: Any) -> None:
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        pass


template_agent_manager = TemplateAgentManager()
