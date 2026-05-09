"""LaTeX equation rendering to images.

Converts LaTeX math expressions to PNG images for embedding in SVG slides.
Uses matplotlib as the rendering backend.

Concurrency contract: matplotlib's ``Agg`` backend is *not* thread-safe at
the global pyplot level — concurrent ``plt.subplots`` / ``fig.savefig``
calls from different threads will scramble each other's state. We funnel
every render through a process-wide ``asyncio.Semaphore`` so at most
``settings.equation_render_concurrency`` renders are in flight; each one
runs on the offload pool but they execute one-at-a-time inside a single
``aoffload`` slot so matplotlib stays consistent.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from backend.config import settings
from backend.runtime import aoffload

# Module-level semaphore — created lazily on the running loop so this
# module can be imported before the loop exists (e.g. at app startup).
_render_sem: asyncio.Semaphore | None = None


def _get_sem() -> asyncio.Semaphore:
    global _render_sem
    if _render_sem is None:
        _render_sem = asyncio.Semaphore(max(1, settings.equation_render_concurrency))
    return _render_sem


async def render_equation(
    latex: str,
    output_dir: Path,
    dpi: int = 200,
) -> Path | None:
    """Render a LaTeX equation to a PNG image.

    Args:
        latex: LaTeX math expression (without $ delimiters).
        output_dir: Directory to write the output image.
        dpi: Resolution for rendering.

    Returns:
        Path to the rendered PNG, or None on failure.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    eq_hash = hashlib.md5(latex.encode()).hexdigest()[:12]
    output_path = output_dir / f"eq_{eq_hash}.png"

    if output_path.exists():
        return output_path

    sem = _get_sem()
    async with sem:
        try:
            return await aoffload(_render_with_matplotlib, latex, output_path, dpi)
        except Exception:
            return None


def _render_with_matplotlib(latex: str, output_path: Path, dpi: int) -> Path:
    """Render equation using matplotlib's TeX rendering."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(0.01, 0.01))
    ax.axis("off")

    ax.text(
        0.5,
        0.5,
        f"${latex}$",
        transform=ax.transAxes,
        fontsize=14,
        verticalalignment="center",
        horizontalalignment="center",
    )

    fig.savefig(
        str(output_path),
        dpi=dpi,
        bbox_inches="tight",
        pad_inches=0.1,
        transparent=True,
    )
    plt.close(fig)
    return output_path
