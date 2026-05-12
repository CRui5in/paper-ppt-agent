"""Preview endpoint for generated SVG slides."""

from pathlib import Path
import json
import re
import xml.etree.ElementTree as ET
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from backend.api.schemas import PreviewResponse, PreviewSlide
from backend.config import settings
from backend.generator.project_manager import get_svg_files
from backend.generator.svg_finalize.render_ready import prepare_svg_file_for_render
from backend.runtime import aoffload, apath_exists, aread_text, aremove
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
    """Serve a pre-repair archived SVG from svg_archive/repair/."""
    job = session_manager.get_job(job_id)
    if job is None or not job.project_dir:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    # Sanitize filename to prevent path traversal
    safe_name = Path(filename).name
    if safe_name != filename or ".." in filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filename.")

    svg_path = Path(job.project_dir) / "svg_archive" / "repair" / safe_name
    if not await apath_exists(svg_path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archive not found.")

    content = await aread_text(svg_path, encoding="utf-8")
    return Response(content=content, media_type="image/svg+xml")


@router.get("/preview/{job_id}", response_model=PreviewResponse)
async def get_preview(job_id: str) -> PreviewResponse:
    job = session_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    normalized_status = "complete" if job.output_path and Path(job.output_path).exists() else job.status
    return await _build_preview_response(job_id, Path(job.project_dir) if job.project_dir else None, job.output_path, normalized_status)


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
    slide_files, _source = _editable_slide_files(project_dir)
    if len(slide_files) <= 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="At least one slide must remain.")
    if slide_index < 1 or slide_index > len(slide_files):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Slide not found.")

    svg_path = slide_files[slide_index - 1]
    document_path = _slide_document_path(project_dir, svg_path)
    notes_path = _slide_notes_path(project_dir, svg_path)
    try:
        svg_path.unlink(missing_ok=True)
        document_path.unlink(missing_ok=True)
        notes_path.unlink(missing_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to delete slide: {exc}") from exc
    normalized_status = "complete" if job.output_path and Path(job.output_path).exists() else job.status
    return await _build_preview_response(job_id, project_dir, job.output_path, normalized_status)


@router.get("/preview-project", response_model=PreviewResponse)
async def get_project_preview(project_dir: str) -> PreviewResponse:
    resolved_project_dir = _resolve_workspace_path(project_dir)
    if resolved_project_dir is None or not resolved_project_dir.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

    output_path = _find_latest_output(resolved_project_dir)
    return await _build_preview_response(
        job_id=resolved_project_dir.name,
        project_dir=resolved_project_dir,
        output_path=str(output_path) if output_path else None,
        status_value="complete" if output_path else "unknown",
    )


async def _build_preview_response(
    job_id: str,
    project_dir: Path | None,
    output_path: str | None,
    status_value: str,
) -> PreviewResponse:
    slides: list[PreviewSlide] = []
    if project_dir and project_dir.exists():
        final_files = get_svg_files(project_dir, "final")
        output_files = get_svg_files(project_dir, "output")
        slide_files = final_files or output_files
        source = "final" if final_files else "output"
        for index, svg_path in enumerate(slide_files, start=1):
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
            slides.append(
                PreviewSlide(
                    index=index,
                    name=svg_path.stem,
                    source=source,
                    content=content,
                    notes=_read_slide_notes(project_dir, svg_path),
                    document=_read_slide_document(project_dir, svg_path),
                )
            )

    if not slides:
        slides = _slides_from_retained_events(job_id)

    return PreviewResponse(
        job_id=job_id,
        project_dir=str(project_dir) if project_dir else None,
        slides=slides,
        output_path=output_path,
        status=status_value,
    )


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


def _find_latest_output(project_dir: Path) -> Path | None:
    exports_dir = project_dir / "exports"
    if not exports_dir.exists():
        return None
    candidates = sorted(exports_dir.glob("*.pptx"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


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
