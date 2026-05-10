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
from typing import Any

from fastapi import APIRouter, HTTPException, status

from backend.api.schemas import GenerateRequest, GenerateResponse
from backend.runtime.scheduler import QueueFull, SchedulerDraining, get_scheduler
from backend.session.manager import session_manager
from backend.session.progress import payloads_from_progress_event

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
        if timeout and timeout > 0:
            await asyncio.wait_for(_iterate_pipeline(job_id, request), timeout=timeout)
        else:
            await _iterate_pipeline(job_id, request)
    except asyncio.TimeoutError:
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
        error_event = ProgressEvent("error", "error", str(exc), current_job.progress)
        for payload, updates in payloads_from_progress_event(job_id, current_job, error_event):
            session_manager.record_event(job_id, payload, **updates)
        cleanup_needed = True
    finally:
        if cleanup_needed:
            _cleanup_partial_workspace(job_id)


@router.post("/generate", response_model=GenerateResponse)
async def generate_presentation(request: GenerateRequest) -> GenerateResponse:
    from backend.orchestrator.pipeline import GenerationRequest

    session = session_manager.get_session(request.session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found.",
        )

    job = session_manager.create_job(request.session_id)
    session_manager.update_job(
        job.id,
        status="pending",
        message="Queued for generation",
        provider=request.model_settings.provider,
        model_name=request.model_settings.model,
        base_url=request.model_settings.base_url,
        canvas_format=request.options.canvas_format,
        style=request.options.style,
        language=request.options.language,
        detail_level=request.options.detail_level,
        instruction=request.instruction,
    )

    pipeline_request = GenerationRequest(
        file_path=session.file_path,
        source_type=session.source_type,  # type: ignore[arg-type]
        provider=request.model_settings.provider,
        model=request.model_settings.model,
        api_key=request.model_settings.api_key,
        base_url=request.model_settings.base_url,
        canvas_format=request.options.canvas_format,
        style=request.options.style,
        num_pages=request.options.num_pages,
        instruction=request.instruction,
        language=request.options.language,
        detail_level=request.options.detail_level,
        timeout_seconds=request.options.timeout_seconds,
        max_critic_attempts=request.options.max_critic_attempts,
        style_overrides=(
            request.options.style_overrides.model_dump(exclude_none=True)
            if request.options.style_overrides
            else None
        ),
        enable_deep_research=request.options.enable_deep_research,
        icon_library=request.options.icon_library,
        deepseek_settings=(
            request.model_settings.deepseek_settings.model_dump()
            if request.model_settings.provider == "deepseek"
            and request.model_settings.deepseek_settings
            else None
        ),
        openai_settings=(
            request.model_settings.openai_settings.model_dump()
            if request.model_settings.provider == "openai"
            and request.model_settings.openai_settings
            else None
        ),
        enable_visual_critic=request.options.enable_visual_critic,
        visual_qa_max_attempts=request.options.visual_qa_max_attempts,
        enable_icon=request.options.enable_icon,
        enable_icon_rag=request.options.enable_icon_rag,
        gemini_api_key=request.options.gemini_api_key,
        template_id=request.options.template_id,
        research_config=request.options.research_config,
    )

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
