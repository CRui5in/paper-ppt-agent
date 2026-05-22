"""PPTX template importer with a review-first workflow.

The importer renders PPTX slides to SVG, extracts reusable asset candidates from
those SVGs, persists a review draft, and only writes a real layout template after
the user confirms the draft.  It can optionally run a text LLM over extracted
structure to improve page/asset decisions and generate design_spec.md.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import math
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from xml.etree import ElementTree as ET

from pydantic import BaseModel, Field, ValidationError

from backend.config import settings
from backend.llm.registry import create_provider
from backend.llm.types import LLMMessage

from .template_import.externalize_images import externalize_svg_batch
from .template_import.manifest import build_manifest
from .template_import.optimize_reference import optimize_reference_batch

logger = logging.getLogger(__name__)

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)

PAGE_TYPES = ("cover", "toc", "chapter", "content", "ending")
ASSET_ROLES = ("logo", "background", "decoration", "content_image", "ignore")
AssetRole = Literal["logo", "background", "decoration", "content_image", "ignore"]

PAGE_TYPE_FILES = {
    "cover": "01_cover.svg",
    "toc": "02_toc.svg",
    "chapter": "02_chapter.svg",
    "content": "03_content.svg",
    "ending": "04_ending.svg",
}

PAGE_TYPE_MAP = {
    "cover_candidate": "cover",
    "chapter_candidate": "chapter",
    "toc_candidate": "toc",
    "ending_candidate": "ending",
    "content_candidate": "content",
}

IMPORT_STEPS = [
    ("uploaded", "Upload received"),
    ("analyzing", "Analyze PPTX structure"),
    ("rendering", "Render slides to SVG"),
    ("detecting_assets", "Detect reusable assets"),
    ("baseline_preview", "Generate rule template draft"),
    ("llm_review", "Analyze template with LLM"),
    ("review", "Wait for review"),
    ("registering", "Register template"),
]

_import_tasks: dict[str, dict[str, Any]] = {}
_import_lock = threading.Lock()


class LLMPageSelections(BaseModel):
    cover: int | None = None
    toc: int | None = None
    chapter: int | None = None
    content: int | None = None
    ending: int | None = None


class LLMAssetDecision(BaseModel):
    asset_id: str
    role: AssetRole
    name: str | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reason: str = ""


class LLMPlaceholderDecision(BaseModel):
    page_type: Literal["cover", "toc", "chapter", "content", "ending"]
    placeholder: str
    source_text: str | None = None


class LLMElementAction(BaseModel):
    page_type: Literal["cover", "toc", "chapter", "content", "ending"]
    element_id: str
    action: Literal["keep", "remove", "replace_with_placeholder"]
    placeholder: str | None = None
    reason: str = ""


class LLMTemplateImportPlan(BaseModel):
    label: str | None = None
    page_selections: LLMPageSelections = Field(default_factory=LLMPageSelections)
    asset_decisions: list[LLMAssetDecision] = Field(default_factory=list)
    placeholder_decisions: list[LLMPlaceholderDecision] = Field(default_factory=list)
    element_actions: list[LLMElementAction] = Field(default_factory=list)
    preserve_texts: list[str] = Field(default_factory=list)
    design_spec_md: str = ""
    notes: list[str] = Field(default_factory=list)


@dataclass
class ImportResult:
    template_id: str = ""
    label: str = ""
    status: str = "processing"
    stage: str = "uploaded"
    progress: float = 0.0
    message: str = ""
    steps: list[dict[str, str]] = field(default_factory=list)
    review_required: bool = False
    export_mode: str = ""
    slide_count: int = 0
    cover_svg: str = ""
    content_svg: str = ""
    theme_colors: list[str] = field(default_factory=list)
    error: str = ""


# ── Paths and JSON state ─────────────────────────────────────────────────────


def _templates_root() -> Path:
    return settings.templates_dir / "layouts"


def _user_index_path() -> Path:
    return _templates_root() / "user_templates.json"


def _imports_root() -> Path:
    return settings.workspaces_dir / "template_imports"


def _import_dir(import_id: str) -> Path:
    return _imports_root() / import_id


def _state_path(import_id: str) -> Path:
    return _import_dir(import_id) / "state.json"


def _review_path(import_id: str) -> Path:
    return _import_dir(import_id) / "review.json"


def _work_dir(import_id: str) -> Path:
    return _import_dir(import_id) / "work"


def _review_manifest_path(import_id: str) -> Path:
    """Canonical sidecar for heavy slide/element review data."""
    return _import_dir(import_id) / "review_manifest.json"


def _read_review_manifest(import_id: str) -> dict[str, Any]:
    """Read the heavy review sidecar with compatibility for older imports.

    Early sidecar externalization wrote ``review_manifest.json`` under
    ``work/`` while several readers looked next to ``review.json``. Keep both
    locations readable, but standardize new writes at the import root.
    """
    for path in (_review_manifest_path(import_id), _work_dir(import_id) / "review_manifest.json"):
        manifest = _safe_read_json(path)
        if manifest:
            return manifest
    return {}


def _safe_read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _default_steps(active_stage: str = "uploaded") -> list[dict[str, str]]:
    seen_active = False
    steps = []
    for step_id, label in IMPORT_STEPS:
        if step_id == active_stage:
            status = "active"
            seen_active = True
        elif not seen_active:
            status = "complete"
        else:
            status = "pending"
        steps.append({"id": step_id, "label": label, "status": status})
    return steps


def _update_state(import_id: str, **updates: Any) -> dict[str, Any]:
    state = _safe_read_json(_state_path(import_id))
    stage = str(updates.get("stage") or state.get("stage") or "uploaded")
    state.setdefault("import_id", import_id)
    state.setdefault("status", "processing")
    state.setdefault("created_at", time.time())
    state["updated_at"] = time.time()
    state.update(updates)
    state["stage"] = stage
    state["steps"] = _default_steps(stage)
    if state.get("status") in {"review_required", "complete", "error"}:
        for step in state["steps"]:
            if state["status"] == "error" and step["id"] == stage:
                step["status"] = "error"
            elif state["status"] == "review_required" and step["id"] == "review":
                step["status"] = "active"
            elif state["status"] == "complete":
                step["status"] = "complete"
    _write_json(_state_path(import_id), state)
    with _import_lock:
        _import_tasks[import_id] = state
    return state


def initialize_import_task(import_id: str, pptx_path: Path, label: str | None = None) -> dict[str, Any]:
    """Create the initial persistent import state."""
    template_id = _generate_template_id(pptx_path.name, import_id)
    return _update_state(
        import_id,
        status="processing",
        stage="uploaded",
        progress=0.02,
        message="Upload received.",
        review_required=False,
        template_id=template_id,
        label=label or _label_from_name(pptx_path.name),
        source_file=str(pptx_path),
        slide_count=0,
        export_mode="",
        error=None,
    )


def set_import_collaboration_mode(import_id: str, mode: str) -> dict[str, Any]:
    """Persist whether this import is owned by classic LLM, Agent, or direct flow."""
    normalized = mode if mode in {"agent", "direct"} else "classic"
    return _update_state(import_id, collaboration_mode=normalized)


def mark_import_ready_for_review(
    import_id: str,
    *,
    message: str = "Review page types and reusable assets before registering the template.",
) -> dict[str, Any]:
    """Move an already-built review draft into the review stage without LLM analysis."""
    review = get_import_review(import_id)
    if review is None:
        raise FileNotFoundError(import_id)
    review["status"] = "review_required"
    review.setdefault("conversation", [])
    _write_json(_review_path(import_id), review)
    return _update_state(
        import_id,
        status="review_required",
        stage="review",
        progress=0.9,
        message=message,
        review_required=True,
        label=review.get("draft", {}).get("label") or review.get("label"),
        theme_colors=review.get("theme_colors", []),
        error=None,
    )


def get_import_task(import_id: str) -> dict[str, Any] | None:
    """Get persistent status for an import task."""
    state = _safe_read_json(_state_path(import_id))
    if state:
        return state
    with _import_lock:
        return _import_tasks.get(import_id)


def get_import_result(import_id: str) -> ImportResult | None:
    state = get_import_task(import_id)
    return _result_from_state(state) if state else None


def _result_from_state(state: dict[str, Any]) -> ImportResult:
    return ImportResult(
        template_id=state.get("template_id") or "",
        label=state.get("label") or "",
        status=state.get("status") or "processing",
        stage=state.get("stage") or "uploaded",
        progress=float(state.get("progress") or 0),
        message=state.get("message") or "",
        steps=state.get("steps") or [],
        review_required=bool(state.get("review_required")),
        export_mode=state.get("export_mode") or "",
        slide_count=int(state.get("slide_count") or 0),
        theme_colors=state.get("theme_colors") or [],
        error=state.get("error") or "",
    )


# ── IDs and labels ───────────────────────────────────────────────────────────


def _label_from_name(pptx_name: str) -> str:
    return Path(pptx_name).stem.replace("_", " ").replace("-", " ").title()


def _generate_template_id(pptx_name: str, import_id: str | None = None) -> str:
    stem = Path(pptx_name).stem
    safe = "".join(c if c.isalnum() else "_" for c in stem.lower()).strip("_") or "template"
    digest_seed = f"{stem}:{import_id or ''}"
    short_hash = hashlib.sha1(digest_seed.encode("utf-8")).hexdigest()[:6]
    return f"user_{safe}_{short_hash}"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _to_float(value: str | None, default: float = 0.0) -> float:
    if not value:
        return default
    try:
        return float(str(value).replace("px", ""))
    except ValueError:
        return default


def _float_values(value: str | None) -> list[float]:
    if not value:
        return []
    values = []
    for item in re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", str(value).replace("px", "")):
        try:
            values.append(float(item))
        except ValueError:
            continue
    return values


def _multiply_matrix(
    left: tuple[float, float, float, float, float, float],
    right: tuple[float, float, float, float, float, float],
) -> tuple[float, float, float, float, float, float]:
    la, lb, lc, ld, le, lf = left
    ra, rb, rc, rd, re_, rf = right
    return (
        la * ra + lc * rb,
        lb * ra + ld * rb,
        la * rc + lc * rd,
        lb * rc + ld * rd,
        la * re_ + lc * rf + le,
        lb * re_ + ld * rf + lf,
    )


def _parse_svg_transform(transform: str | None) -> tuple[float, float, float, float, float, float]:
    matrix: tuple[float, float, float, float, float, float] = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    if not transform:
        return matrix
    for name, args in re.findall(r"([a-zA-Z]+)\(([^)]*)\)", transform):
        values = _float_values(args)
        op = name.lower()
        next_matrix = matrix
        if op == "matrix" and len(values) >= 6:
            next_matrix = (values[0], values[1], values[2], values[3], values[4], values[5])
        elif op == "translate" and values:
            tx = values[0]
            ty = values[1] if len(values) > 1 else 0.0
            next_matrix = (1.0, 0.0, 0.0, 1.0, tx, ty)
        elif op == "scale" and values:
            sx = values[0]
            sy = values[1] if len(values) > 1 else sx
            next_matrix = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        elif op == "rotate" and values:
            angle = math.radians(values[0])
            cos_v = math.cos(angle)
            sin_v = math.sin(angle)
            rotate = (cos_v, sin_v, -sin_v, cos_v, 0.0, 0.0)
            if len(values) >= 3:
                cx, cy = values[1], values[2]
                next_matrix = _multiply_matrix(
                    _multiply_matrix((1.0, 0.0, 0.0, 1.0, cx, cy), rotate),
                    (1.0, 0.0, 0.0, 1.0, -cx, -cy),
                )
            else:
                next_matrix = rotate
        matrix = _multiply_matrix(matrix, next_matrix)
    return matrix


def _apply_svg_matrix(
    matrix: tuple[float, float, float, float, float, float],
    x: float,
    y: float,
) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return a * x + c * y + e, b * x + d * y + f


def _matrix_scale(matrix: tuple[float, float, float, float, float, float]) -> tuple[float, float]:
    a, b, c, d, _e, _f = matrix
    scale_x = math.hypot(a, b) or 1.0
    scale_y = math.hypot(c, d) or scale_x
    return scale_x, scale_y


def _svg_canvas_size(svg_path: Path, fallback_w: int = 1280, fallback_h: int = 720) -> tuple[int, int]:
    try:
        root = ET.parse(svg_path).getroot()
    except ET.ParseError:
        return fallback_w, fallback_h
    view_box = root.attrib.get("viewBox")
    if view_box:
        parts = [p for p in re.split(r"[\s,]+", view_box.strip()) if p]
        if len(parts) == 4:
            return int(round(_to_float(parts[2], fallback_w))), int(round(_to_float(parts[3], fallback_h)))
    return (
        int(round(_to_float(root.attrib.get("width"), fallback_w))),
        int(round(_to_float(root.attrib.get("height"), fallback_h))),
    )


def _element_text_content(element: ET.Element) -> str:
    return " ".join(part.strip() for part in element.itertext() if part and part.strip())


def _normalize_text_candidate(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) > 120:
        return ""
    if not text or re.fullmatch(r"[\d\s./:_-]+", text):
        return ""
    if "{{" in text and "}}" in text:
        return ""
    return text


def _text_occurrences(svg_files: list[Path]) -> dict[str, list[dict[str, float | int]]]:
    occurrences: dict[str, list[dict[str, float | int]]] = {}
    for svg_path in svg_files:
        match = re.search(r"slide_(\d+)\.svg$", svg_path.name)
        slide_index = int(match.group(1)) if match else 0
        canvas_w, canvas_h = _svg_canvas_size(svg_path)
        try:
            root = ET.parse(svg_path).getroot()
        except ET.ParseError:
            continue
        seen_on_slide: set[str] = set()
        for element in root.iter():
            if _local_name(element.tag) != "text":
                continue
            candidate = _text_candidate(element)
            text = candidate["norm"]
            if not text or text in seen_on_slide:
                continue
            seen_on_slide.add(text)
            occurrences.setdefault(text, []).append(
                {
                    "slide_index": slide_index,
                    "x": round(float(candidate["x"]), 2),
                    "y": round(float(candidate["y"]), 2),
                    "canvas_w": canvas_w,
                    "canvas_h": canvas_h,
                }
            )
    return occurrences


def _repeated_text_items(svg_files: list[Path], *, min_pages: int = 2, max_items: int = 80) -> list[dict[str, Any]]:
    items = []
    for text, records in _text_occurrences(svg_files).items():
        pages = sorted({int(record["slide_index"]) for record in records})
        if len(pages) < min_pages:
            continue
        edge_hits = 0
        for record in records:
            y = float(record["y"])
            canvas_h = float(record["canvas_h"] or 720)
            if y <= canvas_h * 0.18 or y >= canvas_h * 0.86:
                edge_hits += 1
        if edge_hits < max(1, len(pages) // 2):
            continue
        items.append(
            {
                "text": text,
                "pages": pages,
                "page_count": len(pages),
                "edge_hits": edge_hits,
            }
        )
    items.sort(key=lambda item: (-int(item["page_count"]), len(str(item["text"])), str(item["text"])))
    return items[:max_items]


def _collect_repeated_texts(svg_files: list[Path]) -> set[str]:
    return {str(item["text"]) for item in _repeated_text_items(svg_files)}


# ── SVG export backends ──────────────────────────────────────────────────────

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

_POWERSHELL_PDF_EXPORT = r"""
param(
    [Parameter(Mandatory = $true)][string]$PptxPath,
    [Parameter(Mandatory = $true)][string]$PdfPath
)
$ErrorActionPreference = 'Stop'
$pdfDir = Split-Path -Parent $PdfPath
New-Item -ItemType Directory -Force -Path $pdfDir | Out-Null
$powerpoint = $null
$presentation = $null
try {
    $powerpoint = New-Object -ComObject PowerPoint.Application
    $powerpoint.Visible = -1
    $presentation = $powerpoint.Presentations.Open($PptxPath, $false, $false, $false)
    $presentation.SaveAs($PdfPath, 32)
} finally {
    if ($presentation -ne $null) { $presentation.Close() }
    if ($powerpoint -ne $null) { $powerpoint.Quit() }
    [System.GC]::Collect()
    [System.GC]::WaitForPendingFinalizers()
}
"""

_POWERSHELL_PNG_EXPORT = r"""
param(
    [Parameter(Mandatory = $true)][string]$PptxPath,
    [Parameter(Mandatory = $true)][string]$OutputDir,
    [Parameter(Mandatory = $true)][int]$Width,
    [Parameter(Mandatory = $true)][int]$Height
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
        $fileName = ('slide_{0:D2}.png' -f $slide.SlideIndex)
        $target = Join-Path $OutputDir $fileName
        $slide.Export($target, 'PNG', $Width, $Height)
    }
} finally {
    if ($presentation -ne $null) { $presentation.Close() }
    if ($powerpoint -ne $null) { $powerpoint.Quit() }
    [System.GC]::Collect()
    [System.GC]::WaitForPendingFinalizers()
}
"""


def _run_powershell(script: str, *args: str) -> subprocess.CompletedProcess[bytes]:
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as f:
        f.write(script)
        script_path = Path(f.name)
    try:
        return subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path), *args],
            capture_output=True,
            check=False,
        )
    finally:
        script_path.unlink(missing_ok=True)


def _export_via_powerpoint(pptx_path: Path, output_dir: Path) -> list[Path]:
    completed = _run_powershell(
        _POWERSHELL_SVG_EXPORT,
        "-PptxPath",
        str(pptx_path),
        "-OutputDir",
        str(output_dir),
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"PowerPoint SVG export failed: {stderr}")
    svgs = sorted(output_dir.glob("slide_*.svg"))
    if not svgs:
        raise RuntimeError("PowerPoint export completed but no SVG files found")
    return svgs


def _export_via_pymupdf(pptx_path: Path, output_dir: Path) -> list[Path]:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF (fitz) is not installed.") from exc

    pdf_dir = output_dir.parent / "pdf_temp"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = pdf_dir / f"{pptx_path.stem}.pdf"

    if platform.system() != "Windows":
        raise RuntimeError("PPTX to PDF export requires PowerPoint on Windows.")

    completed = _run_powershell(
        _POWERSHELL_PDF_EXPORT,
        "-PptxPath",
        str(pptx_path),
        "-PdfPath",
        str(pdf_path),
    )
    if completed.returncode != 0:
        raise RuntimeError("PowerPoint PDF export failed.")
    if not pdf_path.exists():
        raise RuntimeError("PDF export completed but no PDF file found.")

    output_dir.mkdir(parents=True, exist_ok=True)
    scale = 96.0 / 72.0
    matrix = fitz.Matrix(scale, scale)
    svg_files: list[Path] = []
    with fitz.open(pdf_path) as doc:
        for index, page in enumerate(doc, 1):
            target = output_dir / f"slide_{index:02d}.svg"
            target.write_text(page.get_svg_image(matrix=matrix, text_as_path=False), encoding="utf-8")
            svg_files.append(target)

    shutil.rmtree(pdf_dir, ignore_errors=True)
    return svg_files


def _export_slides_to_svg(pptx_path: Path, output_dir: Path) -> tuple[list[Path], str]:
    try:
        return _export_via_powerpoint(pptx_path, output_dir), "powerpoint-svg"
    except Exception as exc:
        logger.info("PowerPoint SVG export failed (%s), trying PyMuPDF fallback", exc)

    try:
        return _export_via_pymupdf(pptx_path, output_dir), "pymupdf-svg"
    except Exception as exc:
        logger.info("PyMuPDF export also failed (%s)", exc)

    return [], "metadata-only"


def _export_slide_previews_to_png(pptx_path: Path, output_dir: Path, *, width: int = 1280, height: int = 720) -> list[Path]:
    pptx_path = pptx_path.resolve()
    output_dir = output_dir.resolve()
    completed = _run_powershell(
        _POWERSHELL_PNG_EXPORT,
        "-PptxPath",
        str(pptx_path),
        "-OutputDir",
        str(output_dir),
        "-Width",
        str(width),
        "-Height",
        str(height),
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"PowerPoint PNG preview export failed: {stderr}")
    return sorted(output_dir.glob("slide_*.png"))


# ── Review draft generation ─────────────────────────────────────────────────


def _select_representatives(manifest: dict[str, Any]) -> dict[str, int | None]:
    slides = manifest.get("slides", [])
    page_types = manifest.get("pageTypeCandidates", {})
    selected: dict[str, int | None] = {ptype: None for ptype in PAGE_TYPES}

    for candidate_type, page_type in PAGE_TYPE_MAP.items():
        candidates = page_types.get(candidate_type, [])
        if candidates and selected[page_type] is None:
            selected[page_type] = int(candidates[0])

    if selected["content"] is None and slides:
        selected["content"] = int(slides[len(slides) // 2]["index"])
    if selected["cover"] is None and slides:
        selected["cover"] = int(slides[0]["index"])
    if selected["ending"] is None and slides:
        selected["ending"] = int(slides[-1]["index"])
    if selected["chapter"] is None:
        selected["chapter"] = selected["content"]
    return selected


def _extract_theme_colors(theme: dict[str, Any]) -> list[str]:
    colors = theme.get("colors", {})
    result = []
    for key in ("dk1", "lt1", "accent1", "accent2", "accent3", "accent4"):
        val = colors.get(key)
        if val and isinstance(val, str) and val.startswith("#"):
            result.append(val)
    return result[:6]


def _mime_for_path(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _data_uri_for_file(path: Path, max_bytes: int = 900_000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) > max_bytes:
        return ""
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{_mime_for_path(path)};base64,{encoded}"


def _is_image_asset(path: Path) -> bool:
    return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


def _candidate_template_assets(template_dir: Path) -> list[Path]:
    candidates = []
    for child in template_dir.iterdir() if template_dir.exists() else []:
        if child.is_file() and _is_image_asset(child):
            candidates.append(child)
    for subdir_name in ("assets", "images", "img"):
        subdir = template_dir / subdir_name
        if subdir.is_dir():
            candidates.extend(child for child in subdir.iterdir() if child.is_file() and _is_image_asset(child))
    return sorted(candidates, key=lambda path: path.name.lower())


def _pick_placeholder_asset(template_dir: Path, href: str) -> Path | None:
    candidates = _candidate_template_assets(template_dir)
    if not candidates:
        return None
    lower_href = href.lower()
    lower_names = [(path, path.name.lower()) for path in candidates]

    def first_matching(*needles: str) -> Path | None:
        for needle in needles:
            for path, name in lower_names:
                if needle in name:
                    return path
        return None

    if "cover" in lower_href or "bg" in lower_href or "background" in lower_href:
        return first_matching("cover", "bg", "background", "背景") or candidates[0]
    if "large" in lower_href or "logo_large" in lower_href:
        return first_matching("大型", "large", "logo")
    if "header" in lower_href or "logo_header" in lower_href:
        return first_matching("右上角", "header", "logo")
    if "logo" in lower_href:
        return first_matching("logo", "标志")
    return candidates[0]


def _resolve_preview_asset(svg_path: Path, href: str) -> Path | None:
    clean_href = href.split("#", 1)[0]
    local_path = (svg_path.parent / clean_href).resolve()
    if local_path.exists():
        return local_path
    if "{{" in href and "}}" in href:
        return _pick_placeholder_asset(svg_path.parent, href)
    basename = PurePosixPath(clean_href).name
    for candidate in (
        svg_path.parent / "assets" / basename,
        svg_path.parent.parent / "assets" / basename,
    ):
        if basename and candidate.exists():
            return candidate.resolve()
    # Some older templates refer to ../images/... although assets live in the
    # template directory itself.
    alt_path = (svg_path.parent / basename).resolve()
    if alt_path.exists():
        return alt_path
    return None


def inline_svg_asset_refs(svg_path: Path, *, max_bytes: int = 240_000) -> str:
    """Return an SVG string with local image refs inlined for browser preview."""
    try:
        text = svg_path.read_text(encoding="utf-8")
    except OSError:
        return ""

    remaining_inline_budget = max(0, int(max_bytes) - len(text.encode("utf-8")))

    def repl(match: re.Match[str]) -> str:
        nonlocal remaining_inline_budget
        attr = match.group("attr")
        quote = match.group("quote")
        href = match.group("href")
        if href.startswith(("data:", "http://", "https://", "#")):
            return match.group(0)
        asset_path = _resolve_preview_asset(svg_path, href)
        if asset_path is None or not asset_path.exists():
            return match.group(0)
        try:
            asset_size = asset_path.stat().st_size
        except OSError:
            asset_size = 0
        # Never truncate the SVG document. If inlining an asset would push the
        # preview response over budget, keep the original local href instead.
        if asset_size * 2 > remaining_inline_budget:
            return match.group(0)
        data_uri = _data_uri_for_file(asset_path)
        if not data_uri:
            return match.group(0)
        encoded_size = len(data_uri.encode("utf-8"))
        if encoded_size > remaining_inline_budget:
            return match.group(0)
        remaining_inline_budget -= encoded_size
        return f"{attr}={quote}{data_uri or href}{quote}"

    pattern = re.compile(r'(?P<attr>(?:xlink:)?href)=(?P<quote>["\'])(?P<href>[^"\']+)(?P=quote)')
    return pattern.sub(repl, text)


def _asset_path_from_href(svg_path: Path, href: str) -> Path | None:
    if not href or href.startswith(("data:", "http://", "https://", "#")):
        return None
    clean_href = href.split("#", 1)[0]
    return (svg_path.parent / clean_href).resolve()


def _image_size(path: Path) -> dict[str, int]:
    try:
        from PIL import Image

        with Image.open(path) as img:
            return {"width": int(img.width), "height": int(img.height)}
    except Exception:
        return {"width": 0, "height": 0}


def _href_for_element(element: ET.Element) -> str:
    return (
        element.attrib.get("href")
        or element.attrib.get(f"{{{XLINK_NS}}}href")
        or element.attrib.get("xlink:href")
        or ""
    )


def _actionable_element_kind(element: ET.Element) -> str:
    local = _local_name(element.tag)
    if local == "text":
        return "text"
    if local == "image":
        return "image"
    if local in {"rect", "circle", "ellipse", "line", "polyline", "polygon", "path"}:
        return "shape"
    return ""


def _element_id(slide_index: int, kind: str, ordinal: int) -> str:
    return f"s{slide_index:02d}_{kind}_{ordinal:03d}"


def _iter_actionable_elements(root: ET.Element, slide_index: int):
    counts: dict[str, int] = {}
    for parent in root.iter():
        for child in list(parent):
            kind = _actionable_element_kind(child)
            if not kind:
                continue
            counts[kind] = counts.get(kind, 0) + 1
            yield _element_id(slide_index, kind, counts[kind]), parent, child, kind


def _element_bbox(element: ET.Element, kind: str, canvas_w: int, canvas_h: int) -> dict[str, float]:
    if kind == "text":
        candidate = _text_candidate(element)
        return {
            "x": float(candidate["x"]),
            "y": max(0.0, float(candidate["y"]) - float(candidate["height"])),
            "width": float(candidate["width"]),
            "height": float(candidate["height"]),
        }
    if _local_name(element.tag) == "circle":
        cx = _to_float(element.attrib.get("cx"), canvas_w / 2)
        cy = _to_float(element.attrib.get("cy"), canvas_h / 2)
        r = _to_float(element.attrib.get("r"), 0)
        return {"x": cx - r, "y": cy - r, "width": 2 * r, "height": 2 * r}
    if _local_name(element.tag) == "ellipse":
        cx = _to_float(element.attrib.get("cx"), canvas_w / 2)
        cy = _to_float(element.attrib.get("cy"), canvas_h / 2)
        rx = _to_float(element.attrib.get("rx"), 0)
        ry = _to_float(element.attrib.get("ry"), 0)
        return {"x": cx - rx, "y": cy - ry, "width": 2 * rx, "height": 2 * ry}
    if _local_name(element.tag) == "path":
        bbox = _path_bbox_estimate(element)
        if bbox is not None:
            return bbox
    return {
        "x": _to_float(element.attrib.get("x")),
        "y": _to_float(element.attrib.get("y")),
        "width": _to_float(element.attrib.get("width")),
        "height": _to_float(element.attrib.get("height")),
    }


def _path_bbox_estimate(element: ET.Element) -> dict[str, float] | None:
    """Best-effort bbox for decorative paths exported by PPT/PDF.

    The review manifest only needs a useful target for user/agent feedback.
    We approximate path geometry from numeric coordinate pairs, then apply the
    element's own transform matrix. This catches outlined numerals and badges
    that have no x/y/width/height attributes.
    """

    nums = _float_values(element.attrib.get("d", ""))
    if len(nums) < 4:
        return None
    points = list(zip(nums[0::2], nums[1::2]))
    if not points:
        return None
    matrix = _parse_svg_transform(element.attrib.get("transform"))
    transformed = [_apply_svg_matrix(matrix, x, y) for x, y in points]
    xs = [p[0] for p in transformed]
    ys = [p[1] for p in transformed]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}


def _extract_slide_elements(
    svg_path: Path,
    slide_index: int,
    assets_by_file: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    canvas_w, canvas_h = _svg_canvas_size(svg_path)
    try:
        root = ET.parse(svg_path).getroot()
    except ET.ParseError:
        return []
    elements = []
    for element_id, _parent, element, kind in _iter_actionable_elements(root, slide_index):
        bbox = _element_bbox(element, kind, canvas_w, canvas_h)
        if kind != "text" and (bbox["width"] <= 0 or bbox["height"] <= 0):
            continue
        item: dict[str, Any] = {
            "element_id": element_id,
            "type": kind,
            "x": round(bbox["x"], 2),
            "y": round(bbox["y"], 2),
            "width": round(bbox["width"], 2),
            "height": round(bbox["height"], 2),
        }
        if kind == "text":
            candidate = _text_candidate(element)
            item["text"] = str(candidate["text"])[:220]
            item["font_size"] = round(float(candidate["font_size"]), 2)
        elif kind == "image":
            href = _href_for_element(element)
            file_name = PurePosixPath(href.split("#", 1)[0]).name
            asset = assets_by_file.get(file_name, {})
            item["file_name"] = file_name
            item["asset_id"] = asset.get("asset_id", "")
            item["asset_role_guess"] = asset.get("recommended_role", "")
        elements.append(item)
    return elements[:180]


def _asset_keep_threshold(slide_count: int) -> int:
    if slide_count <= 0:
        return 4
    return max(4, math.ceil(slide_count * 0.35))


def _asset_is_frequent_for_default_keep(asset: dict[str, Any], slide_count: int) -> bool:
    pages = {int(page) for page in asset.get("pages", []) if page}
    return len(pages) >= _asset_keep_threshold(slide_count)


def _extract_asset_candidates(svg_files: list[Path]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    canvas_w = 1280
    canvas_h = 720
    slide_count = len(svg_files)

    for svg_path in svg_files:
        slide_match = re.search(r"slide_(\d+)\.svg$", svg_path.name)
        slide_index = int(slide_match.group(1)) if slide_match else len(grouped) + 1
        canvas_w, canvas_h = _svg_canvas_size(svg_path, canvas_w, canvas_h)
        try:
            root = ET.parse(svg_path).getroot()
        except ET.ParseError:
            continue

        for element in root.iter():
            if _local_name(element.tag) != "image":
                continue
            href = _href_for_element(element)
            asset_path = _asset_path_from_href(svg_path, href)
            if asset_path is None:
                continue
            file_name = PurePosixPath(href.split("#", 1)[0]).name
            key = file_name
            x = _to_float(element.attrib.get("x"))
            y = _to_float(element.attrib.get("y"))
            width = _to_float(element.attrib.get("width"))
            height = _to_float(element.attrib.get("height"))
            if key not in grouped:
                file_bytes = asset_path.read_bytes() if asset_path.exists() else file_name.encode("utf-8")
                digest = hashlib.sha1(file_bytes).hexdigest()
                grouped[key] = {
                    "asset_id": f"asset_{digest[:12]}",
                    "file_name": file_name,
                    "path": str(asset_path),
                    "sha1": digest,
                    "image_size": _image_size(asset_path),
                    "occurrences": [],
                }
            grouped[key]["occurrences"].append(
                {
                    "slide_index": slide_index,
                    "x": round(x, 2),
                    "y": round(y, 2),
                    "width": round(width, 2),
                    "height": round(height, 2),
                }
            )

    candidates: list[dict[str, Any]] = []
    for item in grouped.values():
        occurrences = item["occurrences"]
        pages = sorted({int(occ["slide_index"]) for occ in occurrences})
        buckets: dict[tuple[int, int, int, int], int] = {}
        max_area = 0.0
        corner_hits = 0
        for occ in occurrences:
            w = float(occ["width"] or 0)
            h = float(occ["height"] or 0)
            x = float(occ["x"] or 0)
            y = float(occ["y"] or 0)
            max_area = max(max_area, (w * h) / max(canvas_w * canvas_h, 1))
            if (x < canvas_w * 0.18 or x + w > canvas_w * 0.82) and (
                y < canvas_h * 0.22 or y + h > canvas_h * 0.82
            ):
                corner_hits += 1
            bucket = (round(x / 24), round(y / 24), round(w / 24), round(h / 24))
            buckets[bucket] = buckets.get(bucket, 0) + 1

        repeated = len(pages) > 1
        stable_count = max(buckets.values()) if buckets else 0
        stable = repeated and stable_count > 1
        frequent = stable and len(pages) >= _asset_keep_threshold(slide_count)
        if frequent and max_area > 0.55:
            recommended = "background"
        elif frequent and max_area < 0.08 and corner_hits >= max(1, len(occurrences) // 2):
            recommended = "logo"
        elif frequent and max_area < 0.16:
            recommended = "decoration"
        else:
            recommended = "ignore"

        candidates.append(
            {
                **item,
                "usage_count": len(occurrences),
                "pages": pages,
                "position_stable": stable,
                "recommended_role": recommended,
                "role": recommended,
                "name": item["file_name"].rsplit(".", 1)[0],
            }
        )

    candidates.sort(key=lambda item: (-int(item["usage_count"]), item["file_name"]))
    return candidates


def _build_review_payload(
    import_id: str,
    manifest: dict[str, Any],
    svg_files: list[Path],
    export_mode: str,
    template_id: str,
    label: str,
) -> dict[str, Any]:
    page_selections = _select_representatives(manifest)
    assets = _extract_asset_candidates(svg_files)
    assets_by_file = {asset["file_name"]: asset for asset in assets}
    slides = []
    slides_manifest = []
    slide_by_index = {int(slide["index"]): slide for slide in manifest.get("slides", [])}
    for svg_path in svg_files:
        match = re.search(r"slide_(\d+)\.svg$", svg_path.name)
        if not match:
            continue
        idx = int(match.group(1))
        slide = slide_by_index.get(idx, {})
        elements = _extract_slide_elements(svg_path, idx, assets_by_file)
        slide_record = {
            "index": idx,
            "page_type": PAGE_TYPE_MAP.get(slide.get("pageType", ""), "content"),
            "text_samples": slide.get("textSamples", []),
            "elements": elements,
            "preview_svg_path": str(svg_path),
        }
        slides_manifest.append(slide_record)
        slides.append(
            {
                "index": idx,
                "page_type": slide_record["page_type"],
                "text_samples": slide_record["text_samples"],
                "preview_svg_path": slide_record["preview_svg_path"],
            }
        )

    if svg_files:
        _write_json(
            _review_manifest_path(import_id),
            {
                "schema_version": 1,
                "import_id": import_id,
                "slides": slides_manifest,
                "assets": assets,
                "storage": {
                    "source": "work/svg",
                    "raw_source": "work/svg_raw",
                    "asset_dir": "work/assets",
                    "path_format": "slide_XX.svg",
                },
            },
        )

    review = {
        "import_id": import_id,
        "template_id": template_id,
        "label": label,
        "status": "review_required",
        "export_mode": export_mode,
        "slide_count": len(manifest.get("slides", [])),
        "page_types": list(PAGE_TYPES),
        "asset_roles": list(ASSET_ROLES),
        "page_type_candidates": {
            PAGE_TYPE_MAP.get(key, key): value
            for key, value in manifest.get("pageTypeCandidates", {}).items()
        },
        "slides": slides,
        "assets": assets,
        "text_candidates": _repeated_text_items(svg_files),
        "draft": {
            "label": label,
            "page_selections": page_selections,
            "assets": {},
            "preserve_texts": [],
            "design_spec": "",
        },
        "theme_colors": _extract_theme_colors(manifest.get("theme", {})),
        "llm": {"enabled": False, "status": "not_run"},
    }
    review["draft"]["assets"] = {
        asset["asset_id"]: {
            "role": asset["recommended_role"],
            "name": asset["name"],
        }
        for asset in review["assets"]
    }
    return review


# ── Template SVG generation ─────────────────────────────────────────────────


def _remove_children_where(root: ET.Element, predicate) -> None:
    for parent in root.iter():
        for child in list(parent):
            if predicate(child):
                parent.remove(child)


def _style_value(element: ET.Element, name: str) -> str:
    value = element.attrib.get(name)
    if value:
        return value
    style = element.attrib.get("style", "")
    match = re.search(rf"(?:^|;)\s*{re.escape(name)}\s*:\s*([^;]+)", style)
    return match.group(1).strip() if match else ""


def _text_coordinate_nodes(element: ET.Element) -> list[tuple[ET.Element, list[float], list[float]]]:
    nodes = []
    for node in element.iter():
        if _local_name(node.tag) not in {"text", "tspan"}:
            continue
        xs = _float_values(node.attrib.get("x"))
        ys = _float_values(node.attrib.get("y"))
        if xs and ys:
            nodes.append((node, xs, ys))
    return nodes


def _text_candidate(element: ET.Element) -> dict[str, Any]:
    text = _element_text_content(element)
    matrix = _parse_svg_transform(element.attrib.get("transform"))
    scale_x, scale_y = _matrix_scale(matrix)
    raw_font_size = _to_float(_style_value(element, "font-size"), 18)
    font_size = max(1.0, raw_font_size * scale_y)
    coordinate_nodes = _text_coordinate_nodes(element)

    points: list[tuple[float, float]] = []
    for _node, xs, ys in coordinate_nodes:
        y_value = ys[0]
        x_values = [xs[0]]
        if len(xs) > 1:
            x_values.append(xs[-1])
        for x_value in x_values:
            points.append(_apply_svg_matrix(matrix, x_value, y_value))

    if not points:
        x_value = _to_float(element.attrib.get("x"), 0)
        y_value = _to_float(element.attrib.get("y"), 0)
        points.append(_apply_svg_matrix(matrix, x_value, y_value))

    min_x = min(point[0] for point in points)
    max_x = max(point[0] for point in points)
    min_y = min(point[1] for point in points)
    glyph_width = max(font_size * max(len(text), 4) * 0.55, font_size * 3)
    coordinate_width = max_x - min_x + raw_font_size * scale_x
    return {
        "text": text,
        "norm": _normalize_text_candidate(text),
        "x": min_x,
        "y": min_y,
        "width": max(glyph_width, coordinate_width),
        "height": max(font_size * 1.25, 12),
        "font_size": font_size,
        "font_family": _style_value(element, "font-family") or "Arial, sans-serif",
        "font_weight": _style_value(element, "font-weight") or element.attrib.get("font-weight", ""),
        "fill": _style_value(element, "fill") or element.attrib.get("fill") or "#111827",
        "text_anchor": _style_value(element, "text-anchor") or element.attrib.get("text-anchor", ""),
    }


def _is_framework_text_candidate(candidate: dict[str, Any], canvas_h: int) -> bool:
    y = float(candidate.get("y") or 0)
    return y <= canvas_h * 0.17 or y >= canvas_h * 0.88


def _placeholder_text(candidate: dict[str, Any] | None, token: str, fallback: dict[str, Any]) -> str:
    c = {**fallback, **(candidate or {})}
    attrs = [
        f'x="{float(c["x"]):.2f}"',
        f'y="{float(c["y"]):.2f}"',
        f'fill="{html.escape(str(c.get("fill") or "#111827"), quote=True)}"',
        f'font-family="{html.escape(str(c.get("font_family") or "Arial, sans-serif"), quote=True)}"',
        f'font-size="{float(c.get("font_size") or 18):.2f}"',
    ]
    if c.get("font_weight"):
        attrs.append(f'font-weight="{html.escape(str(c["font_weight"]), quote=True)}"')
    if c.get("text_anchor"):
        attrs.append(f'text-anchor="{html.escape(str(c["text_anchor"]), quote=True)}"')
    return f"    <text {' '.join(attrs)}>{{{{{token}}}}}</text>"


def _bbox_union(boxes: list[dict[str, float]], canvas_w: int, canvas_h: int) -> dict[str, float] | None:
    if not boxes:
        return None
    x1 = max(0.0, min(box["x"] for box in boxes))
    y1 = max(0.0, min(box["y"] for box in boxes))
    x2 = min(float(canvas_w), max(box["x"] + box["width"] for box in boxes))
    y2 = min(float(canvas_h), max(box["y"] + box["height"] for box in boxes))
    if x2 <= x1 or y2 <= y1:
        return None
    pad = 12.0
    return {
        "x": max(0.0, x1 - pad),
        "y": max(0.0, y1 - pad),
        "width": min(float(canvas_w), x2 + pad) - max(0.0, x1 - pad),
        "height": min(float(canvas_h), y2 + pad) - max(0.0, y1 - pad),
    }


def _placeholder_context(
    root: ET.Element,
    page_type: str,
    canvas_w: int,
    canvas_h: int,
    asset_roles_by_file: dict[str, str],
    preserve_texts: set[str],
    placeholder_hints: dict[str, str] | None = None,
    suppressed_texts: set[str] | None = None,
) -> dict[str, Any]:
    text_candidates: list[dict[str, Any]] = []
    removed_boxes: list[dict[str, float]] = []
    suppressed_texts = suppressed_texts or set()

    for element in root.iter():
        local = _local_name(element.tag)
        if local == "text":
            candidate = _text_candidate(element)
            keep = bool(candidate["norm"] in preserve_texts and _is_framework_text_candidate(candidate, canvas_h))
            suppressed = bool(candidate["norm"] in suppressed_texts)
            if not keep and candidate["text"].strip():
                if not suppressed:
                    text_candidates.append(candidate)
                removed_boxes.append(
                    {
                        "x": candidate["x"],
                        "y": max(0.0, candidate["y"] - candidate["height"]),
                        "width": candidate["width"],
                        "height": candidate["height"],
                    }
                )
        elif local == "image":
            href = _href_for_element(element)
            file_name = PurePosixPath(href.split("#", 1)[0]).name
            role = asset_roles_by_file.get(file_name, "ignore")
            if role in {"content_image", "ignore"}:
                removed_boxes.append(
                    {
                        "x": _to_float(element.attrib.get("x")),
                        "y": _to_float(element.attrib.get("y")),
                        "width": _to_float(element.attrib.get("width")),
                        "height": _to_float(element.attrib.get("height")),
                    }
                )

    def fallback(x: float, y: float, size: float = 24, weight: str = "700", fill: str = "#111827") -> dict[str, Any]:
        return {
            "x": x,
            "y": y,
            "font_size": size,
            "font_weight": weight,
            "fill": fill,
            "font_family": "Arial, sans-serif",
        }

    large = sorted(text_candidates, key=lambda c: (-float(c["font_size"]), float(c["y"]), float(c["x"])))
    by_flow = sorted(text_candidates, key=lambda c: (float(c["y"]), float(c["x"])))
    used: set[int] = set()
    placeholder_hints = placeholder_hints or {}

    def take_hint(token: str) -> dict[str, Any] | None:
        wanted = _normalize_text_candidate(placeholder_hints.get(token, ""))
        if not wanted:
            return None
        for candidate in large:
            if id(candidate) in used:
                continue
            candidate_norm = str(candidate.get("norm") or "")
            if candidate_norm == wanted or wanted in candidate_norm or candidate_norm in wanted:
                used.add(id(candidate))
                return candidate
        return None

    def take(predicate, default: dict[str, Any] | None = None) -> dict[str, Any] | None:
        for candidate in large:
            if id(candidate) in used:
                continue
            if predicate(candidate):
                used.add(id(candidate))
                return candidate
        return default

    placeholders: list[str] = []
    if page_type == "cover":
        title = take_hint("TITLE") or take(lambda c: canvas_h * 0.18 <= c["y"] <= canvas_h * 0.72, fallback(canvas_w * 0.08, canvas_h * 0.42, 40))
        subtitle = take_hint("SUBTITLE") or take(
            lambda c: c is not title and c["y"] >= float(title.get("y", 0)),
            fallback(float(title["x"]), min(canvas_h * 0.92, float(title["y"]) + canvas_h * 0.08), 22, "400", "#4B5563"),
        )
        placeholders.append(_placeholder_text(title, "TITLE", fallback(canvas_w * 0.08, canvas_h * 0.42, 40)))
        placeholders.append(_placeholder_text(subtitle, "SUBTITLE", fallback(canvas_w * 0.08, canvas_h * 0.5, 22, "400")))
        placeholders.append(
            _placeholder_text(None, "AUTHOR", fallback(float(title["x"]), canvas_h * 0.72, 14, "400", "#6B7280"))
        )
        placeholders.append(
            _placeholder_text(None, "DATE", fallback(float(title["x"]) + canvas_w * 0.16, canvas_h * 0.72, 14, "400", "#6B7280"))
        )
    elif page_type == "chapter":
        num = take_hint("CHAPTER_NUM") or take(lambda c: re.fullmatch(r"\d+", c["text"].strip()) is not None, fallback(canvas_w * 0.18, canvas_h * 0.5, 64, "700", "#CBD5E1"))
        title = take_hint("CHAPTER_TITLE") or take(lambda c: c is not num, fallback(canvas_w * 0.28, canvas_h * 0.5, 34))
        placeholders.append(_placeholder_text(num, "CHAPTER_NUM", fallback(canvas_w * 0.18, canvas_h * 0.5, 64)))
        placeholders.append(_placeholder_text(title, "CHAPTER_TITLE", fallback(canvas_w * 0.28, canvas_h * 0.5, 34)))
        placeholders.append(_placeholder_text(None, "CHAPTER_TITLE_EN", fallback(float(title["x"]), float(title["y"]) + 34, 16, "400", "#6B7280")))
    elif page_type == "toc":
        title = take_hint("PAGE_TITLE") or take(
            lambda c: bool(re.search(r"目录|contents|toc", c["text"], re.I)),
            fallback(canvas_w * 0.06, canvas_h * 0.16, 30),
        )
        placeholders.append(_placeholder_text(title, "PAGE_TITLE", fallback(canvas_w * 0.06, canvas_h * 0.16, 30)))
        items = [c for c in by_flow if id(c) not in used and c["y"] > canvas_h * 0.14]
        for index, candidate in enumerate(items[:5], 1):
            token = f"TOC_ITEM_{index}_TITLE"
            placeholders.append(_placeholder_text(take_hint(token) or candidate, token, fallback(canvas_w * 0.12, canvas_h * (0.28 + index * 0.08), 20, "600")))
    elif page_type == "ending":
        thanks = take_hint("THANK_YOU") or take(lambda c: canvas_h * 0.15 <= c["y"] <= canvas_h * 0.8, fallback(canvas_w * 0.5, canvas_h * 0.46, 44))
        if thanks:
            thanks["text_anchor"] = thanks.get("text_anchor") or "middle"
        placeholders.append(_placeholder_text(thanks, "THANK_YOU", fallback(canvas_w * 0.5, canvas_h * 0.46, 44)))
        placeholders.append(_placeholder_text(None, "CONTACT_INFO", fallback(canvas_w * 0.5, canvas_h * 0.56, 18, "400", "#4B5563") | {"text_anchor": "middle"}))
    else:
        title = take_hint("PAGE_TITLE") or take(lambda c: canvas_h * 0.08 <= c["y"] <= canvas_h * 0.28, fallback(canvas_w * 0.06, canvas_h * 0.14, 26))
        placeholders.append(_placeholder_text(title, "PAGE_TITLE", fallback(canvas_w * 0.06, canvas_h * 0.14, 26)))
        content_boxes = [
            box
            for box in removed_boxes
            if box["y"] >= canvas_h * 0.16 and box["y"] + box["height"] <= canvas_h * 0.94
        ]
        if title:
            content_boxes = [
                box
                for box in content_boxes
                if not (abs(box["x"] - float(title["x"])) < 2 and abs((box["y"] + box["height"]) - float(title["y"])) < 4)
            ]
        content_area = _bbox_union(content_boxes, canvas_w, canvas_h) or {
            "x": canvas_w * 0.06,
            "y": canvas_h * 0.2,
            "width": canvas_w * 0.88,
            "height": canvas_h * 0.62,
        }
        return {"placeholders": placeholders, "content_area": content_area}

    return {"placeholders": placeholders, "content_area": None}


def _templateize_svg(
    svg_path: Path,
    page_type: str,
    canvas_w: int,
    canvas_h: int,
    asset_roles_by_file: dict[str, str],
    preserve_texts: set[str] | None = None,
    placeholder_hints: dict[str, str] | None = None,
    suppressed_texts: set[str] | None = None,
) -> str:
    root = ET.parse(svg_path).getroot()
    preserve_texts = preserve_texts or set()
    context = _placeholder_context(
        root,
        page_type,
        canvas_w,
        canvas_h,
        asset_roles_by_file,
        preserve_texts,
        placeholder_hints=placeholder_hints,
        suppressed_texts=suppressed_texts,
    )

    def should_remove(element: ET.Element) -> bool:
        local = _local_name(element.tag)
        if local == "text":
            candidate = _text_candidate(element)
            return not (candidate["norm"] in preserve_texts and _is_framework_text_candidate(candidate, canvas_h))
        if local == "image":
            href = _href_for_element(element)
            file_name = PurePosixPath(href.split("#", 1)[0]).name
            role = asset_roles_by_file.get(file_name, "ignore")
            return role in {"content_image", "ignore"}
        return False

    _remove_children_where(root, should_remove)
    svg_text = ET.tostring(root, encoding="unicode")
    svg_text = _normalize_template_asset_refs(svg_text)
    return _inject_placeholders(svg_text, page_type, canvas_w, canvas_h, context)


def _placeholder_markup_for_box(box: dict[str, float], placeholder: str) -> str:
    token = placeholder or "CONTENT_AREA"
    if token == "CONTENT_AREA":
        return f"""
    <g id="content-area">
      <rect x="{box['x']:.2f}" y="{box['y']:.2f}" width="{box['width']:.2f}" height="{box['height']:.2f}" fill="none" stroke="#CBD5E1" stroke-width="1" stroke-dasharray="8 5"/>
      <text x="{box['x'] + box['width'] / 2:.2f}" y="{box['y'] + box['height'] / 2:.2f}" text-anchor="middle" fill="#94A3B8" font-family="Arial, sans-serif" font-size="18">{{{{CONTENT_AREA}}}}</text>
    </g>"""
    font_size = max(14.0, min(36.0, box.get("height", 24.0) * 0.75))
    return (
        f'    <text x="{box["x"]:.2f}" y="{box["y"] + font_size:.2f}" '
        f'fill="#111827" font-family="Arial, sans-serif" font-size="{font_size:.2f}" '
        f'font-weight="700">{{{{{token}}}}}</text>'
    )


def _placeholder_style_overrides(action: dict[str, Any] | None) -> dict[str, Any]:
    if not action:
        return {}
    out: dict[str, Any] = {}
    for key in ("x", "y", "font_size"):
        if key not in action:
            continue
        try:
            out[key] = float(action[key])
        except (TypeError, ValueError):
            continue
    for key in ("font_weight", "fill", "font_family", "text_anchor"):
        value = str(action.get(key) or "").strip()
        if value:
            out[key] = value
    return out


def _box_overrides(action: dict[str, Any] | None) -> dict[str, float]:
    if not action:
        return {}
    out: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        if key not in action:
            continue
        try:
            out[key] = float(action[key])
        except (TypeError, ValueError):
            continue
    return out


def _replace_text_element_with_placeholder(element: ET.Element, placeholder: str) -> None:
    for child in list(element):
        element.remove(child)
    for attr in ("style", "letter-spacing", "word-spacing", "font-stretch", "transform"):
        element.attrib.pop(attr, None)
    element.attrib["font-family"] = "Arial, sans-serif"
    element.attrib["font-weight"] = "700"
    element.attrib["letter-spacing"] = "0"
    element.text = f"{{{{{placeholder}}}}}"


def _templateize_svg_with_plan(
    svg_path: Path,
    slide_index: int,
    page_type: str,
    canvas_w: int,
    canvas_h: int,
    asset_roles_by_file: dict[str, str],
    preserve_texts: set[str],
    element_actions: list[dict[str, Any]],
) -> str:
    root = ET.parse(svg_path).getroot()
    actions = {
        str(action.get("element_id")): action
        for action in element_actions
        if action.get("page_type") == page_type and action.get("element_id")
    }
    injected: list[str] = []

    for element_id, parent, element, kind in list(_iter_actionable_elements(root, slide_index)):
        action = actions.get(element_id)
        action_name = action.get("action") if action else ""
        placeholder = str(action.get("placeholder") or "").strip() if action else ""

        if action_name == "keep":
            continue
        if action_name == "replace_with_placeholder" and placeholder:
            box = {**_element_bbox(element, kind, canvas_w, canvas_h), **_box_overrides(action)}
            if kind == "text" and placeholder != "CONTENT_AREA":
                candidate = {**_text_candidate(element), **_placeholder_style_overrides(action)}
                injected.append(
                    _placeholder_text(
                        candidate,
                        placeholder,
                        {
                            "x": box["x"],
                            "y": box["y"] + box["height"],
                            "font_size": max(14.0, min(36.0, box.get("height", 24.0) * 0.75)),
                            "font_weight": "700",
                            "fill": "#111827",
                            "font_family": "Arial, sans-serif",
                        },
                    )
                )
            else:
                injected.append(_placeholder_markup_for_box(box, placeholder))
            parent.remove(element)
            continue
        if action_name == "remove":
            parent.remove(element)
            continue

        if kind == "text":
            candidate = _text_candidate(element)
            if candidate["norm"] in preserve_texts and _is_framework_text_candidate(candidate, canvas_h):
                continue
            parent.remove(element)
            continue
        if kind == "image":
            href = _href_for_element(element)
            file_name = PurePosixPath(href.split("#", 1)[0]).name
            role = asset_roles_by_file.get(file_name, "ignore")
            if role not in {"logo", "background", "decoration"}:
                parent.remove(element)

    svg_text = _normalize_template_asset_refs(ET.tostring(root, encoding="unicode"))
    if injected:
        placeholders = "\n  <g id=\"template-placeholders\">\n" + "\n".join(injected) + "\n  </g>\n"
        if "</svg>" in svg_text:
            return svg_text.replace("</svg>", placeholders + "</svg>")
        return svg_text + placeholders
    return svg_text


def _normalize_template_asset_refs(svg_text: str) -> str:
    """Rebase work/svg image refs for final template-dir SVG files."""
    return svg_text.replace("../assets/", "assets/").replace("..\\assets\\", "assets/")


def _inject_placeholders(
    svg_text: str,
    page_type: str,
    canvas_w: int,
    canvas_h: int,
    context: dict[str, Any] | None = None,
) -> str:
    margin_x = int(canvas_w * 0.05)
    top_y = int(canvas_h * 0.12)
    content_x = int(canvas_w * 0.06)
    content_y = int(canvas_h * 0.2)
    content_w = int(canvas_w * 0.88)
    content_h = int(canvas_h * 0.62)
    footer_y = int(canvas_h * 0.94)

    context = context or {}
    dynamic_lines = context.get("placeholders") or []
    if dynamic_lines:
        content_area = context.get("content_area")
        content_markup = ""
        if page_type == "content" and content_area:
            content_markup = f"""
    <g id="content-area">
      <rect x="{content_area['x']:.2f}" y="{content_area['y']:.2f}" width="{content_area['width']:.2f}" height="{content_area['height']:.2f}" fill="none" stroke="#CBD5E1" stroke-width="1" stroke-dasharray="8 5"/>
      <text x="{content_area['x'] + content_area['width'] / 2:.2f}" y="{content_area['y'] + content_area['height'] / 2:.2f}" text-anchor="middle" fill="#94A3B8" font-family="Arial, sans-serif" font-size="18">{{{{CONTENT_AREA}}}}</text>
    </g>"""
        placeholders = "\n  <g id=\"template-placeholders\">\n" + "\n".join(dynamic_lines) + content_markup + "\n  </g>\n"
    elif page_type == "cover":
        placeholders = f"""
  <g id="template-placeholders">
    <text x="{margin_x}" y="{int(canvas_h * 0.42)}" fill="#111827" font-family="Arial, sans-serif" font-size="44" font-weight="700">{{{{TITLE}}}}</text>
    <text x="{margin_x}" y="{int(canvas_h * 0.5)}" fill="#4B5563" font-family="Arial, sans-serif" font-size="22">{{{{SUBTITLE}}}}</text>
    <text x="{margin_x}" y="{int(canvas_h * 0.62)}" fill="#6B7280" font-family="Arial, sans-serif" font-size="16">{{{{AUTHOR}}}} · {{{{DATE}}}}</text>
  </g>
