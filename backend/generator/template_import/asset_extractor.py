"""Aggregate image assets across PPTX master/layout/slide layers.

Computes SHA-1 (with pHash fallback) so renamed/duplicated images collapse
into a single :class:`AssetCandidate`, derives ``position_stable`` from
normalized bbox variance, and assigns ``recommended_role`` via the
decision tree in ``design.md`` §asset_extractor.

Public surface:

* :func:`extract` — return list of :class:`AssetCandidate`.
* :func:`extract_with_warnings` — same, but also returns the list of
  non-fatal :class:`ManifestWarning` entries the pipeline injects into
  ``manifest.warnings``.

Hard failures raise :class:`ExtractionError`. Per-asset failures (oversize
blob, decode failure) are reported as warnings and the asset is skipped.
"""

from __future__ import annotations

import base64
import hashlib
import math
import mimetypes
import posixpath
import re
import statistics
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from .types import (
    AssetCandidate,
    AssetLayer,
    AssetOccurrence,
    AssetRole,
    ExtractionError,
    ManifestWarning,
    RoleSource,
    SlideMetrics,
)


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_NS_SVG = "http://www.w3.org/2000/svg"
_NS_XLINK = "http://www.w3.org/1999/xlink"

_REL_IMAGE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
_REL_LAYOUT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
_REL_MASTER = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster"

_EMU_PER_INCH = 914_400
_DEFAULT_DPI = 96  # PPT default canvas resolution (matches design.md)

_MAX_ASSET_BYTES = 8 * 1024 * 1024
_PHASH_HAMMING_THRESHOLD = 6
_DEFAULT_CANVAS = (1280, 720)
_PREVIEW_MAX_BYTES = 900_000  # cap data-URI preview at ~900KB to keep review.json small


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def extract(
    pptx: Path,
    svg_files: list[Path],
    metrics: list[SlideMetrics],
    *,
    keep_threshold: int | None = None,
) -> list[AssetCandidate]:
    """Return aggregated :class:`AssetCandidate` list for one PPTX.

    Walks the PPTX zip across the master / layout / slide layers, hashes
    each ``ppt/media/imageNNN.<ext>`` blob, and joins occurrences with
    ``<image href>`` references found in ``svg_files``. ``keep_threshold``
    defaults to :func:`_asset_keep_threshold` of ``len(metrics)``.

    Drops the soft-failure warnings; use :func:`extract_with_warnings`
    when the pipeline needs to surface them via ``manifest.warnings``.
    """
    candidates, _warnings = extract_with_warnings(
        pptx, svg_files, metrics, keep_threshold=keep_threshold
    )
    return candidates


def extract_with_warnings(
    pptx: Path,
    svg_files: list[Path],
    metrics: list[SlideMetrics],
    *,
    keep_threshold: int | None = None,
) -> tuple[list[AssetCandidate], list[ManifestWarning]]:
    """Like :func:`extract` but also returns ``manifest.warnings`` entries."""
    warnings: list[ManifestWarning] = []

    try:
        zf = zipfile.ZipFile(pptx)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ExtractionError(
            f"cannot open PPTX zip: {exc}",
            step_id="detecting_assets",
            context={"path": str(pptx)},
        ) from exc

    with zf:
        names = zf.namelist()
        if not any(n.startswith("ppt/") for n in names):
            raise ExtractionError(
                "PPTX is missing the 'ppt/' namespace",
                step_id="detecting_assets",
                context={"path": str(pptx)},
            )

        # 1. Hash every media blob → seed candidate dict.
        candidates, sha1_by_media_path = _build_candidates_from_media(zf, names, warnings)

        # 2. Walk three PPTX layers and add master/layout occurrences.
        slide_layer_targets = _populate_pptx_occurrences(
            zf, names, candidates, sha1_by_media_path
        )

    # 3. Use rendered SVGs to add slide-layer occurrences (precise geometry).
    canvas_w, canvas_h = _resolve_canvas(metrics)
    _populate_svg_occurrences(
        svg_files, candidates, sha1_by_media_path, slide_layer_targets, warnings
    )

    # 4. Compute position_stable + recommended_role for each candidate.
    slide_count = len(svg_files) or len(metrics)
    if keep_threshold is None:
        keep_threshold = _asset_keep_threshold(slide_count)
    _decide_roles(candidates, canvas_w, canvas_h, keep_threshold)

    # 5. Emit results in deterministic order (stable across runs).
    ordered = sorted(candidates.values(), key=lambda c: c.sha1)
    return ordered, warnings


