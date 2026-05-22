"""SVG element to DrawingML shape converters.

Each function converts a specific SVG element type to DrawingML XML.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from typing import Any

from .context import ConvertContext
from .paths import normalize_path, parse_svg_path, path_commands_to_drawingml
from .styles import build_fill_xml, build_stroke_xml
from .utils import (
    EMU_PER_PX,
    ANGLE_UNIT,
    estimate_text_width,
    font_px_to_half_pts,
    get_style_attr,
    parse_hex_color,
    parse_svg_length,
    parse_svg_ratio,
    parse_transform,
    px_to_emu,
    xml_escape,
)


def decode_base64_payload(data: str) -> bytes:
    """Decode data-URI base64 that may be whitespace-wrapped or missing padding."""
    payload = re.sub(r"\s+", "", data)
    payload += "=" * (-len(payload) % 4)
    return base64.b64decode(payload, validate=False)


def _wrap_shape(
    shape_id: int,
    name: str,
    off_x: int,
    off_y: int,
    ext_cx: int,
    ext_cy: int,
    geom_xml: str,
    fill_xml: str,
    stroke_xml: str,
    rot: int = 0,
) -> str:
    """Wrap geometry into a standard <p:sp> shape element."""
    rot_attr = f' rot="{rot}"' if rot else ""
    return (
        f'<p:sp>'
        f'<p:nvSpPr><p:cNvPr id="{shape_id}" name="{name}"/>'
        f'<p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr>'
        f'<a:xfrm{rot_attr}>'
        f'<a:off x="{off_x}" y="{off_y}"/>'
        f'<a:ext cx="{ext_cx}" cy="{ext_cy}"/>'
        f'</a:xfrm>'
        f'{geom_xml}'
        f'{fill_xml}'
        f'{stroke_xml}'
        f'</p:spPr>'
        f'</p:sp>'
    )


def convert_rect(elem: Any, ctx: ConvertContext) -> str:
    """Convert SVG <rect> to DrawingML shape."""
    x = parse_svg_length(elem.get("x", 0))
    y = parse_svg_length(elem.get("y", 0))
    w = parse_svg_length(elem.get("width", 0))
    h = parse_svg_length(elem.get("height", 0))
    if w <= 0 or h <= 0:
        return ""
    if _matrix_has_rotation_or_flip(ctx.matrix):
        d = f"M{x},{y} L{x + w},{y} L{x + w},{y + h} L{x},{y + h} Z"
        return convert_path(_clone_with_attr(elem, "d", d), ctx)

    off_x = px_to_emu(ctx.ctx_x(x))
    off_y = px_to_emu(ctx.ctx_y(y))
    ext_cx = px_to_emu(ctx.ctx_w(w))
    ext_cy = px_to_emu(ctx.ctx_h(h))

    opacity = _get_opacity(elem, ctx)
    fill = build_fill_xml(elem, ctx, opacity)
    stroke = build_stroke_xml(elem, ctx, opacity)

    geom = '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
    sid = ctx.next_id()
    return _wrap_shape(sid, f"Rect {sid}", off_x, off_y, ext_cx, ext_cy, geom, fill, stroke)


def convert_circle(elem: Any, ctx: ConvertContext) -> str:
    """Convert SVG <circle> to DrawingML ellipse."""
    cx_val = parse_svg_length(elem.get("cx", 0))
    cy_val = parse_svg_length(elem.get("cy", 0))
    r = parse_svg_length(elem.get("r", 0))
    if r <= 0:
        return ""

    x = cx_val - r
    y = cy_val - r
    off_x = px_to_emu(ctx.ctx_x(x))
    off_y = px_to_emu(ctx.ctx_y(y))
    ext_cx = px_to_emu(ctx.ctx_w(r * 2))
    ext_cy = px_to_emu(ctx.ctx_h(r * 2))

    opacity = _get_opacity(elem, ctx)
    fill = build_fill_xml(elem, ctx, opacity)
    stroke = build_stroke_xml(elem, ctx, opacity)

    geom = '<a:prstGeom prst="ellipse"><a:avLst/></a:prstGeom>'
    sid = ctx.next_id()
    return _wrap_shape(sid, f"Circle {sid}", off_x, off_y, ext_cx, ext_cy, geom, fill, stroke)


def convert_ellipse(elem: Any, ctx: ConvertContext) -> str:
    """Convert SVG <ellipse> to DrawingML ellipse."""
    cx_val = parse_svg_length(elem.get("cx", 0))
    cy_val = parse_svg_length(elem.get("cy", 0))
    rx = parse_svg_length(elem.get("rx", 0))
    ry = parse_svg_length(elem.get("ry", 0))
    if rx <= 0 or ry <= 0:
        return ""

    off_x = px_to_emu(ctx.ctx_x(cx_val - rx))
    off_y = px_to_emu(ctx.ctx_y(cy_val - ry))
    ext_cx = px_to_emu(ctx.ctx_w(rx * 2))
    ext_cy = px_to_emu(ctx.ctx_h(ry * 2))

    opacity = _get_opacity(elem, ctx)
    fill = build_fill_xml(elem, ctx, opacity)
    stroke = build_stroke_xml(elem, ctx, opacity)

    geom = '<a:prstGeom prst="ellipse"><a:avLst/></a:prstGeom>'
    sid = ctx.next_id()
    return _wrap_shape(sid, f"Ellipse {sid}", off_x, off_y, ext_cx, ext_cy, geom, fill, stroke)


def convert_line(elem: Any, ctx: ConvertContext) -> str:
    """Convert SVG <line> to DrawingML shape."""
    x1 = parse_svg_length(elem.get("x1", 0))
    y1 = parse_svg_length(elem.get("y1", 0))
    x2 = parse_svg_length(elem.get("x2", 0))
    y2 = parse_svg_length(elem.get("y2", 0))

    min_x = min(x1, x2)
    min_y = min(y1, y2)
    w = abs(x2 - x1) or 0.1
    h = abs(y2 - y1) or 0.1

    off_x = px_to_emu(ctx.ctx_x(min_x))
    off_y = px_to_emu(ctx.ctx_y(min_y))
    ext_cx = px_to_emu(ctx.ctx_w(w))
    ext_cy = px_to_emu(ctx.ctx_h(h))

    opacity = _get_opacity(elem, ctx)
    fill = "<a:noFill/>"
    stroke = build_stroke_xml(elem, ctx, opacity)

    geom = '<a:prstGeom prst="line"><a:avLst/></a:prstGeom>'
    sid = ctx.next_id()

    # Flip handling for line direction
    flip_h = ' flipH="1"' if x2 < x1 else ""
    flip_v = ' flipV="1"' if y2 < y1 else ""
    rot_attr = ""

    return (
        f'<p:sp>'
        f'<p:nvSpPr><p:cNvPr id="{sid}" name="Line {sid}"/>'
        f'<p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr>'
        f'<a:xfrm{flip_h}{flip_v}{rot_attr}>'
        f'<a:off x="{off_x}" y="{off_y}"/>'
        f'<a:ext cx="{ext_cx}" cy="{ext_cy}"/>'
        f'</a:xfrm>'
        f'{geom}'
        f'{fill}'
        f'{stroke}'
        f'</p:spPr>'
        f'</p:sp>'
    )


def convert_path(elem: Any, ctx: ConvertContext) -> str:
    """Convert SVG <path> to DrawingML custom geometry."""
    d = elem.get("d", "")
    if not d:
        return ""

    commands = normalize_path(parse_svg_path(d))
    if not commands:
        return ""

    path_xml, bx, by, bw, bh = path_commands_to_drawingml(
        commands, ctx.translate_x, ctx.translate_y, ctx.scale_x, ctx.scale_y, matrix=ctx.matrix
    )

    if bw <= 0 or bh <= 0:
        return ""

    opacity = _get_opacity(elem, ctx)
    fill = build_fill_xml(elem, ctx, opacity)
    stroke = build_stroke_xml(elem, ctx, opacity)

    geom = f'<a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/><a:rect l="0" t="0" r="0" b="0"/><a:pathLst>{path_xml}</a:pathLst></a:custGeom>'
    sid = ctx.next_id()
    return _wrap_shape(sid, f"Path {sid}", bx, by, bw, bh, geom, fill, stroke)


def convert_polygon(elem: Any, ctx: ConvertContext) -> str:
    """Convert SVG <polygon> to DrawingML custom geometry."""
    points_str = elem.get("points", "")
    if not points_str:
        return ""
    d = _points_to_path(points_str, closed=True)
    elem_copy = _clone_with_attr(elem, "d", d)
    return convert_path(elem_copy, ctx)


def convert_polyline(elem: Any, ctx: ConvertContext) -> str:
    """Convert SVG <polyline> to DrawingML custom geometry."""
    points_str = elem.get("points", "")
    if not points_str:
        return ""
    d = _points_to_path(points_str, closed=False)
    elem_copy = _clone_with_attr(elem, "d", d)
    return convert_path(elem_copy, ctx)


def convert_text(elem: Any, ctx: ConvertContext) -> str:
    """Convert SVG <text> to DrawingML text box."""
    x = parse_svg_length(elem.get("x", 0))
    y = parse_svg_length(elem.get("y", 0))
    font_size_str = ctx.get_attr(elem, "font-size", "16")
    font_size = parse_svg_length(font_size_str, 16)

    # PowerPoint does not reliably preserve positioned tspans, so split them
    # into independent text boxes when x/y/dx/dy is involved.
    segments: list[dict[str, Any]] = []
    has_positioned_tspan = False
    cur_x = x
    cur_y = y

    if elem.text and elem.text.strip():
        segments.append({
            "x": cur_x,
            "y": cur_y,
            "anchor": ctx.get_attr(elem, "text-anchor", "start"),
            "runs": [_make_run(elem.text.strip(), elem, ctx, font_size)],
        })

    ns = elem.tag.split("}")[0] + "}" if "}" in elem.tag else ""
    for child in elem:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "tspan":
            positioned = any(child.get(attr) is not None for attr in ("x", "y", "dx", "dy"))
            has_positioned_tspan = has_positioned_tspan or positioned
            if child.get("x") is not None:
                cur_x = parse_svg_length(child.get("x", cur_x), cur_x)
            elif child.get("dx") is not None:
                cur_x += parse_svg_length(child.get("dx", 0), 0)
            if child.get("y") is not None:
                cur_y = parse_svg_length(child.get("y", cur_y), cur_y)
            elif child.get("dy") is not None:
                cur_y += parse_svg_length(child.get("dy", 0), 0)

            text = (child.text or "").strip()
            if text:
                child_size = get_style_attr(child, "font-size")
                if child_size:
                    fs = parse_svg_length(child_size, font_size)
                else:
                    fs = font_size
                run = _make_run(text, child, ctx, fs)
                segments.append({
                    "x": cur_x,
                    "y": cur_y,
                    "anchor": ctx.get_attr(child, "text-anchor", ctx.get_attr(elem, "text-anchor", "start")),
                    "runs": [run],
                })
                cur_x += estimate_text_width(text, fs, run["bold"])
            if child.tail and child.tail.strip():
                tail_run = _make_run(child.tail.strip(), elem, ctx, font_size)
                segments.append({
                    "x": cur_x,
                    "y": cur_y,
                    "anchor": ctx.get_attr(elem, "text-anchor", "start"),
                    "runs": [tail_run],
                })
                cur_x += estimate_text_width(child.tail.strip(), font_size, tail_run["bold"])

    if not segments:
        return ""

    if has_positioned_tspan:
        parts = []
        for segment in segments:
            parts.append(_build_text_shape(segment["x"], segment["y"], segment["runs"], ctx, segment["anchor"]))
        return "".join(parts)

    return _build_text_shape(x, y, [run for segment in segments for run in segment["runs"]], ctx, ctx.get_attr(elem, "text-anchor", "start"))


def convert_image(elem: Any, ctx: ConvertContext) -> str:
    """Convert SVG <image> to DrawingML picture."""
    href = elem.get("{http://www.w3.org/1999/xlink}href") or elem.get("href", "")
    if not href:
        return ""

    x = parse_svg_length(elem.get("x", 0))
    y = parse_svg_length(elem.get("y", 0))
    w = parse_svg_length(elem.get("width", 0))
    h = parse_svg_length(elem.get("height", 0))
    if w <= 0 or h <= 0:
        return ""

    # Load image data
    if href.startswith("data:"):
        # Base64 embedded
        match = re.match(r"data:(image/\w+);base64,(.+)", href, re.DOTALL)
        if not match:
            return ""
        mime = match.group(1)
        try:
            img_data = decode_base64_payload(match.group(2))
        except (ValueError, base64.binascii.Error):
            import logging
            logging.getLogger(__name__).warning(
                "Invalid base64 image payload in SVG, skipping image element"
            )
            return ""
        ext = mime.split("/")[-1]
        if ext == "jpeg":
            ext = "jpg"
    else:
        # External file
        img_path = (ctx.svg_dir / href).resolve()
        if not img_path.exists():
            img_path = (ctx.svg_dir.parent / href).resolve()
        if not img_path.exists():
            import logging
            logging.getLogger(__name__).warning(
                "Image href %r not found, skipping element", href
            )
            return ""
        img_data = img_path.read_bytes()
        ext = img_path.suffix.lstrip(".").lower()
        if ext == "jpeg":
            ext = "jpg"

    # Use a slide-scoped, content-derived name so images from later slides do
    # not overwrite earlier ones in /ppt/media when the package is assembled.
    digest = hashlib.sha1(img_data).hexdigest()[:12]
    img_name = f"slide{ctx.slide_num}_image{len(ctx.media_files) + 1}_{digest}.{ext}"
    ctx.media_files[img_name] = img_data
    rel_id = ctx.next_rel_id()
    ctx.rel_entries.append({
        "id": rel_id,
        "type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
        "target": f"../media/{img_name}",
    })

    off_x = px_to_emu(ctx.ctx_x(x))
    off_y = px_to_emu(ctx.ctx_y(y))
    ext_cx = px_to_emu(ctx.ctx_w(w))
    ext_cy = px_to_emu(ctx.ctx_h(h))

    sid = ctx.next_id()
    return (
        f'<p:pic>'
        f'<p:nvPicPr><p:cNvPr id="{sid}" name="Image {sid}"/>'
        f'<p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr>'
        f'<p:nvPr/></p:nvPicPr>'
        f'<p:blipFill>'
        f'<a:blip r:embed="{rel_id}"/>'
        f'<a:stretch><a:fillRect/></a:stretch>'
        f'</p:blipFill>'
        f'<p:spPr>'
        f'<a:xfrm><a:off x="{off_x}" y="{off_y}"/>'
        f'<a:ext cx="{ext_cx}" cy="{ext_cy}"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f'</p:spPr>'
        f'</p:pic>'
    )


# --- Helpers ---


def _get_opacity(elem: Any, ctx: ConvertContext) -> float:
    """Get combined opacity from element and inherited styles."""
    op = ctx.get_attr(elem, "opacity", "1")
    return max(0.0, min(1.0, parse_svg_ratio(op, 1.0)))


def _make_run(text: str, elem: Any, ctx: ConvertContext, font_size: float) -> dict:
    """Create a text run dict from element attributes."""
    from backend.generator.svg_to_pptx.font_mapping import parse_font_family

    fill = ctx.get_attr(elem, "fill", "#000000")
    color = _resolve_text_color(fill, ctx)
    opacity = _get_opacity(elem, ctx) * parse_svg_ratio(ctx.get_attr(elem, "fill-opacity", "1"), 1.0)
    weight = ctx.get_attr(elem, "font-weight", "normal")
    style = ctx.get_attr(elem, "font-style", "normal")
    family = ctx.get_attr(elem, "font-family", "Arial")
    decoration = ctx.get_attr(elem, "text-decoration", "")

    fonts = parse_font_family(family)
    return {
        "text": text,
        "font_size": font_size,
        "color": color,
        "opacity": max(0.0, min(1.0, opacity)),
        "bold": weight in ("bold", "700", "800", "900"),
        "italic": style == "italic",
        "underline": "underline" in decoration,
        "font_family": fonts["latin"],
        "ea_font_family": fonts["ea"],
    }


def _resolve_text_color(fill: str, ctx: ConvertContext) -> str:
    """Resolve text fill to a solid fallback color.

    DrawingML text runs do not support SVG gradient fills directly. Use the
    first gradient stop as a pragmatic fallback so PPT export stays visually
    closer to the preview.
    """
    color = parse_hex_color(fill)
    if color:
        return color

    if fill.startswith("url("):
        grad_id = fill[4:-1].strip()
        if grad_id.startswith("#"):
            grad_id = grad_id[1:]
        grad = ctx.defs.get(grad_id)
        if grad is not None:
            for child in grad:
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if tag != "stop":
                    continue
                stop_color = parse_hex_color(get_style_attr(child, "stop-color", "#000000"))
                if stop_color:
                    return stop_color

    return "000000"


def _build_run_xml(run: dict) -> str:
    """Build DrawingML <a:r> run XML from a run dict."""
    size = font_px_to_half_pts(run["font_size"])
    bold = ' b="1"' if run["bold"] else ""
    italic = ' i="1"' if run["italic"] else ""
    underline = ' u="sng"' if run["underline"] else ""
    latin = xml_escape(run["font_family"])
    ea = xml_escape(run.get("ea_font_family", run["font_family"]))
    alpha = ""
    if run.get("opacity", 1.0) < 0.999:
        alpha = f'<a:alpha val="{round(run["opacity"] * 100000)}"/>'

    fill = f'<a:solidFill><a:srgbClr val="{run["color"]}">{alpha}</a:srgbClr></a:solidFill>'

    return (
        f'<a:r>'
        f'<a:rPr lang="zh-CN" sz="{size}"{bold}{italic}{underline} dirty="0">'
        f'{fill}'
        f'<a:latin typeface="{latin}"/>'
        f'<a:ea typeface="{ea}"/>'
        f'<a:cs typeface="{latin}"/>'
        f'</a:rPr>'
        f'<a:t>{xml_escape(run["text"])}</a:t>'
        f'</a:r>'
    )


def _build_text_shape(x: float, y: float, runs: list[dict], ctx: ConvertContext, anchor: str) -> str:
    """Build a single DrawingML text box."""
    total_text = "".join(run["text"] for run in runs)
    max_font_size = max((run["font_size"] for run in runs), default=16)
    text_w = estimate_text_width(total_text, max_font_size, any(run["bold"] for run in runs))
    text_h = max_font_size * 1.35
    padded_w = max(text_w * 1.6 + max_font_size, 24)

    algn_map = {"start": "l", "middle": "ctr", "end": "r"}
    algn = algn_map.get(anchor, "l")

    anchor_x = x
    if anchor == "middle":
        anchor_x = x - padded_w / 2
    elif anchor == "end":
        anchor_x = x - padded_w

    off_x = px_to_emu(ctx.ctx_x(anchor_x))
    off_y = px_to_emu(ctx.ctx_y(y) - max_font_size * 1.05)  # SVG y uses baseline
    ext_cx = px_to_emu(ctx.ctx_w(padded_w))
    ext_cy = px_to_emu(ctx.ctx_h(text_h))
    runs_xml = "".join(_build_run_xml(run) for run in runs)
    sid = ctx.next_id()

    return (
        f'<p:sp>'
        f'<p:nvSpPr><p:cNvPr id="{sid}" name="Text {sid}"/>'
        f'<p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr>'
        f'<a:xfrm><a:off x="{off_x}" y="{off_y}"/>'
        f'<a:ext cx="{ext_cx}" cy="{ext_cy}"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f'<a:noFill/>'
        f'</p:spPr>'
        f'<p:txBody>'
        f'<a:bodyPr wrap="none" anchor="t" lIns="0" tIns="0" rIns="0" bIns="0"><a:noAutofit/></a:bodyPr>'
        f'<a:lstStyle/>'
        f'<a:p><a:pPr algn="{algn}"/>{runs_xml}</a:p>'
        f'</p:txBody>'
        f'</p:sp>'
    )


def _points_to_path(points_str: str, closed: bool) -> str:
    """Convert SVG points attribute to path d string."""
    nums = re.findall(r"[+-]?(?:\d+\.?\d*|\.\d+)", points_str)
    if len(nums) < 4:
        return ""
    parts = [f"M{nums[0]},{nums[1]}"]
    for i in range(2, len(nums) - 1, 2):
        parts.append(f"L{nums[i]},{nums[i + 1]}")
    if closed:
        parts.append("Z")
    return " ".join(parts)


def _matrix_has_rotation_or_flip(matrix: tuple[float, float, float, float, float, float]) -> bool:
    a, b, c, d, _e, _f = matrix
    return abs(b) > 1e-9 or abs(c) > 1e-9 or a < 0 or d < 0


class _FakeElem:
    """Minimal element-like object for forwarding attributes."""

    def __init__(self, attrib: dict):
        self.attrib = dict(attrib)
        self.tag = ""
        self.text = None

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.attrib.get(key, default)

    def __iter__(self):
        return iter([])


def _clone_with_attr(elem: Any, key: str, value: str) -> _FakeElem:
    """Create a fake element with an extra attribute."""
    fake = _FakeElem(elem.attrib)
    fake.attrib[key] = value
    fake.tag = elem.tag
    return fake