"""
    elif page_type == "chapter":
        placeholders = f"""
  <g id="template-placeholders">
    <text x="{margin_x}" y="{int(canvas_h * 0.38)}" fill="#1F2937" font-family="Arial, sans-serif" font-size="28" font-weight="700">{{{{CHAPTER_NUM}}}}</text>
    <text x="{margin_x}" y="{int(canvas_h * 0.5)}" fill="#111827" font-family="Arial, sans-serif" font-size="42" font-weight="700">{{{{CHAPTER_TITLE}}}}</text>
    <text x="{margin_x}" y="{int(canvas_h * 0.58)}" fill="#6B7280" font-family="Arial, sans-serif" font-size="20">{{{{CHAPTER_TITLE_EN}}}}</text>
  </g>
"""
    elif page_type == "toc":
        placeholders = f"""
  <g id="template-placeholders">
    <text x="{margin_x}" y="{top_y}" fill="#111827" font-family="Arial, sans-serif" font-size="30" font-weight="700">{{{{PAGE_TITLE}}}}</text>
    <text x="{content_x}" y="{content_y}" fill="#374151" font-family="Arial, sans-serif" font-size="22">{{{{TOC_ITEM_1_TITLE}}}}</text>
    <text x="{content_x}" y="{content_y + 64}" fill="#374151" font-family="Arial, sans-serif" font-size="22">{{{{TOC_ITEM_2_TITLE}}}}</text>
    <text x="{content_x}" y="{content_y + 128}" fill="#374151" font-family="Arial, sans-serif" font-size="22">{{{{TOC_ITEM_3_TITLE}}}}</text>
    <text x="{canvas_w - margin_x}" y="{footer_y}" text-anchor="end" fill="#6B7280" font-family="Arial, sans-serif" font-size="14">{{{{PAGE_NUM}}}}</text>
  </g>
