from __future__ import annotations

import asyncio
import time
from pathlib import Path

from backend.session.manager import session_manager


def test_generate_creates_job_and_updates_status(client, pdf_bytes: bytes, monkeypatch):
    upload_response = client.post(
        "/api/upload",
        files={"file": ("paper.pdf", pdf_bytes, "application/pdf")},
    )
    session_id = upload_response.json()["session_id"]

    async def fake_run(job_id: str, request):
        job = session_manager.get_job(job_id)
        assert job is not None
        output_path = Path(request.file_path.parent) / "presentation.pptx"
        output_path.write_bytes(b"pptx")
        session_manager.record_event(
            job_id,
            {
                "type": "progress",
                "job_id": job_id,
                "stage": "parsing",
                "status": "started",
                "message": "Parsing",
                "progress": 0.1,
                "slides_completed": 0,
                "total_slides": 3,
                "data": {"project_dir": str(request.file_path.parent)},
            },
            status="parsing",
            progress=0.1,
            message="Parsing",
            total_slides=3,
            project_dir=str(request.file_path.parent),
        )
        await asyncio.sleep(0)
        session_manager.record_event(
            job_id,
            {
                "type": "complete",
                "job_id": job_id,
                "stage": "export",
                "status": "complete",
                "message": "Done",
                "progress": 1.0,
                "slides_completed": 3,
                "total_slides": 3,
                "data": {"output_path": str(output_path)},
            },
            status="complete",
            progress=1.0,
            message="Done",
            slides_completed=3,
            total_slides=3,
            output_path=str(output_path),
        )

    monkeypatch.setattr("backend.api.endpoints.generate._run_generation_job", fake_run)

    response = client.post(
        "/api/generate",
        json={
            "session_id": session_id,
            "instruction": "",
            "model_config": {
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "test-key",
                "base_url": "https://example.invalid/v1",
            },
            "options": {
                "canvas_format": "ppt169",
                "style": "academic",
                "language": "en",
                "detail_level": "high",
            },
        },
    )

    assert response.status_code == 200
    job_id = response.json()["job_id"]
    assert job_id

    time.sleep(0.05)
    status_response = client.get(f"/api/status/{job_id}")
    assert status_response.status_code == 200
    status_payload = status_response.json()
    assert status_payload["status"] == "complete"
    assert status_payload["slides_completed"] == 3

    download_response = client.get(f"/api/download/{job_id}")
    assert download_response.status_code == 200


def test_preview_and_websocket_receive_slide_events(client, pdf_bytes: bytes, monkeypatch):
    upload_response = client.post(
        "/api/upload",
        files={"file": ("paper.pdf", pdf_bytes, "application/pdf")},
    )
    session_id = upload_response.json()["session_id"]

    async def fake_run(job_id: str, request):
        project_dir = request.file_path.parent / "job_workspace"
        svg_dir = project_dir / "svg_output"
        svg_dir.mkdir(parents=True, exist_ok=True)
        svg_content = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text x="10" y="30">Hi</text></svg>'
        (svg_dir / "01_slide.svg").write_text(svg_content, encoding="utf-8")

        session_manager.record_event(
            job_id,
            {
                "type": "progress",
                "job_id": job_id,
                "stage": "generation",
                "status": "started",
                "message": "Generating",
                "progress": 0.4,
                "slides_completed": 0,
                "total_slides": 1,
                "data": {"project_dir": str(project_dir), "total_slides": 1},
            },
            status="generation",
            progress=0.4,
            message="Generating",
            total_slides=1,
            project_dir=str(project_dir),
        )
        session_manager.record_event(
            job_id,
            {
                "type": "slide_ready",
                "job_id": job_id,
                "stage": "generation",
                "status": "progress",
                "message": "Generated slide 1/1",
                "progress": 0.75,
                "slides_completed": 1,
                "total_slides": 1,
                "data": {"page": 1, "svg": svg_content},
            },
            slides_completed=1,
        )

    monkeypatch.setattr("backend.api.endpoints.generate._run_generation_job", fake_run)

    response = client.post(
        "/api/generate",
        json={
            "session_id": session_id,
            "instruction": "",
            "model_config": {
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "test-key",
                "base_url": "https://example.invalid/v1",
            },
            "options": {
                "canvas_format": "ppt169",
                "style": "academic",
                "language": "en",
                "detail_level": "very_high",
            },
        },
    )
    job_id = response.json()["job_id"]

    with client.websocket_connect(f"/ws/{job_id}") as websocket:
        first = websocket.receive_json()
        assert first["type"] == "progress"
        assert first["job_id"] == job_id

    deadline = time.time() + 1.0
    slides = []
    preview_response = None
    while time.time() < deadline:
        preview_response = client.get(f"/api/preview/{job_id}")
        assert preview_response.status_code == 200
        slides = preview_response.json()["slides"]
        if slides:
            break
        time.sleep(0.01)
    assert preview_response is not None
    assert len(slides) == 1


def test_preview_recovers_retained_slide_events_when_workspace_is_missing(client, workspace_tmp: Path):
    file_path = workspace_tmp / "uploads" / "abc123" / "paper.pdf"
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"data")
    session = session_manager.create_session(file_path, "pdf", "paper.pdf", 4, session_id="abc123")
    job = session_manager.create_job(session.id)
    svg_content = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text>Recovered</text></svg>'

    session_manager.record_event(
        job.id,
        {
            "type": "slide_ready",
            "job_id": job.id,
            "stage": "generation",
            "status": "progress",
            "message": "Generated slide 1/3",
            "progress": 0.6,
            "slides_completed": 1,
            "total_slides": 3,
            "data": {"page": 1, "svg": svg_content},
        },
        status="error",
        message="Error code: 429",
        slides_completed=1,
        total_slides=3,
        project_dir=None,
        error="Error code: 429",
    )

    preview_response = client.get(f"/api/preview/{job.id}")

    assert preview_response.status_code == 200
    payload = preview_response.json()
    assert payload["status"] == "error"
    assert payload["project_dir"] is None
    assert payload["slides"] == [
        {
            "index": 1,
            "name": "slide_1",
            "source": "event",
            "content": svg_content,
        }
    ]
