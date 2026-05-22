"""Download endpoint for completed presentations."""

import json
import shutil
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from backend.api.schemas import ReexportResponse
from backend.config import settings
from backend.generator.project_manager import get_notes, get_svg_files, sync_svg_output_assets_to_final
from backend.generator.pptx_sanitize import sanitize_pptx_file, validate_pptx_xml
from backend.generator.svg_to_pptx import create_pptx
from backend.session.manager import session_manager

router = APIRouter()


@router.get("/download/{job_id}")
async def download_presentation(job_id: str) -> FileResponse:
    job = session_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    if job.status != "complete" or not job.output_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Presentation is not ready yet.",
        )

    project_dir = Path(job.project_dir) if job.project_dir else None
    path = _edited_pptist_deck(project_dir) or _dirty_scene_deck(project_dir) or Path(job.output_path)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Output file not found.")
    sanitize_pptx_file(path)
    _raise_if_invalid_pptx(path)

    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=_download_filename(project_dir, path),
    )


@router.get("/download-file")
async def download_presentation_file(output_path: str) -> FileResponse:
    path = _resolve_workspace_file(output_path)
    if path is None or not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Output file not found.")
    sanitize_pptx_file(path)
    _raise_if_invalid_pptx(path)

    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=path.name,
    )


@router.post("/download/{job_id}/reexport", response_model=ReexportResponse)
async def reexport_presentation(job_id: str) -> ReexportResponse:
    job = session_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    if not job.project_dir:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job has no project workspace.",
        )

    project_dir = _resolve_workspace_file(job.project_dir)
    if project_dir is None or not project_dir.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    canvas_format = job.canvas_format or "ppt169"
    output_path = project_dir / "exports" / f"presentation_{timestamp}.pptx"

    from backend.runtime import aoffload

    edited_deck = _edited_pptist_deck(project_dir) or _dirty_scene_deck(project_dir)
    if edited_deck is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        await aoffload(shutil.copy2, edited_deck, output_path)
        await aoffload(sanitize_pptx_file, output_path)
        _raise_if_invalid_pptx(output_path)
        fallback_slides: list[int] = []
        warnings: list[str] = []
    else:
        sync_svg_output_assets_to_final(project_dir)
        svg_files = get_svg_files(project_dir, "final") or get_svg_files(project_dir, "output")
        if not svg_files:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No slide SVGs found for re-export.",
            )

        notes = get_notes(project_dir, svg_files)
    # PPTX assembly is python-pptx + zipfile + optional SVG raster fallback —
    # all synchronous and
    # CPU-heavy (5–20s for a long deck). Offload so re-export from the result
    # page doesn't freeze the API while it runs.
        await aoffload(
            create_pptx,
            svg_files,
            output_path,
            canvas_format=canvas_format,
            notes=notes,
        )
        fallback_slides = _read_fallback_slides(output_path)
        warnings = (
            [f"Slides {', '.join(map(str, fallback_slides))} used raster fallback export."]
            if fallback_slides
            else []
        )

    session_manager.update_job(
        job_id,
        status="complete",
        message="Presentation re-exported",
        output_path=str(output_path),
        error=None,
    )

    return ReexportResponse(
        job_id=job_id,
        status="complete",
        output_path=str(output_path),
        fallback_slides=fallback_slides,
        warnings=warnings,
    )


def _raise_if_invalid_pptx(path: Path) -> None:
    issues = validate_pptx_xml(path)
    if issues:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"PPTX package validation failed: {'; '.join(issues[:3])}",
        )


def _read_fallback_slides(output_path: Path) -> list[int]:
    report_path = output_path.with_suffix(".conversion_report.json")
    if not report_path.exists():
        return []
    try:
        rows = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(rows, list):
        return []
    slides: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("mode") != "fallback_image":
            continue
        slide = row.get("slide")
        if isinstance(slide, int):
            slides.append(slide)
    return slides


def _dirty_scene_deck(project_dir: Path | None) -> Path | None:
    if project_dir is None:
        return None
    deck = project_dir / "pptx_scene" / "deck.pptx"
    meta_path = project_dir / "pptx_scene" / "scene_meta.json"
    if not deck.exists() or not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return deck if meta.get("dirty") is True else None


def _edited_pptist_deck(project_dir: Path | None) -> Path | None:
    if project_dir is None:
        return None
    deck = project_dir / "pptist" / "current.pptx"
    return deck if deck.exists() else None


def _download_filename(project_dir: Path | None, path: Path) -> str:
    if project_dir is not None and path == project_dir / "pptist" / "current.pptx":
        title = _pptist_deck_title(project_dir)
        if title:
            return f"{_safe_filename_stem(title)}.pptx"
    if project_dir is not None and path == project_dir / "pptx_scene" / "deck.pptx":
        title = _pptist_deck_title(project_dir) or project_dir.name
        return f"{_safe_filename_stem(title)}.pptx"
    return path.name


def _pptist_deck_title(project_dir: Path) -> str | None:
    deck_path = project_dir / "pptist" / "deck.json"
    if not deck_path.exists():
        return None
    try:
        payload = json.loads(deck_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    title = payload.get("title")
    return title.strip() if isinstance(title, str) and title.strip() else None


def _safe_filename_stem(value: str) -> str:
    safe = "".join("_" if char in '<>:"/\\|?*\r\n\t' else char for char in value).strip(" .")
    return safe[:120] or "presentation"


def _resolve_workspace_file(raw_path: str) -> Path | None:
    try:
        resolved = Path(raw_path).resolve()
        resolved.relative_to(settings.workspaces_dir.resolve())
    except (OSError, ValueError):
        return None
    return resolved
