"""Image search and replacement API endpoints.

Phase B: Post-generation image search, insertion, and replacement.
Supports both replacing existing <image> elements and AI-powered insertion
when no <image> element exists in the slide SVG.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from backend.api.schemas import (
    ImageApplyRequest,
    ImageApplyResponse,
    ImageSearchRequest,
    ImageSearchResponse,
    ImageSearchResultItem,
    ImageUndoResponse,
)
from backend.session.manager import session_manager

router = APIRouter()


@router.post("/image-search/{job_id}", response_model=ImageSearchResponse)
async def search_images_endpoint(
    job_id: str, request: ImageSearchRequest
) -> ImageSearchResponse:
    """Search for images online and return results (no download)."""
    job = session_manager.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found.",
        )
    if not job.project_dir:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Job has no project directory.",
        )

    # Use client-provided key first, fall back to job config
    from backend.api.schemas import ResearchConfig

    tavily_key = request.tavily_api_key or _get_job_config(job, "tavily_api_key")
    serpapi_key = request.serpapi_key or _get_job_config(job, "serpapi_key")
    config = ResearchConfig(
        tavily_api_key=tavily_key,
        serpapi_key=serpapi_key,
    )

    if not config.tavily_api_key and not config.serpapi_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No Tavily or SerpAPI key configured. Please enter one in the panel or in Research settings.",
        )

    from backend.orchestrator.image_search import search_images

    results = await search_images(
        request.query, config, max_results=request.max_results
    )

    return ImageSearchResponse(
        results=[
            ImageSearchResultItem(
                url=r.url,
                thumbnail=r.thumbnail or "",
                description=r.description or "",
                source=r.source or "",
            )
            for r in results
        ]
    )


@router.post("/image-search/{job_id}/apply", response_model=ImageApplyResponse)
async def apply_image_endpoint(
    job_id: str, request: ImageApplyRequest
) -> ImageApplyResponse:
    """Download a selected image and insert/replace it in the target slide SVG.

    If the SVG contains <image> elements, replaces the first one.
    If no <image> elements exist, uses LLM (if configured) to intelligently
    insert the image, or falls back to a heuristic placement.
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
            detail="Job has no project directory.",
        )

    project_dir = Path(job.project_dir)
    images_dir = project_dir / "images"
    images_dir.mkdir(exist_ok=True)

    # Generate a filename for the new image
    import hashlib

    url_hash = hashlib.md5(request.image_url.encode()).hexdigest()[:8]
    filename = f"search_img_slide{request.slide_index}_{url_hash}.png"
    output_path = images_dir / filename

    # Download the image
    from backend.orchestrator.image_search import download_image

    saved = await download_image(request.image_url, output_path)
    if saved is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to download the selected image.",
        )

    new_href = f"../images/{filename}"

    # Find the SVG file for the target slide
    svg_path = _find_slide_svg(project_dir, request.slide_index)
    if svg_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Could not find SVG for slide {request.slide_index}.",
        )

    from backend.orchestrator.image_search import (
        auto_insert_image,
        backup_svg,
        replace_image_in_svg,
        svg_has_image_elements,
    )

    # Backup before modification
    backup_svg(svg_path)

    svg_content = svg_path.read_text(encoding="utf-8")
    action = ""

    if svg_has_image_elements(svg_content):
        # Replace existing <image> element
        modified = replace_image_in_svg(
            svg_content, new_href, request.target_element
        )
        action = "replaced"
    else:
        # No <image> element — use LLM to insert, or fallback heuristic
        provider = request.provider or job.provider or "openai"
        model = request.model or job.model_name or "gpt-4o"
        api_key = request.api_key or ""

        if api_key:
            modified = await auto_insert_image(
                svg_content,
                new_href,
                image_description=request.image_description or "",
                image_path=saved,
                provider=provider,
                model=model,
                api_key=api_key,
                base_url=request.base_url,
            )
            action = "inserted_ai"
        else:
            # No LLM key — use heuristic fallback
            from backend.orchestrator.image_search import _insert_image_fallback

            modified = _insert_image_fallback(svg_content, new_href)
            action = "inserted_heuristic"

    # Save modified SVG
    svg_path.write_text(modified, encoding="utf-8")

    # Re-embed images in all SVGs (both svg_output and svg_final)
    svg_updated = True
    from backend.generator.svg_finalize.embed_images import (
        build_image_index,
        embed_images_in_svg,
    )

    image_index = build_image_index(project_dir)
    for svg_dir_name in ("svg_output", "svg_final"):
        svg_dir = project_dir / svg_dir_name
        if svg_dir.exists():
            for svg_file in sorted(svg_dir.glob("*.svg")):
                embed_images_in_svg(svg_file, image_index=image_index)

    return ImageApplyResponse(
        status="ok",
        local_path=str(saved),
        svg_updated=svg_updated,
        action=action,
    )


@router.post("/image-search/{job_id}/undo", response_model=ImageUndoResponse)
async def undo_image_endpoint(job_id: str) -> ImageUndoResponse:
    """Undo the last image change by restoring the SVG backup."""
    job = session_manager.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found.",
        )
    if not job.project_dir:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Job has no project directory.",
        )

    from backend.orchestrator.image_search import restore_svg_backup

    project_dir = Path(job.project_dir)
    restored = False

    # Try to restore backups in both svg directories
    for svg_dir_name in ("svg_output", "svg_final"):
        svg_dir = project_dir / svg_dir_name
        if not svg_dir.exists():
            continue
        for svg_file in svg_dir.glob("*.svg.bak"):
            original = svg_file.with_suffix("")  # remove .bak
            if restore_svg_backup(original):
                restored = True

    if not restored:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No backup found to undo.",
        )

    # Re-embed images after restoring
    from backend.generator.svg_finalize.embed_images import (
        build_image_index,
        embed_images_in_svg,
    )

    image_index = build_image_index(project_dir)
    for svg_dir_name in ("svg_output", "svg_final"):
        svg_dir = project_dir / svg_dir_name
        if svg_dir.exists():
            for svg_file in sorted(svg_dir.glob("*.svg")):
                embed_images_in_svg(svg_file, image_index=image_index)

    return ImageUndoResponse(status="ok", svg_restored=True)


def _get_job_config(job, key: str) -> str | None:
    """Extract a config value from the job's stored options."""
    try:
        options = job.generation_options or {}
        research = options.get("research_config") or {}
        return research.get(key)
    except Exception:
        return None


def _find_slide_svg(project_dir: Path, slide_index: int) -> Path | None:
    """Find the SVG file corresponding to a slide index."""
    for dir_name in ("svg_final", "svg_output"):
        svg_dir = project_dir / dir_name
        if not svg_dir.exists():
            continue
        # Look for files matching the slide index pattern: NN_*.svg
        for svg_path in sorted(svg_dir.glob("*.svg")):
            match = re.match(r"(\d+)_", svg_path.name)
            if match and int(match.group(1)) == slide_index:
                return svg_path
        # Also try matching by position in sorted list
        all_svgs = sorted(svg_dir.glob("*.svg"))
        if 0 <= slide_index - 1 < len(all_svgs):
            return all_svgs[slide_index - 1]
    return None
