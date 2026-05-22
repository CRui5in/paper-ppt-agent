"""Project directory management for SVG-based presentation generation.

Creates and manages the project workspace structure required by the
SVG generation → post-processing → PPTX export pipeline.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from backend.config import CANVAS_FORMATS


def init_project(
    name: str,
    canvas_format: str = "ppt169",
    base_dir: Path = Path("workspaces"),
) -> Path:
    """Create a new project directory with required structure.

    Args:
        name: Project name.
        canvas_format: Canvas format key (e.g., "ppt169").
        base_dir: Parent directory for projects.

    Returns:
        Path to the created project directory.
    """
    now = datetime.now()
    date_str = now.strftime("%Y%m%d")
    timestamp = now.strftime("%H%M%S")
    project_name = f"{name}_{canvas_format}_{date_str}_{timestamp}"
    project_dir = base_dir / project_name
    project_dir.mkdir(parents=True, exist_ok=True)

    # Create required subdirectories
    for subdir in [
        "svg_output",
        "svg_final",
        "images",
        "notes",
        "templates",
        "sources",
        "exports",
    ]:
        (project_dir / subdir).mkdir(exist_ok=True)

    # Write project metadata
    fmt = CANVAS_FORMATS.get(canvas_format, CANVAS_FORMATS["ppt169"])
    readme = project_dir / "README.md"
    readme.write_text(
        f"# {name}\n\n"
        f"- **Format**: {fmt['name']} ({fmt['ratio']})\n"
        f"- **ViewBox**: `{fmt['viewbox']}`\n"
        f"- **Created**: {date_str}\n",
        encoding="utf-8",
    )

    return project_dir


def clone_project_for_refine(
    source_project_dir: Path,
    refine_job_id: str,
    base_dir: Path | None = None,
) -> Path:
    """Clone a completed project into an isolated workspace for refine.

    The cloned workspace keeps the source assets, manuscript, design spec,
    and current SVGs, but starts with a clean ``exports/`` and ``svg_archive/``
    so parallel refine jobs cannot overwrite each other's outputs.
    """
    source_project_dir = Path(source_project_dir)
    if not source_project_dir.exists():
        raise FileNotFoundError(
            f"Source project directory does not exist: {source_project_dir}"
        )

    target_base = base_dir or source_project_dir.parent
    short_id = refine_job_id[:8]
    target_dir = target_base / f"{source_project_dir.name}_refine_{short_id}"

    # If a previous refine job re-used the same short id (unlikely but
    # possible), disambiguate rather than crash.
    suffix = 2
    while target_dir.exists():
        target_dir = target_base / f"{source_project_dir.name}_refine_{short_id}_{suffix}"
        suffix += 1

    shutil.copytree(
        source_project_dir,
        target_dir,
        ignore=shutil.ignore_patterns("exports", "svg_archive", "__pycache__", ".__*"),
    )
    (target_dir / "exports").mkdir(parents=True, exist_ok=True)
    (target_dir / "svg_archive").mkdir(parents=True, exist_ok=True)
    return target_dir


def cleanup_project_dir(project_dir: Path | str | None) -> bool:
    """Best-effort delete of an abandoned project directory.

    Used when a job is cancelled or fails before producing any deliverable
    so the workspace doesn't accumulate orphaned partial runs. Returns
    ``True`` if the directory was deleted, ``False`` otherwise (missing,
    permission denied, etc.). Never raises.
    """
    if not project_dir:
        return False
    path = Path(project_dir)
    if not path.exists() or not path.is_dir():
        return False
    try:
        shutil.rmtree(path, ignore_errors=False)
        return True
    except OSError:
        # Fall back to ignore_errors so we never block the cancel path
        # on a stuck file handle (Windows AV scanners, open SVG previews,
        # etc.).
        shutil.rmtree(path, ignore_errors=True)
        return not path.exists()


def has_deliverable(project_dir: Path | str | None) -> bool:
    """True if *project_dir* contains at least one exported PPTX file.

    Cancel paths use this to decide whether to keep partial work or scrub
    the workspace.
    """
    if not project_dir:
        return False
    path = Path(project_dir)
    exports = path / "exports"
    if not exports.exists():
        return False
    try:
        return any(exports.glob("*.pptx"))
    except OSError:
        return False


def prepare_for_finalize(project_dir: Path) -> None:
    """Copy svg_output/ to svg_final/ in preparation for post-processing.

    Imported templates can reference sibling assets such as
    ``assets/logo.png`` from the generated SVG.  Keep those resources next to
    the copied SVGs so the finalization image-embedding pass can resolve them.
    """
    svg_output = project_dir / "svg_output"
    svg_final = project_dir / "svg_final"

    if svg_final.exists():
        shutil.rmtree(svg_final)
    svg_final.mkdir()

    for svg_file in sorted(svg_output.glob("*.svg")):
        shutil.copy2(svg_file, svg_final / svg_file.name)

    sync_svg_output_assets_to_final(project_dir)


def sync_svg_output_assets_to_final(project_dir: Path) -> None:
    """Copy non-SVG resource directories from svg_output/ to svg_final/.

    This is intentionally separate from :func:`prepare_for_finalize` so older
    workspaces can be repaired during re-export without overwriting edited
    ``svg_final/*.svg`` files.
    """
    svg_output = project_dir / "svg_output"
    svg_final = project_dir / "svg_final"
    if not svg_output.exists() or not svg_final.exists():
        return

    for child in sorted(svg_output.iterdir(), key=lambda path: path.name):
        if child.is_file() or child.suffix.lower() == ".svg":
            continue
        if child.name.startswith(".__"):
            continue
        target = svg_final / child.name
        if target.exists():
            shutil.rmtree(target)
        if child.is_dir():
            shutil.copytree(
                child,
                target,
                ignore=shutil.ignore_patterns(".__*", "__pycache__"),
            )


def get_svg_files(project_dir: Path, source: str = "final") -> list[Path]:
    """Get sorted list of SVG files from a project directory.

    Args:
        project_dir: Project directory path.
        source: "output" or "final".

    Returns:
        Sorted list of SVG file paths.
    """
    svg_dir = project_dir / f"svg_{source}"
    if not svg_dir.exists():
        return []
    return sorted(
        path
        for path in svg_dir.glob("*.svg")
        if not path.name.startswith(".__render_") and not path.name.startswith(".__preview_")
    )


def get_notes(project_dir: Path, svg_files: list[Path]) -> dict[str, str]:
    """Match speaker notes files to SVG files.

    Returns:
        Dict mapping SVG stem to notes markdown content.
    """
    notes_dir = project_dir / "notes"
    if not notes_dir.exists():
        return {}

    notes = {}
    for svg_path in svg_files:
        stem = svg_path.stem
        # Try exact match first
        md_path = notes_dir / f"{stem}.md"
        if md_path.exists():
            notes[stem] = md_path.read_text(encoding="utf-8")
    return notes
