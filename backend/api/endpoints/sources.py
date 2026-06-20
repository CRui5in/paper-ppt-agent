"""Multi-source API endpoints.

NotebookLM-style sources panel contract. One session owns an ordered list of
Sources (pdf / text / markdown / url). The endpoints here are the wire surface
for the frontend Sources panel (see Layer 7).

Endpoints
---------
POST   /api/sources                  create an empty sources group → session_id
POST   /api/sources/{sid}/files      append one or more uploaded files
POST   /api/sources/{sid}/text       append pasted text
POST   /api/sources/{sid}/url        append a URL (fetched + validated now)
GET    /api/sources/{sid}            list all sources for the session
DELETE /api/sources/{sid}/{src_id}   remove one source (and its upload bytes)

The legacy ``POST /api/upload`` endpoint keeps working: it builds a single-file
session via the same manager, so old clients and the old frontend path are
unaffected.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from backend.api.schemas import (
    AddTextSourceRequest,
    AddUrlSourceRequest,
    AddUrlSourceResponse,
    BrowserInstallStatusResponse,
    SourceItem,
    SourcePreviewResponse,
    SourcesGroupResponse,
)
from backend.config import settings
from backend.parser.source_model import Source
from backend.parser.url_parser import UrlFetchError, fetch_url_html
from backend.runtime import aensure_dir, aoffload, awrite_bytes, awrite_text
from backend.runtime.web_browser import get_url_browser_install_status
from backend.session.manager import session_manager

router = APIRouter()


# ── file-kind detection (mirrors upload.py but widened for md/txt) ───────────

_FILE_KIND_BY_SUFFIX = {
    ".pdf": "pdf",
    ".tex": "latex",
    ".zip": "latex",
    ".tgz": "latex",
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
}
_FILE_KIND_BY_SUFFIX_PATTERN = {".tar.gz": "latex"}


def detect_file_kind(filename: str | None) -> tuple[str | None, str]:
    lower = (filename or "").lower()
    for suffix, kind in _FILE_KIND_BY_SUFFIX_PATTERN.items():
        if lower.endswith(suffix):
            return kind, suffix
    suffix = Path(lower).suffix.lower()
    return _FILE_KIND_BY_SUFFIX.get(suffix), suffix


# ── helpers ──────────────────────────────────────────────────────────────────


def _upload_dir(session_id: str) -> Path:
    """Per-session upload root: ``workspaces/uploads/<session_id>/``."""
    return settings.workspaces_dir / "uploads" / session_id


def _source_to_item(src: Source) -> SourceItem:
    char_count = src.char_count
    if not char_count and src.raw_text:
        char_count = len(src.raw_text)
    return SourceItem(
        id=src.id,
        kind=src.kind,
        label=src.label,
        role=src.role,
        order=src.order,
        file_size=src.file_size,
        url=src.url,
        parse_status=src.parse_status,
        parse_error=src.parse_error,
        char_count=char_count,
    )


def _group_response(session_id: str) -> SourcesGroupResponse:
    sources = session_manager.get_sources(session_id)
    return SourcesGroupResponse(
        session_id=session_id,
        sources=[_source_to_item(s) for s in sources],
    )


def _enforce_capacity(session_id: str, adding: int) -> None:
    current = len(session_manager.get_sources(session_id))
    if current + adding > settings.max_sources_per_session:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"A session supports at most {settings.max_sources_per_session} "
                f"sources (current: {current}, attempted to add: {adding})."
            ),
        )


def _require_session(session_id: str):
    session = session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id!r} not found.",
        )
    return session


def _new_source_id() -> str:
    return uuid.uuid4().hex[:12]


def _require_source(session_id: str, source_id: str) -> Source:
    _require_session(session_id)
    source = next(
        (item for item in session_manager.get_sources(session_id) if item.id == source_id),
        None,
    )
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Source {source_id!r} not found.",
        )
    return source


async def _read_preview_text(path: Path) -> tuple[str, bool]:
    limit = max(1, settings.source_preview_max_chars)

    def _read() -> tuple[str, bool]:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            content = handle.read(limit + 1)
        return content[:limit], len(content) > limit

    return await aoffload(_read)


# ── endpoints ────────────────────────────────────────────────────────────────


@router.post("/sources", response_model=SourcesGroupResponse, status_code=status.HTTP_201_CREATED)
async def create_sources_group() -> SourcesGroupResponse:
    """Create an empty multi-source session.

    A placeholder file_path/source_type are recorded so the legacy Session
    fields stay populated; the real inputs live in ``session.sources``.
    """
    session_id = _new_source_id()
    upload_dir = _upload_dir(session_id)
    await aensure_dir(upload_dir)
    # Sentinel file path: nothing to parse yet, but Session requires a Path.
    session_manager.create_session(
        file_path=upload_dir,
        source_type="mixed",
        file_name="",
        file_size=0,
        session_id=session_id,
        sources=[],
    )
    return SourcesGroupResponse(session_id=session_id, sources=[])


@router.post("/sources/{session_id}/files", response_model=SourcesGroupResponse)
async def add_file_sources(
    session_id: str,
    files: list[UploadFile] = File(...),
) -> SourcesGroupResponse:
    _require_session(session_id)
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No files provided.",
        )
    _enforce_capacity(session_id, len(files))

    upload_dir = _upload_dir(session_id)
    await aensure_dir(upload_dir)
    for upload in files:
        kind, suffix = detect_file_kind(upload.filename)
        if kind is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Unsupported file type for {upload.filename!r}. "
                    "Use .pdf, .tex, .zip, .tgz, .tar.gz, .md, or .txt."
                ),
            )
        content = await upload.read()
        if not content:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{upload.filename!r} is empty.",
            )
        if len(content) > settings.max_upload_bytes:
            max_mb = settings.max_upload_bytes // (1024 * 1024)
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"{upload.filename!r} exceeds the {max_mb} MB limit.",
            )

        source_id = _new_source_id()
        # Per-source subdir keeps each source's extracted assets isolated (the
        # merger relies on this for figure-id namespacing).
        source_dir = upload_dir / source_id
        await aensure_dir(source_dir)
        file_path = source_dir / (upload.filename or f"source{suffix}")
        await awrite_bytes(file_path, content)

        source = Source(
            id=source_id,
            kind=kind,  # type: ignore[arg-type]
            label=upload.filename or file_path.name,
            file_path=file_path,
            file_size=len(content),
            char_count=(
                len(content.decode("utf-8-sig", errors="replace"))
                if kind in {"text", "markdown"}
                else 0
            ),
        )
        session_manager.add_source(session_id, source)

    return _group_response(session_id)


@router.post("/sources/{session_id}/text", response_model=SourcesGroupResponse)
async def add_text_source(
    session_id: str,
    payload: AddTextSourceRequest,
) -> SourcesGroupResponse:
    _require_session(session_id)
    _enforce_capacity(session_id, 1)

    text = payload.text.strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pasted text is empty.",
        )
    source_id = _new_source_id()
    source_dir = _upload_dir(session_id) / source_id
    await aensure_dir(source_dir)
    label = payload.label.strip() or f"Pasted text {source_id}"
    source = Source(
        id=source_id,
        kind="text",
        label=label,
        role=payload.role,
        raw_text=text,
        file_size=len(text.encode("utf-8")),
        char_count=len(text),
    )
    session_manager.add_source(session_id, source)
    return _group_response(session_id)


@router.post("/sources/{session_id}/url", response_model=AddUrlSourceResponse)
async def add_url_source(
    session_id: str,
    payload: AddUrlSourceRequest,
) -> AddUrlSourceResponse:
    """Add a URL source. The page is fetched + validated immediately so a bad
    URL fails here (HTTP 400) rather than 20 minutes into a pipeline run.

    The extracted markdown is cached on disk under the session upload dir so
    the parser doesn't re-fetch during generation.
    """
    _require_session(session_id)
    _enforce_capacity(session_id, 1)

    try:
        result = await fetch_url_html(payload.url)
    except UrlFetchError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    source_id = _new_source_id()
    source_dir = _upload_dir(session_id) / source_id
    await aensure_dir(source_dir)
    # Cache the extracted markdown; the UrlParser reads it back at parse time
    # instead of re-fetching (network may be flaky / page may change).
    await awrite_text(source_dir / "page.txt", result.markdown, encoding="utf-8")
    await awrite_text(source_dir / "page.html", result.html, encoding="utf-8")

    label = payload.label.strip() if payload.label else (result.title or result.url)
    char_count = len(result.markdown)
    source = Source(
        id=source_id,
        kind="url",
        label=label,
        role=payload.role,
        url=result.final_url,
        raw_text=result.markdown,
        file_size=len(result.markdown.encode("utf-8")),
        parse_status="ok",
        char_count=char_count,
    )
    session_manager.add_source(session_id, source)

    return AddUrlSourceResponse(
        source=_source_to_item(source),
        title=result.title,
        char_count=char_count,
    )


@router.get("/sources/{session_id}", response_model=SourcesGroupResponse)
async def get_sources_group(session_id: str) -> SourcesGroupResponse:
    _require_session(session_id)
    return _group_response(session_id)


@router.get(
    "/sources/runtime/browser-status",
    response_model=BrowserInstallStatusResponse,
)
async def get_browser_install_status() -> BrowserInstallStatusResponse:
    current = get_url_browser_install_status()
    return BrowserInstallStatusResponse(
        state=current.state,
        progress=current.progress,
        message=current.message,
    )


@router.get(
    "/sources/{session_id}/{source_id}/preview",
    response_model=SourcePreviewResponse,
)
async def get_source_preview(
    session_id: str,
    source_id: str,
) -> SourcePreviewResponse:
    source = _require_source(session_id, source_id)
    if source.kind == "pdf" and source.file_path is not None:
        return SourcePreviewResponse(
            source_id=source.id,
            title=source.label,
            preview_type="pdf",
            file_url=f"/api/sources/{session_id}/{source_id}/file",
            mime_type="application/pdf",
        )
    if source.kind == "url" or (source.kind == "text" and source.raw_text is not None):
        content = source.raw_text or ""
        limit = max(1, settings.source_preview_max_chars)
        truncated = len(content) > limit
        return SourcePreviewResponse(
            source_id=source.id,
            title=source.label,
            preview_type="text",
            content=content[:limit],
            mime_type="text/plain",
            truncated=truncated,
        )
    if source.kind in {"text", "markdown"} and source.file_path is not None:
        content, truncated = await _read_preview_text(source.file_path)
        return SourcePreviewResponse(
            source_id=source.id,
            title=source.label,
            preview_type="text",
            content=content,
            mime_type="text/markdown" if source.kind == "markdown" else "text/plain",
            truncated=truncated,
        )
    return SourcePreviewResponse(
        source_id=source.id,
        title=source.label,
        preview_type="unsupported",
    )


@router.get("/sources/{session_id}/{source_id}/file")
async def get_source_preview_file(session_id: str, source_id: str) -> FileResponse:
    source = _require_source(session_id, source_id)
    if source.kind != "pdf" or source.file_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This source has no directly previewable file.",
        )
    path = source.file_path.resolve()
    try:
        path.relative_to(_upload_dir(session_id).resolve())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Source file is outside the session upload directory.",
        ) from exc
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Source file no longer exists.",
        )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=source.label,
        content_disposition_type="inline",
        headers={"Cache-Control": "no-store"},
    )


@router.delete("/sources/{session_id}/{source_id}", response_model=SourcesGroupResponse)
async def remove_source(session_id: str, source_id: str) -> SourcesGroupResponse:
    _require_session(session_id)
    session, removed = session_manager.remove_source(session_id, source_id)
    if removed is not None and removed.file_path is not None:
        # Best-effort cleanup of the source's upload subdir. Never fails the
        # request — the session is already updated.
        source_dir = Path(removed.file_path).parent
        if source_dir.is_dir() and source_dir != _upload_dir(session_id):
            try:
                import shutil

                shutil.rmtree(source_dir, ignore_errors=True)
            except OSError:
                pass
    return _group_response(session_id)
