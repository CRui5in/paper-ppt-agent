"""Job status endpoint."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Response, status

from backend.api.schemas import CancelJobResponse, JobStatus
from backend.session.manager import session_manager

router = APIRouter()


@router.get("/status/{job_id}", response_model=JobStatus)
async def get_job_status(job_id: str) -> JobStatus:
    job = session_manager.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found.",
        )

    normalized_status = "complete" if job.output_path and Path(job.output_path).exists() else job.status

    return JobStatus(
        status=normalized_status,
        progress=job.progress,
        message=job.message,
        slides_completed=job.slides_completed,
        total_slides=job.total_slides,
        output_path=job.output_path,
        error=None if normalized_status == "complete" else job.error,
    )


@router.post("/status/{job_id}/cancel", response_model=CancelJobResponse)
async def cancel_job(job_id: str) -> CancelJobResponse:
    job = session_manager.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found.",
        )

    if job.status in {"complete", "error", "cancelled"}:
        return CancelJobResponse(job_id=job_id, status=job.status)

    cancelled = session_manager.cancel_job(job_id)
    if not cancelled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job is not cancellable.",
        )

    return CancelJobResponse(job_id=job_id, status="cancelling")


@router.delete("/status/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(job_id: str) -> Response:
    deleted = session_manager.delete_job(job_id, delete_files=True)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
