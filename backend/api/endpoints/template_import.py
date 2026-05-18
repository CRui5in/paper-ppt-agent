"""Template import endpoints — upload PPTX and manage user templates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Response, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.config import settings
from backend.api.schemas import ModelConfig

# DEPRECATED: replaced by template_import.v2 facade when settings.template_import_v2=True.
# v1 helpers are still imported for the legacy code path; the v2 facade
# (``backend.generator.template_import``) is dispatched to when the feature
# flag is on.
from backend.generator.template_importer import (
    ImportResult,
    assist_import_review,
    confirm_import_template,
    get_import_review,
    get_import_result,
    get_import_task,
    initialize_import_task,
    import_pptx_template,
    inline_svg_asset_refs,
    list_user_templates,
    mark_import_ready_for_review,
    preview_import_template,
    remove_user_template,
    rename_user_template,
    save_import_review,
    set_import_collaboration_mode,
)
from backend.generator import template_import as template_import_v2
from backend.generator.template_manager import load_template
from backend.generator.template_agent import (
    TemplateAgentConfig,
    template_agent_manager,
)
from backend.runtime import aensure_dir, aoffload, awrite_bytes


def _v2_enabled() -> bool:
    """Feature flag: route through the v2 facade when on."""
    return bool(getattr(settings, "template_import_v2", False))


def _collaboration_mode_for_import(import_id: str) -> Literal["classic", "agent"]:
    if _v2_enabled():
        state = template_import_v2.get_status(import_id) or {}
    else:
        state = get_import_task(import_id) or {}
    return "agent" if state.get("collaboration_mode") == "agent" else "classic"


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/templates")


# ── Response models ───────────────────────────────────────────────────────────


class ImportStartResponse(BaseModel):
    import_id: str
    status: str = "processing"
    template_id: str | None = None
    collaboration_mode: Literal["classic", "agent"] = "classic"


class ImportStatusResponse(BaseModel):
    import_id: str
    status: str  # processing | review_required | complete | error
    stage: str = "uploaded"
    progress: float = 0.0
    message: str = ""
    steps: list[dict[str, str]] = []
    review_required: bool = False
    template_id: str | None = None
    label: str | None = None
    slide_count: int = 0
    export_mode: str = ""
    theme_colors: list[str] = []
    error: str | None = None
    collaboration_mode: Literal["classic", "agent"] = "classic"


class TemplatePreviewResponse(BaseModel):
    template_id: str
    label: str
    cover_svg: str = ""
    toc_svg: str = ""
    chapter_svg: str = ""
    content_svg: str = ""
    ending_svg: str = ""
    design_spec: str = ""
    theme_colors: list[str] = []


class DeleteTemplateResponse(BaseModel):
    template_id: str
    deleted: bool


class UserTemplateItem(BaseModel):
    template_id: str
    label: str
    summary: str = ""
    slide_count: int = 0


class TemplateReviewDraftRequest(BaseModel):
    label: str | None = None
    page_selections: dict[str, int | None] | None = None
    assets: dict[str, dict[str, str | None]] | None = None
    preserve_texts: list[str] | None = None
    placeholder_hints: dict[str, dict[str, str]] | None = None
    element_actions: list[dict[str, Any]] | None = None
    design_spec: str | None = None
    annotations: list[dict[str, Any]] | None = None


class TemplateReviewResponse(BaseModel):
    import_id: str
    template_id: str
    label: str
    status: str
    export_mode: str = ""
    slide_count: int = 0
    page_types: list[str] = []
    asset_roles: list[str] = []
    page_type_candidates: dict[str, list[int]] = {}
    slides: list[dict] = []
    assets: list[dict] = []
    draft: dict = {}
    theme_colors: list[str] = []
    text_candidates: list[dict] = []
    llm: dict = Field(default_factory=dict)
    feedback_history: list[dict] = []
    annotations: list[dict] = []
    conversation: list[dict] = []
    llm_trace: dict = Field(default_factory=dict)


class ConfirmImportResponse(ImportStatusResponse):
    pass


class RenameTemplateRequest(BaseModel):
    label: str


class TemplateImportAssistRequest(BaseModel):
    llm_config: ModelConfig | None = Field(default=None, alias="model_config")


class TemplateImportFeedbackRequest(BaseModel):
    llm_config: ModelConfig = Field(alias="model_config")
    feedback: str
    draft: TemplateReviewDraftRequest | None = None


class TemplateAgentConfigRequest(BaseModel):
    mode: Literal["claude_code", "custom"] = "claude_code"
    api_key: str | None = None
    auth_token: str | None = None
    base_url: str | None = None
    model: str | None = None
    custom_model_option: str | None = None
    load_project_settings: bool = True
    # ``None`` => unlimited; otherwise must be at least 1 (the SDK still
    # enforces its own internal sanity ceiling).
    max_turns: int | None = Field(default=None, ge=1)
    reply_language: Literal["zh", "en"] = "en"


class TemplateAgentStartRequest(BaseModel):
    feedback: str
    config: TemplateAgentConfigRequest = Field(default_factory=TemplateAgentConfigRequest)
    draft: TemplateReviewDraftRequest | None = None
    # When true the backend treats the feedback as an internal seed prompt
    # (e.g. the auto template-ization kickoff) and skips appending it to the
    # visible review conversation.
    silent: bool = False
    # False means a read-only inspection run: the Agent may look at the
    # workspace and ask the user what to do, but completion must not satisfy
    # the template-planning gate.
    planning: bool = True


class TemplateAgentStartResponse(BaseModel):
    agent_job_id: str
    import_id: str
    status: str


class TemplateAgentTemplateSvgRequest(BaseModel):
    svg: str


class TemplateAgentStatusResponse(BaseModel):
    agent_job_id: str
    import_id: str
    status: str
    message: str = ""
    error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float | None = None
    completed_at: float | None = None


class TemplateImportFileItem(BaseModel):
    name: str
    path: str
    type: Literal["file", "directory"]
    size: int | None = None
    image: bool = False
    preview_url: str | None = None


class TemplateImportFileListResponse(BaseModel):
    cwd: str
    parent: str | None = None
    items: list[TemplateImportFileItem]


# ── In-memory import results (for preview) ───────────────────────────────────

_import_results: dict[str, ImportResult] = {}


# ── Preview debounce cache (Task 12.5) ────────────────────────────────────────
#
# Concurrent ``POST /import/{id}/preview`` calls for the same import_id are
# serialized through a per-import asyncio.Lock; redundant builds with the
# same draft signature short-circuit on a small LRU cache. The cache is
# cleared on /confirm and bounded to 32 imports to keep memory flat.

_PREVIEW_CACHE_MAX_ENTRIES = 32
_preview_locks: dict[str, asyncio.Lock] = {}
_preview_cache: "OrderedDict[str, tuple[str, dict[str, Any]]]" = OrderedDict()


def _preview_lock_for(import_id: str) -> asyncio.Lock:
    """Return a per-import_id lock, creating one on first access."""
    lock = _preview_locks.get(import_id)
    if lock is None:
        lock = asyncio.Lock()
        _preview_locks[import_id] = lock
    return lock


def _preview_signature(payload: dict[str, Any]) -> str:
    """Stable SHA-1 of the draft payload."""
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()


def _preview_cache_get(import_id: str, sig: str) -> dict[str, Any] | None:
    cached = _preview_cache.get(import_id)
    if cached is None or cached[0] != sig:
        return None
    # Refresh recency.
    _preview_cache.move_to_end(import_id)
    return cached[1]


def _preview_cache_put(import_id: str, sig: str, value: dict[str, Any]) -> None:
    _preview_cache[import_id] = (sig, value)
    _preview_cache.move_to_end(import_id)
    while len(_preview_cache) > _PREVIEW_CACHE_MAX_ENTRIES:
        _preview_cache.popitem(last=False)


def _preview_cache_drop(import_id: str) -> None:
    """Invalidate the cached preview for an import (called on /confirm)."""
    _preview_cache.pop(import_id, None)
    _preview_locks.pop(import_id, None)


def _import_workspace(import_id: str) -> Path:
    return settings.workspaces_dir / "template_imports" / import_id


def _template_import_workspace(import_id: str) -> Path:
    root = _import_workspace(import_id).resolve()
    if not root.exists() or not root.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Import workspace '{import_id}' not found.",
        )
    return root


def _resolve_import_workspace_path(root: Path, relative: str = "") -> Path:
    rel = (relative or "").replace("\\", "/").strip("/")
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Path is outside the import workspace.") from None
    return target


def _workspace_relative_path(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def _is_previewable_image(path: Path) -> bool:
    return path.suffix.lower() in {".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _slide_svg_path(import_id: str, slide_index: int):
    workspace = _import_workspace(import_id)
    candidates = []
    for svg_dir in (
        workspace / "svg",
        workspace / "work" / "svg",
        workspace / "svg_raw",
        workspace / "work" / "svg_raw",
    ):
        candidates.extend(
            [
                svg_dir / f"slide_{slide_index:02d}.svg",
                svg_dir / f"slide_{slide_index:03d}.svg",
                svg_dir / f"slide-{slide_index:03d}.svg",
                svg_dir / f"slide-{slide_index:02d}.svg",
            ]
        )
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def _asset_preview_path(import_id: str, file_name: str):
    safe_name = file_name.replace("\\", "/").rsplit("/", 1)[-1]
    workspace = _import_workspace(import_id)
    candidates = [
        workspace / "assets" / safe_name,
        workspace / "work" / "assets" / safe_name,
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def _coerce_preserve_texts(value: Any) -> list[str]:
    items = value if isinstance(value, list) else []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = str(
                item.get("text")
                or item.get("value")
                or item.get("content")
                or item.get("label")
                or ""
            )
        else:
            text = ""
        text = " ".join(text.split())
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _review_response_payload(import_id: str, review: dict[str, Any]) -> dict[str, Any]:
    """Return a lightweight review payload; heavy previews are URL-addressed."""
    payload = dict(review)
    draft = dict(payload.get("draft") or {})
    draft["preserve_texts"] = _coerce_preserve_texts(draft.get("preserve_texts"))
    payload["draft"] = draft
    slides: list[dict[str, Any]] = []
    for raw in list(review.get("slides") or []):
        if not isinstance(raw, dict):
            continue
        slide = dict(raw)
        slide.pop("preview_svg", None)
        try:
            idx = int(slide.get("index") or 0)
        except (TypeError, ValueError):
            idx = 0
        if idx > 0:
            slide["preview_svg_url"] = f"/api/templates/import/{import_id}/slides/{idx}.svg"
        slides.append(slide)
    payload["slides"] = slides

    assets: list[dict[str, Any]] = []
    raw_assets = review.get("assets") or []
    if isinstance(raw_assets, dict):
        raw_assets = list(raw_assets.values())
    for raw in list(raw_assets):
        if not isinstance(raw, dict):
            continue
        asset = dict(raw)
        asset.pop("preview_data_uri", None)
        file_name = str(asset.get("file_name") or asset.get("name") or "").strip()
        if file_name:
            asset["preview_url"] = f"/api/templates/import/{import_id}/assets/{quote(file_name, safe='')}"
        assets.append(asset)
    payload["assets"] = assets

    llm_trace = payload.get("llm_trace")
    if isinstance(llm_trace, list):
        payload["llm_trace"] = llm_trace[-1] if llm_trace else {}
    return payload


async def _save_review_draft(import_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Save a review patch and keep annotation fields in the shared review file.

    The legacy v1 importer owns most review fields; the v2 facade owns the
    annotation normalizer. Both paths write to the same
    ``workspaces/template_imports/<id>/review.json`` file, so centralising this
    merge prevents feedback / preview calls from silently dropping visual notes.
    """
    review = await aoffload(save_import_review, import_id, payload)
    if "annotations" in payload:
        try:
            review = await aoffload(
                template_import_v2.update_review,
                import_id,
                {"annotations": payload["annotations"]},
            )
        except FileNotFoundError:
            pass
    _preview_cache_drop(import_id)
    return review


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/upload", response_model=ImportStartResponse)
async def upload_template_pptx(
    file: UploadFile,
    model_config_json: str | None = Form(None, alias="model_config"),
    collaboration_mode: Literal["classic", "agent"] = Form("classic"),
) -> ImportStartResponse:
    """Upload a PPTX file and start async template import."""
    if not file.filename or not file.filename.lower().endswith(".pptx"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .pptx files are accepted.",
        )

    llm_config: ModelConfig | None = None
    if model_config_json:
        try:
            llm_config = ModelConfig.model_validate_json(model_config_json)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Template import requires a valid LLM model configuration.",
            ) from exc
    if collaboration_mode == "classic" and llm_config is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="LLM mode requires a valid model configuration.",
        )

    # Save uploaded file
    content = await file.read()
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds {settings.max_upload_bytes // (1024*1024)}MB limit.",
        )

    import_id = uuid.uuid4().hex[:12]
    upload_dir = settings.workspaces_dir / "template_imports" / import_id
    await aensure_dir(upload_dir)
    pptx_path = upload_dir / file.filename
    await awrite_bytes(pptx_path, content)
    if _v2_enabled():
        await aoffload(template_import_v2.initialize_import, import_id, pptx_path)
        await aoffload(template_import_v2.set_import_collaboration_mode, import_id, collaboration_mode)
    else:
        await aoffload(initialize_import_task, import_id, pptx_path)
        await aoffload(set_import_collaboration_mode, import_id, collaboration_mode)

    # Run the long-running import on the shared offload pool. We don't
    # await it — the caller polls /import/{id} for completion.
    async def _run_import() -> None:
        try:
            if _v2_enabled():
                result = await template_import_v2.run_import(
                    import_id,
                    pptx_path,
                    model_config=llm_config.model_dump() if collaboration_mode == "classic" and llm_config else None,
                )
            else:
                result = await aoffload(import_pptx_template, pptx_path, task_id=import_id)
                if result.status == "processing" and collaboration_mode == "classic" and llm_config:
                    await assist_import_review(import_id, llm_config.model_dump(), required=True)
                elif result.status == "processing" and collaboration_mode == "agent":
                    await aoffload(
                        mark_import_ready_for_review,
                        import_id,
                        message="Agent mode workspace is ready.",
                    )
                final_result = get_import_result(import_id) or result
                _import_results[import_id] = final_result
                return
            _import_results[import_id] = result
        except Exception:  # pragma: no cover — caught for surface visibility
            logger.exception("template import failed for %s", import_id)
            if not _v2_enabled():
                final_result = get_import_result(import_id)
                if final_result:
                    _import_results[import_id] = final_result

    asyncio.create_task(_run_import(), name=f"template-import-{import_id}")

    return ImportStartResponse(
        import_id=import_id,
        status="processing",
        collaboration_mode=collaboration_mode,
    )