def _asset_keep_threshold(slide_count: int) -> int:
    """Threshold for promoting an asset / chrome candidate to ``stable``.

    Defined as ``max(2, ceil(slide_count * 0.4))`` and shared with
    :mod:`chrome_detector`.
    """
    return max(2, math.ceil(slide_count * 0.4))


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: hash every media blob in the zip → seed candidate dict.
# ─────────────────────────────────────────────────────────────────────────────

def _build_candidates_from_media(
    zf: zipfile.ZipFile,
    names: list[str],
    warnings: list[ManifestWarning],
) -> tuple[dict[str, AssetCandidate], dict[str, str]]:
    """Read every ``ppt/media/*`` entry, hash, build :class:`AssetCandidate`.

    Returns ``(sha1 → AssetCandidate, ppt_media_path → sha1)``.
    """
    candidates: dict[str, AssetCandidate] = {}
    sha1_by_path: dict[str, str] = {}

    for name in names:
        if not name.startswith("ppt/media/") or name.endswith("/"):
            continue
        try:
            data = zf.read(name)
        except (KeyError, RuntimeError, zipfile.BadZipFile):
            warnings.append(
                {
                    "code": "asset-decode-failed",
                    "reason": "could not read media blob from zip",
                    "context": {"file_name": PurePosixPath(name).name, "path": name},
                }
            )
            continue

        if len(data) > _MAX_ASSET_BYTES:
            warnings.append(
                {
                    "code": "asset-too-large",
                    "reason": f"asset exceeds {_MAX_ASSET_BYTES} bytes",
                    "context": {
                        "file_name": PurePosixPath(name).name,
                        "bytes": len(data),
                    },
                }
            )
            continue

        sha1 = hashlib.sha1(data).hexdigest()
        sha1_by_path[name] = sha1

        if sha1 in candidates:
            continue

        file_name = PurePosixPath(name).name
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        width_px, height_px, decoded_ok = _safe_image_size(data)
        phash = _safe_dhash(data) if decoded_ok else None
        if not decoded_ok:
            warnings.append(
                {
                    "code": "asset-decode-failed",
                    "reason": "PIL could not decode the image",
                    "context": {"file_name": file_name},
                }
            )

        candidates[sha1] = AssetCandidate(
            asset_id=sha1[:16],
            file_name=file_name,
            pages=[],
            occurrences=[],
            sha1=sha1,
            phash=phash,
            bytes=len(data),
            mime=mime,
            width_px=width_px,
            height_px=height_px,
            preview_data_uri=_make_preview_data_uri(data, mime),
        )

    return candidates, sha1_by_path


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: walk PPTX layers and synthesize master/layout occurrences.
# ─────────────────────────────────────────────────────────────────────────────

