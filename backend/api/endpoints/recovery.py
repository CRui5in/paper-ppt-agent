"""Explicit generation continuation with optional user instructions."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import time
import uuid

from fastapi import APIRouter, HTTPException

from backend.api.schemas import GenerateResponse, ResumeGenerationRequest
from backend.config import settings
from backend.runtime import aoffload
from backend.runtime.io import _write_text_atomic
from backend.runtime.scheduler import get_scheduler, QueueFull, SchedulerDraining
from backend.session.manager import session_manager

router = APIRouter()
_locks: dict[str, asyncio.Lock] = {}
TERMINAL = {"complete", "error", "cancelled"}


def _job(job_id: str):
    job = session_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    return job


def _request_path(job_id: str) -> Path:
    return settings.runtime_dir / "job_requests" / f"{job_id}.json"


def _recovery_info(job) -> dict:
    agent = str(job.provider or "").startswith("agent:")
    directory = Path(job.project_dir) if job.project_dir else None
    exists = bool(directory and directory.is_dir())
    request_available = _request_path(job.id).is_file()
    supported = (request_available and (directory is None or exists)) or (agent and exists)
    resumable = job.status in {"error", "cancelled"} and supported
    # Only output pages have passed initial SVG preparation.
    from backend.generator.project_manager import get_svg_files
    completed = len(get_svg_files(directory, source="output")) if exists else 0
    if exists and (directory / "design_spec.md").is_file():
        stage = "generation"
    elif exists and (directory / "manuscript.md").is_file():
        stage = "strategy"
    elif exists and (directory / "checkpoints" / "parsing.json").is_file():
        stage = "research"
    else:
        stage = "agent" if agent else "parsing"
    if completed and completed >= job.total_slides > 0:
        stage = "export" if (directory / "checkpoints" / "postprocess.json").is_file() else "postprocess"
    return {"can_resume": resumable, "can_retry": resumable and not agent, "stage": stage,
            "backend": "agent" if agent else "provider",
            "completed_pages": completed, "total_pages": job.total_slides,
            "reason": None if supported else "Saved generation request or workspace is unavailable."}


@router.get("/generate/{job_id}/recovery")
async def get_recovery(job_id: str) -> dict:
    return await aoffload(_recovery_info, _job(job_id))


async def _save_feedback(job, text: str) -> dict:
    text = text.strip()
    if not text:
        raise HTTPException(422, "Feedback message is required.")
    record = {"id": uuid.uuid4().hex, "job_id": job.id, "message": text,
              "created_at": time.time(), "status": job.status, "error": job.error,
              "slides_completed": job.slides_completed, "total_slides": job.total_slides}
    path = settings.runtime_dir / "job_feedback" / job.id / f"{record['id']}.json"
    await aoffload(_write_text_atomic, path, json.dumps(record, ensure_ascii=False), "utf-8")
    return record


@router.post("/generate/{job_id}/resume", response_model=GenerateResponse)
async def resume_generation(job_id: str, payload: ResumeGenerationRequest) -> GenerateResponse:
    from backend.api.endpoints.generate import _run_agent_feedback_job, _run_generation_job, _run_provider_subprocess_job
    from backend.runtime.job_event_monitor import write_generation_request
    from backend.workers.generation_worker import _request_from_payload

    parent = _job(job_id)
    key = str(Path(parent.project_dir).resolve()) if parent.project_dir else job_id
    async with _locks.setdefault(key, asyncio.Lock()):
        if parent.status not in {"error", "cancelled"}:
            raise HTTPException(409, "Only failed or cancelled jobs can be resumed.")
        if session_manager.is_job_running(parent.id):
            raise HTTPException(409, "The previous worker is stopping. Please try again shortly.")
        if any(job.status not in TERMINAL and (job.id == job_id or
               (parent.project_dir and job.project_dir == parent.project_dir) or job.parent_job_id == job_id)
               for job in session_manager.list_jobs()):
            raise HTTPException(409, "A continuation of this task is already running.")
        info = await aoffload(_recovery_info, parent)
        if not info["can_resume"]:
            raise HTTPException(409, info["reason"] or "Task cannot be resumed.")
        agent = str(parent.provider or "").startswith("agent:")
        request = None
        if _request_path(job_id).is_file():
            raw = await aoffload(_request_path(job_id).read_text, encoding="utf-8")
            request = _request_from_payload(json.loads(raw))
        if parent.project_dir and not Path(parent.project_dir).is_dir():
            raise HTTPException(409, "The saved workspace is missing.")
        if payload.feedback.strip():
            await _save_feedback(parent, payload.feedback)
        job = session_manager.create_job(parent.session_id)
        session_manager.update_job(
            job.id, parent_job_id=parent.id, project_dir=parent.project_dir,
            status="pending", message="Queued to continue from checkpoint", provider=parent.provider,
            model_name=parent.model_name, base_url=parent.base_url, canvas_format=parent.canvas_format,
            style=parent.style, language=parent.language, detail_level=parent.detail_level,
            instruction=parent.instruction, total_slides=parent.total_slides,
            slides_completed=info["completed_pages"], agent_session_id=parent.agent_session_id,
            worker_backend=None if agent else "scheduler-subprocess",
        )
        try:
            if request is not None:
                request = replace(request, job_id=job.id, resume_project_dir=parent.project_dir,
                                  retry_failed_pages=payload.mode == "retry_failed",
                                  resume_feedback="\n\n".join(filter(None, [request.resume_feedback, payload.feedback.strip()])))
                request_file = await write_generation_request(job.id, request)

            async def runner():
                if agent and parent.project_dir:
                    await _run_agent_feedback_job(
                        job.id, project_dir=Path(parent.project_dir),
                        feedback=payload.feedback.strip() or "Continue the interrupted task from its saved work.",
                        runtime=str(parent.provider).split(":", 1)[1], total_slides_hint=parent.total_slides,
                        session_id=parent.agent_session_id,
                    )
                elif agent:
                    await _run_generation_job(job.id, request)
                else:
                    await _run_provider_subprocess_job(job.id, str(request_file), request.timeout_seconds)

            await session_manager.flush_now()
            await get_scheduler().submit(job.id, runner)
        except Exception as exc:
            session_manager.update_job(job.id, status="error", error=str(exc), message="Could not queue continuation")
            await session_manager.flush_now()
            if isinstance(exc, (QueueFull, SchedulerDraining)):
                raise HTTPException(429 if isinstance(exc, QueueFull) else 503, str(exc)) from exc
            raise
        return GenerateResponse(job_id=job.id, status="queued")
