"""Template import v2 — facade exposing the pipeline as endpoint-callable helpers.

This package implements the template-import overhaul described in
``.kiro/specs/template-import-overhaul/design.md``. Leaf modules
(``renderer``, ``asset_extractor``, ``chrome_detector``,
``page_classifier``, ``placeholder_schema``, ``templateizer``,
``llm_client``, ``persistence``, ``observability``) are mutually
independent and orchestrated by :class:`pipeline.Pipeline`.

The public surface mirrors what
``backend/api/endpoints/template_import.py`` calls on the v1 path, so the
endpoint layer can switch between the two by toggling
``settings.template_import_v2`` without changing request/response shapes.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path
from typing import Any

from backend.config import settings

from . import persistence
from .llm_client import LLMClient, merge_llm_plan
from .pipeline import Pipeline, build_editable_template_deck, inline_svg_asset_refs, preview_templateized
from .types import (
    ImportResult,
    ImportTaskState,
    PipelineContext,
    ReviewDraft,
)


# ─────────────────────────────────────────────────────────────────────────────
# ID + label helpers (kept in sync with v1 ``template_importer``)
# ─────────────────────────────────────────────────────────────────────────────


def _label_from_name(pptx_name: str) -> str:
    return Path(pptx_name).stem.replace("_", " ").replace("-", " ").title()


def _generate_template_id(pptx_name: str, import_id: str | None = None) -> str:
    stem = Path(pptx_name).stem
    safe = "".join(c if c.isalnum() else "_" for c in stem.lower()).strip("_") or "template"
    digest_seed = f"{stem}:{import_id or ''}"
    short_hash = hashlib.sha1(digest_seed.encode("utf-8")).hexdigest()[:6]
    return f"user_{safe}_{short_hash}"


def _build_context(
    import_id: str,
    pptx_path: Path,
    label: str | None,
    model_config: dict[str, Any] | None,
) -> PipelineContext:
    work_dir = settings.workspaces_dir / "template_imports" / import_id
    work_dir.mkdir(parents=True, exist_ok=True)
    return PipelineContext(
        import_id=import_id,
        work_dir=work_dir,
        pptx_path=pptx_path,
        label=label or _label_from_name(pptx_path.name),
        model_config=dict(model_config or {}),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Facade — endpoint-callable helpers
# ─────────────────────────────────────────────────────────────────────────────


def initialize_import(
    import_id: str,
    pptx_path: Path,
    *,
    label: str | None = None,
) -> str:
    """Create the initial work_dir + state.json. Returns the assigned ``template_id``."""
    template_id = _generate_template_id(pptx_path.name, import_id)
    ctx = _build_context(import_id, pptx_path, label, None)
    state = persistence.read_state(import_id) or {}
    state.setdefault("import_id", import_id)
    state["importer_version"] = "template_import.v2"
    state["template_id"] = template_id
    state.setdefault("label", ctx.label)
    state.setdefault("source_file", str(pptx_path))
    state.setdefault("status", "processing")
    state.setdefault("stage", "uploaded")
    state.setdefault("progress", 0.0)
    state.setdefault("review_required", False)
    state.setdefault("steps", [])
    persistence.write_state(import_id, state)
    return template_id


def set_import_collaboration_mode(import_id: str, mode: str) -> ImportTaskState:
    """Persist the import-level collaboration owner for UI and pipeline decisions."""
    state = persistence.read_state(import_id)
    if state is None:
        raise FileNotFoundError(import_id)
    state["collaboration_mode"] = mode if mode in {"direct", "agent", "classic"} else "direct"  # type: ignore[typeddict-item]
    state["updated_at"] = time.time()
    persistence.write_state(import_id, state)
    return state


async def run_import(
    import_id: str,
    pptx_path: Path,
    *,
    label: str | None = None,
    model_config: dict[str, Any] | None = None,
) -> ImportResult:
    """Run the full pipeline up to ``review_required`` (or error)."""
    ctx = _build_context(import_id, pptx_path, label, model_config)
    pipeline = Pipeline(ctx)
    return await pipeline.run(ctx)


async def retry_import_step(
    import_id: str,
    step_id: str,
    *,
    pptx_path: Path | None = None,
    label: str | None = None,
    model_config: dict[str, Any] | None = None,
) -> ImportResult:
    """Reset ``step_id`` (and later steps) to pending, then re-run."""
    state = persistence.read_state(import_id)
    if state is None:
        raise FileNotFoundError(import_id)
    pptx = pptx_path or Path(state.get("source_file") or "")
    ctx = _build_context(import_id, pptx, label or state.get("label"), model_config)
    pipeline = Pipeline(ctx)
    return await pipeline.retry_step(import_id, step_id)


async def preview_draft(import_id: str) -> dict[str, str]:
    """Render preview SVGs for the current draft. Returns ``page_type → svg_text``.

    Falls back to the on-disk renderer outputs without rebuilding the
    layout pack. The return shape mirrors the v1 ``preview_import_template``
    response (sans theme metadata) so it can be wrapped by the API layer.
    """
    review = persistence.read_review(import_id)
    if review is None:
        raise FileNotFoundError(import_id)
    state = persistence.read_state(import_id)
    if state is None:
        raise FileNotFoundError(import_id)

    work_dir = settings.workspaces_dir / "template_imports" / import_id
    svg_dir = work_dir / "svg"
    selections = review.get("page_selections") or {}

    out: dict[str, str] = {}
    for pt in ("cover", "toc", "chapter", "content", "ending"):
        slide_index = selections.get(pt)
        if not slide_index:
            out[pt] = ""
            continue
        svg_path = svg_dir / f"slide-{int(slide_index):03d}.svg"
        if not svg_path.exists():
            out[pt] = ""
            continue
        try:
            text = svg_path.read_text(encoding="utf-8")
        except OSError:
            out[pt] = ""
            continue
        # Inline asset refs for browser preview.
        asset_dir = work_dir / "assets"
        asset_map: dict[str, Path] = {}
        if asset_dir.is_dir():
            for child in asset_dir.iterdir():
                if child.is_file():
                    asset_map[child.name] = child
        out[pt] = inline_svg_asset_refs(text, asset_map)
    return out


async def preview_clean_template(import_id: str) -> dict[str, Any]:
    """Return a clean templateized preview for the current PPTist/review state."""
    return preview_templateized(import_id)


def editable_template_deck(import_id: str) -> dict[str, Any] | None:
    """Return a PPTist-editable clean template deck when Agent output exists."""
    return build_editable_template_deck(import_id)


async def confirm_import(import_id: str) -> ImportResult:
    """Materialize the reviewed draft into a layout pack."""
    state = persistence.read_state(import_id)
    if state is None:
        raise FileNotFoundError(import_id)
    pptx = Path(state.get("source_file") or "")
    ctx = _build_context(import_id, pptx, state.get("label"), None)
    pipeline = Pipeline(ctx)
    return await pipeline.confirm(import_id)


def get_review(import_id: str) -> ReviewDraft | None:
    """Read the current review draft, or ``None`` if absent."""
    return persistence.read_review(import_id)


def update_review(import_id: str, draft: dict[str, Any]) -> ReviewDraft:
    """Merge user-supplied draft fields into ``review.json`` and persist.

    Only the fields the v1 endpoint accepts (``label`` /
    ``page_selections`` / ``assets``) are merged; other fields pass through
    untouched. Raises :class:`FileNotFoundError` when the import is unknown.
    """
    review = persistence.read_review(import_id)
    if review is None:
        raise FileNotFoundError(import_id)
    current = review.setdefault("draft", {})
    if not isinstance(current, dict):
        current = {}
        review["draft"] = current  # type: ignore[typeddict-item]

    if "label" in draft and draft["label"] is not None:
        label = str(draft["label"]).strip() or review.get("label", "")
        review["label"] = label  # type: ignore[typeddict-item]
        current["label"] = label
    if "page_selections" in draft and isinstance(draft["page_selections"], dict):
        selections = dict(review.get("page_selections") or {})
        draft_selections = dict(current.get("page_selections") or {})
        for pt in ("cover", "toc", "chapter", "content", "ending"):
            if pt in draft["page_selections"]:
                value = draft["page_selections"][pt]
                normalized = int(value) if value else None
                selections[pt] = normalized
                draft_selections[pt] = normalized
        review["page_selections"] = selections  # type: ignore[typeddict-item]
        current["page_selections"] = draft_selections
    if "assets" in draft and isinstance(draft["assets"], dict):
        assets = dict(review.get("assets") or {})
        draft_assets = dict(current.get("assets") or {})
        for asset_id, value in draft["assets"].items():
            if not isinstance(value, dict):
                continue
            entry = dict(assets.get(asset_id) or {})
            entry["asset_id"] = asset_id
            if value.get("role") is not None:
                entry["role"] = str(value["role"])
            if value.get("name") is not None:
                entry["name"] = str(value["name"])
            entry["role_source"] = "user"
            assets[asset_id] = entry  # type: ignore[assignment]
            draft_assets[asset_id] = {
                "role": entry.get("role"),
                "name": entry.get("name", ""),
            }
        review["assets"] = assets  # type: ignore[typeddict-item]
        current["assets"] = draft_assets

    if "preserve_texts" in draft and isinstance(draft["preserve_texts"], list):
        preserve_texts = [
            str(item)
            for item in draft["preserve_texts"]
            if str(item).strip()
        ]
        review["preserve_texts"] = preserve_texts  # type: ignore[typeddict-item]
        current["preserve_texts"] = preserve_texts

    if "placeholder_hints" in draft and isinstance(draft["placeholder_hints"], dict):
        hints: dict[str, dict[str, str]] = {}
        for page_type, values in draft["placeholder_hints"].items():
            if not isinstance(values, dict):
                continue
            hints[str(page_type)] = {
                str(name): str(original)
                for name, original in values.items()
                if str(name).strip() and str(original).strip()
            }
        review["placeholder_hints"] = hints  # type: ignore[typeddict-item]
        current["placeholder_hints"] = hints

    if "element_actions" in draft and isinstance(draft["element_actions"], list):
        element_actions = [
            dict(item)
            for item in draft["element_actions"]
            if isinstance(item, dict)
        ]
        review["element_actions"] = element_actions  # type: ignore[typeddict-item]
        current["element_actions"] = element_actions

    if "design_spec" in draft and draft["design_spec"] is not None:
        design_spec = str(draft["design_spec"])
        review["design_spec_md"] = design_spec  # type: ignore[typeddict-item]
        current["design_spec"] = design_spec

    if "annotations" in draft and isinstance(draft["annotations"], list):
        # Replace whole list — frontend always sends the full array.
        review["annotations"] = [
            _normalize_annotation_record(entry)
            for entry in draft["annotations"]
            if isinstance(entry, dict)
        ]  # type: ignore[typeddict-item]

    persistence.write_review(import_id, review)
    return review


def get_status(import_id: str) -> dict[str, Any] | None:
    """Read the persisted ``state.json`` for an import task.

    Returns the raw ``ImportTaskState`` dict (TypedDict erased to ``dict``
    at runtime) or ``None`` if the import is unknown. Mirrors v1's
    :func:`backend.generator.template_importer.get_import_task`.
    """
    return persistence.read_state(import_id)


def rename_user_template(template_id: str, label: str) -> bool:
    """Rename a v2-installed user template.

    Forwards to :func:`persistence.rename_user_template`, which raises
    :class:`ValueError` when ``template_id`` does not start with
    ``user_`` (Requirement 9.3). Callers in the API layer translate
    that into HTTP 400.
    """
    return persistence.rename_user_template(template_id, label)


def remove_user_template(template_id: str) -> bool:
    """Delete a v2-installed user template.

    Forwards to :func:`persistence.remove_user_template`, which raises
    :class:`ValueError` for non-``user_`` ids (Requirement 9.3).
    """
    return persistence.remove_user_template(template_id)


# ─────────────────────────────────────────────────────────────────────────────
# Annotation CRUD facade
# ─────────────────────────────────────────────────────────────────────────────


def _clamp01(value: Any) -> float:
    """Clamp ``value`` (cast to float) into [0, 1]."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def _normalize_bbox_norm(bbox: Any) -> dict[str, float]:
    """Coerce an arbitrary mapping into ``{x, y, width, height}`` ∈ [0,1]."""
    src = bbox if isinstance(bbox, dict) else {}
    return {
        "x": _clamp01(src.get("x")),
        "y": _clamp01(src.get("y")),
        "width": _clamp01(src.get("width")),
        "height": _clamp01(src.get("height")),
    }