def _populate_pptx_occurrences(
    zf: zipfile.ZipFile,
    names: list[str],
    candidates: dict[str, AssetCandidate],
    sha1_by_media_path: dict[str, str],
) -> dict[str, set[str]]:
    """Add master/layout occurrences and return slide_path → media_targets used.

    The returned dict lets phase 3 cross-check whether an SVG ``<image>``
    likely came from the slide layer (vs an inherited master/layout image).
    """
    canvas_w_emu, canvas_h_emu = _read_slide_size_emu(zf)
    canvas_w_px = _emu_to_px(canvas_w_emu) or _DEFAULT_CANVAS[0]
    canvas_h_px = _emu_to_px(canvas_h_emu) or _DEFAULT_CANVAS[1]

    # Map each slide path → its layout/master so we can replicate occurrences.
    slide_paths = sorted(
        n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)
    )
    layout_for_slide: dict[str, str | None] = {}
    master_for_slide: dict[str, str | None] = {}
    slide_to_index: dict[str, int] = {}

    for idx, slide_path in enumerate(slide_paths, start=1):
        slide_to_index[slide_path] = idx
        slide_rels = _parse_relationships(zf, slide_path)
        layout_path = next(
            (r["target"] for r in slide_rels.values() if r["type"] == _REL_LAYOUT),
            None,
        )
        layout_for_slide[slide_path] = layout_path
        if layout_path is None:
            master_for_slide[slide_path] = None
            continue
        layout_rels = _parse_relationships(zf, layout_path)
        master_path = next(
            (r["target"] for r in layout_rels.values() if r["type"] == _REL_MASTER),
            None,
        )
        master_for_slide[slide_path] = master_path

    # Slide-layer media targets (used by phase 3 to scope sha1 lookup).
    slide_layer_targets: dict[str, set[str]] = {}
    for slide_path in slide_paths:
        slide_layer_targets[slide_path] = set(
            _media_targets(zf, slide_path)
        )

    # For each layout/master, capture its embedded image picture frames once.
    layout_pics_cache: dict[str, list[_PicFrame]] = {}
    master_pics_cache: dict[str, list[_PicFrame]] = {}

    def _layout_pics(path: str) -> list[_PicFrame]:
        if path not in layout_pics_cache:
            layout_pics_cache[path] = _picture_frames(
                zf, path, canvas_w_px, canvas_h_px
            )
        return layout_pics_cache[path]

    def _master_pics(path: str) -> list[_PicFrame]:
        if path not in master_pics_cache:
            master_pics_cache[path] = _picture_frames(
                zf, path, canvas_w_px, canvas_h_px
            )
        return master_pics_cache[path]

    # Append master/layout occurrences once per (slide, layer image).
    for slide_path, slide_index in slide_to_index.items():
        layout_path = layout_for_slide.get(slide_path)
        master_path = master_for_slide.get(slide_path)

        if layout_path is not None:
            for frame in _layout_pics(layout_path):
                sha1 = sha1_by_media_path.get(frame.media_path)
                if sha1 is None or sha1 not in candidates:
                    continue
                candidates[sha1].occurrences.append(
                    AssetOccurrence(
                        slide_index=slide_index,
                        x=frame.x,
                        y=frame.y,
                        width=frame.width,
                        height=frame.height,
                        layer="layout",
                    )
                )

        if master_path is not None:
            for frame in _master_pics(master_path):
                sha1 = sha1_by_media_path.get(frame.media_path)
                if sha1 is None or sha1 not in candidates:
                    continue
                candidates[sha1].occurrences.append(
                    AssetOccurrence(
                        slide_index=slide_index,
                        x=frame.x,
                        y=frame.y,
                        width=frame.width,
                        height=frame.height,
                        layer="master",
                    )
                )

    return slide_layer_targets


class _PicFrame:
    __slots__ = ("media_path", "x", "y", "width", "height")

    def __init__(
        self, media_path: str, x: float, y: float, width: float, height: float
    ) -> None:
        self.media_path = media_path
        self.x = x
        self.y = y
        self.width = width
        self.height = height


