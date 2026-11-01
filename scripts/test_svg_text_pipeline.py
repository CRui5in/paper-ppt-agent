"""Regression tests for SVG text geometry, mathematical layout, and PPT export."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import xml.etree.ElementTree as ET

from backend.generator.svg_finalize.flatten_tspan import flatten_text_in_svg
from backend.generator.svg_finalize.merge_adjacent_text import merge_adjacent_text_in_svg
from backend.generator.svg_finalize.render_math import render_math_in_tree
from backend.generator.svg_finalize.render_ready import prepare_svg_file_content_for_render
from backend.generator.svg_to_pptx.converter import convert_svg_to_slide_shapes
from backend.orchestrator.deck_plan import _extract_page_title_parts
from backend.orchestrator.svg_executor import _normalize_obvious_short_text_lines

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">{}</svg>'
NS = '{http://www.w3.org/2000/svg}'


class SvgTextGeometryTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'slide_001.svg'

    def write(self, body):
        self.path.write_text(SVG.format(body), encoding='utf-8')

    def test_positioned_multiline_text_keeps_its_card_coordinates(self):
        source = SVG.format(
            '<g data-textbox="64 228 336 108"><text font-size="16">'
            '<tspan x="64" y="236">A long first line describing the architecture</tspan>'
            '<tspan x="64" y="262">A second line completing the explanation</tspan>'
            '</text></g>'
        )
        result = _normalize_obvious_short_text_lines(source, '')
        root = ET.fromstring(result)
        lines = [e for e in root.iter(NS + 'tspan') if e.get('x') is not None]
        self.assertEqual((lines[0].get('x'), lines[0].get('y')), ('64', '236'))
        self.assertTrue(all(e.get('x') == '64' and 228 <= float(e.get('y')) <= 336 for e in lines))
        self.assertIn('completing the explanation', ''.join(root.itertext()))

    def test_single_positioned_tspan_is_resolved_before_sibling_merge(self):
        self.write('<g><text font-size="20"><tspan x="84" y="496">First sentence</tspan></text>'
                   '<text font-size="15"><tspan x="84" y="532">Supporting explanation</tspan></text></g>')
        flatten_text_in_svg(self.path)
        merge_adjacent_text_in_svg(self.path)
        texts = list(ET.parse(self.path).getroot().iter(NS + 'text'))
        self.assertEqual([(e.get('x'), e.get('y')) for e in texts], [('84', '496'), ('84', '532')])

    def test_unresolved_text_anchors_are_not_assumed_to_be_zero(self):
        self.write('<g><text><tspan x="100" y="200">First</tspan></text>'
                   '<text><tspan x="100" y="300">Second</tspan></text></g>')
        self.assertEqual(merge_adjacent_text_in_svg(self.path), 0)
        self.assertEqual(len(list(ET.parse(self.path).getroot().iter(NS + 'text'))), 2)

    def test_relative_scripts_survive_both_text_passes(self):
        self.write('<text x="100" y="200" font-size="28">x<tspan dy="8" font-size="16">i</tspan>'
                   '<tspan dy="-8"> + y</tspan></text>')
        flatten_text_in_svg(self.path)
        merge_adjacent_text_in_svg(self.path)
        spans = list(ET.parse(self.path).getroot().iter(NS + 'tspan'))
        self.assertEqual([e.get('dy') for e in spans], ['8', '-8'])

    def test_different_baselines_are_not_merged(self):
        self.write('<g><text x="100" y="200">x</text><text x="100" y="207">i</text></g>')
        self.assertEqual(merge_adjacent_text_in_svg(self.path), 0)

    def test_independently_positioned_runs_on_one_baseline_keep_their_x(self):
        self.write('<text font-size="20"><tspan x="100" y="200">Left</tspan>'
                   '<tspan x="300" y="200">Right</tspan></text>')
        flatten_text_in_svg(self.path)
        spans = list(ET.parse(self.path).getroot().iter(NS + 'tspan'))
        self.assertEqual([e.get('x') for e in spans], ['100', '300'])

    def test_flattening_preserves_parent_transform_and_data(self):
        self.write('<text transform="translate(40 60)" data-role="caption">'
                   '<tspan x="10" y="20">One</tspan><tspan x="10" y="50">Two</tspan></text>')
        flatten_text_in_svg(self.path)
        texts = list(ET.parse(self.path).getroot().iter(NS + 'text'))
        self.assertTrue(all(e.get('transform') == 'translate(40 60)' and e.get('data-role') == 'caption' for e in texts))

    def test_bold_body_text_is_not_promoted_to_a_subtitle(self):
        title, subtitle = _extract_page_title_parts('<!-- page_type: content -->\n# Results\n\n**Measured comparison**\n\nEvidence.')
        self.assertEqual((title, subtitle), ('Results', ''))

    def test_native_export_advances_past_parent_text_before_subscript(self):
        self.write('<text x="100" y="200" font-size="28" fill="#123456">x'
                   '<tspan dy="8" font-size="16">i</tspan><tspan dy="-8">+y</tspan></text>')
        xml, _, _ = convert_svg_to_slide_shapes(self.path)
        root = ET.fromstring(xml)
        ns = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}
        offsets = root.findall('.//a:xfrm/a:off', ns)
        self.assertGreater(int(offsets[1].get('x')), int(offsets[0].get('x')))
        self.assertTrue(all(e.get('val') == '123456' for e in root.findall('.//a:srgbClr', ns)))


class MathematicalLayoutTests(unittest.TestCase):
    def test_math_becomes_vector_geometry_and_exports_as_shapes(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'math.svg'
            path.write_text(SVG.format(r'<g data-latex="\sum_{i=0}^{n-1}\frac{a_i}{b_i}" data-math-box="100 180 500 100" font-size="32"/>'), encoding='utf-8')
            rendered = prepare_svg_file_content_for_render(path)
            root = ET.fromstring(rendered)
            self.assertTrue(list(root.iter(NS + 'path')))
            self.assertFalse(list(root.iter(NS + 'image')))
            path.write_text(rendered, encoding='utf-8')
            xml, media, _ = convert_svg_to_slide_shapes(path)
            self.assertIn('<a:custGeom>', xml)
            self.assertFalse(media)

    def test_rendering_is_idempotent_and_reacts_to_expression_edits(self):
        root = ET.fromstring(SVG.format(r'<g data-latex="x_i^2" data-math-box="100 180 500 100"/>'))
        self.assertEqual(render_math_in_tree(root), 1)
        first = ET.tostring(root)
        self.assertEqual(render_math_in_tree(root), 0)
        self.assertEqual(first, ET.tostring(root))
        group = next(root.iter(NS + 'g'))
        group.set('data-latex', r'y_j^3')
        self.assertEqual(render_math_in_tree(root), 1)
        self.assertNotEqual(first, ET.tostring(root))

    def test_unsupported_notation_reports_an_error(self):
        root = ET.fromstring(SVG.format(r'<g data-latex="\unknowncommand{x}" data-math-box="100 180 500 100"/>'))
        with self.assertRaisesRegex(ValueError, 'Cannot typeset expression'):
            render_math_in_tree(root)


if __name__ == '__main__':
    unittest.main()
