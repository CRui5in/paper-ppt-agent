"""Font replacement endpoint — apply custom fonts to a completed PPTX and SVGs."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from backend.api.schemas import FontReplaceRequest, FontReplaceResponse
from backend.generator.pptx_font_editor import (
    FontReplaceConfig,
    replace_fonts_in_pptx,
    replace_fonts_in_svg_dir,
)
from backend.runtime import aoffload
from backend.session.manager import session_manager

router = APIRouter()


@router.post("/download/{job_id}/apply-fonts", response_model=FontReplaceResponse)
async def apply_fonts(job_id: str, request: FontReplaceRequest) -> FontReplaceResponse:
    """Apply custom fonts to the generated PPTX and SVG preview files.

    Replaces fonts in both the exported PPTX (for download) and the SVG
    files in svg_final/ (for live preview). The frontend can then
    re-fetch the preview to see the font changes immediately.
    """
    job = session_manager.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found.",
        )

    if not job.project_dir:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Job has no project directory — cannot apply fonts.",
        )

    if not job.output_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Job has no exported PPTX — nothing to edit.",
        )

    source_pptx = Path(job.output_path)
    if not source_pptx.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Exported PPTX file not found on disk.",
        )

    project_dir = Path(job.project_dir)

    # Build font config
    config = FontReplaceConfig(
        western_heading=request.western_heading,
        western_body=request.western_body,
        cjk_heading=request.cjk_heading,
        cjk_body=request.cjk_body,
    )

    # ── 1. Replace fonts in PPTX ──────────────────────────────────────────
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = project_dir / "exports" / f"presentation_{timestamp}_fonts.pptx"

    try:
        # PPTX font replacement opens the .pptx with python-pptx and
        # rewrites every <a:rPr typeface=…> entry — fast in CPU but fully
        # synchronous file IO. Offload so concurrent generations on the
        # same server aren't blocked while a font swap is in flight.
        final_path, edit_result = await aoffload(
            replace_fonts_in_pptx,
            source_pptx,
            config,
            output_path=output_path,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Font replacement failed: {exc}",
        ) from exc

    # ── 2. Replace fonts in SVG files (for live preview) ───────────────────
    svg_fonts_replaced = 0
    for svg_dir_name in ("svg_final", "svg_output"):
        svg_dir = project_dir / svg_dir_name
        svg_fonts_replaced += await aoffload(replace_fonts_in_svg_dir, svg_dir, config)

    return FontReplaceResponse(
        output_path=str(final_path),
        slides_modified=edit_result.slides_modified,
        fonts_replaced=edit_result.fonts_replaced,
        svg_fonts_replaced=svg_fonts_replaced,
        status="complete",
    )