@router.get("/import/{import_id}", response_model=ImportStatusResponse)
async def get_import_status(import_id: str) -> ImportStatusResponse:
    """Poll the status of a template import task."""
    # v2: read from persisted state.json directly (no in-memory result map).
    if _v2_enabled():
        state = await aoffload(template_import_v2.get_status, import_id)
        if state:
            return ImportStatusResponse(
                import_id=import_id,
                status=str(state.get("status") or "processing"),
                stage=str(state.get("stage") or "uploaded"),
                progress=float(state.get("progress") or 0.0),
                message=str(state.get("message") or ""),
                steps=list(state.get("steps") or []),
                review_required=bool(state.get("review_required")),
                template_id=state.get("template_id"),
                label=state.get("label"),
                slide_count=int(state.get("slide_count") or 0),
                export_mode=str(state.get("export_mode") or ""),
                theme_colors=list(state.get("theme_colors") or []),
                error=state.get("error"),
                collaboration_mode=state.get("collaboration_mode") or "classic",
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Import task '{import_id}' not found.",
        )

    # Check in-memory result first
    result = _import_results.get(import_id)
    if result:
        task = get_import_task(import_id) or {}
        return ImportStatusResponse(
            import_id=import_id,
            status=result.status,
            stage=result.stage,
            progress=result.progress,
            message=result.message,
            steps=result.steps,
            review_required=result.review_required,
            template_id=result.template_id or None,
            label=result.label or None,
            slide_count=result.slide_count,
            export_mode=result.export_mode,
            theme_colors=result.theme_colors,
            error=result.error or None,
            collaboration_mode=task.get("collaboration_mode") or "classic",
        )

    # Check task tracker
    task = get_import_task(import_id)
    if task:
        return ImportStatusResponse(
            import_id=import_id,
            status=task.get("status", "processing"),
            stage=task.get("stage", "uploaded"),
            progress=task.get("progress", 0.0),
            message=task.get("message", ""),
            steps=task.get("steps", []),
            review_required=task.get("review_required", False),
            template_id=task.get("template_id"),
            label=task.get("label"),
            slide_count=task.get("slide_count", 0),
            export_mode=task.get("export_mode", ""),
            theme_colors=task.get("theme_colors", []),
            error=task.get("error"),
            collaboration_mode=task.get("collaboration_mode") or "classic",
        )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Import task '{import_id}' not found.",
        )


