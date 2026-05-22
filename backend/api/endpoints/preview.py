"""Preview endpoint for generated SVG slides."""

from datetime import datetime, timezone
from pathlib import Path
import json
import re
import shutil
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.api.schemas import PreviewResponse, PreviewSlide
from backend.config import settings
from backend.generator.project_manager import get_svg_files
from backend.generator.pptx_sanitize import sanitize_pptx_bytes, validate_pptx_xml
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
from backend.generator.svg_finalize.render_ready import prepare_svg_content_for_render, prepare_svg_file_for_render
from backend.runtime import aoffload, apath_exists, aread_bytes, aread_text, aremove, awrite_bytes, awrite_text
from backend.session.manager import session_manager

router = APIRouter()


class SlideUpdateRequest(BaseModel):
    content: str = Field(min_length=20)
    document: dict[str, Any] | None = None
    notes: str | None = None


class SlideCreateRequest(BaseModel):
    content: str | None = None
    document: dict[str, Any] | None = None
    notes: str | None = None


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


@router.get("/critic/{job_id}")
async def get_critic_history(job_id: str) -> dict:
    """Return persisted critic events from critic_history.json."""
    import json

    job = session_manager.get_job(job_id)
    if job is None or not job.project_dir:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    critic_path = Path(job.project_dir) / "critic_history.json"
    if not await apath_exists(critic_path):
        return {"events": []}

    try:
        text = await aread_text(critic_path, encoding="utf-8")
        data = json.loads(text)
    except (json.JSONDecodeError, OSError):
        return {"events": []}

    # Merge generation and refine events
    events = data.get("generation_events") or []
    refine_events = data.get("refine_events") or []
    if refine_events:
        events = refine_events  # Prefer refine events if present

    return {"events": events}


@router.get("/critic-archive/{job_id}/{filename}")
async def get_critic_archive_svg(job_id: str, filename: str) -> Response:
    """Serve an archived critic SVG snapshot or visual QA render."""
    job = session_manager.get_job(job_id)
    if job is None or not job.project_dir:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    # Sanitize filename to prevent path traversal
    safe_name = Path(filename).name
    if safe_name != filename or ".." in filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filename.")

    archive_path = Path(job.project_dir) / "svg_archive" / "repair" / safe_name
    if not await apath_exists(archive_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archive not found.")

    suffix = archive_path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        media_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }[suffix]
        content = await aread_bytes(archive_path)
        return Response(content=content, media_type=media_type)

    content = await aread_text(archive_path, encoding="utf-8")
    try:
        content = await aoffload(
            prepare_svg_content_for_render,
            content,
            Path(job.project_dir) / "svg_output",
        )
    except Exception:
        pass
    return Response(content=content, media_type="image/svg+xml")


@router.get("/preview/{job_id}", response_model=PreviewResponse)
async def get_preview(job_id: str) -> PreviewResponse:
    job = session_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    normalized_status = "complete" if job.output_path and Path(job.output_path).exists() else job.status
    return await _build_preview_response(job_id, Path(job.project_dir) if job.project_dir else None, job.output_path, normalized_status)


@router.get("/pptist/preview/{job_id}/deck")
async def get_pptist_preview_deck(job_id: str) -> dict[str, Any]:
    job = session_manager.get_job(job_id)
    project_dir = _viewable_project_dir_for_id(job_id, job)
    saved = await _read_pptist_deck(project_dir)
    if saved is not None:
        return _pptist_deck_response(
            saved,
            source={
                "kind": "preview",
                "id": job_id,
                "saved_deck": True,
                "source_pptx_url": _pptist_source_url(_pptist_current_pptx(project_dir)),
                "source_pptx_path": str(_pptist_current_pptx(project_dir)) if _pptist_current_pptx(project_dir) else None,
            },
        )

    output_path = job.output_path if job else str(_find_latest_output(project_dir) or "")
    source_pptx = _pptist_current_pptx(project_dir) or _scene_source_pptx(project_dir, output_path, _project_scene_paths(project_dir))
    preview = await _build_preview_response(
        job_id,
        project_dir,
        output_path or (str(source_pptx) if source_pptx else None),
        "complete" if source_pptx or output_path else "unknown",
    )
    return _blank_pptist_deck_response(
        title=project_dir.name,
        source={
            "kind": "preview",
            "id": job_id,
            "saved_deck": False,
            "source_pptx_url": _pptist_source_url(source_pptx),
            "source_pptx_path": str(source_pptx) if source_pptx else None,
            "fallback_slides": [_model_dump(slide) for slide in preview.slides],
        },
    )


