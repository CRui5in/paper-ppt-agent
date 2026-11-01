"""Lossless nested text and container-driven paragraph layout regressions."""

from pathlib import Path
from tempfile import TemporaryDirectory
import re
import unittest
import xml.etree.ElementTree as ET

from backend.generator.svg_text_layout import reflow_declared_paragraphs, measure_text, layout_text_groups, _bounds
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


class ParagraphLayoutTests(unittest.TestCase):
    def test_header_label_and_title_have_distinct_rows(self):
        root = ET.fromstring(SVG.format('<g data-role="header"><text x="40" y="40" font-size="12">Chapter</text><text x="40" y="48" font-size="28">Title</text><rect x="40" y="55" width="100" height="2"/></g>'))
        layout_text_groups(root)
        texts = list(root.iter(NS+'text'))
        self.assertGreater(_bounds(texts[1])[1], _bounds(texts[0])[3])
        self.assertGreater(float(next(root.iter(NS+'rect')).get('y')), _bounds(texts[1])[3])

    def test_centered_label_lines_fit_their_shape(self):
        root = ET.fromstring(SVG.format('<g><rect x="100" y="180" width="80" height="36"/><text x="140" y="210" text-anchor="middle" font-size="12">First</text><text x="140" y="226" text-anchor="middle" font-size="12">Second</text></g>'))
        layout_text_groups(root)
        for text in root.iter(NS+'text'):
            box = _bounds(text)
            self.assertGreaterEqual(box[1], 180)
            self.assertLessEqual(box[3], 216)

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
