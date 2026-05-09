from __future__ import annotations

import asyncio

from backend.runtime.scheduler import Scheduler


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


def test_scheduler_starts_jobs_immediately_without_queueing():
    async def scenario() -> None:
        scheduler = Scheduler(max_concurrent=1, queue_capacity=1)
        started: list[str] = []
        release = asyncio.Event()

        async def job(name: str) -> None:
            started.append(name)
            await release.wait()

        await scheduler.start()
        try:
            first_size = await scheduler.submit("first", lambda: job("first"))
            second_size = await scheduler.submit("second", lambda: job("second"))

            assert first_size == 0
            assert second_size == 0
            await _wait_until(lambda: set(started) == {"first", "second"})
            assert scheduler.queue_size == 0
            assert scheduler.running_count == 2
        finally:
            release.set()
            await scheduler.shutdown(timeout=0.1)

    asyncio.run(scenario())


def test_scheduler_cancel_only_targets_requested_job():
    async def scenario() -> None:
        scheduler = Scheduler()
        release = asyncio.Event()
        started: list[str] = []
        cancelled: list[str] = []
        completed: list[str] = []

        async def job(name: str) -> None:
            try:
                started.append(name)
                await release.wait()
                completed.append(name)
            except asyncio.CancelledError:
                cancelled.append(name)
                raise

        await scheduler.start()
        try:
            await scheduler.submit("first", lambda: job("first"))
            await scheduler.submit("second", lambda: job("second"))
            await _wait_until(lambda: set(started) == {"first", "second"})

            assert scheduler.cancel("first") is True
            await _wait_until(lambda: cancelled == ["first"])
            assert scheduler.is_active("second") is True
            release.set()
            await _wait_until(lambda: completed == ["second"])
        finally:
            release.set()
            await scheduler.shutdown(timeout=0.1)

    asyncio.run(scenario())
