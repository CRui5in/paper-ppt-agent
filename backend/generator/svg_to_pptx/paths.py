"""SVG path parsing and conversion to DrawingML custom geometry.

Handles: M, L, H, V, C, S, Q, T, A, Z commands.
All commands are normalized to M/L/C/Z (absolute cubic Bézier).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .utils import Matrix, identity_matrix, px_to_emu, transform_point


@dataclass
class PathCmd:
    cmd: str  # M, L, C, Z (uppercase = absolute, after normalization)
    args: list[float]


def parse_svg_path(d: str) -> list[PathCmd]:
    """Tokenize SVG path d attribute into commands."""
    tokens = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", d)
    commands: list[PathCmd] = []
    current_cmd = ""
    current_args: list[float] = []

    for token in tokens:
        if token.isalpha():
            if current_cmd:
                commands.append(PathCmd(current_cmd, current_args))
            current_cmd = token
            current_args = []
        else:
            current_args.append(float(token))
    if current_cmd:
        commands.append(PathCmd(current_cmd, current_args))

    return commands


def normalize_path(commands: list[PathCmd]) -> list[PathCmd]:
    """Convert all path commands to absolute M/L/C/Z."""
    result: list[PathCmd] = []
    cx, cy = 0.0, 0.0  # Current point
    sx, sy = 0.0, 0.0  # Subpath start
    last_ctrl_x, last_ctrl_y = 0.0, 0.0
    last_cmd = ""

    for cmd in commands:
        c = cmd.cmd
        args = cmd.args
        is_rel = c.islower()
        C = c.upper()

        if C == "M":
            for i in range(0, len(args), 2):
                x, y = args[i], args[i + 1]
                if is_rel:
                    x += cx; y += cy
                result.append(PathCmd("M" if i == 0 else "L", [x, y]))
                cx, cy = x, y
                if i == 0:
                    sx, sy = x, y
        elif C == "L":
            for i in range(0, len(args), 2):
                x, y = args[i], args[i + 1]
                if is_rel:
                    x += cx; y += cy
                result.append(PathCmd("L", [x, y]))
                cx, cy = x, y
        elif C == "H":
            for x in args:
                if is_rel:
                    x += cx
                result.append(PathCmd("L", [x, cy]))
                cx = x
        elif C == "V":
            for y in args:
                if is_rel:
                    y += cy
                result.append(PathCmd("L", [cx, y]))
                cy = y
        elif C == "C":
            for i in range(0, len(args), 6):
                pts = list(args[i:i + 6])
                if is_rel:
                    for j in range(0, 6, 2):
                        pts[j] += cx
                        pts[j + 1] += cy
                result.append(PathCmd("C", pts))
                last_ctrl_x, last_ctrl_y = pts[2], pts[3]
                cx, cy = pts[4], pts[5]
        elif C == "S":
            for i in range(0, len(args), 4):
                # Reflected control point
                if last_cmd in ("C", "S"):
                    rx = 2 * cx - last_ctrl_x
                    ry = 2 * cy - last_ctrl_y
                else:
                    rx, ry = cx, cy
                x2, y2 = args[i], args[i + 1]
                x, y = args[i + 2], args[i + 3]
                if is_rel:
                    x2 += cx; y2 += cy; x += cx; y += cy
                result.append(PathCmd("C", [rx, ry, x2, y2, x, y]))
                last_ctrl_x, last_ctrl_y = x2, y2
                cx, cy = x, y
        elif C == "Q":
            for i in range(0, len(args), 4):
                qx, qy = args[i], args[i + 1]
                x, y = args[i + 2], args[i + 3]
                if is_rel:
                    qx += cx; qy += cy; x += cx; y += cy
                # Convert quadratic to cubic
                c1x = cx + 2 / 3 * (qx - cx)
                c1y = cy + 2 / 3 * (qy - cy)
                c2x = x + 2 / 3 * (qx - x)
                c2y = y + 2 / 3 * (qy - y)
                result.append(PathCmd("C", [c1x, c1y, c2x, c2y, x, y]))
                last_ctrl_x, last_ctrl_y = qx, qy
                cx, cy = x, y
        elif C == "T":
            for i in range(0, len(args), 2):
                if last_cmd in ("Q", "T"):
                    qx = 2 * cx - last_ctrl_x
                    qy = 2 * cy - last_ctrl_y
                else:
                    qx, qy = cx, cy
                x, y = args[i], args[i + 1]
                if is_rel:
                    x += cx; y += cy
                c1x = cx + 2 / 3 * (qx - cx)
                c1y = cy + 2 / 3 * (qy - cy)
                c2x = x + 2 / 3 * (qx - x)
                c2y = y + 2 / 3 * (qy - y)
                result.append(PathCmd("C", [c1x, c1y, c2x, c2y, x, y]))
                last_ctrl_x, last_ctrl_y = qx, qy
                cx, cy = x, y
        elif C == "A":
            for i in range(0, len(args), 7):
                rx_a, ry_a = abs(args[i]), abs(args[i + 1])
                x_rot = args[i + 2]
                large_arc = int(args[i + 3])
                sweep = int(args[i + 4])
                x, y = args[i + 5], args[i + 6]
                if is_rel:
                    x += cx; y += cy
                cubics = _arc_to_cubics(cx, cy, rx_a, ry_a, x_rot, large_arc, sweep, x, y)
                for cubic in cubics:
                    result.append(PathCmd("C", cubic))
                cx, cy = x, y
        elif C == "Z":
            result.append(PathCmd("Z", []))
            cx, cy = sx, sy

        last_cmd = C

    return result


def path_commands_to_drawingml(
    commands: list[PathCmd],
    offset_x: float = 0,
    offset_y: float = 0,
    scale_x: float = 1,
    scale_y: float = 1,
    matrix: Matrix | None = None,
) -> tuple[str, float, float, float, float]:
    """Convert normalized path commands to DrawingML custom geometry XML.

    Returns:
        (path_xml, min_x_emu, min_y_emu, width_emu, height_emu)
    """
    if matrix is not None:
        commands = transform_path_commands(commands, matrix)
        offset_x = 0
        offset_y = 0
        scale_x = 1
        scale_y = 1

    # Calculate bounding box
    all_x: list[float] = []
    all_y: list[float] = []
    for cmd in commands:
        for i in range(0, len(cmd.args), 2):
            if i + 1 < len(cmd.args):
                all_x.append(cmd.args[i])
                all_y.append(cmd.args[i + 1])

    if not all_x:
        return "", 0, 0, 0, 0

    min_x = min(all_x)
    min_y = min(all_y)
    max_x = max(all_x)
    max_y = max(all_y)
    width = max(max_x - min_x, 0.1)
    height = max(max_y - min_y, 0.1)

    # Scale factor for DrawingML coordinates (relative to bounding box)
    # DrawingML uses a coordinate space where the geometry fills the shape
    W = round(width * scale_x * 10000)
    H = round(height * scale_y * 10000)

    def tx(x: float) -> int:
        return round((x - min_x) / width * W)

    def ty(y: float) -> int:
        return round((y - min_y) / height * H)

    parts = [f'<a:path w="{W}" h="{H}">']
    for cmd in commands:
        if cmd.cmd == "M":
            parts.append(f'<a:moveTo><a:pt x="{tx(cmd.args[0])}" y="{ty(cmd.args[1])}"/></a:moveTo>')
        elif cmd.cmd == "L":
            parts.append(f'<a:lnTo><a:pt x="{tx(cmd.args[0])}" y="{ty(cmd.args[1])}"/></a:lnTo>')
        elif cmd.cmd == "C":
            parts.append(
                f'<a:cubicBezTo>'
                f'<a:pt x="{tx(cmd.args[0])}" y="{ty(cmd.args[1])}"/>'
                f'<a:pt x="{tx(cmd.args[2])}" y="{ty(cmd.args[3])}"/>'
                f'<a:pt x="{tx(cmd.args[4])}" y="{ty(cmd.args[5])}"/>'
                f'</a:cubicBezTo>'
            )
        elif cmd.cmd == "Z":
            parts.append("<a:close/>")
    parts.append("</a:path>")

    path_xml = "".join(parts)

    # Return bounding box in EMU
    bx = px_to_emu((min_x * scale_x) + offset_x)
    by = px_to_emu((min_y * scale_y) + offset_y)
    bw = px_to_emu(width * scale_x)
    bh = px_to_emu(height * scale_y)
    return path_xml, bx, by, bw, bh


def transform_path_commands(commands: list[PathCmd], matrix: Matrix | None) -> list[PathCmd]:
    """Apply an affine transform to every point in normalized path commands."""
    matrix = matrix or identity_matrix()
    result: list[PathCmd] = []
    for cmd in commands:
        if cmd.cmd in {"M", "L"} and len(cmd.args) >= 2:
            x, y = transform_point(matrix, cmd.args[0], cmd.args[1])
            result.append(PathCmd(cmd.cmd, [x, y]))
        elif cmd.cmd == "C" and len(cmd.args) >= 6:
            x1, y1 = transform_point(matrix, cmd.args[0], cmd.args[1])
            x2, y2 = transform_point(matrix, cmd.args[2], cmd.args[3])
            x3, y3 = transform_point(matrix, cmd.args[4], cmd.args[5])
            result.append(PathCmd(cmd.cmd, [x1, y1, x2, y2, x3, y3]))
        else:
            result.append(cmd)
    return result


def _arc_to_cubics(
    x1: float, y1: float,
    rx: float, ry: float,
    x_rot: float,
    large_arc: int, sweep: int,
    x2: float, y2: float,
) -> list[list[float]]:
    """Convert SVG arc to sequence of cubic Bézier curves (SVG spec F.6.5)."""
    if rx == 0 or ry == 0:
        return [[x1, y1, x2, y2, x2, y2]]

    phi = math.radians(x_rot)
    cos_phi = math.cos(phi)
    sin_phi = math.sin(phi)

    # Step 1: Compute (x1', y1')
    dx = (x1 - x2) / 2
    dy = (y1 - y2) / 2
    x1p = cos_phi * dx + sin_phi * dy
    y1p = -sin_phi * dx + cos_phi * dy

    # Step 2: Compute (cx', cy')
    rx2, ry2 = rx * rx, ry * ry
    x1p2, y1p2 = x1p * x1p, y1p * y1p
    lam = x1p2 / rx2 + y1p2 / ry2
    if lam > 1:
        s = math.sqrt(lam)
        rx *= s; ry *= s
        rx2 = rx * rx; ry2 = ry * ry

    num = max(rx2 * ry2 - rx2 * y1p2 - ry2 * x1p2, 0)
    den = rx2 * y1p2 + ry2 * x1p2
    sq = math.sqrt(num / den) if den > 0 else 0
    if large_arc == sweep:
        sq = -sq

    cxp = sq * rx * y1p / ry
    cyp = -sq * ry * x1p / rx

    # Step 3: Compute (cx, cy)
    cx = cos_phi * cxp - sin_phi * cyp + (x1 + x2) / 2
    cy = sin_phi * cxp + cos_phi * cyp + (y1 + y2) / 2

    # Step 4: Compute angles
    theta1 = _angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dtheta = _angle(
        (x1p - cxp) / rx, (y1p - cyp) / ry,
        (-x1p - cxp) / rx, (-y1p - cyp) / ry,
    )
    if sweep == 0 and dtheta > 0:
        dtheta -= 2 * math.pi
    elif sweep == 1 and dtheta < 0:
        dtheta += 2 * math.pi

    # Split into ≤90° segments
    n_segs = max(1, math.ceil(abs(dtheta) / (math.pi / 2)))
    d_per = dtheta / n_segs

    cubics = []
    for i in range(n_segs):
        t1 = theta1 + i * d_per
        t2 = t1 + d_per
        alpha = 4 / 3 * math.tan(d_per / 4)

        # Control points in unit circle
        cos_t1 = math.cos(t1); sin_t1 = math.sin(t1)
        cos_t2 = math.cos(t2); sin_t2 = math.sin(t2)

        ep1x = rx * cos_t1; ep1y = ry * sin_t1
        ep2x = rx * cos_t2; ep2y = ry * sin_t2

        c1x = ep1x - alpha * rx * sin_t1
        c1y = ep1y + alpha * ry * cos_t1
        c2x = ep2x + alpha * rx * sin_t2
        c2y = ep2y - alpha * ry * cos_t2

        # Rotate and translate
        def tr(px: float, py: float) -> tuple[float, float]:
            return cos_phi * px - sin_phi * py + cx, sin_phi * px + cos_phi * py + cy

        tc1 = tr(c1x, c1y)
        tc2 = tr(c2x, c2y)
        tp2 = tr(ep2x, ep2y)

        cubics.append([tc1[0], tc1[1], tc2[0], tc2[1], tp2[0], tp2[1]])

    return cubics


def _angle(ux: float, uy: float, vx: float, vy: float) -> float:
    """Compute angle between two vectors."""
    dot = ux * vx + uy * vy
    cross = ux * vy - uy * vx
    return math.atan2(cross, dot)
