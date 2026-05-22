"""Shared dataclasses, TypedDicts, Pydantic models, and exceptions.

This module is the *single source of truth* for the data shapes used by
every leaf module in :mod:`backend.generator.template_import` and by
the orchestrating :class:`pipeline.Pipeline`. It contains **no logic**;
only type / data declarations.

Conventions (see ``design.md`` §"Components and Interfaces" and
§"Data Models"):

* ``@dataclass(frozen=True)`` — immutable value types passed across module
  boundaries (``PipelineContext``, ``BoundingBox``, ``RenderResult``,
  ``SlideMetrics``, ``PlaceholderInstance``).
* ``@dataclass`` — mutable aggregates filled in by leaf modules
  (``AssetCandidate``, ``AssetOccurrence``, ``ChromeTextItem``,
  ``ElementAction``, ``PageClassification``, ``TemplateizeOutput``,
  ``ImportResult``, ``InstallResult``, ``LayoutPack``).
* ``TypedDict`` (``total=False``) — JSON-shaped objects persisted to
  disk. They round-trip through ``json.dumps``/``json.loads`` and are
  expected to preserve unknown forward-compatible fields untouched
  (Requirement 12.1).
* ``BaseModel`` — Pydantic v2 models for the LLM-validated structures
  (``LLMTemplateImportPlan`` and its sub-models), which require strict
  schema enforcement and structured retry on parse failure.

All timestamps are Unix seconds (``float``); paths are :class:`pathlib.Path`;
coordinates default to viewBox pixels unless otherwise noted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────────────────
# Literal aliases (kept stable so they can be re-used by leaf modules).
# ─────────────────────────────────────────────────────────────────────────────

PageType = Literal["cover", "toc", "chapter", "content", "ending"]
"""The five page archetypes a template is decomposed into."""

AssetRole = Literal["logo", "background", "decoration", "content_image", "ignore"]
"""Recommended/assigned role for an extracted image asset."""

AssetLayer = Literal["master", "layout", "slide"]
"""Which PPTX inheritance layer an :class:`AssetOccurrence` came from."""

ElementActionLabel = Literal["keep", "remove", "replace_with_placeholder"]
"""Action applied to a single SVG element during templateization."""

RoleSource = Literal["rule", "llm", "user"]
"""Who decided a role / element action: heuristic, LLM, or human."""

ExportMode = Literal["powerpoint-com", "libreoffice"]
"""Which renderer produced the SVG output."""

StepStatus = Literal["pending", "active", "complete", "error", "skipped"]
"""Individual pipeline-step status (granular)."""

TaskStatus = Literal["processing", "review_required", "complete", "error"]
"""Coarse-grained import-task status driven by ``state.json.status``."""

ErrorKind = Literal["render", "extraction", "llm", "persistence", "unknown"]
"""Top-level error taxonomy (see ``design.md`` §"Error Classification")."""

PlaceholderName = Literal[
    "TITLE",
    "PAGE_TITLE",
    "SUBTITLE",
    "AUTHOR",
    "DATE",
    "GROUP",
    "CHAPTER_NUM",
    "CHAPTER_NUMBER",
    "CHAPTER_TITLE",
    "TOC_LIST",
    "TOC_ITEM_1",
    "TOC_ITEM_2",
    "TOC_ITEM_3",
    "TOC_ITEM_4",
    "TOC_ITEM_5",
    "CONTENT_AREA",
    "ENDING_TITLE",
    "ENDING_MESSAGE",
    "LOGO_HEADER",
    "LOGO_FOOTER",
]
"""Whitelisted placeholder names (mirrors :data:`placeholder_schema.PLACEHOLDER_WHITELIST`)."""

ChatRole = Literal["user", "assistant", "system"]
"""Who authored a :class:`ChatMessage`."""

ManifestWarningCode = Literal[
    "asset-too-large",
    "asset-decode-failed",
    "templateize.geometry_violation",
    "renderer.fallback",
    "llm.placeholder_rejected",
    "render.empty_slide",
]
"""Codes used in :class:`ManifestWarning.code`."""


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class TemplateImportError(Exception):
    """Base class for all template-import failures.

    The ``error_kind`` attribute is set by subclasses and surfaces in
    ``state.json.error_kind`` so the frontend can render the right
    recovery affordance (Requirement 10.2).
    """

    error_kind: ErrorKind = "unknown"

    def __init__(
        self,
        reason: str,
        *,
        step_id: str | None = None,
        error_kind: ErrorKind | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.step_id = step_id
        if error_kind is not None:
            self.error_kind = error_kind
        self.context: dict[str, Any] = dict(context) if context else {}


class RenderError(TemplateImportError):
    """Raised when neither PowerPoint COM nor LibreOffice can render the PPTX."""

    error_kind: ErrorKind = "render"


class ExtractionError(TemplateImportError):
    """Raised on PPTX zip / SVG parse / hashing failures (hard, not per-asset)."""

    error_kind: ErrorKind = "extraction"


class LLMPlanError(TemplateImportError):
    """Raised when the LLM call cannot produce a schema-valid plan after retry."""

    error_kind: ErrorKind = "llm"


class PersistenceError(TemplateImportError):
    """Raised on state/review/manifest write failures or transactional rollback."""

    error_kind: ErrorKind = "persistence"


# ─────────────────────────────────────────────────────────────────────────────
# Geometry & rendering value types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BoundingBox:
    """Axis-aligned bounding box in viewBox pixels."""

    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class SlideMetrics:
    """Stable per-slide rendering metrics written to ``slide_metrics.json``.

    Downstream modules read this instead of reverse-engineering the SVG
    ``viewBox`` (Requirement 8.1).
    """

    slide_index: int                         # 1-based
    canvas_width: int
    canvas_height: int
    font_substitutions: dict[str, str]       # logical → resolved font
    rendered_at: float                       # Unix seconds


@dataclass(frozen=True)
class RenderResult:
    """Aggregate result of :func:`renderer.render`."""

    svg_files: list[Path]                    # work/svg/slide-001.svg ...
    slide_metrics: list[SlideMetrics]
    export_mode: ExportMode
    canvas: tuple[int, int]                  # (W, H) in pixels


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline context
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PipelineContext:
    """Per-import inputs handed to :class:`pipeline.Pipeline`."""

    import_id: str
    work_dir: Path
    pptx_path: Path
    label: str
    model_config: dict[str, Any]


# ─────────────────────────────────────────────────────────────────────────────
# Asset extraction
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AssetOccurrence:
    """One concrete reference to an image, with normalized layer info."""

    slide_index: int
    x: float
    y: float
    width: float
    height: float
    layer: AssetLayer


@dataclass
class AssetCandidate:
    """An aggregated image asset across master/layout/slide layers.

    Persisted under ``review.json.assets_full`` and (post-confirm) as
    ``manifest.common_assets``.
    """

    asset_id: str                            # sha1(content)[:16]
    file_name: str
    pages: list[int]
    occurrences: list[AssetOccurrence]
    sha1: str
    phash: str | None = None
    bytes: int = 0
    mime: str = "application/octet-stream"
    width_px: int | None = None
    height_px: int | None = None
    position_stable: bool = False
    recommended_role: AssetRole = "decoration"
    role_source: RoleSource = "rule"
    role_confidence: float = 0.0
    preview_data_uri: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Chrome detection
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ChromeTextItem:
    """A repeated text element identified as page chrome."""

    text: str
    pages: list[int]
    page_count: int
    position_stable_chrome: bool
    bbox_norm_mean: tuple[float, float, float, float]
    bbox_norm_stddev: tuple[float, float, float, float]
    placeholder_hint: str | None
    action: ElementActionLabel


# ─────────────────────────────────────────────────────────────────────────────
# Element actions (templateizer input)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ElementAction:
    """A decision to keep / remove / replace a single SVG element.

    Used as the input to :func:`templateizer.templateize` and as the
    in-memory analogue of the persisted :class:`ElementActionRecord`.
    """

    page_type: PageType
    element_id: str
    action: ElementActionLabel
    placeholder: str | None = None
    reason: str | None = None
    source: RoleSource = "rule"


# ─────────────────────────────────────────────────────────────────────────────
# Page classifier
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PageClassification:
    """Per-slide classification result returned by :func:`page_classifier.classify`."""

    page_type: PageType | None
    confidence: float                        # 0..1
    signals: dict[str, float] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Templateizer output
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PlaceholderInstance:
    """A concrete placeholder appearance in a templated SVG."""

    name: str
    bbox: BoundingBox
    style: dict[str, str]                    # font-size / fill / font-weight / ...


@dataclass
class TemplateizeOutput:
    """Result of templateizing a single page-type SVG."""

    page_type: PageType
    svg_text: str
    placeholders: list[PlaceholderInstance]
    content_area: BoundingBox | None         # only for page_type == "content"
    z_order_preserved: bool = True
    warnings: list["ManifestWarning"] = field(default_factory=list)
    """Non-fatal warnings raised during templateization (e.g. geometry violations).

    Forwarded by :class:`pipeline.Pipeline` into ``manifest.warnings`` on confirm.
    """


# ─────────────────────────────────────────────────────────────────────────────
# User annotations (visual feedback, drawn on the big-preview overlay)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class UserAnnotation:
    """A user-drawn rectangular note on a single slide.

    Persisted as JSON inside ``review.json.annotations`` and replayed on
    next load. The bbox is normalized to ``[0,1]`` against the slide
    canvas so the same annotation re-renders correctly regardless of
    preview size. ``linked_element_id`` is optional — when set, the
    annotation also targets a specific SVG element (e.g. for telling
    the LLM "this <text> element is actually a chapter number, not body
    text"). Annotations are surfaced to the LLM as plain-text bullets
    so text-only models can use them as visual feedback.
    """

    annotation_id: str
    slide_index: int
    bbox_norm: BoundingBox
    note: str
    linked_element_id: str | None = None
    created_at: float = 0.0
    resolved: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# JSON-shaped persisted objects (TypedDict — round-trip stable)
# ─────────────────────────────────────────────────────────────────────────────

class ChatMessage(TypedDict, total=False):
    """One entry in ``review.json.conversation``."""

    role: ChatRole
    content: str
    created_at: float


class UserAnnotationRecord(TypedDict, total=False):
    """Persisted JSON form of :class:`UserAnnotation` for ``review.json.annotations``."""

    annotation_id: str
    slide_index: int
    bbox_norm: dict[str, float]   # {x, y, width, height}
    note: str
    linked_element_id: str | None
    created_at: float
    resolved: bool


class AssetCandidateOverride(TypedDict, total=False):
    """User/LLM override for an asset's role.

    The full :class:`AssetCandidate` lives in ``review.json.assets_full``;
    this trimmed shape is what is persisted under ``review.json.assets``.
    """

    asset_id: str
    name: str
    role: AssetRole
    role_source: RoleSource


class ElementActionRecord(TypedDict, total=False):
    """Persisted JSON form of :class:`ElementAction`."""

    page_type: PageType
    element_id: str
    action: ElementActionLabel
    placeholder: str
    reason: str
    source: RoleSource


class PageSelections(TypedDict, total=False):
    """``page_type → slide_index | None`` mapping."""

    cover: int | None
    toc: int | None
    chapter: int | None
    content: int | None
    ending: int | None


class PageTypeCandidates(TypedDict, total=False):
    """``page_type → list[slide_index]`` candidate sets."""

    cover: list[int]
    toc: list[int]
    chapter: list[int]
    content: list[int]
    ending: list[int]


class PlaceholderHints(TypedDict, total=False):
    """``page_type → {name: original_text}`` provided by users / the LLM."""

    cover: dict[str, str]
    toc: dict[str, str]
    chapter: dict[str, str]
    content: dict[str, str]
    ending: dict[str, str]


class StepRecord(TypedDict, total=False):
    """One entry in ``state.json.steps``."""

    id: str
    label: str
    status: StepStatus
    started_at: float
    ended_at: float
    duration_ms: int
    message: str
    error: str


class LLMTraceActionPlan(TypedDict, total=False):
    """Compact summary of what an LLM iteration changed.

    Shape mirrors :class:`LLMTemplateImportPlan` but as a JSON dict so it
    is round-trip stable as part of :class:`LLMTraceEntry`.
    """

    page_selections: PageSelections
    asset_decisions: list[dict[str, Any]]
    placeholder_decisions: list[dict[str, Any]]
    element_actions: list[ElementActionRecord]


class LLMTraceEntry(TypedDict, total=False):
    """One iteration of LLM assistance, appended to ``review.json.llm_trace``.

    Replicated to ``assets/templates/layouts/<id>/import_trace.json`` on
    confirm (Requirement 10.4).
    """

    iteration: int
    updated_at: float
    input_hash: str
    input_excerpt: str          # truncated to 4 KB
    raw_response_excerpt: str   # truncated to 4 KB
    action_plan: LLMTraceActionPlan
    changed: bool
    retried_no_change: bool
    rule_patches: list[str]


class ReviewDraft(TypedDict, total=False):
    """The mutable, user-editable JSON document at ``review.json``.

    ``schema_version=2``. Unknown / forward-compatible fields are
    preserved on round-trip (Requirement 12.1).
    """

    schema_version: int
    import_id: str
    template_id: str
    label: str
    status: TaskStatus
    export_mode: ExportMode
    slide_count: int

    page_selections: PageSelections
    page_type_candidates: PageTypeCandidates

    assets: dict[str, AssetCandidateOverride]
    assets_full: list[dict[str, Any]]        # full AssetCandidate as JSON
    preserve_texts: list[str]
    placeholder_hints: PlaceholderHints

    element_actions: list[ElementActionRecord]

    annotations: list[UserAnnotationRecord]
    """User-drawn rectangular notes on slides (visual feedback for text-only LLMs)."""

    conversation: list[ChatMessage]
    feedback_history: list[str]
    llm_trace: list[LLMTraceEntry]

    design_spec_md: str | None


class ImportTaskState(TypedDict, total=False):
    """The coarse JSON document at ``state.json``.

    Drives the frontend ``ProgressView``. ``steps`` ordering is asserted
    by Property 1.
    """

    import_id: str
    status: TaskStatus
    stage: str                               # current step id
    progress: float                          # 0..1
    message: str
    created_at: float
    updated_at: float
    review_required: bool
    template_id: str | None
    label: str
    source_file: str
    slide_count: int
    export_mode: ExportMode
    error: str | None
    error_kind: ErrorKind | None
    steps: list[StepRecord]
    theme_colors: list[str]


class ManifestCanvas(TypedDict, total=False):
    """Canvas dimensions block in :class:`TemplateManifest`."""

    width: int
    height: int


class ManifestTheme(TypedDict, total=False):
    """Theme block in :class:`TemplateManifest`."""

    colors: dict[str, str]
    fonts: dict[str, str]


class ManifestWarning(TypedDict, total=False):
    """One non-fatal warning recorded during import.

    Surfaced under ``manifest.warnings`` and rendered as a banner in the
    frontend ``AssetReviewer``.
    """

    code: ManifestWarningCode
    reason: str
    context: dict[str, Any]


class ManifestAssetEntry(TypedDict, total=False):
    """An asset referenced by the final ``LayoutPack`` manifest.

    Only assets with role ∈ {logo, background, decoration} appear here
    (per design ``Data Models`` §Manifest).
    """

    asset_id: str
    file_name: str
    role: AssetRole
    pages: list[int]
    bbox_norm: dict[str, float]              # {x, y, width, height}
    sha1: str
    bytes: int


class ManifestContentArea(TypedDict, total=False):
    """JSON form of :class:`BoundingBox` for ``manifest.content_area``."""

    x: float
    y: float
    width: float
    height: float


class TemplateManifest(TypedDict, total=False):
    """``manifest.json`` written under ``assets/templates/layouts/<id>/``.

    ``schema_version=2``. v1 manifests are upgraded *in memory* on read
    but not rewritten (see ``design.md`` §Migration Strategy).
    """

    schema_version: int
    template_id: str
    label: str
    source_file: str
    slide_count: int
    canvas: ManifestCanvas
    theme: ManifestTheme
    page_type_candidates: PageTypeCandidates
    page_selections: PageSelections
    common_assets: list[ManifestAssetEntry]
    content_area: ManifestContentArea | None
    warnings: list[ManifestWarning]
    imported_at: float
    export_mode: ExportMode
    importer_version: str


# ─────────────────────────────────────────────────────────────────────────────
# Layout pack (assets/templates/layouts/<id>/) — in-memory representation
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LayoutPack:
    """In-memory bundle of the artifacts written for one finished template.

    Fields map 1:1 onto the on-disk layout directory:

    .. code-block:: text

        <layout_id>/
        ├── 01_cover.svg          ← svgs["cover"]
        ├── 02_toc.svg            ← svgs["toc"]
        ├── 02_chapter.svg        ← svgs["chapter"]
        ├── 03_content.svg        ← svgs["content"]
        ├── 04_ending.svg         ← svgs["ending"]
        ├── design_spec.md        ← design_spec
        ├── manifest.json         ← manifest
        ├── import_trace.json     ← import_trace (optional)
        └── assets/<sha1>.<ext>   ← assets[file_name] = bytes
    """

    template_id: str
    label: str
    manifest: TemplateManifest
    svgs: dict[PageType, str]                # page_type → SVG text
    design_spec: str
    assets: dict[str, bytes] = field(default_factory=dict)
    import_trace: list[LLMTraceEntry] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline result types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ImportResult:
    """Return type for :meth:`pipeline.Pipeline.run` / ``confirm`` / ``retry_step``.

    Mirrors the shape of the existing v1 ``ImportResult`` so the FastAPI
    endpoint contract stays stable across the v1 → v2 cutover.
    """

    template_id: str = ""
    label: str = ""
    status: TaskStatus = "processing"
    stage: str = "uploaded"
    progress: float = 0.0
    message: str = ""
    steps: list[StepRecord] = field(default_factory=list)
    review_required: bool = False
    export_mode: str = ""
    slide_count: int = 0
    cover_svg: str = ""
    content_svg: str = ""
    theme_colors: list[str] = field(default_factory=list)
    error: str = ""
    error_kind: ErrorKind | None = None


@dataclass
class InstallResult:
    """Outcome of :func:`persistence.transactional_install_layout`."""

    template_id: str
    layout_dir: Path
    already_complete: bool = False
    rolled_back: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# LLM-validated plan (Pydantic v2 — strict schema for structured retry)
# ─────────────────────────────────────────────────────────────────────────────

class LLMPageSelections(BaseModel):
    """LLM's choice of representative slide per page type."""

    cover: int | None = None
    toc: int | None = None
    chapter: int | None = None
    content: int | None = None
    ending: int | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class LLMAssetDecision(BaseModel):
    """LLM's role assignment for one asset."""

    asset_id: str
    role: AssetRole
    name: str | None = None
    reason: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class LLMPlaceholderDecision(BaseModel):
    """LLM-supplied ``placeholder_hints`` entry for one (page_type, name)."""

    page_type: PageType
    name: PlaceholderName
    text: str


class LLMElementAction(BaseModel):
    """LLM's per-element keep/remove/replace decision.

    ``placeholder`` must be one of :data:`PlaceholderName`; values outside
    that whitelist are rejected at ``validate_actions`` time
    (Requirement 7.4).
    """

    page_type: PageType
    element_id: str
    action: ElementActionLabel
    placeholder: PlaceholderName | None = None
    reason: str | None = None


class LLMTemplateImportPlan(BaseModel):
    """Top-level Pydantic model the LLM must return.

    Failure to validate triggers the structured-repair retry path
    (Requirement 7.1).
    """

    label: str | None = None
    page_selections: LLMPageSelections = Field(default_factory=LLMPageSelections)
    asset_decisions: list[LLMAssetDecision] = Field(default_factory=list)
    placeholder_decisions: list[LLMPlaceholderDecision] = Field(default_factory=list)
    element_actions: list[LLMElementAction] = Field(default_factory=list)
    preserve_texts: list[str] = Field(default_factory=list)
    design_spec_md: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: list[str] = Field(default_factory=list)


__all__ = [
    # Literals
    "PageType",
    "AssetRole",
    "AssetLayer",
    "ElementActionLabel",
    "RoleSource",
    "ExportMode",
    "StepStatus",
    "TaskStatus",
    "ErrorKind",
    "PlaceholderName",
    "ChatRole",
    "ManifestWarningCode",
    # Exceptions
    "TemplateImportError",
    "RenderError",
    "ExtractionError",
    "LLMPlanError",
    "PersistenceError",
    # Geometry / rendering
    "BoundingBox",
    "SlideMetrics",
    "RenderResult",
    # Pipeline
    "PipelineContext",
    # Asset extraction
    "AssetOccurrence",
    "AssetCandidate",
    # Chrome detection
    "ChromeTextItem",
    # User annotations
    "UserAnnotation",
    # Element actions
    "ElementAction",
    # Page classifier
    "PageClassification",
    # Templateizer
    "PlaceholderInstance",
    "TemplateizeOutput",
    # Persisted JSON shapes
    "ChatMessage",
    "UserAnnotationRecord",
    "AssetCandidateOverride",
    "ElementActionRecord",
    "PageSelections",
    "PageTypeCandidates",
    "PlaceholderHints",
    "StepRecord",
    "LLMTraceActionPlan",
    "LLMTraceEntry",
    "ReviewDraft",
    "ImportTaskState",
    "ManifestCanvas",
    "ManifestTheme",
    "ManifestWarning",
    "ManifestAssetEntry",
    "ManifestContentArea",
    "TemplateManifest",
    # Layout pack
    "LayoutPack",
    # Result types
    "ImportResult",
    "InstallResult",
    # LLM plan (Pydantic)
    "LLMPageSelections",
    "LLMAssetDecision",
    "LLMPlaceholderDecision",
    "LLMElementAction",
    "LLMTemplateImportPlan",
]
