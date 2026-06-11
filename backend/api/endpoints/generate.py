"""Generation API endpoint.

The HTTP handler does the cheap synchronous work (validate session, derive
the pipeline request, register a Job row) and then enqueues the actual
generation through the scheduler. Submitting through the scheduler is what
makes ``POST /generate`` return in milliseconds even when an earlier job is
still in its parsing/research stage on a busy server.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, status

from backend.api.schemas import AgentFeedbackRequest, AgentFeedbackResponse, GenerateRequest, GenerateResponse
from backend.runtime.job_timeout import run_with_optional_job_timeout
from backend.runtime.scheduler import QueueFull, SchedulerDraining, get_scheduler
from backend.session.manager import session_manager
from backend.session.progress import describe_exception, payloads_from_progress_event

logger = logging.getLogger(__name__)

router = APIRouter()


async def _iterate_pipeline(job_id: str, request: Any) -> None:
    from backend.orchestrator.pipeline import run_pipeline
    from backend.usage.tracker import set_usage_context

    set_usage_context(job_id=job_id)

    async for event in run_pipeline(request):
        current_job = session_manager.get_job(job_id)
        if current_job is None:
            return
        for payload, updates in payloads_from_progress_event(job_id, current_job, event):
            session_manager.record_event(job_id, payload, **updates)


async def _iterate_agent_feedback_pipeline(
    job_id: str,
    *,
    project_dir: Path,
    feedback: str,
    runtime: str,
    total_slides_hint: int,
    session_id: str | None = None,
) -> None:
    from backend.orchestrator.agent_pipeline import run_agent_feedback_pipeline
    from backend.usage.tracker import set_usage_context

    set_usage_context(job_id=job_id)

    async for event in run_agent_feedback_pipeline(
        project_dir=project_dir,
        feedback=feedback,
        runtime=runtime,
        total_slides_hint=total_slides_hint,
        session_id=session_id,
        job_id=job_id,
    ):
        current_job = session_manager.get_job(job_id)
        if current_job is None:
            return
        for payload, updates in payloads_from_progress_event(job_id, current_job, event):
            session_manager.record_event(job_id, payload, **updates)


def _cleanup_partial_workspace(job_id: str) -> None:
    """Preserve the workspace of a cancelled/failed job.

    Failed runs often still contain useful parse output, manuscript drafts,
    or partially generated SVGs. Keeping the project directory lets the result
    page preview whatever exists and allows a later re-export when SVGs were
    already produced.
    """
    job = session_manager.get_job(job_id)
    if job is not None and job.project_dir:
        session_manager.update_job(job_id, project_dir=job.project_dir)


async def _run_generation_job(job_id: str, request: Any) -> None:
    from backend.orchestrator.pipeline import ProgressEvent

    job = session_manager.get_job(job_id)
    if job is None:
        return

    timeout = getattr(request, "timeout_seconds", None)
    cleanup_needed = False
    try:
        timed_out = await run_with_optional_job_timeout(_iterate_pipeline(job_id, request), timeout)
        if timed_out:
            current_job = session_manager.get_job(job_id)
            if current_job is None:
                return
            msg = f"Job exceeded timeout of {timeout}s"
            error_event = ProgressEvent("error", "error", msg, current_job.progress)
            for payload, updates in payloads_from_progress_event(job_id, current_job, error_event):
                session_manager.record_event(job_id, payload, **updates)
            cleanup_needed = True
    except asyncio.CancelledError:
        session_manager.mark_job_cancelled(job_id)
        cleanup_needed = True
        raise
    except Exception as exc:
        current_job = session_manager.get_job(job_id)
        if current_job is None:
            return
        if current_job.status == "error" and current_job.error:
            cleanup_needed = True
            return
        error_event = ProgressEvent(
            "error",
            "error",
            describe_exception(exc, "Generation failed"),
            current_job.progress,
        )
        for payload, updates in payloads_from_progress_event(job_id, current_job, error_event):
            session_manager.record_event(job_id, payload, **updates)
        cleanup_needed = True
    finally:
        if cleanup_needed:
            _cleanup_partial_workspace(job_id)


async def _run_agent_feedback_job(
    job_id: str,
    *,
    project_dir: Path,
    feedback: str,
    runtime: str,
    total_slides_hint: int,
    session_id: str | None = None,
) -> None:
    from backend.orchestrator.pipeline import ProgressEvent

    job = session_manager.get_job(job_id)
    if job is None:
        return

    cleanup_needed = False
    try:
        await _iterate_agent_feedback_pipeline(
            job_id,
            project_dir=project_dir,
            feedback=feedback,
            runtime=runtime,
            total_slides_hint=total_slides_hint,
            session_id=session_id,
        )
    except asyncio.CancelledError:
        session_manager.mark_job_cancelled(job_id, "Agent feedback task cancelled")
        cleanup_needed = True
        raise
    except Exception as exc:
        current_job = session_manager.get_job(job_id)
        if current_job is None:
            return
        error_event = ProgressEvent(
            "error",
            "error",
            describe_exception(exc, "Agent feedback failed"),
            current_job.progress,
        )
        for payload, updates in payloads_from_progress_event(job_id, current_job, error_event):
            session_manager.record_event(job_id, payload, **updates)
        cleanup_needed = True
    finally:
        if cleanup_needed:
            _cleanup_partial_workspace(job_id)


async def _run_provider_subprocess_job(
    job_id: str,
    request_file: str,
    timeout: float | None,
) -> None:
    """Run provider generation in an isolated child under scheduler control."""
    from backend.orchestrator.pipeline import ProgressEvent
    from backend.runtime import aoffload
    from backend.runtime.job_event_monitor import monitor_job_events, sync_job_events_once
    from backend.workers.subprocess_runner import run_generation_subprocess

    monitor_task = asyncio.create_task(
        monitor_job_events(job_id),
        name=f"job-{job_id}-event-monitor",
    )
    session_manager.register_task(job_id, monitor_task)
    try:
        returncode = await aoffload(
            run_generation_subprocess,
            job_id,
            request_file,
            timeout,
        )
        await sync_job_events_once(job_id)
        job = session_manager.get_job(job_id)
        if job is None or job.status in {"complete", "error", "cancelled"}:
            return
        if returncode == 0:
            message = "Generation worker exited before reporting completion."
        else:
            message = f"Generation worker exited with code {returncode}."
        error_event = ProgressEvent("error", "error", message, job.progress)
        for payload, updates in payloads_from_progress_event(job_id, job, error_event):
            session_manager.record_event(job_id, payload, **updates)
    except asyncio.CancelledError:
        session_manager.mark_job_cancelled(job_id)
        raise
    finally:
        try:
            await sync_job_events_once(job_id)
        except Exception:
            pass
        if not monitor_task.done():
            try:
                await asyncio.wait_for(monitor_task, timeout=2.0)
            except asyncio.TimeoutError:
                monitor_task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass


def _agent_runtime_from_job(job: Any) -> str:
    provider = str(getattr(job, "provider", "") or "")
    if provider.startswith("agent:"):
        runtime = provider.split(":", 1)[1].strip()
        return runtime or "claude_code"
    return "claude_code"


@router.post("/generate", response_model=GenerateResponse)
async def generate_presentation(request: GenerateRequest) -> GenerateResponse:
    from backend.orchestrator.pipeline import GenerationRequest

    session = session_manager.get_session(request.session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found.",
        )

    generation_backend = request.options.generation_backend
    model_settings = request.model_settings
    if generation_backend == "provider" and model_settings is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provider generation requires model_config.",
        )
    if generation_backend == "agent":
        from backend.orchestrator.agent_pipeline import agent_runtime_defaults, agent_runtime_status

        runtime = (
            request.options.agent_config.runtime
            if request.options.agent_config
            else "claude_code"
        )
        runtime_status = await agent_runtime_status(runtime)
        if not runtime_status.get("available"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=runtime_status.get("message") or f"Agent runtime '{runtime}' is not ready.",
            )
        agent_defaults = agent_runtime_defaults(runtime)
    else:
        agent_defaults = {}
    agent_config_payload = (
        request.options.agent_config.model_dump(exclude_none=True)
        if request.options.agent_config
        else None
    )
    if agent_config_payload is not None:
        for key in ("model", "api_key", "auth_token", "base_url"):
            value = str(agent_config_payload.get(key) or "").strip()
            if value:
                agent_config_payload[key] = value
            else:
                agent_config_payload.pop(key, None)
        if agent_config_payload.get("runtime") != "claude_code":
            for key in ("api_key", "auth_token", "base_url"):
                agent_config_payload.pop(key, None)

    job = session_manager.create_job(request.session_id)
    provider_label = (
        model_settings.provider
        if model_settings is not None
        else f"agent:{(request.options.agent_config.runtime if request.options.agent_config else 'claude_code')}"
    )
    model_label = (
        model_settings.model
        if model_settings is not None
        else (
            agent_config_payload.get("model")
            if agent_config_payload and agent_config_payload.get("model")
            else agent_defaults.get("model")
        )
        or "agent-default"
    )
    session_manager.update_job(
        job.id,
        status="pending",
        message="Queued for generation",
        provider=provider_label,
        model_name=model_label,
        base_url=(
            model_settings.base_url
            if model_settings is not None
            else agent_config_payload.get("base_url") if agent_config_payload else None
        ),
        canvas_format=request.options.canvas_format,
        style=request.options.style,
        language=request.options.language,
        detail_level=request.options.detail_level,
        instruction=request.instruction,
        worker_backend="scheduler-subprocess" if generation_backend == "provider" else None,
    )

    pipeline_request = GenerationRequest(
        file_path=session.file_path,
        source_type=session.source_type,  # type: ignore[arg-type]
        provider=model_settings.provider if model_settings is not None else "",
        model=model_settings.model if model_settings is not None else "",
        api_key=model_settings.api_key if model_settings is not None else "",
        base_url=model_settings.base_url if model_settings is not None else None,
        artifact_thinking_mode=(
            model_settings.artifact_thinking_mode
            if model_settings is not None
            else "disabled"
        ),
        canvas_format=request.options.canvas_format,
        style=request.options.style,
        num_pages=request.options.num_pages,
        instruction=request.instruction,
        language=request.options.language,
        detail_level=request.options.detail_level,
        generation_mode=request.options.generation_mode,
        parallel_concurrency=request.options.parallel_concurrency,
        timeout_seconds=request.options.timeout_seconds,
        max_critic_attempts=request.options.max_critic_attempts,
        style_overrides=(
            request.options.style_overrides.model_dump(exclude_none=True)
            if request.options.style_overrides
            else None
        ),
        enable_deep_research=request.options.enable_deep_research,
        deepseek_settings=(
            model_settings.deepseek_settings.model_dump()
            if model_settings is not None
            and model_settings.provider == "deepseek"
            and model_settings.deepseek_settings
            else None
        ),
        openai_settings=(
            model_settings.openai_settings.model_dump()
            if model_settings is not None
            and model_settings.provider == "openai"
            and model_settings.openai_settings
            else None
        ),
        enable_visual_critic=request.options.enable_visual_critic,
        visual_qa_max_attempts=request.options.visual_qa_max_attempts,
        template_id=request.options.template_id,
        research_config=request.options.research_config,
        job_id=job.id,
        generation_backend=generation_backend,
        agent_config=agent_config_payload,
    )

    if generation_backend == "provider":
        from backend.runtime.job_event_monitor import write_generation_request

        request_file = await write_generation_request(job.id, pipeline_request)
        scheduler = get_scheduler()

        async def _runner() -> None:
            await _run_provider_subprocess_job(
                job.id,
                str(request_file),
                pipeline_request.timeout_seconds,
            )

        try:
            position = await scheduler.submit(job.id, _runner)
            if position > 0:
                session_manager.update_job(
                    job.id,
                    message=f"Queued for generation (position {position + 1})",
                )
        except QueueFull as exc:
            session_manager.update_job(
                job.id,
                status="error",
                error="Server is busy: too many jobs queued.",
                message="Server is busy: too many jobs queued.",
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=str(exc),
            ) from exc
        except SchedulerDraining as exc:
            session_manager.update_job(
                job.id,
                status="error",
                error="Server is shutting down.",
                message="Server is shutting down.",
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
    else:
        scheduler = get_scheduler()

        async def _runner() -> None:
            await _run_generation_job(job.id, pipeline_request)

        try:
            await scheduler.submit(job.id, _runner)
        except QueueFull as exc:
            session_manager.update_job(
                job.id,
                status="error",
                error="Server is busy: too many jobs queued.",
                message="Server is busy: too many jobs queued.",
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=str(exc),
            ) from exc
        except SchedulerDraining as exc:
            session_manager.update_job(
                job.id,
                status="error",
                error="Server is shutting down.",
                message="Server is shutting down.",
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc

    return GenerateResponse(job_id=job.id, status="queued")


@router.post("/generate/{job_id}/agent-feedback", response_model=AgentFeedbackResponse)
async def send_agent_generation_feedback(
    job_id: str,
    payload: AgentFeedbackRequest,
) -> AgentFeedbackResponse:
    job = session_manager.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found.",
        )
    if not job.project_dir:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Agent workspace is not ready yet.",
        )
    if not str(job.provider or "").startswith("agent:"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Feedback is only supported for Agent generation jobs.",
        )
    if job.status in {"error", "cancelled"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Feedback cannot resume a failed or cancelled Agent job.",
        )
    if job.status == "pausing":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Wait for the Agent to finish pausing before sending guidance.",
        )

    text = payload.message.strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Feedback message is required.",
        )

    # Route only to an Agent session that is still able to consume a message.
    # The live session object can remain registered while the backend is
    # validating/finalizing/exporting, after the Agent turn itself has already
    # ended. In that state returning "injected" is misleading: the user sees
    # "received" but no Agent will act on it.
    from backend.orchestrator.agent_session_registry import get as get_live_session

    live_session = get_live_session(job_id)
    can_inject_live = bool(
        live_session is not None
        and getattr(live_session, "can_accept_feedback", False)
    )
    if live_session is not None and not can_inject_live and job.status != "complete":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "The Agent has finished its current turn and the deck is being "
                "finalized/exported. Wait for completion, then send guidance "
                "as a revision."
            ),
        )

    from backend.runtime import aensure_dir, awrite_text

    target_dir = Path(job.project_dir) / "agent_feedback"
    await aensure_dir(target_dir)
    filename = f"{int(time.time() * 1000)}.md"
    target = target_dir / filename
    await awrite_text(target, text + "\n", encoding="utf-8")
    session_manager.record_event(
        job_id,
        {
            "type": "progress",
            "job_id": job_id,
            "stage": "agent",
            "status": "progress",
            "message": "User guidance received.",
            "progress": job.progress,
            "slides_completed": job.slides_completed,
            "total_slides": job.total_slides,
            "data": {
                "agent_feedback": {
                    "path": str(target),
                    "message": text,
                }
            },
        },
    )

    if can_inject_live and live_session is not None:
        was_paused = job.status == "paused"
        runtime = _agent_runtime_from_job(job)
        await live_session.send_message(
            text,
            interrupt_current=not was_paused and runtime == "claude_code",
        )
        if was_paused:
            session_manager.record_event(
                job_id,
                {
                    "type": "progress",
                    "job_id": job_id,
                    "stage": "agent",
                    "status": "progress",
                    "message": "Agent is applying guidance...",
                    "progress": job.progress,
                    "slides_completed": job.slides_completed,
                    "total_slides": job.total_slides,
                    "data": {"agent_resumed": True},
                },
                status="generation",
                message="Agent is applying guidance...",
                error=None,
            )
        return AgentFeedbackResponse(job_id=job_id, status="injected", path=str(target))

    if job.status == "complete":
        scheduler = get_scheduler()
        runtime = _agent_runtime_from_job(job)
        project_dir = Path(job.project_dir)
        # Load persisted session_id for context continuity.
        from backend.orchestrator.agent_pipeline import _load_agent_session_id
        prev_session_id = _load_agent_session_id(project_dir)
        session_manager.update_job(
            job_id,
            status="pending",
            message="Queued for Agent feedback revision",
            progress=0.05,
            error=None,
        )

        async def _runner() -> None:
            await _run_agent_feedback_job(
                job_id,
                project_dir=project_dir,
                feedback=text,
                runtime=runtime,
                total_slides_hint=job.total_slides,
                session_id=prev_session_id,
            )

        try:
            await scheduler.submit(job_id, _runner, priority=5)
            return AgentFeedbackResponse(job_id=job_id, status="queued", path=str(target))
        except RuntimeError as exc:
            session_manager.update_job(
                job_id,
                status="error",
                message=str(exc),
                error=str(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except (QueueFull, SchedulerDraining) as exc:
            session_manager.update_job(
                job_id,
                status="error",
                message=str(exc),
                error=str(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc

    return AgentFeedbackResponse(job_id=job_id, status="received", path=str(target))


@router.post("/generate/{job_id}/agent-interrupt", response_model=AgentFeedbackResponse)
async def interrupt_agent_generation(job_id: str) -> AgentFeedbackResponse:
    job = session_manager.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found.",
        )
    if not str(job.provider or "").startswith("agent:"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Interrupt is only supported for Agent generation jobs.",
        )
    if job.status in {"complete", "error", "cancelled"}:
        return AgentFeedbackResponse(job_id=job_id, status=job.status)
    from backend.orchestrator.agent_session_registry import get as get_live_session

    live_session = get_live_session(job_id)
    if live_session is None:
        session_manager.cancel_job(job_id)
        session_manager.mark_job_cancelled(
            job_id,
            "Agent job stopped before a live session was available.",
        )
        return AgentFeedbackResponse(job_id=job_id, status="cancelled")

    if job.status in {"paused", "pausing"}:
        cancelled = session_manager.cancel_job(job_id)
        if not cancelled:
            await live_session.close()
        session_manager.mark_job_cancelled(job_id, "Agent task cancelled.")
        return AgentFeedbackResponse(job_id=job_id, status="cancelled")

    session_manager.update_job(
        job_id,
        status="pausing",
        message="Pausing Agent...",
        error=None,
    )
    await live_session.interrupt()
    return AgentFeedbackResponse(job_id=job_id, status="pausing")