class TemplateImportRetryRequest(BaseModel):
    step_id: Literal[
        "uploaded",
        "analyzing",
        "rendering",
        "detecting_assets",
        "llm_review",
        "review",
    ]


@router.post("/import/{import_id}/retry", response_model=ImportStatusResponse)
async def retry_template_import_step(
    import_id: str,
    payload: TemplateImportRetryRequest,
) -> ImportStatusResponse:
    """Re-run a pipeline step (and any later steps) from the v2 facade.

    Only available when ``settings.template_import_v2`` is enabled —
    the v1 importer doesn't expose per-step retry semantics.
    """
    if not _v2_enabled():
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Per-step retry requires the template_import_v2 feature flag.",
        )
    try:
        result = await template_import_v2.retry_import_step(import_id, payload.step_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Import task '{import_id}' not found.",
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None

    # Drop any cached preview — the rerun likely changed the rendered output.
    _preview_cache_drop(import_id)

    return ImportStatusResponse(
        import_id=import_id,
        status=result.status,
        stage=result.stage,
        progress=result.progress,
        message=result.message,
        steps=result.steps,
        review_required=result.review_required,
        template_id=result.template_id or None,
        label=result.label or None,
        slide_count=result.slide_count,
        export_mode=result.export_mode,
        theme_colors=result.theme_colors,
        error=result.error or None,
        collaboration_mode=_collaboration_mode_for_import(import_id),
    )


@router.get("/import/{import_id}/review", response_model=TemplateReviewResponse)
async def get_template_import_review(import_id: str) -> TemplateReviewResponse:
    """Return the review draft for a rendered PPTX import."""
    review = await aoffload(get_import_review, import_id)
    if not review:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Import review '{import_id}' not found.",
        )
    return TemplateReviewResponse(**_review_response_payload(import_id, review))


