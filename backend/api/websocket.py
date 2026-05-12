"""WebSocket endpoint for job progress updates.

Wire protocol
-------------
Server → client messages are JSON objects. Every progress/slide_ready/
complete/error frame carries a monotonically increasing ``seq`` (per job)
and ``ts`` (server epoch seconds), assigned in :mod:`session.manager`.

Connection lifecycle:

1. Client opens ``/ws/{job_id}?since_seq=<int>``. ``since_seq`` defaults
   to 0 and represents the last seq the client has already processed.
2. Server immediately sends a ``snapshot`` event reflecting current job
   state, then replays any retained events with ``seq > since_seq``.
3. Server then streams new events as they arrive.
4. Server emits ``{"type": "ping", "ts": ...}`` every ``settings.ws_heartbeat_seconds``
   so reverse proxies and browsers don't tear the socket down during long
   silent stages. Clients can ignore pings (or echo them as ``pong``).

If the underlying job has reached a terminal state and there is nothing
left to deliver, the server closes the socket cleanly.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.config import settings
from backend.session.manager import session_manager
from backend.session.progress import build_snapshot_event

router = APIRouter()

# Heartbeat interval is configurable so reverse-proxy timeouts can be tuned
# from the environment. Default 15s comfortably beats nginx's 60s default
# ``proxy_read_timeout`` and keeps Cloudflare-style proxies happy.
def _heartbeat_seconds() -> float:
    return float(max(1, settings.ws_heartbeat_seconds))

TERMINAL_STATUSES = {"complete", "error", "cancelled"}


@router.websocket("/ws/{job_id}")
async def job_updates(websocket: WebSocket, job_id: str) -> None:
    await websocket.accept()

    # Parse ``?since_seq=N``. Anything malformed → start from 0 (full replay).
    since_seq_raw = websocket.query_params.get("since_seq", "0")
    try:
        since_seq = max(0, int(since_seq_raw))
    except (TypeError, ValueError):
        since_seq = 0

    queue = session_manager.subscribe_ws(job_id)
    try:
        job = session_manager.get_job(job_id)
        if job is None:
            await websocket.send_json({
                "type": "error",
                "job_id": job_id,
                "stage": "error",
                "status": "error",
                "message": "Job not found.",
                "progress": 0.0,
                "slides_completed": 0,
                "total_slides": 0,
                "data": {"error": "not_found"},
            })
            # 1008 (policy violation) tells the client the resource is
            # permanently gone — it must not reconnect.
            await websocket.close(code=1008)
            return

        # 1. Always send a snapshot first so the client can sync its
        #    coarse state (status / progress / counts) immediately.
        snapshot = build_snapshot_event(job_id, job)
        snapshot["last_seq"] = job.last_seq
        await websocket.send_json(snapshot)

        # 2. Replay any retained events the client missed. We drop those
        #    that are already in the live ``queue`` to avoid duplicate
        #    delivery — the queue may contain only events that arrived
        #    after subscribe, so this is mostly defensive.
        replayed_seqs: set[int] = set()
        for event in session_manager.get_events_after(job_id, since_seq):
            seq = int(event.get("seq", 0) or 0)
            if seq:
                replayed_seqs.add(seq)
            await websocket.send_json(event)

        # If the job is already terminal and we've replayed everything,
        # close cleanly so the client stops reconnecting. Use an explicit
        # code=1000 — Starlette's default close has no status code, which
        # the browser surfaces as 1005 and our reconnect loop interprets
        # as an abnormal disconnect.
        job = session_manager.get_job(job_id)
        if job and job.status in TERMINAL_STATUSES and not session_manager.is_job_running(job_id):
            # Drain any queued events that were enqueued before subscribe
            # but not yet consumed by the queue iterator.
            await _drain_queue_into_socket(websocket, queue, replayed_seqs)
            await websocket.close(code=1000)
            return

        # 3. Stream live events with periodic heartbeats. ``asyncio.wait``
        #    races the queue against a heartbeat timer so the socket stays
        #    warm during long silent stages (e.g. LLM long calls).
        while True:
            event = await _next_event_or_heartbeat(websocket, queue)
            if event is None:
                # Heartbeat already sent inside helper; loop again.
                continue
            seq = int(event.get("seq", 0) or 0)
            if seq and seq in replayed_seqs:
                continue  # already delivered during replay
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    except Exception:
        # Any unexpected error → close gracefully so the client can retry.
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        session_manager.unsubscribe_ws(job_id, queue)


async def _next_event_or_heartbeat(
    websocket: WebSocket,
    queue: asyncio.Queue,
) -> dict | None:
    """Wait for the next event or send a heartbeat on timeout.

    Returns the event dict, or ``None`` if a heartbeat was sent and the
    caller should keep waiting.
    """
    try:
        return await asyncio.wait_for(queue.get(), timeout=_heartbeat_seconds())
    except asyncio.TimeoutError:
        await websocket.send_json({
            "type": "ping",
            "ts": _now(),
        })
        return None


async def _drain_queue_into_socket(
    websocket: WebSocket,
    queue: asyncio.Queue,
    already_sent: set[int],
) -> None:
    """Best-effort drain of any pending queue items before close."""
    while not queue.empty():
        try:
            event = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        seq = int(event.get("seq", 0) or 0)
        if seq and seq in already_sent:
            continue
        try:
            await websocket.send_json(event)
        except Exception:
            return


def _now() -> float:
    import time

    return time.time()
