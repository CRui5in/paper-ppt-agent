"""PowerPoint-native slide scene extraction, rendering, and patching.

This module is intentionally separate from the existing SVG preview/editor
pipeline.  The scene model treats the PPTX / OOXML deck as the source of truth
and browser images as render output, so frontend editing does not have to infer
PowerPoint objects from SVG export artifacts.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import mimetypes
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from lxml import etree


NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

WIDE_CX = 12_192_000
WIDE_CY = 6_858_000
CANVAS_W = 1280
CANVAS_H = 720
RENDER_SCALE = 2
RENDER_W = CANVAS_W * RENDER_SCALE
RENDER_H = CANVAS_H * RENDER_SCALE
SCENE_VERSION = 3
IMAGE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


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


@dataclass(frozen=True)
class ScenePaths:
    scene_dir: Path
    deck_pptx: Path
    scene_json_dir: Path
    render_dir: Path
    edit_base_dir: Path
    media_dir: Path

    @property
    def staged_media_dir(self) -> Path:
        return self.scene_dir / "staged_media"


class SceneError(RuntimeError):
    """Raised when PPTX scene extraction or patching cannot be completed."""


def scene_paths(scene_dir: Path, *, deck_name: str = "deck.pptx") -> ScenePaths:
    scene_dir = Path(scene_dir)
    return ScenePaths(
        scene_dir=scene_dir,
        deck_pptx=scene_dir / deck_name,
        scene_json_dir=scene_dir / "slides",
        render_dir=scene_dir / "render_png",
        edit_base_dir=scene_dir / "edit_base_png",
        media_dir=scene_dir / "media",
    )


def ensure_scene_cache(
    source_pptx: Path,
    paths: ScenePaths,
    *,
    force: bool = False,
    render: bool = True,
) -> dict[str, Any]:
    """Ensure ``paths`` contains an editable deck, scene JSON, and previews."""
    source_pptx = Path(source_pptx)
    if not source_pptx.exists():
        raise FileNotFoundError(source_pptx)
    paths.scene_dir.mkdir(parents=True, exist_ok=True)
    meta_path = paths.scene_dir / "scene_meta.json"
    source_sig = f"{SCENE_VERSION}:{_deck_signature(source_pptx)}"
    existing_sig = ""
    existing_source_sig = ""
    dirty = False
    if meta_path.exists() and not force:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            existing_sig = str(meta.get("deck_signature") or "") if isinstance(meta, dict) else ""
            existing_source_sig = str(meta.get("source_signature") or "") if isinstance(meta, dict) else ""
            dirty = bool(meta.get("dirty") is True) if isinstance(meta, dict) else False
        except (OSError, json.JSONDecodeError):
            existing_sig = ""
            existing_source_sig = ""
            dirty = False

    source_changed = existing_source_sig != source_sig
    copied_source = False
    if force or not paths.deck_pptx.exists() or (source_changed and not dirty):
        shutil.copy2(source_pptx, paths.deck_pptx)
        copied_source = True

    current_sig = f"{SCENE_VERSION}:{_deck_signature(paths.deck_pptx)}"

    needs_rebuild = force or copied_source or existing_sig != current_sig or not any(paths.scene_json_dir.glob("slide_*.json"))
    if needs_rebuild:
        _write_scene_files(paths.deck_pptx, paths)
        _write_json(
            meta_path,
            {
                "version": SCENE_VERSION,
                "deck_signature": current_sig,
                "source_signature": source_sig,
                "dirty": bool(dirty and not copied_source),
                "updated_at": time.time(),
            },
        )

    if render:
        _ensure_render_outputs(paths)
    return _read_scene(paths, 1) if (paths.scene_json_dir / "slide_01.json").exists() else {}


def scene_version(paths: ScenePaths) -> int:
    try:
        return int(paths.deck_pptx.stat().st_mtime_ns)
    except OSError:
        return 0


def slide_scene_path(paths: ScenePaths, slide_index: int) -> Path:
    return paths.scene_json_dir / f"slide_{slide_index:02d}.json"


def slide_render_path(paths: ScenePaths, slide_index: int) -> Path:
    return paths.render_dir / f"slide_{slide_index:02d}.png"


def slide_edit_base_path(paths: ScenePaths, slide_index: int) -> Path:
    return paths.edit_base_dir / f"slide_{slide_index:02d}.png"


def get_slide_scene(
    source_pptx: Path,
    paths: ScenePaths,
    slide_index: int,
    *,
    render: bool = True,
) -> dict[str, Any]:
    ensure_scene_cache(source_pptx, paths, render=render)
    scene = _read_scene(paths, slide_index)
    if not scene:
        raise FileNotFoundError(slide_scene_path(paths, slide_index))
    return scene


def patch_slide_scene(
    source_pptx: Path,
    paths: ScenePaths,
    slide_index: int,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply object-level operations to the editable PPTX deck and rebuild scene."""
    ensure_scene_cache(source_pptx, paths, render=False)
    if not operations:
        return _read_scene(paths, slide_index)

    tmp_pptx = paths.deck_pptx.with_suffix(".tmp.pptx")
    try:
        _patch_deck(paths.deck_pptx, tmp_pptx, slide_index, operations, paths)
        # Validate the written deck can be parsed before replacing the current
        # editable deck. This is the cheap safety net before PowerPoint render.
        parse_pptx_scenes(tmp_pptx)
        tmp_pptx.replace(paths.deck_pptx)
        _write_scene_files(paths.deck_pptx, paths)
        _mark_scene_dirty(paths)
        _ensure_render_outputs(paths, force=True)
    finally:
        tmp_pptx.unlink(missing_ok=True)
    scene = _read_scene(paths, slide_index)
    if not scene:
        raise SceneError(f"slide {slide_index} scene missing after patch")
    return scene


def patch_deck_operations(
    source_pptx: Path,
    paths: ScenePaths,
    slide_index: int,
    operations: list[dict[str, Any]],
    *,
    mode: str = "commit",
) -> dict[str, Any]:
    """Apply a batch of editor operations.

    ``mode='preview'`` validates the patch against a temporary PPTX and returns
    the current scene without replacing the editable deck. ``mode='commit'`` is
    the normal transaction path used by the editor.
    """
    if mode == "preview":
        ensure_scene_cache(source_pptx, paths, render=False)
        if operations:
            tmp_pptx = paths.deck_pptx.with_suffix(".preview.pptx")
            try:
                _patch_deck(paths.deck_pptx, tmp_pptx, slide_index, operations, paths)
                parse_pptx_scenes(tmp_pptx)
            finally:
                tmp_pptx.unlink(missing_ok=True)
        scene = _read_scene(paths, slide_index)
        if not scene:
            raise SceneError(f"slide {slide_index} scene missing")
        return scene
    return patch_slide_scene(source_pptx, paths, slide_index, operations)


def duplicate_slide(source_pptx: Path, paths: ScenePaths, slide_index: int) -> list[dict[str, Any]]:
    """Duplicate one slide in the editable deck and rebuild the scene cache."""
    ensure_scene_cache(source_pptx, paths, render=False)
    tmp_pptx = paths.deck_pptx.with_suffix(".tmp.pptx")
    try:
        _duplicate_slide_in_deck(paths.deck_pptx, tmp_pptx, slide_index)
        parse_pptx_scenes(tmp_pptx)
        tmp_pptx.replace(paths.deck_pptx)
        _write_scene_files(paths.deck_pptx, paths)
        _mark_scene_dirty(paths)
        _ensure_render_outputs(paths, force=True)
    finally:
        tmp_pptx.unlink(missing_ok=True)
    return parse_pptx_scenes(paths.deck_pptx)


def delete_slide(source_pptx: Path, paths: ScenePaths, slide_index: int) -> list[dict[str, Any]]:
    """Delete one slide from the editable deck and rebuild the scene cache."""
    ensure_scene_cache(source_pptx, paths, render=False)
    tmp_pptx = paths.deck_pptx.with_suffix(".tmp.pptx")
    try:
        _delete_slide_in_deck(paths.deck_pptx, tmp_pptx, slide_index)
        parse_pptx_scenes(tmp_pptx)
        tmp_pptx.replace(paths.deck_pptx)
        _write_scene_files(paths.deck_pptx, paths)
        _mark_scene_dirty(paths)
        _ensure_render_outputs(paths, force=True)
    finally:
        tmp_pptx.unlink(missing_ok=True)
    return parse_pptx_scenes(paths.deck_pptx)


def reorder_slides(source_pptx: Path, paths: ScenePaths, order: list[int]) -> list[dict[str, Any]]:
    """Reorder slide references in the editable deck and rebuild the scene cache."""
    ensure_scene_cache(source_pptx, paths, render=False)
    tmp_pptx = paths.deck_pptx.with_suffix(".tmp.pptx")
    try:
        _reorder_slides_in_deck(paths.deck_pptx, tmp_pptx, order)
        parse_pptx_scenes(tmp_pptx)
        tmp_pptx.replace(paths.deck_pptx)
        _write_scene_files(paths.deck_pptx, paths)
        _mark_scene_dirty(paths)
        _ensure_render_outputs(paths, force=True)
    finally:
        tmp_pptx.unlink(missing_ok=True)
    return parse_pptx_scenes(paths.deck_pptx)


def stage_image_asset(paths: ScenePaths, filename: str, data: bytes, mime_type: str | None = None) -> dict[str, Any]:
    """Stage an uploaded/searched image for a later OOXML image operation."""
    if not data:
        raise ValueError("image is empty")
    guessed = (mime_type or mimetypes.guess_type(filename)[0] or "").lower()
    ext = _image_ext_for_mime(guessed) or Path(filename).suffix.lower().lstrip(".") or "png"
    if ext == "jpg":
        ext = "jpeg"
    digest = hashlib.sha1(data).hexdigest()[:16]
    asset_id = f"asset_{digest}.{ext}"
    paths.staged_media_dir.mkdir(parents=True, exist_ok=True)
    target = paths.staged_media_dir / asset_id
    target.write_bytes(data)
    return {
        "asset_id": asset_id,
        "media_name": asset_id,
        "mime_type": guessed or mimetypes.guess_type(asset_id)[0] or "image/png",
        "size": len(data),
    }


