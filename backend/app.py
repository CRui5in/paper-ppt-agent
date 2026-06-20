"""FastAPI application entrypoint."""

from __future__ import annotations

import atexit
import asyncio
import logging
import signal
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.router import api_router
from backend.api.websocket import router as websocket_router
from backend.config import settings
from backend.runtime.offload import init_offload, shutdown_offload

logger = logging.getLogger(__name__)


def _cleanup_all_job_processes() -> None:
    """Kill all tracked generation worker processes on API exit.

    Registered as an atexit handler so child processes are cleaned up
    whether the API exits normally, via Ctrl+C, or when the terminal
    window is closed on Windows.
    """
    from backend.runtime.worker_process_registry import (
        terminate_job_process_tree,
        _registry_dir,
    )
    import json

    registry = _registry_dir()
    if not registry.exists():
        return
    killed = 0
    for proc_file in registry.glob("*.json"):
        try:
            payload = json.loads(proc_file.read_text(encoding="utf-8"))
            job_id = str(payload.get("job_id", ""))
            if job_id:
                terminate_job_process_tree(job_id)
                killed += 1
        except Exception:
            pass
    if killed:
        logger.info("atexit: cleaned up %d job process(es)", killed)


atexit.register(_cleanup_all_job_processes)


def _install_signal_handlers() -> None:
    """Install signal handlers that trigger process cleanup before exit.

    On Windows, SIGINT (Ctrl+C) and SIGTERM are handled. Closing the
    terminal window sends CTRL_CLOSE_EVENT which Python translates to
    a KeyboardInterrupt or SystemExit — atexit still fires in those cases.
    """
    def _handler(signum: int, frame: object) -> None:
        logger.info("signal %d received, cleaning up job processes", signum)
        _cleanup_all_job_processes()
        raise SystemExit(128 + signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (OSError, ValueError, AttributeError):
            pass


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: bring up the runtime pool, scheduler, event bus.

    The order matters:

      1. ``init_offload`` first — every async helper above relies on the pool.
      2. Scheduler / EventBus next; they pull events through the pool.
      3. On shutdown we drain the scheduler so in-flight jobs get a chance
         to flush their final ``error: cancelled`` event before sockets close,
         then tear down the pool last.
    """
    _install_signal_handlers()
    init_offload(settings.io_pool_workers)
    try:
        # Scheduler / EventBus are wired in here once their modules land
        # (kept opt-in so the import graph stays clean during the rollout).
        try:
            from backend.runtime.scheduler import get_scheduler
            scheduler = get_scheduler()
            await scheduler.start()
            logger.info("scheduler started")
            from backend.runtime.job_event_monitor import resume_provider_job_monitors

            resume_provider_job_monitors()
        except ImportError:
            scheduler = None

        try:
            yield
        finally:
            _cleanup_all_job_processes()
            from backend.runtime.web_browser import close_url_browser

            await close_url_browser()
            if scheduler is not None:
                await scheduler.shutdown(timeout=30.0)
    finally:
        shutdown_offload()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Paper PPT Agent",
        version="0.1.0",
        description="Generate editable PowerPoint presentations from academic paper PDFs or TeX source packages.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    settings.workspaces_dir.mkdir(parents=True, exist_ok=True)
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)

    @app.get("/healthz")
    async def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/healthz/runtime")
    async def runtime_healthcheck() -> dict:
        from backend.runtime.offload import offload_stats
        from backend.runtime.scheduler import get_scheduler

        tasks = []
        current = asyncio.current_task()
        for task in asyncio.all_tasks():
            if task is current:
                continue
            tasks.append({
                "name": task.get_name(),
                "done": task.done(),
                "cancelled": task.cancelled(),
            })
        return {
            "status": "ok",
            "scheduler": get_scheduler().diagnostics(),
            "offload": offload_stats(),
            "tasks": tasks,
        }

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "name": "Paper PPT Agent",
            "frontend": "Open the Vite frontend to upload a paper PDF or TeX source package and generate a PPT draft.",
        }

    app.include_router(api_router)
    app.include_router(websocket_router)
    return app


app = create_app()
