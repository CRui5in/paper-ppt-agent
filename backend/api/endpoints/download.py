"""Download endpoint for completed presentations."""

from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from backend.api.schemas import ReexportResponse
from backend.config import settings
from backend.generator.project_manager import get_notes, get_svg_files
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

    path = Path(job.output_path)
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Output file not found.")

    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=path.name,
    )


@router.get("/download-file")
async def download_presentation_file(output_path: str) -> FileResponse:
    path = _resolve_workspace_file(output_path)
    if path is None or not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Output file not found.")

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

    svg_files = get_svg_files(project_dir, "final") or get_svg_files(project_dir, "output")
    if not svg_files:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No slide SVGs found for re-export.",
        )

    notes = get_notes(project_dir, svg_files)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    canvas_format = job.canvas_format or "ppt169"
    output_path = project_dir / "exports" / f"presentation_{timestamp}.pptx"

    from backend.runtime import aoffload

    # PPTX assembly is python-pptx + zipfile + cairosvg — all synchronous and
    # CPU-heavy (5–20s for a long deck). Offload so re-export from the result
    # page doesn't freeze the API while it runs.
    await aoffload(
        create_pptx,
        svg_files,
        output_path,
        canvas_format=canvas_format,
        notes=notes,
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
    )


def _resolve_workspace_file(raw_path: str) -> Path | None:
    try:
        resolved = Path(raw_path).resolve()
        resolved.relative_to(settings.workspaces_dir.resolve())
    except (OSError, ValueError):
        return None
    return resolved