def inspect_deck_scene(source_pptx: Path, paths: ScenePaths) -> dict[str, Any]:
    """Run PPTX-native structural checks and return localized repair actions."""
    ensure_scene_cache(source_pptx, paths, render=False)
    issues: list[dict[str, Any]] = []
    for scene_file in sorted(paths.scene_json_dir.glob("slide_*.json")):
        scene = json.loads(scene_file.read_text(encoding="utf-8"))
        slide_index = int(scene.get("slide_index") or 0)
        elements = [e for e in scene.get("elements") or [] if isinstance(e, dict) and e.get("visible", True)]
        if not elements:
            issues.append(_deck_issue("empty_slide", "warning", slide_index, [], None, "Slide has no editable objects.", [
                {"label": "Add text", "operation": {"type": "addText", "x": 120, "y": 120, "width": 520, "height": 96, "text": "New point", "fontSize": 30}},
                {"label": "Add image", "operation": {"type": "addImage", "x": 720, "y": 160, "width": 360, "height": 240}},
            ]))
        for element in elements:
            bbox = element.get("bbox") or {}
            element_id = str(element.get("id") or "")
            x = _float(bbox.get("x"), 0)
            y = _float(bbox.get("y"), 0)
            width = _float(bbox.get("width"), 0)
            height = _float(bbox.get("height"), 0)
            if x < 0 or y < 0 or x + width > CANVAS_W or y + height > CANVAS_H:
                issues.append(_deck_issue("object_out_of_bounds", "error", slide_index, [element_id], bbox, "Object extends outside the slide.", [
                    {"label": "Fit to slide", "operation": {"type": "resize", "id": element_id, "x": max(24, min(x, CANVAS_W - width - 24)), "y": max(24, min(y, CANVAS_H - height - 24)), "width": min(width, CANVAS_W - 48), "height": min(height, CANVAS_H - 48)}},
                ]))
            if element.get("type") == "text":
                text = str(element.get("text") or "")
                font_size = _float(((element.get("runs") or [{}])[0] or {}).get("fontSize"), 24)
                capacity = max(1, int((width / max(8, font_size * 0.58)) * max(1, height / max(10, font_size * 1.25))))
                if len(text) > capacity:
                    issues.append(_deck_issue("text_overflow", "warning", slide_index, [element_id], bbox, "Text may overflow its box.", [
                        {"label": "Grow text box", "operation": {"type": "resize", "id": element_id, "x": x, "y": y, "width": min(CANVAS_W - x - 24, max(width, width * 1.25)), "height": min(CANVAS_H - y - 24, max(height + 24, height * 1.2))}},
                        {"label": "Reduce font", "operation": {"type": "updateTextStyle", "id": element_id, "fontSize": max(10, font_size - 2)}},
                    ]))
            if element.get("type") == "image" and not element.get("media_name"):
                issues.append(_deck_issue("missing_image_media", "error", slide_index, [element_id], bbox, "Image relationship is missing media.", [
                    {"label": "Replace image", "action": "replaceImage", "element_id": element_id},
                    {"label": "Delete", "operation": {"type": "delete", "id": element_id}},
                ]))
            if element.get("type") == "table":
                cells = element.get("cells") or []
                if any(not str(cell or "").strip() for row in cells for cell in (row or [])):
                    issues.append(_deck_issue("empty_table_cells", "warning", slide_index, [element_id], bbox, "Table contains empty cells.", [
                        {"label": "Select table", "action": "select", "element_id": element_id},
                    ]))
        for i, first in enumerate(elements):
            first_box = first.get("bbox") or {}
            for second in elements[i + 1:]:
                second_box = second.get("bbox") or {}
                if _intersection_area(first_box, second_box) > min(_box_area(first_box), _box_area(second_box)) * 0.35:
                    first_id = str(first.get("id") or "")
                    second_id = str(second.get("id") or "")
                    issues.append(_deck_issue("possible_overlap", "warning", slide_index, [first_id, second_id], _union_box(first_box, second_box), "Objects overlap enough to affect legibility.", [
                        {"label": "Send back", "operation": {"type": "reorder", "id": second_id, "direction": "backward"}},
                        {"label": "Move apart", "operation": {"type": "move", "id": second_id, "x": min(CANVAS_W - _float(second_box.get("width"), 0) - 24, _float(second_box.get("x"), 0) + 32), "y": min(CANVAS_H - _float(second_box.get("height"), 0) - 24, _float(second_box.get("y"), 0) + 32)}},
                    ]))
                    break
    return {
        "passed": not any(issue["severity"] == "error" for issue in issues),
        "error_count": sum(1 for issue in issues if issue["severity"] == "error"),
        "warning_count": sum(1 for issue in issues if issue["severity"] == "warning"),
        "issues": issues,
    }


def add_scene_urls_to_slide(
    slide: dict[str, Any],
    *,
    scene_url: str,
    render_url: str,
    edit_base_url: str,
    version: int,
) -> dict[str, Any]:
    slide["scene_url"] = scene_url
    slide["render_url"] = f"{render_url}?v={version}" if version else render_url
    slide["edit_base_url"] = f"{edit_base_url}?v={version}" if version else edit_base_url
    slide["scene_version"] = version
    slide["edit_capabilities"] = {
        "move": True,
        "resize": True,
        "rotate": True,
        "reorder": True,
        "updateText": True,
        "updateStyle": True,
        "delete": True,
        "duplicate": True,
        "addText": True,
        "addShape": True,
        "addImage": True,
        "addTable": True,
        "replaceImage": True,
        "check": True,
    }
    return slide


def parse_pptx_scenes(pptx_path: Path) -> list[dict[str, Any]]:
    with zipfile.ZipFile(pptx_path, "r") as zf:
        slide_size = _read_slide_size(zf)
        slide_paths = _slide_xml_paths(zf)
        scenes: list[dict[str, Any]] = []
        for index, slide_path in enumerate(slide_paths, start=1):
            rels = _read_rels(zf, _slide_rels_path(slide_path))
            root = etree.fromstring(zf.read(slide_path))
            sp_tree = root.find(".//p:cSld/p:spTree", namespaces=NS)
            elements: list[dict[str, Any]] = []
            if sp_tree is not None:
                for z, child in enumerate(list(sp_tree)):
                    tag = _local(child)
                    if tag in {"nvGrpSpPr", "grpSpPr"}:
                        continue
                    parsed = _parse_shape(child, rels, slide_size, z_order=z)
                    if parsed:
                        elements.append(parsed)
            scenes.append(
                {
                    "version": SCENE_VERSION,
                    "slide_index": index,
                    "width_emu": slide_size[0],
                    "height_emu": slide_size[1],
                    "width": CANVAS_W,
                    "height": CANVAS_H,
                    "elements": elements,
                    "render": {},
                }
            )
        return scenes


def media_response_path(paths: ScenePaths, media_name: str) -> Path:
    safe = Path(media_name).name
    return paths.media_dir / safe


def decorate_scene_for_api(scene: dict[str, Any], *, media_url_prefix: str, render_url: str, edit_base_url: str, version: int) -> dict[str, Any]:
    out = copy.deepcopy(scene)
    out["scene_version"] = version
    out["render_url"] = f"{render_url}?v={version}" if version else render_url
    out["edit_base_url"] = f"{edit_base_url}?v={version}" if version else edit_base_url

    def _walk(elements: Iterable[dict[str, Any]]) -> None:
        for element in elements:
            media_name = element.get("media_name")
            if media_name:
                element["image_url"] = f"{media_url_prefix}/{media_name}?v={version}" if version else f"{media_url_prefix}/{media_name}"
            children = element.get("children")
            if isinstance(children, list):
                _walk(children)

    _walk(out.get("elements") or [])
    return out


# ── Scene extraction ────────────────────────────────────────────────────────


def _write_scene_files(pptx_path: Path, paths: ScenePaths) -> None:
    paths.scene_json_dir.mkdir(parents=True, exist_ok=True)
    paths.media_dir.mkdir(parents=True, exist_ok=True)
    for stale in paths.scene_json_dir.glob("slide_*.json"):
        stale.unlink(missing_ok=True)
    for stale in paths.media_dir.iterdir() if paths.media_dir.exists() else []:
        if stale.is_file():
            stale.unlink(missing_ok=True)

    scenes = parse_pptx_scenes(pptx_path)
    _extract_media_files(pptx_path, paths.media_dir)
    for scene in scenes:
        _write_json(slide_scene_path(paths, int(scene["slide_index"])), scene)


def _mark_scene_dirty(paths: ScenePaths) -> None:
    meta_path = paths.scene_dir / "scene_meta.json"
    meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            parsed = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                meta = parsed
        except (OSError, json.JSONDecodeError):
            meta = {}
    meta["dirty"] = True
    meta["dirty_at"] = time.time()
    if paths.deck_pptx.exists():
        meta["version"] = SCENE_VERSION
        meta["deck_signature"] = f"{SCENE_VERSION}:{_deck_signature(paths.deck_pptx)}"
        meta["updated_at"] = time.time()
    _write_json(meta_path, meta)


def _read_scene(paths: ScenePaths, slide_index: int) -> dict[str, Any]:
    path = slide_scene_path(paths, slide_index)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_slide_size(zf: zipfile.ZipFile) -> tuple[int, int]:
    try:
        root = etree.fromstring(zf.read("ppt/presentation.xml"))
        sld_sz = root.find(".//p:sldSz", namespaces=NS)
        if sld_sz is not None:
            cx = _int_attr(sld_sz, "cx", WIDE_CX)
            cy = _int_attr(sld_sz, "cy", WIDE_CY)
            if cx > 0 and cy > 0:
                return cx, cy
    except Exception:
        pass
    return WIDE_CX, WIDE_CY


