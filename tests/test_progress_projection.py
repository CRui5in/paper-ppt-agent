from __future__ import annotations

from types import SimpleNamespace

from backend.session.manager import Job
from backend.session.progress import (
    build_snapshot_event,
    describe_exception,
    payloads_from_progress_event,
)


def test_pending_snapshot_uses_started_status() -> None:
    job = Job(
        id="job-1",
        session_id="session-1",
        status="pending",
        progress=0.0,
        message="Generation started",
    )

    snapshot = build_snapshot_event(job.id, job)

    assert snapshot["stage"] == "parsing"
    assert snapshot["status"] == "started"


def test_generation_progress_keeps_svg_only_in_slide_ready() -> None:
    job = Job(id="job-1", session_id="session-1", status="generation", total_slides=3)
    event = SimpleNamespace(
        stage="generation",
        status="progress",
        message="Generated slide 2/3",
        progress=0.5,
        data={"page": 2, "svg": "<svg />", "completed_count": 1},
    )

    payloads = payloads_from_progress_event(job.id, job, event)

    progress_payload = payloads[0][0]
    slide_payload = payloads[1][0]
    assert "svg" not in progress_payload["data"]
    assert slide_payload["data"]["svg"] == "<svg />"


def test_error_payload_preserves_current_stage_and_nonempty_message() -> None:
    job = Job(
        id="job-1",
        session_id="session-1",
        status="research",
        progress=0.24,
        message="Generating manuscript",
    )
    event = SimpleNamespace(
        stage="error",
        status="error",
        message="",
        progress=0.24,
        data={},
    )

    payloads = payloads_from_progress_event(job.id, job, event)

    progress_payload = payloads[0][0]
    error_payload = payloads[1][0]
    assert progress_payload["stage"] == "research"
    assert error_payload["stage"] == "research"
    assert progress_payload["message"] == "Generation failed during research."
    assert error_payload["data"]["error"] == "Generation failed during research."


def test_describe_exception_handles_empty_timeout_message() -> None:
    assert describe_exception(TimeoutError(), "Generation failed") == (
        "Generation failed: upstream request timed out (TimeoutError)"
    )
