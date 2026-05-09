"""Single global ThreadPoolExecutor for offloading sync work.

The event loop runs on a single thread; any blocking call (file IO, fitz,
subprocess.run, python-pptx, PIL, cairosvg) must be pushed here so other
coroutines (HTTP handlers, websocket frames, the scheduler) keep running.

Sized for IO concurrency (default 16). For CPU-heavy stages we still rely
on the GIL releasing in C extensions (fitz / Pillow / cairosvg) plus
asyncio.create_subprocess_exec for true OS-level parallelism on pandoc /
pdflatex.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_pool: ThreadPoolExecutor | None = None


def init_offload(workers: int) -> None:
    """Create the global offload pool. Idempotent."""
    global _pool
    if _pool is not None:
        return
    if workers < 1:
        workers = 1
    _pool = ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="ppt-offload",
    )
    logger.info("offload pool started (workers=%d)", workers)


def shutdown_offload(wait: bool = True) -> None:
    """Tear down the global pool. Idempotent."""
    global _pool
    if _pool is None:
        return
    pool = _pool
    _pool = None
    try:
        pool.shutdown(wait=wait, cancel_futures=True)
    except TypeError:  # pragma: no cover — Python <3.9 compat
        pool.shutdown(wait=wait)
    logger.info("offload pool stopped")


def offload_stats() -> dict[str, int | bool | None]:
    """Best-effort diagnostics for the global offload executor."""
    pool = _pool
    if pool is None:
        return {
            "started": False,
            "max_workers": None,
            "queued": None,
            "threads": None,
        }
    queue = getattr(pool, "_work_queue", None)
    threads = getattr(pool, "_threads", None)
    queued = queue.qsize() if queue is not None and hasattr(queue, "qsize") else None
    return {
        "started": True,
        "max_workers": getattr(pool, "_max_workers", None),
        "queued": queued,
        "threads": len(threads) if threads is not None else None,
    }


async def aoffload(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """Run a sync callable on the global offload pool.

    Falls back to the loop's default executor if ``init_offload`` was never
    called (e.g. unit tests that import a module directly), so callers never
    have to defensively check.
    """
    loop = asyncio.get_running_loop()
    pool = _pool  # local to avoid TOCTOU during shutdown

    def _invoke() -> _T:
        return fn(*args, **kwargs)

    return await loop.run_in_executor(pool, _invoke)
