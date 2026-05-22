"""Template import endpoints — upload PPTX and manage user templates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import re
import shutil
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.config import settings
from backend.api.schemas import ModelConfig
from backend.llm.registry import create_provider
from backend.llm.types import LLMMessage
from backend.usage.tracker import reset_usage_context, set_usage_context

# DEPRECATED: replaced by template_import.v2 facade when settings.template_import_v2=True.
# v1 helpers are still imported for the legacy code path; the v2 facade
# (``backend.generator.template_import``) is dispatched to when the feature
# flag is on.
from backend.generator.template_importer import (
    ImportResult,
    assist_import_review,
    confirm_direct_import_template,
    confirm_import_template,
    get_import_review,
    get_import_result,
    get_import_task,
    generate_direct_import_design_spec,
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
from backend.generator.pptx_sanitize import sanitize_pptx_bytes, validate_pptx_xml
from backend.generator.template_import import persistence as template_import_persistence
from backend.generator.pptx_scene import (
    SceneError,
    add_scene_urls_to_slide,
    decorate_scene_for_api,
    delete_slide,
    duplicate_slide,
    ensure_scene_cache,
    get_slide_scene,
    inspect_deck_scene,
    media_response_path,
    mime_type_for_media,
    patch_deck_operations,
    patch_slide_scene,
    reorder_slides,
    scene_paths,
    scene_version,
    stage_image_asset,
    slide_edit_base_path,
    slide_render_path,
    slide_scene_path,
)
from backend.generator.template_manager import load_template
from backend.generator.template_agent import (
    TemplateAgentConfig,
    claude_code_environment_status,
    template_agent_manager,
)
from backend.runtime import aensure_dir, aoffload, awrite_bytes, awrite_text


def _v2_enabled() -> bool:
    """Feature flag: route through the v2 facade when on."""
    return bool(getattr(settings, "template_import_v2", False))


def _v2_state(import_id: str) -> dict[str, Any] | None:
    try:
        return template_import_v2.get_status(import_id) or None
    except Exception:
        return None


def _is_v2_import(import_id: str) -> bool:
    state = _v2_state(import_id)
    if not state:
        return False
    if state.get("importer_version") == "template_import.v2":
        return True
    step_ids = {
        str(step.get("id"))
        for step in state.get("steps") or []
        if isinstance(step, dict) and step.get("id")
    }
    v2_steps = {"uploaded", "analyzing", "rendering", "detecting_assets", "llm_review", "review"}
    if {"baseline_preview", "registering"} & step_ids:
        return False
    return v2_steps.issubset(step_ids)


def _get_import_review_any(import_id: str) -> dict[str, Any] | None:
    try:
        review = template_import_v2.get_review(import_id)
        if review:
            return dict(review)
    except Exception:
        pass
    review = get_import_review(import_id)
    return dict(review) if review else None


def _collaboration_mode_for_import(import_id: str) -> Literal["classic", "agent", "direct"]:
    state = _v2_state(import_id) or get_import_task(import_id) or {}
    mode = state.get("collaboration_mode")
    return mode if mode in {"classic", "agent", "direct"} else "direct"


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/templates")

_TEMPLATE_PAGE_TYPES: tuple[str, ...] = ("cover", "toc", "chapter", "content", "ending")

_SVG_BLOCKLIST = ("script", "foreignObject", "iframe", "object", "embed", "link", "meta", "base")
_SVG_BLOCK_RE = re.compile(
    rf"<\s*({'|'.join(_SVG_BLOCKLIST)})\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_SVG_SELF_CLOSING_BLOCK_RE = re.compile(
    rf"<\s*({'|'.join(_SVG_BLOCKLIST)})\b[^>]*/\s*>",
    re.IGNORECASE | re.DOTALL,
)
_SVG_EVENT_ATTR_RE = re.compile(
    r"""\s+on[a-z0-9:_-]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""",
    re.IGNORECASE,
)
_SVG_JS_HREF_RE = re.compile(
    r"""\s+(href|xlink:href)\s*=\s*(?:"\s*javascript:[^"]*"|'\s*javascript:[^']*'|javascript:[^\s>]+)""",
    re.IGNORECASE,
)


def _sanitize_template_svg(svg: str) -> str:
    """Defang active SVG content before persisting user/agent template edits."""
    cleaned = _SVG_BLOCK_RE.sub("", svg)
    cleaned = _SVG_SELF_CLOSING_BLOCK_RE.sub("", cleaned)
    cleaned = _SVG_EVENT_ATTR_RE.sub("", cleaned)
    cleaned = _SVG_JS_HREF_RE.sub(' href="#"', cleaned)
    return cleaned


# ── Response models ───────────────────────────────────────────────────────────


class ImportStartResponse(BaseModel):
    import_id: str
    status: str = "processing"
    template_id: str | None = None
    collaboration_mode: Literal["classic", "agent", "direct"] = "classic"


class ClaudeCodeStatusResponse(BaseModel):
    available: bool
    cli_path: str | None = None
    sdk_available: bool = False
    sdk_error: str | None = None
    message: str = ""


class ImportStatusResponse(BaseModel):
    import_id: str
    status: str  # processing | review_required | complete | error
    stage: str = "uploaded"
    progress: float = 0.0
    message: str = ""
    steps: list[dict[str, Any]] = []
    review_required: bool = False
    template_id: str | None = None
    label: str | None = None
    slide_count: int = 0
    export_mode: str = ""
    theme_colors: list[str] = []
    error: str | None = None
    collaboration_mode: Literal["classic", "agent", "direct"] = "classic"


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


class TemplateizePreviewResponse(TemplatePreviewResponse):
    warnings: list[dict[str, Any]] = []
    placeholders: dict[str, list[dict[str, Any]]] = {}


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
    pptist_version: str | None = None


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


class TemplateDesignSpecRequest(BaseModel):
    llm_config: ModelConfig = Field(alias="model_config")
    feedback: str | None = None


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
    pptist_version: str | None = None
    # When true the backend treats the feedback as an internal prompt and skips
    # appending it to the visible review conversation. Silent runs are only
    # allowed for read-only inspection; templateization must be user-triggered.
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


class SlideScenePatchRequest(BaseModel):
    operations: list[dict[str, Any]] = Field(default_factory=list)
    scene_version: int | None = None


class DeckScenePatchRequest(BaseModel):
    slide_index: int = Field(ge=1)
    operations: list[dict[str, Any]] = Field(default_factory=list)
    scene_version: int | None = None
    base_scene_version: int | None = None
    mode: str = "commit"


