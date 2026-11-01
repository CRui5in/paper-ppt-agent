"""Lossless nested text and container-driven paragraph layout regressions."""

from pathlib import Path
from tempfile import TemporaryDirectory
import re
import unittest
import xml.etree.ElementTree as ET

from backend.generator.svg_text_layout import reflow_declared_paragraphs, measure_text, layout_text_groups, _bounds, paragraph_layout_issues
from backend.generator.svg_to_pptx.converter import convert_svg_to_slide_shapes
from backend.generator.svg_finalize.render_ready import prepare_svg_file_content_for_render
from backend.generator.svg_critic import _text_of, check_svg
from backend.orchestrator.svg_executor import _styled_text_fragments

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">{}</svg>'
NS = '{http://www.w3.org/2000/svg}'
A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'


def signature(text):
    return re.sub(r'\s+', '', text)


class NestedTextTests(unittest.TestCase):
    def native_xml(self, body):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'slide.svg'
            path.write_text(SVG.format(body), encoding='utf-8')
            return ET.fromstring(convert_svg_to_slide_shapes(path)[0])

    def test_nested_highlight_and_its_tail_survive_export(self):
        root = self.native_xml('<text x="100" y="200" font-size="18">'
                               '<tspan x="100" dy="0">前文<tspan fill="#FF0000">关键词</tspan>后文</tspan>'
                               '<tspan x="100" dy="28">下一行</tspan></text>')
        self.assertEqual(''.join(e.text or '' for e in root.iter(A+'t')), '前文关键词后文下一行')
        red = [e for e in root.iter(A+'r') if e.find('.//' + A+'srgbClr') is not None and e.find('.//' + A+'srgbClr').get('val') == 'FF0000']
        self.assertEqual(''.join(e.text or '' for e in red[0].iter(A+'t')), '关键词')

    def test_multiple_nested_metric_values_and_suffixes_survive(self):
        root = self.native_xml('<text x="100" y="200" font-size="15">'
                               '<tspan x="100" dy="0">Precision <tspan font-weight="bold">0.8123</tspan> Recall <tspan fill="#0000FF">0.9456</tspan></tspan>'
                               '<tspan x="100" dy="28">Error <tspan fill="#FF0000">0.012</tspan> units</tspan></text>')
        self.assertEqual(signature(''.join(e.text or '' for e in root.iter(A+'t'))), signature('Precision 0.8123 Recall 0.9456 Error 0.012 units'))

    def test_recursive_fragment_collection_keeps_descendant_tails(self):
        text = ET.fromstring('<text font-size="18">开始<tspan fill="#0000FF">甲<tspan font-weight="bold">乙</tspan>丙</tspan>结束</text>')
        self.assertEqual(''.join(t for t, _ in _styled_text_fragments(text)), '开始甲乙丙结束')

    def test_plain_word_space_before_highlight_is_preserved(self):
        root = self.native_xml('<text x="100" y="200" font-size="18">Precision <tspan fill="#FF0000">0.8123</tspan></text>')
        self.assertEqual(''.join(e.text or '' for e in root.iter(A+'t')), 'Precision 0.8123')

    def test_layout_checker_reads_nested_keywords_and_values(self):
        text = ET.fromstring('<text>Metric <tspan><tspan fill="#0000FF">0.8123</tspan> units</tspan></text>')
        self.assertEqual(_text_of(text), 'Metric 0.8123 units')

    def test_layout_checker_evaluates_geometry_without_banning_design_styles(self):
        report = check_svg(SVG.format('<text x="40" y="60" font-size="28">Title</text><rect x="40" y="74" width="80" height="2" fill="#0000FF"/><text x="40" y="200" font-size="18">正文<tspan font-weight="bold">强调</tspan></text>'))
        self.assertFalse({'accent_line_under_title', 'bold_tspan_in_cjk'} & {v.rule for v in report.violations})

    def test_decorative_dots_are_not_inferred_to_be_empty_list_bullets(self):
        circle = '<circle cx="40" cy="160" r="4" fill="#2B6CB0"/>'
        report = check_svg(SVG.format(circle))
        self.assertNotIn('empty_bullet', {v.rule for v in report.violations})
        explicit = check_svg(SVG.format(circle.replace('<circle ', '<circle data-role="bullet" ')))
        self.assertIn('empty_bullet', {v.rule for v in explicit.violations})