@router.put("/pptist/preview/{job_id}/deck")
async def save_pptist_preview_deck(job_id: str, payload: PptistDeckSaveRequest) -> dict[str, Any]:
    job = session_manager.get_job(job_id)
    project_dir = _preview_project_dir_for_id(job_id, job)
    deck = _normalize_pptist_deck(payload)
    await _write_pptist_deck(project_dir, deck)
    return {
        "status": "saved",
        "output_path": str(_pptist_current_pptx(project_dir)) if _pptist_current_pptx(project_dir) else job.output_path if job else None,
        "slide_count": len(deck.get("slides") or []),
        "updated_at": deck["updated_at"],
    }


@router.post("/pptist/preview/{job_id}/export")
async def export_pptist_preview_deck(job_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    job = session_manager.get_job(job_id)
    project_dir = _preview_project_dir_for_id(job_id, job)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Exported PPTX is empty.")
    data = sanitize_pptx_bytes(data)
    pptist_dir = _pptist_dir(project_dir)
    current_pptx = pptist_dir / "current.pptx"
    await awrite_bytes(current_pptx, data)
    validation_issues = await aoffload(validate_pptx_xml, current_pptx)
    if validation_issues:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Exported PPTX is invalid after sanitizing: {'; '.join(validation_issues[:3])}",
        )
    exports_dir = project_dir / "exports"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    export_path = exports_dir / f"presentation_pptist_{timestamp}.pptx"
    await awrite_bytes(export_path, data)
    render_info = await _refresh_preview_svg_render(project_dir, current_pptx)
    try:
        await aoffload(ensure_scene_cache, current_pptx, _project_scene_paths(project_dir), force=True, render=False)
    except Exception as exc:  # pragma: no cover - invalid uploads or parser gaps should not block saving.
        render_info.setdefault("warnings", []).append(f"PPTist scene cache refresh skipped: {exc}")
    deck = await _read_pptist_deck(project_dir)
    slide_count = len(deck.get("slides") or []) if deck else 0
    if job is not None:
        session_manager.update_job(
            job_id,
            status="complete",
            message="Presentation saved from PPTist",
            output_path=str(current_pptx),
            error=None,
        )
    return {
        "status": "complete",
        "output_path": str(current_pptx),
        "export_path": str(export_path),
        "slide_count": render_info.get("slide_count") or slide_count,
        "updated_at": _utc_now(),
        "warnings": render_info.get("warnings", []),
    }


@router.put("/preview/{job_id}/slides/{slide_index}", response_model=PreviewSlide)
async def update_preview_slide(job_id: str, slide_index: int, payload: SlideUpdateRequest) -> PreviewSlide:
    job = session_manager.get_job(job_id)
    project_dir = _editable_project_dir(job)
    slide_files, source = _editable_slide_files(project_dir)
    if slide_index < 1 or slide_index > len(slide_files):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Slide not found.")

    content = _validated_svg(payload.content)
    svg_path = slide_files[slide_index - 1]
    svg_path.write_text(content, encoding="utf-8")
    document = _normalize_slide_document(payload.document, content)
    if document:
        _write_slide_document(project_dir, svg_path, document)
    notes = payload.notes if payload.notes is not None else _notes_from_document(document)
    if notes is not None:
        _write_slide_notes(project_dir, svg_path, notes)
    return PreviewSlide(index=slide_index, name=svg_path.stem, source=source, content=content, notes=notes or "", document=document)


@router.post("/preview/{job_id}/slides", response_model=PreviewSlide)
async def create_preview_slide(job_id: str, payload: SlideCreateRequest) -> PreviewSlide:
    job = session_manager.get_job(job_id)
    project_dir = _editable_project_dir(job)
    slide_files, source = _editable_slide_files(project_dir)
    target_dir = project_dir / f"svg_{source}"
    target_dir.mkdir(parents=True, exist_ok=True)
    next_index = len(slide_files) + 1
    svg_path = target_dir / f"{next_index:02d}_custom.svg"
    while svg_path.exists():
        next_index += 1
        svg_path = target_dir / f"{next_index:02d}_custom.svg"
    content = _validated_svg(payload.content or _blank_slide_svg())
    svg_path.write_text(content, encoding="utf-8")
    document = _normalize_slide_document(payload.document, content) or _blank_slide_document(content)
    if document:
        _write_slide_document(project_dir, svg_path, document)
    notes = payload.notes if payload.notes is not None else _notes_from_document(document)
    if notes is not None:
        _write_slide_notes(project_dir, svg_path, notes)
    return PreviewSlide(index=next_index, name=svg_path.stem, source=source, content=content, notes=notes or "", document=document)


