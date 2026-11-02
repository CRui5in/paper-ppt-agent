"""Regression tests for SVG text geometry, mathematical layout, and PPT export."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import xml.etree.ElementTree as ET

from backend.generator.svg_finalize.flatten_tspan import flatten_text_in_svg
from backend.generator.svg_finalize.merge_adjacent_text import merge_adjacent_text_in_svg
from backend.generator.svg_finalize.render_math import render_math_in_tree, math_expression
from backend.generator.svg_finalize.render_ready import prepare_svg_file_content_for_render
from backend.generator.svg_to_pptx.converter import convert_svg_to_slide_shapes
from backend.generator.svg_text_layout import text_runs
from backend.generator.svg_to_pptx.builder import create_pptx
import json
from backend.orchestrator.deck_plan import _extract_page_title_parts
from backend.orchestrator.svg_executor import _normalize_obvious_short_text_lines, _svg_for_model

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">{}</svg>'
NS = '{http://www.w3.org/2000/svg}'


class SvgTextGeometryTests(unittest.TestCase):
    def test_native_conversion_failure_is_reported_without_a_full_slide_image(self):
        with TemporaryDirectory() as directory:
            source = Path(directory)/'unsupported.svg'
            source.write_text(SVG.format('<defs><filter id="shadow"><feDropShadow/></filter></defs>'
                '<g filter="url(#shadow)"><rect width="100" height="100"/></g>'), encoding='utf8')
            output = Path(directory)/'export.pptx'
            with self.assertRaisesRegex(ValueError, 'Native PPTX export failed on slide 1'):
                create_pptx([source], output)
            self.assertFalse(output.exists())

    def test_model_context_keeps_math_source_and_rebuilds_identical_vectors(self):
        source = SVG.format(r'<path id="diagram" d="M20 20 L80 80"/>'
            r'<g data-latex="\sum_{i=0}^{n-1}\frac{a_i}{b_i}" data-math-box="100 180 500 100" font-size="28"/>')
        rendered = _normalize_obvious_short_text_lines(source, '')
        compact = _svg_for_model(rendered)
        root = ET.fromstring(compact)
        math_group = next(e for e in root.iter() if math_expression(e))
        self.assertEqual(math_expression(math_group), r'\sum_{i=0}^{n-1}\frac{a_i}{b_i}')
        self.assertEqual(len(math_group), 1)
        self.assertEqual(math_group[0].tag, NS+'desc')
        self.assertFalse(math_group.get('data-math-rendered'))
        self.assertIsNotNone(next(e for e in root.iter() if e.get('id') == 'diagram'))
        self.assertLess(len(compact), len(rendered)/3)
        rebuilt = _normalize_obvious_short_text_lines(compact, '')
        original_paths = [e.get('d') for e in ET.fromstring(rendered).iter(NS+'path')]
        self.assertEqual([e.get('d') for e in ET.fromstring(rebuilt).iter(NS+'path')], original_paths)

    def test_native_script_fragments_share_the_parent_end_anchor(self):
        from backend.generator.svg_text_layout import text_anchor_offsets
        self.write('<text x="400" y="200" text-anchor="end" font-size="20">x<tspan baseline-shift="sub" font-size="14">i</tspan> tail</text>')
        runs = text_runs(next(ET.parse(self.path).getroot().iter(NS+'text')))
        offset = text_anchor_offsets(runs)[0]
        xml, media, _ = convert_svg_to_slide_shapes(self.path)
        drawing = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
        offsets = ET.fromstring(xml).findall('.//p:sp/p:spPr/a:xfrm/a:off', {
            'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
            'a': drawing[1:-1],
        })
        self.assertAlmostEqual(int(offsets[0].get('x')) / 9525, 400-offset, places=3)
        self.assertGreater(int(offsets[1].get('x')), int(offsets[0].get('x')))
        self.assertFalse(media)

    def test_html_scripts_survive_preview_and_native_export_with_scoped_baselines(self):
        self.write('<text x="100" y="200" font-size="20" fill="#123456">x<sub>i</sub> + y<sup>2</sup> end</text>')
        prepared = prepare_svg_file_content_for_render(self.path)
        root = ET.fromstring(prepared)
        self.assertFalse([e for e in root.iter() if e.tag.rsplit('}', 1)[-1] in {'sub','sup'}])
        runs = text_runs(next(root.iter(NS+'text')))
        self.assertEqual(''.join(r.text for r in runs), 'xi + y2 end')
        by_text = {r.text: r for r in runs}
        self.assertGreater(by_text['i'].y, by_text['x'].y)
        self.assertLess(by_text['2'].y, by_text['x'].y)
        self.assertEqual(by_text[' end'].y, by_text['x'].y)
        self.assertEqual(float(by_text['i'].attrs['font-size']), 14)
        self.path.write_text(prepared, encoding='utf-8')
        xml, media, _ = convert_svg_to_slide_shapes(self.path)
        drawing = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
        self.assertEqual(''.join(ET.fromstring(xml).itertext()).replace(' ', ''), 'xi+y2end')
        self.assertFalse(media)
        self.assertEqual(len(list(ET.fromstring(xml).iter(drawing+'t'))), 5)

    def test_nested_script_styles_and_descendant_tails_keep_their_geometry(self):
        self.write('<text x="100" y="200" font-size="20">A<sub fill="#FF0000">i<sup>2</sup>j</sub> + B</text>')
        prepared = prepare_svg_file_content_for_render(self.path)
        runs = text_runs(next(ET.fromstring(prepared).iter(NS+'text')))
        self.assertEqual(''.join(r.text for r in runs), 'Ai2j + B')
        by_text = {r.text:r for r in runs}
        self.assertEqual(by_text['i'].y, by_text['j'].y)
        self.assertEqual(by_text['A'].y, by_text[' + B'].y)
        self.assertLess(by_text['2'].y, by_text['i'].y)
        self.assertEqual(by_text['2'].attrs['fill'], '#FF0000')
        self.assertAlmostEqual(float(by_text['2'].attrs['font-size']), 9.8)

    def test_script_preparation_is_idempotent_and_keeps_native_editability(self):
        self.write('<text x="100" y="200" font-size="18">z<sub>t+1</sub> = x<sup>2</sup></text>')
        first = prepare_svg_file_content_for_render(self.path)
        self.path.write_text(first, encoding='utf-8')
        second = prepare_svg_file_content_for_render(self.path)
        self.assertEqual(ET.tostring(ET.fromstring(first)), ET.tostring(ET.fromstring(second)))
        output = create_pptx([self.path], Path(self.directory.name)/'scripts.pptx')
        conversion = json.loads(output.with_suffix('.conversion_report.json').read_text(encoding='utf-8'))
        audit = json.loads(output.with_suffix('.export_audit.json').read_text(encoding='utf-8'))
        self.assertEqual(conversion[0]['mode'], 'native')
        self.assertEqual(audit['summary']['status'], 'passed')
        self.assertGreater(audit['slides'][0]['output_metrics']['text_boxes'], 1)

    def test_text_integrity_still_rejects_unhandled_inline_content(self):
        self.write('<text x="100" y="200">Before<unknown>keep this</unknown>after</text>')
        with self.assertRaisesRegex(ValueError, 'text content changed'):
            convert_svg_to_slide_shapes(self.path)

    def test_native_line_boxes_define_spacing_independent_of_slide_master(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'slide.svg'
            path.write_text(SVG.format('<text x="80" y="180" font-size="12">Label</text>'
                '<text x="80" y="204" font-size="24">12.345</text>'), encoding='utf-8')
            xml = ET.fromstring(convert_svg_to_slide_shapes(path)[0])
            drawing = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
            properties = list(xml.iter(drawing+'pPr'))
            self.assertEqual(len(properties), 2)
            for properties in properties:
                self.assertEqual(properties.find(drawing+'lnSpc/'+drawing+'spcPct').get('val'), '100000')
                self.assertEqual(properties.find(drawing+'spcBef/'+drawing+'spcPts').get('val'), '0')
                self.assertEqual(properties.find(drawing+'spcAft/'+drawing+'spcPts').get('val'), '0')

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
    def test_equation_collisions_use_glyph_bounds_instead_of_the_whole_reserved_box(self):
        from backend.generator.svg_critic import check_svg
        equation = r'<g data-latex="x^2" data-math-box="100 180 200 80" font-size="28"/>'
        overlapping = '<text x="100" y="225" font-size="18">Explanation</text>'
        outside_glyphs = '<text x="100" y="257" font-size="12">Caption in the lower margin</text>'
        self.assertIn('equation_overlaps_text', {v.rule for v in check_svg(SVG.format(equation+overlapping)).violations})
        self.assertNotIn('equation_overlaps_text', {v.rule for v in check_svg(SVG.format(equation+outside_glyphs)).violations})

    def test_chinese_mathematical_labels_have_distinct_vector_glyphs(self):
        from backend.generator.svg_finalize.render_math import _math_path, _cjk_math_font
        try:
            _cjk_math_font('面积长度')
        except ValueError:
            self.skipTest('This host has no CJK font installed')
        with self.assertNoLogs('matplotlib', level='WARNING'):
            area = _math_path(r'\text{面积}=a^2', 28)[0]
            length = _math_path(r'\text{长度}=a^2', 28)[0]
        self.assertNotEqual(area, length)

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
