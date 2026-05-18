"""LibreOffice headless renderer (cross-platform fallback path).

Invokes ``soffice --headless --convert-to svg --outdir <out> <pptx>``,
then normalizes the LibreOffice file naming
(``<stem>.svg, <stem>-1.svg, …``) into ``slide-001.svg`` ordering.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from ..types import RenderError


_TIMEOUT_SECONDS = 120


def _resolve_soffice() -> str | None:
    """Locate the ``soffice`` binary via env override or ``PATH``."""
    override = os.environ.get("LIBREOFFICE_BIN")
    if override:
        if Path(override).exists():
            return override
        # Also accept a bare command on PATH (e.g. "soffice").
        on_path = shutil.which(override)
        if on_path:
            return on_path
    return shutil.which("soffice")


def _render_via_libreoffice(pptx: Path, out_dir: Path) -> list[Path]:
    """Convert ``pptx`` to per-slide SVG files via LibreOffice headless.

    Returns the renamed slide files in slide order. Raises
    :class:`RenderError` (``error_kind="render"``) when ``soffice`` is
    missing, exits non-zero, times out, or produces no output.
    """
    pptx = Path(pptx)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    soffice = _resolve_soffice()
    if not soffice:
        raise RenderError("soffice not found", error_kind="render")

    cmd = [
        soffice,
        "--headless",
        "--convert-to",
        "svg",
        "--outdir",
        str(out_dir),
        str(pptx),
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderError(
            f"LibreOffice export timed out after {_TIMEOUT_SECONDS}s",
            error_kind="render",
        ) from exc
    except OSError as exc:
        raise RenderError(
            f"LibreOffice invocation failed: {exc}",
            error_kind="render",
        ) from exc

    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RenderError(
            f"LibreOffice export failed: {stderr or 'non-zero exit'}",
            error_kind="render",
        )

    files = _normalize_libreoffice_filenames(out_dir, pptx.stem)
    if not files:
        stderr = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RenderError(
            f"LibreOffice export produced no SVG files: {stderr or 'unknown'}",
            error_kind="render",
        )
    return files


def _normalize_libreoffice_filenames(out_dir: Path, stem: str) -> list[Path]:
    """Rename ``<stem>.svg, <stem>-1.svg, …`` → ``slide-001.svg, ...``.

    Some LibreOffice builds emit a single ``<stem>.svg`` for multi-page
    decks (one combined SVG); others emit one file per slide with the
    suffix scheme ``<stem>.svg`` (slide 1) plus ``<stem>-N.svg`` for the
    remainder. Both layouts are normalized into a contiguous
    ``slide-001.svg`` sequence in slide order.
    """
    out_dir = Path(out_dir)
    base = out_dir / f"{stem}.svg"
    pattern = re.compile(re.escape(stem) + r"-(\d+)\.svg$")

    suffixed: list[tuple[int, Path]] = []
    for path in out_dir.iterdir():
        if not path.is_file():
            continue
        match = pattern.match(path.name)
        if match:
            suffixed.append((int(match.group(1)), path))
    suffixed.sort(key=lambda item: item[0])

    ordered: list[Path] = []
    if base.exists():
        ordered.append(base)
    ordered.extend(path for _, path in suffixed)

    if not ordered:
        return []

    # Rename to canonical ``slide-NNN.svg`` via a two-phase shuffle so we
    # never collide with an existing source file mid-rename.
    renamed: list[Path] = []
    for index, source in enumerate(ordered, start=1):
        tmp = out_dir / f".__pending_slide_{index:03d}.svg"
        if tmp.exists():
            tmp.unlink()
        source.rename(tmp)
        renamed.append(tmp)

    final: list[Path] = []
    for index, tmp in enumerate(renamed, start=1):
        target = out_dir / f"slide-{index:03d}.svg"
        if target.exists():
            target.unlink()
        tmp.rename(target)
        final.append(target)
    return final


__all__ = ["_render_via_libreoffice", "_normalize_libreoffice_filenames"]