class DeckImageUrlRequest(BaseModel):
    image_url: str = Field(min_length=1)
    filename: str | None = None


class SlideReorderRequest(BaseModel):
    slide_indexes: list[int] = Field(min_length=1)


class PptistDeckSaveRequest(BaseModel):
    title: str = ""
    width: float = 1280
    height: float = 720
    theme: dict[str, Any] | None = None
    slides: list[dict[str, Any]] = Field(default_factory=list)
    source: dict[str, Any] | str | None = None
    notes: list[Any] | dict[str, Any] | None = None
    thumbnails: list[dict[str, Any]] = Field(default_factory=list)


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


def _slide_png_path(import_id: str, slide_index: int):
    workspace = _import_workspace(import_id)
    candidates = []
    for png_dir in (
        workspace / "preview_png",
        workspace / "work" / "preview_png",
        workspace / "png",
        workspace / "work" / "png",
    ):
        candidates.extend(
            [
                png_dir / f"slide_{slide_index:02d}.png",
                png_dir / f"slide_{slide_index:03d}.png",
                png_dir / f"slide-{slide_index:03d}.png",
                png_dir / f"slide-{slide_index:02d}.png",
            ]
        )
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def _template_pptist_dir(import_id: str) -> Path:
    return _import_workspace(import_id) / "pptist"


def _template_pptist_deck_path(import_id: str) -> Path:
    return _template_pptist_dir(import_id) / "deck.json"


def _template_pptist_current_pptx(import_id: str) -> Path | None:
    path = _template_pptist_dir(import_id) / "current.pptx"
    return path if path.exists() else None


def _template_pptist_saved_version(import_id: str) -> str | None:
    deck_path = _template_pptist_deck_path(import_id)
    if deck_path.exists():
        try:
            deck = json.loads(deck_path.read_text(encoding="utf-8"))
            if isinstance(deck, dict) and deck.get("updated_at"):
                return str(deck["updated_at"])
        except (json.JSONDecodeError, OSError):
            pass
        return str(int(deck_path.stat().st_mtime_ns))
    current = _template_pptist_current_pptx(import_id)
    if current is not None:
        return str(int(current.stat().st_mtime_ns))
    return None