@router.put("/import/{import_id}/review", response_model=TemplateReviewResponse)
async def update_template_import_review(
    import_id: str,
    draft: TemplateReviewDraftRequest,
) -> TemplateReviewResponse:
    """Save manual page-type and asset review choices."""
    payload = draft.model_dump(exclude_none=True)
    try:
        review = await _save_review_draft(import_id, payload)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Import review '{import_id}' not found.",
        ) from None
    return TemplateReviewResponse(**_review_response_payload(import_id, review))


@router.get("/import/{import_id}/slides/{slide_index}.svg")
async def get_template_import_slide_svg(import_id: str, slide_index: int) -> Response:
    """Return one imported slide SVG preview, addressed outside review.json."""
    path = _slide_svg_path(import_id, slide_index)
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Slide {slide_index} for import '{import_id}' not found.",
        )
    svg = await aoffload(lambda: inline_svg_asset_refs(path, max_bytes=5_000_000))
    return Response(content=svg, media_type="image/svg+xml")


@router.get("/import/{import_id}/assets/{file_name}")
async def get_template_import_asset_preview(import_id: str, file_name: str) -> Response:
    """Return one imported asset preview, addressed outside review.json."""
    path = _asset_preview_path(import_id, file_name)
    if not path.exists() or not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset '{file_name}' for import '{import_id}' not found.",
        )
    data = path.read_bytes()
    media_type = "application/octet-stream"
    suffix = path.suffix.lower()
    if suffix == ".svg":
        media_type = "image/svg+xml"
    elif suffix in {".jpg", ".jpeg"}:
        media_type = "image/jpeg"
    elif suffix == ".png":
        media_type = "image/png"
    elif suffix == ".gif":
        media_type = "image/gif"
    elif suffix == ".webp":
        media_type = "image/webp"
    return Response(content=data, media_type=media_type)


