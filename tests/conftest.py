from __future__ import annotations

import io
import shutil
import tarfile
import uuid
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import settings
import backend.runtime.scheduler as scheduler_module
from backend.session.manager import session_manager


@pytest.fixture
def workspace_tmp() -> Path:
    path = Path.cwd() / ".test-runtime" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolate_state(workspace_tmp: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(scheduler_module, "_scheduler", None)
    monkeypatch.setattr(settings, "runtime_dir", workspace_tmp / ".runtime")
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(session_manager, "_state_file", settings.runtime_dir / "session_state.json")
    monkeypatch.setattr(settings, "workspaces_dir", workspace_tmp / "workspaces")
    settings.workspaces_dir.mkdir(parents=True, exist_ok=True)
    session_manager.clear()
    yield
    session_manager.clear()
    monkeypatch.setattr(scheduler_module, "_scheduler", None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def pdf_bytes() -> bytes:
    return b"%PDF-1.4\n%fake paper\n"


@pytest.fixture
def zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
      archive.writestr("main.tex", "\\documentclass{article}\\begin{document}Hello\\end{document}")
    return buffer.getvalue()


@pytest.fixture
def tar_gz_bytes() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        content = b"\\documentclass{article}\\begin{document}Hello\\end{document}"
        info = tarfile.TarInfo(name="main.tex")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()
