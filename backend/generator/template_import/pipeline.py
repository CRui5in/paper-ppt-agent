"""Sequential state machine orchestrating a template import.

Implements the ``uploaded → analyzing → rendering → detecting_assets →
llm_review → review`` flow described in ``design.md``. Each step
delegates to a leaf module and is wrapped by ``observability.step_timing``;
failures are mapped to the ``error_kind`` taxonomy
(``render`` / ``extraction`` / ``llm`` / ``persistence`` / ``unknown``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any

from . import asset_extractor, chrome_detector, page_classifier, persistence, templateizer
from . import renderer as renderer_pkg
from .llm_client import LLMClient
from .observability import step_timing
from .types import (
    AssetCandidate,
    BoundingBox,
    ChromeTextItem,
    ElementAction,
    ElementActionRecord,
    ExtractionError,
    ImportResult,
    ImportTaskState,
    LayoutPack,
    LLMPlanError,
    LLMTraceEntry,
    ManifestAssetEntry,
    ManifestCanvas,
    ManifestContentArea,
    ManifestTheme,
    ManifestWarning,
    PageType,
    PersistenceError,
    PipelineContext,
    RenderError,
    RenderResult,
    ReviewDraft,
    SlideMetrics,
    StepRecord,
    TemplateImportError,
    TemplateManifest,
    TemplateizeOutput,
)


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Step table (id, label, default error_kind, skippable)
# ─────────────────────────────────────────────────────────────────────────────

_STEPS: tuple[tuple[str, str, str, bool], ...] = (
    ("uploaded",         "Upload received",     "extraction", False),
    ("analyzing",        "Analyzing PPTX",      "extraction", False),
    ("rendering",        "Rendering slides",    "render",     False),
    ("detecting_assets", "Detecting assets",    "extraction", False),
    ("llm_review",       "LLM review",          "llm",        True),
    ("review",           "Awaiting review",     "unknown",    False),
)

_STEP_IDS: tuple[str, ...] = tuple(s[0] for s in _STEPS)
_PAGE_TYPES: tuple[str, ...] = ("cover", "toc", "chapter", "content", "ending")
_IMPORTER_VERSION = "template_import.v2"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — state record management
# ─────────────────────────────────────────────────────────────────────────────


def _initial_state(ctx: PipelineContext) -> ImportTaskState:
    """Build the initial ``ImportTaskState`` for a fresh import."""
    now = time.time()
    steps: list[StepRecord] = [
        {"id": sid, "label": label, "status": "pending"}
        for sid, label, _kind, _skip in _STEPS
    ]
    state: ImportTaskState = {
        "import_id": ctx.import_id,
        "status": "processing",
        "stage": _STEPS[0][0],
        "progress": 0.0,
        "message": "",
        "created_at": now,
        "updated_at": now,
        "review_required": False,
        "template_id": None,
        "label": ctx.label,
        "source_file": str(ctx.pptx_path),
        "slide_count": 0,
        "export_mode": "",
        "error": None,
        "error_kind": None,
        "steps": steps,
        "theme_colors": [],
    }
    return state


def _ensure_state(ctx: PipelineContext) -> ImportTaskState:
    state = persistence.read_state(ctx.import_id)
    if state is None:
        state = _initial_state(ctx)
        persistence.write_state(ctx.import_id, state)
        return state
    # Make sure all six steps exist (safety net for partially migrated states).
    by_id = {s.get("id"): s for s in state.get("steps") or [] if isinstance(s, dict)}
    if set(_STEP_IDS) - set(by_id.keys()):
        steps: list[StepRecord] = []
        for sid, label, _kind, _skip in _STEPS:
            existing = by_id.get(sid)
            if existing:
                steps.append(existing)
            else:
                steps.append({"id": sid, "label": label, "status": "pending"})
        state["steps"] = steps
        persistence.write_state(ctx.import_id, state)
    return state


def _step_record(state: ImportTaskState, step_id: str) -> StepRecord:
    for record in state.get("steps") or []:
        if isinstance(record, dict) and record.get("id") == step_id:
            return record  # type: ignore[return-value]
    # Defensive fallback — append a fresh record.
    record = {"id": step_id, "label": step_id, "status": "pending"}
    steps = list(state.get("steps") or [])
    steps.append(record)
    state["steps"] = steps
    return record  # type: ignore[return-value]


def _set_step_status(
    state: ImportTaskState,
    step_id: str,
    status: str,
    *,
    started_at: float | None = None,
    ended_at: float | None = None,
    message: str | None = None,
    error: str | None = None,
) -> None:
    record = _step_record(state, step_id)
    record["status"] = status  # type: ignore[typeddict-item]
    if started_at is not None:
        record["started_at"] = started_at
    if ended_at is not None:
        record["ended_at"] = ended_at
        if "started_at" in record:
            try:
                duration = max(0.0, float(ended_at) - float(record["started_at"]))
                record["duration_ms"] = int(duration * 1000)
            except (TypeError, ValueError):
                pass
    if message is not None:
        record["message"] = message
    if error is not None:
        record["error"] = error


def _progress_for(step_id: str) -> float:
    if step_id not in _STEP_IDS:
        return 0.0
    idx = _STEP_IDS.index(step_id)
    # Map 0..len-1 → ~0.05..0.85; review step caps at 0.9 to leave room for confirm.
    span = max(1, len(_STEP_IDS))
    return min(0.9, 0.05 + (idx + 1) / span * 0.85)


def _result_from_state(state: ImportTaskState) -> ImportResult:
    return ImportResult(
        template_id=str(state.get("template_id") or ""),
        label=str(state.get("label") or ""),
        status=state.get("status") or "processing",  # type: ignore[arg-type]
        stage=str(state.get("stage") or _STEPS[0][0]),
        progress=float(state.get("progress") or 0.0),
        message=str(state.get("message") or ""),
        steps=list(state.get("steps") or []),
        review_required=bool(state.get("review_required")),
        export_mode=str(state.get("export_mode") or ""),
        slide_count=int(state.get("slide_count") or 0),
        theme_colors=list(state.get("theme_colors") or []),
        error=str(state.get("error") or ""),
        error_kind=state.get("error_kind"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — page-type / asset assembly
# ─────────────────────────────────────────────────────────────────────────────


def _load_or_init_review(ctx: PipelineContext, template_id: str) -> ReviewDraft:
    review = persistence.read_review(ctx.import_id)
    if review is not None:
        return review
    draft: ReviewDraft = {  # type: ignore[typeddict-unknown-key]
        "schema_version": 2,
        "import_id": ctx.import_id,
        "template_id": template_id,
        "label": ctx.label,
        "status": "processing",
        "slide_count": 0,
        "page_selections": {},
        "page_type_candidates": {},
        "assets": {},
        "assets_full": [],
        "preserve_texts": [],
        "placeholder_hints": {},
        "element_actions": [],
        "conversation": [],
        "feedback_history": [],
        "llm_trace": [],
    }
    return draft


def _candidate_to_full(candidate: AssetCandidate) -> dict[str, Any]:
    return {
        "asset_id": candidate.asset_id,
        "file_name": candidate.file_name,
        "pages": list(candidate.pages),
        "occurrences": [
            {
                "slide_index": occ.slide_index,
                "x": occ.x,
                "y": occ.y,
                "width": occ.width,
                "height": occ.height,
                "layer": occ.layer,
            }
            for occ in candidate.occurrences
        ],
        "sha1": candidate.sha1,
        "phash": candidate.phash,
        "bytes": candidate.bytes,
        "mime": candidate.mime,
        "width_px": candidate.width_px,
        "height_px": candidate.height_px,
        "position_stable": candidate.position_stable,
        "recommended_role": candidate.recommended_role,
        "role_source": candidate.role_source,
        "role_confidence": candidate.role_confidence,
    }


def _assets_role_overrides(candidates: list[AssetCandidate]) -> dict[str, Any]:
    """Build the trimmed ``review.assets`` dict of role overrides."""
    out: dict[str, Any] = {}
    for c in candidates:
        out[c.asset_id] = {
            "asset_id": c.asset_id,
            "name": c.file_name,
            "role": c.recommended_role,
            "role_source": c.role_source,
        }
    return out


def _slide_signal_view(
    metric: SlideMetrics,
    total: int,
    candidates: list[AssetCandidate],
) -> dict[str, Any]:
    """Build a minimal slide-record dict consumable by ``page_classifier.classify``."""
    images: list[dict[str, Any]] = []
    for c in candidates:
        for occ in c.occurrences:
            if occ.slide_index == metric.slide_index:
                images.append({"width": float(occ.width), "height": float(occ.height)})
    return {
        "slide_index": metric.slide_index,
        "total_slides": total,
        "text_runs": [],
        "text_density": 0.0,
        "images": images,
        "font_sizes": [],
    }


def _build_initial_manifest(
    ctx: PipelineContext,
    render_result: RenderResult | None,
) -> dict[str, Any]:
    """Best-effort initial manifest from ``manifest.build_manifest`` if available."""
    try:
        from .manifest import build_manifest

        out_dir = ctx.work_dir / "analysis"
        out_dir.mkdir(parents=True, exist_ok=True)
        return build_manifest(ctx.pptx_path, out_dir)
    except Exception:  # noqa: BLE001 — best-effort initial scan
        canvas = (1280, 720)
        if render_result is not None:
            canvas = render_result.canvas
        return {
            "source": {"name": Path(ctx.pptx_path).name},
            "slideSize": {"width_px": canvas[0], "height_px": canvas[1]},
            "theme": {"colors": {}, "fonts": {}},
            "assets": {"commonAssets": [], "allAssets": []},
            "slides": [],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — inline_svg_asset_refs (preview = final-output parity)
# ─────────────────────────────────────────────────────────────────────────────


_HREF_PATTERN = re.compile(
    r'(?P<attr>(?:xlink:)?href)=(?P<quote>["\'])(?P<href>[^"\']*)(?P=quote)'
)
_DATA_URI_MAX_BYTES = 900_000


def _data_uri_for_bytes(name: str, data: bytes) -> str:
    if not data or len(data) > _DATA_URI_MAX_BYTES:
        return ""
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def inline_svg_asset_refs(svg_text: str, asset_map: dict[str, bytes]) -> str:
    """Inline ``<image href>`` / ``xlink:href`` references to ``data:`` URIs.

    * ``data:``, ``http://``, ``https://`` and ``#`` references pass
      through unchanged (Requirement 12.2 — never produce ``http(s)://``).
    * Local references whose basename matches a key in ``asset_map`` are
      replaced with a ``data:<mime>;base64,...`` URI built from the
      corresponding bytes. References whose basename is *not* in the map
      are left untouched so they can be resolved at render time against
      the on-disk asset directory.
    * Everything outside the matched ``href="..."`` / ``xlink:href="..."``
      attribute values is left byte-identical, so ``{{NAME}}`` placeholder
      positions (line + column) are preserved across repeated invocations
      (Requirement 3.3, 12.2; Property 22).
    * Idempotent: ``inline(inline(x)) == inline(x)`` because already-inlined
      ``data:`` references short-circuit the substitution.
    """
    if not svg_text:
        return svg_text

    def _repl(match: re.Match[str]) -> str:
        attr = match.group("attr")
        quote = match.group("quote")
        href = match.group("href")
        if not href:
            return match.group(0)
        if href.startswith(("data:", "http://", "https://", "#")):
            return match.group(0)
        # Skip placeholder tokens — let the runtime templater fill them in.
        if "{{" in href and "}}" in href:
            return match.group(0)
        clean = href.split("#", 1)[0].split("?", 1)[0]
        if not clean:
            return match.group(0)
        basename = PurePosixPath(clean).name or clean
        payload = asset_map.get(basename)
        if payload is None:
            payload = asset_map.get(clean)
        if payload is None:
            return match.group(0)
        data_uri = _data_uri_for_bytes(basename, payload)
        if not data_uri:
            return match.group(0)
        return f"{attr}={quote}{data_uri}{quote}"

    return _HREF_PATTERN.sub(_repl, svg_text)


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline
# ─────────────────────────────────────────────────────────────────────────────


class Pipeline:
    """Runs the template-import state machine for a single ``import_id``."""

    def __init__(self, ctx: PipelineContext | None = None) -> None:
        self._ctx = ctx

    # ── run ────────────────────────────────────────────────────────────

    async def run(self, ctx: PipelineContext) -> ImportResult:
        """Execute all pipeline steps in order until ``review_required``."""
        self._ctx = ctx
        state = _ensure_state(ctx)
        # Honor an already-complete state — nothing to redo.
        if state.get("status") == "complete":
            return _result_from_state(state)

        # Reset error markers for this run; per-step reruns clear earlier
        # error/skipped statuses inside retry_step before calling run().
        state["error"] = None
        state["error_kind"] = None
        if state.get("status") == "error":
            state["status"] = "processing"
        persistence.write_state(ctx.import_id, state)

        # Per-run intermediates carried between steps.
        render_result: RenderResult | None = None
        manifest_data: dict[str, Any] = {}
        candidates: list[AssetCandidate] = []
        warnings_aggregate: list[ManifestWarning] = []
        chrome_items: list[ChromeTextItem] = []
        review = _load_or_init_review(ctx, state.get("template_id") or ctx.import_id)

        for step_id, _label, error_kind, skippable in _STEPS:
            step_record = _step_record(state, step_id)
            if step_record.get("status") == "complete":
                continue
            if step_record.get("status") == "skipped" and skippable:
                continue

            state["stage"] = step_id
            _set_step_status(state, step_id, "active", started_at=time.time())
            state["progress"] = _progress_for(step_id) - 0.05
            persistence.write_state(ctx.import_id, state)

            try:
                with step_timing(ctx.import_id, step_id):
                    if step_id == "uploaded":
                        # Just record source-file metadata.
                        try:
                            stat = ctx.pptx_path.stat()
                            state["source_file"] = str(ctx.pptx_path)
                            _set_step_status(
                                state,
                                step_id,
                                "active",
                                message=f"{ctx.pptx_path.name} ({stat.st_size} bytes)",
                            )
                        except OSError:
                            pass

                    elif step_id == "analyzing":
                        manifest_data = _build_initial_manifest(ctx, render_result)
                        slide_count = len(manifest_data.get("slides") or [])
                        state["slide_count"] = slide_count
                        review["slide_count"] = slide_count

                    elif step_id == "rendering":
                        svg_dir = ctx.work_dir / "svg"
                        try:
                            render_result = renderer_pkg.render(ctx.pptx_path, svg_dir)
                        except RenderError:
                            raise
                        except Exception as exc:  # pragma: no cover - defensive
                            raise RenderError(
                                str(exc) or exc.__class__.__name__,
                                step_id=step_id,
                                error_kind="render",
                            ) from exc
                        state["export_mode"] = render_result.export_mode
                        state["slide_count"] = len(render_result.svg_files)
                        review["export_mode"] = render_result.export_mode  # type: ignore[typeddict-item]
                        review["slide_count"] = len(render_result.svg_files)

                    elif step_id == "detecting_assets":
                        if render_result is None:
                            # Reload from on-disk metrics + svg dir if possible.
                            render_result = _reload_render_result(ctx)
                        if render_result is None:
                            raise ExtractionError(
                                "renderer output unavailable",
                                step_id=step_id,
                                error_kind="extraction",
                            )
                        try:
                            candidates, warnings = asset_extractor.extract_with_warnings(
                                ctx.pptx_path,
                                render_result.svg_files,
                                render_result.slide_metrics,
                            )
                        except ExtractionError:
                            raise
                        except Exception as exc:  # pragma: no cover - defensive
                            raise ExtractionError(
                                str(exc) or exc.__class__.__name__,
                                step_id=step_id,
                                error_kind="extraction",
                            ) from exc
                        warnings_aggregate.extend(warnings)
                        review["assets_full"] = [_candidate_to_full(c) for c in candidates]
                        review["assets"] = _assets_role_overrides(candidates)  # type: ignore[typeddict-item]

                        try:
                            chrome_items = chrome_detector.detect(
                                render_result.svg_files,
                                render_result.slide_metrics,
                                preserve_texts=list(review.get("preserve_texts") or []),
                                placeholder_hints={
                                    k: dict(v)
                                    for k, v in (review.get("placeholder_hints") or {}).items()
                                    if isinstance(v, dict)
                                },
                            )
                        except Exception:  # noqa: BLE001 - chrome detection is best-effort
                            chrome_items = []

                    elif step_id == "llm_review":
                        if state.get("collaboration_mode") == "agent":
                            _set_step_status(
                                state,
                                step_id,
                                "skipped",
                                ended_at=time.time(),
                                message="Agent mode owns template planning.",
                            )
                            persistence.write_state(ctx.import_id, state)
                            continue
                        try:
                            client = LLMClient(ctx)
                            await client.assist(
                                ctx,
                                review,
                                manifest_data,
                                feedback=None,
                                required=False,
                            )
                        except LLMPlanError as exc:
                            if not skippable:
                                raise
                            _set_step_status(
                                state,
                                step_id,
                                "skipped",
                                ended_at=time.time(),
                                message=f"LLM unavailable: {exc.reason}",
                            )
                            persistence.write_state(ctx.import_id, state)
                            continue

                    elif step_id == "review":
                        # Build ReviewDraft + page_selections + page_type_candidates.
                        if render_result is None:
                            render_result = _reload_render_result(ctx)
                        slide_count = (
                            len(render_result.svg_files) if render_result else int(state.get("slide_count") or 0)
                        )
                        slide_metrics: list[SlideMetrics] = (
                            render_result.slide_metrics if render_result else []
                        )

                        # Run page classification when we have metrics.
                        slides_view = [
                            _slide_signal_view(m, slide_count, candidates) for m in slide_metrics
                        ]
                        try:
                            classifications = page_classifier.classify(slides_view, slide_metrics)
                            selections, _patches = page_classifier.select_representatives(
                                classifications
                            )
                        except Exception:  # noqa: BLE001 - classifier failures shouldn't block review
                            classifications = {}
                            selections = {pt: None for pt in _PAGE_TYPES}

                        review.setdefault("page_selections", {})
                        for pt in _PAGE_TYPES:
                            current = review["page_selections"].get(pt) if review.get("page_selections") else None  # type: ignore[union-attr]
                            if current is None and selections.get(pt) is not None:
                                review["page_selections"][pt] = selections[pt]  # type: ignore[index]

                        # Page-type candidate sets — argmax bucketing.
                        candidates_by_type: dict[str, list[int]] = {pt: [] for pt in _PAGE_TYPES}
                        for idx, cls in classifications.items():
                            if cls.page_type in candidates_by_type:
                                candidates_by_type[cls.page_type].append(int(idx))
                        for pt in _PAGE_TYPES:
                            candidates_by_type[pt].sort()
                        review["page_type_candidates"] = candidates_by_type  # type: ignore[typeddict-item]

                        review["status"] = "review_required"  # type: ignore[typeddict-item]
                        persistence.write_review(ctx.import_id, review)

                        state["review_required"] = True
                        state["status"] = "review_required"
                        state["template_id"] = review.get("template_id") or state.get("template_id")
                        state["progress"] = 0.9
                        state["message"] = "Awaiting review"

            except TemplateImportError as exc:
                ended = time.time()
                kind = exc.error_kind or error_kind
                _set_step_status(
                    state,
                    step_id,
                    "error",
                    ended_at=ended,
                    error=exc.reason,
                )
                state["status"] = "error"
                state["error"] = exc.reason
                state["error_kind"] = kind  # type: ignore[typeddict-item]
                state["updated_at"] = ended
                persistence.write_state(ctx.import_id, state)
                return _result_from_state(state)
            except Exception as exc:  # noqa: BLE001 - convert to TemplateImportError
                ended = time.time()
                _set_step_status(
                    state,
                    step_id,
                    "error",
                    ended_at=ended,
                    error=str(exc) or exc.__class__.__name__,
                )
                state["status"] = "error"
                state["error"] = str(exc) or exc.__class__.__name__
                state["error_kind"] = error_kind  # type: ignore[typeddict-item]
                state["updated_at"] = ended
                persistence.write_state(ctx.import_id, state)
                return _result_from_state(state)

            # Step succeeded — but only mark complete if it wasn't already
            # set to "skipped" inside the with-block.
            if _step_record(state, step_id).get("status") == "active":
                _set_step_status(state, step_id, "complete", ended_at=time.time())
            state["progress"] = _progress_for(step_id)
            state["updated_at"] = time.time()
            persistence.write_state(ctx.import_id, state)

        persistence.write_review(ctx.import_id, review)
        return _result_from_state(state)

    # ── retry_step ─────────────────────────────────────────────────────

    async def retry_step(self, import_id: str, step_id: str) -> ImportResult:
        """Reset ``step_id`` and any later steps to ``pending`` then re-run."""
        if step_id not in _STEP_IDS:
            raise ValueError(f"unknown step_id: {step_id}")
        state = persistence.read_state(import_id)
        if state is None:
            raise FileNotFoundError(import_id)

        # Validate the target step is in a retryable state.
        target = _step_record(state, step_id)
        if target.get("status") not in {"error", "skipped", "pending"}:
            # Allow retrying complete steps too — useful when a downstream
            # change requires re-running. Not currently exposed through the
            # API but keeps the function permissive.
            pass

        # Reset the target step + everything after it to pending; preserve
        # successful steps before it (and their durations).
        target_idx = _STEP_IDS.index(step_id)
        for idx, sid in enumerate(_STEP_IDS):
            if idx >= target_idx:
                record = _step_record(state, sid)
                # Preserve label, drop everything else.
                preserved_label = record.get("label", sid)
                state_steps = list(state.get("steps") or [])
                for j, r in enumerate(state_steps):
                    if isinstance(r, dict) and r.get("id") == sid:
                        state_steps[j] = {  # type: ignore[assignment]
                            "id": sid,
                            "label": preserved_label,
                            "status": "pending",
                        }
                        break
                state["steps"] = state_steps  # type: ignore[typeddict-item]

        state["status"] = "processing"
        state["error"] = None
        state["error_kind"] = None
        state["stage"] = step_id
        state["updated_at"] = time.time()
        persistence.write_state(import_id, state)

        ctx = self._ctx
        if ctx is None or ctx.import_id != import_id:
            ctx = _rebuild_context(import_id, state)
        return await self.run(ctx)

    # ── confirm ────────────────────────────────────────────────────────

    async def confirm(self, import_id: str) -> ImportResult:
        """Materialize the reviewed draft into a ``LayoutPack`` on disk."""
        state = persistence.read_state(import_id)
        review = persistence.read_review(import_id)
        if state is None or review is None:
            raise FileNotFoundError(import_id)

        template_id = str(review.get("template_id") or state.get("template_id") or "")
        if not template_id:
            raise PersistenceError(
                "missing template_id",
                step_id="confirm",
                error_kind="persistence",
            )

        # Idempotency short-circuit: a complete state with the layout dir
        # already on disk → return as-is without rewriting.
        if state.get("status") == "complete":
            existing = persistence.load_template_pack(template_id)
            if existing is not None:
                return _result_from_state(state)

        ctx = self._ctx
        if ctx is None or ctx.import_id != import_id:
            ctx = _rebuild_context(import_id, state)

        # Reload renderer outputs from disk.
        render_result = _reload_render_result(ctx)
        if render_result is None:
            raise PersistenceError(
                "rendered SVGs unavailable",
                step_id="confirm",
                error_kind="persistence",
            )

        slide_metrics_by_index: dict[int, SlideMetrics] = {
            m.slide_index: m for m in render_result.slide_metrics
        }

        # Build asset role lookups (file_name → role).
        assets_full = list(review.get("assets_full") or [])
        asset_overrides = dict(review.get("assets") or {})
        asset_role_by_file: dict[str, str] = {}
        common_assets: list[ManifestAssetEntry] = []
        layout_assets: dict[str, bytes] = {}
        canvas_w = render_result.canvas[0] or 1280
        canvas_h = render_result.canvas[1] or 720

        for asset in assets_full:
            asset_id = asset.get("asset_id")
            file_name = asset.get("file_name") or ""
            override = asset_overrides.get(asset_id) if asset_id else None
            role = (
                (override or {}).get("role")
                if isinstance(override, dict)
                else None
            ) or asset.get("recommended_role") or "decoration"
            asset_role_by_file[file_name] = role
            if role in ("logo", "background", "decoration"):
                # First occurrence drives the geometry.
                first_occ = (asset.get("occurrences") or [{}])[0]
                bbox = {
                    "x": float(first_occ.get("x") or 0.0) / max(1.0, float(canvas_w)),
                    "y": float(first_occ.get("y") or 0.0) / max(1.0, float(canvas_h)),
                    "width": float(first_occ.get("width") or 0.0) / max(1.0, float(canvas_w)),
                    "height": float(first_occ.get("height") or 0.0) / max(1.0, float(canvas_h)),
                }
                entry: ManifestAssetEntry = {  # type: ignore[typeddict-unknown-key]
                    "asset_id": str(asset_id or ""),
                    "file_name": str(file_name),
                    "role": role,  # type: ignore[typeddict-item]
                    "pages": list(asset.get("pages") or []),
                    "bbox_norm": bbox,
                    "sha1": str(asset.get("sha1") or ""),
                    "bytes": int(asset.get("bytes") or 0),
                }
                common_assets.append(entry)
                # Copy bytes from work_dir/assets if present.
                src_path = ctx.work_dir / "assets" / file_name
                if src_path.exists():
                    try:
                        layout_assets[file_name] = src_path.read_bytes()
                    except OSError:
                        pass

        # Element actions filtered by page_type.
        all_actions: list[ElementActionRecord] = list(review.get("element_actions") or [])

        page_selections = dict(review.get("page_selections") or {})
        templateize_outputs: dict[str, TemplateizeOutput] = {}
        warnings_aggregate: list[ManifestWarning] = []
        content_area_box: BoundingBox | None = None
        export_mode = str(review.get("export_mode") or render_result.export_mode)
        svg_dir = ctx.work_dir / "svg"

        for page_type in _PAGE_TYPES:
            slide_index = page_selections.get(page_type)
            if not slide_index:
                continue
            svg_path = svg_dir / f"slide-{int(slide_index):03d}.svg"
            if not svg_path.exists():
                continue
            metric = slide_metrics_by_index.get(int(slide_index))
            if metric is None:
                metric = SlideMetrics(
                    slide_index=int(slide_index),
                    canvas_width=canvas_w,
                    canvas_height=canvas_h,
                    font_substitutions={},
                    rendered_at=time.time(),
                )

            actions: list[ElementAction] = []
            for record in all_actions:
                if not isinstance(record, dict):
                    continue
                if record.get("page_type") != page_type:
                    continue
                actions.append(
                    ElementAction(
                        page_type=record.get("page_type", page_type),  # type: ignore[arg-type]
                        element_id=str(record.get("element_id") or ""),
                        action=record.get("action", "keep"),  # type: ignore[arg-type]
                        placeholder=record.get("placeholder"),
                        reason=record.get("reason"),
                        source=record.get("source", "rule"),  # type: ignore[arg-type]
                    )
                )

            # Run chrome detector on the fly — cheap and avoids stale cache.
            try:
                chrome_items = chrome_detector.detect(
                    [svg_path],
                    [metric],
                    preserve_texts=list(review.get("preserve_texts") or []),
                    placeholder_hints={
                        k: dict(v)
                        for k, v in (review.get("placeholder_hints") or {}).items()
                        if isinstance(v, dict)
                    },
                )
            except Exception:  # noqa: BLE001
                chrome_items = []

            try:
                output = templateizer.templateize(
                    svg_path,
                    page_type,  # type: ignore[arg-type]
                    metrics=metric,
                    actions=actions,
                    chrome_items=chrome_items,
                    asset_roles=asset_role_by_file,
                )
            except Exception as exc:  # noqa: BLE001
                raise PersistenceError(
                    f"templateize failed for {page_type}: {exc}",
                    step_id="confirm",
                    error_kind="persistence",
                ) from exc

            templateize_outputs[page_type] = output
            warnings_aggregate.extend(output.warnings or [])
            if page_type == "content" and output.content_area is not None:
                content_area_box = output.content_area

        # Inline image references in templated SVGs so the persisted pack
        # never points at ``http(s)://`` URLs and renders identically in
        # the preview and in production (Requirement 12.2; Property 22).
        for page_type, output in templateize_outputs.items():
            output.svg_text = inline_svg_asset_refs(output.svg_text, layout_assets)

        # Assemble manifest.
        canvas_block: ManifestCanvas = {"width": int(canvas_w), "height": int(canvas_h)}
        theme_block: ManifestTheme = {  # type: ignore[typeddict-unknown-key]
            "colors": {},
            "fonts": {},
        }
        # Pull theme colors if previously captured on the state record.
        theme_colors = list(state.get("theme_colors") or [])
        for idx, color in enumerate(theme_colors[:6]):
            theme_block["colors"][f"accent{idx + 1}"] = str(color)

        content_area_dict: ManifestContentArea | None = None
        if content_area_box is not None:
            content_area_dict = {
                "x": float(content_area_box.x),
                "y": float(content_area_box.y),
                "width": float(content_area_box.width),
                "height": float(content_area_box.height),
            }

        page_selections_clean = {
            pt: int(v) if v is not None else None for pt, v in page_selections.items() if pt in _PAGE_TYPES
        }
        page_type_candidates = {
            pt: list(v) for pt, v in (review.get("page_type_candidates") or {}).items() if pt in _PAGE_TYPES
        }

        manifest: TemplateManifest = {  # type: ignore[typeddict-unknown-key]
            "schema_version": 2,
            "template_id": template_id,
            "label": str(review.get("label") or state.get("label") or template_id),
            "source_file": str(state.get("source_file") or ctx.pptx_path.name),
            "slide_count": int(state.get("slide_count") or render_result and len(render_result.svg_files) or 0),
            "canvas": canvas_block,
            "theme": theme_block,
            "page_type_candidates": page_type_candidates,  # type: ignore[typeddict-item]
            "page_selections": page_selections_clean,  # type: ignore[typeddict-item]
            "common_assets": common_assets,
            "content_area": content_area_dict,
            "warnings": warnings_aggregate,
            "imported_at": time.time(),
            "export_mode": export_mode,  # type: ignore[typeddict-item]
            "importer_version": _IMPORTER_VERSION,
        }

        svgs: dict[str, str] = {pt: out.svg_text for pt, out in templateize_outputs.items()}

        design_spec = str(review.get("design_spec_md") or "").strip()
        if not design_spec:
            design_spec = _default_design_spec(review, manifest)

        import_trace: list[LLMTraceEntry] = list(review.get("llm_trace") or [])

        pack = LayoutPack(
            template_id=template_id,
            label=manifest["label"],
            manifest=manifest,
            svgs=svgs,  # type: ignore[arg-type]
            design_spec=design_spec,
            assets=layout_assets,
            import_trace=import_trace,
        )

        try:
            install_result = persistence.transactional_install_layout(template_id, pack)
        except PersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                str(exc) or exc.__class__.__name__,
                step_id="confirm",
                error_kind="persistence",
            ) from exc

        try:
            persistence.update_user_index(template_id, manifest["label"], manifest)
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                f"index update failed: {exc}",
                step_id="confirm",
                error_kind="persistence",
            ) from exc

        # Finalize state.
        state["status"] = "complete"
        state["stage"] = "complete"
        state["progress"] = 1.0
        state["template_id"] = template_id
        state["label"] = manifest["label"]
        state["export_mode"] = export_mode
        state["review_required"] = False
        state["error"] = None
        state["error_kind"] = None
        state["updated_at"] = time.time()
        persistence.write_state(import_id, state)

        result = _result_from_state(state)
        if install_result.already_complete:
            result.message = "Template already installed."
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Module-level helpers
# ─────────────────────────────────────────────────────────────────────────────


def _rebuild_context(import_id: str, state: ImportTaskState) -> PipelineContext:
    """Reconstruct a :class:`PipelineContext` from on-disk state.

    Used when ``retry_step`` / ``confirm`` is called without a live
    pipeline reference (e.g. fresh worker process).
    """
    from backend.config import settings

    work_dir = settings.workspaces_dir / "template_imports" / import_id
    pptx_path = Path(state.get("source_file") or "")
    return PipelineContext(
        import_id=import_id,
        work_dir=work_dir,
        pptx_path=pptx_path,
        label=str(state.get("label") or import_id),
        model_config={},
    )


def _reload_render_result(ctx: PipelineContext) -> RenderResult | None:
    """Best-effort reload of the rendered SVG batch from disk."""
    from .renderer.slide_metrics import read_slide_metrics

    svg_dir = ctx.work_dir / "svg"
    if not svg_dir.is_dir():
        return None
    svg_files = sorted(svg_dir.glob("slide-*.svg"))
    if not svg_files:
        return None
    metrics = read_slide_metrics(svg_dir)
    if not metrics:
        # Synthesize minimal metrics from filename order if metadata is missing.
        canvas = (1280, 720)
        metrics = [
            SlideMetrics(
                slide_index=idx + 1,
                canvas_width=canvas[0],
                canvas_height=canvas[1],
                font_substitutions={},
                rendered_at=0.0,
            )
            for idx in range(len(svg_files))
        ]
    canvas_w = metrics[0].canvas_width if metrics else 1280
    canvas_h = metrics[0].canvas_height if metrics else 720
    return RenderResult(
        svg_files=svg_files,
        slide_metrics=metrics,
        export_mode="libreoffice",  # best-guess; actual export_mode is recorded on state
        canvas=(canvas_w, canvas_h),
    )


def _default_design_spec(review: ReviewDraft, manifest: TemplateManifest) -> str:
    """Synthesize a minimal ``design_spec.md`` when none was provided."""
    lines = [
        f"# {manifest.get('label') or manifest.get('template_id')}",
        "",
        f"- Imported from: {manifest.get('source_file') or ''}",
        f"- Slide count: {manifest.get('slide_count', 0)}",
        f"- Export mode: {manifest.get('export_mode') or ''}",
        "",
        "## Page Selections",
    ]
    for pt in _PAGE_TYPES:
        sel = (manifest.get("page_selections") or {}).get(pt)
        lines.append(f"- {pt}: {sel if sel else '—'}")
    return "\n".join(lines) + "\n"


__all__ = ["Pipeline", "inline_svg_asset_refs"]
