"""Font replacement endpoint — apply custom fonts to SVG previews and re-export."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from backend.config import settings
from backend.generator.project_manager import get_svg_files
from backend.generator.svg_finalize.normalize_fonts import (
    FontReplaceConfig,
    replace_fonts_in_svg_dir,
)
from backend.runtime import aoffload
from backend.session.manager import session_manager

router = APIRouter()


class UpdateFontsRequest(BaseModel):
    """Font targets — set only the fonts you want to replace. None = keep."""

    western_heading: str | None = None
    western_body: str | None = None
    cjk_heading: str | None = None
    cjk_body: str | None = None


class UpdateFontsResponse(BaseModel):
    svg_fonts_replaced: int
    status: str = "complete"


@router.post("/status/{job_id}/update-fonts", response_model=UpdateFontsResponse)
async def update_svg_fonts(job_id: str, request: UpdateFontsRequest) -> UpdateFontsResponse:
    """Replace fonts in SVG preview files for a completed job.

    This updates the SVG files so the preview reflects the new fonts.
    After calling this, the user can click "Re-export" to get a new PPTX
    with the updated fonts (re-export reads from the same SVG files).
    """
    job = session_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    if not job.project_dir:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Job has no project directory.")

    try:
        project_dir = Path(job.project_dir).resolve()
        project_dir.relative_to(settings.workspaces_dir.resolve())
    except (OSError, ValueError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid project directory.")

    config = FontReplaceConfig(
        western_heading=request.western_heading,
        western_body=request.western_body,
        cjk_heading=request.cjk_heading,
        cjk_body=request.cjk_body,
    )

    total = 0
    for subdir in ("svg_final", "svg_output"):
        total += await aoffload(replace_fonts_in_svg_dir, project_dir / subdir, config)

    return UpdateFontsResponse(svg_fonts_replaced=total)
