"""Global configuration using Pydantic Settings."""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

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
    assets_dir: Path = PROJECT_ROOT / "assets"
    workspaces_dir: Path = PROJECT_ROOT / "workspaces"
    runtime_dir: Path = PROJECT_ROOT / ".runtime"
    templates_dir: Path = PROJECT_ROOT / "assets" / "templates"
    icons_dir: Path = PROJECT_ROOT / "assets" / "icons"
    references_dir: Path = PROJECT_ROOT / "assets" / "references"
    codex_bin: Path | None = None

    # Limits
    # Historical compatibility knob. The bounded runtime scheduler below is
    # the active concurrency control for generate/refine submissions.
    max_concurrent_jobs: int = 1
    max_upload_bytes: int = 64 * 1024 * 1024  # 64 MB per uploaded paper

    # ── Async runtime ────────────────────────────────────────────────────
    # Size of the global ThreadPoolExecutor used by ``runtime.aoffload``.
    # All blocking file IO and CPU-bound library calls (fitz, python-pptx,
    # cairosvg, PIL) flow through this pool, so size it for IO concurrency
    # rather than core count.
    io_pool_workers: int = 16
    # Cap expensive CPU stages across independent jobs. Jobs may run at the
    # same time, but parsing/finalize/export should not all saturate the host
    # and starve normal API requests.
    heavy_stage_max_concurrent: int = 1
    # Cap total in-flight LLM requests across all jobs. Per-job/page
    # parallelism still exists; this prevents two active parallel jobs from
    # multiplying into a provider/network storm.
    llm_max_concurrent_requests: int = 4
    # Per-request timeout (seconds) for LLM SDK calls. The SDK default is
    # 600s with 2 internal retries, so a hung upstream (common with API
    # aggregators/proxies) silently blocks a slide for up to 30 min. We set
    # an explicit bound so genuine hangs surface and retry within minutes,
    # while leaving headroom for slow reasoning calls (strategy ~227s seen,
    # may run longer for dense papers on slower models like Opus).
    llm_request_timeout: float = 420.0
    # SVG generation often needs more than 8k output tokens for dense,
    # editable slides. Keep this configurable because provider-compatible
    # endpoints expose different per-model output caps.
    svg_generation_max_tokens: int = 16384
    svg_generation_max_tokens_ceiling: int = 32768
    svg_repair_max_tokens: int = 8192
    # ── Job scheduling ───────────────────────────────────────────────────
    # Backlog cap for queued jobs; submissions beyond this return 429.
    job_queue_capacity: int = 16
    # Number of generation/refine/agent jobs that may run at once. Provider
    # jobs still run in isolated child processes; this is the API-side
    # admission limit before those children are launched.
    generation_worker_concurrency: int = 4

    # ── External tool timeouts (seconds) ─────────────────────────────────
    pandoc_timeout: int = 60
    pdflatex_timeout: int = 90
    cairosvg_timeout: int = 30
    # ── Multi-source URL fetching ────────────────────────────────────────
    # Per-request timeout for the url source's HTTP fetch. Matches the web
    # research reader in research_enrichment (20s) so a slow site doesn't
    # stall the whole pipeline.
    url_fetch_timeout: float = 20.0
    # Cap on downloaded HTML before trafilatura extraction. Protects against
    # accidental multi-MB pages; the extracted body is typically far smaller.
    url_fetch_max_bytes: int = 5_000_000
    # Hard cap on sources per session (papers + pasted text + urls combined).
    max_sources_per_session: int = 20
    # Number of parallel equation renders allowed in flight.
    equation_render_concurrency: int = 4
    agent_runtime_ready_timeout: int = 30
    # Optional hard limit for Agent SDK stream silence. Disabled by default so
    # long-running Codex/Claude turns are surfaced through idle notices instead
    # of being stopped as failures. Set >0 to re-enable a hard stop.
    agent_turn_idle_timeout: int = 0

    # ── WebSocket ────────────────────────────────────────────────────────
    ws_subscriber_queue_size: int = 1024
    ws_heartbeat_seconds: int = 15

    # ── Persistence ──────────────────────────────────────────────────────
    # Debounce window (ms) for full session_state.json snapshots. Events
    # always go to the per-job NDJSON stream synchronously so nothing is
    # lost on a hard crash; the snapshot just rolls up indices.
    persist_debounce_ms: int = 200

    # ── Template import v2 (gradual rollout) ─────────────────────────────
    template_import_v2: bool = False  # gradual rollout — when True, route through v2 pipeline


settings = Settings()