def _picture_frames(
    zf: zipfile.ZipFile, part_path: str, canvas_w_px: int, canvas_h_px: int
) -> list[_PicFrame]:
    """Extract ``p:pic`` frames from a master/layout XML with their bbox."""
    root = _load_xml(zf, part_path)
    if root is None:
        return []
    rels = _parse_relationships(zf, part_path)
    frames: list[_PicFrame] = []
    for pic in root.iter(f"{{{_NS_P}}}pic"):
        blip = pic.find(f".//{{{_NS_A}}}blip")
        if blip is None:
            continue
        rel_id = blip.attrib.get(f"{{{_NS_R}}}embed")
        rel = rels.get(rel_id or "")
        if rel is None or rel["type"] != _REL_IMAGE:
            continue
        media_path = rel["target"]

        xfrm = pic.find(f"{{{_NS_P}}}spPr/{{{_NS_A}}}xfrm")
        if xfrm is None:
            xfrm = pic.find(f".//{{{_NS_A}}}xfrm")

        x_px = 0.0
        y_px = 0.0
        w_px = float(canvas_w_px)
        h_px = float(canvas_h_px)
        if xfrm is not None:
            off = xfrm.find(f"{{{_NS_A}}}off")
            ext = xfrm.find(f"{{{_NS_A}}}ext")
            if off is not None:
                x_px = _emu_to_px(_as_int(off.attrib.get("x"), 0))
                y_px = _emu_to_px(_as_int(off.attrib.get("y"), 0))
            if ext is not None:
                w_emu = _as_int(ext.attrib.get("cx"), 0)
                h_emu = _as_int(ext.attrib.get("cy"), 0)
                if w_emu > 0:
                    w_px = float(_emu_to_px(w_emu))
                if h_emu > 0:
                    h_px = float(_emu_to_px(h_emu))
        frames.append(_PicFrame(media_path, x_px, y_px, w_px, h_px))
    return frames


def _media_targets(zf: zipfile.ZipFile, slide_path: str) -> Iterable[str]:
    rels = _parse_relationships(zf, slide_path)
    for rel in rels.values():
        if rel["type"] == _REL_IMAGE:
            yield rel["target"]


def _read_slide_size_emu(zf: zipfile.ZipFile) -> tuple[int, int]:
    root = _load_xml(zf, "ppt/presentation.xml")
    if root is None:
        return 0, 0
    sld_sz = root.find(f"{{{_NS_P}}}sldSz")
    if sld_sz is None:
        return 0, 0
    return _as_int(sld_sz.attrib.get("cx"), 0), _as_int(sld_sz.attrib.get("cy"), 0)


def _parse_relationships(
    zf: zipfile.ZipFile, part_path: str
) -> dict[str, dict[str, str]]:
    rels_path = _rels_path_for(part_path)
    root = _load_xml(zf, rels_path)
    if root is None:
        return {}
    out: dict[str, dict[str, str]] = {}
    for rel in root.findall(f"{{{_NS_REL}}}Relationship"):
        rel_id = rel.attrib.get("Id")
        target = rel.attrib.get("Target")
        rel_type = rel.attrib.get("Type")
        if not rel_id or not target or not rel_type:
            continue
        out[rel_id] = {
            "type": rel_type,
            "target": _normalize_part(target, part_path),
        }
    return out


def _rels_path_for(part_path: str) -> str:
    p = PurePosixPath(part_path)
    return str(p.parent / "_rels" / f"{p.name}.rels")


def _normalize_part(path: str, base: str) -> str:
    if path.startswith("/"):
        return path.lstrip("/")
    joined = posixpath.normpath(
        posixpath.join(str(PurePosixPath(base).parent), path)
    )
    return joined.replace("\\", "/").lstrip("/")


def _load_xml(zf: zipfile.ZipFile, part: str) -> ET.Element | None:
    try:
        with zf.open(part) as fh:
            return ET.parse(fh).getroot()
    except KeyError:
        return None
    except ET.ParseError:
        return None


def _emu_to_px(emu: int) -> int:
    if emu <= 0:
        return 0
    return int(round(emu / _EMU_PER_INCH * _DEFAULT_DPI))


def _as_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3: parse rendered SVGs and add slide-layer occurrences.
# ─────────────────────────────────────────────────────────────────────────────

