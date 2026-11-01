"""Bridge durable worker event logs back into live API session state."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.runtime import aoffload
from backend.runtime.job_event_log import read_messages_after
from backend.runtime.job_request import serialize_generation_request
from backend.session.manager import session_manager
from backend.session.progress import payloads_from_progress_event


logger = logging.getLogger(__name__)
TERMINAL_STATUSES = {"complete", "error", "cancelled"}
_offsets: dict[str, int] = {}
_locks: dict[str, asyncio.Lock] = {}
_worker_stderr: dict[str, list[str]] = {}


@dataclass
class _ProgressEvent:
    stage: str
    status: str
    message: str = ""
    progress: float = 0.0
    data: dict | None = None


async def monitor_job_events(job_id: str) -> None:
    while True:
        await sync_job_events_once(job_id)
        job = session_manager.get_job(job_id)
        if job is None or job.status in TERMINAL_STATUSES:
            return
        await asyncio.sleep(0.25)


def resume_provider_job_monitors() -> None:
    for job in session_manager.list_jobs():
        if job.status in TERMINAL_STATUSES:
            continue
        if job.worker_backend != "huey-sqlite":
            continue
        if session_manager.has_registered_task(job.id):
            continue
        task = asyncio.create_task(
            monitor_job_events(job.id),
            name=f"job-{job.id}-event-monitor",
        )
        session_manager.register_task(job.id, task)


async def sync_job_events_once(job_id: str) -> None:
    lock = _locks.setdefault(job_id, asyncio.Lock())
    async with lock:
        offset = _offsets.get(job_id, 0)
        offset, messages = await aoffload(read_messages_after, job_id, offset)
        _offsets[job_id] = offset
        for message in messages:
            await _apply_message(job_id, message)
        await _mark_lost_worker_if_needed(job_id)


async def _apply_message(job_id: str, message: dict[str, Any]) -> None:
    job = session_manager.get_job(job_id)
    if job is None:
        return
    if job.status in TERMINAL_STATUSES:
        return

    msg_type = message.get("type")
    if msg_type == "worker_stdout":
        payload = message.get("payload")
        if not isinstance(payload, str):
            return
        try:
            worker_message = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("job %s worker emitted non-json stdout: %s", job_id, payload[:500])
            return
        await _apply_worker_message(job_id, worker_message)
        return

    if msg_type == "worker_stderr":
        payload = str(message.get("payload") or "").strip()
        if payload:
            lines = _worker_stderr.setdefault(job_id, [])
            lines.append(payload)
            del lines[:-20]
        return

    if msg_type == "worker_timeout":
        _record_error(job_id, str(message.get("message") or "Generation worker timed out."))
        return

    if msg_type == "worker_exit":
        returncode = int(message.get("returncode", 0) or 0)
        if returncode != 0:
            stderr_tail = _worker_stderr.get(job_id) or []
            detail = _best_worker_stderr_detail(stderr_tail)
            suffix = f" {detail}" if detail else ""
            _record_error(job_id, f"Generation worker exited with code {returncode}.{suffix}")


def _best_worker_stderr_detail(lines: list[str]) -> str:
    for line in reversed(lines):
        text = line.strip()
        if not text:
            continue
        if text.startswith(("INFO:", "WARNING:")):
            continue
        return text[:500]
    return ""


async def _apply_worker_message(job_id: str, message: dict[str, Any]) -> None:
    if message.get("type") != "event" or not isinstance(message.get("event"), dict):
        return
    event_raw = message["event"]
    event = _ProgressEvent(
        stage=str(event_raw.get("stage") or "generation"),
        status=str(event_raw.get("status") or "progress"),
        message=str(event_raw.get("message") or ""),
        progress=float(event_raw.get("progress", 0.0) or 0.0),
        data=event_raw.get("data") if isinstance(event_raw.get("data"), dict) else None,
    )
    job = session_manager.get_job(job_id)
    if job is None:
        return
    for payload, updates in payloads_from_progress_event(job_id, job, event):
        session_manager.record_event(job_id, payload, **updates)
        job = session_manager.get_job(job_id) or job


def _record_error(job_id: str, message: str) -> None:
    job = session_manager.get_job(job_id)
    if job is None or job.status in TERMINAL_STATUSES:
        return
    event = _ProgressEvent("error", "error", message, job.progress)
    for payload, updates in payloads_from_progress_event(job_id, job, event):
        session_manager.record_event(job_id, payload, **updates)


async def _mark_lost_worker_if_needed(job_id: str) -> None:
    job = session_manager.get_job(job_id)
    if job is None or job.status in TERMINAL_STATUSES:
        return
    if getattr(job, "worker_backend", None) != "huey-sqlite":
        return

    from backend.runtime.worker_process_registry import (
        has_job_process_record,
        is_job_process_lost,
        unregister_job_process,
    )

    has_record = await aoffload(has_job_process_record, job_id)
    lost = await aoffload(is_job_process_lost, job_id)
    if not has_record and job.status != "pending":
        lost = True
    if not lost:
        return
    await aoffload(unregister_job_process, job_id)
    _record_error(
        job_id,
        "Generation worker process exited or was lost before reporting completion.",
    )


async def write_generation_request(job_id: str, request: Any) -> Path:
    target_dir = settings.runtime_dir / "job_requests"
    target = target_dir / f"{job_id}.json"
    payload = {
        "job_id": job_id,
        "request": serialize_generation_request(request),
    }

    def _write() -> None:
        from backend.runtime.io import _write_text_atomic
        _write_text_atomic(target, json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")

    await aoffload(_write)
    return target