@router.get("/import/{import_id}/files", response_model=TemplateImportFileListResponse)
async def list_template_import_files(
    import_id: str,
    path: str = "",
) -> TemplateImportFileListResponse:
    """Browse files in the Agent's import workspace for @-mentions."""
    root = _template_import_workspace(import_id)
    target = _resolve_import_workspace_path(root, path)
    if not target.exists() or not target.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace path '{path}' not found.",
        )
    items: list[TemplateImportFileItem] = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.name.startswith("."):
            continue
        rel = _workspace_relative_path(root, child)
        is_file = child.is_file()
        is_image = is_file and _is_previewable_image(child)
        items.append(
            TemplateImportFileItem(
                name=child.name,
                path=rel,
                type="directory" if child.is_dir() else "file",
                size=child.stat().st_size if is_file else None,
                image=is_image,
                preview_url=f"/api/templates/import/{import_id}/files/preview?path={quote(rel, safe='')}"
                if is_image
                else None,
            )
        )
    cwd = _workspace_relative_path(root, target) if target != root else ""
    parent_path = None
    if target != root:
        parent = target.parent
        parent_path = _workspace_relative_path(root, parent) if parent != root else ""
    return TemplateImportFileListResponse(cwd=cwd, parent=parent_path, items=items)


@router.get("/import/{import_id}/files/preview", response_model=None)
async def get_template_import_file_preview(import_id: str, path: str):
    """Return a previewable image from the Agent workspace."""
    root = _template_import_workspace(import_id)
    target = _resolve_import_workspace_path(root, path)
    if not target.exists() or not target.is_file() or not _is_previewable_image(target):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Preview '{path}' not found.",
        )
    if target.suffix.lower() == ".svg":
        svg = await aoffload(lambda: inline_svg_asset_refs(target, max_bytes=5_000_000))
        return Response(content=svg, media_type="image/svg+xml")
    media_type = mimetypes.guess_type(target.name)[0]
    return FileResponse(target, media_type=media_type or "application/octet-stream")


# ── Annotation CRUD ───────────────────────────────────────────────────────


class AnnotationBBoxNorm(BaseModel):
    x: float = Field(default=0.0, ge=0.0, le=1.0)
    y: float = Field(default=0.0, ge=0.0, le=1.0)
    width: float = Field(default=0.0, ge=0.0, le=1.0)
    height: float = Field(default=0.0, ge=0.0, le=1.0)


class CreateAnnotationRequest(BaseModel):
    slide_index: int
    bbox_norm: AnnotationBBoxNorm
    note: str
    linked_element_id: str | None = None


class AnnotationResponseItem(BaseModel):
    annotation_id: str
    slide_index: int
    bbox_norm: AnnotationBBoxNorm
    note: str
    linked_element_id: str | None = None
    created_at: float = 0.0
    resolved: bool = False


class CreateAnnotationResponse(BaseModel):
    annotation_id: str
    annotations: list[AnnotationResponseItem]


class DeleteAnnotationResponse(BaseModel):
    deleted: bool
    annotations: list[AnnotationResponseItem]


class UpdateAnnotationRequest(BaseModel):
    bbox_norm: AnnotationBBoxNorm | None = None
    note: str | None = None
    linked_element_id: str | None = None
    resolved: bool | None = None


class UpdateAnnotationResponse(BaseModel):
    annotation: AnnotationResponseItem
    annotations: list[AnnotationResponseItem]


@router.post(
    "/import/{import_id}/annotation",
    response_model=CreateAnnotationResponse,
)
async def create_template_import_annotation(
    import_id: str,
    payload: CreateAnnotationRequest,
) -> CreateAnnotationResponse:
    """Persist a new user-drawn annotation on the review draft."""
    bbox = payload.bbox_norm.model_dump()
    try:
        record = await aoffload(
            template_import_v2.add_annotation,
            import_id,
            payload.slide_index,
            bbox,
            payload.note,
            payload.linked_element_id,
        )
        annotations = await aoffload(template_import_v2.list_annotations, import_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Import review '{import_id}' not found.",
        ) from None
    _preview_cache_drop(import_id)
    return CreateAnnotationResponse(
        annotation_id=str(record["annotation_id"]),
        annotations=[AnnotationResponseItem(**a) for a in annotations],
    )