_TRANSLATE_RE = re.compile(
    r"translate\s*\(\s*(-?\d+(?:\.\d+)?)\s*(?:[ ,]\s*(-?\d+(?:\.\d+)?))?\s*\)"
)
_DATA_URI_RE = re.compile(r"^data:([^;]+);base64,(.*)$", re.DOTALL)


def _populate_svg_occurrences(
    svg_files: list[Path],
    candidates: dict[str, AssetCandidate],
    sha1_by_media_path: dict[str, str],
    slide_layer_targets: dict[str, set[str]],
    warnings: list[ManifestWarning],
) -> None:
    """Parse each SVG, locate ``<image>`` elements, append slide-layer occurrences."""
    sha1_by_basename: dict[str, str] = {}
    for media_path, sha1 in sha1_by_media_path.items():
        sha1_by_basename.setdefault(PurePosixPath(media_path).name, sha1)

    for i, svg_path in enumerate(svg_files):
        slide_index = i + 1
        try:
            tree = ET.parse(svg_path)
        except (ET.ParseError, OSError) as exc:
            raise ExtractionError(
                f"malformed SVG: {svg_path.name}: {exc}",
                step_id="detecting_assets",
                context={"path": str(svg_path)},
            ) from exc

        root = tree.getroot()
        for image_elem, ancestors in _iter_images_with_ancestors(root):
            href = (
                image_elem.attrib.get("href")
                or image_elem.attrib.get(f"{{{_NS_XLINK}}}href")
                or image_elem.attrib.get("xlink:href")
                or ""
            )
            if not href:
                continue

            tx, ty = _accumulated_translate(ancestors)
            x = _to_float(image_elem.attrib.get("x")) + tx
            y = _to_float(image_elem.attrib.get("y")) + ty
            width = _to_float(image_elem.attrib.get("width"))
            height = _to_float(image_elem.attrib.get("height"))

            sha1 = _resolve_sha1_for_href(
                href, candidates, sha1_by_basename, warnings
            )
            if sha1 is None:
                continue
            cand = candidates.get(sha1)
            if cand is None:
                continue
            cand.occurrences.append(
                AssetOccurrence(
                    slide_index=slide_index,
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                    layer="slide",
                )
            )


def _iter_images_with_ancestors(
    root: ET.Element,
) -> Iterable[tuple[ET.Element, list[ET.Element]]]:
    """Yield ``(image, ancestors)`` pairs preserving DOM ancestry."""
    stack: list[ET.Element] = []

    def _walk(node: ET.Element) -> Iterable[tuple[ET.Element, list[ET.Element]]]:
        stack.append(node)
        try:
            local = node.tag.rsplit("}", 1)[-1] if "}" in node.tag else node.tag
            if local == "image":
                yield node, list(stack[:-1])
            for child in list(node):
                yield from _walk(child)
        finally:
            stack.pop()

    yield from _walk(root)


def _accumulated_translate(ancestors: list[ET.Element]) -> tuple[float, float]:
    tx = ty = 0.0
    for ancestor in ancestors:
        transform = ancestor.attrib.get("transform")
        if not transform:
            continue
        for match in _TRANSLATE_RE.finditer(transform):
            tx += float(match.group(1))
            ty += float(match.group(2) or 0.0)
    return tx, ty


