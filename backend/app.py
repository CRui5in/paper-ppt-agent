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

async def _probe_vision_models() -> None:
    """Probe configured models for vision support at startup."""
    from backend.config import get_provider_credentials
    from backend.llm import create_provider
    from backend.llm.vision_probe import vision_probe

    models_str = (settings.vision_models or "").strip()
    if not models_str:
        logger.info("Vision probe: no VISION_MODELS configured, skipping")
        return

    models = [m.strip() for m in models_str.split(",") if m.strip()]
    if not models:
        return

    provider_name, api_key, base_url = get_provider_credentials()

    if not api_key:
        logger.warning("Vision probe: no API key for provider '%s', skipping", provider_name)
        return

    try:
        llm = create_provider(provider_name, api_key, base_url=base_url)
        logger.info("Vision probe: testing %d models via %s ...", len(models), provider_name)
        await vision_probe.probe_all(models, llm)
    except Exception as exc:
        logger.warning("Vision probe failed: %s", exc)


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

    # ── Vision capability probe (background, non-blocking) ──
    _probe_task = asyncio.create_task(_probe_vision_models())
    _probe_task.add_done_callback(
        lambda t: t.exception() and logger.warning("Vision probe task error: %s", t.exception())
    )
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

    @app.get("/healthz/vision")
    async def vision_probe_status() -> dict:
        """Return vision capability probe results for all configured models."""
        from backend.llm.vision_probe import vision_probe
        results = vision_probe.all_results()
        return {
            "probed": vision_probe.is_probed(),
            "models": {
                k: {
                    "model": v.model,
                    "supports_vision": v.supports_vision,
                    "response": v.response_excerpt[:60] if v.response_excerpt else None,
                    "error": v.error[:80] if v.error else None,
                }
                for k, v in results.items()
            },
            "vision_model_fallback": vision_probe.get_vision_model(),
        }

    app.include_router(api_router)
    app.include_router(websocket_router)
    return app


app = create_app()
