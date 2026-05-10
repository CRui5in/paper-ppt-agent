from __future__ import annotations

from pathlib import Path

from backend.session.manager import session_manager


def test_session_manager_broadcasts_events_and_cleans_uploads(workspace_tmp: Path):
    upload_dir = workspace_tmp / "uploads" / "abc123"
    upload_dir.mkdir(parents=True)
    file_path = upload_dir / "paper.pdf"
    file_path.write_bytes(b"data")

    session = session_manager.create_session(file_path, "pdf", "paper.pdf", 4, session_id="abc123")
    job = session_manager.create_job(session.id)

    queue = session_manager.subscribe_ws(job.id)
    session_manager.record_event(job.id, {"job_id": job.id, "type": "progress"}, status="parsing")

    event = queue.get_nowait()
    assert event["type"] == "progress"

    session_manager.unsubscribe_ws(job.id, queue)
    session_manager.delete_session(session.id)
    assert not upload_dir.exists()


def test_delete_job_removes_workspace_and_last_upload(workspace_tmp: Path):
    upload_dir = workspace_tmp / "uploads" / "abc123"
    upload_dir.mkdir(parents=True)
    file_path = upload_dir / "paper.pdf"
    file_path.write_bytes(b"data")
    project_dir = workspace_tmp / "workspaces" / "paper_ppt_abc"
    project_dir.mkdir(parents=True)
    (project_dir / "artifact.txt").write_text("x", encoding="utf-8")

    session = session_manager.create_session(file_path, "pdf", "paper.pdf", 4, session_id="abc123")
    job = session_manager.create_job(session.id)
    session_manager.update_job(job.id, project_dir=str(project_dir))

    assert session_manager.delete_job(job.id)

    assert session_manager.get_job(job.id) is None
    assert session_manager.get_session(session.id) is None
    assert not project_dir.exists()
    assert not upload_dir.exists()
