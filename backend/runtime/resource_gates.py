"""Global async resource gates for expensive shared resources."""

from __future__ import annotations

import asyncio
import weakref
from contextlib import asynccontextmanager
from typing import AsyncIterator

from backend.config import settings


_llm_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)
_heavy_stage_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _gate_for(
    gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore],
    limit: int,
) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    gate = gates.get(loop)
    if gate is None:
        gate = asyncio.Semaphore(max(1, int(limit or 1)))
        gates[loop] = gate
    return gate


@asynccontextmanager
async def llm_request_slot() -> AsyncIterator[None]:
    """Limit total in-flight LLM API calls across independent jobs."""
    gate = _gate_for(_llm_gates, settings.llm_max_concurrent_requests)
    async with gate:
        yield


@asynccontextmanager
async def heavy_stage_slot() -> AsyncIterator[None]:
    """Limit CPU-heavy parse/finalize/export stages across jobs."""
    gate = _gate_for(_heavy_stage_gates, settings.heavy_stage_max_concurrent)
    async with gate:
        yield
