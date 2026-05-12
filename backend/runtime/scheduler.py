"""Immediate job runner.

The API process should never serialize user jobs behind a shared queue.
Each submitted generate/refine request gets its own asyncio task immediately,
with lifecycle state tracked by job_id. Resource-heavy sync work is still
isolated through ``backend.runtime.aoffload`` / async subprocess helpers, but
the scheduler itself has no backlog and no priority ordering.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


class SchedulerDraining(RuntimeError):
    """Submitted after the runner started shutting down."""


class QueueFull(RuntimeError):
    """Compatibility exception; immediate mode does not raise this."""


DEFAULT_PRIORITY = 10
JobRunner = Callable[[], Coroutine[Any, Any, None]]


@dataclass
class _RunningJob:
    job_id: str
    task: asyncio.Task[Any]
    submit_ts: float = field(default_factory=time.monotonic)
    priority: int = DEFAULT_PRIORITY


class Scheduler:
    """Immediate per-job task runner.

    ``max_concurrent`` and ``queue_capacity`` are accepted for backwards
    compatibility with tests/config, but they do not limit submissions.
    """

    def __init__(
        self,
        max_concurrent: int | None = None,
        queue_capacity: int | None = None,
    ) -> None:
        self._running: dict[str, _RunningJob] = {}
        self._draining = False
        self._started = False

    @property
    def queue_size(self) -> int:
        return 0

    @property
    def running_count(self) -> int:
        return sum(1 for item in self._running.values() if not item.task.done())

    def diagnostics(self) -> dict[str, Any]:
        return {
            "mode": "immediate",
            "started": self._started,
            "draining": self._draining,
            "queue_size": 0,
            "queue_capacity": 0,
            "queued_job_ids": [],
            "running_count": self.running_count,
            "running_job_ids": [
                job_id
                for job_id, item in self._running.items()
                if not item.task.done()
            ],
        }

    async def start(self) -> None:
        self._started = True
        self._draining = False

    async def shutdown(self, timeout: float = 30.0) -> None:
        self._draining = True
        deadline = time.monotonic() + timeout
        while self.running_count and time.monotonic() < deadline:
            await asyncio.sleep(0.1)

        for job_id, item in list(self._running.items()):
            if not item.task.done():
                logger.warning("scheduler: cancelling job %s on shutdown", job_id)
                item.task.cancel()

        for item in list(self._running.values()):
            try:
                await item.task
            except (asyncio.CancelledError, Exception):
                pass

        self._running.clear()
        self._started = False

    async def submit(
        self,
        job_id: str,
        runner: JobRunner,
        *,
        priority: int = DEFAULT_PRIORITY,
    ) -> int:
        """Start a job immediately. Returns 0 because there is no queue."""
        if self._draining:
            raise SchedulerDraining("scheduler is shutting down")
        if not self._started:
            await self.start()

        if job_id in self._running and not self._running[job_id].task.done():
            raise RuntimeError(f"job {job_id} is already running")

        task = asyncio.create_task(
            self._run_one(job_id, runner),
            name=f"job-{job_id}",
        )
        self._running[job_id] = _RunningJob(
            job_id=job_id,
            task=task,
            priority=priority,
        )
        return 0

    def cancel(self, job_id: str) -> bool:
        item = self._running.get(job_id)
        if item is None or item.task.done():
            return False
        item.task.cancel()
        return True

    def is_active(self, job_id: str) -> bool:
        item = self._running.get(job_id)
        return bool(item and not item.task.done())

    async def _run_one(self, job_id: str, runner: JobRunner) -> None:
        started = time.monotonic()
        logger.info("scheduler: starting independent job %s", job_id)
        try:
            await runner()
        except asyncio.CancelledError:
            logger.info("scheduler: job %s cancelled", job_id)
        except Exception:
            logger.exception("scheduler: job %s raised unhandled exception", job_id)
        finally:
            elapsed = time.monotonic() - started
            logger.info("scheduler: job %s finished after %.1fs", job_id, elapsed)
            current = self._running.get(job_id)
            if current is not None and current.task is asyncio.current_task():
                self._running.pop(job_id, None)


_scheduler: Scheduler | None = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler
