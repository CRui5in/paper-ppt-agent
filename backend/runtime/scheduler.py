"""Job scheduler with bounded queue and configurable concurrency.

Every long-running job (generate / refine) is submitted here instead of
being launched directly from the HTTP handler. This guarantees:

    * the HTTP handler returns in O(ms) regardless of how many jobs are
      already queued or running — fixing the symptom where "starting a new
      PPT" hung while another job was in the parsing stage;

    * total in-flight work is capped by ``settings.max_concurrent_jobs``,
      so we never exhaust the global IO pool / external tools (pandoc,
      pdflatex) by overloading them with parallel runs;

    * the queue itself has a hard cap (``settings.job_queue_capacity``)
      so an attacker / runaway client can't grow memory unbounded; the
      submit path returns ``QueueFull`` which the API surface translates
      into HTTP 429.

Cancellation has two-stage semantics:

    * If the job is *running*, ``cancel(job_id)`` calls ``task.cancel()``
      which propagates ``asyncio.CancelledError`` into the coroutine; the
      pipeline catches that, runs cleanup, and emits a ``cancelled``
      progress event.

    * If the job is still *queued*, ``cancel`` flips it in the cancel set
      and the worker skips it on dequeue. We don't try to delete from the
      PriorityQueue (that's O(n) and racy); lazy skip is simpler and
      correct.

The scheduler does not own job state — it only owns the *task lifecycle*.
``SessionManager`` continues to be the source of truth for ``Job`` rows.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from backend.config import settings

logger = logging.getLogger(__name__)


class SchedulerDraining(RuntimeError):
    """Submitted after the scheduler started shutting down."""


class QueueFull(RuntimeError):
    """The bounded queue is at capacity. Caller should map to HTTP 429."""


# Lower priority value = runs sooner. Default 10; refines could pass 5
# in the future to jump the queue, generate stays at 10.
DEFAULT_PRIORITY = 10
JobRunner = Callable[[], Coroutine[Any, Any, None]]


@dataclass(order=True)
class _QueueItem:
    priority: int
    submit_ts: float = field(compare=True)
    job_id: str = field(compare=False)
    runner: JobRunner = field(compare=False)


class Scheduler:
    """Single-process priority scheduler with N concurrent workers."""

    def __init__(
        self,
        max_concurrent: int | None = None,
        queue_capacity: int | None = None,
    ) -> None:
        self._max_concurrent = max_concurrent or settings.max_concurrent_jobs
        capacity = queue_capacity or settings.job_queue_capacity
        self._queue: asyncio.PriorityQueue[_QueueItem] = asyncio.PriorityQueue(
            maxsize=capacity
        )
        self._running: dict[str, asyncio.Task[Any]] = {}
        self._cancelled_queued: set[str] = set()
        self._sem = asyncio.Semaphore(self._max_concurrent)
        self._workers: list[asyncio.Task[None]] = []
        self._draining = False
        self._started = False

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def running_count(self) -> int:
        return sum(1 for t in self._running.values() if not t.done())

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        # We run a single dispatcher loop and use a Semaphore for concurrency
        # control. This keeps priority semantics intact: every dequeued item
        # is the highest-priority one *at that moment*, even when many are
        # queued at the same time as a slot frees up.
        self._workers.append(
            asyncio.create_task(self._dispatch_loop(), name="scheduler-dispatch")
        )

    async def shutdown(self, timeout: float = 30.0) -> None:
        """Stop accepting new jobs, wait for in-flight ones, then return.

        Workers in flight get ``timeout`` seconds to flush their final
        progress event before we cancel them outright.
        """
        self._draining = True

        deadline = time.monotonic() + timeout
        while self._running and time.monotonic() < deadline:
            await asyncio.sleep(0.1)

        for jid, task in list(self._running.items()):
            if not task.done():
                logger.warning("scheduler: cancelling job %s on shutdown", jid)
                task.cancel()

        for w in self._workers:
            w.cancel()
        for w in self._workers:
            try:
                await w
            except (asyncio.CancelledError, Exception):
                pass
        self._workers.clear()

    async def submit(
        self,
        job_id: str,
        runner: JobRunner,
        *,
        priority: int = DEFAULT_PRIORITY,
    ) -> int:
        """Enqueue a job. Returns the queue size after insertion."""
        if self._draining:
            raise SchedulerDraining("scheduler is shutting down")
        if not self._started:
            await self.start()
        item = _QueueItem(
            priority=priority,
            submit_ts=time.monotonic(),
            job_id=job_id,
            runner=runner,
        )
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull as exc:
            raise QueueFull(
                f"job queue at capacity ({self._queue.maxsize})"
            ) from exc
        return self._queue.qsize()

    def cancel(self, job_id: str) -> bool:
        """Cancel a job whether it's running or still queued.

        Returns True if a cancellation was actually delivered.
        """
        task = self._running.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            return True
        # Queued: lazy mark; the dispatcher will skip it on dequeue.
        self._cancelled_queued.add(job_id)
        return True

    def is_active(self, job_id: str) -> bool:
        if job_id in self._running:
            return True
        return any(
            item.job_id == job_id
            for item in list(getattr(self._queue, "_queue", []))  # type: ignore[attr-defined]
        )

    async def _dispatch_loop(self) -> None:
        while not self._draining:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                return

            if item.job_id in self._cancelled_queued:
                self._cancelled_queued.discard(item.job_id)
                logger.info("scheduler: skipping cancelled queued job %s", item.job_id)
                continue

            await self._sem.acquire()
            asyncio.create_task(
                self._run_one(item),
                name=f"scheduler-run-{item.job_id}",
            )

    async def _run_one(self, item: _QueueItem) -> None:
        job_id = item.job_id
        try:
            wait_secs = time.monotonic() - item.submit_ts
            logger.info(
                "scheduler: starting job %s (queued %.1fs)", job_id, wait_secs
            )
            task = asyncio.create_task(item.runner(), name=f"job-{job_id}")
            self._running[job_id] = task
            try:
                await task
            except asyncio.CancelledError:
                logger.info("scheduler: job %s cancelled", job_id)
            except Exception:
                logger.exception("scheduler: job %s raised unhandled exception", job_id)
        finally:
            self._running.pop(job_id, None)
            self._sem.release()


_scheduler: Scheduler | None = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler
