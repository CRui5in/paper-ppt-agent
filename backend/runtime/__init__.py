"""Async runtime primitives.

This package centralizes everything that bridges sync world (file IO,
subprocesses, CPU-bound libraries like fitz / python-pptx) into the asyncio
event loop. The single rule of the codebase is:

    No async function may call sync IO, subprocess, or fitz/PIL/python-pptx
    directly. Always go through ``aoffload`` / ``arun`` / the helpers in
    ``runtime.io``.

Public surface:

    aoffload(fn, *args, **kw)              — run a sync callable on the IO pool
    arun(argv, *, timeout, ...)            — async subprocess with hard timeout + SIGKILL
    aread_text / awrite_text / ...         — async file IO helpers
    Scheduler / EventBus                   — job dispatch and event fanout
"""

from .offload import aoffload, init_offload, offload_stats, shutdown_offload
from .subproc import SubprocessError, SubprocessTimeout, arun
from .io import (
    aread_text,
    awrite_text,
    aread_bytes,
    awrite_bytes,
    aensure_dir,
    apath_exists,
    aremove,
)

__all__ = [
    "aoffload",
    "init_offload",
    "offload_stats",
    "shutdown_offload",
    "arun",
    "SubprocessError",
    "SubprocessTimeout",
    "aread_text",
    "awrite_text",
    "aread_bytes",
    "awrite_bytes",
    "aensure_dir",
    "apath_exists",
    "aremove",
]
