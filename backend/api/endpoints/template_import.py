"""Template import endpoints — upload PPTX and manage user templates."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, status
from pydantic import BaseModel

from backend.config import settings
from backend.generator.template_importer import (
    ImportResult,
    get_import_task,
    import_pptx_template,
    list_user_templates,
    remove_user_template,
)
from backend.generator.template_manager import load_template
from backend.runtime import aensure_dir, aoffload, awrite_bytes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/templates")


# ── Response models ───────────────────────────────────────────────────────────


class ImportStartResponse(BaseModel):
    import_id: str
    status: str = "processing"
    template_id: str | None = None


class ImportStatusResponse(BaseModel):
    import_id: str
    status: str  # processing | complete | error
    template_id: str | None = None
    label: str | None = None
    slide_count: int = 0
    export_mode: str = ""
    theme_colors: list[str] = []
    error: str | None = None


class TemplatePreviewResponse(BaseModel):
    template_id: str
    label: str
    cover_svg: str = ""
    content_svg: str = ""
    theme_colors: list[str] = []


class DeleteTemplateResponse(BaseModel):
    template_id: str
    deleted: bool


class UserTemplateItem(BaseModel):
    template_id: str
    label: str
    summary: str = ""
    slide_count: int = 0


# ── In-memory import results (for preview) ───────────────────────────────────

_import_results: dict[str, ImportResult] = {}


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/upload", response_model=ImportStartResponse)
async def upload_template_pptx(file: UploadFile) -> ImportStartResponse:
    """Upload a PPTX file and start async template import."""
    if not file.filename or not file.filename.lower().endswith(".pptx"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .pptx files are accepted.",
        )

    # Save uploaded file
    content = await file.read()
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds {settings.max_upload_bytes // (1024*1024)}MB limit.",
        )

    import_id = uuid.uuid4().hex[:12]
    upload_dir = settings.workspaces_dir / "template_uploads" / import_id
    await aensure_dir(upload_dir)
    pptx_path = upload_dir / file.filename
    await awrite_bytes(pptx_path, content)

    # Run the long-running import on the shared offload pool. We don't
    # await it — the caller polls /import/{id} for completion.
    async def _run_import() -> None:
        try:
            result = await aoffload(import_pptx_template, pptx_path, task_id=import_id)
            _import_results[import_id] = result
        except Exception:  # pragma: no cover — caught for surface visibility
            logger.exception("template import failed for %s", import_id)

    asyncio.create_task(_run_import(), name=f"template-import-{import_id}")

    return ImportStartResponse(import_id=import_id, status="processing")


@router.get("/import/{import_id}", response_model=ImportStatusResponse)
async def get_import_status(import_id: str) -> ImportStatusResponse:
    """Poll the status of a template import task."""
    # Check in-memory result first
    result = _import_results.get(import_id)
    if result:
        return ImportStatusResponse(
            import_id=import_id,
            status=result.status,
            template_id=result.template_id or None,
            label=result.label or None,
            slide_count=result.slide_count,
            export_mode=result.export_mode,
            theme_colors=result.theme_colors,
            error=result.error or None,
        )

    # Check task tracker
    task = get_import_task(import_id)
    if task:
        return ImportStatusResponse(
            import_id=import_id,
            status=task.get("status", "processing"),
            template_id=task.get("template_id"),
            label=task.get("label"),
            slide_count=task.get("slide_count", 0),
            export_mode=task.get("export_mode", ""),
            error=task.get("error"),
        )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Import task '{import_id}' not found.",
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

    return TemplatePreviewResponse(
        template_id=tmpl.info.template_id,
        label=tmpl.info.label,
        cover_svg=tmpl.cover_svg[:50000] if tmpl.cover_svg else "",  # cap for response size
        content_svg=tmpl.content_svg[:50000] if tmpl.content_svg else "",
        theme_colors=theme_colors,
    )


@router.delete("/{template_id}", response_model=DeleteTemplateResponse)
async def delete_template(template_id: str) -> DeleteTemplateResponse:
    """Delete a user-imported template."""
    if not template_id.startswith("user_"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only user-imported templates (prefix 'user_') can be deleted.",
        )
    deleted = remove_user_template(template_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template '{template_id}' not found.",
        )
    return DeleteTemplateResponse(template_id=template_id, deleted=True)