@router.delete("/preview/{job_id}/slides/{slide_index}", response_model=PreviewResponse)
async def delete_preview_slide(job_id: str, slide_index: int) -> PreviewResponse:
    job = session_manager.get_job(job_id)
    project_dir = _editable_project_dir(job)
    paths = _project_scene_paths(project_dir)
    source_pptx = _scene_source_pptx(project_dir, job.output_path if job else None, paths)
    scene_indexes = _available_scene_slide_indexes(source_pptx, paths) if source_pptx is not None else set()
    slide_files, _source = _editable_slide_files(project_dir)
    if source_pptx is not None and slide_index in scene_indexes:
        if len(scene_indexes) <= 1:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="At least one slide must remain.")
        try:
            await aoffload(delete_slide, source_pptx, paths, slide_index)
        except (FileNotFoundError, SceneError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
        if job is not None:
            session_manager.update_job(job_id, status="complete", output_path=str(paths.deck_pptx), error=None)
        if 1 <= slide_index <= len(slide_files):
            svg_path = slide_files[slide_index - 1]
            _delete_svg_slide_assets(project_dir, svg_path)
        return await _build_preview_response(job_id, project_dir, str(paths.deck_pptx), "complete")

    if len(slide_files) <= 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="At least one slide must remain.")
    if slide_index < 1 or slide_index > len(slide_files):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Slide not found.")

    svg_path = slide_files[slide_index - 1]
    try:
        _delete_svg_slide_assets(project_dir, svg_path)
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to delete slide: {exc}") from exc
    normalized_status = "complete" if job.output_path and Path(job.output_path).exists() else job.status
    return await _build_preview_response(job_id, project_dir, job.output_path, normalized_status)


@router.get("/preview/{job_id}/slides/{slide_index}/scene")
async def get_preview_slide_scene(job_id: str, slide_index: int) -> dict[str, Any]:
    job = session_manager.get_job(job_id)
    project_dir = _viewable_project_dir_for_id(job_id, job)
    paths = _project_scene_paths(project_dir)
    source_pptx = _scene_source_pptx(project_dir, job.output_path if job else None, paths)
    if source_pptx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PowerPoint deck found for scene editing.")
    try:
        scene = await aoffload(get_slide_scene, source_pptx, paths, slide_index)
    except FileNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Slide scene not found.") from None
    version = scene_version(paths)
    return decorate_scene_for_api(
        scene,
        media_url_prefix=f"/api/preview/{job_id}/slides/{slide_index}/media",
        render_url=f"/api/preview/{job_id}/slides/{slide_index}/render.png",
        edit_base_url=f"/api/preview/{job_id}/slides/{slide_index}/edit-base.png",
        version=version,
    )


@router.get("/preview/{job_id}/deck/scene")
async def get_preview_deck_scene(job_id: str) -> dict[str, Any]:
    job = session_manager.get_job(job_id)
    project_dir = _viewable_project_dir_for_id(job_id, job)
    paths = _project_scene_paths(project_dir)
    source_pptx = _scene_source_pptx(project_dir, job.output_path if job else None, paths)
    if source_pptx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PowerPoint deck found for scene editing.")
    await aoffload(ensure_scene_cache, source_pptx, paths)
    version = scene_version(paths)
    scenes = []
    for index in sorted(_available_scene_slide_indexes(source_pptx, paths)):
        scene = await aoffload(get_slide_scene, source_pptx, paths, index)
        scenes.append(
            decorate_scene_for_api(
                scene,
                media_url_prefix=f"/api/preview/{job_id}/slides/{index}/media",
                render_url=f"/api/preview/{job_id}/slides/{index}/render.png",
                edit_base_url=f"/api/preview/{job_id}/slides/{index}/edit-base.png",
                version=version,
            )
        )
    return {"source": "preview", "id": job_id, "scene_version": version, "slides": scenes}