"""
    elif page_type == "ending":
        placeholders = f"""
  <g id="template-placeholders">
    <text x="{int(canvas_w * 0.5)}" y="{int(canvas_h * 0.45)}" text-anchor="middle" fill="#111827" font-family="Arial, sans-serif" font-size="46" font-weight="700">{{{{THANK_YOU}}}}</text>
    <text x="{int(canvas_w * 0.5)}" y="{int(canvas_h * 0.54)}" text-anchor="middle" fill="#4B5563" font-family="Arial, sans-serif" font-size="20">{{{{CONTACT_INFO}}}}</text>
  </g>
"""
    else:
        placeholders = f"""
  <g id="template-placeholders">
    <text x="{margin_x}" y="{top_y}" fill="#111827" font-family="Arial, sans-serif" font-size="28" font-weight="700">{{{{PAGE_TITLE}}}}</text>
    <g id="content-area">
      <rect x="{content_x}" y="{content_y}" width="{content_w}" height="{content_h}" fill="none" stroke="#CBD5E1" stroke-width="1" stroke-dasharray="8 5"/>
      <text x="{content_x + content_w / 2:.0f}" y="{content_y + content_h / 2:.0f}" text-anchor="middle" fill="#94A3B8" font-family="Arial, sans-serif" font-size="18">{{{{CONTENT_AREA}}}}</text>
    </g>
    <text x="{margin_x}" y="{footer_y}" fill="#6B7280" font-family="Arial, sans-serif" font-size="12">Source: {{{{SOURCE}}}}</text>
    <text x="{canvas_w - margin_x}" y="{footer_y}" text-anchor="end" fill="#6B7280" font-family="Arial, sans-serif" font-size="14">{{{{PAGE_NUM}}}}</text>
  </g>
