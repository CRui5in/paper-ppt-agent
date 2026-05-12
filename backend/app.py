"""FastAPI application entrypoint."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.router import api_router
from backend.api.websocket import router as websocket_router
from backend.config import settings
from backend.runtime.offload import init_offload, shutdown_offload

logger = logging.getLogger(__name__)


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
    init_offload(settings.io_pool_workers)
    try:
        # Scheduler / EventBus are wired in here once their modules land
        # (kept opt-in so the import graph stays clean during the rollout).
        try:
            from backend.runtime.scheduler import get_scheduler
            scheduler = get_scheduler()
            await scheduler.start()
            logger.info("scheduler started")
        except ImportError:
            scheduler = None

        try:
            yield
        finally:
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
