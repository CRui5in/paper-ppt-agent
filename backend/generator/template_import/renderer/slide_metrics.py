"""Persist per-slide rendering metrics consumed by downstream modules.

Replaces ``viewBox`` reverse-engineering with a stable
``slide_metrics.json`` artifact containing canvas size, font
substitutions, and render timestamps, written next to the SVG output.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..types import SlideMetrics

_FILENAME = "slide_metrics.json"


def write_slide_metrics(out_dir: Path, metrics: list[SlideMetrics]) -> Path:
    """Write ``out_dir / slide_metrics.json`` and return its path.

    Each :class:`SlideMetrics` entry is serialized with ``slide_index``,
    ``canvas_width``, ``canvas_height``, ``font_substitutions``, and
    ``rendered_at``. The JSON is emitted with ``sort_keys=True``,
    ``ensure_ascii=False``, compact separators, and a trailing newline so
    repeated writes are byte-stable (Requirement 14.2).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "slide_index": int(m.slide_index),
            "canvas_width": int(m.canvas_width),
            "canvas_height": int(m.canvas_height),
            "font_substitutions": dict(m.font_substitutions),
            "rendered_at": float(m.rendered_at),
        }
        for m in metrics
    ]
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
    target = out_dir / _FILENAME
    target.write_text(text, encoding="utf-8")
    return target


def read_slide_metrics(out_dir: Path) -> list[SlideMetrics]:
    """Read ``out_dir / slide_metrics.json`` back into typed dataclasses.

    Returns an empty list if the file is missing. Unknown / forward-
    compatible fields are silently ignored.
    """
    path = Path(out_dir) / _FILENAME
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    result: list[SlideMetrics] = []
    for entry in raw:
        result.append(
            SlideMetrics(
                slide_index=int(entry["slide_index"]),
                canvas_width=int(entry["canvas_width"]),
                canvas_height=int(entry["canvas_height"]),
                font_substitutions=dict(entry.get("font_substitutions") or {}),
                rendered_at=float(entry.get("rendered_at", 0.0)),
            )
        )
    return result


__all__ = ["write_slide_metrics", "read_slide_metrics"]