def _resolve_sha1_for_href(
    href: str,
    candidates: dict[str, AssetCandidate],
    sha1_by_basename: dict[str, str],
    warnings: list[ManifestWarning],
) -> str | None:
    if href.startswith("data:"):
        match = _DATA_URI_RE.match(href.strip())
        if match is None:
            return None
        try:
            payload = "".join(match.group(2).split())
            payload += "=" * (-len(payload) % 4)
            blob = base64.b64decode(payload, validate=False)
        except (ValueError, base64.binascii.Error):
            return None
        if len(blob) > _MAX_ASSET_BYTES:
            warnings.append(
                {
                    "code": "asset-too-large",
                    "reason": f"inline data URI exceeds {_MAX_ASSET_BYTES} bytes",
                    "context": {"href_prefix": href[:64]},
                }
            )
            return None
        sha1 = hashlib.sha1(blob).hexdigest()
        if sha1 in candidates:
            return sha1
        # Synthesize candidate from inline asset (rendered into SVG by COM/LO).
        mime = match.group(1) or "application/octet-stream"
        width_px, height_px, decoded_ok = _safe_image_size(blob)
        phash = _safe_dhash(blob) if decoded_ok else None
        candidates[sha1] = AssetCandidate(
            asset_id=sha1[:16],
            file_name=f"inline_{sha1[:8]}.{_ext_for_mime(mime)}",
            pages=[],
            occurrences=[],
            sha1=sha1,
            phash=phash,
            bytes=len(blob),
            mime=mime,
            width_px=width_px,
            height_px=height_px,
            preview_data_uri=_make_preview_data_uri(blob, mime),
        )
        return sha1

    if href.startswith(("http://", "https://", "#")):
        return None

    basename = PurePosixPath(href.split("#", 1)[0].split("?", 1)[0]).name
    sha1 = sha1_by_basename.get(basename)
    if sha1 is not None:
        return sha1

    # Fuzzy fallback by case-insensitive basename match.
    lower = basename.lower()
    for name, candidate_sha1 in sha1_by_basename.items():
        if name.lower() == lower:
            return candidate_sha1
    return None


