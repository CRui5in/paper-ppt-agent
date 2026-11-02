"""Local TeX-to-vector typesetting shared by preview, layout checks and export."""

from __future__ import annotations

import atexit
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import xml.etree.ElementTree as ET

from ..svg_to_pptx.paths import PathCmd, normalize_path, parse_svg_path, transform_path_commands
from ..svg_to_pptx.utils import multiply_matrices, parse_style_attribute, parse_svg_length, parse_transform_matrix

NS = '{http://www.w3.org/2000/svg}'
_worker = None
_responses = None
_lock = threading.Lock()


def _stop_worker():
    global _worker
    if _worker is not None:
        worker, _worker = _worker, None
        if worker.poll() is None:
            worker.terminate()
        try:
            worker.wait(timeout=3)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()
        for stream in (worker.stdin, worker.stdout):
            stream.close()


atexit.register(_stop_worker)


def _tex_svg(expression: str) -> ET.Element:
    global _worker, _responses
    with _lock:
        if _worker is None:
            node = shutil.which('node')
            if node is None:
                raise ValueError('Formula typesetting requires Node.js and the frontend npm dependencies.')
            _worker = subprocess.Popen(
                [node, str(Path(__file__).with_name('mathjax_worker.cjs'))],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
            responses = _responses = queue.Queue()
            def read(worker):
                try:
                    for line in worker.stdout:
                        responses.put(line)
                finally:
                    responses.put(None)
            threading.Thread(target=read, args=(_worker,), daemon=True, name='mathjax-reader').start()
        try:
            _worker.stdin.write(json.dumps({'expression': expression}) + '\n')
            _worker.stdin.flush()
            line = _responses.get(timeout=30)
            if line is None:
                _stop_worker()
                raise ValueError('Formula engine stopped. Install dependencies with npm ci in frontend/.')
            response = json.loads(line)
        except (OSError, queue.Empty, json.JSONDecodeError) as exc:
            _stop_worker()
            raise ValueError('Formula engine did not return vector output.') from exc
        if response.get('error'):
            raise ValueError(response['error'])
        def element(node):
            result = ET.Element(NS + node['tag'], {key: str(value) for key, value in node['attributes'].items() if key != 'xmlns'})
            for child in node['children']:
                if isinstance(child, str):
                    if len(result):
                        result[-1].tail = (result[-1].tail or '') + child
                    else:
                        result.text = (result.text or '') + child
                else:
                    result.append(element(child))
            return result
        root = element(response['tree'])
        import cssselect2
        import tinycss2
        matcher = cssselect2.Matcher()
        for rule in tinycss2.parse_stylesheet(response.get('css', ''), skip_comments=True, skip_whitespace=True):
            if rule.type != 'qualified-rule':
                continue
            declarations = {item.lower_name: tinycss2.serialize(item.value).strip()
                            for item in tinycss2.parse_declaration_list(rule.content)
                            if item.type == 'declaration'}
            for selector in cssselect2.compile_selector_list(rule.prelude):
                matcher.add_selector(selector, declarations)
        for wrapped in cssselect2.ElementWrapper.from_xml_root(root).iter_subtree():
            declarations = {}
            for _specificity, _order, _pseudo, matched in matcher.match(wrapped):
                declarations.update(matched)
            if declarations:
                declarations.update(parse_style_attribute(wrapped.etree_element.get('style', '')))
                wrapped.etree_element.set('style', ';'.join(f'{key}:{value}' for key, value in declarations.items()))
        return root


def _mpl_commands(path) -> list[PathCmd]:
    from matplotlib.path import Path as MplPath
    commands = []
    names = {MplPath.MOVETO: 'M', MplPath.LINETO: 'L', MplPath.CURVE3: 'Q', MplPath.CURVE4: 'C'}
    for vertices, code in path.iter_segments(simplify=False):
        if code == MplPath.CLOSEPOLY:
            commands.append(PathCmd('Z', []))
        elif code in names:
            commands.append(PathCmd(names[code], list(vertices)))
    return normalize_path(commands)


def _serialize(commands) -> str:
    return ' '.join(command.cmd + ' '.join(f'{value:.9g}' for value in command.args) for command in commands)


def _outline_text(root: ET.Element) -> None:
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextPath
    from .render_math import _cjk_math_font
    import unicodedata
    for element in root.iter(NS + 'text'):
        text = ''.join(element.itertext())
        if not text.strip():
            element.tag = NS + 'path'
            element.text = None
            continue
        cjk = ''.join(sorted({char for char in text if unicodedata.east_asian_width(char) in {'W', 'F'}}))
        family = _cjk_math_font(cjk) if cjk else 'DejaVu Serif'
        size = float(element.get('font-size', '1000').removesuffix('px'))
        prop = FontProperties(family=family, weight=element.get('font-weight', 'normal'),
                              style=element.get('font-style', 'normal'))
        from matplotlib.ft2font import FT2Font
        from matplotlib.font_manager import findfont
        font = FT2Font(findfont(prop))
        if any(not font.get_char_index(ord(char)) for char in text if not char.isspace()):
            raise ValueError(f'No vector glyph is installed for mathematical text: {text}')
        glyphs = TextPath((float(element.get('x', '0')), -float(element.get('y', '0'))), text, size=size, prop=prop)
        commands = transform_path_commands(_mpl_commands(glyphs), (1, 0, 0, -1, 0, 0))
        element.tag = NS + 'path'
        element.text = None
        for child in list(element):
            element.remove(child)
        element.set('d', _serialize(commands))


@lru_cache(maxsize=256)
def math_artwork(expression: str, font_size: float):
    """Return outlined paths and exact Bézier bounds in an upward coordinate system."""
    from matplotlib.path import Path as MplPath
    root = _tex_svg(expression)
    _outline_text(root)
    flattened = root
    definitions = {element.get('id'): element for element in flattened.iter() if element.get('id')}
    shapes, bounds = [], []
    ratio = font_size / 1000

    def walk(element, matrix, style, viewport, references=()):
        matrix = multiply_matrices(matrix, parse_transform_matrix(element.get('transform', '')))
        style = {**style, **{key: element.get(key) for key in ('fill', 'stroke', 'stroke-width') if element.get(key) is not None},
                 **parse_style_attribute(element.get('style', ''))}
        tag = element.tag.rsplit('}', 1)[-1]
        if tag == 'svg':
            def length(name, default, dimension):
                value = element.get(name, str(default))
                return float(value[:-1])*dimension/100 if value.endswith('%') else float(value.removesuffix('px'))
            width = length('width', viewport[0], viewport[0])
            height = length('height', viewport[1], viewport[1])
            x, y = length('x', 0, viewport[0]), length('y', 0, viewport[1])
            view = [float(value) for value in element.get('viewBox', f'0 0 {width} {height}').split()]
            vx, vy, vw, vh = view
            sx, sy = width/vw, height/vh
            align = element.get('preserveAspectRatio', 'xMidYMid')
            dx = dy = 0
            if align != 'none':
                sx = sy = max(sx, sy) if 'slice' in align else min(sx, sy)
                dx = (width-vw*sx) * (0 if 'xMin' in align else 1 if 'xMax' in align else .5)
                dy = (height-vh*sy) * (0 if 'YMin' in align else 1 if 'YMax' in align else .5)
            matrix = multiply_matrices(matrix, (sx, 0, 0, sy, x+dx-vx*sx, y+dy-vy*sy))
            viewport = (vw, vh)
        if tag == 'defs':
            return
        if tag == 'use':
            href = element.get('{http://www.w3.org/1999/xlink}href', element.get('href', ''))
            if href not in references and href.removeprefix('#') in definitions:
                shift = (1, 0, 0, 1, float(element.get('x', 0)), float(element.get('y', 0)))
                walk(definitions[href[1:]], multiply_matrices(matrix, shift), style, viewport, references + (href,))
            else:
                raise ValueError('Unresolved vector glyph reference')
        elif tag in {'path', 'rect', 'line'}:
            if tag == 'path':
                geometry = element.get('d', '')
            elif tag == 'rect':
                x, y, w, h = [float(element.get(key, 0)) for key in ('x', 'y', 'width', 'height')]
                geometry = f'M{x} {y}h{w}v{h}h{-w}Z'
            else:
                x1, y1, x2, y2 = [float(element.get(key, 0)) for key in ('x1', 'y1', 'x2', 'y2')]
                geometry = f'M{x1} {y1}L{x2} {y2}'
            commands = transform_path_commands(normalize_path(parse_svg_path(geometry)), matrix)
            vertices, codes = [], []
            for command in commands:
                if command.cmd == 'Z':
                    vertices.append((0, 0)); codes.append(MplPath.CLOSEPOLY)
                else:
                    points = list(zip(command.args[::2], command.args[1::2]))
                    vertices.extend(points)
                    codes.extend([{'M': MplPath.MOVETO, 'L': MplPath.LINETO, 'C': MplPath.CURVE4}[command.cmd]] * len(points))
            if vertices:
                factor = math.sqrt(abs(matrix[0]*matrix[3]-matrix[1]*matrix[2]))
                width = parse_svg_length(style.get('stroke-width', 0)) * factor
                box = MplPath(vertices, codes).get_extents()
                if style.get('stroke', 'none') != 'none':
                    from matplotlib.transforms import Bbox
                    box = Bbox.from_extents(box.x0-width/2, box.y0-width/2, box.x1+width/2, box.y1+width/2)
                bounds.append(box)
                extra = {}
                if style.get('stroke-linecap'):
                    extra['stroke-linecap'] = style['stroke-linecap']
                if style.get('stroke-dasharray'):
                    extra['stroke-dasharray'] = ' '.join(str(float(value)*factor) for value in style['stroke-dasharray'].replace(',', ' ').split())
                shape = (_serialize(commands), style.get('fill', 'currentColor'), style.get('stroke', 'none'), width, tuple(extra.items()))
                if shapes and shapes[-1][1:] == shape[1:]:
                    shapes[-1] = (shapes[-1][0] + ' ' + shape[0], *shape[1:])
                else:
                    shapes.append(shape)
        else:
            if tag not in {'svg', 'g', 'clipPath'}:
                raise ValueError(f'Unsupported vector primitive from formula engine: {tag}')
            for child in element:
                walk(child, matrix, style, viewport, references)

    walk(flattened, (ratio, 0, 0, -ratio, 0, 0), {}, (float(root.get("width")), float(root.get("height"))))
    if not bounds:
        raise ValueError('The expression has no drawable mathematical content')
    x0, y0 = min(box.x0 for box in bounds), min(box.y0 for box in bounds)
    x1, y1 = max(box.x1 for box in bounds), max(box.y1 for box in bounds)
    return tuple(shapes), (float(x0), float(y0), float(x1-x0), float(y1-y0))