"""
    if "</svg>" in svg_text:
        return svg_text.replace("</svg>", placeholders + "</svg>")
    return svg_text + placeholders


def _generate_design_spec(manifest: dict[str, Any], review: dict[str, Any], template_id: str, label: str) -> str:
    theme = manifest.get("theme", {})
    colors = theme.get("colors", {})
    fonts = theme.get("fonts", {})
    size = manifest.get("slideSize", {})

    color_lines = [f"  - {name}: {value}" for name, value in sorted(colors.items())]
    font_lines = [f"  - {name}: {value}" for name, value in sorted(fonts.items())]
    selected_lines = [
        f"  - {ptype}: slide {idx}" for ptype, idx in review.get("draft", {}).get("page_selections", {}).items() if idx
    ]
    kept_assets = []
    draft_assets = review.get("draft", {}).get("assets", {})
    assets_by_id = {asset["asset_id"]: asset for asset in review.get("assets", [])}
    for asset_id, draft in draft_assets.items():
        if draft.get("role") in {"logo", "background", "decoration"}:
            asset = assets_by_id.get(asset_id, {})
            kept_assets.append(f"  - {draft.get('name') or asset.get('file_name')}: {draft.get('role')}")
    llm_status = review.get("llm", {}).get("status") or "not_run"
    preserved_texts = review.get("draft", {}).get("preserve_texts", []) or []
    preserved_lines = [f"  - {text}" for text in preserved_texts[:20]]

    return f"""# Template Design Spec: {label}

## Source
- Template ID: {template_id}
- Original file: {manifest.get('source', {}).get('name', 'unknown')}
- Slide size: {size.get('width_px', 1280)} x {size.get('height_px', 720)} px
- Total slides: {len(manifest.get('slides', []))}

## Color Scheme
{chr(10).join(color_lines) if color_lines else '  - (none detected)'}

## Typography
{chr(10).join(font_lines) if font_lines else '  - (none detected)'}

## Page Type Selection
{chr(10).join(selected_lines) if selected_lines else '  - (not selected)'}

## Reusable Assets
{chr(10).join(kept_assets) if kept_assets else '  - (none selected)'}

## Reusable Text Framework
{chr(10).join(preserved_lines) if preserved_lines else '  - (none explicitly selected)'}

## Placeholder Contract
- Cover: `{{{{TITLE}}}}`, `{{{{SUBTITLE}}}}`, `{{{{AUTHOR}}}}`, `{{{{DATE}}}}`
- Content: `{{{{PAGE_TITLE}}}}`, `{{{{CONTENT_AREA}}}}`, `{{{{SOURCE}}}}`, `{{{{PAGE_NUM}}}}`
- Chapter: `{{{{CHAPTER_NUM}}}}`, `{{{{CHAPTER_TITLE}}}}`, `{{{{CHAPTER_TITLE_EN}}}}`
- TOC: `{{{{TOC_ITEM_1_TITLE}}}}`...
- Ending: `{{{{THANK_YOU}}}}`, `{{{{CONTACT_INFO}}}}`