def _to_float(value: str | None, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ext_for_mime(mime: str) -> str:
    guess = mimetypes.guess_extension(mime) or ".bin"
    return guess.lstrip(".")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4: position_stable + recommended_role decision tree.
# ─────────────────────────────────────────────────────────────────────────────

def _decide_roles(
    candidates: dict[str, AssetCandidate],
    canvas_w: int,
    canvas_h: int,
    keep_threshold: int,
) -> None:
    canvas_area = max(canvas_w * canvas_h, 1)

    for cand in candidates.values():
        if cand.occurrences:
            cand.pages = sorted({occ.slide_index for occ in cand.occurrences})
        else:
            cand.pages = []

        pages_count = len(cand.pages)

        if cand.occurrences:
            xs = [occ.x / canvas_w for occ in cand.occurrences]
            ys = [occ.y / canvas_h for occ in cand.occurrences]
            std_x = statistics.pstdev(xs) if len(xs) > 1 else 0.0
            std_y = statistics.pstdev(ys) if len(ys) > 1 else 0.0
            avg_w = statistics.mean(occ.width for occ in cand.occurrences)
            avg_h = statistics.mean(occ.height for occ in cand.occurrences)
        else:
            std_x = std_y = 0.0
            avg_w = avg_h = 0.0

        # ``position_stable`` (the manifest flag) keeps its strict
        # definition: appears on at least ``keep_threshold`` slides with
        # very low coordinate variance. This is what the manifest /
        # generation stage consumes downstream.
        cand.position_stable = bool(
            pages_count >= keep_threshold
            and std_x <= 0.04
            and std_y <= 0.04
        )

        area_ratio = (avg_w * avg_h) / canvas_area

        # The recommended-role gate is intentionally softer than
        # ``position_stable`` — a logo that only appears on 2–3 slides
        # still belongs in the layout. We only mark assets ``ignore``
        # when the user almost certainly doesn't want them carried into
        # the template (no occurrences, or the same image jumping all
        # over the canvas across slides).
        positional_variance = max(std_x, std_y)
        looks_consistent = positional_variance <= 0.06

        role: AssetRole
        confidence: float
        if pages_count == 0:
            role, confidence = "ignore", 0.9
        elif pages_count == 1:
            # Single-page asset: small ones are usually decorations
            # left over from the source deck, large ones are content.
            if area_ratio >= 0.70:
                role, confidence = "background", 0.7
            elif area_ratio <= 0.12:
                role, confidence = "decoration", 0.6
            else:
                role, confidence = "content_image", 0.85
        elif not looks_consistent:
            # Multiple pages but the asset jumps around → most likely
            # an in-content illustration that recurs in different
            # positions.
            role, confidence = "content_image", 0.7
        elif area_ratio >= 0.70:
            role, confidence = "background", 1.0
        elif area_ratio <= 0.12:
            role, confidence = "logo", 1.0
        else:
            role, confidence = "decoration", 1.0

        cand.recommended_role = role
        cand.role_source = "rule"
        cand.role_confidence = confidence


# ─────────────────────────────────────────────────────────────────────────────
# pHash fallback (Pillow optional).
# ─────────────────────────────────────────────────────────────────────────────

def _dhash(image_bytes: bytes, size: int = 8) -> str:
    """Compute the difference hash (dhash) for an image as a bit string.

    Uses Pillow to load → grayscale → resize to ``(size+1, size)`` → row-wise
    pixel difference. Returns a ``size*size``-bit binary string.

    Raises any underlying Pillow exception; callers should use
    :func:`_safe_dhash` to swallow decode failures.
    """
    from PIL import Image  # local import: optional dependency

    img = (
        Image.open(BytesIO(image_bytes))
        .convert("L")
        .resize((size + 1, size), Image.Resampling.LANCZOS)
    )
    bits: list[str] = []
    for y in range(size):
        for x in range(size):
            left = img.getpixel((x, y))
            right = img.getpixel((x + 1, y))
            bits.append("1" if right > left else "0")
    return "".join(bits)


def _make_preview_data_uri(data: bytes, mime: str) -> str | None:
    """Encode bytes as a base64 ``data:`` URI for the review-pane thumbnail.

    Caps payload at :data:`_PREVIEW_MAX_BYTES` so ``review.json`` stays
    small even when a deck contains dozens of large embedded images.
    Falls back to ``None`` (caller renders an icon placeholder) when the
    blob is too big or the MIME type isn't a known image format.
    """
    if not data:
        return None
    if len(data) > _PREVIEW_MAX_BYTES:
        return None
    if not mime or not mime.startswith("image/"):
        return None
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _safe_dhash(image_bytes: bytes) -> str | None:
    try:
        return _dhash(image_bytes)
    except Exception:  # noqa: BLE001 — soft failure, intentional
        return None


def hamming_distance(a: str, b: str) -> int:
    """Bitwise hamming distance between two equal-length bit strings."""
    if len(a) != len(b):
        raise ValueError("hamming_distance requires equal-length inputs")
    return sum(1 for x, y in zip(a, b) if x != y)


def phash_match(a: str | None, b: str | None) -> bool:
    """``True`` when two pHashes are within :data:`_PHASH_HAMMING_THRESHOLD`."""
    if not a or not b or len(a) != len(b):
        return False
    return hamming_distance(a, b) <= _PHASH_HAMMING_THRESHOLD


def _safe_image_size(image_bytes: bytes) -> tuple[int | None, int | None, bool]:
    """Return ``(width, height, decoded_ok)`` using Pillow without raising."""
    try:
        from PIL import Image  # local import

        with Image.open(BytesIO(image_bytes)) as img:
            return int(img.width), int(img.height), True
    except Exception:  # noqa: BLE001 — soft failure, intentional
        return None, None, False


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_canvas(metrics: list[SlideMetrics]) -> tuple[int, int]:
    if not metrics:
        return _DEFAULT_CANVAS
    first = metrics[0]
    width = first.canvas_width or _DEFAULT_CANVAS[0]
    height = first.canvas_height or _DEFAULT_CANVAS[1]
    return int(width), int(height)


__all__ = [
    "extract",
    "extract_with_warnings",
    "hamming_distance",
    "phash_match",
]
