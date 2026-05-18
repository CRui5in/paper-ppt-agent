"""Dual-path PPTX → SVG renderer (PowerPoint COM primary, LibreOffice fallback).

Exposes a single :func:`render` facade that auto-selects the renderer
based on platform availability, records ``export_mode`` and canvas
``(W, H)`` on the result, and writes ``slide_metrics.json`` alongside
the rendered SVG files.
"""

from __future__ import annotations

import platform
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

from ..types import RenderError, RenderResult, SlideMetrics
from .libreoffice import _normalize_libreoffice_filenames, _render_via_libreoffice
from .powerpoint_com import _render_via_powerpoint_com
from .slide_metrics import read_slide_metrics, write_slide_metrics


_DEFAULT_CANVAS = (1280, 720)


def _to_float(value: str | None, fallback: float) -> float:
    if value is None:
        return fallback
    match = re.match(r"-?\d+(?:\.\d+)?", value.strip())
    if not match:
        return fallback
    try:
        return float(match.group(0))
    except ValueError:
        return fallback


def _svg_canvas_size(svg_path: Path) -> tuple[int, int]:
    """Return ``(width, height)`` parsed from the SVG ``viewBox``.

    Falls back to ``width``/``height`` attributes, then to the default
    1280×720 canvas if neither is present or parseable.
    """
    fallback_w, fallback_h = _DEFAULT_CANVAS
    try:
        root = ET.parse(svg_path).getroot()
    except (ET.ParseError, OSError):
        return fallback_w, fallback_h
    view_box = root.attrib.get("viewBox")
    if view_box:
        parts = [p for p in re.split(r"[\s,]+", view_box.strip()) if p]
        if len(parts) == 4:
            return (
                int(round(_to_float(parts[2], fallback_w))),
                int(round(_to_float(parts[3], fallback_h))),
            )
    return (
        int(round(_to_float(root.attrib.get("width"), fallback_w))),
        int(round(_to_float(root.attrib.get("height"), fallback_h))),
    )


def render(
    pptx: Path,
    work_dir: Path,
    *,
    prefer: Literal["auto", "com", "libreoffice"] = "auto",
) -> RenderResult:
    """Render a PPTX into per-slide SVG files plus ``slide_metrics.json``.

    Strategy when ``prefer="auto"``:

      1. On Windows, try PowerPoint COM first.
      2. Fall back to LibreOffice headless.
      3. If both fail, raise :class:`RenderError`
         (``error_kind="render"``, ``reason="no renderer available"``).

    ``prefer="com"`` and ``prefer="libreoffice"`` short-circuit the
    fallback. The first SVG's ``viewBox`` provides the canvas
    dimensions (default 1280×720); per-slide :class:`SlideMetrics` are
    written next to the SVG output via
    :func:`slide_metrics.write_slide_metrics`.
    """
    pptx_path = Path(pptx)
    out_dir = Path(work_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    svg_files: list[Path]
    export_mode: Literal["powerpoint-com", "libreoffice"]

    if prefer == "com":
        svg_files = _render_via_powerpoint_com(pptx_path, out_dir)
        export_mode = "powerpoint-com"
    elif prefer == "libreoffice":
        svg_files = _render_via_libreoffice(pptx_path, out_dir)
        export_mode = "libreoffice"
    else:
        # ``auto`` — prefer COM on Windows, then LibreOffice anywhere.
        primary_error: RenderError | None = None
        if platform.system() == "Windows":
            try:
                svg_files = _render_via_powerpoint_com(pptx_path, out_dir)
                export_mode = "powerpoint-com"
            except RenderError as exc:
                primary_error = exc
                svg_files = []  # type: ignore[assignment]
        if not (platform.system() == "Windows" and primary_error is None):
            try:
                svg_files = _render_via_libreoffice(pptx_path, out_dir)
                export_mode = "libreoffice"
            except RenderError as fallback_error:
                reason = "no renderer available"
                if primary_error:
                    reason = (
                        f"{reason}: powerpoint-com={primary_error.reason}; "
                        f"libreoffice={fallback_error.reason}"
                    )
                else:
                    reason = f"{reason}: libreoffice={fallback_error.reason}"
                raise RenderError(reason, error_kind="render") from fallback_error

    if not svg_files:
        raise RenderError("renderer produced no SVG files", error_kind="render")

    canvas_w, canvas_h = _svg_canvas_size(svg_files[0])
    rendered_at = time.time()
    metrics = [
        SlideMetrics(
            slide_index=i + 1,
            canvas_width=canvas_w,
            canvas_height=canvas_h,
            font_substitutions={},
            rendered_at=rendered_at,
        )
        for i in range(len(svg_files))
    ]
    write_slide_metrics(out_dir, metrics)

    return RenderResult(
        svg_files=list(svg_files),
        slide_metrics=metrics,
        export_mode=export_mode,
        canvas=(canvas_w, canvas_h),
    )


__all__ = [
    "render",
    "read_slide_metrics",
    "write_slide_metrics",
    "_render_via_powerpoint_com",
    "_render_via_libreoffice",
    "_normalize_libreoffice_filenames",
]
