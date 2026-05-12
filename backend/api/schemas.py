"""Pydantic request/response models for the API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class FileInfo(BaseModel):
    name: str
    size: int
    source_type: str  # "pdf" or "latex"


class UploadResponse(BaseModel):
    session_id: str
    file_info: FileInfo


class DeepSeekSettings(BaseModel):
    thinking_enabled: bool = True
    reasoning_effort: Literal["high", "max"] = "max"


class OpenAISettings(BaseModel):
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"] = "medium"
    verbosity: Literal["low", "medium", "high"] = "high"


class ModelConfig(BaseModel):
    provider: str  # "openai", "deepseek", "anthropic", "gemini"
    model: str
    api_key: str
    base_url: str | None = None
    deepseek_settings: DeepSeekSettings | None = None
    openai_settings: OpenAISettings | None = None


class StyleOverrides(BaseModel):
    palette: list[str] | None = None  # e.g. ["#0b1220", "#ff8a3d", "#f5f7fb"]
    font: str | None = None           # Default font for all text
    font_heading: str | None = None   # Western heading font (advanced mode)
    font_body: str | None = None      # Western body font (advanced mode)
    cjk_heading: str | None = None    # CJK heading font (advanced mode)
    cjk_body: str | None = None       # CJK body font (advanced mode)
    density: str | None = None        # "compact" | "normal" | "spacious"


class ResearchConfig(BaseModel):
    """Optional external research enrichment.

    All sources default to OFF. When any source is enabled, related work and/or
    web discussions are fetched and injected into Pass 1 of the deep analysis
    so the LLM can position the paper against existing literature. When all
    sources are OFF, the pipeline runs in pure-LLM mode.
    """

    arxiv_search_enabled: bool = False
    semantic_scholar_enabled: bool = False
    web_search_enabled: bool = False
    semantic_scholar_api_key: str | None = None
    web_search_provider: Literal["tavily", "serpapi"] = "tavily"
    tavily_api_key: str | None = None
    serpapi_key: str | None = None
    # Maximum candidates to fetch per source before relevance filtering.
    max_results_per_source: int = 20
    # When True, run a lightweight relevance filter pass before injecting findings.
    relevance_filter: bool = True


class GenerationOptions(BaseModel):
    canvas_format: str = "ppt169"
    style: str = "academic"
    num_pages: int | None = None
    language: str = "zh"
    detail_level: str = "normal"
    icon_library: str = "chunk"  # chunk / tabler-filled / tabler-outline
    timeout_seconds: int | None = Field(default=None, ge=1)
    max_critic_attempts: int = Field(default=3, ge=1, le=10)
    style_overrides: StyleOverrides | None = None
    enable_deep_research: bool = False
    enable_visual_critic: bool = False
    visual_qa_max_attempts: int = Field(default=1, ge=1, le=10)
    enable_icon: bool = False
    enable_icon_rag: bool = False
    gemini_api_key: str | None = None
    template_id: str | None = None  # Template ID from assets/templates/layouts/
    research_config: ResearchConfig | None = None


class GenerateRequest(BaseModel):
    session_id: str
    instruction: str = ""
    model_settings: ModelConfig = Field(alias="model_config")
    options: GenerationOptions = Field(default_factory=GenerationOptions)


class GenerateResponse(BaseModel):
    job_id: str
    status: str = "started"


class JobStatus(BaseModel):
    status: str  # parsing, research, strategy, generation, postprocess, export, complete, error, cancelled
    progress: float = 0.0
    message: str = ""
    slides_completed: int = 0
    total_slides: int = 0
    output_path: str | None = None
    error: str | None = None


class CancelJobResponse(BaseModel):
    job_id: str
    status: str


class ReexportResponse(BaseModel):
    job_id: str
    status: str
    output_path: str


class PreviewSlide(BaseModel):
    index: int
    name: str
    source: str  # "output" or "final"
    content: str


class PreviewResponse(BaseModel):
    job_id: str
    project_dir: str | None = None
    slides: list[PreviewSlide] = Field(default_factory=list)
    output_path: str | None = None
    status: str


class RefineRequest(BaseModel):
    """Request to iterate on an existing generation using user feedback."""

    job_id: str           # ID of the completed generation job to refine
    feedback: str         # User's natural-language feedback / instructions
    model_settings: ModelConfig = Field(alias="model_config")
    options: GenerationOptions = Field(default_factory=GenerationOptions)
    target_pages: list[int] = Field(default_factory=list)
    allow_structure_changes: bool = False


class RefineResponse(BaseModel):
    job_id: str   # new job ID for the refine run
    status: str = "started"


class ProviderModel(BaseModel):
    id: str
    display_name: str
    supports_vision: bool = False


class ProviderListItem(BaseModel):
    name: str
    display_name: str
    default_base_url: str | None = None
    models: list[ProviderModel]


class ProvidersResponse(BaseModel):
    providers: list[ProviderListItem]


# ── Image Search ─────────────────────────────────────────────────────────────


class ImageSearchRequest(BaseModel):
    """Request to search for images online."""

    query: str = Field(min_length=1, max_length=200)
    slide_index: int | None = Field(default=None, ge=1)
    max_results: int = Field(default=8, ge=1, le=20)
    tavily_api_key: str | None = None  # Client-provided key (takes priority)
    serpapi_key: str | None = None


class ImageSearchResultItem(BaseModel):
    """A single image search result."""

    url: str
    thumbnail: str = ""
    description: str = ""
    source: str = ""


class ImageSearchResponse(BaseModel):
    """Response containing image search results."""

    results: list[ImageSearchResultItem] = Field(default_factory=list)


class ImageApplyRequest(BaseModel):
    """Request to apply a selected image to a slide."""

    image_url: str = Field(min_length=1)
    slide_index: int = Field(ge=1)
    target_element: str | None = None
    image_description: str = ""  # Description for LLM context
    # LLM config for AI-powered image insertion (when no <image> in SVG)
    api_key: str | None = None
    provider: str = "openai"
    model: str = "gpt-4o"
    base_url: str | None = None

    @field_validator("image_url")
    @classmethod
    def validate_image_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("image_url must start with http:// or https://")
        return v


class ImageApplyResponse(BaseModel):
    """Response after applying an image."""

    status: str
    local_path: str | None = None
    svg_updated: bool = False
    action: str = ""  # "replaced" | "inserted"


class ImageUndoResponse(BaseModel):
    """Response after undoing an image change."""

    status: str
    svg_restored: bool = False