## Notes
- This template was imported from a PPTX file through the review-first V1 importer.
- LLM analysis status: {llm_status}.
"""


# ── Optional LLM-assisted review ─────────────────────────────────────────────


def _read_design_spec_reference(max_chars: int = 36000) -> str:
    path = settings.templates_dir / "design_spec_reference.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text[:max_chars]


def _strip_markdown_fence(text: str) -> str:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:markdown)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _read_svg_excerpt(import_id: str, slide_index: int, max_chars: int = 12000) -> str:
    svg_path = _work_dir(import_id) / "svg" / f"slide_{slide_index:02d}.svg"
    try:
        text = svg_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text[:max_chars]


def _strip_data_uris(svg_text: str) -> str:
    return re.sub(r"data:image/[^\"')\s]+", "data:image/*;base64,<omitted>", svg_text)


def _compact_llm_trace_value(value: Any, key: str = "") -> Any:
    if isinstance(value, str):
        limit = 5000 if "svg" in key or "preview" in key else 1800
        text = _strip_data_uris(value) if "svg" in key or "preview" in key else value
        if len(text) > limit:
            return text[:limit] + f"\n... <truncated {len(text) - limit} chars>"
        return text
    if isinstance(value, list):
        return [_compact_llm_trace_value(item, key) for item in value[:30]]
    if isinstance(value, dict):
        return {str(k): _compact_llm_trace_value(v, str(k)) for k, v in value.items()}
    return value


def _draft_signature(review: dict[str, Any]) -> str:
    draft = review.get("draft", {})
    relevant = {
        "page_selections": draft.get("page_selections", {}),
        "assets": draft.get("assets", {}),
        "preserve_texts": draft.get("preserve_texts", []),
        "placeholder_hints": draft.get("placeholder_hints", {}),
        "element_actions": draft.get("element_actions", []),
        "annotations": review.get("annotations", []),
    }
    return json.dumps(relevant, ensure_ascii=False, sort_keys=True)


def _append_conversation_message(
    review: dict[str, Any],
    role: str,
    content: str,
    meta: dict[str, Any] | None = None,
) -> None:
    messages = review.setdefault("conversation", [])
    messages.append(
        {
            "role": role,
            "content": content,
            "created_at": time.time(),
            "meta": meta or {},
        }
    )
    if len(messages) > 40:
        review["conversation"] = messages[-40:]


def _review_manifest_slides(import_id: str) -> dict[int, dict[str, Any]]:
    manifest = _read_review_manifest(import_id)
    return {
        int(slide.get("index")): slide
        for slide in manifest.get("slides", [])
        if isinstance(slide, dict) and slide.get("index")
    }


def _manifest_summary_for_llm(
    manifest: dict[str, Any],
    review: dict[str, Any],
    *,
    feedback: str | None = None,
    current_preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    slides = []
    import_id = str(review.get("import_id") or "")
    review_slides_by_index = {
        int(slide.get("index")): slide
        for slide in review.get("slides", [])
        if slide.get("index")
    }
    sidecar_slides_by_index = _review_manifest_slides(import_id)
    for slide in manifest.get("slides", [])[:80]:
        slide_index = int(slide.get("index") or 0)
        review_slide = review_slides_by_index.get(slide_index) or {}
        sidecar_slide = sidecar_slides_by_index.get(slide_index) or {}
        elements = review_slide.get("elements") or sidecar_slide.get("elements") or []
        slides.append(
            {
                "index": slide.get("index"),
                "page_type_guess": PAGE_TYPE_MAP.get(slide.get("pageType", ""), "content"),
                "text_samples": (slide.get("textSamples") or [])[:12],
                "text_count": slide.get("textCount"),
                "shape_count": slide.get("shapeCount"),
                "image_count": len(slide.get("imageAssets") or []),
                "elements": elements[:140],
                "svg_excerpt": _read_svg_excerpt(import_id, slide_index),
            }
        )

    assets = []
    for asset in review.get("assets", [])[:80]:
        assets.append(
            {
                "asset_id": asset.get("asset_id"),
                "file_name": asset.get("file_name"),
                "image_size": asset.get("image_size"),
                "usage_count": asset.get("usage_count"),
                "pages": asset.get("pages"),
                "position_stable": asset.get("position_stable"),
                "rule_role": asset.get("recommended_role"),
                "occurrences": (asset.get("occurrences") or [])[:20],
            }
        )

    preview_payload = None
    if current_preview:
        preview_payload = {
            "label": current_preview.get("label", ""),
            "cover_svg_excerpt": _strip_data_uris(str(current_preview.get("cover_svg") or ""))[:12000],
            "toc_svg_excerpt": _strip_data_uris(str(current_preview.get("toc_svg") or ""))[:12000],
            "chapter_svg_excerpt": _strip_data_uris(str(current_preview.get("chapter_svg") or ""))[:12000],
            "content_svg_excerpt": _strip_data_uris(str(current_preview.get("content_svg") or ""))[:12000],
            "ending_svg_excerpt": _strip_data_uris(str(current_preview.get("ending_svg") or ""))[:12000],
        }

    return {
        "source": manifest.get("source", {}),
        "slide_size": manifest.get("slideSize", {}),
        "theme": manifest.get("theme", {}),
        "page_type_candidates": review.get("page_type_candidates", {}),
        "current_page_selections": review.get("draft", {}).get("page_selections", {}),
        "current_draft": review.get("draft", {}),
        "feedback_history": review.get("feedback_history", [])[-6:],
        "user_feedback": feedback or "",
        "user_annotations": _summarize_user_annotations(review),
        "current_template_preview": preview_payload,
        "slides": slides,
        "asset_candidates": assets,
        "repeated_text_candidates": review.get("text_candidates", [])[:80],
    }


def _summarize_user_annotations(review: dict[str, Any]) -> list[str]:
    """Serialize user-drawn visual notes into stable, LLM-readable bullets."""
    raw = review.get("annotations") or []
    items: list[tuple[int, float, dict[str, Any]]] = []
    for entry in raw:
        if not isinstance(entry, dict) or entry.get("resolved"):
            continue
        try:
            slide_index = int(entry.get("slide_index") or 0)
        except (TypeError, ValueError):
            slide_index = 0
        try:
            created_at = float(entry.get("created_at") or 0.0)
        except (TypeError, ValueError):
            created_at = 0.0
        items.append((slide_index, created_at, entry))
    items.sort(key=lambda item: (item[0], item[1]))

    bullets: list[str] = []
    for slide_index, _, entry in items:
        bbox = entry.get("bbox_norm") if isinstance(entry.get("bbox_norm"), dict) else {}
        note = str(entry.get("note") or "").strip()
        if not note:
            continue
        try:
            x = float(bbox.get("x") or 0.0)
            y = float(bbox.get("y") or 0.0)
            w = float(bbox.get("width") or 0.0)
            h = float(bbox.get("height") or 0.0)
        except (TypeError, ValueError):
            x = y = w = h = 0.0
        linked = entry.get("linked_element_id")
        link_text = f" [element={linked}]" if linked else ""
        bullets.append(
            f"On slide {slide_index}, region "
            f"(x={x * 100:.1f}%, y={y * 100:.1f}%, "
            f"w={w * 100:.1f}%, h={h * 100:.1f}%){link_text}: {note}"
        )
    return bullets


def _resolve_user_annotations(review: dict[str, Any]) -> int:
    annotations = review.get("annotations")
    if not isinstance(annotations, list):
        return 0
    resolved_count = 0
    next_annotations: list[Any] = []
    for annotation in annotations:
        if isinstance(annotation, dict) and not annotation.get("resolved"):
            annotation = {**annotation, "resolved": True}
            resolved_count += 1
        next_annotations.append(annotation)
    if resolved_count:
        review["annotations"] = next_annotations
        draft = review.get("draft")
        if isinstance(draft, dict) and isinstance(draft.get("annotations"), list):
            draft["annotations"] = [
                {**item, "resolved": True} if isinstance(item, dict) and not item.get("resolved") else item
                for item in draft["annotations"]
            ]
    return resolved_count


def _extract_json_object(text: str) -> str:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.S)
    if fenced:
        return fenced.group(1)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return stripped[start : end + 1]
    return stripped


def _parse_llm_import_plan(response: Any) -> LLMTemplateImportPlan:
    raw = getattr(response, "raw", None)
    try:
        parsed = raw.choices[0].message.parsed
        if isinstance(parsed, LLMTemplateImportPlan):
            return parsed
        if isinstance(parsed, BaseModel):
            return LLMTemplateImportPlan.model_validate(parsed.model_dump())
    except (AttributeError, IndexError, TypeError):
        pass

    return _parse_llm_import_plan_text(getattr(response, "content", "") or "")


def _parse_llm_import_plan_text(content: str) -> LLMTemplateImportPlan:
    json_text = _extract_json_object(content)
    if not json_text:
        raise ValueError("LLM template analysis returned an empty response.")
    if not json_text.lstrip().startswith("{"):
        raise ValueError("LLM template analysis did not return a JSON object.")
    try:
        return LLMTemplateImportPlan.model_validate_json(json_text)
    except (ValidationError, ValueError):
        return LLMTemplateImportPlan.model_validate(json.loads(json_text))


async def _repair_llm_import_plan_json(
    provider: Any,
    model_config: dict[str, Any],
    malformed_content: str,
    parse_error: Exception,
) -> LLMTemplateImportPlan:
    repair_messages = [
        LLMMessage.system(
            "You repair malformed JSON. Return only valid JSON matching the requested schema. "
            "Do not add markdown, comments, or explanations. Do not change the user's intended values."
        ),
        LLMMessage.user(
            "Repair this malformed JSON response for the PPTX template import plan. "
            f"The parser error was: {parse_error}\n\n{malformed_content}"
        ),
    ]
    response = await provider.chat(
        repair_messages,
        model_config["model"],
        temperature=0.0,
        max_tokens=8192,
    )
    return _parse_llm_import_plan_text(response.content or "")


async def _regenerate_llm_import_plan_json(
    provider: Any,
    model_config: dict[str, Any],
    payload: dict[str, Any],
    parse_error: Exception,
) -> LLMTemplateImportPlan:
    retry_messages = [
        LLMMessage.system(
            "You generate a valid JSON object for a PPTX template import plan. "
            "Return only JSON. No markdown, no prose, no code fences. "
            "All required top-level arrays must be present, even when empty."
        ),
        LLMMessage.user(
            "The previous response was empty or not a JSON object. Generate the template import plan again. "
            f"The parser error was: {parse_error}\n\n"
            "Required JSON fields: label, page_selections, asset_decisions, placeholder_decisions, "
            "element_actions, preserve_texts, notes. "
            "element_actions must use element_id values from slides[].elements.\n\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        ),
    ]
    response = await provider.chat(
        retry_messages,
        model_config["model"],
        temperature=0.0,
        max_tokens=8192,
    )
    return _parse_llm_import_plan_text(response.content or "")


async def _call_template_import_llm(model_config: dict[str, Any], payload: dict[str, Any]) -> LLMTemplateImportPlan:
    provider = create_provider(
        model_config["provider"],
        model_config["api_key"],
        base_url=model_config.get("base_url"),
        deepseek_settings=model_config.get("deepseek_settings"),
        openai_settings=model_config.get("openai_settings"),
    )
    system = (
        "You are a senior presentation template import analyst. You receive exhaustive PPTX evidence: "
        "slide text, SVG element geometry, SVG excerpts, image asset metadata, repeated text, previous draft state, "
        "and optional user feedback. Your job is to turn a content-filled deck into a reusable template plan. "
        "Treat normal body copy, charts, tables, screenshots, and one-off photos as source content to remove. "
        "Keep only reusable framework elements such as logos, headers, nav bars, footers, page numbers, backgrounds, "
        "brand marks, and decorative systems. Do not write design_spec.md in this step."
    )
    user = (
        "Analyze this PPTX template import draft and return exactly one valid JSON object. "
        "No markdown, no code fence, no comments, no trailing commas.\n\n"
        "Required JSON shape:\n"
        "{"
        "\"label\": string|null,"
        "\"page_selections\": {\"cover\": number|null, \"toc\": number|null, \"chapter\": number|null, \"content\": number|null, \"ending\": number|null},"
        "\"asset_decisions\": [{\"asset_id\": string, \"role\": \"logo\"|\"background\"|\"decoration\"|\"content_image\"|\"ignore\", \"name\": string|null, \"confidence\": number, \"reason\": string}],"
        "\"placeholder_decisions\": [{\"page_type\": \"cover\"|\"toc\"|\"chapter\"|\"content\"|\"ending\", \"placeholder\": string, \"source_text\": string|null}],"
        "\"element_actions\": [{\"page_type\": \"cover\"|\"toc\"|\"chapter\"|\"content\"|\"ending\", \"element_id\": string, \"action\": \"keep\"|\"remove\"|\"replace_with_placeholder\", \"placeholder\": string|null, \"reason\": string}],"
        "\"preserve_texts\": [string],"
        "\"notes\": [string]"
        "}\n\n"
        "Rules:\n"
        "- element_actions are required and must use only element_id values from slides[].elements.\n"
        "- Use replace_with_placeholder for title, subtitle, page title, chapter title, content-area image/shape/text slots, TOC item labels, page numbers, and similar reusable slots.\n"
        "- Use keep for reusable visual framework elements and repeated navigation/header/footer/brand labels.\n"
        "- Use remove for source body content, one-off research images, charts, screenshots, and example data.\n"
        "- Most image assets should be content_image or ignore unless repeated placement and context strongly indicate logo/background/decoration.\n"
        "- Placeholder names must be uppercase tokens without braces, such as TITLE, SUBTITLE, PAGE_TITLE, CONTENT_AREA, TOC_ITEM_1_TITLE, CHAPTER_TITLE, PAGE_NUM, THANK_YOU.\n"
        "- If user_feedback is present, it refers to current_template_preview when provided; revise element_actions, asset roles, page selections, preserve_texts, and placeholders to visibly change that preview.\n"
        "- user_annotations are high-priority visual feedback. Each annotation gives a slide index, normalized region, and user note; match it to nearby slide elements and change element_actions/page selections/assets so the preview visibly responds.\n"
        "- If user_feedback says an element/text/number/extra character should be removed, output remove actions for the corresponding elements instead of preserving them.\n"
        "- If user_feedback says placeholders are in the wrong area or stacked in a corner, map placeholders to better original elements or replace fewer elements with placeholders.\n"
        "- Keep notes short and actionable.\n\n"
        f"Evidence JSON:\n{json.dumps(payload, ensure_ascii=False)}"
    )
    messages = [LLMMessage.system(system), LLMMessage.user(user)]
    try:
        response = await provider.chat(
            messages,
            model_config["model"],
            temperature=0.1,
            max_tokens=8192,
            response_format=LLMTemplateImportPlan,
        )
    except Exception:
        response = await provider.chat(
            messages,
            model_config["model"],
            temperature=0.1,
            max_tokens=8192,
        )
    try:
        return _parse_llm_import_plan(response)
    except Exception as parse_error:
        content = getattr(response, "content", "") or ""
        if not content:
            logger.warning("Template import LLM returned empty content; regenerating JSON plan: %s", parse_error)
            try:
                return await _regenerate_llm_import_plan_json(provider, model_config, payload, parse_error)
            except Exception as regenerate_error:
                raise ValueError(
                    "LLM template analysis did not return valid JSON after structured retry. "
                    "The model response may have been truncated or ignored the JSON schema."
                ) from regenerate_error
        logger.warning("Template import LLM returned malformed JSON; attempting repair: %s", parse_error)
        try:
            return await _repair_llm_import_plan_json(provider, model_config, content, parse_error)
        except Exception as repair_error:
            logger.warning("Template import JSON repair failed; regenerating JSON plan: %s", repair_error)
            try:
                return await _regenerate_llm_import_plan_json(provider, model_config, payload, repair_error)
            except Exception as regenerate_error:
                raise ValueError(
                    "LLM template analysis did not return valid JSON after repair and structured retry. "
                    "The model response may have been truncated or ignored the JSON schema."
                ) from regenerate_error


async def _call_template_design_spec_llm(
    model_config: dict[str, Any],
    payload: dict[str, Any],
    review: dict[str, Any],
) -> str:
    provider = create_provider(
        model_config["provider"],
        model_config["api_key"],
        base_url=model_config.get("base_url"),
        deepseek_settings=model_config.get("deepseek_settings"),
        openai_settings=model_config.get("openai_settings"),
    )
    spec_payload = {
        "source": payload.get("source"),
        "slide_size": payload.get("slide_size"),
        "theme": payload.get("theme"),
        "page_selections": review.get("draft", {}).get("page_selections", {}),
        "asset_roles": review.get("draft", {}).get("assets", {}),
        "preserve_texts": review.get("draft", {}).get("preserve_texts", []),
        "element_actions": review.get("draft", {}).get("element_actions", [])[:220],
        "notes": review.get("llm", {}).get("notes", []),
        "feedback_history": payload.get("feedback_history", []),
        "user_feedback": payload.get("user_feedback", ""),
        "current_template_preview": payload.get("current_template_preview"),
    }
    reference = _read_design_spec_reference()
    messages = [
        LLMMessage.system(
            "You write production-quality design_spec.md files for imported presentation templates. "
            "Return Markdown only. Do not wrap it in JSON or code fences. "
            "The output MUST follow the provided reference structure exactly, including sections I through XI. "
            "Adapt project/content-specific sections to a reusable imported template pack when the source is a template."
        ),
        LLMMessage.user(
            "Reference design_spec.md structure to follow:\n"
            f"{reference or '(reference unavailable)'}\n\n"
            "Write design_spec.md for this imported PPTX template. Include visual system, canvas constraints, "
            "typography, layout rules for the five page types, placeholder contract, reusable assets, "
            "generation guidance, speaker-note expectations, and technical constraints.\n\n"
            f"{json.dumps(spec_payload, ensure_ascii=False)}"
        ),
    ]
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            response = await provider.chat(
                messages,
                model_config["model"],
                temperature=0.2,
                max_tokens=8192,
            )
            text = _strip_markdown_fence(response.content or "")
            if text:
                return text
            last_error = ValueError("empty design_spec response")
        except Exception as exc:
            last_error = exc
    raise ValueError("LLM design_spec generation returned an empty response.") from last_error


def _merge_llm_plan(review: dict[str, Any], manifest: dict[str, Any], plan: LLMTemplateImportPlan) -> None:
    draft = review.setdefault("draft", {})
    valid_slides = {int(slide["index"]) for slide in review.get("slides", []) if slide.get("index")}
    if plan.label and plan.label.strip():
        draft["label"] = plan.label.strip()

    selections = draft.setdefault("page_selections", {})
    plan_selections = plan.page_selections.model_dump(exclude_none=True)
    for page_type in PAGE_TYPES:
        value = plan_selections.get(page_type)
        if value is not None and int(value) in valid_slides:
            selections[page_type] = int(value)
    if not selections.get("content") and valid_slides:
        selections["content"] = int(sorted(valid_slides)[len(valid_slides) // 2])

    assets_by_id = {asset["asset_id"]: asset for asset in review.get("assets", [])}
    draft_assets = draft.setdefault("assets", {})
    for decision in plan.asset_decisions:
        asset = assets_by_id.get(decision.asset_id)
        if not asset:
            continue
        role = decision.role
        if role in {"logo", "background", "decoration"} and not _asset_is_frequent_for_default_keep(
            asset,
            int(review.get("slide_count") or 0),
        ):
            role = decision.role if decision.confidence >= 0.88 else "ignore"
        name = (decision.name or asset.get("name") or asset.get("file_name") or "").strip()
        asset["recommended_role"] = role
        asset["role"] = role
        asset["name"] = name
        asset["recommendation_source"] = "llm"
        asset["llm_reason"] = decision.reason
        asset["llm_confidence"] = decision.confidence
        draft_assets[decision.asset_id] = {
            "role": role,
            "name": name,
        }

    llm_preserve = {_normalize_text_candidate(text) for text in plan.preserve_texts}
    next_preserve = sorted(text for text in llm_preserve if text)
    if next_preserve or not draft.get("preserve_texts"):
        draft["preserve_texts"] = next_preserve
    placeholder_hints: dict[str, dict[str, str]] = {}
    for decision in plan.placeholder_decisions:
        source_text = _normalize_text_candidate(decision.source_text or "")
        placeholder = re.sub(r"[^A-Z0-9_]", "", decision.placeholder.upper().replace("{{", "").replace("}}", ""))
        if not source_text or not placeholder:
            continue
        placeholder_hints.setdefault(decision.page_type, {})[placeholder] = source_text
    if placeholder_hints or not draft.get("placeholder_hints"):
        draft["placeholder_hints"] = placeholder_hints
    known_element_ids = {
        element.get("element_id")
        for slide in review.get("slides", [])
        for element in slide.get("elements", [])
        if element.get("element_id")
    }
    element_actions = []
    for action in plan.element_actions:
        if action.element_id not in known_element_ids:
            continue
        placeholder = ""
        if action.placeholder:
            placeholder = re.sub(r"[^A-Z0-9_]", "", action.placeholder.upper().replace("{{", "").replace("}}", ""))
        element_actions.append(
            {
                "page_type": action.page_type,
                "element_id": action.element_id,
                "action": action.action,
                "placeholder": placeholder,
                "reason": action.reason,
            }
        )
    existing_actions = draft.get("element_actions") if isinstance(draft.get("element_actions"), list) else []
    if _should_merge_sparse_element_actions(existing_actions, element_actions):
        draft["element_actions"] = _merge_element_action_patch(existing_actions, element_actions)
    elif element_actions or not existing_actions:
        draft["element_actions"] = element_actions
    if plan.design_spec_md.strip():
        draft["design_spec"] = plan.design_spec_md.strip()


def _selected_slide(review: dict[str, Any], page_type: str) -> dict[str, Any] | None:
    selections = review.get("draft", {}).get("page_selections", {})
    slide_index = selections.get(page_type)
    if slide_index is None:
        candidates = review.get("page_type_candidates", {}).get(page_type) or []
        slide_index = candidates[0] if candidates else None
    try:
        selected_index = int(slide_index)
    except (TypeError, ValueError):
        return None
    for slide in review.get("slides", []):
        try:
            if int(slide.get("index") or 0) == selected_index:
                return slide
        except (TypeError, ValueError):
            continue
    return None


def _upsert_element_action(
    draft: dict[str, Any],
    *,
    page_type: str,
    element_id: str,
    action: str,
    placeholder: str = "",
    reason: str = "",
) -> bool:
    actions = draft.setdefault("element_actions", [])
    next_action = {
        "page_type": page_type,
        "element_id": element_id,
        "action": action,
        "placeholder": placeholder,
        "reason": reason,
    }
    for existing in actions:
        if existing.get("page_type") == page_type and existing.get("element_id") == element_id:
            changed = any(existing.get(key) != value for key, value in next_action.items())
            existing.update(next_action)
            return changed
    actions.append(next_action)
    return True


def _element_action_key(action: dict[str, Any]) -> tuple[str, str]:
    return (str(action.get("page_type") or ""), str(action.get("element_id") or ""))


def _should_merge_sparse_element_actions(
    existing_actions: list[dict[str, Any]] | None,
    incoming_actions: list[dict[str, Any]],
) -> bool:
    """Treat short action lists as patches so feedback cannot wipe the plan.

    The review UI edits annotations, page selections, and asset roles; it does
    not intentionally send a brand-new minimal action plan. Some LLM feedback
    responses and stale client drafts may contain only the changed action. In
    that case replacing the full plan makes every page without actions lose its
    placeholders, so merge instead.
    """
    existing_actions = existing_actions or []
    if not existing_actions or not incoming_actions:
        return False
    if len(incoming_actions) >= len(existing_actions):
        return False
    existing_placeholders = sum(
        1 for action in existing_actions if action.get("action") == "replace_with_placeholder"
    )
    incoming_placeholders = sum(
        1 for action in incoming_actions if action.get("action") == "replace_with_placeholder"
    )
    if existing_placeholders and incoming_placeholders < existing_placeholders:
        return True
    return len(incoming_actions) < max(3, len(existing_actions) // 2)


def _merge_element_action_patch(
    existing_actions: list[dict[str, Any]],
    incoming_actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    index_by_key: dict[tuple[str, str], int] = {}
    for action in existing_actions:
        key = _element_action_key(action)
        if not key[0] or not key[1]:
            continue
        index_by_key[key] = len(merged)
        merged.append(dict(action))
    for action in incoming_actions:
        key = _element_action_key(action)
        if not key[0] or not key[1]:
            continue
        if key in index_by_key:
            merged[index_by_key[key]].update(action)
        else:
            index_by_key[key] = len(merged)
            merged.append(dict(action))
    return merged


def _feedback_has_any(feedback: str, terms: tuple[str, ...]) -> bool:
    return any(term.lower() in feedback.lower() for term in terms)


def _annotation_has_any(annotation: dict[str, Any], terms: tuple[str, ...]) -> bool:
    note = str(annotation.get("note") or "")
    return _feedback_has_any(note, terms)


def _annotation_box_px(
    annotation: dict[str, Any],
    canvas_w: int,
    canvas_h: int,
) -> dict[str, float]:
    bbox = annotation.get("bbox_norm") if isinstance(annotation.get("bbox_norm"), dict) else {}

    def val(name: str) -> float:
        try:
            value = float(bbox.get(name) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        return min(max(value, 0.0), 1.0)

    x = val("x") * canvas_w
    y = val("y") * canvas_h
    width = max(1.0, val("width") * canvas_w)
    height = max(1.0, val("height") * canvas_h)
    return {"x": x, "y": y, "width": width, "height": height}


def _box_intersection_ratio(a: dict[str, float], b: dict[str, float]) -> float:
    ax2 = a["x"] + a["width"]
    ay2 = a["y"] + a["height"]
    bx2 = b["x"] + b["width"]
    by2 = b["y"] + b["height"]
    ix = max(0.0, min(ax2, bx2) - max(a["x"], b["x"]))
    iy = max(0.0, min(ay2, by2) - max(a["y"], b["y"]))
    if ix <= 0 or iy <= 0:
        return 0.0
    intersection = ix * iy
    smaller = max(1.0, min(a["width"] * a["height"], b["width"] * b["height"]))
    return intersection / smaller


def _box_center_inside(inner: dict[str, float], outer: dict[str, float]) -> bool:
    cx = inner["x"] + inner["width"] / 2
    cy = inner["y"] + inner["height"] / 2
    return (
        outer["x"] <= cx <= outer["x"] + outer["width"]
        and outer["y"] <= cy <= outer["y"] + outer["height"]
    )


def _upsert_action_style(
    action: dict[str, Any],
    patch: dict[str, Any],
) -> bool:
    changed = False
    for key, value in patch.items():
        if action.get(key) != value:
            action[key] = value
            changed = True
    return changed


def _apply_annotation_layout_patches(
    review: dict[str, Any],
    manifest: dict[str, Any],
) -> list[str]:
    """Translate visual annotations into concrete SVG placeholder layout patches.

    The LLM action schema decides semantic operations. User annotations often
    ask for visual edits such as centering, shrinking, or separating overlapping
    placeholders; those need explicit numeric SVG attributes, otherwise the
    model can only describe the change in chat. This pass writes those
    attributes onto draft.element_actions so preview/confirm can apply them.
    """
    annotations = [
        annotation
        for annotation in review.get("annotations") or []
        if isinstance(annotation, dict)
        and not annotation.get("resolved")
        and str(annotation.get("note") or "").strip()
    ]
    if not annotations:
        return []

    draft = review.setdefault("draft", {})
    actions = draft.setdefault("element_actions", [])
    if not isinstance(actions, list):
        actions = []
        draft["element_actions"] = actions

    size = manifest.get("slideSize", {}) if isinstance(manifest, dict) else {}
    try:
        canvas_w = int(size.get("width_px") or 1280)
        canvas_h = int(size.get("height_px") or 720)
    except (TypeError, ValueError):
        canvas_w, canvas_h = 1280, 720

    slides_by_index = {
        int(slide.get("index")): slide
        for slide in review.get("slides", [])
        if isinstance(slide, dict) and slide.get("index")
    }
    selections = draft.get("page_selections", {}) if isinstance(draft.get("page_selections"), dict) else {}
    selected_page_types_by_slide: dict[int, list[str]] = {}
    for page_type in PAGE_TYPES:
        try:
            slide_index = int(selections.get(page_type) or 0)
        except (TypeError, ValueError):
            slide_index = 0
        if slide_index > 0:
            selected_page_types_by_slide.setdefault(slide_index, []).append(page_type)

    patches: list[str] = []
    for annotation in annotations:
        try:
            slide_index = int(annotation.get("slide_index") or 0)
        except (TypeError, ValueError):
            continue
        page_types = selected_page_types_by_slide.get(slide_index, [])
        if not page_types:
            continue
        slide = slides_by_index.get(slide_index) or {}
        element_boxes = {}
        for element in slide.get("elements") or []:
            if not isinstance(element, dict) or not element.get("element_id"):
                continue
            try:
                box = {
                    "x": float(element.get("x") or 0.0),
                    "y": float(element.get("y") or 0.0),
                    "width": max(1.0, float(element.get("width") or 1.0)),
                    "height": max(1.0, float(element.get("height") or 1.0)),
                }
            except (TypeError, ValueError):
                continue
            element_boxes[str(element["element_id"])] = {**box, "element": element}

        annotation_box = _annotation_box_px(annotation, canvas_w, canvas_h)
        wants_center = _annotation_has_any(annotation, ("居中", "居中对齐", "center", "centered", "middle"))
        wants_smaller = _annotation_has_any(
            annotation,
            ("太大", "过大", "字号", "字体", "缩小", "小一点", "smaller", "font size", "too large"),
        )
        wants_separate = _annotation_has_any(annotation, ("重叠", "叠", "拥挤", "挤", "overlap", "stack", "clutter"))
        wants_remove = _annotation_has_any(
            annotation,
            ("删除", "移除", "去掉", "去除", "remove", "delete", "clear"),
        )

        for page_type in page_types:
            overlapping_actions: list[tuple[dict[str, Any], dict[str, Any]]] = []
            overlapping_elements: list[dict[str, Any]] = []
            for element_id, box in element_boxes.items():
                element_box = {k: float(box[k]) for k in ("x", "y", "width", "height")}
                overlaps = (
                    _box_intersection_ratio(element_box, annotation_box) >= 0.08
                    or _box_center_inside(element_box, annotation_box)
                )
                if not overlaps:
                    continue
                overlapping_elements.append({"element_id": element_id, **box})
                for action in actions:
                    if (
                        isinstance(action, dict)
                        and action.get("page_type") == page_type
                        and action.get("element_id") == element_id
                    ):
                        overlapping_actions.append((action, box))

            if wants_remove:
                removed = 0
                for item in overlapping_elements:
                    if _upsert_element_action(
                        draft,
                        page_type=page_type,
                        element_id=str(item["element_id"]),
                        action="remove",
                        reason="User annotation requested removing this marked region.",
                    ):
                        removed += 1
                if removed:
                    patches.append(f"第 {slide_index} 页标注区域已转成删除操作：{removed} 个元素。")
                continue

            replace_actions = [
                (action, box)
                for action, box in overlapping_actions
                if action.get("action") == "replace_with_placeholder"
                and str(action.get("placeholder") or "").strip()
                and str(action.get("placeholder") or "").strip() != "CONTENT_AREA"
            ]
            if not replace_actions:
                continue
            if not (wants_center or wants_smaller or wants_separate):
                continue

            replace_actions.sort(key=lambda pair: (float(pair[1].get("y") or 0), float(pair[1].get("x") or 0)))
            count = len(replace_actions)
            center_x = annotation_box["x"] + annotation_box["width"] / 2
            line_gap = annotation_box["height"] / (count + 1)
            changed_count = 0
            for idx, (action, box) in enumerate(replace_actions):
                element = box.get("element") if isinstance(box.get("element"), dict) else {}
                try:
                    original_font = float(
                        action.get("font_size")
                        or element.get("font_size")
                        or max(14.0, min(36.0, float(box.get("height") or 24.0) * 0.75))
                    )
                except (TypeError, ValueError):
                    original_font = 18.0
                if wants_smaller or wants_separate:
                    if count == 1:
                        fitted_font = max(12.0, min(original_font, annotation_box["height"] * 0.5))
                    else:
                        fitted_font = max(10.0, min(original_font, line_gap * 0.62))
                else:
                    fitted_font = original_font

                patch: dict[str, Any] = {}
                if wants_center or wants_separate:
                    patch["x"] = round(center_x, 2)
                    patch["text_anchor"] = "middle"
                if wants_smaller or wants_separate:
                    patch["font_size"] = round(fitted_font, 2)
                if wants_separate:
                    patch["y"] = round(
                        annotation_box["y"] + line_gap * (idx + 1) + fitted_font * 0.35,
                        2,
                    )
                elif wants_center and count == 1:
                    patch["y"] = round(
                        annotation_box["y"] + annotation_box["height"] / 2 + fitted_font * 0.35,
                        2,
                    )

                if patch and _upsert_action_style(action, patch):
                    changed_count += 1

            if changed_count:
                patches.append(f"第 {slide_index} 页标注区域已应用布局补丁：{changed_count} 个占位符。")

    return patches


def _apply_feedback_action_patches(review: dict[str, Any], feedback: str) -> list[str]:
    """Translate common natural-language review feedback into concrete SVG actions.

    The LLM remains the planner, but feedback like "chapter number was not
    replaced" is easy to make deterministic. This keeps the agent loop from
    becoming a black-box note taker when the requested change maps cleanly to
    extracted SVG elements.
    """
    if not feedback.strip():
        return []
    draft = review.setdefault("draft", {})
    patches: list[str] = []
    if (
        _feedback_has_any(feedback, ("左上角", "top-left", "top left", "corner"))
        and _feedback_has_any(feedback, ("占位", "placeholder", "{{"))
    ):
        patches.append("占位符坐标改用 PPT 导出文本的 transform/tspan 原始位置。")

    if _feedback_has_any(feedback, ("章节", "chapter")) and _feedback_has_any(
        feedback,
        ("数字", "编号", "number", "num", "占位", "placeholder"),
    ):
        chapter_slide = _selected_slide(review, "chapter")
        numeric_elements = []
        for element in (chapter_slide or {}).get("elements", []):
            if element.get("type") != "text":
                continue
            text = re.sub(r"\s+", "", str(element.get("text") or ""))
            if re.fullmatch(r"(?:第)?\d{1,3}", text):
                numeric_elements.append(element)
        if numeric_elements:
            target = max(
                numeric_elements,
                key=lambda item: (
                    float(item.get("font_size") or 0),
                    float(item.get("height") or 0),
                    -float(item.get("y") or 0),
                ),
            )
            if _upsert_element_action(
                draft,
                page_type="chapter",
                element_id=str(target["element_id"]),
                action="replace_with_placeholder",
                placeholder="CHAPTER_NUM",
                reason="User feedback requested replacing the chapter number with CHAPTER_NUM.",
            ):
                patches.append("章节页数字已绑定为 CHAPTER_NUM 占位符。")

    wants_content_header_removed = _feedback_has_any(feedback, ("内容页", "content")) and _feedback_has_any(
        feedback,
        ("页眉", "header", "顶部", "top"),
    ) and _feedback_has_any(feedback, ("去掉", "删除", "移除", "remove", "clear"))
    if wants_content_header_removed:
        content_slide = _selected_slide(review, "content")
        repeated_texts = {
            _normalize_text_candidate(str(item.get("text") or ""))
            for item in review.get("text_candidates", [])
            if int(item.get("page_count") or len(item.get("pages") or [])) >= 2
        }
        preserve_texts = {
            _normalize_text_candidate(str(text))
            for text in draft.get("preserve_texts", [])
            if _normalize_text_candidate(str(text))
        }
        removed_count = 0
        for element in (content_slide or {}).get("elements", []):
            if element.get("type") != "text":
                continue
            text = str(element.get("text") or "").strip()
            norm = _normalize_text_candidate(text)
            if not norm or norm in repeated_texts or norm in preserve_texts:
                continue
            y = float(element.get("y") or 0)
            height = float(element.get("height") or 0)
            if y + height > 190:
                continue
            if any(brand in norm for brand in ("中国科学院", "大学", "研究院", "logo", "Logo")) and len(norm) > 6:
                continue
            if _upsert_element_action(
                draft,
                page_type="content",
                element_id=str(element["element_id"]),
                action="remove",
                reason="User feedback requested removing content-page header text.",
            ):
                removed_count += 1
        if removed_count:
            patches.append(f"内容页顶部非复用文本已标记删除：{removed_count} 个元素。")

    if _feedback_has_any(feedback, ("日期", "date")) and _feedback_has_any(feedback, ("合并", "merge", "重复", "multiple")):
        date_actions = [
            action
            for action in draft.get("element_actions", [])
            if action.get("page_type") == "cover"
            and action.get("action") == "replace_with_placeholder"
            and action.get("placeholder") == "DATE"
        ]
        for action in date_actions[1:]:
            action["action"] = "remove"
            action["placeholder"] = ""
            action["reason"] = "User feedback requested merging duplicate date placeholders."
        if len(date_actions) > 1:
            patches.append("封面重复 DATE 占位已合并为一个。")

    if "element_actions" in draft:
        draft["element_actions"] = [
            action
            for action in draft["element_actions"]
            if action.get("action") in {"keep", "remove", "replace_with_placeholder"} and action.get("element_id")
        ]
    return patches


async def assist_import_review(
    import_id: str,
    model_config: dict[str, Any] | None,
    *,
    required: bool = False,
    feedback: str | None = None,
) -> dict[str, Any]:
    review = get_import_review(import_id)
    if review is None:
        raise FileNotFoundError(import_id)
    work_dir = _work_dir(import_id)
    manifest = _safe_read_json(work_dir / "manifest.json")
    if not manifest:
        raise FileNotFoundError(f"manifest for {import_id}")

    if not model_config or not model_config.get("api_key") or not model_config.get("model"):
        review["llm"] = {"enabled": False, "status": "missing_config"}
        _write_json(_review_path(import_id), review)
        if required:
            _update_state(
                import_id,
                status="error",
                stage="llm_review",
                progress=0.82,
                message="Template import requires an LLM model configuration.",
                error="Template import requires an LLM model configuration.",
                review_required=False,
            )
            raise ValueError("Template import requires an LLM model configuration.")
        review.setdefault("draft", {})["design_spec"] = _generate_design_spec(
            manifest,
            review,
            review.get("template_id", ""),
            review.get("draft", {}).get("label") or review.get("label") or "Imported Template",
        )
        _write_json(_review_path(import_id), review)
        return review

    feedback_text = (feedback or "").strip()
    if feedback_text:
        history = review.setdefault("feedback_history", [])
        history.append(
            {
                "feedback": feedback_text,
                "created_at": time.time(),
            }
        )
        _append_conversation_message(review, "user", feedback_text)
        _write_json(_review_path(import_id), review)

    _update_state(
        import_id,
        status="processing",
        stage="llm_review",
        progress=0.88,
        message="Optimizing template draft with feedback." if feedback_text else "Running intelligent template analysis.",
        review_required=False,
    )
    try:
        current_preview = None
        try:
            current_preview = preview_import_template(import_id)
        except Exception as preview_exc:
            logger.warning("Could not build current template preview for LLM context: %s", preview_exc)
        payload = _manifest_summary_for_llm(
            manifest,
            review,
            feedback=feedback_text,
            current_preview=current_preview,
        )
        before_signature = _draft_signature(review)
        plan = await _call_template_import_llm(model_config, payload)
        _merge_llm_plan(review, manifest, plan)
        rule_patches = _apply_feedback_action_patches(review, feedback_text)
        rule_patches.extend(_apply_annotation_layout_patches(review, manifest))
        after_signature = _draft_signature(review)
        changed = before_signature != after_signature or bool(rule_patches)
        retried_no_change = False
        if feedback_text and not changed:
            retried_no_change = True
            retry_payload = {
                **payload,
                "previous_attempt_no_visible_change": True,
                "must_change_instruction": (
                    "The previous feedback attempt produced no change to page selections, asset roles, "
                    "preserved texts, placeholder hints, or element_actions. Re-read user_feedback and "
                    "current_template_preview. Return a revised action plan that makes visible changes."
                ),
            }
            plan = await _call_template_import_llm(model_config, retry_payload)
            _merge_llm_plan(review, manifest, plan)
            rule_patches.extend(_apply_feedback_action_patches(review, feedback_text))
            rule_patches.extend(_apply_annotation_layout_patches(review, manifest))
            after_retry_signature = _draft_signature(review)
            changed = before_signature != after_retry_signature or bool(rule_patches)
            payload = retry_payload
        if feedback_text:
            _resolve_user_annotations(review)
        review["llm"] = {
            "enabled": True,
            "status": "complete",
            "provider": model_config.get("provider"),
            "model": model_config.get("model"),
            "notes": plan.notes,
            "iteration": len(review.get("feedback_history", [])) + 1,
            "changed": changed,
            "retried_no_change": retried_no_change,
            "rule_patches": rule_patches,
        }
        review["llm_trace"] = {
            "iteration": review["llm"]["iteration"],
            "updated_at": time.time(),
            "user_feedback": feedback_text,
            "changed": changed,
            "retried_no_change": retried_no_change,
            "rule_patches": rule_patches,
            "input": _compact_llm_trace_value(payload),
            "action_plan": plan.model_dump(mode="json"),
        }
        if feedback_text or not review.get("draft", {}).get("design_spec"):
            review.setdefault("draft", {})["design_spec"] = await _call_template_design_spec_llm(
                model_config,
                payload,
                review,
            )
        if feedback_text:
            if changed:
                notes = [*rule_patches, *plan.notes[:4]]
                assistant_text = "\n".join(notes) or "已根据反馈更新模板草稿，并重新生成预览。"
            else:
                assistant_text = "本轮 LLM 没有产生可见的 action plan 变更。请展开 LLM 输入/输出查看它看到的内容和返回的动作。"
            _append_conversation_message(
                review,
                "assistant",
                assistant_text,
                {"changed": changed, "iteration": review["llm"]["iteration"]},
            )
        elif not review.get("conversation"):
            _append_conversation_message(
                review,
                "assistant",
                "\n".join(plan.notes[:4]) or "已生成模板草稿，请先检查草稿预览，再决定是否反馈优化或注册。",
                {"changed": changed, "iteration": review["llm"]["iteration"]},
            )
    except Exception as exc:
        logger.exception("Template import LLM analysis failed")
        review["llm"] = {
            "enabled": True,
            "status": "error",
            "provider": model_config.get("provider"),
            "model": model_config.get("model"),
            "error": str(exc),
        }
        if feedback_text:
            _append_conversation_message(review, "assistant", f"反馈优化失败：{exc}", {"error": True})
        _write_json(_review_path(import_id), review)
        if required:
            _update_state(
                import_id,
                status="error",
                stage="llm_review",
                progress=0.88,
                message="LLM template analysis failed.",
                error=str(exc),
                review_required=False,
            )
            raise
        review.setdefault("draft", {})["design_spec"] = _generate_design_spec(
            manifest,
            review,
            review.get("template_id", ""),
            review.get("draft", {}).get("label") or review.get("label") or "Imported Template",
        )

    _write_json(_review_path(import_id), review)
    _update_state(
        import_id,
        status="review_required",
        stage="review",
        progress=0.9,
        message="Review page types and reusable assets before registering the template.",
        review_required=True,
        label=review.get("draft", {}).get("label") or review.get("label"),
        theme_colors=review.get("theme_colors", []),
    )
    return review


async def generate_direct_import_design_spec(import_id: str, model_config: dict[str, Any]) -> dict[str, Any]:
    """Generate ``design_spec.md`` for Direct mode before auto-registration."""
    review = _ensure_direct_review(import_id)
    work_dir = _work_dir(import_id)
    manifest = _safe_read_json(work_dir / "manifest.json")
    if not manifest:
        raise FileNotFoundError(f"manifest for {import_id}")

    _update_state(
        import_id,
        status="processing",
        stage="llm_review",
        progress=0.86,
        message="Generating Direct template design_spec.md with LLM.",
        review_required=False,
    )
    current_preview = None
    try:
        current_preview = _direct_template_preview(import_id)
    except Exception as preview_exc:
        logger.warning("Could not build direct template preview for design_spec context: %s", preview_exc)
    payload = _manifest_summary_for_llm(
        manifest,
        review,
        feedback="Generate a complete design_spec.md for this Direct five-slide template.",
        current_preview=current_preview,
    )
    try:
        spec = await _call_template_design_spec_llm(model_config, payload, review)
    except Exception as exc:
        _update_state(
            import_id,
            status="error",
            stage="llm_review",
            progress=0.86,
            message="Direct template design_spec.md generation failed.",
            error=str(exc) or exc.__class__.__name__,
            review_required=False,
        )
        raise
    draft = review.setdefault("draft", {})
    draft["design_spec"] = spec
    review["llm"] = {
        "enabled": True,
        "status": "complete",
        "provider": model_config.get("provider"),
        "model": model_config.get("model"),
        "direct": True,
        "notes": ["Generated design_spec.md for Direct mode."],
    }
    review["llm_trace"] = {
        "iteration": 1,
        "updated_at": time.time(),
        "user_feedback": "",
        "changed": True,
        "retried_no_change": False,
        "rule_patches": [],
        "input": _compact_llm_trace_value(payload),
        "action_plan": {"design_spec_md": spec},
    }
    _write_json(_review_path(import_id), review)
    return review


# ── Main workflow ────────────────────────────────────────────────────────────


def import_pptx_template(pptx_path: Path, *, task_id: str | None = None) -> ImportResult:
    """Analyze a PPTX and persist a review draft; does not register a template."""
    import_id = task_id or hashlib.sha1(str(pptx_path).encode("utf-8")).hexdigest()[:12]
    state = get_import_task(import_id) or initialize_import_task(import_id, pptx_path)
    template_id = state.get("template_id") or _generate_template_id(pptx_path.name, import_id)
    label = state.get("label") or _label_from_name(pptx_path.name)
    work_dir = _work_dir(import_id)
    svg_raw_dir = work_dir / "svg_raw"
    svg_clean_dir = work_dir / "svg"

    try:
        work_dir.mkdir(parents=True, exist_ok=True)

        _update_state(import_id, status="processing", stage="analyzing", progress=0.12, message="Analyzing PPTX structure.")
        manifest = build_manifest(pptx_path, work_dir)
        slide_count = len(manifest.get("slides", []))

        _update_state(
            import_id,
            status="processing",
            stage="rendering",
            progress=0.34,
            message="Rendering slides to SVG.",
            slide_count=slide_count,
        )
        svg_files, export_mode = _export_slides_to_svg(pptx_path, svg_raw_dir)
        if not svg_files:
            raise RuntimeError(
                "Could not render PPTX slides to SVG. Please run on a Windows host with PowerPoint available."
            )
        try:
            _export_slide_previews_to_png(pptx_path, work_dir / "preview_png")
        except Exception as exc:
            logger.info("PowerPoint PNG preview export failed (%s); falling back to SVG previews", exc)
        try:
            from backend.generator.pptx_scene import ensure_scene_cache, scene_paths

            ensure_scene_cache(pptx_path, scene_paths(_import_dir(import_id) / "pptx_scene"))
        except Exception as exc:
            logger.info("PowerPoint scene cache generation failed (%s); lazy scene endpoints remain available", exc)

        _update_state(
            import_id,
            status="processing",
            stage="detecting_assets",
            progress=0.58,
            message="Cleaning SVGs and detecting reusable assets.",
            export_mode=export_mode,
        )
        externalize_svg_batch(
            svg_files=svg_files,
            output_dir=svg_clean_dir,
            assets_dir=work_dir / "assets",
        )
        cleaned_svgs = sorted(svg_clean_dir.glob("slide_*.svg"))
        if cleaned_svgs:
            optimize_reference_batch([str(svg_clean_dir)], precision=2)
        cleaned_svgs = sorted(svg_clean_dir.glob("slide_*.svg"))
        if not cleaned_svgs:
            raise RuntimeError("No cleaned SVG files were produced.")

        review = _build_review_payload(import_id, manifest, cleaned_svgs, export_mode, template_id, label)
        _write_json(_review_path(import_id), review)
        _write_json(work_dir / "manifest.json", manifest)

        _update_state(
            import_id,
            status="processing",
            stage="baseline_preview",
            progress=0.74,
            message="Generating a rule-based template draft with placeholders.",
            review_required=False,
            template_id=template_id,
            label=label,
            export_mode=export_mode,
            slide_count=slide_count,
            theme_colors=review.get("theme_colors", []),
            error=None,
        )
        preview_import_template(import_id)

        state = _update_state(
            import_id,
            status="processing",
            stage="llm_review",
            progress=0.82,
            message="Waiting for mandatory LLM template analysis.",
            review_required=False,
            template_id=template_id,
            label=label,
            export_mode=export_mode,
            slide_count=slide_count,
            theme_colors=review.get("theme_colors", []),
            error=None,
        )
        return _result_from_state(state)
    except Exception as exc:
        logger.exception("Template import failed")
        state = _update_state(
            import_id,
            status="error",
            stage=state.get("stage", "analyzing"),
            progress=float(state.get("progress") or 0),
            message="Template import failed.",
            error=str(exc),
            review_required=False,
        )
        return _result_from_state(state)


def get_import_review(import_id: str) -> dict[str, Any] | None:
    review = _safe_read_json(_review_path(import_id))
    return review or None


def save_import_review(import_id: str, draft: dict[str, Any]) -> dict[str, Any]:
    review = get_import_review(import_id)
    if review is None:
        raise FileNotFoundError(import_id)

    current = review.setdefault("draft", {})
    if "label" in draft:
        current["label"] = str(draft.get("label") or review.get("label") or "").strip()
    if "page_selections" in draft and isinstance(draft["page_selections"], dict):
        selections = current.setdefault("page_selections", {})
        for page_type in PAGE_TYPES:
            if page_type in draft["page_selections"]:
                value = draft["page_selections"][page_type]
                selections[page_type] = int(value) if value else None
    if "assets" in draft and isinstance(draft["assets"], dict):
        assets = current.setdefault("assets", {})
        known_ids = {asset["asset_id"] for asset in review.get("assets", [])}
        for asset_id, value in draft["assets"].items():
            if asset_id not in known_ids or not isinstance(value, dict):
                continue
            role = value.get("role")
            if role not in ASSET_ROLES:
                role = "content_image"
            assets[asset_id] = {
                "role": role,
                "name": str(value.get("name") or "").strip(),
            }
    if "preserve_texts" in draft and isinstance(draft["preserve_texts"], list):
        preserve_texts = [
            str(text).strip()
            for text in draft["preserve_texts"]
            if str(text).strip()
        ]
        if preserve_texts or not current.get("preserve_texts"):
            current["preserve_texts"] = preserve_texts
    if "placeholder_hints" in draft and isinstance(draft["placeholder_hints"], dict):
        hints: dict[str, dict[str, str]] = {}
        for page_type in PAGE_TYPES:
            raw_mapping = draft["placeholder_hints"].get(page_type)
            if not isinstance(raw_mapping, dict):
                continue
            hints[page_type] = {
                str(key).strip(): str(value).strip()
                for key, value in raw_mapping.items()
                if str(key).strip() and str(value).strip()
            }
        if hints or not current.get("placeholder_hints"):
            current["placeholder_hints"] = hints
    if "element_actions" in draft and isinstance(draft["element_actions"], list):
        actions: list[dict[str, Any]] = []
        for action in draft["element_actions"]:
            if not isinstance(action, dict):
                continue
            if action.get("page_type") not in PAGE_TYPES:
                continue
            if action.get("action") not in {"keep", "remove", "replace_with_placeholder"}:
                continue
            element_id = str(action.get("element_id") or "").strip()
            if not element_id:
                continue
            normalized_action: dict[str, Any] = {
                "page_type": action["page_type"],
                "element_id": element_id,
                "action": action["action"],
                "placeholder": str(action.get("placeholder") or "").strip(),
                "reason": str(action.get("reason") or "").strip(),
            }
            for numeric_key in ("x", "y", "width", "height", "font_size"):
                if numeric_key not in action:
                    continue
                try:
                    normalized_action[numeric_key] = float(action[numeric_key])
                except (TypeError, ValueError):
                    continue
            for text_key in ("font_weight", "fill", "font_family", "text_anchor"):
                value = str(action.get(text_key) or "").strip()
                if value:
                    normalized_action[text_key] = value
            actions.append(normalized_action)
        existing_actions = current.get("element_actions") if isinstance(current.get("element_actions"), list) else []
        if _should_merge_sparse_element_actions(existing_actions, actions):
            current["element_actions"] = _merge_element_action_patch(existing_actions, actions)
        elif actions or not existing_actions:
            current["element_actions"] = actions
    if "design_spec" in draft:
        current["design_spec"] = str(draft.get("design_spec") or "").strip()
    if "annotations" in draft and isinstance(draft["annotations"], list):
        # Store annotations top-level so both the review UI and LLM payload
        # see the same visual feedback notes.
        review["annotations"] = [
            annotation
            for annotation in draft["annotations"]
            if isinstance(annotation, dict)
        ]

    _write_json(_review_path(import_id), review)
    _update_state(import_id, label=current.get("label") or review.get("label"))
    return review


def _review_template_context(import_id: str, *, require_llm_complete: bool = True) -> dict[str, Any]:
    review = get_import_review(import_id)
    state = get_import_task(import_id)
    if review is None or state is None:
        raise FileNotFoundError(import_id)
    is_agent_mode = state.get("collaboration_mode") == "agent"
    if require_llm_complete and not is_agent_mode and review.get("llm", {}).get("status") != "complete":
        raise ValueError("Template import must complete LLM analysis before preview or registration.")
    element_actions = review.get("draft", {}).get("element_actions") or []

    work_dir = _work_dir(import_id)
    manifest = _safe_read_json(work_dir / "manifest.json")
    template_id = review.get("template_id") or state.get("template_id")
    if not str(template_id).startswith("user_"):
        raise ValueError("Reviewed imports must register as user templates.")
    label = review.get("draft", {}).get("label") or review.get("label") or template_id
    template_dir = _templates_root() / template_id
    svg_clean_dir = work_dir / "svg"
    size = manifest.get("slideSize", {})
    canvas_w = int(size.get("width_px") or 1280)
    canvas_h = int(size.get("height_px") or 720)

    selections = review.get("draft", {}).get("page_selections", {})
    if not selections.get("content"):
        raise ValueError("A content representative slide is required.")

    assets_by_id = {asset["asset_id"]: asset for asset in review.get("assets", [])}
    asset_roles_by_file: dict[str, str] = {}
    for asset_id, draft in review.get("draft", {}).get("assets", {}).items():
        asset = assets_by_id.get(asset_id)
        if not asset:
            continue
        asset_roles_by_file[asset["file_name"]] = draft.get("role", "content_image")
    preserve_texts: set[str] = set()
    for raw_text in review.get("draft", {}).get("preserve_texts", []) or []:
        if isinstance(raw_text, str):
            text = raw_text
        elif isinstance(raw_text, dict):
            text = str(
                raw_text.get("text")
                or raw_text.get("value")
                or raw_text.get("content")
                or raw_text.get("label")
                or ""
            )
        else:
            text = ""
        norm = _normalize_text_candidate(text)
        if norm:
            preserve_texts.add(norm)
    placeholder_hints = review.get("draft", {}).get("placeholder_hints") or {}
    design_spec = review.get("draft", {}).get("design_spec") or _generate_design_spec(manifest, review, template_id, label)
    if not str(design_spec).strip():
        raise ValueError("LLM template analysis did not produce design_spec.md.")

    return {
        "review": review,
        "state": state,
        "work_dir": work_dir,
        "manifest": manifest,
        "template_id": template_id,
        "label": label,
        "svg_clean_dir": svg_clean_dir,
        "canvas_w": canvas_w,
        "canvas_h": canvas_h,
        "selections": selections,
        "asset_roles_by_file": asset_roles_by_file,
        "preserve_texts": preserve_texts,
        "placeholder_hints": placeholder_hints,
        "element_actions": element_actions,
        "design_spec": design_spec,
    }


def _placeholder_token_from_text(text: str) -> str | None:
    value = html.unescape(text or "").strip()
    if value.startswith("{{") and value.endswith("}}"):
        value = value[2:-2]
    elif value.startswith("{") and value.endswith("}"):
        value = value[1:-1]
    else:
        return None
    token = re.sub(r"\s+", "", value).upper()
    if re.fullmatch(r"[A-Z][A-Z0-9_]{1,64}", token):
        return token
    return None


def _strip_svg_style_property(style: str, property_name: str) -> str:
    pieces: list[str] = []
    target = property_name.lower()
    for part in style.split(";"):
        if not part.strip():
            continue
        key = part.split(":", 1)[0].strip().lower()
        if key == target:
            continue
        pieces.append(part.strip())
    return ";".join(pieces)


def _first_svg_coord(value: str) -> str:
    return value.replace(",", " ").split()[0] if value.strip() else value


def _normalize_agent_svg_placeholders(svg_text: str) -> str:
    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError:
        return re.sub(
            r"\{\s*([A-Za-z][A-Za-z0-9_\s]{1,80})\s*\}",
            lambda match: "{{" + re.sub(r"\s+", "", match.group(1)).upper() + "}}",
            svg_text,
        )

    changed = False
    for elem in root.iter():
        if elem.tag.rsplit("}", 1)[-1] != "text":
            continue
        token = _placeholder_token_from_text("".join(elem.itertext()))
        if not token:
            continue
        for child in list(elem):
            elem.remove(child)
        elem.text = "{{" + token + "}}"
        for attr in list(elem.attrib):
            local = attr.rsplit("}", 1)[-1]
            if local in {"x", "dx"}:
                elem.set(attr, _first_svg_coord(elem.attrib[attr]))
            elif local == "letter-spacing":
                elem.attrib.pop(attr, None)
            elif local == "style":
                style = _strip_svg_style_property(elem.attrib[attr], "letter-spacing")
                if style:
                    elem.set(attr, style)
                else:
                    elem.attrib.pop(attr, None)
        changed = True
    if not changed:
        return svg_text
    return ET.tostring(root, encoding="unicode")


def _normalize_agent_svg_file(path: Path) -> None:
    try:
        original = path.read_text(encoding="utf-8")
    except OSError:
        return
    normalized = _normalize_agent_svg_placeholders(original)
    if normalized != original:
        path.write_text(normalized, encoding="utf-8")


def _agent_template_dir(import_id: str) -> Path:
    return _import_dir(import_id) / "agent_template"


def _agent_template_files(agent_dir: Path, context: dict[str, Any] | None = None) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for page_type, file_name in PAGE_TYPE_FILES.items():
        path = agent_dir / file_name
        if path.exists() and path.is_file():
            _normalize_agent_svg_file(path)
            files[page_type] = path
    return files


def _read_agent_source_marker(svg_text: str) -> dict[str, str]:
    match = re.search(r"<!--\s*agent-source\s+([^>]*)-->", svg_text)
    if not match:
        return {}
    return {
        key: value
        for key, value in re.findall(r'([a-zA-Z0-9_-]+)="([^"]*)"', match.group(1))
    }


def _add_agent_source_marker(svg_text: str, *, page_type: str, slide_index: int, source_sha1: str) -> str:
    marker = (
        f'<!-- agent-source page_type="{page_type}" slide_index="{slide_index}" '
        f'source_sha1="{source_sha1}" policy="source_copy_minimal_edit" -->'
    )
    if "agent-source " in svg_text:
        return svg_text
    return re.sub(r"(<svg\b[^>]*>)", "\\1\n" + marker, svg_text, count=1)


def _rebase_agent_template_svg(svg_text: str) -> str:
    return svg_text.replace("../assets/", "assets/").replace("..\\assets\\", "assets\\")


def _archive_agent_template_file(agent_dir: Path, path: Path, page_type: str) -> str | None:
    if not path.exists():
        return None
    archive_dir = agent_dir / "archive" / str(int(time.time()))
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived = archive_dir / f"{page_type}_{path.name}"
    counter = 1
    while archived.exists():
        archived = archive_dir / f"{page_type}_{counter}_{path.name}"
        counter += 1
    shutil.move(str(path), str(archived))
    try:
        return archived.relative_to(agent_dir.parent).as_posix()
    except ValueError:
        return archived.as_posix()


def _ensure_agent_template_seeded(context: dict[str, Any], agent_dir: Path) -> None:
    """Ensure Agent preview files exist without overwriting user-edited SVGs.

    The first Agent run seeds missing files from the selected source slides so the
    editor starts with the real imported layout. After that, existing files are
    treated as the source of truth and are never rewritten here; this prevents
    preview/confirm from snapping edited templates back to the original PPTX shape.
    """
    agent_dir.mkdir(parents=True, exist_ok=True)
    _sync_agent_template_assets(context, agent_dir)
    pages: list[dict[str, Any]] = []
    for page_type, file_name in PAGE_TYPE_FILES.items():
        source = _source_svg_for_selection(context, page_type)
        if source is None:
            continue
        try:
            source_text = source.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            slide_index = int(context["selections"].get(page_type) or 0)
        except (TypeError, ValueError):
            slide_index = 0
        if slide_index <= 0:
            continue
        source_hash = hashlib.sha1(source_text.encode("utf-8")).hexdigest()
        target = agent_dir / file_name
        if not target.exists():
            target.write_text(
                _add_agent_source_marker(
                    _rebase_agent_template_svg(source_text),
                    page_type=page_type,
                    slide_index=slide_index,
                    source_sha1=source_hash,
                ),
                encoding="utf-8",
            )
        pages.append(
            {
                "page_type": page_type,
                "slide_index": slide_index,
                "source_svg": source.relative_to(context["work_dir"].parent).as_posix(),
                "agent_template_svg": target.relative_to(context["work_dir"].parent).as_posix(),
                "source_sha1": source_hash,
                "existing": target.exists(),
                "archived_previous": None,
                "baseline_policy": "edit_source_copy_minimally",
            }
        )
    _write_json(
        agent_dir / "source_map.json",
        {
            "schema_version": 1,
            "mode": "source_derived",
            "status": "seeded",
            "created_at": time.time(),
            "baseline_rule": "Agent template SVGs are copied from selected source slides before any edits.",
            "pages": pages,
        },
    )


def _source_svg_for_selection(context: dict[str, Any], page_type: str) -> Path | None:
    slide_index = context["selections"].get(page_type)
    if not slide_index and page_type == "toc":
        return None
    if not slide_index:
        slide_index = context["selections"].get("content")
    try:
        idx = int(slide_index)
    except (TypeError, ValueError):
        return None
    for name in (f"slide_{idx:02d}.svg", f"slide_{idx:03d}.svg", f"slide-{idx:02d}.svg", f"slide-{idx:03d}.svg"):
        path = context["svg_clean_dir"] / name
        if path.exists():
            return path
    return None


def _agent_svg_source_matches_context(path: Path, page_type: str, context: dict[str, Any]) -> bool:
    source = _source_svg_for_selection(context, page_type)
    if source is None:
        return False
    try:
        svg_text = path.read_text(encoding="utf-8")
        source_hash = hashlib.sha1(source.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    except OSError:
        return False
    marker = _read_agent_source_marker(svg_text)
    if not marker:
        return False
    try:
        selected_index = int(context["selections"].get(page_type) or 0)
    except (TypeError, ValueError):
        selected_index = 0
    return (
        marker.get("page_type") == page_type
        and marker.get("slide_index") == str(selected_index)
        and marker.get("source_sha1") == source_hash
    )


def _sync_agent_template_assets(context: dict[str, Any], agent_dir: Path) -> None:
    src_assets = context["work_dir"] / "assets"
    if not src_assets.is_dir():
        return
    dest_assets = agent_dir / "assets"
    if dest_assets.exists():
        return
    shutil.copytree(src_assets, dest_assets)


def _agent_design_spec(agent_dir: Path, fallback: str) -> str:
    path = agent_dir / "design_spec.md"
    if not path.exists():
        return fallback
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return fallback
    return text or fallback


def _agent_template_preview(context: dict[str, Any]) -> dict[str, Any] | None:
    agent_dir = _agent_template_dir(str(context["review"].get("import_id") or ""))
    _ensure_agent_template_seeded(context, agent_dir)
    files = _agent_template_files(agent_dir, context)
    if not files:
        return None
    _sync_agent_template_assets(context, agent_dir)

    def preview_file(page_type: str) -> str:
        path = files.get(page_type)
        if path is None:
            return ""
        return inline_svg_asset_refs(path, max_bytes=5_000_000)

    return {
        "template_id": context["template_id"],
        "label": context["label"],
        "cover_svg": preview_file("cover"),
        "toc_svg": preview_file("toc"),
        "chapter_svg": preview_file("chapter"),
        "content_svg": preview_file("content"),
        "ending_svg": preview_file("ending"),
        "design_spec": _agent_design_spec(agent_dir, str(context["design_spec"]))[:40000],
        "theme_colors": context["review"].get("theme_colors") or [],
    }


def _copy_agent_template_outputs(context: dict[str, Any], template_dir: Path) -> bool:
    agent_dir = _agent_template_dir(str(context["review"].get("import_id") or ""))
    _ensure_agent_template_seeded(context, agent_dir)
    files = _agent_template_files(agent_dir, context)
    if not files:
        return False
    _sync_agent_template_assets(context, agent_dir)

    # First create fallback files for any page type the Agent did not author.
    _write_templateized_svgs(context, template_dir)
    for page_type, src in files.items():
        shutil.copyfile(src, template_dir / PAGE_TYPE_FILES[page_type])

    src_assets = agent_dir / "assets"
    if src_assets.is_dir():
        dest_assets = template_dir / "assets"
        if dest_assets.exists():
            shutil.rmtree(dest_assets)
        shutil.copytree(src_assets, dest_assets)

    design_spec = _agent_design_spec(agent_dir, str(context["design_spec"]))
    (template_dir / "design_spec.md").write_text(design_spec, encoding="utf-8")
    return True


def _write_templateized_svgs(context: dict[str, Any], target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    elements_by_slide: dict[int, dict[str, dict[str, Any]]] = {}
    sidecar_slides = _review_manifest_slides(str(context["review"].get("import_id") or ""))
    slide_records = list(context["review"].get("slides", []))
    if sidecar_slides:
        slide_records = list(sidecar_slides.values())
    for slide in slide_records:
        try:
            slide_index = int(slide.get("index") or 0)
        except (TypeError, ValueError):
            continue
        elements_by_slide[slide_index] = {
            str(element.get("element_id")): element
            for element in slide.get("elements", [])
            if element.get("element_id")
        }

    for page_type in PAGE_TYPES:
        slide_index = context["selections"].get(page_type)
        if not slide_index and page_type == "toc":
            continue
        if not slide_index:
            slide_index = context["selections"].get("content")
        slide_index = int(slide_index)
        src_svg = context["svg_clean_dir"] / f"slide_{int(slide_index):02d}.svg"
        if not src_svg.exists():
            continue
        page_actions = [
            action
            for action in context["element_actions"]
            if action.get("page_type") == page_type
        ]
        has_placeholder_actions = any(
            action.get("action") == "replace_with_placeholder"
            and str(action.get("placeholder") or "").strip()
            for action in page_actions
        )
        if has_placeholder_actions:
            output = _templateize_svg_with_plan(
                src_svg,
                slide_index,
                page_type,
                int(context["canvas_w"]),
                int(context["canvas_h"]),
                context["asset_roles_by_file"],
                context["preserve_texts"],
                context["element_actions"],
            )
        else:
            suppressed_texts = {
                _normalize_text_candidate(str(elements_by_slide.get(slide_index, {}).get(str(action.get("element_id")), {}).get("text") or ""))
                for action in page_actions
                if action.get("action") == "remove"
            }
            suppressed_texts.discard("")
            output = _templateize_svg(
                src_svg,
                page_type,
                int(context["canvas_w"]),
                int(context["canvas_h"]),
                context["asset_roles_by_file"],
                context["preserve_texts"],
                placeholder_hints=context["placeholder_hints"].get(page_type, {}),
                suppressed_texts=suppressed_texts,
            )
        (target_dir / PAGE_TYPE_FILES[page_type]).write_text(output, encoding="utf-8")


def _robust_rmtree(path: Path, retries: int = 5, delay: float = 0.1) -> None:
    """Remove a directory tree, retrying on Windows transient locks.

    Windows can briefly fail ``rmtree`` with ``WinError 145`` ("directory
    not empty") when a child file handle is still being released, or with
    ``WinError 32`` ("file is being used by another process"). We retry a
    few times with a short backoff and clear read-only bits via
    ``os.chmod`` before the second attempt.
    """
    if not path.exists():
        return

    def _on_error(func: Any, target: str, exc_info: Any) -> None:
        # Strip the read-only attribute that occasionally gets set by
        # asset extractors on Windows, then retry the operation once.
        try:
            os.chmod(target, 0o666)
        except OSError:
            pass
        try:
            func(target)
        except OSError:
            pass

    last_exc: OSError | None = None
    for attempt in range(retries):
        try:
            shutil.rmtree(path, onerror=_on_error)
            if not path.exists():
                return
        except OSError as exc:
            last_exc = exc
        time.sleep(delay * (2 ** attempt))
    if path.exists() and last_exc is not None:
        raise last_exc


def preview_import_template(import_id: str, *, require_llm_complete: bool = False) -> dict[str, Any]:
    state = get_import_task(import_id) or {}
    if state.get("collaboration_mode") == "direct":
        return _direct_template_preview(import_id)
    context = _review_template_context(import_id, require_llm_complete=require_llm_complete)
    is_agent_mode = context["state"].get("collaboration_mode") == "agent"
    if is_agent_mode:
        agent_preview = _agent_template_preview(context)
        if agent_preview is not None:
            return agent_preview
    if is_agent_mode:
        return {
            "template_id": context["template_id"],
            "label": context["label"],
            "cover_svg": "",
            "toc_svg": "",
            "chapter_svg": "",
            "content_svg": "",
            "ending_svg": "",
            "design_spec": "",
            "theme_colors": context["review"].get("theme_colors") or [],
        }

    preview_dir = context["work_dir"] / "draft_preview"
    _robust_rmtree(preview_dir)
    _write_templateized_svgs(context, preview_dir)

    src_assets = context["work_dir"] / "assets"
    if src_assets.is_dir():
        dest_assets = preview_dir / "assets"
        _robust_rmtree(dest_assets)
        shutil.copytree(src_assets, dest_assets)

    def preview_file(name: str) -> str:
        path = preview_dir / name
        if not path.exists():
            return ""
        return inline_svg_asset_refs(path)

    return {
        "template_id": context["template_id"],
        "label": context["label"],
        "cover_svg": preview_file(PAGE_TYPE_FILES["cover"]),
        "toc_svg": preview_file(PAGE_TYPE_FILES["toc"]),
        "chapter_svg": preview_file(PAGE_TYPE_FILES["chapter"]),
        "content_svg": preview_file(PAGE_TYPE_FILES["content"]),
        "ending_svg": preview_file(PAGE_TYPE_FILES["ending"]),
        "design_spec": str(context["design_spec"])[:40000],
        "theme_colors": context["review"].get("theme_colors") or [],
    }


def confirm_import_template(import_id: str) -> ImportResult:
    state = get_import_task(import_id) or {}
    if state.get("collaboration_mode") == "direct":
        return confirm_direct_import_template(import_id)
    context = _review_template_context(import_id)
    review = context["review"]
    manifest = context["manifest"]
    template_id = context["template_id"]
    label = context["label"]
    template_dir = _templates_root() / template_id

    _update_state(
        import_id,
        status="processing",
        stage="registering",
        progress=0.9,
        message="Registering reviewed template.",
        review_required=False,
    )

    if template_dir.exists():
        shutil.rmtree(template_dir)
    template_dir.mkdir(parents=True, exist_ok=True)
    used_agent_outputs = False
    if state.get("collaboration_mode") == "agent":
        used_agent_outputs = _copy_agent_template_outputs(context, template_dir)
    if not used_agent_outputs:
        _write_templateized_svgs(context, template_dir)

        src_assets = context["work_dir"] / "assets"
        if src_assets.is_dir():
            dest_assets = template_dir / "assets"
            if dest_assets.exists():
                shutil.rmtree(dest_assets)
            shutil.copytree(src_assets, dest_assets)

        (template_dir / "design_spec.md").write_text(str(context["design_spec"]), encoding="utf-8")
    manifest_with_review = {**manifest, "importReview": review}
    if used_agent_outputs:
        manifest_with_review["agentTemplateOutputs"] = True
    (template_dir / "manifest.json").write_text(
        json.dumps(manifest_with_review, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _register_user_template(template_id, label, manifest)

    state = _update_state(
        import_id,
        status="complete",
        stage="registering",
        progress=1.0,
        message="Template registered.",
        review_required=False,
        template_id=template_id,
        label=label,
        slide_count=int(review.get("slide_count") or 0),
        export_mode=review.get("export_mode") or "",
        theme_colors=review.get("theme_colors") or [],
        error=None,
    )
    return _result_from_state(state)


def _direct_page_selections() -> dict[str, int]:
    return {page_type: idx for idx, page_type in enumerate(PAGE_TYPES, start=1)}


def _ensure_direct_review(import_id: str) -> dict[str, Any]:
    review = get_import_review(import_id)
    if review is None:
        raise FileNotFoundError(import_id)
    slide_count = int(review.get("slide_count") or 0)
    if slide_count != 5:
        raise ValueError(
            f"Direct template mode requires exactly 5 slides in order: cover, toc, chapter, content, ending; got {slide_count}."
        )
    selections = _direct_page_selections()
    review["page_type_candidates"] = {page_type: [idx] for page_type, idx in selections.items()}
    draft = review.setdefault("draft", {})
    draft["page_selections"] = selections
    current_llm = review.get("llm") if isinstance(review.get("llm"), dict) else {}
    review["llm"] = {
        **current_llm,
        "enabled": bool(current_llm.get("enabled")),
        "status": current_llm.get("status") or "not_run",
        "direct": True,
    }
    _write_json(_review_path(import_id), review)
    return review


def _direct_template_preview(import_id: str) -> dict[str, Any]:
    review = _ensure_direct_review(import_id)
    work_dir = _work_dir(import_id)
    selections = _direct_page_selections()

    def preview_file(page_type: str) -> str:
        path = work_dir / "svg" / f"slide_{selections[page_type]:02d}.svg"
        if not path.exists():
            return ""
        return inline_svg_asset_refs(path)

    return {
        "template_id": str(review.get("template_id") or ""),
        "label": str(review.get("label") or review.get("template_id") or ""),
        "cover_svg": preview_file("cover"),
        "toc_svg": preview_file("toc"),
        "chapter_svg": preview_file("chapter"),
        "content_svg": preview_file("content"),
        "ending_svg": preview_file("ending"),
        "design_spec": str(review.get("draft", {}).get("design_spec") or "")[:40000],
        "theme_colors": review.get("theme_colors") or [],
    }


def _rewrite_svg_asset_refs_for_template(svg_text: str, source_svg: Path, assets_dir: Path) -> str:
    """Rewrite local image hrefs so registered Direct SVGs resolve assets/."""
    if not svg_text:
        return svg_text

    def repl(match: re.Match[str]) -> str:
        attr = match.group("attr")
        quote = match.group("quote")
        href = match.group("href")
        if not href or href.startswith(("data:", "http://", "https://", "#")):
            return match.group(0)
        if "{{" in href and "}}" in href:
            return match.group(0)
        asset_path = _resolve_preview_asset(source_svg, href)
        if asset_path is None or not asset_path.exists():
            return match.group(0)
        try:
            asset_path.relative_to(assets_dir)
        except ValueError:
            return match.group(0)
        rewritten = f"assets/{asset_path.name}"
        return f"{attr}={quote}{rewritten}{quote}"

    pattern = re.compile(r'(?P<attr>(?:xlink:)?href)=(?P<quote>["\'])(?P<href>[^"\']+)(?P=quote)')
    return pattern.sub(repl, svg_text)


def confirm_direct_import_template(import_id: str) -> ImportResult:
    """Register a 5-slide PPTX as an immutable direct template.

    The uploaded deck itself is the template: slide 1 cover, slide 2 TOC,
    slide 3 chapter, slide 4 content, slide 5 ending. No templateizer or
    placeholder rewrite runs in this mode.
    """
    try:
        review = _ensure_direct_review(import_id)
    except Exception as exc:
        state = _update_state(
            import_id,
            status="error",
            stage="direct_validation",
            progress=0.9,
            message="Direct template validation failed.",
            error=str(exc) or exc.__class__.__name__,
            review_required=False,
        )
        return _result_from_state(state)
    state = get_import_task(import_id) or {}
    manifest = _safe_read_json(_work_dir(import_id) / "manifest.json")
    template_id = str(review.get("template_id") or state.get("template_id") or _generate_template_id(import_id, import_id))
    label = str(review.get("draft", {}).get("label") or review.get("label") or state.get("label") or template_id)
    template_dir = _templates_root() / template_id
    work_dir = _work_dir(import_id)

    _update_state(
        import_id,
        status="processing",
        stage="registering",
        progress=0.9,
        message="Registering direct template.",
        review_required=False,
    )

    if template_dir.exists():
        shutil.rmtree(template_dir)
    template_dir.mkdir(parents=True, exist_ok=True)
    src_assets = work_dir / "assets"
    for page_type, slide_index in _direct_page_selections().items():
        src = work_dir / "svg" / f"slide_{slide_index:02d}.svg"
        if not src.exists():
            raise ValueError(f"Rendered SVG for slide {slide_index} is missing.")
        try:
            svg_text = src.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"Could not read rendered SVG for slide {slide_index}.") from exc
        svg_text = _rewrite_svg_asset_refs_for_template(svg_text, src, src_assets)
        (template_dir / PAGE_TYPE_FILES[page_type]).write_text(svg_text, encoding="utf-8")

    if src_assets.is_dir():
        shutil.copytree(src_assets, template_dir / "assets", dirs_exist_ok=True)

    design_spec = str(review.get("draft", {}).get("design_spec") or "").strip()
    if not design_spec:
        raise ValueError("Direct template registration requires LLM-generated design_spec.md.")
    (template_dir / "design_spec.md").write_text(design_spec, encoding="utf-8")

    manifest_with_review = {
        **manifest,
        "directTemplate": True,
        "pageSelections": _direct_page_selections(),
        "importReview": review,
    }
    (template_dir / "manifest.json").write_text(
        json.dumps(manifest_with_review, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _register_user_template(template_id, label, manifest)

    state = _update_state(
        import_id,
        status="complete",
        stage="registering",
        progress=1.0,
        message="Direct template registered.",
        review_required=False,
        template_id=template_id,
        label=label,
        slide_count=5,
        export_mode=review.get("export_mode") or "",
        theme_colors=review.get("theme_colors") or [],
        error=None,
    )
    return _result_from_state(state)


def _register_user_template(template_id: str, label: str, manifest: dict[str, Any]) -> None:
    index = _safe_read_json(_user_index_path())
    templates = index.get("templates", {})
    templates[template_id] = {
        "label": label,
        "summary": f"Imported from {manifest.get('source', {}).get('name', 'PPTX')}",
        "tone": "user-imported",
        "themeMode": "custom",
        "keywords": ["user-imported"],
        "slideCount": len(manifest.get("slides", [])),
        "theme": manifest.get("theme", {}),
        "createdAt": time.time(),
    }
    index["templates"] = templates
    _write_json(_user_index_path(), index)


def rename_user_template(template_id: str, label: str) -> bool:
    if not template_id.startswith("user_"):
        return False
    index = _safe_read_json(_user_index_path())
    templates = index.get("templates", {})
    if template_id not in templates:
        return False
    templates[template_id]["label"] = label.strip() or template_id
    index["templates"] = templates
    _write_json(_user_index_path(), index)
    return True


def remove_user_template(template_id: str) -> bool:
    template_dir = _templates_root() / template_id
    if not template_dir.is_dir():
        return False
    shutil.rmtree(template_dir, ignore_errors=True)

    index = _safe_read_json(_user_index_path())
    templates = index.get("templates", {})
    templates.pop(template_id, None)
    index["templates"] = templates
    _write_json(_user_index_path(), index)
    return True


def list_user_templates() -> list[dict[str, Any]]:
    index = _safe_read_json(_user_index_path())
    templates = index.get("templates", {})
    return [{"template_id": tid, **meta} for tid, meta in templates.items()]