@router.delete(
    "/import/{import_id}/annotation/{annotation_id}",
    response_model=DeleteAnnotationResponse,
)
async def delete_template_import_annotation(
    import_id: str,
    annotation_id: str,
) -> DeleteAnnotationResponse:
    """Remove an annotation from the review draft."""
    try:
        deleted = await aoffload(template_import_v2.remove_annotation, import_id, annotation_id)
        annotations = await aoffload(template_import_v2.list_annotations, import_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Import review '{import_id}' not found.",
        ) from None
    _preview_cache_drop(import_id)
    return DeleteAnnotationResponse(
        deleted=bool(deleted),
        annotations=[AnnotationResponseItem(**a) for a in annotations],
    )


@router.patch(
    "/import/{import_id}/annotation/{annotation_id}",
    response_model=UpdateAnnotationResponse,
)
async def update_template_import_annotation(
    import_id: str,
    annotation_id: str,
    payload: UpdateAnnotationRequest,
) -> UpdateAnnotationResponse:
    """Patch a persisted annotation note / resolved state / geometry."""
    patch: dict[str, Any] = {}
    if payload.bbox_norm is not None:
        patch["bbox_norm"] = payload.bbox_norm.model_dump()
    if payload.note is not None:
        patch["note"] = payload.note
    if payload.linked_element_id is not None:
        patch["linked_element_id"] = payload.linked_element_id
    if payload.resolved is not None:
        patch["resolved"] = payload.resolved
    try:
        record = await aoffload(
            template_import_v2.update_annotation,
            import_id,
            annotation_id,
            patch,
        )
        annotations = await aoffload(template_import_v2.list_annotations, import_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Import review '{import_id}' not found.",
        ) from None
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Annotation '{annotation_id}' not found.",
        ) from None
    _preview_cache_drop(import_id)
    return UpdateAnnotationResponse(
        annotation=AnnotationResponseItem(**record),
        annotations=[AnnotationResponseItem(**a) for a in annotations],
    )


@router.post("/import/{import_id}/assist", response_model=TemplateReviewResponse)
async def assist_template_import_review(
    import_id: str,
    payload: TemplateImportAssistRequest,
) -> TemplateReviewResponse:
    """Run optional LLM analysis over an existing import review draft."""
    try:
        model_config = payload.llm_config.model_dump() if payload.llm_config else None
        review = await assist_import_review(import_id, model_config, required=True)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Import review '{import_id}' not found.",
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"LLM template analysis failed: {exc}",
        ) from None
    _preview_cache_drop(import_id)
    return TemplateReviewResponse(**_review_response_payload(import_id, review))


@router.post("/import/{import_id}/feedback", response_model=TemplateReviewResponse)
async def optimize_template_import_with_feedback(
    import_id: str,
    payload: TemplateImportFeedbackRequest,
) -> TemplateReviewResponse:
    """Revise an import draft using user feedback and the current LLM model."""
    feedback = payload.feedback.strip()
    if not feedback:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Feedback is required.")
    try:
        if payload.draft is not None:
            await _save_review_draft(import_id, payload.draft.model_dump(exclude_none=True))
        review = await assist_import_review(
            import_id,
            payload.llm_config.model_dump(),
            required=True,
            feedback=feedback,
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Import review '{import_id}' not found.",
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Template feedback optimization failed: {exc}",
        ) from None
    _preview_cache_drop(import_id)
    return TemplateReviewResponse(**_review_response_payload(import_id, review))


@router.post("/import/{import_id}/agent", response_model=TemplateAgentStartResponse)
async def start_template_import_agent(
    import_id: str,
    payload: TemplateAgentStartRequest,
) -> TemplateAgentStartResponse:
    """Start the optional high-autonomy Agent collaboration mode.

    This path is intentionally separate from ``/feedback`` so the original
    single-call LLM collaboration flow remains unchanged.
    """
    feedback = payload.feedback.strip()
    if not feedback:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Feedback is required.")

    # Make sure the review exists and persist the current UI draft first so
    # the agent sees exactly the same state the user is looking at.
    if not await aoffload(get_import_review, import_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Import review '{import_id}' not found.",
        )
    if payload.draft is not None:
        await _save_review_draft(import_id, payload.draft.model_dump(exclude_none=True))

    cfg = TemplateAgentConfig(
        mode=payload.config.mode,
        api_key=payload.config.api_key,
        auth_token=payload.config.auth_token,
        base_url=payload.config.base_url,
        model=payload.config.model,
        custom_model_option=payload.config.custom_model_option,
        load_project_settings=payload.config.load_project_settings,
        max_turns=payload.config.max_turns,
        reply_language=payload.config.reply_language,
    )
    job = await template_agent_manager.start(
        import_id,
        feedback,
        cfg,
        silent=payload.silent,
        planning=payload.planning,
    )
    return TemplateAgentStartResponse(
        agent_job_id=job.id,
        import_id=import_id,
        status=job.status,
    )


@router.get(
    "/import/{import_id}/agent/{agent_job_id}",
    response_model=TemplateAgentStatusResponse,
)
async def get_template_import_agent_status(
    import_id: str,
    agent_job_id: str,
) -> TemplateAgentStatusResponse:
    """Return coarse status for a Template Agent job."""
    job = template_agent_manager.get(agent_job_id)
    if job is None or job.import_id != import_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent job '{agent_job_id}' not found.",
        )
    return _template_agent_status_response(job)