def _slide_xml_paths(zf: zipfile.ZipFile) -> list[str]:
    names = [n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
    by_name = {name: name for name in names}
    ordered: list[str] = []
    if "ppt/presentation.xml" in zf.namelist() and "ppt/_rels/presentation.xml.rels" in zf.namelist():
        try:
            root = etree.fromstring(zf.read("ppt/presentation.xml"))
            rel_targets = _presentation_rel_targets(zf)
            for node in root.findall(".//p:sldIdLst/p:sldId", namespaces=NS):
                rid = node.get(f"{{{NS['r']}}}id") or ""
                target = rel_targets.get(rid, "")
                normalized = f"ppt/{target.lstrip('/')}" if not target.startswith("ppt/") else target
                normalized = normalized.replace("\\", "/")
                if normalized in by_name and normalized not in ordered:
                    ordered.append(normalized)
        except Exception:
            ordered = []
    sorted_names = sorted(names, key=lambda n: int(re.search(r"slide(\d+)\.xml", n).group(1)))  # type: ignore[union-attr]
    ordered.extend(name for name in sorted_names if name not in ordered)
    return ordered


def _slide_path_for_index(zf: zipfile.ZipFile, slide_index: int) -> str:
    slide_paths = _slide_xml_paths(zf)
    if slide_index < 1 or slide_index > len(slide_paths):
        raise FileNotFoundError(f"slide {slide_index}")
    return slide_paths[slide_index - 1]


def _slide_rels_path(slide_path: str) -> str:
    name = slide_path.rsplit("/", 1)[-1]
    return f"ppt/slides/_rels/{name}.rels"


def _read_rels(zf: zipfile.ZipFile, rel_path: str) -> dict[str, dict[str, str]]:
    if rel_path not in zf.namelist():
        return {}
    root = etree.fromstring(zf.read(rel_path))
    rels: dict[str, dict[str, str]] = {}
    for rel in root.findall("rel:Relationship", namespaces=NS):
        rid = rel.get("Id")
        if rid:
            rels[rid] = {
                "target": rel.get("Target") or "",
                "type": rel.get("Type") or "",
            }
    return rels


def _parse_shape(elem: etree._Element, rels: dict[str, dict[str, str]], slide_size: tuple[int, int], *, z_order: int) -> dict[str, Any] | None:
    tag = _local(elem)
    c_nv_pr = elem.find(".//p:cNvPr", namespaces=NS)
    if c_nv_pr is None:
        return None
    shape_id = c_nv_pr.get("id") or str(z_order + 2)
    name = c_nv_pr.get("name") or f"{tag} {shape_id}"
    bbox = _bbox_for_element(elem, slide_size)
    base = {
        "id": f"pptx:{shape_id}",
        "shape_id": shape_id,
        "name": name,
        "z": z_order,
        "bbox": bbox,
        "rotation": bbox.get("rotation", 0),
        "locked": False,
        "visible": c_nv_pr.get("hidden") != "1",
        "capabilities": ["move", "resize", "rotate", "reorder", "delete", "duplicate"],
        "ooxml": {"cNvPrId": shape_id, "tag": tag},
    }

    if tag == "grpSp":
        children: list[dict[str, Any]] = []
        for child_z, child in enumerate(list(elem)):
            child_tag = _local(child)
            if child_tag in {"nvGrpSpPr", "grpSpPr"}:
                continue
            parsed = _parse_shape(child, rels, slide_size, z_order=child_z)
            if parsed:
                children.append(parsed)
        base.update({"type": "group", "children": children, "capabilities": ["move", "reorder", "delete", "duplicate"]})
        return base

    if tag == "pic":
        rid = _blip_rid(elem)
        target = rels.get(rid or "", {}).get("target", "") if rid else ""
        media_name = Path(target).name if target else ""
        base.update(
            {
                "type": "image",
                "media_name": media_name,
                "relation_id": rid,
                "capabilities": base["capabilities"] + ["replaceImage"],
            }
        )
        return base

    if tag == "graphicFrame":
        cells = _table_cells(elem)
        base["type"] = "table" if cells else "graphic"
        base["locked"] = not bool(cells)
        base["cells"] = cells
        base["capabilities"] = ["move", "resize", "reorder"] if cells else ["move", "reorder"]
        return base

    if tag in {"sp", "cxnSp"}:
        text = _shape_text(elem)
        runs = _text_runs(elem)
        geom = _shape_geom(elem)
        style = _shape_style(elem)
        if text.strip():
            base.update(
                {
                    "type": "text",
                    "text": text,
                    "runs": runs,
                    "textBox": _text_box_metrics(elem, slide_size),
                    "style": style,
                    "geometry": geom,
                    "capabilities": base["capabilities"] + ["updateText", "updateStyle"],
                }
            )
            return base
        kind = _editable_shape_type(geom, tag)
        base.update({"type": kind, "geometry": geom, "style": style})
        if kind in {"rect", "ellipse", "line"}:
            base["capabilities"] = base["capabilities"] + ["updateStyle"]
        else:
            base["locked"] = True
            base["capabilities"] = ["move", "reorder"]
        return base

    base.update({"type": "unknown", "locked": True, "capabilities": ["move", "reorder"]})
    return base


def _bbox_for_element(elem: etree._Element, slide_size: tuple[int, int]) -> dict[str, float]:
    xfrm = _first_found(
        elem,
        "p:spPr/a:xfrm",
        "p:grpSpPr/a:xfrm",
        "p:xfrm",
        ".//a:xfrm",
    )
    off = xfrm.find("a:off", namespaces=NS) if xfrm is not None else None
    ext = xfrm.find("a:ext", namespaces=NS) if xfrm is not None else None
    x_emu = _int_attr(off, "x", 0)
    y_emu = _int_attr(off, "y", 0)
    w_emu = _int_attr(ext, "cx", 0)
    h_emu = _int_attr(ext, "cy", 0)
    rot = _int_attr(xfrm, "rot", 0) / 60000 if xfrm is not None else 0
    cx, cy = slide_size
    return {
        "x": _emu_to_px(x_emu, cx, CANVAS_W),
        "y": _emu_to_px(y_emu, cy, CANVAS_H),
        "width": _emu_to_px(w_emu, cx, CANVAS_W),
        "height": _emu_to_px(h_emu, cy, CANVAS_H),
        "x_emu": x_emu,
        "y_emu": y_emu,
        "width_emu": w_emu,
        "height_emu": h_emu,
        "rotation": rot,
    }


def _shape_text(elem: etree._Element) -> str:
    paragraphs: list[str] = []
    for paragraph in elem.findall(".//a:p", namespaces=NS):
        text = "".join(t.text or "" for t in paragraph.findall(".//a:t", namespaces=NS))
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _text_runs(elem: etree._Element) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for run in elem.findall(".//a:r", namespaces=NS):
        text_node = run.find("a:t", namespaces=NS)
        text = text_node.text if text_node is not None else ""
        if text is None:
            text = ""
        rpr = run.find("a:rPr", namespaces=NS)
        runs.append(
            {
                "text": text,
                "fontSize": (_int_attr(rpr, "sz", 1800) / 100) * 96 / 72,
                "bold": rpr is not None and rpr.get("b") in {"1", "true"},
                "italic": rpr is not None and rpr.get("i") in {"1", "true"},
                "fill": _solid_fill(rpr) or "#111827",
                "fontFamily": _font_family(rpr),
            }
        )
    return runs


def _text_box_metrics(elem: etree._Element, slide_size: tuple[int, int]) -> dict[str, float]:
    body_pr = elem.find("p:txBody/a:bodyPr", namespaces=NS)
    # PowerPoint defaults: l/r inset 0.1in, t/b inset 0.05in.
    left = _int_attr(body_pr, "lIns", 91_440)
    right = _int_attr(body_pr, "rIns", 91_440)
    top = _int_attr(body_pr, "tIns", 45_720)
    bottom = _int_attr(body_pr, "bIns", 45_720)
    cx, cy = slide_size
    return {
        "insetLeft": _emu_to_px(left, cx, CANVAS_W),
        "insetRight": _emu_to_px(right, cx, CANVAS_W),
        "insetTop": _emu_to_px(top, cy, CANVAS_H),
        "insetBottom": _emu_to_px(bottom, cy, CANVAS_H),
    }


def _font_family(rpr: etree._Element | None) -> str:
    if rpr is None:
        return "Microsoft YaHei"
    for tag in ("a:latin", "a:ea", "a:cs"):
        node = rpr.find(tag, namespaces=NS)
        if node is not None and node.get("typeface"):
            return node.get("typeface") or "Microsoft YaHei"
    return "Microsoft YaHei"


def _shape_geom(elem: etree._Element) -> str:
    prst = elem.find(".//a:prstGeom", namespaces=NS)
    if prst is not None:
        return prst.get("prst") or "rect"
    if elem.find(".//a:custGeom", namespaces=NS) is not None:
        return "custom"
    return "rect"


def _editable_shape_type(geom: str, tag: str) -> str:
    if tag == "cxnSp" or geom == "line":
        return "line"
    if geom in {"rect", "roundRect"}:
        return "rect"
    if geom == "ellipse":
        return "ellipse"
    return "shape"


def _shape_style(elem: etree._Element) -> dict[str, Any]:
    sp_pr = _first_found(elem, "p:spPr", ".//p:spPr")
    ln = sp_pr.find("a:ln", namespaces=NS) if sp_pr is not None else None
    return {
        "fill": _solid_fill(sp_pr) or "transparent",
        "stroke": _solid_fill(ln) or "transparent",
        "strokeWidth": _emu_to_pt(_int_attr(ln, "w", 0)) if ln is not None else 0,
    }


def _solid_fill(elem: etree._Element | None) -> str | None:
    if elem is None:
        return None
    srgb = elem.find(".//a:solidFill/a:srgbClr", namespaces=NS)
    if srgb is not None and srgb.get("val"):
        return f"#{srgb.get('val')}"
    scheme = elem.find(".//a:solidFill/a:schemeClr", namespaces=NS)
    if scheme is not None and scheme.get("val"):
        return f"scheme:{scheme.get('val')}"
    if elem.find(".//a:noFill", namespaces=NS) is not None:
        return "transparent"
    return None


def _table_cells(elem: etree._Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in elem.findall(".//a:tbl/a:tr", namespaces=NS):
        cells: list[str] = []
        for cell in row.findall("a:tc", namespaces=NS):
            cells.append("\n".join(t.text or "" for t in cell.findall(".//a:t", namespaces=NS)))
        if cells:
            rows.append(cells)
    return rows


def _blip_rid(elem: etree._Element) -> str | None:
    blip = elem.find(".//a:blip", namespaces=NS)
    if blip is None:
        return None
    return blip.get(f"{{{NS['r']}}}embed") or blip.get(f"{{{NS['r']}}}link")


def _first_found(elem: etree._Element, *paths: str) -> etree._Element | None:
    for path in paths:
        found = elem.find(path, namespaces=NS)
        if found is not None:
            return found
    return None


def _extract_media_files(pptx_path: Path, media_dir: Path) -> None:
    with zipfile.ZipFile(pptx_path, "r") as zf:
        for name in zf.namelist():
            if not name.startswith("ppt/media/") or name.endswith("/"):
                continue
            target = media_dir / Path(name).name
            target.write_bytes(zf.read(name))


# ── Rendering ───────────────────────────────────────────────────────────────


def _ensure_render_outputs(paths: ScenePaths, *, force: bool = False) -> None:
    paths.render_dir.mkdir(parents=True, exist_ok=True)
    render_stale = _render_outputs_stale(paths.render_dir)
    if force or render_stale or not any(paths.render_dir.glob("slide_*.png")):
        try:
            render_pptx_to_png(paths.deck_pptx, paths.render_dir)
        except Exception:
            # Rendering is best-effort because non-Windows environments can still
            # use scene editing with the SVG fallback UI.
            pass
    edit_base_stale = _render_outputs_stale(paths.edit_base_dir)
    if force or edit_base_stale or not any(paths.edit_base_dir.glob("slide_*.png")):
        try:
            _render_edit_base(paths)
        except Exception:
            # Fall back to the full render image if hiding editable objects fails.
            paths.edit_base_dir.mkdir(parents=True, exist_ok=True)
            for png in paths.render_dir.glob("slide_*.png"):
                shutil.copy2(png, paths.edit_base_dir / png.name)


def render_pptx_to_png(pptx_path: Path, output_dir: Path, *, width: int = RENDER_W, height: int = RENDER_H) -> list[Path]:
    pptx_path = Path(pptx_path).resolve()
    output_dir = Path(output_dir).resolve()
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
        raise SceneError(f"PowerPoint PNG render failed: {stderr}")
    return sorted(output_dir.glob("slide_*.png"))


def _render_outputs_stale(output_dir: Path) -> bool:
    first = next(iter(sorted(output_dir.glob("slide_*.png"))), None)
    if first is None:
        return False
    size = _png_size(first)
    if size is None:
        return True
    width, height = size
    return width < RENDER_W or height < RENDER_H


def _png_size(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()[:24]
    except OSError:
        return None
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _render_edit_base(paths: ScenePaths) -> None:
    temp_dir = paths.scene_dir / f".edit_base_{hashlib.sha1(str(time.time()).encode()).hexdigest()[:8]}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_pptx = temp_dir / "edit_base.pptx"
    try:
        scenes = [_read_scene(paths, i) for i in range(1, 1000)]
        scenes = [scene for scene in scenes if scene]
        hidden: dict[int, set[str]] = {}
        for scene in scenes:
            ids: set[str] = set()
            _collect_editable_shape_ids(scene.get("elements") or [], ids)
            hidden[int(scene["slide_index"])] = ids
        _write_hidden_deck(paths.deck_pptx, temp_pptx, hidden)
        paths.edit_base_dir.mkdir(parents=True, exist_ok=True)
        for stale in paths.edit_base_dir.glob("slide_*.png"):
            stale.unlink(missing_ok=True)
        render_pptx_to_png(temp_pptx, paths.edit_base_dir)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _collect_editable_shape_ids(elements: list[dict[str, Any]], out: set[str]) -> None:
    for element in elements:
        # Keep grouped / complex artwork in the rendered edit base.  The first
        # v2 editor can move the group as a unit, but it does not redraw nested
        # PowerPoint effects with enough fidelity to hide the original safely.
        if element.get("type") == "group":
            continue
        if not element.get("locked") and element.get("type") in {"text", "image"}:
            sid = element.get("shape_id")
            if sid:
                out.add(str(sid))


def _write_hidden_deck(source_pptx: Path, target_pptx: Path, hidden_by_slide: dict[int, set[str]]) -> None:
    with zipfile.ZipFile(source_pptx, "r") as zin, zipfile.ZipFile(target_pptx, "w", zipfile.ZIP_DEFLATED) as zout:
        slide_order = {path: index for index, path in enumerate(_slide_xml_paths(zin), start=1)}
        for item in zin.infolist():
            data = zin.read(item.filename)
            match = re.fullmatch(r"ppt/slides/slide(\d+)\.xml", item.filename)
            if match:
                slide_index = slide_order.get(item.filename, int(match.group(1)))
                hidden = hidden_by_slide.get(slide_index) or set()
                if hidden:
                    root = etree.fromstring(data)
                    for c_nv_pr in root.findall(".//p:cNvPr", namespaces=NS):
                        if (c_nv_pr.get("id") or "") in hidden:
                            c_nv_pr.set("hidden", "1")
                    data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            zout.writestr(item, data)


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


# ── Patching ────────────────────────────────────────────────────────────────


def _patch_deck(source_pptx: Path, target_pptx: Path, slide_index: int, operations: list[dict[str, Any]], paths: ScenePaths | None = None) -> None:
    with zipfile.ZipFile(source_pptx, "r") as zin:
        slide_name = _slide_path_for_index(zin, slide_index)
        if slide_name not in zin.namelist():
            raise FileNotFoundError(slide_name)
        slide_size = _read_slide_size(zin)
        root = etree.fromstring(zin.read(slide_name))
        rel_path = _slide_rels_path(slide_name)
        rel_root = _read_or_new_relationships(zin, rel_path)
        content_root = etree.fromstring(zin.read("[Content_Types].xml")) if "[Content_Types].xml" in zin.namelist() else etree.Element(f"{{{CONTENT_TYPES_NS}}}Types")
        _ensure_content_type(content_root, "rels", "application/vnd.openxmlformats-package.relationships+xml")
        _ensure_content_type(content_root, "xml", "application/xml")
        overrides: dict[str, bytes] = {}
        for op in operations:
            media_override = _maybe_stage_image_override(op, paths, zin)
            if media_override is not None:
                media_path, media_bytes, media_type = media_override
                overrides[media_path] = media_bytes
                _ensure_content_type(content_root, Path(media_path).suffix.lower().lstrip("."), media_type)
                op = {**op, "_media_target": f"../media/{Path(media_path).name}"}
            _apply_operation(root, op, slide_size, rel_root)
        overrides[slide_name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        overrides[rel_path] = etree.tostring(rel_root, xml_declaration=True, encoding="UTF-8", standalone=True)
        overrides["[Content_Types].xml"] = etree.tostring(content_root, xml_declaration=True, encoding="UTF-8", standalone=True)
        _write_zip_with_overrides(zin, target_pptx, overrides)


def _apply_operation(root: etree._Element, op: dict[str, Any], slide_size: tuple[int, int], rel_root: etree._Element | None = None) -> None:
    op_type = str(op.get("type") or "")
    target_id = _op_target_id(op)
    if op_type in {"addText", "addShape", "addImage", "addTable"}:
        _add_shape_operation(root, op, slide_size, rel_root)
        return
    if op_type in {"align", "distribute"}:
        _layout_shapes(root, op, slide_size)
        return
    if op_type not in {
        "move",
        "resize",
        "rotate",
        "reorder",
        "updateText",
        "updateStyle",
        "updateTextStyle",
        "updateImage",
        "updateTableCell",
        "delete",
        "duplicate",
        "groupMove",
    }:
        return
    target = _find_container_by_shape_id(root, target_id)
    if target is None:
        return
    if op_type in {"move", "groupMove"}:
        _set_xfrm(target, slide_size, x=op.get("x"), y=op.get("y"))
    elif op_type == "resize":
        _set_xfrm(target, slide_size, x=op.get("x"), y=op.get("y"), width=op.get("width"), height=op.get("height"))
    elif op_type == "rotate":
        _set_xfrm(target, slide_size, rotation=op.get("rotation"))
    elif op_type == "updateText":
        _replace_text(target, str(op.get("text") or ""))
    elif op_type == "updateStyle":
        _update_style(target, op)
    elif op_type == "updateTextStyle":
        _update_text_style(target, op)
    elif op_type == "updateImage":
        _update_image(target, op, rel_root)
    elif op_type == "updateTableCell":
        _update_table_cell(target, op)
    elif op_type == "delete":
        parent = target.getparent()
        if parent is not None:
            parent.remove(target)
    elif op_type == "duplicate":
        _duplicate_shape(root, target, slide_size, op)
    elif op_type == "reorder":
        _reorder_shape(target, str(op.get("direction") or "front"))


def _op_target_id(op: dict[str, Any]) -> str:
    raw = str(op.get("id") or op.get("element_id") or op.get("shape_id") or "")
    return raw.rsplit(":", 1)[-1] if raw.startswith("pptx:") else raw


def _find_container_by_shape_id(root: etree._Element, shape_id: str) -> etree._Element | None:
    if not shape_id:
        return None
    c_nv_pr = root.find(f".//p:cNvPr[@id='{shape_id}']", namespaces=NS)
    if c_nv_pr is None:
        return None
    current = c_nv_pr
    while current is not None:
        if _local(current) in {"sp", "pic", "cxnSp", "grpSp", "graphicFrame"}:
            return current
        current = current.getparent()
    return None


def _add_shape_operation(root: etree._Element, op: dict[str, Any], slide_size: tuple[int, int], rel_root: etree._Element | None = None) -> None:
    sp_tree = root.find(".//p:cSld/p:spTree", namespaces=NS)
    if sp_tree is None:
        raise SceneError("slide shape tree is missing")
    op_type = str(op.get("type") or "")
    shape_id = _next_shape_id(root)
    x = _float(op.get("x"), 96)
    y = _float(op.get("y"), 96)
    width = _float(op.get("width"), 360)
    height = _float(op.get("height"), 96)
    if op_type == "addTable":
        sp_tree.append(_new_table(shape_id, slide_size, x, y, width, height, op))
        return
    if op_type == "addImage":
        if rel_root is not None and op.get("_media_target"):
            rid = _append_relationship(rel_root, IMAGE_REL_TYPE, str(op["_media_target"]))
            sp_tree.append(_new_picture(shape_id, slide_size, x, y, width, height, rid, op))
        else:
            sp_tree.append(
                _new_text_box(
                    shape_id,
                    slide_size,
                    x,
                    y,
                    width,
                    height,
                    str(op.get("text") or "Drop or select an image"),
                    {**op, "fill": "#eef2ff", "stroke": "#94a3b8", "color": "#475569", "fontSize": 18},
                )
            )
        return
    if op_type == "addText":
        text = str(op.get("text") or "New text")
        sp_tree.append(_new_text_box(shape_id, slide_size, x, y, width, height, text, op))
        return
    geometry = str(op.get("geometry") or op.get("shape") or "rect")
    sp_tree.append(_new_shape(shape_id, slide_size, x, y, width, height, geometry, op))


def _layout_shapes(root: etree._Element, op: dict[str, Any], slide_size: tuple[int, int]) -> None:
    ids = op.get("ids")
    if not isinstance(ids, list) or not ids:
        single = _op_target_id(op)
        ids = [single] if single else []
    targets = [_find_container_by_shape_id(root, _op_target_id({"id": item})) for item in ids]
    containers = [target for target in targets if target is not None]
    if not containers:
        return
    boxes = [_bbox_for_element(target, slide_size) for target in containers]
    op_type = str(op.get("type") or "")
    if op_type == "align":
        mode = str(op.get("mode") or op.get("align") or "left")
        bounds = _layout_bounds(boxes, op, slide_size)
        for target, box in zip(containers, boxes):
            x = box["x"]
            y = box["y"]
            if mode == "left":
                x = bounds["x"]
            elif mode == "center":
                x = bounds["x"] + (bounds["width"] - box["width"]) / 2
            elif mode == "right":
                x = bounds["x"] + bounds["width"] - box["width"]
            elif mode == "top":
                y = bounds["y"]
            elif mode == "middle":
                y = bounds["y"] + (bounds["height"] - box["height"]) / 2
            elif mode == "bottom":
                y = bounds["y"] + bounds["height"] - box["height"]
            _set_xfrm(target, slide_size, x=x, y=y)
        return
    axis = str(op.get("axis") or "horizontal")
    if len(containers) < 3:
        return
    indexed = sorted(zip(containers, boxes), key=lambda item: item[1]["x" if axis == "horizontal" else "y"])
    first = indexed[0][1]
    last = indexed[-1][1]
    if axis == "horizontal":
        span = (last["x"] + last["width"]) - first["x"]
        total = sum(box["width"] for _target, box in indexed)
        gap = (span - total) / max(1, len(indexed) - 1)
        cursor = first["x"]
        for target, box in indexed:
            _set_xfrm(target, slide_size, x=cursor, y=box["y"])
            cursor += box["width"] + gap
    else:
        span = (last["y"] + last["height"]) - first["y"]
        total = sum(box["height"] for _target, box in indexed)
        gap = (span - total) / max(1, len(indexed) - 1)
        cursor = first["y"]
        for target, box in indexed:
            _set_xfrm(target, slide_size, x=box["x"], y=cursor)
            cursor += box["height"] + gap


def _layout_bounds(boxes: list[dict[str, float]], op: dict[str, Any], slide_size: tuple[int, int]) -> dict[str, float]:
    if op.get("scope") == "slide":
        return {"x": 0, "y": 0, "width": CANVAS_W, "height": CANVAS_H}
    x = min(box["x"] for box in boxes)
    y = min(box["y"] for box in boxes)
    right = max(box["x"] + box["width"] for box in boxes)
    bottom = max(box["y"] + box["height"] for box in boxes)
    return {"x": x, "y": y, "width": right - x, "height": bottom - y}


def _set_xfrm(
    container: etree._Element,
    slide_size: tuple[int, int],
    *,
    x: Any = None,
    y: Any = None,
    width: Any = None,
    height: Any = None,
    rotation: Any = None,
) -> None:
    xfrm = _ensure_xfrm(container)
    off = xfrm.find("a:off", namespaces=NS)
    ext = xfrm.find("a:ext", namespaces=NS)
    if off is None:
        off = etree.SubElement(xfrm, f"{{{NS['a']}}}off")
        off.set("x", "0")
        off.set("y", "0")
    if ext is None:
        ext = etree.SubElement(xfrm, f"{{{NS['a']}}}ext")
        ext.set("cx", "0")
        ext.set("cy", "0")
    cx, cy = slide_size
    if x is not None:
        off.set("x", str(_px_to_emu(_float(x, 0), cx, CANVAS_W)))
    if y is not None:
        off.set("y", str(_px_to_emu(_float(y, 0), cy, CANVAS_H)))
    if width is not None:
        ext.set("cx", str(max(1, _px_to_emu(_float(width, 1), cx, CANVAS_W))))
    if height is not None:
        ext.set("cy", str(max(1, _px_to_emu(_float(height, 1), cy, CANVAS_H))))
    if rotation is not None:
        rot = int(round(_float(rotation, 0) * 60000))
        if rot:
            xfrm.set("rot", str(rot))
        else:
            xfrm.attrib.pop("rot", None)


def _ensure_xfrm(container: etree._Element) -> etree._Element:
    tag = _local(container)
    if tag == "graphicFrame":
        xfrm = container.find("p:xfrm", namespaces=NS)
        if xfrm is None:
            xfrm = etree.SubElement(container, f"{{{NS['p']}}}xfrm")
        return xfrm
    if tag == "grpSp":
        parent = container.find("p:grpSpPr", namespaces=NS)
        if parent is None:
            parent = etree.SubElement(container, f"{{{NS['p']}}}grpSpPr")
    else:
        parent = container.find("p:spPr", namespaces=NS)
        if parent is None:
            parent = etree.SubElement(container, f"{{{NS['p']}}}spPr")
    xfrm = parent.find("a:xfrm", namespaces=NS)
    if xfrm is None:
        xfrm = etree.SubElement(parent, f"{{{NS['a']}}}xfrm")
    return xfrm


def _replace_text(container: etree._Element, text: str) -> None:
    if _local(container) != "sp":
        return
    tx_body = container.find("p:txBody", namespaces=NS)
    if tx_body is None:
        tx_body = etree.SubElement(container, f"{{{NS['p']}}}txBody")
        etree.SubElement(tx_body, f"{{{NS['a']}}}bodyPr")
        etree.SubElement(tx_body, f"{{{NS['a']}}}lstStyle")
    first_rpr = copy.deepcopy(tx_body.find(".//a:rPr", namespaces=NS))
    for child in list(tx_body.findall("a:p", namespaces=NS)):
        tx_body.remove(child)
    for line in (text.splitlines() or [""]):
        p = etree.SubElement(tx_body, f"{{{NS['a']}}}p")
        r = etree.SubElement(p, f"{{{NS['a']}}}r")
        if first_rpr is not None:
            r.append(copy.deepcopy(first_rpr))
        else:
            etree.SubElement(r, f"{{{NS['a']}}}rPr", lang="zh-CN", sz="2400", dirty="0")
        t = etree.SubElement(r, f"{{{NS['a']}}}t")
        t.text = line


def _update_style(container: etree._Element, op: dict[str, Any]) -> None:
    if _local(container) not in {"sp", "cxnSp"}:
        return
    sp_pr = container.find("p:spPr", namespaces=NS)
    if sp_pr is None:
        sp_pr = etree.SubElement(container, f"{{{NS['p']}}}spPr")
    if "fill" in op:
        _set_solid_fill(sp_pr, str(op.get("fill") or "transparent"))
    if "stroke" in op or "strokeWidth" in op:
        ln = sp_pr.find("a:ln", namespaces=NS)
        if ln is None:
            ln = etree.SubElement(sp_pr, f"{{{NS['a']}}}ln")
        if "strokeWidth" in op:
            ln.set("w", str(int(round(_float(op.get("strokeWidth"), 0) * 12700))))
        if "stroke" in op:
            _set_solid_fill(ln, str(op.get("stroke") or "transparent"))


def _update_text_style(container: etree._Element, op: dict[str, Any]) -> None:
    if _local(container) != "sp":
        return
    tx_body = container.find("p:txBody", namespaces=NS)
    if tx_body is None:
        return
    for paragraph in tx_body.findall("a:p", namespaces=NS):
        align = op.get("align")
        if align in {"left", "center", "right", "justify"}:
            p_pr = paragraph.find("a:pPr", namespaces=NS)
            if p_pr is None:
                p_pr = etree.Element(f"{{{NS['a']}}}pPr")
                paragraph.insert(0, p_pr)
            p_pr.set("algn", {"left": "l", "center": "ctr", "right": "r", "justify": "just"}[str(align)])
        for run in paragraph.findall("a:r", namespaces=NS):
            rpr = run.find("a:rPr", namespaces=NS)
            if rpr is None:
                rpr = etree.Element(f"{{{NS['a']}}}rPr")
                run.insert(0, rpr)
            if "fontSize" in op:
                rpr.set("sz", str(max(100, int(round(_float(op.get("fontSize"), 24) * 100 * 72 / 96)))))
            if "bold" in op:
                rpr.set("b", "1" if bool(op.get("bold")) else "0")
            if "italic" in op:
                rpr.set("i", "1" if bool(op.get("italic")) else "0")
            if "fill" in op or "color" in op:
                _set_solid_fill(rpr, str(op.get("fill") or op.get("color") or "#111827"))
            font_family = op.get("fontFamily")
            if isinstance(font_family, str) and font_family.strip():
                _set_text_font_family(rpr, font_family.strip())


def _update_table_cell(container: etree._Element, op: dict[str, Any]) -> None:
    if _local(container) != "graphicFrame":
        return
    row_index = int(_float(op.get("row"), 0))
    col_index = int(_float(op.get("col"), 0))
    rows = container.findall(".//a:tbl/a:tr", namespaces=NS)
    if row_index < 0 or row_index >= len(rows):
        return
    cells = rows[row_index].findall("a:tc", namespaces=NS)
    if col_index < 0 or col_index >= len(cells):
        return
    _set_cell_text(cells[col_index], str(op.get("text") or ""))


def _set_text_font_family(rpr: etree._Element, font_family: str) -> None:
    for tag in ("latin", "ea", "cs"):
        node = rpr.find(f"a:{tag}", namespaces=NS)
        if node is None:
            node = etree.SubElement(rpr, f"{{{NS['a']}}}{tag}")
        node.set("typeface", font_family)


def _set_solid_fill(parent: etree._Element, color: str) -> None:
    for child in list(parent):
        if _local(child) in {"solidFill", "noFill", "gradFill", "pattFill"}:
            parent.remove(child)
    if color == "transparent" or not color:
        etree.SubElement(parent, f"{{{NS['a']}}}noFill")
        return
    clean = color.lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", clean):
        clean = "000000"
    solid = etree.SubElement(parent, f"{{{NS['a']}}}solidFill")
    etree.SubElement(solid, f"{{{NS['a']}}}srgbClr", val=clean.upper())


def _new_text_box(
    shape_id: int,
    slide_size: tuple[int, int],
    x: float,
    y: float,
    width: float,
    height: float,
    text: str,
    op: dict[str, Any],
) -> etree._Element:
    sp = _new_shape(shape_id, slide_size, x, y, width, height, "rect", {**op, "fill": "transparent", "stroke": "transparent"})
    nv_sp_pr = sp.find("p:nvSpPr", namespaces=NS)
    c_nv_sp_pr = nv_sp_pr.find("p:cNvSpPr", namespaces=NS) if nv_sp_pr is not None else None
    if c_nv_sp_pr is not None:
        c_nv_sp_pr.set("txBox", "1")
    tx_body = etree.SubElement(sp, f"{{{NS['p']}}}txBody")
    etree.SubElement(tx_body, f"{{{NS['a']}}}bodyPr")
    etree.SubElement(tx_body, f"{{{NS['a']}}}lstStyle")
    for line in (text.splitlines() or [""]):
        paragraph = etree.SubElement(tx_body, f"{{{NS['a']}}}p")
        run = etree.SubElement(paragraph, f"{{{NS['a']}}}r")
        rpr = etree.SubElement(run, f"{{{NS['a']}}}rPr", lang="zh-CN", dirty="0")
        size = max(100, int(round(_float(op.get("fontSize"), 28) * 100 * 72 / 96)))
        rpr.set("sz", str(size))
        _set_solid_fill(rpr, str(op.get("color") or op.get("fill") or "#111827"))
        if isinstance(op.get("fontFamily"), str):
            _set_text_font_family(rpr, str(op["fontFamily"]))
        text_node = etree.SubElement(run, f"{{{NS['a']}}}t")
        text_node.text = line
    return sp


def _new_shape(
    shape_id: int,
    slide_size: tuple[int, int],
    x: float,
    y: float,
    width: float,
    height: float,
    geometry: str,
    op: dict[str, Any],
) -> etree._Element:
    sp = etree.Element(f"{{{NS['p']}}}sp")
    nv_sp_pr = etree.SubElement(sp, f"{{{NS['p']}}}nvSpPr")
    etree.SubElement(nv_sp_pr, f"{{{NS['p']}}}cNvPr", id=str(shape_id), name=str(op.get("name") or f"Shape {shape_id}"))
    etree.SubElement(nv_sp_pr, f"{{{NS['p']}}}cNvSpPr")
    etree.SubElement(nv_sp_pr, f"{{{NS['p']}}}nvPr")
    sp_pr = etree.SubElement(sp, f"{{{NS['p']}}}spPr")
    xfrm = etree.SubElement(sp_pr, f"{{{NS['a']}}}xfrm")
    cx, cy = slide_size
    etree.SubElement(xfrm, f"{{{NS['a']}}}off", x=str(_px_to_emu(x, cx, CANVAS_W)), y=str(_px_to_emu(y, cy, CANVAS_H)))
    etree.SubElement(
        xfrm,
        f"{{{NS['a']}}}ext",
        cx=str(max(1, _px_to_emu(width, cx, CANVAS_W))),
        cy=str(max(1, _px_to_emu(height, cy, CANVAS_H))),
    )
    geom = etree.SubElement(sp_pr, f"{{{NS['a']}}}prstGeom", prst=_safe_geometry(geometry))
    etree.SubElement(geom, f"{{{NS['a']}}}avLst")
    _set_solid_fill(sp_pr, str(op.get("fill") or "#e0f2fe"))
    ln = etree.SubElement(sp_pr, f"{{{NS['a']}}}ln", w=str(int(round(_float(op.get("strokeWidth"), 1) * 12700))))
    _set_solid_fill(ln, str(op.get("stroke") or "#2563eb"))
    return sp


def _new_picture(
    shape_id: int,
    slide_size: tuple[int, int],
    x: float,
    y: float,
    width: float,
    height: float,
    rid: str,
    op: dict[str, Any],
) -> etree._Element:
    pic = etree.Element(f"{{{NS['p']}}}pic")
    nv = etree.SubElement(pic, f"{{{NS['p']}}}nvPicPr")
    etree.SubElement(nv, f"{{{NS['p']}}}cNvPr", id=str(shape_id), name=str(op.get("name") or f"Picture {shape_id}"))
    etree.SubElement(nv, f"{{{NS['p']}}}cNvPicPr")
    etree.SubElement(nv, f"{{{NS['p']}}}nvPr")
    blip_fill = etree.SubElement(pic, f"{{{NS['p']}}}blipFill")
    blip = etree.SubElement(blip_fill, f"{{{NS['a']}}}blip")
    blip.set(f"{{{NS['r']}}}embed", rid)
    stretch = etree.SubElement(blip_fill, f"{{{NS['a']}}}stretch")
    etree.SubElement(stretch, f"{{{NS['a']}}}fillRect")
    sp_pr = etree.SubElement(pic, f"{{{NS['p']}}}spPr")
    xfrm = etree.SubElement(sp_pr, f"{{{NS['a']}}}xfrm")
    cx, cy = slide_size
    etree.SubElement(xfrm, f"{{{NS['a']}}}off", x=str(_px_to_emu(x, cx, CANVAS_W)), y=str(_px_to_emu(y, cy, CANVAS_H)))
    etree.SubElement(
        xfrm,
        f"{{{NS['a']}}}ext",
        cx=str(max(1, _px_to_emu(width, cx, CANVAS_W))),
        cy=str(max(1, _px_to_emu(height, cy, CANVAS_H))),
    )
    geom = etree.SubElement(sp_pr, f"{{{NS['a']}}}prstGeom", prst="rect")
    etree.SubElement(geom, f"{{{NS['a']}}}avLst")
    return pic


def _update_image(container: etree._Element, op: dict[str, Any], rel_root: etree._Element | None) -> None:
    if _local(container) != "pic" or rel_root is None or not op.get("_media_target"):
        return
    blip = container.find(".//a:blip", namespaces=NS)
    if blip is None:
        blip_fill = container.find("p:blipFill", namespaces=NS)
        if blip_fill is None:
            blip_fill = etree.SubElement(container, f"{{{NS['p']}}}blipFill")
        blip = etree.SubElement(blip_fill, f"{{{NS['a']}}}blip")
    rid = _append_relationship(rel_root, IMAGE_REL_TYPE, str(op["_media_target"]))
    blip.set(f"{{{NS['r']}}}embed", rid)
    if blip.get(f"{{{NS['r']}}}link"):
        del blip.attrib[f"{{{NS['r']}}}link"]


def _new_table(
    shape_id: int,
    slide_size: tuple[int, int],
    x: float,
    y: float,
    width: float,
    height: float,
    op: dict[str, Any],
) -> etree._Element:
    rows = max(1, int(_float(op.get("rows"), 3)))
    cols = max(1, int(_float(op.get("cols"), 4)))
    frame = etree.Element(f"{{{NS['p']}}}graphicFrame")
    nv = etree.SubElement(frame, f"{{{NS['p']}}}nvGraphicFramePr")
    etree.SubElement(nv, f"{{{NS['p']}}}cNvPr", id=str(shape_id), name=str(op.get("name") or f"Table {shape_id}"))
    etree.SubElement(nv, f"{{{NS['p']}}}cNvGraphicFramePr")
    etree.SubElement(nv, f"{{{NS['p']}}}nvPr")
    xfrm = etree.SubElement(frame, f"{{{NS['p']}}}xfrm")
    cx, cy = slide_size
    etree.SubElement(xfrm, f"{{{NS['a']}}}off", x=str(_px_to_emu(x, cx, CANVAS_W)), y=str(_px_to_emu(y, cy, CANVAS_H)))
    etree.SubElement(
        xfrm,
        f"{{{NS['a']}}}ext",
        cx=str(max(1, _px_to_emu(width, cx, CANVAS_W))),
        cy=str(max(1, _px_to_emu(height, cy, CANVAS_H))),
    )
    graphic = etree.SubElement(frame, f"{{{NS['a']}}}graphic")
    data = etree.SubElement(graphic, f"{{{NS['a']}}}graphicData", uri="http://schemas.openxmlformats.org/drawingml/2006/table")
    table = etree.SubElement(data, f"{{{NS['a']}}}tbl")
    tbl_pr = etree.SubElement(table, f"{{{NS['a']}}}tblPr", firstRow="1", bandRow="1")
    etree.SubElement(tbl_pr, f"{{{NS['a']}}}tableStyleId").text = "{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}"
    grid = etree.SubElement(table, f"{{{NS['a']}}}tblGrid")
    col_width = max(1, _px_to_emu(width / cols, cx, CANVAS_W))
    for _ in range(cols):
        etree.SubElement(grid, f"{{{NS['a']}}}gridCol", w=str(col_width))
    row_height = max(1, _px_to_emu(height / rows, cy, CANVAS_H))
    for row in range(rows):
        tr = etree.SubElement(table, f"{{{NS['a']}}}tr", h=str(row_height))
        for col in range(cols):
            cell = etree.SubElement(tr, f"{{{NS['a']}}}tc")
            _set_cell_text(cell, str(op.get("header") or f"Header {col + 1}") if row == 0 else "")
            etree.SubElement(cell, f"{{{NS['a']}}}tcPr")
    return frame


def _set_cell_text(cell: etree._Element, text: str) -> None:
    for child in list(cell):
        if _local(child) == "txBody":
            cell.remove(child)
    tx_body = etree.SubElement(cell, f"{{{NS['a']}}}txBody")
    etree.SubElement(tx_body, f"{{{NS['a']}}}bodyPr")
    etree.SubElement(tx_body, f"{{{NS['a']}}}lstStyle")
    paragraph = etree.SubElement(tx_body, f"{{{NS['a']}}}p")
    run = etree.SubElement(paragraph, f"{{{NS['a']}}}r")
    etree.SubElement(run, f"{{{NS['a']}}}rPr", lang="zh-CN", sz="1400", dirty="0")
    text_node = etree.SubElement(run, f"{{{NS['a']}}}t")
    text_node.text = text


def _safe_geometry(value: str) -> str:
    return value if value in {"rect", "roundRect", "ellipse", "triangle", "diamond", "parallelogram", "trapezoid", "hexagon"} else "rect"


def _duplicate_shape(root: etree._Element, target: etree._Element, slide_size: tuple[int, int], op: dict[str, Any]) -> None:
    parent = target.getparent()
    if parent is None:
        return
    clone = copy.deepcopy(target)
    next_id = _next_shape_id(root)
    for c_nv_pr in clone.findall(".//p:cNvPr", namespaces=NS):
        c_nv_pr.set("id", str(next_id))
        c_nv_pr.set("name", f"{c_nv_pr.get('name') or 'Shape'} Copy")
        next_id += 1
    bbox = _bbox_for_element(target, slide_size)
    _set_xfrm(
        clone,
        slide_size,
        x=_float(op.get("x"), bbox["x"] + 24),
        y=_float(op.get("y"), bbox["y"] + 24),
    )
    parent.insert(parent.index(target) + 1, clone)


def _next_shape_id(root: etree._Element) -> int:
    ids = []
    for c_nv_pr in root.findall(".//p:cNvPr", namespaces=NS):
        try:
            ids.append(int(c_nv_pr.get("id") or "0"))
        except ValueError:
            pass
    return (max(ids) if ids else 1) + 1


def _reorder_shape(target: etree._Element, direction: str) -> None:
    parent = target.getparent()
    if parent is None:
        return
    children = list(parent)
    index = children.index(target)
    parent.remove(target)
    if direction == "backward":
        parent.insert(max(2, index - 1), target)
    elif direction == "forward":
        parent.insert(min(len(parent), index + 1), target)
    elif direction == "back":
        parent.insert(2, target)
    else:
        parent.append(target)


def _duplicate_slide_in_deck(source_pptx: Path, target_pptx: Path, slide_index: int) -> None:
    with zipfile.ZipFile(source_pptx, "r") as zin:
        slide_name = _slide_path_for_index(zin, slide_index)
        if slide_name not in zin.namelist():
            raise FileNotFoundError(slide_name)
        slide_numbers = _slide_numbers(zin)
        next_slide = (max(slide_numbers) if slide_numbers else 0) + 1
        new_slide_name = f"ppt/slides/slide{next_slide}.xml"
        new_rels_name = f"ppt/slides/_rels/slide{next_slide}.xml.rels"
        source_rels_name = _slide_rels_path(slide_name)
        overrides: dict[str, bytes] = {
            new_slide_name: zin.read(slide_name),
        }
        if source_rels_name in zin.namelist():
            overrides[new_rels_name] = zin.read(source_rels_name)
        if "ppt/presentation.xml" in zin.namelist() and "ppt/_rels/presentation.xml.rels" in zin.namelist():
            presentation, rels = _presentation_with_added_slide(zin, next_slide)
            overrides["ppt/presentation.xml"] = presentation
            overrides["ppt/_rels/presentation.xml.rels"] = rels
        if "[Content_Types].xml" in zin.namelist():
            overrides["[Content_Types].xml"] = _content_types_with_slide(zin, next_slide)
        _write_zip_with_overrides(zin, target_pptx, overrides)


def _delete_slide_in_deck(source_pptx: Path, target_pptx: Path, slide_index: int) -> None:
    with zipfile.ZipFile(source_pptx, "r") as zin:
        slide_paths = _slide_xml_paths(zin)
        if len(slide_paths) <= 1:
            raise ValueError("At least one slide must remain.")
        slide_name = _slide_path_for_index(zin, slide_index)
        slide_rels_name = _slide_rels_path(slide_name)
        overrides: dict[str, bytes] = {}
        remove = {slide_name, slide_rels_name}
        slide_target = slide_name.removeprefix("ppt/")
        if "ppt/presentation.xml" in zin.namelist() and "ppt/_rels/presentation.xml.rels" in zin.namelist():
            presentation_root = etree.fromstring(zin.read("ppt/presentation.xml"))
            rel_root = etree.fromstring(zin.read("ppt/_rels/presentation.xml.rels"))
            sld_id_lst = presentation_root.find("p:sldIdLst", namespaces=NS)
            rel_to_slide = _presentation_rel_targets(zin)
            removed_rids: set[str] = set()
            if sld_id_lst is not None:
                for node in list(sld_id_lst.findall("p:sldId", namespaces=NS)):
                    rid = node.get(f"{{{NS['r']}}}id") or ""
                    target = rel_to_slide.get(rid, "").lstrip("/")
                    normalized = f"ppt/{target}" if not target.startswith("ppt/") else target
                    if normalized == slide_name or target == slide_target:
                        sld_id_lst.remove(node)
                        removed_rids.add(rid)
            for rel in list(rel_root.findall("rel:Relationship", namespaces=NS)):
                if (rel.get("Id") or "") in removed_rids:
                    rel_root.remove(rel)
            overrides["ppt/presentation.xml"] = etree.tostring(presentation_root, xml_declaration=True, encoding="UTF-8", standalone=True)
            overrides["ppt/_rels/presentation.xml.rels"] = etree.tostring(rel_root, xml_declaration=True, encoding="UTF-8", standalone=True)
        if "[Content_Types].xml" in zin.namelist():
            content_root = etree.fromstring(zin.read("[Content_Types].xml"))
            part_name = f"/{slide_name}"
            for node in list(content_root.findall(f"{{{CONTENT_TYPES_NS}}}Override")):
                if node.get("PartName") == part_name:
                    content_root.remove(node)
            overrides["[Content_Types].xml"] = etree.tostring(content_root, xml_declaration=True, encoding="UTF-8", standalone=True)
        _write_zip_with_overrides(zin, target_pptx, overrides, remove=remove)


def _reorder_slides_in_deck(source_pptx: Path, target_pptx: Path, order: list[int]) -> None:
    with zipfile.ZipFile(source_pptx, "r") as zin:
        if "ppt/presentation.xml" not in zin.namelist():
            raise FileNotFoundError("ppt/presentation.xml")
        slide_numbers = _slide_numbers(zin)
        if sorted(order) != sorted(slide_numbers):
            raise ValueError("Slide order must contain every slide exactly once.")
        root = etree.fromstring(zin.read("ppt/presentation.xml"))
        sld_id_lst = root.find("p:sldIdLst", namespaces=NS)
        if sld_id_lst is None:
            raise SceneError("presentation slide list is missing")
        rel_to_slide = _presentation_rel_targets(zin)
        slide_to_node: dict[int, etree._Element] = {}
        for node in list(sld_id_lst.findall("p:sldId", namespaces=NS)):
            rid = node.get(f"{{{NS['r']}}}id") or ""
            target = rel_to_slide.get(rid, "")
            match = re.search(r"slide(\d+)\.xml$", target)
            if match:
                slide_to_node[int(match.group(1))] = node
            sld_id_lst.remove(node)
        for slide_no in order:
            node = slide_to_node.get(slide_no)
            if node is not None:
                sld_id_lst.append(node)
        overrides = {
            "ppt/presentation.xml": etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True),
        }
        _write_zip_with_overrides(zin, target_pptx, overrides)


def _presentation_with_added_slide(zin: zipfile.ZipFile, slide_no: int) -> tuple[bytes, bytes]:
    root = etree.fromstring(zin.read("ppt/presentation.xml"))
    rel_root = etree.fromstring(zin.read("ppt/_rels/presentation.xml.rels"))
    sld_id_lst = root.find("p:sldIdLst", namespaces=NS)
    if sld_id_lst is None:
        sld_id_lst = etree.SubElement(root, f"{{{NS['p']}}}sldIdLst")
    existing_ids = [
        int(node.get("id") or "255")
        for node in sld_id_lst.findall("p:sldId", namespaces=NS)
        if str(node.get("id") or "").isdigit()
    ]
    existing_rids = [rel.get("Id") or "" for rel in rel_root.findall("rel:Relationship", namespaces=NS)]
    next_id = (max(existing_ids) if existing_ids else 255) + 1
    rid = _next_rel_id(existing_rids)
    etree.SubElement(sld_id_lst, f"{{{NS['p']}}}sldId", id=str(next_id), **{f"{{{NS['r']}}}id": rid})
    etree.SubElement(
        rel_root,
        f"{{{NS['rel']}}}Relationship",
        Id=rid,
        Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide",
        Target=f"slides/slide{slide_no}.xml",
    )
    return (
        etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True),
        etree.tostring(rel_root, xml_declaration=True, encoding="UTF-8", standalone=True),
    )


def _content_types_with_slide(zin: zipfile.ZipFile, slide_no: int) -> bytes:
    root = etree.fromstring(zin.read("[Content_Types].xml"))
    part_name = f"/ppt/slides/slide{slide_no}.xml"
    exists = any(node.get("PartName") == part_name for node in root)
    if not exists:
        etree.SubElement(
            root,
            "{http://schemas.openxmlformats.org/package/2006/content-types}Override",
            PartName=part_name,
            ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml",
        )
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _presentation_rel_targets(zin: zipfile.ZipFile) -> dict[str, str]:
    if "ppt/_rels/presentation.xml.rels" not in zin.namelist():
        return {}
    root = etree.fromstring(zin.read("ppt/_rels/presentation.xml.rels"))
    return {
        rel.get("Id") or "": rel.get("Target") or ""
        for rel in root.findall("rel:Relationship", namespaces=NS)
    }


def _slide_numbers(zin: zipfile.ZipFile) -> list[int]:
    numbers: list[int] = []
    for name in zin.namelist():
        match = re.fullmatch(r"ppt/slides/slide(\d+)\.xml", name)
        if match:
            numbers.append(int(match.group(1)))
    return sorted(numbers)


def _next_rel_id(existing: list[str]) -> str:
    nums = []
    for rid in existing:
        match = re.fullmatch(r"rId(\d+)", rid)
        if match:
            nums.append(int(match.group(1)))
    return f"rId{(max(nums) if nums else 0) + 1}"


def _write_zip_with_overrides(
    zin: zipfile.ZipFile,
    target_pptx: Path,
    overrides: dict[str, bytes],
    *,
    remove: set[str] | None = None,
) -> None:
    remove = remove or set()
    with zipfile.ZipFile(target_pptx, "w", zipfile.ZIP_DEFLATED) as zout:
        written: set[str] = set()
        for item in zin.infolist():
            if item.filename in remove:
                continue
            data = overrides.get(item.filename, zin.read(item.filename))
            zout.writestr(item, data)
            written.add(item.filename)
        for name, data in overrides.items():
            if name not in written:
                zout.writestr(name, data)


def _read_or_new_relationships(zin: zipfile.ZipFile, rel_path: str) -> etree._Element:
    if rel_path in zin.namelist():
        return etree.fromstring(zin.read(rel_path))
    return etree.Element(f"{{{REL_NS}}}Relationships")


def _append_relationship(rel_root: etree._Element, rel_type: str, target: str) -> str:
    existing = [rel.get("Id") or "" for rel in rel_root.findall("rel:Relationship", namespaces=NS)]
    rid = _next_rel_id(existing)
    etree.SubElement(
        rel_root,
        f"{{{REL_NS}}}Relationship",
        Id=rid,
        Type=rel_type,
        Target=target,
    )
    return rid


def _maybe_stage_image_override(
    op: dict[str, Any],
    paths: ScenePaths | None,
    zin: zipfile.ZipFile,
) -> tuple[str, bytes, str] | None:
    if str(op.get("type") or "") not in {"addImage", "updateImage"}:
        return None
    src = op.get("src")
    image: tuple[str, bytes] | None = decode_data_url_image(src) if isinstance(src, str) else None
    asset_name = str(op.get("asset_id") or op.get("media_name") or "").strip()
    if image is None and paths is not None and asset_name:
        staged = paths.staged_media_dir / Path(asset_name).name
        if staged.exists():
            image = (mimetypes.guess_type(staged.name)[0] or "image/png", staged.read_bytes())
    if image is None:
        return None
    mime_type, data = image
    ext = _image_ext_for_mime(mime_type) or Path(asset_name).suffix.lower().lstrip(".") or "png"
    if ext == "jpg":
        ext = "jpeg"
    digest = hashlib.sha1(data).hexdigest()[:12]
    existing_names = {Path(name).name for name in zin.namelist() if name.startswith("ppt/media/")}
    base_name = f"image_{digest}.{ext}"
    media_name = base_name
    counter = 1
    while media_name in existing_names:
        media_name = f"image_{digest}_{counter}.{ext}"
        counter += 1
    return f"ppt/media/{media_name}", data, mime_type or mimetypes.guess_type(media_name)[0] or "image/png"


def _ensure_content_type(root: etree._Element, ext: str, content_type: str) -> None:
    if not ext:
        return
    for node in root.findall(f"{{{CONTENT_TYPES_NS}}}Default"):
        if (node.get("Extension") or "").lower() == ext.lower():
            return
    etree.SubElement(root, f"{{{CONTENT_TYPES_NS}}}Default", Extension=ext, ContentType=content_type)


def _image_ext_for_mime(mime_type: str | None) -> str | None:
    mapping = {
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/jpg": "jpeg",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/svg+xml": "svg",
    }
    return mapping.get((mime_type or "").split(";", 1)[0].lower())


def _deck_issue(
    rule: str,
    severity: str,
    slide_index: int,
    element_ids: list[str],
    bbox: Any,
    detail: str,
    recommended_actions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": hashlib.sha1(f"{rule}:{slide_index}:{','.join(element_ids)}:{detail}".encode("utf-8")).hexdigest()[:12],
        "rule": rule,
        "severity": severity,
        "slide_index": slide_index,
        "element_ids": element_ids,
        "bbox": bbox,
        "detail": detail,
        "recommended_actions": recommended_actions,
    }


def _box_area(box: Any) -> float:
    if not isinstance(box, dict):
        return 0
    return max(0, _float(box.get("width"), 0)) * max(0, _float(box.get("height"), 0))


def _intersection_area(first: Any, second: Any) -> float:
    if not isinstance(first, dict) or not isinstance(second, dict):
        return 0
    left = max(_float(first.get("x"), 0), _float(second.get("x"), 0))
    top = max(_float(first.get("y"), 0), _float(second.get("y"), 0))
    right = min(_float(first.get("x"), 0) + _float(first.get("width"), 0), _float(second.get("x"), 0) + _float(second.get("width"), 0))
    bottom = min(_float(first.get("y"), 0) + _float(first.get("height"), 0), _float(second.get("y"), 0) + _float(second.get("height"), 0))
    return max(0, right - left) * max(0, bottom - top)


def _union_box(first: Any, second: Any) -> dict[str, float]:
    if not isinstance(first, dict):
        first = {}
    if not isinstance(second, dict):
        second = {}
    left = min(_float(first.get("x"), 0), _float(second.get("x"), 0))
    top = min(_float(first.get("y"), 0), _float(second.get("y"), 0))
    right = max(_float(first.get("x"), 0) + _float(first.get("width"), 0), _float(second.get("x"), 0) + _float(second.get("width"), 0))
    bottom = max(_float(first.get("y"), 0) + _float(first.get("height"), 0), _float(second.get("y"), 0) + _float(second.get("height"), 0))
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


# ── Utilities ───────────────────────────────────────────────────────────────


def _deck_signature(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _local(elem: etree._Element) -> str:
    return etree.QName(elem).localname


def _int_attr(elem: etree._Element | None, name: str, fallback: int) -> int:
    if elem is None:
        return fallback
    try:
        return int(float(elem.get(name) or fallback))
    except (TypeError, ValueError):
        return fallback


def _float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed == parsed else fallback


def _emu_to_px(value: int | float, slide_emu: int, canvas_px: int) -> float:
    return round((float(value) / max(1, slide_emu)) * canvas_px, 3)


def _px_to_emu(value: float, slide_emu: int, canvas_px: int) -> int:
    return int(round((float(value) / max(1, canvas_px)) * slide_emu))


def _emu_to_pt(value: int | float) -> float:
    return round(float(value) / 12700, 3)


def mime_type_for_media(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def decode_data_url_image(data_url: str) -> tuple[str, bytes] | None:
    match = re.match(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", data_url, re.DOTALL)
    if not match:
        return None
    payload = re.sub(r"\s+", "", match.group(2))
    payload += "=" * (-len(payload) % 4)
    return match.group(1), base64.b64decode(payload, validate=False)
