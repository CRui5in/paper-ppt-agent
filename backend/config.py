"""Global configuration using Pydantic Settings."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_resource_root() -> Path:
    override = os.getenv("PAPER_PPT_AGENT_PROJECT_ROOT")
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        bundle_dir = getattr(sys, "_MEIPASS", None)
        if bundle_dir:
            return Path(bundle_dir).resolve()
    return Path(__file__).resolve().parent.parent


def _resolve_data_root(resource_root: Path) -> Path:
    override = os.getenv("PAPER_PPT_AGENT_DATA_DIR")
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data).resolve() / "PaperPPTAgent"
        return Path.home() / "AppData" / "Local" / "PaperPPTAgent"
    return resource_root


RESOURCE_ROOT = _resolve_resource_root()
DATA_ROOT = _resolve_data_root(RESOURCE_ROOT)


def _load_env_files() -> None:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / ".env")
    candidates.extend([
        Path.cwd() / ".env",
        RESOURCE_ROOT / ".env",
    ])
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen or not resolved.exists():
            continue
        load_dotenv(resolved, override=False)
        seen.add(resolved)


_load_env_files()

# Canvas format definitions (adapted from ppt-master)
CANVAS_FORMATS = {
    "ppt169": {
        "name": "PPT 16:9",
        "width": 1280,
        "height": 720,
        "viewbox": "0 0 1280 720",
        "ratio": "16:9",
    },
    "ppt43": {
        "name": "PPT 4:3",
        "width": 1024,
        "height": 768,
        "viewbox": "0 0 1024 768",
        "ratio": "4:3",
    },
}

# Design color schemes
DESIGN_STYLES = {
    "academic": {
        "name": "Academic",
        "background": "#FFFFFF",
        "primary": "#1A365D",
        "accent": "#2B6CB0",
        "body_text": "#2D3748",
    },
    "consulting": {
        "name": "Consulting",
        "background": "#FFFFFF",
        "primary": "#003A70",
        "accent": "#0077B6",
        "body_text": "#1A202C",
    },
    "tech": {
        "name": "Tech",
        "background": "#0F172A",
        "primary": "#3B82F6",
        "accent": "#06B6D4",
        "body_text": "#E2E8F0",
    },
    "general": {
        "name": "General",
        "background": "#FFFFFF",
        "primary": "#4F46E5",
        "accent": "#7C3AED",
        "body_text": "#374151",
    },
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # LLM defaults
    default_llm_provider: Literal["openai", "deepseek", "anthropic", "gemini"] = "openai"
    default_llm_model: str = "gpt-4o"
    openai_api_key: str | None = None
    deepseek_api_key: str | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None

    # Paper parsing
    mineru_api_key: str | None = None
    mineru_api_url: str | None = None

    # Image generation
    image_backend: str | None = None

    # Paths
    assets_dir: Path = RESOURCE_ROOT / "assets"
    workspaces_dir: Path = DATA_ROOT / "workspaces"
    runtime_dir: Path = DATA_ROOT / ".runtime"
    templates_dir: Path = RESOURCE_ROOT / "assets" / "templates"
    icons_dir: Path = RESOURCE_ROOT / "assets" / "icons"
    references_dir: Path = RESOURCE_ROOT / "assets" / "references"

    # Limits
    # Historical compatibility knob. Job scheduling is now immediate and
    # per-job; this no longer caps generate/refine submissions.
    max_concurrent_jobs: int = 1
    max_upload_bytes: int = 64 * 1024 * 1024  # 64 MB per uploaded paper

    # ── Async runtime ────────────────────────────────────────────────────
    # Size of the global ThreadPoolExecutor used by ``runtime.aoffload``.
    # All blocking file IO and CPU-bound library calls (fitz, python-pptx,
    # cairosvg, PIL) flow through this pool, so size it for IO concurrency
    # rather than core count.
    io_pool_workers: int = 16

    # ── Job scheduling ───────────────────────────────────────────────────
    # Backlog cap for queued jobs; a 16th queued job returns 429.
    job_queue_capacity: int = 16

    # ── External tool timeouts (seconds) ─────────────────────────────────
    pandoc_timeout: int = 60
    pdflatex_timeout: int = 90
    cairosvg_timeout: int = 30
    # Number of parallel equation renders allowed in flight.
    equation_render_concurrency: int = 4

    # ── WebSocket ────────────────────────────────────────────────────────
    ws_subscriber_queue_size: int = 1024
    ws_heartbeat_seconds: int = 15

    # ── Persistence ──────────────────────────────────────────────────────
    # Debounce window (ms) for full session_state.json snapshots. Events
    # always go to the per-job NDJSON stream synchronously so nothing is
    # lost on a hard crash; the snapshot just rolls up indices.
    persist_debounce_ms: int = 200


settings = Settings()