@router.post(
    "/import/{import_id}/agent/{agent_job_id}/cancel",
    response_model=TemplateAgentStatusResponse,
)
async def cancel_template_import_agent(
    import_id: str,
    agent_job_id: str,
) -> TemplateAgentStatusResponse:
    """Cancel a running Template Agent job."""
    job = template_agent_manager.get(agent_job_id)
    if job is None or job.import_id != import_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent job '{agent_job_id}' not found.",
        )
    cancelled = await template_agent_manager.cancel(agent_job_id)
    return _template_agent_status_response(cancelled or job)


@router.websocket("/import/{import_id}/agent/{agent_job_id}/stream")
async def stream_template_import_agent(
    websocket: WebSocket,
    import_id: str,
    agent_job_id: str,
) -> None:
    """Stream Template Agent events to the collaboration panel."""
    await websocket.accept()
    job = template_agent_manager.get(agent_job_id)
    if job is None or job.import_id != import_id:
        await websocket.send_json(
            {
                "type": "error",
                "agent_job_id": agent_job_id,
                "import_id": import_id,
                "stage": "error",
                "status": "error",
                "message": "Agent job not found.",
                "data": {"error": "not_found"},
            }
        )
        await websocket.close(code=1008)
        return

    try:
        since_seq_raw = websocket.query_params.get("since_seq", "0")
        try:
            since_seq = max(0, int(since_seq_raw))
        except (TypeError, ValueError):
            since_seq = 0

        queue = template_agent_manager.subscribe(agent_job_id)
        await websocket.send_json(template_agent_manager.snapshot(job))
        for event in template_agent_manager.events_after(agent_job_id, since_seq):
            await websocket.send_json(event)

        if job.status in {"complete", "error", "cancelled"}:
            await _drain_agent_queue(websocket, queue)
            await websocket.close(code=1000)
            return

        while True:
            try:
                event = await asyncio.wait_for(
                    queue.get(),
                    timeout=max(1, int(settings.ws_heartbeat_seconds)),
                )
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping", "ts": time.time()})
                continue
            await websocket.send_json(event)
            if event.get("type") in {"complete", "error", "cancelled"}:
                await websocket.close(code=1000)
                return
    except WebSocketDisconnect:
        pass
    finally:
        try:
            template_agent_manager.unsubscribe(agent_job_id, queue)
        except UnboundLocalError:
            pass


def _template_agent_status_response(job) -> TemplateAgentStatusResponse:
    return TemplateAgentStatusResponse(
        agent_job_id=job.id,
        import_id=job.import_id,
        status=job.status,
        message=job.message,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


async def _drain_agent_queue(websocket: WebSocket, queue: asyncio.Queue) -> None:
    while not queue.empty():
        try:
            event = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        await websocket.send_json(event)


_AGENT_TEMPLATE_PAGE_FILES: dict[str, str] = {
    "cover": "01_cover.svg",
    "toc": "02_toc.svg",
    "chapter": "02_chapter.svg",
    "content": "03_content.svg",
    "ending": "04_ending.svg",
}


@router.put("/import/{import_id}/agent-template/{page_type}", response_model=TemplatePreviewResponse)
async def update_template_import_agent_template_svg(
    import_id: str,
    page_type: Literal["cover", "toc", "chapter", "content", "ending"],
    payload: TemplateAgentTemplateSvgRequest,
) -> TemplatePreviewResponse:
    """Persist a manually edited Agent-mode template SVG and return fresh preview."""
    svg = (payload.svg or "").strip()
    if not svg.startswith("<svg") or "</svg>" not in svg:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A valid SVG document is required.")
    if "<script" in svg.lower():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Script tags are not allowed in template SVG.")
    review = await aoffload(get_import_review, import_id)
    if not review:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Import review '{import_id}' not found.",
        )
    agent_dir = _import_workspace(import_id) / "agent_template"
    await aensure_dir(agent_dir)
    target = agent_dir / _AGENT_TEMPLATE_PAGE_FILES[page_type]
    target.write_text(svg, encoding="utf-8")
    _preview_cache_drop(import_id)
    try:
        preview = await aoffload(preview_import_template, import_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    return TemplatePreviewResponse(**preview)


@router.post("/import/{import_id}/preview", response_model=TemplatePreviewResponse)
async def preview_template_import_draft(
    import_id: str,
    draft: TemplateReviewDraftRequest | None = None,
) -> TemplatePreviewResponse:
    """Render the current reviewed draft without registering it as a template.

    Concurrent calls for the same ``import_id`` are merged: an
    ``asyncio.Lock`` per import serializes the build, and a small LRU
    keyed by ``(import_id, draft_signature)`` short-circuits identical
    rebuilds (Task 12.5 / Requirement 3.2).
    """
    draft_payload = draft.model_dump(exclude_none=True) if draft is not None else {}
    sig = _preview_signature(draft_payload)
    is_agent_import = _collaboration_mode_for_import(import_id) == "agent"
    lock = _preview_lock_for(import_id)
    async with lock:
        cached = None if is_agent_import else _preview_cache_get(import_id, sig)
        if cached is not None:
            return TemplatePreviewResponse(**cached)
        try:
            if draft is not None:
                await _save_review_draft(import_id, draft_payload)
            preview = await aoffload(preview_import_template, import_id)
        except FileNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Import review '{import_id}' not found.",
            ) from None
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
        if not is_agent_import:
            _preview_cache_put(import_id, sig, preview)
    return TemplatePreviewResponse(**preview)


