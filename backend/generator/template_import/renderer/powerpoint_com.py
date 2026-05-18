"""PowerPoint COM renderer (Windows-only primary path).

Drives the PowerPoint COM automation interface via PowerShell to export
each slide as ``slide-001.svg, slide-002.svg, ...``. Raises
:class:`RenderError` (``error_kind="render"``) when PowerPoint is
unavailable so the facade can fall back to LibreOffice.
"""

from __future__ import annotations

import platform
import subprocess
import tempfile
from pathlib import Path

from ..types import RenderError


_POWERSHELL_SVG_EXPORT = r"""
param(
    [Parameter(Mandatory = $true)][string]$PptxPath,
    [Parameter(Mandatory = $true)][string]$OutputDir
)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$powerpoint = $null
$presentation = $null
try {
    $powerpoint = New-Object -ComObject PowerPoint.Application
    $powerpoint.Visible = -1
    $presentation = $powerpoint.Presentations.Open($PptxPath, $false, $false, $false)
    foreach ($slide in $presentation.Slides) {
        $fileName = ('slide_{0:D2}.svg' -f $slide.SlideIndex)
        $target = Join-Path $OutputDir $fileName
        $slide.Export($target, 'SVG')
    }
} finally {
    if ($presentation -ne $null) { $presentation.Close() }
    if ($powerpoint -ne $null) { $powerpoint.Quit() }
    [System.GC]::Collect()
    [System.GC]::WaitForPendingFinalizers()
}
"""


def _run_powershell(script: str, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run an inline PowerShell script with the supplied parameters."""
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as f:
        f.write(script)
        script_path = Path(f.name)
    try:
        return subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                *args,
            ],
            capture_output=True,
            check=False,
        )
    finally:
        script_path.unlink(missing_ok=True)


def _render_via_powerpoint_com(pptx: Path, out_dir: Path) -> list[Path]:
    """Export each slide as SVG via PowerPoint COM into ``out_dir``.

    Renames PowerPoint's ``slide_NN.svg`` → canonical ``slide-NNN.svg``
    after export. Returns the rendered files in slide order. Raises
    :class:`RenderError` (``error_kind="render"``) when:

    * the host OS is not Windows,
    * PowerShell or the COM stack rejects the call (non-zero exit),
    * no SVG files end up in ``out_dir``.
    """
    if platform.system() != "Windows":
        raise RenderError(
            "PowerPoint COM export requires Windows",
            error_kind="render",
        )

    pptx = Path(pptx)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        completed = _run_powershell(
            _POWERSHELL_SVG_EXPORT,
            "-PptxPath",
            str(pptx),
            "-OutputDir",
            str(out_dir),
        )
    except FileNotFoundError as exc:
        # ``powershell`` itself missing.
        raise RenderError(
            f"PowerShell unavailable: {exc}",
            error_kind="render",
        ) from exc
    except OSError as exc:
        raise RenderError(
            f"PowerPoint COM invocation failed: {exc}",
            error_kind="render",
        ) from exc

    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RenderError(
            f"PowerPoint SVG export failed: {stderr or 'non-zero exit'}",
            error_kind="render",
        )

    raw = sorted(out_dir.glob("slide_*.svg"))
    if not raw:
        raise RenderError(
            "PowerPoint export completed but no SVG files were found",
            error_kind="render",
        )

    renamed: list[Path] = []
    for index, svg in enumerate(raw, start=1):
        target = out_dir / f"slide-{index:03d}.svg"
        if svg != target:
            if target.exists():
                target.unlink()
            svg.rename(target)
        renamed.append(target)
    return renamed


__all__ = ["_render_via_powerpoint_com"]
