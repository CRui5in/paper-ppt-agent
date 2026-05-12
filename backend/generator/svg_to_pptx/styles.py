"""SVG style to DrawingML XML conversion.

Handles fill, stroke, gradients, and effects (shadow, glow).
"""

from __future__ import annotations

import math
import re
from typing import Any

from .utils import (
    parse_hex_color,
    get_style_attr,
    parse_svg_length,
    parse_svg_ratio,
    px_to_emu,
    resolve_url_id,
)


def build_fill_xml(elem: Any, ctx: Any, opacity: float = 1.0) -> str:
    """Build DrawingML fill XML from SVG element attributes."""
    fill = ctx.get_attr(elem, "fill", "#000000")
    fill_opacity = _parse_opacity(ctx.get_attr(elem, "fill-opacity", "1"))
    combined_opacity = opacity * fill_opacity

    if fill == "none":
        return "<a:noFill/>"

    # Gradient reference
    url_id = resolve_url_id(fill) if fill.startswith("url(") else None
    if url_id and url_id in ctx.defs:
        return build_gradient_fill(ctx.defs[url_id], combined_opacity)

    # Solid color
    color = parse_hex_color(fill)
    if not color:
        color = "000000"
    return build_solid_fill(color, combined_opacity)


def build_solid_fill(color: str, opacity: float = 1.0) -> str:
    """Build solid fill XML."""
    alpha = ""
    if opacity < 0.999:
        alpha_val = round(opacity * 100000)
        alpha = f'<a:alpha val="{alpha_val}"/>'
    return f'<a:solidFill><a:srgbClr val="{color}">{alpha}</a:srgbClr></a:solidFill>'


def build_gradient_fill(grad_elem: Any, opacity: float = 1.0) -> str:
    """Build gradient fill from SVG gradient element."""
    tag = grad_elem.tag.split("}")[-1] if "}" in grad_elem.tag else grad_elem.tag

    # Parse stops
    stops_xml = []
    for child in grad_elem:
        child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if child_tag != "stop":
            continue
        offset = child.get("offset", "0")
        if offset.endswith("%"):
            pos = int(parse_svg_length(offset, 0) * 1000)
        else:
            pos = int(parse_svg_ratio(offset, 0) * 100000)

        color = get_style_attr(child, "stop-color", "#000000")
        stop_opacity = _parse_opacity(get_style_attr(child, "stop-opacity", "1")) * opacity
        hex_color = parse_hex_color(color) or "000000"

        alpha = ""
        if stop_opacity < 0.999:
            alpha = f'<a:alpha val="{round(stop_opacity * 100000)}"/>'
        stops_xml.append(
            f'<a:gs pos="{pos}"><a:srgbClr val="{hex_color}">{alpha}</a:srgbClr></a:gs>'
        )

    if not stops_xml:
        return "<a:noFill/>"

    gs_lst = f'<a:gsLst>{"".join(stops_xml)}</a:gsLst>'

    if tag == "linearGradient":
        x1 = parse_svg_ratio(grad_elem.get("x1", "0"), 0)
        y1 = parse_svg_ratio(grad_elem.get("y1", "0"), 0)
        x2 = parse_svg_ratio(grad_elem.get("x2", "1"), 1)
        y2 = parse_svg_ratio(grad_elem.get("y2", "0"), 0)
        angle_rad = math.atan2(y2 - y1, x2 - x1)
        angle_deg = math.degrees(angle_rad)
        angle_emu = round(angle_deg * 60000) % 21600000
        return f'<a:gradFill>{gs_lst}<a:lin ang="{angle_emu}" scaled="1"/></a:gradFill>'

    # Radial gradient
    return (
        f'<a:gradFill>{gs_lst}'
        f'<a:path path="circle">'
        f'<a:fillToRect l="50000" t="50000" r="50000" b="50000"/>'
        f'</a:path></a:gradFill>'
    )


def build_stroke_xml(elem: Any, ctx: Any, opacity: float = 1.0) -> str:
    """Build DrawingML line (stroke) XML from SVG element attributes."""
    stroke = ctx.get_attr(elem, "stroke", "none")
    if stroke == "none":
        return ""

    stroke_opacity = _parse_opacity(ctx.get_attr(elem, "stroke-opacity", "1"))
    combined_opacity = opacity * stroke_opacity

    color = parse_hex_color(stroke) or "000000"
    width_str = ctx.get_attr(elem, "stroke-width", "1")
    width_px = parse_svg_length(width_str, 1.0)
    width_emu = px_to_emu(width_px)

    # Fill
    fill = build_solid_fill(color, combined_opacity)

    # Dash pattern
    dash = ""
    dash_array = ctx.get_attr(elem, "stroke-dasharray", "")
    if dash_array and dash_array != "none":
        dash = _build_dash_xml(dash_array)

    # Line cap
    cap_map = {"round": "rnd", "square": "sq", "butt": "flat"}
    cap = cap_map.get(ctx.get_attr(elem, "stroke-linecap", ""), "")
    cap_attr = f' cap="{cap}"' if cap else ""

    # Line join
    join = ctx.get_attr(elem, "stroke-linejoin", "")
    join_xml = ""
    if join == "round":
        join_xml = "<a:round/>"
    elif join == "bevel":
        join_xml = "<a:bevel/>"
    elif join == "miter":
        join_xml = '<a:miter lim="800000"/>'

    return f'<a:ln w="{width_emu}"{cap_attr}>{fill}{dash}{join_xml}</a:ln>'


def _build_dash_xml(dash_array: str) -> str:
    """Build dash pattern XML."""
    presets = {
        "4,4": "dash", "6,3": "dash",
        "2,2": "sysDot", "1,1": "sysDot",
        "8,4": "lgDash", "8,4,2,4": "lgDashDot",
    }
    normalized = ",".join(s.strip() for s in dash_array.split(",") if s.strip())
    if normalized in presets:
        return f'<a:prstDash val="{presets[normalized]}"/>'

    # Custom dash
    parts = [float(s) for s in re.findall(r"[\d.]+", dash_array)]
    if len(parts) < 2:
        return '<a:prstDash val="dash"/>'

    entries = []
    for i in range(0, len(parts) - 1, 2):
        d = round(parts[i] * 100)
        sp = round(parts[i + 1] * 100) if i + 1 < len(parts) else d
        entries.append(f'<a:ds d="{d}" sp="{sp}"/>')
    return f'<a:custDash>{"".join(entries)}</a:custDash>'


def _parse_opacity(val: str) -> float:
    return max(0.0, min(1.0, parse_svg_ratio(val, 1.0)))