@router.post("/import/{import_id}/confirm", response_model=ConfirmImportResponse)
async def confirm_template_import(import_id: str) -> ConfirmImportResponse:
    """Register a reviewed import as a user template."""
    try:
        result = await aoffload(confirm_import_template, import_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Import review '{import_id}' not found.",
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None

    _import_results[import_id] = result
    _preview_cache_drop(import_id)
    return ConfirmImportResponse(
        import_id=import_id,
        status=result.status,
        stage=result.stage,
        progress=result.progress,
        message=result.message,
        steps=result.steps,
        review_required=result.review_required,
        template_id=result.template_id or None,
        label=result.label or None,
        slide_count=result.slide_count,
        export_mode=result.export_mode,
        theme_colors=result.theme_colors,
        error=result.error or None,
        collaboration_mode=_collaboration_mode_for_import(import_id),
    )


@router.get("/imported", response_model=list[UserTemplateItem])
async def list_imported_templates() -> list[UserTemplateItem]:
    """List all user-imported templates."""
    templates = list_user_templates()
    return [
        UserTemplateItem(
            template_id=t["template_id"],
            label=t.get("label", t["template_id"]),
            summary=t.get("summary", ""),
            slide_count=t.get("slideCount", 0),
        )
        for t in templates
    ]


@router.get("/{template_id}/preview", response_model=TemplatePreviewResponse)
async def get_template_preview(template_id: str) -> TemplatePreviewResponse:
    """Get preview SVGs and metadata for a template."""
    tmpl = load_template(template_id)
    if tmpl is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template '{template_id}' not found.",
        )

    # Extract theme colors from manifest if available
    theme_colors: list[str] = []
    manifest_path = settings.templates_dir / "layouts" / template_id / "manifest.json"
    if manifest_path.exists():
        import json
        try:
            from backend.runtime import aread_text as _aread_text
            text = await _aread_text(manifest_path, encoding="utf-8")
            manifest = json.loads(text)
            colors = manifest.get("theme", {}).get("colors", {})
            for key in ("dk1", "lt1", "accent1", "accent2"):
                val = colors.get(key)
                if val and val.startswith("#"):
                    theme_colors.append(val)
        except (json.JSONDecodeError, OSError):
            pass

    template_dir = settings.templates_dir / "layouts" / template_id

    def _preview_file(name: str) -> str:
        path = template_dir / name
        if not path.exists():
            return ""
        return inline_svg_asset_refs(path)

    return TemplatePreviewResponse(
        template_id=tmpl.info.template_id,
        label=tmpl.info.label,
        cover_svg=_preview_file("01_cover.svg"),
        toc_svg=_preview_file("02_toc.svg"),
        chapter_svg=_preview_file("02_chapter.svg"),
        content_svg=_preview_file("03_content.svg"),
        ending_svg=_preview_file("04_ending.svg"),
        design_spec=tmpl.design_spec[:40000] if tmpl.design_spec else "",
        theme_colors=theme_colors,
    )


@router.put("/{template_id}", response_model=UserTemplateItem)
async def rename_template(template_id: str, payload: RenameTemplateRequest) -> UserTemplateItem:
    """Rename a user-imported template."""
    if not template_id.startswith("user_"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only user-imported templates can be renamed.",
        )
    try:
        if _v2_enabled():
            renamed = await aoffload(template_import_v2.rename_user_template, template_id, payload.label)
        else:
            renamed = await aoffload(rename_user_template, template_id, payload.label)
    except ValueError as exc:
        # v2 ``persistence.rename_user_template`` raises ValueError for non-user_ ids
        # (defense-in-depth even though we pre-checked the prefix above).
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    if not renamed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template '{template_id}' not found.",
        )
    tmpl = load_template(template_id)
    return UserTemplateItem(
        template_id=template_id,
        label=tmpl.info.label if tmpl else payload.label,
        summary=tmpl.info.summary if tmpl else "",
        slide_count=0,
    )


@router.delete("/{template_id}", response_model=DeleteTemplateResponse)
async def delete_template(template_id: str) -> DeleteTemplateResponse:
    """Delete a user-imported template."""
    if not template_id.startswith("user_"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only user-imported templates (prefix 'user_') can be deleted.",
        )
    try:
        if _v2_enabled():
            deleted = await aoffload(template_import_v2.remove_user_template, template_id)
        else:
            deleted = remove_user_template(template_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template '{template_id}' not found.",
        )
    return DeleteTemplateResponse(template_id=template_id, deleted=True)
