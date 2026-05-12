from __future__ import annotations

import asyncio

import pytest

from backend.usage.tracker import (
    current_usage_context,
    reset_usage_context,
    set_usage_context,
)


def test_usage_context_can_clear_page() -> None:
    snapshot = set_usage_context(job_id="job-a", stage="generation", page=7, attempt=2)
    try:
        set_usage_context(stage="strategy", page=None, attempt=1)

        ctx = current_usage_context()
        assert ctx["job_id"] == "job-a"
        assert ctx["stage"] == "strategy"
        assert ctx["page"] is None
        assert ctx["attempt"] == 1
    finally:
        reset_usage_context(snapshot)


@pytest.mark.asyncio
async def test_usage_context_is_isolated_between_async_tasks() -> None:
    snapshot = set_usage_context(job_id="root", stage="research", page=None, attempt=1)

    async def worker(job_id: str, page: int, pause: float) -> dict:
        set_usage_context(job_id=job_id, stage="generation", page=page, attempt=1)
        await asyncio.sleep(pause)
        return current_usage_context()

    try:
        first, second = await asyncio.gather(
            worker("job-a", 1, 0.02),
            worker("job-b", 9, 0.0),
        )

        assert first["job_id"] == "job-a"
        assert first["page"] == 1
        assert second["job_id"] == "job-b"
        assert second["page"] == 9
        assert current_usage_context()["job_id"] == "root"
    finally:
        reset_usage_context(snapshot)