async def _read_template_pptist_deck(import_id: str) -> dict[str, Any] | None:
    path = _template_pptist_deck_path(import_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


async def _write_template_pptist_deck(import_id: str, deck: dict[str, Any]) -> None:
    await awrite_text(
        _template_pptist_deck_path(import_id),
        json.dumps(deck, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _normalize_template_pptist_deck(payload: PptistDeckSaveRequest) -> dict[str, Any]:
    deck = payload.model_dump()
    deck["updated_at"] = _template_utc_now()
    deck.setdefault("title", "")
    deck.setdefault("width", 1280)
    deck.setdefault("height", 720)
    deck.setdefault("slides", [])
    return deck


def _template_pptist_deck_response(deck: dict[str, Any], *, source: dict[str, Any]) -> dict[str, Any]:
    response = dict(deck)
    response.setdefault("title", "")
    response.setdefault("width", 1280)
    response.setdefault("height", 720)
    response.setdefault("theme", None)
    response.setdefault("slides", [])
    response.setdefault("updated_at", None)
    response["source"] = {**source, **({"deck_source": deck.get("source")} if deck.get("source") is not None else {})}
    return response


def _blank_template_pptist_deck_response(title: str, *, source: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": title,
        "width": 1280,
        "height": 720,
        "theme": None,
        "slides": [],
        "source": source,
        "updated_at": None,
    }


def _template_pptist_source_url(path: Path | None) -> str | None:
    if path is None:
        return None
    return f"/api/download-file?output_path={quote(str(path), safe='')}"


def _template_utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def _template_pptist_fallback_slides(import_id: str) -> list[dict[str, Any]]:
    review = await aoffload(_get_import_review_any, import_id)
    if review:
        payload = _review_response_payload(import_id, review)
        return list(payload.get("slides") or [])
    slides: list[dict[str, Any]] = []
    for index, svg_path in enumerate(
        sorted(
            [
                *(_import_workspace(import_id) / "svg").glob("*.svg"),
                *(_import_workspace(import_id) / "work" / "svg").glob("*.svg"),
            ]
        ),
        start=1,
    ):
        try:
            content = svg_path.read_text(encoding="utf-8")
        except OSError:
            continue
        slides.append(
            {
                "index": index,
                "name": svg_path.stem,
                "source": "svg",
                "preview_svg_url": f"/api/templates/import/{import_id}/slides/{index}.svg",
                "content": content,
            }
        )
    return slides


async def _patch_template_import_state(import_id: str, **updates: Any) -> None:
    state_path = _import_workspace(import_id) / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        state = {}
    state.update(updates)
    state["updated_at"] = time.time()
    await awrite_text(state_path, json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _default_template_page_selections(slide_count: int) -> dict[str, int | None]:
    if slide_count <= 0:
        return {pt: None for pt in _TEMPLATE_PAGE_TYPES}
    selections: dict[str, int | None] = {
        "cover": 1,
        "toc": None,
        "chapter": None,
        "content": 1 if slide_count == 1 else 2,
        "ending": slide_count if slide_count > 1 else None,
    }
    if slide_count >= 3:
        selections["toc"] = 2
    if slide_count >= 4:
        selections["chapter"] = 3
        selections["content"] = 4
    return selections


def _pptx_slide_count_from_package(pptx_path: Path) -> int:
    try:
        import zipfile

        with zipfile.ZipFile(pptx_path, "r") as zf:
            return sum(1 for name in zf.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", name))
    except Exception:
        return 0


async def _sync_template_review_from_pptist_deck(
    import_id: str,
    *,
    pptx_path: Path | None = None,
    slide_count: int | None = None,
) -> int:
    deck = await _read_template_pptist_deck(import_id)
    deck_slides = deck.get("slides") if isinstance(deck, dict) else None
    if slide_count is None or slide_count <= 0:
        slide_count = len(deck_slides) if isinstance(deck_slides, list) else 0
    if (slide_count is None or slide_count <= 0) and pptx_path is not None:
        slide_count = _pptx_slide_count_from_package(pptx_path)
    slide_count = max(int(slide_count or 0), 0)

    review = await aoffload(template_import_persistence.read_review, import_id)
    if not isinstance(review, dict):
        review = {
            "import_id": import_id,
            "template_id": f"user_{import_id}",
            "label": (deck or {}).get("title") or import_id,
            "draft": {},
        }

    label = str((deck or {}).get("title") or review.get("label") or import_id)
    review["import_id"] = import_id
    review.setdefault("template_id", f"user_{import_id}")
    review["label"] = label
    review["status"] = "review_required"
    review["slide_count"] = slide_count
    review["export_mode"] = "pptist-native"
    review["page_types"] = list(_TEMPLATE_PAGE_TYPES)

    draft = review.get("draft") if isinstance(review.get("draft"), dict) else {}
    if _collaboration_mode_for_import(import_id) == "direct":
        selections = (
            {page_type: index + 1 for index, page_type in enumerate(_TEMPLATE_PAGE_TYPES)}
            if slide_count == len(_TEMPLATE_PAGE_TYPES)
            else {}
        )
    else:
        selections = dict(review.get("page_selections") or draft.get("page_selections") or {})
        defaults = _default_template_page_selections(slide_count)
        for page_type in _TEMPLATE_PAGE_TYPES:
            current = selections.get(page_type)
            if current is None and defaults.get(page_type) is not None:
                selections[page_type] = defaults[page_type]
            elif current is not None:
                try:
                    value = int(current)
                except (TypeError, ValueError):
                    value = 0
                selections[page_type] = value if value > 0 else None

    review["page_selections"] = selections
    draft["label"] = label
    draft["page_selections"] = selections
    review["draft"] = draft

    candidates: dict[str, list[int]] = {pt: [] for pt in _TEMPLATE_PAGE_TYPES}
    for page_type, value in selections.items():
        if page_type in candidates and isinstance(value, int) and value > 0:
            candidates[page_type].append(value)
    review["page_type_candidates"] = candidates

    existing_slides = review.get("slides") if isinstance(review.get("slides"), list) else []
    if slide_count and len(existing_slides) != slide_count:
        by_slide = {int(value): page_type for page_type, value in selections.items() if isinstance(value, int) and value > 0}
        review["slides"] = [
            {"index": index, "page_type": by_slide.get(index, "content"), "text_samples": []}
            for index in range(1, slide_count + 1)
        ]
    elif not existing_slides and slide_count:
        review["slides"] = [{"index": index, "page_type": "content", "text_samples": []} for index in range(1, slide_count + 1)]

    await aoffload(template_import_persistence.write_review, import_id, review)
    await _patch_template_import_state(
        import_id,
        slide_count=slide_count,
        export_mode="pptist-native",
        render_status="skipped",
        review_required=True,
        status="review_required",
    )
    return slide_count


async def _refresh_template_import_render(import_id: str, pptx_path: Path) -> dict[str, Any]:
    warnings: list[str] = []
    try:
        from backend.generator.template_import import renderer as renderer_pkg

        workspace = _import_workspace(import_id)
        render_result = await aoffload(renderer_pkg.render, pptx_path, workspace / "svg")
        await aoffload(_mirror_template_render_for_legacy_paths, workspace / "svg", workspace / "work" / "svg")
        await _patch_template_import_state(
            import_id,
            slide_count=len(render_result.svg_files),
            export_mode=render_result.export_mode,
        )
        return {"slide_count": len(render_result.svg_files), "warnings": warnings}
    except Exception as exc:  # pragma: no cover - environment renderer availability varies.
        reason = str(exc)
        no_renderer = "no renderer available" in reason.lower()
        log = logger.info if no_renderer else logger.warning
        log("PPTist template render refresh skipped for %s: %s", import_id, exc)
        slide_count = await _sync_template_review_from_pptist_deck(import_id, pptx_path=pptx_path)
        warnings.append(
            "Server slide snapshot rendering is unavailable; using the saved PPTist deck for templateization."
            if no_renderer
            else f"Template render refresh failed; using the saved PPTist deck for templateization: {exc}"
        )
        return {
            "slide_count": slide_count,
            "warnings": warnings,
            "export_mode": "pptist-native",
            "render_status": "skipped",
        }


def _mirror_template_render_for_legacy_paths(src_dir: Path, dst_dir: Path) -> None:
    if not src_dir.is_dir():
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    for index, src in enumerate(sorted(src_dir.glob("*.svg")), start=1):
        shutil.copy2(src, dst_dir / f"slide_{index:02d}.svg")


def _template_scene_paths(import_id: str):
    return scene_paths(_import_workspace(import_id) / "pptx_scene")


def _template_import_pptx_path(import_id: str) -> Path | None:
    pptist_deck = _template_pptist_current_pptx(import_id)
    if pptist_deck is not None:
        return pptist_deck
    paths = _template_scene_paths(import_id)
    if paths.deck_pptx.exists():
        return paths.deck_pptx
    state = _v2_state(import_id) or get_import_task(import_id) or {}
    source_file = state.get("source_file")
    if isinstance(source_file, str) and source_file:
        source = Path(source_file)
        if source.exists() and source.suffix.lower() == ".pptx":
            return source
    workspace = _import_workspace(import_id)
    candidates = sorted(workspace.glob("*.pptx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


async def _template_scene_file(import_id: str, slide_index: int, kind: str) -> FileResponse:
    source_pptx = _template_import_pptx_path(import_id)
    if source_pptx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PowerPoint deck found for scene editing.")
    paths = _template_scene_paths(import_id)
    await aoffload(get_slide_scene, source_pptx, paths, slide_index)
    path = slide_edit_base_path(paths, slide_index) if kind == "edit_base" else slide_render_path(paths, slide_index)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Slide render not found.")
    return FileResponse(path, media_type="image/png", filename=path.name, headers={"Cache-Control": "no-store"})


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
    if "page_selections" not in draft:
        draft["page_selections"] = payload.get("page_selections") or {}
    if "assets" not in draft:
        draft["assets"] = payload.get("assets") or {}
    if "placeholder_hints" not in draft:
        draft["placeholder_hints"] = payload.get("placeholder_hints") or {}
    if "element_actions" not in draft:
        draft["element_actions"] = payload.get("element_actions") or []
    if "design_spec" not in draft and payload.get("design_spec_md") is not None:
        draft["design_spec"] = payload.get("design_spec_md")
    if "preserve_texts" not in draft:
        draft["preserve_texts"] = payload.get("preserve_texts") or []
    draft["preserve_texts"] = _coerce_preserve_texts(draft.get("preserve_texts"))
    payload["draft"] = draft
    payload.setdefault("page_types", ["cover", "toc", "chapter", "content", "ending"])
    if not payload.get("slides"):
        try:
            state = _v2_state(import_id) or {}
            slide_count = int(payload.get("slide_count") or state.get("slide_count") or 0)
        except Exception:
            slide_count = 0
        selections = payload.get("page_selections") if isinstance(payload.get("page_selections"), dict) else {}
        by_slide = {int(v): str(k) for k, v in selections.items() if isinstance(v, int)}
        payload["slides"] = [
            {
                "index": idx,
                "page_type": by_slide.get(idx, "content"),
                "text_samples": [],
            }
            for idx in range(1, slide_count + 1)
        ]
    slides: list[dict[str, Any]] = []
    source_pptx = _template_import_pptx_path(import_id)
    scene_paths_for_import = _template_scene_paths(import_id) if source_pptx is not None else None
    scene_slide_indexes = _available_template_scene_slide_indexes(source_pptx, scene_paths_for_import) if source_pptx is not None and scene_paths_for_import is not None else set()
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
            if _slide_png_path(import_id, idx).exists():
                slide["preview_image_url"] = f"/api/templates/import/{import_id}/slides/{idx}.png"
            if source_pptx is not None and scene_paths_for_import is not None and idx in scene_slide_indexes:
                add_scene_urls_to_slide(
                    slide,
                    scene_url=f"/api/templates/import/{import_id}/slides/{idx}/scene",
                    render_url=f"/api/templates/import/{import_id}/slides/{idx}/render.png",
                    edit_base_url=f"/api/templates/import/{import_id}/slides/{idx}/edit-base.png",
                    version=scene_version(scene_paths_for_import) or int(source_pptx.stat().st_mtime_ns),
                )
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
    payload["pptist_version"] = _template_pptist_saved_version(import_id)
    return payload


def _available_template_scene_slide_indexes(source_pptx: Path | None, paths) -> set[int]:
    if source_pptx is None or paths is None:
        return set()
    try:
        ensure_scene_cache(source_pptx, paths, render=False)
    except Exception:
        return set()
    indexes: set[int] = set()
    for scene_file in paths.scene_json_dir.glob("slide_*.json"):
        try:
            index = int(scene_file.stem.rsplit("_", 1)[-1])
        except ValueError:
            continue
        if slide_scene_path(paths, index).exists():
            indexes.add(index)
    return indexes


async def _save_review_draft(import_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Save a review patch and keep annotation fields in the shared review file.

    The legacy v1 importer owns most review fields; the v2 facade owns the
    annotation normalizer. Both paths write to the same
    ``workspaces/template_imports/<id>/review.json`` file, so centralising this
    merge prevents feedback / preview calls from silently dropping visual notes.
    """
    is_v2_import = _is_v2_import(import_id)
    if is_v2_import and await aoffload(template_import_v2.get_review, import_id):
        review = await aoffload(template_import_v2.update_review, import_id, payload)
    else:
        review = await aoffload(save_import_review, import_id, payload)
    if "annotations" in payload and not is_v2_import:
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


@router.get("/agent/claude-code/status", response_model=ClaudeCodeStatusResponse)
async def get_template_agent_claude_code_status() -> ClaudeCodeStatusResponse:
    """Check whether the backend can run Claude Code Agent mode."""
    return ClaudeCodeStatusResponse(**claude_code_environment_status())


@router.post("/upload", response_model=ImportStartResponse)
async def upload_template_pptx(
    file: UploadFile,
    model_config_json: str | None = Form(None, alias="model_config"),
    collaboration_mode: Literal["classic", "agent", "direct"] = Form("direct"),
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
    use_v2_import = _v2_enabled() or collaboration_mode in {"direct", "agent"}
    if use_v2_import:
        await aoffload(template_import_v2.initialize_import, import_id, pptx_path)
        await aoffload(template_import_v2.set_import_collaboration_mode, import_id, collaboration_mode)
    else:
        await aoffload(initialize_import_task, import_id, pptx_path)
        await aoffload(set_import_collaboration_mode, import_id, collaboration_mode)

    # Run the long-running import on the shared offload pool. We don't
    # await it — the caller polls /import/{id} for completion.
    async def _run_import() -> None:
        try:
            if use_v2_import:
                result = await template_import_v2.run_import(
                    import_id,
                    pptx_path,
                    model_config=llm_config.model_dump() if collaboration_mode == "classic" and llm_config else None,
                )
            else:
                result = await aoffload(import_pptx_template, pptx_path, task_id=import_id)
                if result.status == "processing" and collaboration_mode == "classic" and llm_config:
                    await assist_import_review(import_id, llm_config.model_dump(), required=True)
                    _preview_cache_drop(import_id)
                elif result.status == "processing" and collaboration_mode == "agent":
                    await aoffload(
                        mark_import_ready_for_review,
                        import_id,
                        message="Agent mode workspace is ready.",
                    )
                elif result.status == "processing" and collaboration_mode == "direct":
                    await aoffload(
                        mark_import_ready_for_review,
                        import_id,
                        message="Direct import workspace is ready.",
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
    # v2 imports are persisted on disk; direct/agent always use this path.
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
            collaboration_mode=state.get("collaboration_mode") or "direct",
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
    if not _v2_enabled() and not _v2_state(import_id):
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
    review = await aoffload(_get_import_review_any, import_id)
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


@router.get("/import/{import_id}/pptist/deck")
async def get_template_import_pptist_deck(import_id: str) -> dict[str, Any]:
    workspace = _template_import_workspace(import_id)
    saved = await _read_template_pptist_deck(import_id)
    editable_template = await aoffload(template_import_v2.editable_template_deck, import_id)
    if editable_template is not None:
        saved_source = saved.get("source") if isinstance(saved, dict) else None
        saved_stage = saved_source.get("stage") if isinstance(saved_source, dict) else None
        saved_version = saved_source.get("agent_template_version") if isinstance(saved_source, dict) else None
        saved_deck_version = (
            saved_source.get("editable_template_deck_version") if isinstance(saved_source, dict) else None
        )
        editable_source = editable_template.get("source") if isinstance(editable_template, dict) else None
        editable_version = editable_source.get("agent_template_version") if isinstance(editable_source, dict) else None
        editable_deck_version = (
            editable_source.get("editable_template_deck_version") if isinstance(editable_source, dict) else None
        )
        agent_template_changed = bool(editable_version and editable_version != saved_version)
        editable_deck_changed = bool(editable_deck_version and editable_deck_version != saved_deck_version)
        if saved_stage != "clean_template_edit" or agent_template_changed or editable_deck_changed:
            if saved is not None:
                backup = _template_pptist_dir(import_id) / "source_deck.json"
                if saved_stage != "clean_template_edit" and not backup.exists():
                    await awrite_text(backup, json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
            await _write_template_pptist_deck(import_id, editable_template)
            saved = editable_template
    if saved is not None:
        return _template_pptist_deck_response(
            saved,
            source={
                "kind": "templateImport",
                "id": import_id,
                "saved_deck": True,
                "source_pptx_url": _template_pptist_source_url(_template_pptist_current_pptx(import_id)),
                "source_pptx_path": str(_template_pptist_current_pptx(import_id)) if _template_pptist_current_pptx(import_id) else None,
            },
        )

    source_pptx = _template_import_pptx_path(import_id)
    fallback_slides = await _template_pptist_fallback_slides(import_id)
    return _blank_template_pptist_deck_response(
        title=workspace.name,
        source={
            "kind": "templateImport",
            "id": import_id,
            "saved_deck": False,
            "source_pptx_url": _template_pptist_source_url(source_pptx),
            "source_pptx_path": str(source_pptx) if source_pptx else None,
            "fallback_slides": fallback_slides,
        },
    )


@router.put("/import/{import_id}/pptist/deck")
async def save_template_import_pptist_deck(import_id: str, payload: PptistDeckSaveRequest) -> dict[str, Any]:
    _template_import_workspace(import_id)
    previous_deck = await _read_template_pptist_deck(import_id)
    deck = _normalize_template_pptist_deck(payload)
    review_for_stage = template_import_persistence.read_review(import_id)
    llm_meta = review_for_stage.get("llm") if isinstance(review_for_stage, dict) and isinstance(review_for_stage.get("llm"), dict) else {}
    if _collaboration_mode_for_import(import_id) == "agent" and llm_meta.get("templateized") is True:
        source = deck.get("source") if isinstance(deck.get("source"), dict) else {}
        deck["source"] = {
            **source,
            "kind": "templateImport",
            "id": import_id,
            "stage": "clean_template_edit",
            "template_page_types": ["cover", "toc", "chapter", "content", "ending"],
            "agent_template_version": source.get("agent_template_version"),
            "editable_template_deck_version": source.get("editable_template_deck_version"),
        }
        if isinstance(previous_deck, dict) and previous_deck.get("design_spec") and not deck.get("design_spec"):
            deck["design_spec"] = previous_deck.get("design_spec")
    await _write_template_pptist_deck(import_id, deck)
    slide_count = len(deck.get("slides") or [])
    state = template_import_persistence.read_state(import_id)
    if state is not None:
        state["slide_count"] = slide_count
        state["updated_at"] = time.time()
        template_import_persistence.write_state(import_id, state)
    review = template_import_persistence.read_review(import_id)
    if review is not None:
        review["slide_count"] = slide_count  # type: ignore[typeddict-item]
        if _collaboration_mode_for_import(import_id) == "direct":
            direct_order = ["cover", "toc", "chapter", "content", "ending"]
            if slide_count == len(direct_order):
                selections = {page_type: index + 1 for index, page_type in enumerate(direct_order)}
                review["page_selections"] = selections  # type: ignore[typeddict-item]
                review["page_type_candidates"] = {  # type: ignore[typeddict-item]
                    page_type: [slide_index]
                    for page_type, slide_index in selections.items()
                }
                draft = review.setdefault("draft", {})
                if isinstance(draft, dict):
                    draft["page_selections"] = selections
            else:
                review["page_selections"] = {}  # type: ignore[typeddict-item]
                review["page_type_candidates"] = {page_type: [] for page_type in ["cover", "toc", "chapter", "content", "ending"]}  # type: ignore[typeddict-item]
        template_import_persistence.write_review(import_id, review)
    current = _template_pptist_current_pptx(import_id)
    return {
        "status": "saved",
        "output_path": str(current) if current else None,
        "slide_count": slide_count,
        "updated_at": deck["updated_at"],
    }


@router.post("/import/{import_id}/pptist/export")
async def export_template_import_pptist_deck(import_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    _template_import_workspace(import_id)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Exported PPTX is empty.")
    data = sanitize_pptx_bytes(data)

    pptist_dir = _template_pptist_dir(import_id)
    current_pptx = pptist_dir / "current.pptx"
    await awrite_bytes(current_pptx, data)
    validation_issues = await aoffload(validate_pptx_xml, current_pptx)
    if validation_issues:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Exported PPTX is invalid after sanitizing: {'; '.join(validation_issues[:3])}",
        )
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    export_path = _import_workspace(import_id) / f"pptist_export_{timestamp}.pptx"
    await awrite_bytes(export_path, data)
    await _patch_template_import_state(import_id, source_file=str(current_pptx))
    render_info = await _refresh_template_import_render(import_id, current_pptx)
    _preview_cache_drop(import_id)
    deck = await _read_template_pptist_deck(import_id)
    return {
        "status": "complete",
        "output_path": str(current_pptx),
        "export_path": str(export_path),
        "slide_count": render_info.get("slide_count") or (len(deck.get("slides") or []) if deck else 0),
        "updated_at": _template_utc_now(),
        "warnings": render_info.get("warnings", []),
    }


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


@router.get("/import/{import_id}/slides/{slide_index}.png")
async def get_template_import_slide_png(import_id: str, slide_index: int) -> FileResponse:
    """Return one imported slide raster preview, when available."""
    path = _slide_png_path(import_id, slide_index)
    if not path.exists() or not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Slide PNG {slide_index} for import '{import_id}' not found.",
        )
    return FileResponse(path, media_type="image/png")


@router.get("/import/{import_id}/slides/{slide_index}/scene")
async def get_template_import_slide_scene(import_id: str, slide_index: int) -> dict[str, Any]:
    source_pptx = _template_import_pptx_path(import_id)
    if source_pptx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PowerPoint deck found for scene editing.")
    paths = _template_scene_paths(import_id)
    try:
        scene = await aoffload(get_slide_scene, source_pptx, paths, slide_index)
    except FileNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Slide scene not found.") from None
    version = scene_version(paths)
    return decorate_scene_for_api(
        scene,
        media_url_prefix=f"/api/templates/import/{import_id}/slides/{slide_index}/media",
        render_url=f"/api/templates/import/{import_id}/slides/{slide_index}/render.png",
        edit_base_url=f"/api/templates/import/{import_id}/slides/{slide_index}/edit-base.png",
        version=version,
    )


@router.get("/import/{import_id}/deck/scene")
async def get_template_import_deck_scene(import_id: str) -> dict[str, Any]:
    source_pptx = _template_import_pptx_path(import_id)
    if source_pptx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PowerPoint deck found for scene editing.")
    paths = _template_scene_paths(import_id)
    await aoffload(ensure_scene_cache, source_pptx, paths)
    version = scene_version(paths)
    scenes = []
    for index in sorted(_available_template_scene_slide_indexes(source_pptx, paths)):
        scene = await aoffload(get_slide_scene, source_pptx, paths, index)
        scenes.append(
            decorate_scene_for_api(
                scene,
                media_url_prefix=f"/api/templates/import/{import_id}/slides/{index}/media",
                render_url=f"/api/templates/import/{import_id}/slides/{index}/render.png",
                edit_base_url=f"/api/templates/import/{import_id}/slides/{index}/edit-base.png",
                version=version,
            )
        )
    return {"source": "template", "id": import_id, "scene_version": version, "slides": scenes}


@router.patch("/import/{import_id}/slides/{slide_index}/scene")
async def patch_template_import_slide_scene(
    import_id: str,
    slide_index: int,
    payload: SlideScenePatchRequest,
) -> dict[str, Any]:
    source_pptx = _template_import_pptx_path(import_id)
    if source_pptx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PowerPoint deck found for scene editing.")
    paths = _template_scene_paths(import_id)
    current_version = scene_version(paths)
    if payload.scene_version and current_version and payload.scene_version != current_version:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Slide scene changed. Refresh before saving.")
    try:
        scene = await aoffload(patch_slide_scene, source_pptx, paths, slide_index, payload.operations)
    except (FileNotFoundError, SceneError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    _preview_cache_drop(import_id)
    version = scene_version(paths)
    return decorate_scene_for_api(
        scene,
        media_url_prefix=f"/api/templates/import/{import_id}/slides/{slide_index}/media",
        render_url=f"/api/templates/import/{import_id}/slides/{slide_index}/render.png",
        edit_base_url=f"/api/templates/import/{import_id}/slides/{slide_index}/edit-base.png",
        version=version,
    )


@router.patch("/import/{import_id}/deck")
async def patch_template_import_deck_scene(
    import_id: str,
    payload: DeckScenePatchRequest,
) -> dict[str, Any]:
    return await patch_template_import_deck_operations(import_id, payload)


@router.patch("/import/{import_id}/deck/operations")
async def patch_template_import_deck_operations(
    import_id: str,
    payload: DeckScenePatchRequest,
) -> dict[str, Any]:
    source_pptx = _template_import_pptx_path(import_id)
    if source_pptx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PowerPoint deck found for scene editing.")
    paths = _template_scene_paths(import_id)
    try:
        scene = await aoffload(
            patch_deck_operations,
            source_pptx,
            paths,
            payload.slide_index,
            payload.operations,
            mode=payload.mode,
        )
    except (FileNotFoundError, SceneError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    if payload.mode != "preview":
        _preview_cache_drop(import_id)
    version = scene_version(paths)
    return decorate_scene_for_api(
        scene,
        media_url_prefix=f"/api/templates/import/{import_id}/slides/{payload.slide_index}/media",
        render_url=f"/api/templates/import/{import_id}/slides/{payload.slide_index}/render.png",
        edit_base_url=f"/api/templates/import/{import_id}/slides/{payload.slide_index}/edit-base.png",
        version=version,
    )


@router.post("/import/{import_id}/deck/check")
async def check_template_import_deck(import_id: str) -> dict[str, Any]:
    source_pptx = _template_import_pptx_path(import_id)
    if source_pptx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PowerPoint deck found for scene editing.")
    paths = _template_scene_paths(import_id)
    try:
        return await aoffload(inspect_deck_scene, source_pptx, paths)
    except (FileNotFoundError, SceneError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None


@router.post("/import/{import_id}/assets/images")
async def upload_template_import_deck_image(import_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    _import_workspace(import_id)
    paths = _template_scene_paths(import_id)
    data = await file.read()
    mime_type = file.content_type or mime_type_for_media(Path(file.filename or "image.png"))
    if not (mime_type or "").startswith("image/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Expected an image upload.")
    try:
        return await aoffload(stage_image_asset, paths, file.filename or "image.png", data, mime_type)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None


@router.post("/import/{import_id}/assets/images/from-url")
async def stage_template_import_deck_image_from_url(import_id: str, payload: DeckImageUrlRequest) -> dict[str, Any]:
    _import_workspace(import_id)
    paths = _template_scene_paths(import_id)
    paths.staged_media_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(payload.filename or "search-image.png").name
    download_path = paths.staged_media_dir / f"download_{safe_name}"
    try:
        from backend.orchestrator.image_search import download_image

        saved = await download_image(payload.image_url, download_path)
        if saved is None or not Path(saved).exists():
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to download the selected image.")
        data = Path(saved).read_bytes()
        mime_type = mime_type_for_media(Path(saved))
        if not mime_type.startswith("image/"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Downloaded file is not an image.")
        return await aoffload(stage_image_asset, paths, Path(saved).name, data, mime_type)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to stage selected image: {exc}") from None
    finally:
        download_path.unlink(missing_ok=True)


@router.post("/import/{import_id}/slides/{slide_index}/duplicate", response_model=TemplateReviewResponse)
async def duplicate_template_import_slide(import_id: str, slide_index: int) -> TemplateReviewResponse:
    source_pptx = _template_import_pptx_path(import_id)
    if source_pptx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PowerPoint deck found for scene editing.")
    paths = _template_scene_paths(import_id)
    try:
        await aoffload(duplicate_slide, source_pptx, paths, slide_index)
    except (FileNotFoundError, SceneError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    _preview_cache_drop(import_id)
    review = await aoffload(get_import_review, import_id)
    if not review:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Import review '{import_id}' not found.")
    return TemplateReviewResponse(**_review_response_payload(import_id, review))


@router.delete("/import/{import_id}/slides/{slide_index}", response_model=TemplateReviewResponse)
async def delete_template_import_slide(import_id: str, slide_index: int) -> TemplateReviewResponse:
    source_pptx = _template_import_pptx_path(import_id)
    if source_pptx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PowerPoint deck found for scene editing.")
    paths = _template_scene_paths(import_id)
    scene_indexes = _available_template_scene_slide_indexes(source_pptx, paths)
    if slide_index not in scene_indexes:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Slide scene not found.")
    if len(scene_indexes) <= 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="At least one slide must remain.")
    try:
        await aoffload(delete_slide, source_pptx, paths, slide_index)
    except (FileNotFoundError, SceneError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    _preview_cache_drop(import_id)
    review = await aoffload(get_import_review, import_id)
    if not review:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Import review '{import_id}' not found.")
    return TemplateReviewResponse(**_review_response_payload(import_id, review))


@router.patch("/import/{import_id}/slides/reorder", response_model=TemplateReviewResponse)
async def reorder_template_import_slides(import_id: str, payload: SlideReorderRequest) -> TemplateReviewResponse:
    source_pptx = _template_import_pptx_path(import_id)
    if source_pptx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PowerPoint deck found for scene editing.")
    paths = _template_scene_paths(import_id)
    try:
        await aoffload(reorder_slides, source_pptx, paths, payload.slide_indexes)
    except (FileNotFoundError, SceneError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    _preview_cache_drop(import_id)
    review = await aoffload(get_import_review, import_id)
    if not review:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Import review '{import_id}' not found.")
    return TemplateReviewResponse(**_review_response_payload(import_id, review))


@router.get("/import/{import_id}/slides/{slide_index}/render.png")
async def get_template_import_slide_render(import_id: str, slide_index: int) -> FileResponse:
    return await _template_scene_file(import_id, slide_index, "render")


@router.get("/import/{import_id}/slides/{slide_index}/edit-base.png")
async def get_template_import_slide_edit_base(import_id: str, slide_index: int) -> FileResponse:
    return await _template_scene_file(import_id, slide_index, "edit_base")


@router.get("/import/{import_id}/slides/{slide_index}/media/{media_name}")
async def get_template_import_slide_media(import_id: str, slide_index: int, media_name: str) -> FileResponse:
    source_pptx = _template_import_pptx_path(import_id)
    if source_pptx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PowerPoint deck found for scene editing.")
    paths = _template_scene_paths(import_id)
    await aoffload(get_slide_scene, source_pptx, paths, slide_index)
    path = media_response_path(paths, media_name)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media not found.")
    return FileResponse(path, media_type=mime_type_for_media(path), filename=path.name)


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


@router.post("/import/{import_id}/direct-design-spec", response_model=TemplateReviewResponse)
async def generate_template_import_direct_design_spec(
    import_id: str,
    payload: TemplateImportAssistRequest,
) -> TemplateReviewResponse:
    """Generate ``design_spec.md`` for Direct import before final registration."""
    if _collaboration_mode_for_import(import_id) != "direct":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Direct design_spec generation is only available for Direct imports.",
        )
    if payload.llm_config is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Direct import requires a valid LLM model configuration.",
        )

    pptist_pptx = _template_pptist_current_pptx(import_id)
    if pptist_pptx is not None:
        await _patch_template_import_state(import_id, source_file=str(pptist_pptx))
        await _refresh_template_import_render(import_id, pptist_pptx)

    model_config = payload.llm_config.model_dump()
    try:
        if not _is_v2_import(import_id):
            review = await generate_direct_import_design_spec(import_id, model_config)
            _preview_cache_drop(import_id)
            return TemplateReviewResponse(**_review_response_payload(import_id, review))

        review = await aoffload(_get_import_review_any, import_id)
        if not review:
            raise FileNotFoundError(import_id)
        slide_count = int(review.get("slide_count") or len(review.get("slides") or []) or 0)
        if slide_count != 5:
            raise ValueError("Direct import requires exactly 5 slides.")
        try:
            preview = await template_import_v2.preview_clean_template(import_id)
        except Exception as preview_exc:
            logger.warning("Could not build v2 direct template preview for design_spec context: %s", preview_exc)
            preview = {
                "template_id": str(review.get("template_id") or import_id),
                "label": str(review.get("label") or import_id),
                "cover_svg": "",
                "toc_svg": "",
                "chapter_svg": "",
                "content_svg": "",
                "ending_svg": "",
                "design_spec": "",
                "theme_colors": list(review.get("theme_colors") or []),
            }
        spec = await _generate_design_spec_for_import(import_id, payload, preview, review)
        review = await _save_review_draft(import_id, {"design_spec": spec})
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
            detail=f"Direct design_spec generation failed: {exc}",
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
    if payload.silent and payload.planning:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Templateization must be started by an explicit user request.",
        )
    if payload.config.mode != "claude_code":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Template Agent mode currently supports Claude Code only.",
        )

    # Make sure the review exists and persist the current UI draft first so
    # the agent sees exactly the same state the user is looking at.
    if not await aoffload(_get_import_review_any, import_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Import review '{import_id}' not found.",
        )
    if _is_v2_import(import_id):
        current_pptist_version = _template_pptist_saved_version(import_id)
        if not current_pptist_version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Save the PPTist deck before starting Template Agent.",
            )
        if payload.pptist_version != current_pptist_version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="PPTist deck changed after this review was loaded. Save or refresh before starting Template Agent.",
            )
    if payload.draft is not None:
        await _save_review_draft(import_id, payload.draft.model_dump(exclude_none=True))

    agent_runtime = claude_code_environment_status()
    if not agent_runtime.get("available"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=agent_runtime.get("message") or "Claude Code is not available.",
        )

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
    svg = _sanitize_template_svg((payload.svg or "").strip())
    if not svg.startswith("<svg") or "</svg>" not in svg:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A valid SVG document is required.")
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
            if _is_v2_import(import_id) and await aoffload(template_import_v2.get_review, import_id):
                preview = await template_import_v2.preview_clean_template(import_id)
            else:
                preview = await aoffload(preview_import_template, import_id)
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
                detail=f"Template preview failed: {exc}",
            ) from None
        if not is_agent_import:
            _preview_cache_put(import_id, sig, preview)
    return TemplatePreviewResponse(**preview)


@router.post("/import/{import_id}/templateize/preview", response_model=TemplateizePreviewResponse)
async def preview_templateized_import(
    import_id: str,
    draft: TemplateReviewDraftRequest | None = None,
) -> TemplateizePreviewResponse:
    """Build a clean templateized preview from the current PPTist/review state."""
    draft_payload = draft.model_dump(exclude_none=True) if draft is not None else {}
    try:
        if draft is not None:
            await _save_review_draft(import_id, draft_payload)
        preview = await template_import_v2.preview_clean_template(import_id)
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
            detail=f"Templateize preview failed: {exc}",
        ) from None
    return TemplateizePreviewResponse(**preview)


@router.post("/import/{import_id}/confirm", response_model=ConfirmImportResponse)
async def confirm_template_import(import_id: str) -> ConfirmImportResponse:
    """Register a reviewed import as a user template."""
    pptist_pptx = _template_pptist_current_pptx(import_id)
    if pptist_pptx is not None:
        await _patch_template_import_state(import_id, source_file=str(pptist_pptx))
        await _refresh_template_import_render(import_id, pptist_pptx)
    try:
        if _is_v2_import(import_id) and await aoffload(template_import_v2.get_review, import_id):
            result = await template_import_v2.confirm_import(import_id)
        else:
            result = await aoffload(confirm_import_template, import_id)
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
            detail=f"Template import failed: {exc}",
        ) from None

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


@router.post("/{template_id}/design-spec", response_model=TemplatePreviewResponse)
async def generate_template_design_spec(
    template_id: str,
    payload: TemplateDesignSpecRequest,
) -> TemplatePreviewResponse:
    """Generate/update design_spec.md for a user template using workbench LLM config."""
    if not template_id.startswith("user_"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only user-imported templates can update design_spec.md.",
        )
    template_dir = (settings.templates_dir / "layouts" / template_id).resolve()
    layouts_root = (settings.templates_dir / "layouts").resolve()
    try:
        template_dir.relative_to(layouts_root)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid template id.") from None
    if not template_dir.is_dir():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Template '{template_id}' not found.")

    spec = await _generate_design_spec_for_template(template_id, template_dir, payload)
    (template_dir / "design_spec.md").write_text(spec, encoding="utf-8")
    return await get_template_preview(template_id)


async def _generate_design_spec_for_template(
    template_id: str,
    template_dir: Path,
    payload: TemplateDesignSpecRequest,
) -> str:
    cfg = payload.llm_config.model_dump()
    provider = create_provider(
        cfg["provider"],
        cfg["api_key"],
        base_url=cfg.get("base_url"),
        deepseek_settings=cfg.get("deepseek_settings"),
        openai_settings=cfg.get("openai_settings"),
    )

    def read_limited(path: Path, limit: int = 18000) -> str:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        text = re.sub(r"data:image/[^\"')\s]+", "data:image/*;base64,<omitted>", text)
        return text[:limit]

    page_files = {
        "cover": "01_cover.svg",
        "toc": "02_toc.svg",
        "chapter": "02_chapter.svg",
        "content": "03_content.svg",
        "ending": "04_ending.svg",
    }
    svgs = {page: read_limited(template_dir / name) for page, name in page_files.items()}
    current_spec = read_limited(template_dir / "design_spec.md", 12000)
    reference_spec = read_limited(settings.templates_dir / "design_spec_reference.md", 36000)

    system = (
        "You are a senior presentation design-system writer. Generate a complete "
        "design_spec.md for a PowerPoint SVG template pack. Follow the provided "
        "reference structure exactly, including sections I through XI. Do not modify SVGs. "
        "Output only Markdown; do not use code fences."
    )
    user = (
        f"Template id: {template_id}\n"
        f"User request: {(payload.feedback or '').strip() or 'Generate a complete design_spec.md.'}\n\n"
        "Reference design_spec.md structure:\n"
        f"{reference_spec or '(reference unavailable)'}\n\n"
        "Current design_spec.md, if any:\n"
        f"{current_spec or '(empty)'}\n\n"
        "Template SVG excerpts by page type:\n"
        f"{json.dumps(svgs, ensure_ascii=False)}\n\n"
        "Write a Markdown design spec covering the reusable imported template: project/template information, "
        "canvas, colors, typography, page-type layout rules, placeholder/content guidance, assets, speaker notes, "
        "and technical constraints."
    )
    response = await provider.chat(
        [LLMMessage.system(system), LLMMessage.user(user)],
        cfg["model"],
        temperature=0.2,
        max_tokens=8192,
    )
    text = (response.content or "").strip()
    text = re.sub(r"^```(?:markdown)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    if not text:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="LLM returned an empty design spec.")
    return text


async def _generate_design_spec_for_import(
    import_id: str,
    payload: TemplateImportAssistRequest,
    preview: dict[str, Any],
    review: dict[str, Any],
) -> str:
    if payload.llm_config is None:
        raise ValueError("A model configuration is required.")
    cfg = payload.llm_config.model_dump()
    provider = create_provider(
        cfg["provider"],
        cfg["api_key"],
        base_url=cfg.get("base_url"),
        deepseek_settings=cfg.get("deepseek_settings"),
        openai_settings=cfg.get("openai_settings"),
    )

    def compact_svg(value: Any, limit: int = 14000) -> str:
        text = str(value or "")
        text = re.sub(r"data:image/[^\"')\s]+", "data:image/*;base64,<omitted>", text)
        return text[:limit]

    reference_path = settings.templates_dir / "design_spec_reference.md"
    try:
        reference_spec = reference_path.read_text(encoding="utf-8")[:36000]
    except OSError:
        reference_spec = ""

    page_files = {
        "cover": "cover_svg",
        "toc": "toc_svg",
        "chapter": "chapter_svg",
        "content": "content_svg",
        "ending": "ending_svg",
    }
    svgs = {page: compact_svg(preview.get(key)) for page, key in page_files.items()}
    slide_count = int(review.get("slide_count") or len(review.get("slides") or []) or 0)
    system = (
        "You write production-quality design_spec.md files for imported presentation templates. "
        "Follow the supplied reference structure exactly, including sections I through XI. "
        "Output Markdown only, without code fences. Do not rewrite or generate SVG."
    )
    user = (
        f"Import id: {import_id}\n"
        f"Import mode: direct\n"
        f"Slide count: {slide_count}\n\n"
        "Reference design_spec.md structure:\n"
        f"{reference_spec or '(reference unavailable)'}\n\n"
        "Direct import contract:\n"
        "- The edited PPT pages are the template source.\n"
        "- The five canonical page roles are cover, TOC, chapter, content, ending.\n"
        "- Produce a design_spec.md compatible with Agent/imported template consumption.\n\n"
        "Template SVG excerpts by page type:\n"
        f"{json.dumps(svgs, ensure_ascii=False)}\n\n"
        "Write a complete Markdown design spec covering visual system, canvas constraints, "
        "colors, typography, page-type layout rules, placeholder/content guidance, assets, "
        "speaker notes, and SVG/PPT compatibility constraints."
    )
    snapshot = set_usage_context(job_id=import_id, stage="template_design_spec", page=None, attempt=1)
    try:
        response = await provider.chat(
            [LLMMessage.system(system), LLMMessage.user(user)],
            cfg["model"],
            temperature=0.2,
            max_tokens=8192,
        )
    finally:
        reset_usage_context(snapshot)
    text = (response.content or "").strip()
    text = re.sub(r"^```(?:markdown)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    if not text:
        raise ValueError("LLM returned an empty design_spec.md.")
    return text


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