@router.patch("/preview/{job_id}/slides/{slide_index}/scene")
async def patch_preview_slide_scene(job_id: str, slide_index: int, payload: SlideScenePatchRequest) -> dict[str, Any]:
    job = session_manager.get_job(job_id)
    project_dir = _preview_project_dir_for_id(job_id, job)
    paths = _project_scene_paths(project_dir)
    source_pptx = _scene_source_pptx(project_dir, job.output_path if job else None, paths)
    if source_pptx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PowerPoint deck found for scene editing.")
    current_version = scene_version(paths)
    if payload.scene_version and current_version and payload.scene_version != current_version:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Slide scene changed. Refresh before saving.")
    try:
        scene = await aoffload(patch_slide_scene, source_pptx, paths, slide_index, payload.operations)
    except (FileNotFoundError, SceneError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    if job is not None:
        session_manager.update_job(job_id, status="complete", output_path=str(paths.deck_pptx), error=None)
    version = scene_version(paths)
    return decorate_scene_for_api(
        scene,
        media_url_prefix=f"/api/preview/{job_id}/slides/{slide_index}/media",
        render_url=f"/api/preview/{job_id}/slides/{slide_index}/render.png",
        edit_base_url=f"/api/preview/{job_id}/slides/{slide_index}/edit-base.png",
        version=version,
    )


@router.patch("/preview/{job_id}/deck")
async def patch_preview_deck_scene(job_id: str, payload: DeckScenePatchRequest) -> dict[str, Any]:
    return await patch_preview_deck_operations(job_id, payload)


@router.patch("/preview/{job_id}/deck/operations")
async def patch_preview_deck_operations(job_id: str, payload: DeckScenePatchRequest) -> dict[str, Any]:
    job = session_manager.get_job(job_id)
    project_dir = _preview_project_dir_for_id(job_id, job)
    paths = _project_scene_paths(project_dir)
    source_pptx = _scene_source_pptx(project_dir, job.output_path if job else None, paths)
    if source_pptx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PowerPoint deck found for scene editing.")
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
    if job is not None and payload.mode != "preview":
        session_manager.update_job(job_id, status="complete", output_path=str(paths.deck_pptx), error=None)
    version = scene_version(paths)
    return decorate_scene_for_api(
        scene,
        media_url_prefix=f"/api/preview/{job_id}/slides/{payload.slide_index}/media",
        render_url=f"/api/preview/{job_id}/slides/{payload.slide_index}/render.png",
        edit_base_url=f"/api/preview/{job_id}/slides/{payload.slide_index}/edit-base.png",
        version=version,
    )


@router.post("/preview/{job_id}/deck/check")
async def check_preview_deck(job_id: str) -> dict[str, Any]:
    job = session_manager.get_job(job_id)
    project_dir = _preview_project_dir_for_id(job_id, job)
    paths = _project_scene_paths(project_dir)
    source_pptx = _scene_source_pptx(project_dir, job.output_path if job else None, paths)
    if source_pptx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PowerPoint deck found for scene editing.")
    try:
        return await aoffload(inspect_deck_scene, source_pptx, paths)
    except (FileNotFoundError, SceneError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None


@router.post("/preview/{job_id}/assets/images")
async def upload_preview_deck_image(job_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    job = session_manager.get_job(job_id)
    project_dir = _preview_project_dir_for_id(job_id, job)
    paths = _project_scene_paths(project_dir)
    data = await file.read()
    mime_type = file.content_type or mime_type_for_media(Path(file.filename or "image.png"))
    if not (mime_type or "").startswith("image/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Expected an image upload.")
    try:
        return await aoffload(stage_image_asset, paths, file.filename or "image.png", data, mime_type)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None


@router.post("/preview/{job_id}/assets/images/from-url")
async def stage_preview_deck_image_from_url(job_id: str, payload: DeckImageUrlRequest) -> dict[str, Any]:
    job = session_manager.get_job(job_id)
    project_dir = _preview_project_dir_for_id(job_id, job)
    paths = _project_scene_paths(project_dir)
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


@router.post("/preview/{job_id}/slides/{slide_index}/duplicate", response_model=PreviewResponse)
async def duplicate_preview_slide(job_id: str, slide_index: int) -> PreviewResponse:
    job = session_manager.get_job(job_id)
    project_dir = _preview_project_dir_for_id(job_id, job)
    paths = _project_scene_paths(project_dir)
    source_pptx = _scene_source_pptx(project_dir, job.output_path if job else None, paths)
    if source_pptx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PowerPoint deck found for scene editing.")
    try:
        await aoffload(duplicate_slide, source_pptx, paths, slide_index)
    except (FileNotFoundError, SceneError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    if job is not None:
        session_manager.update_job(job_id, status="complete", output_path=str(paths.deck_pptx), error=None)
    return await _build_preview_response(job_id, project_dir, str(paths.deck_pptx), "complete")


@router.patch("/preview/{job_id}/slides/reorder", response_model=PreviewResponse)
async def reorder_preview_slides(job_id: str, payload: SlideReorderRequest) -> PreviewResponse:
    job = session_manager.get_job(job_id)
    project_dir = _preview_project_dir_for_id(job_id, job)
    paths = _project_scene_paths(project_dir)
    source_pptx = _scene_source_pptx(project_dir, job.output_path if job else None, paths)
    if source_pptx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PowerPoint deck found for scene editing.")
    try:
        await aoffload(reorder_slides, source_pptx, paths, payload.slide_indexes)
    except (FileNotFoundError, SceneError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    if job is not None:
        session_manager.update_job(job_id, status="complete", output_path=str(paths.deck_pptx), error=None)
    return await _build_preview_response(job_id, project_dir, str(paths.deck_pptx), "complete")


@router.get("/preview/{job_id}/slides/{slide_index}/render.png")
async def get_preview_slide_render(job_id: str, slide_index: int) -> FileResponse:
    return await _preview_scene_file(job_id, slide_index, "render")


@router.get("/preview/{job_id}/slides/{slide_index}/edit-base.png")
async def get_preview_slide_edit_base(job_id: str, slide_index: int) -> FileResponse:
    return await _preview_scene_file(job_id, slide_index, "edit_base")


@router.get("/preview/{job_id}/slides/{slide_index}/media/{media_name}")
async def get_preview_slide_media(job_id: str, slide_index: int, media_name: str) -> FileResponse:
    job = session_manager.get_job(job_id)
    project_dir = _viewable_project_dir_for_id(job_id, job)
    paths = _project_scene_paths(project_dir)
    source_pptx = _scene_source_pptx(project_dir, job.output_path if job else None, paths)
    if source_pptx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PowerPoint deck found for scene editing.")
    await aoffload(get_slide_scene, source_pptx, paths, slide_index)
    path = media_response_path(paths, media_name)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media not found.")
    return FileResponse(path, media_type=mime_type_for_media(path), filename=path.name)


@router.get("/preview-project", response_model=PreviewResponse)
async def get_project_preview(project_dir: str, last_slide_only: bool = False) -> PreviewResponse:
    resolved_project_dir = _resolve_workspace_path(project_dir)
    if resolved_project_dir is None or not resolved_project_dir.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

    output_path = _find_latest_output(resolved_project_dir)
    return await _build_preview_response(
        job_id=resolved_project_dir.name,
        project_dir=resolved_project_dir,
        output_path=str(output_path) if output_path else None,
        status_value="complete" if output_path else "unknown",
        last_slide_only=last_slide_only,
    )


async def _build_preview_response(
    job_id: str,
    project_dir: Path | None,
    output_path: str | None,
    status_value: str,
    *,
    last_slide_only: bool = False,
) -> PreviewResponse:
    slides: list[PreviewSlide] = []
    if project_dir and project_dir.exists():
        final_files = get_svg_files(project_dir, "final")
        output_files = get_svg_files(project_dir, "output")
        slide_files = final_files or output_files
        if last_slide_only and slide_files:
            slide_files = slide_files[-1:]
        source = "final" if final_files else "output"
        scene_source = _scene_source_pptx(project_dir, output_path, _project_scene_paths(project_dir))
        scene_paths_for_project = _project_scene_paths(project_dir) if scene_source is not None else None
        scene_slide_indexes = _available_scene_slide_indexes(scene_source, scene_paths_for_project) if scene_source is not None and scene_paths_for_project is not None else set()
        emitted_slide_indexes: set[int] = set()
        for fallback_index, svg_path in enumerate(slide_files, start=1):
            index = _slide_index_from_svg_path(svg_path, fallback_index)
            emitted_slide_indexes.add(index)
            # ``prepare_svg_file_for_render`` rewrites href base64 inlines etc.
            # — synchronous CPU work; offload it.
            try:
                prepared_path = await aoffload(prepare_svg_file_for_render, svg_path)
            except FileNotFoundError:
                continue
            try:
                content = await aread_text(prepared_path, encoding="utf-8")
            finally:
                await aremove(prepared_path, missing_ok=True)
            slide_payload: dict[str, Any] = dict(
                index=index,
                name=svg_path.stem,
                source=source,
                content=content,
                notes=_read_slide_notes(project_dir, svg_path),
                document=_read_slide_document(project_dir, svg_path),
            )
            if scene_source is not None and scene_paths_for_project is not None and index in scene_slide_indexes:
                add_scene_urls_to_slide(
                    slide_payload,
                    scene_url=f"/api/preview/{job_id}/slides/{index}/scene",
                    render_url=f"/api/preview/{job_id}/slides/{index}/render.png",
                    edit_base_url=f"/api/preview/{job_id}/slides/{index}/edit-base.png",
                    version=scene_version(scene_paths_for_project) or _safe_mtime_version(scene_source),
                )
            slides.append(PreviewSlide(**slide_payload))
        if scene_source is not None and scene_paths_for_project is not None:
            for index in sorted(scene_slide_indexes - emitted_slide_indexes):
                slide_payload = dict(
                    index=index,
                    name=f"slide_{index:02d}",
                    source="pptx_scene",
                    content=_blank_slide_svg(),
                    notes="",
                    document=None,
                )
                add_scene_urls_to_slide(
                    slide_payload,
                    scene_url=f"/api/preview/{job_id}/slides/{index}/scene",
                    render_url=f"/api/preview/{job_id}/slides/{index}/render.png",
                    edit_base_url=f"/api/preview/{job_id}/slides/{index}/edit-base.png",
                    version=scene_version(scene_paths_for_project) or _safe_mtime_version(scene_source),
                )
                slides.append(PreviewSlide(**slide_payload))
            slides.sort(key=lambda item: item.index)

    if not slides:
        slides = _slides_from_retained_events(job_id)

    return PreviewResponse(
        job_id=job_id,
        project_dir=str(project_dir) if project_dir else None,
        slides=slides,
        output_path=output_path,
        status=status_value,
    )


def _pptist_dir(project_dir: Path) -> Path:
    return project_dir / "pptist"


def _pptist_deck_path(project_dir: Path) -> Path:
    return _pptist_dir(project_dir) / "deck.json"


def _pptist_current_pptx(project_dir: Path | None) -> Path | None:
    if project_dir is None:
        return None
    path = _pptist_dir(project_dir) / "current.pptx"
    return path if path.exists() else None


async def _read_pptist_deck(project_dir: Path) -> dict[str, Any] | None:
    path = _pptist_deck_path(project_dir)
    if not await apath_exists(path):
        return None
    try:
        data = json.loads(await aread_text(path, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


async def _write_pptist_deck(project_dir: Path, deck: dict[str, Any]) -> None:
    await awrite_text(
        _pptist_deck_path(project_dir),
        json.dumps(deck, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def _refresh_preview_svg_render(project_dir: Path, pptx_path: Path) -> dict[str, Any]:
    warnings: list[str] = []
    tmp_dir = _pptist_dir(project_dir) / "render_svg_tmp"
    final_dir = project_dir / "svg_final"
    try:
        from backend.generator.template_import import renderer as renderer_pkg

        if tmp_dir.exists():
            await aoffload(shutil.rmtree, tmp_dir)
        render_result = await aoffload(renderer_pkg.render, pptx_path, tmp_dir)
        if final_dir.exists():
            await aoffload(shutil.rmtree, final_dir)
        await aoffload(shutil.copytree, tmp_dir, final_dir)
        return {"slide_count": len(render_result.svg_files), "warnings": warnings}
    except Exception as exc:  # pragma: no cover - renderer availability varies by host.
        reason = str(exc)
        warnings.append(f"PPTist render refresh failed; saved PPTX is still available: {reason}")
        return {"slide_count": 0, "warnings": warnings, "render_status": "skipped"}
    finally:
        if tmp_dir.exists():
            await aoffload(shutil.rmtree, tmp_dir, True)


def _normalize_pptist_deck(payload: PptistDeckSaveRequest) -> dict[str, Any]:
    deck = payload.model_dump()
    deck["updated_at"] = _utc_now()
    deck.setdefault("title", "")
    deck.setdefault("width", 1280)
    deck.setdefault("height", 720)
    deck.setdefault("slides", [])
    return deck


def _pptist_deck_response(deck: dict[str, Any], *, source: dict[str, Any]) -> dict[str, Any]:
    response = dict(deck)
    response.setdefault("title", "")
    response.setdefault("width", 1280)
    response.setdefault("height", 720)
    response.setdefault("theme", None)
    response.setdefault("slides", [])
    response.setdefault("updated_at", None)
    response["source"] = {**source, **({"deck_source": deck.get("source")} if deck.get("source") is not None else {})}
    return response


def _blank_pptist_deck_response(title: str, *, source: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": title,
        "width": 1280,
        "height": 720,
        "theme": None,
        "slides": [],
        "source": source,
        "updated_at": None,
    }


def _pptist_source_url(path: Path | None) -> str | None:
    if path is None:
        return None
    return f"/api/download-file?output_path={quote(str(path), safe='')}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _model_dump(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    if hasattr(model, "dict"):
        return model.dict()
    return dict(model)


def _slide_index_from_svg_path(svg_path: Path, fallback_index: int) -> int:
    match = re.match(r"^0*(\d+)(?:_|$)", svg_path.stem)
    if not match:
        return fallback_index
    try:
        return int(match.group(1))
    except ValueError:
        return fallback_index


def _delete_svg_slide_assets(project_dir: Path, svg_path: Path) -> None:
    document_path = _slide_document_path(project_dir, svg_path)
    notes_path = _slide_notes_path(project_dir, svg_path)
    svg_path.unlink(missing_ok=True)
    document_path.unlink(missing_ok=True)
    notes_path.unlink(missing_ok=True)


def _slides_from_retained_events(job_id: str) -> list[PreviewSlide]:
    """Recover previews from retained WebSocket slide events.

    Older failed/cancelled jobs may have had their workspace deleted before we
    started preserving project directories. The event ring can still contain
    the SVG preview payloads that were sent live to the browser, so use those
    as a best-effort fallback.
    """
    slides_by_index: dict[int, PreviewSlide] = {}
    for event in session_manager.get_events_after(job_id, 0):
        if event.get("type") != "slide_ready":
            continue
        data = event.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("svg"), str):
            continue
        try:
            index = int(data.get("page") or len(slides_by_index) + 1)
        except (TypeError, ValueError):
            index = len(slides_by_index) + 1
        slides_by_index[index] = PreviewSlide(
            index=index,
            name=f"slide_{index}",
            source="event",
            content=data["svg"],
        )
    return [slides_by_index[index] for index in sorted(slides_by_index)]


def _resolve_workspace_path(raw_path: str) -> Path | None:
    try:
        resolved = Path(raw_path).resolve()
        resolved.relative_to(settings.workspaces_dir.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _resolve_workspace_file(raw_path: str) -> Path | None:
    try:
        resolved = Path(raw_path).resolve()
        resolved.relative_to(settings.workspaces_dir.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _find_latest_output(project_dir: Path) -> Path | None:
    exports_dir = project_dir / "exports"
    if not exports_dir.exists():
        return None
    candidates = sorted(exports_dir.glob("*.pptx"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _project_scene_paths(project_dir: Path):
    return scene_paths(project_dir / "pptx_scene")


def _scene_source_pptx(project_dir: Path | None, output_path: str | None, paths=None) -> Path | None:
    if project_dir is None:
        return None
    paths = paths or _project_scene_paths(project_dir)
    if paths.deck_pptx.exists() and _scene_cache_is_dirty(paths):
        return paths.deck_pptx
    pptist_deck = _pptist_current_pptx(project_dir)
    if pptist_deck is not None:
        return pptist_deck
    if output_path:
        candidate = _resolve_workspace_file(output_path)
        if candidate is not None and candidate.exists() and candidate.suffix.lower() == ".pptx":
            return candidate
    if paths.deck_pptx.exists():
        return paths.deck_pptx
    latest = _find_latest_output(project_dir)
    if latest and latest.exists():
        return latest
    return None


def _scene_cache_is_dirty(paths) -> bool:
    meta_path = paths.scene_dir / "scene_meta.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(meta, dict) and meta.get("dirty") is True


def _safe_mtime_version(path: Path) -> int:
    try:
        return int(path.stat().st_mtime_ns)
    except OSError:
        return 0


def _available_scene_slide_indexes(source_pptx: Path | None, paths) -> set[int]:
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


async def _preview_scene_file(job_id: str, slide_index: int, kind: str) -> FileResponse:
    job = session_manager.get_job(job_id)
    project_dir = _viewable_project_dir_for_id(job_id, job)
    paths = _project_scene_paths(project_dir)
    source_pptx = _scene_source_pptx(project_dir, job.output_path if job else None, paths)
    if source_pptx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No PowerPoint deck found for scene editing.")
    await aoffload(get_slide_scene, source_pptx, paths, slide_index)
    path = slide_edit_base_path(paths, slide_index) if kind == "edit_base" else slide_render_path(paths, slide_index)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Slide render not found.")
    return FileResponse(path, media_type="image/png", filename=path.name, headers={"Cache-Control": "no-store"})


def _preview_project_dir_for_id(job_id: str, job) -> Path:
    if job is not None:
        return _editable_project_dir(job)
    candidate = _resolve_workspace_path(str(settings.workspaces_dir / Path(job_id).name))
    if candidate is None or not candidate.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return candidate


def _viewable_project_dir_for_id(job_id: str, job) -> Path:
    if job is not None:
        return _viewable_project_dir(job)
    candidate = _resolve_workspace_path(str(settings.workspaces_dir / Path(job_id).name))
    if candidate is None or not candidate.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return candidate


def _viewable_project_dir(job) -> Path:
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    if job.status not in {"complete", "error", "cancelled"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Presentation preview is not ready yet.")
    if not job.project_dir:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job has no project workspace.")
    project_dir = _resolve_workspace_path(job.project_dir)
    if project_dir is None or not project_dir.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
    return project_dir


def _editable_project_dir(job) -> Path:
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    if job.status != "complete":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only completed presentations can be edited.")
    if not job.project_dir:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job has no project workspace.")
    project_dir = _resolve_workspace_path(job.project_dir)
    if project_dir is None or not project_dir.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
    return project_dir


def _editable_slide_files(project_dir: Path) -> tuple[list[Path], str]:
    final_files = get_svg_files(project_dir, "final")
    output_files = get_svg_files(project_dir, "output")
    if final_files:
        return final_files, "final"
    if output_files:
        return output_files, "output"
    return [], "final"


def _validated_svg(content: str) -> str:
    svg = content.strip()
    if not re.match(r"^<svg[\s>]", svg):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Expected an SVG document.")
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid SVG: {exc}") from exc
    tag = root.tag.rsplit("}", 1)[-1].lower()
    if tag != "svg":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Expected an SVG root element.")
    return svg


def _slide_document_path(project_dir: Path, svg_path: Path) -> Path:
    return project_dir / "slide_documents" / f"{svg_path.stem}.json"


def _slide_notes_path(project_dir: Path, svg_path: Path) -> Path:
    return project_dir / "notes" / f"{svg_path.stem}.md"


def _read_slide_document(project_dir: Path, svg_path: Path) -> dict[str, Any] | None:
    path = _slide_document_path(project_dir, svg_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _read_slide_notes(project_dir: Path, svg_path: Path) -> str:
    path = _slide_notes_path(project_dir, svg_path)
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _write_slide_document(project_dir: Path, svg_path: Path, document: dict[str, Any]) -> None:
    path = _slide_document_path(project_dir, svg_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_slide_notes(project_dir: Path, svg_path: Path, notes: str) -> None:
    path = _slide_notes_path(project_dir, svg_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if notes.strip():
        path.write_text(notes[:1000], encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def _notes_from_document(document: dict[str, Any] | None) -> str | None:
    if not isinstance(document, dict):
        return None
    notes = document.get("speakerNotes")
    return notes if isinstance(notes, str) else None


def _normalize_slide_document(document: dict[str, Any] | None, content: str) -> dict[str, Any] | None:
    if document is None:
        return None
    if not isinstance(document, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid slide document.")
    version = document.get("version", 1)
    width = _safe_number(document.get("width"), 1280)
    height = _safe_number(document.get("height"), 720)
    elements = document.get("elements", [])
    if not isinstance(elements, list):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid slide document elements.")
    background_svg = document.get("backgroundSvg")
    if not isinstance(background_svg, str) or not background_svg.strip():
        background_svg = content
    normalized = {
        "version": version,
        "width": width,
        "height": height,
        "backgroundSvg": _validated_svg(background_svg),
        "elements": elements,
    }
    notes = document.get("speakerNotes")
    if isinstance(notes, str):
        normalized["speakerNotes"] = notes[:1000]
    return normalized


def _safe_number(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _blank_slide_svg() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720">'
        '<rect width="1280" height="720" fill="#ffffff"/>'
        '<text x="96" y="120" fill="#0f172a" font-size="42" font-family="Arial, sans-serif" font-weight="700">'
        "New slide"
        "</text>"
        "</svg>"
    )


def _blank_slide_document(content: str) -> dict[str, Any]:
    return {
        "version": 1,
        "width": 1280,
        "height": 720,
        "backgroundSvg": content,
        "elements": [
            {
                "id": "text-title",
                "type": "text",
                "x": 96,
                "y": 78,
                "width": 420,
                "height": 56,
                "text": "New slide",
                "fontSize": 42,
                "fontFamily": "Arial",
                "fill": "#0f172a",
                "fontStyle": "bold",
                "align": "left",
                "sourceTag": "text",
                "sourceIndex": 0,
                "committed": False,
            }
        ],
    }