class ParagraphLayoutTests(unittest.TestCase):
    def test_wrapping_keeps_an_inline_script_with_its_base(self):
        width = measure_text('Long x', {'font-size':'18'}) + .1
        root = ET.fromstring(SVG.format(f'<g data-role="body" data-textbox="100 180 {width} 180">'
            '<text x="100" y="204" font-size="18">Long x<tspan baseline-shift="sub" font-size="12">123</tspan></text></g>'))
        reflow_declared_paragraphs(root)
        lines = [signature(''.join(line.itertext())) for line in next(root.iter(NS+'text'))]
        self.assertIn('x123', lines)

    def test_mixed_size_paragraphs_wrap_and_preserve_scripts(self):
        root = ET.fromstring(SVG.format('<g data-role="body" data-textbox="100 180 180 220">'
            '<text x="100" y="204" font-size="18">A value x<tspan baseline-shift="sub" font-size="12">i</tspan>'
            ' with a <tspan font-size="20" font-weight="bold">larger label</tspan> and a complete explanation that needs wrapping.</text></g>'))
        original = signature(''.join(root.itertext()))
        self.assertGreater(reflow_declared_paragraphs(root), 0)
        text = next(root.iter(NS+'text'))
        self.assertLessEqual(_bounds(text)[2], 280.1)
        self.assertEqual(signature(''.join(root.itertext())), original)
        self.assertTrue([e for e in text.iter() if e.get('baseline-shift')])
        first = ET.tostring(root)
        reflow_declared_paragraphs(root)
        self.assertEqual(ET.tostring(root), first)

    def test_inline_emphasis_shares_the_complete_text_anchor(self):
        for anchor in ('middle', 'end'):
            with self.subTest(anchor=anchor):
                el = ET.fromstring(f'<text x="400" y="200" text-anchor="{anchor}" font-size="20">Before <tspan font-weight="bold">bold</tspan> after</text>')
                left, _, right, _ = _bounds(el)
                if anchor == 'end':
                    self.assertAlmostEqual(right, 400, places=5)
                else:
                    self.assertAlmostEqual((left+right)/2, 400, places=5)

    def test_real_overflow_is_retained_when_many_style_warnings_fill_the_report(self):
        labels = ''.join(f'<text x="40" y="{110+i*16}" font-size="8">Label {i}</text>' for i in range(25))
        bad_line = '<text x="1250" y="590" font-size="18">This full sentence exceeds the canvas.</text>'
        report = check_svg(SVG.format(labels + bad_line))
        self.assertFalse(report.passed)
        self.assertIn('out_of_bounds', {v.rule for v in report.violations})

    def test_content_boundary_follows_the_explicit_footer(self):
        footer = '<g data-role="footer"><line x1="40" y1="660" x2="1240" y2="660"/><text x="40" y="688" font-size="12">Footer</text></g>'
        fits = '<rect x="40" y="252" width="560" height="380" fill="#FFFFFF"/>'
        crosses = '<rect x="40" y="252" width="560" height="425" fill="#FFFFFF"/>'
        self.assertNotIn('content_area_overflow', {v.rule for v in check_svg(SVG.format(fits + footer)).violations})
        self.assertIn('content_area_overflow', {v.rule for v in check_svg(SVG.format(crosses + footer)).violations})
        self.assertNotIn('content_area_overflow', {v.rule for v in check_svg(SVG.format(fits)).violations})

    def test_nested_transforms_move_shapes_text_and_equations_in_the_same_geometry_space(self):
        source = '<text x="100" y="200" font-size="18">Explanation</text>'
        shape = '<g transform="translate(400 0)"><rect x="80" y="175" width="300" height="50" fill="#fff"/></g>'
        self.assertNotIn('shape_covers_text', {v.rule for v in check_svg(SVG.format(source+shape)).violations})
        moved = '<g transform="translate(400 0)">'+source+'</g>'+shape
        self.assertIn('shape_covers_text', {v.rule for v in check_svg(SVG.format(moved)).violations})
        equation = '<g transform="translate(400 0)"><g data-latex="x^2" data-math-box="100 180 200 80"/></g>'
        label = '<text x="500" y="225" font-size="18">Explanation</text>'
        self.assertIn('equation_overlaps_text', {v.rule for v in check_svg(SVG.format(equation+label)).violations})

    def test_scaled_inherited_text_and_translated_footer_use_world_bounds(self):
        text = '<g font-size="30" transform="translate(1200 0)"><g transform="scale(2)"><text x="0" y="100">Overflow</text></g></g>'
        self.assertIn('out_of_bounds', {v.rule for v in check_svg(SVG.format(text)).violations})
        footer = '<g data-role="footer" transform="translate(0 40)"><line x1="40" y1="620" x2="1240" y2="620"/></g>'
        card = '<g transform="translate(0 20)"><rect x="40" y="252" width="560" height="380" fill="#fff"/></g>'
        self.assertNotIn('content_area_overflow', {v.rule for v in check_svg(SVG.format(card+footer)).violations})
        self.assertIn('content_area_overflow', {v.rule for v in check_svg(SVG.format(card.replace('380','410')+footer)).violations})

    def test_capacity_uses_last_line_glyphs_instead_of_estimated_descent(self):
        root = ET.fromstring(SVG.format('<g data-role="body" data-textbox="100 180 300 90">'
            '<text x="100" y="200" font-size="15">FIRST LINE</text>'
            '<text x="100" y="222.5" font-size="15">SECOND LINE</text>'
            '<text x="100" y="245" font-size="15">THIRD LINE</text>'
            '<text x="100" y="267.5" font-size="15">FINAL RESULT</text></g>'))
        self.assertEqual(paragraph_layout_issues(root), [])
        self.assertGreater(reflow_declared_paragraphs(root), 0)
        self.assertTrue(all(_bounds(text)[3] <= 270.1 for text in root.iter(NS+'text')))

    def test_container_audit_measures_parent_script_and_tail_as_a_complete_line(self):
        svg = SVG.format('<rect x="100" y="180" width="220" height="50" fill="#FFFFFF"/>'
            '<text x="116" y="210" font-size="18">Value 10<tspan font-size="12" baseline-shift="super">2</tspan>'
            ': A complete explanatory sentence extends beyond this small container.</text>')
        report = check_svg(svg)
        self.assertIn('text_overflow_in_container', {v.rule for v in report.violations})

    def test_paragraphs_share_box_capacity_and_stack_after_wrapping(self):
        root = ET.fromstring(SVG.format('<g data-role="body" data-textbox="100 180 160 220">'
            '<text x="100" y="200" font-size="18">这是一段需要自动换行的正文，其中有<tspan font-weight="bold">强调词</tspan>。</text>'
            '<text x="100" y="228" font-size="18">第二段接在第一段后面。</text></g>'))
        before = signature(''.join(root.itertext()))
        self.assertGreater(reflow_declared_paragraphs(root), 0)
        paragraphs = list(root.iter(NS+'text'))
        self.assertGreater(_bounds(paragraphs[1])[1], _bounds(paragraphs[0])[3])
        self.assertEqual(signature(''.join(root.itertext())), before)
        self.assertEqual(paragraph_layout_issues(root), [])
        laid_out = ET.tostring(root)
        reflow_declared_paragraphs(root)
        self.assertEqual(ET.tostring(root), laid_out)

    def test_overfull_shared_box_is_rejected_as_a_whole(self):
        root = ET.fromstring(SVG.format('<g data-role="body" data-textbox="100 180 160 60">'
            '<text x="100" y="200" font-size="18">这是一段需要自动换行的较长正文，包含完整的信息。</text>'
            '<text x="100" y="228" font-size="18">第二段也需要保留。</text></g>'))
        before = ET.tostring(root)
        self.assertEqual(reflow_declared_paragraphs(root), 0)
        self.assertEqual(ET.tostring(root), before)
        issues = paragraph_layout_issues(root)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['paragraphs'], 2)
        self.assertGreater(issues[0]['required_height'], 60)

    def test_header_label_and_title_have_distinct_rows(self):
        root = ET.fromstring(SVG.format('<g data-role="header"><text x="40" y="40" font-size="12">Chapter</text><text x="40" y="48" font-size="28">Title</text><rect x="40" y="55" width="100" height="2"/></g>'))
        layout_text_groups(root)
        texts = list(root.iter(NS+'text'))
        self.assertGreater(_bounds(texts[1])[1], _bounds(texts[0])[3])
        self.assertGreater(float(next(root.iter(NS+'rect')).get('y')), _bounds(texts[1])[3])

    def test_centered_label_lines_fit_their_shape(self):
        root = ET.fromstring(SVG.format('<g data-layout="center-label"><rect x="100" y="180" width="80" height="36"/><text x="140" y="210" text-anchor="middle" font-size="12">First</text><text x="140" y="226" text-anchor="middle" font-size="12">Second</text></g>'))
        layout_text_groups(root)
        for text in root.iter(NS+'text'):
            box = _bounds(text)
            self.assertGreaterEqual(box[1], 180)
            self.assertLessEqual(box[3], 216)

    def test_external_data_labels_keep_their_authored_positions_through_export(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'slide.svg'
            path.write_text(SVG.format('<g><rect x="185" y="370" width="70" height="100" fill="#1A365D"/>'
                '<text x="220" y="360" font-size="14" text-anchor="middle" fill="#1A365D">0.8832</text>'
                '<text x="220" y="492" font-size="14" text-anchor="middle">CatBoost</text></g>'), encoding='utf-8')
            prepared = prepare_svg_file_content_for_render(path)
            texts = list(ET.fromstring(prepared).iter(NS+'text'))
            self.assertEqual([(t.get('x'), t.get('y')) for t in texts], [('220','360'), ('220','492')])
            path.write_text(prepared, encoding='utf-8')
            xml = ET.fromstring(convert_svg_to_slide_shapes(path)[0])
            self.assertEqual(''.join(e.text or '' for e in xml.iter(A+'t')), '0.8832CatBoost')

    def test_opted_in_group_with_multiple_shapes_does_not_guess_label_owner(self):
        root = ET.fromstring(SVG.format('<g data-layout="center-label"><rect x="100" y="200" width="80" height="100"/>'
            '<rect x="100" y="240" width="80" height="20"/>'
            '<text x="140" y="190" text-anchor="middle">Label</text></g>'))
        before = ET.tostring(root)
        self.assertEqual(layout_text_groups(root), 0)
        self.assertEqual(ET.tostring(root), before)

    def test_manual_emphasis_lines_become_a_paragraph_without_orphan_punctuation(self):
        root = ET.fromstring(SVG.format('<g data-textbox="100 180 280 240" data-role="body">'
            '<text x="100" y="200" font-size="16" font-family="Microsoft YaHei">'
            '<tspan x="100" dy="0">这种观测方式具有</tspan>'
            '<tspan x="100" dy="28" fill="#0000FF">广域覆盖</tspan>'
            '<tspan x="100" dy="28">、</tspan>'
            '<tspan x="100" dy="28" fill="#0000FF">周期重访</tspan>'
            '<tspan x="100" dy="28">等特点，可用于连续监测。</tspan>'
            '</text></g>'))
        before = signature(''.join(root.itertext()))
        self.assertGreater(reflow_declared_paragraphs(root), 0)
        self.assertEqual(signature(''.join(root.itertext())), before)
        text = next(root.iter(NS+'text'))
        self.assertLess(len(list(text)), 5)
        for line in text:
            content = ''.join(line.itertext())
            self.assertNotIn(content.strip(), ('、', '，', '。'))
            self.assertFalse(content.startswith(('、', '，', '。')))
            width = sum(measure_text(''.join(run.itertext()), {'font-size':'16', 'font-family':'Microsoft YaHei', **run.attrib}) for run in line)
            self.assertLessEqual(width, 280.1)

    def test_ordinary_paragraph_layout_is_idempotent(self):
        root = ET.fromstring(SVG.format('<g data-textbox="100 180 280 240" data-role="body"><text x="100" y="200" font-size="16">一段正文与<tspan fill="#0000FF">强调文字</tspan>共同参与完整排版。</text></g>'))
        reflow_declared_paragraphs(root)
        first = ET.tostring(root)
        reflow_declared_paragraphs(root)
        self.assertEqual(ET.tostring(root), first)

    def test_paragraph_inherits_its_container_font_size(self):
        root = ET.fromstring(SVG.format('<g font-size="24" data-textbox="100 180 160 240" data-role="body"><text x="100" y="210">这段正文继承容器字号并按字体实际宽度换行。</text></g>'))
        reflow_declared_paragraphs(root)
        text = next(root.iter(NS+'text'))
        self.assertGreater(len(list(text)), 2)
        self.assertTrue(all(number.attrib.get('font-size') == '24' for line in text for number in line))

    def test_insufficient_height_keeps_complete_source_text(self):
        root = ET.fromstring(SVG.format('<g data-textbox="100 180 80 20" data-role="body"><text x="100" y="200" font-size="16">这段文字超过了容器容量，仍然必须完整保留。</text></g>'))
        before = ET.tostring(root)
        reflow_declared_paragraphs(root)
        self.assertEqual(ET.tostring(root), before)

    def test_preview_and_native_export_share_nested_text_content(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'slide.svg'
            path.write_text(SVG.format('<g data-textbox="100 180 300 150" data-role="body"><text x="100" y="200" font-size="16"><tspan x="100" dy="0">正文<tspan fill="#0000FF">完整</tspan>保留</tspan><tspan x="100" dy="28">数值<tspan font-weight="bold">12.345</tspan>及单位</tspan></text></g>'), encoding='utf-8')
            preview = prepare_svg_file_content_for_render(path)
            path.write_text(preview, encoding='utf-8')
            xml = ET.fromstring(convert_svg_to_slide_shapes(path)[0])
            self.assertEqual(signature(''.join(ET.fromstring(preview).itertext())), signature(''.join(e.text or '' for e in xml.iter(A+'t'))))


if __name__ == '__main__':
    unittest.main()
