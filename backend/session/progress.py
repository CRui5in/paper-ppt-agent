"""Helpers for projecting pipeline events into API and WebSocket state."""

from __future__ import annotations

from typing import Any

from .manager import Job

PIPELINE_STAGES = (
    "parsing",
    "research",
    "strategy",
    "generation",
    "postprocess",
    "export",
    "cancelled",
)


def describe_exception(exc: BaseException, fallback: str = "Operation failed") -> str:
    """Return a user-visible error string, even for empty-message exceptions."""
    message = str(exc).strip()
    if message:
        return message
    exc_type = type(exc).__name__
    exc_module = type(exc).__module__
    if "timeout" in exc_type.lower():
        return f"{fallback}: upstream request timed out ({exc_type})"
    return f"{fallback}: {exc_module}.{exc_type}"


def _event_stage(job: Job, event: Any) -> str:
    if event.stage in PIPELINE_STAGES:
        return event.stage
    if event.stage == "error" or event.status == "error":
        if job.status in PIPELINE_STAGES:
            return job.status
        return "export" if job.progress >= 0.85 else "parsing"
    return "export"


def build_snapshot_event(job_id: str, job: Job) -> dict[str, Any]:
    """Build a progress-shaped snapshot event for new WebSocket subscribers."""
    stage = job.status if job.status in PIPELINE_STAGES else "parsing"
    status = "progress"
    if job.status == "pending":
        status = "started"
    elif job.status == "error":
        status = "error"
        stage = "export" if job.progress >= 0.85 else stage
    elif job.status == "cancelled":
        status = "error"
        stage = "cancelled"
    elif job.status == "complete":
        status = "complete"
        stage = "export"

    return {
        "type": "progress",
        "job_id": job_id,
        "stage": stage,
        "status": status,
        "message": job.message,
        "progress": job.progress,
        "slides_completed": job.slides_completed,
        "total_slides": job.total_slides,
        "data": {
            "output_path": job.output_path,
            "project_dir": job.project_dir,
        },
    }


def payloads_from_progress_event(job_id: str, job: Job, event: Any) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Turn a pipeline event into one or more socket payloads plus job updates."""
    payloads: list[tuple[dict[str, Any], dict[str, Any]]] = []
    stage = _event_stage(job, event)
    message = event.message or (
        f"Generation failed during {stage}."
        if event.stage == "error" or event.status == "error"
        else ""
    )
    updates: dict[str, Any] = {
        "message": message,
        "progress": event.progress,
    }

    if event.data and event.data.get("project_dir"):
        updates["project_dir"] = event.data["project_dir"]

    if event.stage == "generation" and event.data and event.data.get("total_slides"):
        updates["total_slides"] = int(event.data["total_slides"])

    if event.stage == "generation" and event.status == "progress":
        page = int(event.data.get("page", 0)) if event.data else 0
        completed_count = event.data.get("completed_count") if event.data else None
        if completed_count is not None:
            updates["slides_completed"] = max(
                job.slides_completed,
                int(completed_count),
            )
        else:
            updates["slides_completed"] = max(job.slides_completed, page)

    if event.stage == "export" and event.status == "complete":
        updates["output_path"] = event.data.get("output_path") if event.data else None
        updates["status"] = "complete"
        updates["error"] = None
    elif event.stage == "error" or event.status == "error":
        updates["status"] = "error"
        updates["error"] = message
    else:
        updates["status"] = event.stage

    progress_data = dict(event.data or {})
    # ``slide_ready`` carries the live SVG. The coarse progress frame should
    # stay small; otherwise every generated page is sent and retained twice.
    progress_data.pop("svg", None)

    progress_payload = {
        "type": "progress",
        "job_id": job_id,
        "stage": stage,
        "status": event.status,
        "message": message,
        "progress": event.progress,
        "slides_completed": updates.get("slides_completed", job.slides_completed),
        "total_slides": updates.get("total_slides", job.total_slides),
        "data": progress_data,
    }
    payloads.append((progress_payload, updates))

    if event.stage == "generation" and event.status == "progress" and event.data:
        slide_payload = {
            "type": "slide_ready",
            "job_id": job_id,
            "stage": event.stage,
            "status": event.status,
            "message": event.message,
            "progress": event.progress,
            "slides_completed": updates.get("slides_completed", job.slides_completed),
            "total_slides": updates.get("total_slides", job.total_slides),
            "data": {
                "page": event.data.get("page"),
                "svg": event.data.get("svg"),
                "completed_count": event.data.get("completed_count"),
                "generation_mode": event.data.get("generation_mode"),
            },
        }
        payloads.append((slide_payload, {}))

    if event.stage == "export" and event.status == "complete":
        complete_data: dict[str, Any] = {
            "output_path": updates.get("output_path"),
        }
        if event.data and event.data.get("critic"):
            complete_data["critic"] = event.data["critic"]
        complete_payload = {
            "type": "complete",
            "job_id": job_id,
            "stage": "export",
            "status": "complete",
            "message": event.message,
            "progress": 1.0,
            "slides_completed": updates.get("slides_completed", job.slides_completed),
            "total_slides": updates.get("total_slides", job.total_slides),
            "data": complete_data,
        }
        payloads.append((complete_payload, {}))

    if event.stage == "error" or event.status == "error":
        error_payload = {
            "type": "error",
            "job_id": job_id,
            "stage": stage,
            "status": "error",
            "message": message,
            "progress": job.progress,
            "slides_completed": job.slides_completed,
            "total_slides": job.total_slides,
            "data": {"error": message},
        }
        payloads.append((error_payload, {}))

    return payloads
