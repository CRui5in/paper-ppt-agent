"""Preview endpoint for generated SVG slides."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Response, status

from backend.api.schemas import PreviewResponse, PreviewSlide
from backend.config import settings
from backend.generator.project_manager import get_svg_files
from backend.generator.svg_finalize.render_ready import prepare_svg_file_for_render
from backend.runtime import aoffload, apath_exists, aread_text, aremove
from backend.session.manager import session_manager

router = APIRouter()


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
            prepared_path = await aoffload(prepare_svg_file_for_render, svg_path)
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
