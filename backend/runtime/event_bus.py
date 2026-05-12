"""Per-job event fan-out with bounded queues and a server-driven heartbeat.

This is the live event channel used by the websocket endpoint. It does not
own durability — committed events live in ``SessionManager`` (in-memory ring
+ NDJSON disk log). The bus only buffers events for connected subscribers
and exposes an ``async for`` interface that yields:

    * ``replay`` events that arrived while the subscriber was disconnected
      (delivered first, drained from the SessionManager ring);
    * ``live`` events as they are published;
    * synthetic ``heartbeat`` frames every ``ws_heartbeat_seconds`` so
      proxies don't kill idle WebSocket connections;
    * synthetic ``degraded`` frames when the per-subscriber queue overflows
      (the client can surface "network congestion, frames dropped").

Bounded queue + drop-oldest is intentional: we'd rather show the user the
*latest* progress than freeze the producer because one slow client can't
keep up. The ``dropped`` count is reported back so the UI can display it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from backend.config import settings

logger = logging.getLogger(__name__)


class _Subscriber:
    __slots__ = ("queue", "dropped", "buffer")

    def __init__(self, capacity: int) -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=capacity)
        self.dropped: int = 0
        # Mirror of last frame to coalesce the next ``degraded`` notice.
        self.buffer: deque[dict[str, Any]] = deque(maxlen=1)

    def offer(self, event: dict[str, Any]) -> None:
        """Non-blocking publish. Drops the *oldest* live frame on overflow.

        Heartbeat / degraded frames are never themselves dropped; they're
        cheap and the client uses them for liveness signaling.
        """
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover
                pass
            self.dropped += 1
            try:
                self.queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover
                pass


class EventBus:
    """In-memory fan-out for live job events.

    Singleton-style: one bus per process, accessed via ``get_event_bus``.
    Decoupled from ``SessionManager`` so the persistence layer can evolve
    independently of the transport.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[_Subscriber]] = {}
        self._lock = asyncio.Lock()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._stopped = False

    async def start(self) -> None:
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="event-bus-heartbeat"
            )

    async def stop(self) -> None:
        self._stopped = True
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
            self._heartbeat_task = None

    async def publish(self, job_id: str, event: dict[str, Any]) -> None:
        """Push an event to every subscriber of *job_id*."""
        subs = self._subscribers.get(job_id)
        if not subs:
            return
        for sub in subs:
            sub.offer(event)
            if sub.dropped and sub.dropped % 8 == 1:
                sub.offer(
                    {
                        "type": "degraded",
                        "job_id": job_id,
                        "dropped": sub.dropped,
                        "ts": time.time(),
                    }
                )

    @asynccontextmanager
    async def subscribe(self, job_id: str) -> AsyncIterator[_Subscriber]:
        sub = _Subscriber(capacity=settings.ws_subscriber_queue_size)
        async with self._lock:
            self._subscribers.setdefault(job_id, []).append(sub)
        try:
            yield sub
        finally:
            async with self._lock:
                bucket = self._subscribers.get(job_id)
                if bucket:
                    self._subscribers[job_id] = [s for s in bucket if s is not sub]
                    if not self._subscribers[job_id]:
                        self._subscribers.pop(job_id, None)

    async def _heartbeat_loop(self) -> None:
        interval = max(1, int(settings.ws_heartbeat_seconds))
        while not self._stopped:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            now = time.time()
            for job_id, subs in list(self._subscribers.items()):
                frame = {"type": "heartbeat", "job_id": job_id, "ts": now}
                for sub in subs:
                    sub.offer(frame)


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