def _normalize_annotation_record(entry: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a persisted annotation dict to the canonical shape."""
    return {
        "annotation_id": str(entry.get("annotation_id") or uuid.uuid4().hex[:12]),
        "slide_index": int(entry.get("slide_index") or 1),
        "bbox_norm": _normalize_bbox_norm(entry.get("bbox_norm")),
        "note": str(entry.get("note") or "").strip(),
        "linked_element_id": entry.get("linked_element_id") or None,
        "created_at": float(entry.get("created_at") or time.time()),
        "resolved": bool(entry.get("resolved") or False),
    }


def list_annotations(import_id: str) -> list[dict[str, Any]]:
    """Return persisted annotations for ``import_id``."""
    review = persistence.read_review(import_id)
    if review is None:
        raise FileNotFoundError(import_id)
    raw = review.get("annotations") or []
    return [dict(entry) for entry in raw if isinstance(entry, dict)]


def add_annotation(
    import_id: str,
    slide_index: int,
    bbox_norm: dict[str, float],
    note: str,
    linked_element_id: str | None = None,
) -> dict[str, Any]:
    """Append a new annotation. Returns the persisted record."""
    review = persistence.read_review(import_id)
    if review is None:
        raise FileNotFoundError(import_id)
    record = _normalize_annotation_record(
        {
            "annotation_id": uuid.uuid4().hex[:12],
            "slide_index": slide_index,
            "bbox_norm": bbox_norm,
            "note": note,
            "linked_element_id": linked_element_id,
            "created_at": time.time(),
            "resolved": False,
        }
    )
    annotations = list(review.get("annotations") or [])
    annotations.append(record)
    review["annotations"] = annotations  # type: ignore[typeddict-item]
    persistence.write_review(import_id, review)
    return record


def remove_annotation(import_id: str, annotation_id: str) -> bool:
    """Delete an annotation by id. Returns ``True`` if a record was removed."""
    review = persistence.read_review(import_id)
    if review is None:
        raise FileNotFoundError(import_id)
    annotations = list(review.get("annotations") or [])
    remaining = [
        entry for entry in annotations
        if not (isinstance(entry, dict) and entry.get("annotation_id") == annotation_id)
    ]
    if len(remaining) == len(annotations):
        return False
    review["annotations"] = remaining  # type: ignore[typeddict-item]
    persistence.write_review(import_id, review)
    return True


def update_annotation(
    import_id: str,
    annotation_id: str,
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Patch an existing annotation and return the normalized record."""
    review = persistence.read_review(import_id)
    if review is None:
        raise FileNotFoundError(import_id)
    annotations = list(review.get("annotations") or [])
    for idx, entry in enumerate(annotations):
        if not isinstance(entry, dict) or entry.get("annotation_id") != annotation_id:
            continue
        next_entry = dict(entry)
        if "bbox_norm" in patch:
            next_entry["bbox_norm"] = patch["bbox_norm"]
        if "note" in patch:
            next_entry["note"] = patch["note"]
        if "linked_element_id" in patch:
            next_entry["linked_element_id"] = patch["linked_element_id"]
        if "resolved" in patch:
            next_entry["resolved"] = patch["resolved"]
        next_entry["annotation_id"] = annotation_id
        normalized = _normalize_annotation_record(next_entry)
        annotations[idx] = normalized
        review["annotations"] = annotations  # type: ignore[typeddict-item]
        persistence.write_review(import_id, review)
        return normalized
    raise KeyError(annotation_id)


def list_user_templates() -> list[dict[str, Any]]:
    """Read the ``user_templates.json`` index as a flat list of rows."""
    index_path = persistence._layouts_root() / "user_templates.json"
    raw = persistence._read_json_or_none(index_path)
    if not isinstance(raw, dict):
        return []
    templates = raw.get("templates")
    if not isinstance(templates, dict):
        return []
    out: list[dict[str, Any]] = []
    for tid, row in templates.items():
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "template_id": tid,
                "label": row.get("label", tid),
                "summary": row.get("summary", ""),
                "slide_count": row.get("slide_count", row.get("slideCount", 0)),
                "slideCount": row.get("slideCount", row.get("slide_count", 0)),
            }
        )
    out.sort(key=lambda r: r["template_id"])
    return out


__all__ = [
    "Pipeline",
    "PipelineContext",
    "LLMClient",
    "merge_llm_plan",
    "inline_svg_asset_refs",
    "initialize_import",
    "run_import",
    "retry_import_step",
    "preview_draft",
    "editable_template_deck",
    "confirm_import",
    "get_review",
    "get_status",
    "update_review",
    "rename_user_template",
    "remove_user_template",
    "list_user_templates",
    "add_annotation",
    "update_annotation",
    "remove_annotation",
    "list_annotations",
    # Short aliases — kept in sync with v1 ``template_importer`` naming
    # so callers can use either form without caring which path they hit.
    "initialize",
    "import_pptx",
    "confirm",
    "retry",
]


# ─────────────────────────────────────────────────────────────────────────────
# Short aliases (mirror v1 ``template_importer`` shorthand)
# ─────────────────────────────────────────────────────────────────────────────

initialize = initialize_import
import_pptx = run_import
confirm = confirm_import
retry = retry_import_step
