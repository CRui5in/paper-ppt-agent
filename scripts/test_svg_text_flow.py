"""Initial text-flow layout: measured placement, styles, capacity, and native export."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import xml.etree.ElementTree as ET

from backend.generator.svg_text_flow import layout_text_flows, text_flow_issues
from backend.generator.svg_text_layout import _bounds
from backend.generator.svg_finalize.render_ready import prepare_svg_file_content_for_render
from backend.generator.svg_to_pptx.converter import convert_svg_to_slide_shapes
from backend.generator.svg_critic import check_svg


NS = '{http://www.w3.org/2000/svg}'
A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">{}</svg>'


class TextFlowTests(unittest.TestCase):
    def flow(self, content, frame='100 180 240 240', attrs=''):
        return ET.fromstring(SVG.format(f'<g data-layout="text-flow" data-flow-box="{frame}" data-gap="12" {attrs}>{content}</g>'))

    def test_heading_and_wrapped_paragraph_have_measured_separation(self):
        root = self.flow('<text font-size="28">一个标题</text><text font-size="20">正文在这个区域内按实测字体宽度换行，并保留下一段所需的间距。</text>')
        layout_text_flows(root)
        heading, body = list(root.iter(NS+'text'))
        self.assertGreaterEqual(_bounds(heading)[1], 180)
        self.assertGreater(_bounds(body)[1] - _bounds(heading)[3], 12)
        self.assertLessEqual(_bounds(body)[2], 340.1)
        self.assertFalse(text_flow_issues(root))
        before = ET.tostring(root)
        layout_text_flows(root)
        self.assertEqual(ET.tostring(root), before)

    def test_metric_label_and_value_keep_their_sizes_and_gap(self):
        root = self.flow('<text font-size="14">Label</text><text font-size="32" font-weight="bold">0.1234</text>', '100 180 200 100')
        layout_text_flows(root)
        label, value = list(root.iter(NS+'text'))
        self.assertGreater(_bounds(value)[1], _bounds(label)[3])
        self.assertEqual([e.get('font-size') for e in (label,value)], ['14','32'])

    def test_overfull_frame_keeps_complete_styled_copy_and_reports_capacity(self):
        root = self.flow('<text font-size="20">这段正文包含<tspan fill="#FF0000">12.345 单位</tspan>和完整尾句，需要多行显示。</text><text font-size="20">下一段。</text>', '100 180 120 40')
        before = ''.join(root.itertext())
        layout_text_flows(root)
        self.assertEqual(''.join(root.itertext()), before)
        self.assertEqual(next(root.iter(NS+'g')).get('data-flow-box'), '100 180 120 40')
        self.assertGreater(text_flow_issues(root)[0]['required_height'], 40)
        self.assertIn('paragraph_capacity', {v.rule for v in check_svg(ET.tostring(root,encoding='unicode')).violations})
        self.assertTrue(any(e.get('fill') == '#FF0000' for e in root.iter(NS+'tspan')))

    def test_center_and_end_alignment_remain_within_frame(self):
        for anchor in ('middle','end'):
            with self.subTest(anchor=anchor):
                root = self.flow(f'<text font-size="24" text-anchor="{anchor}">A title</text>')
                layout_text_flows(root)
                box = _bounds(next(root.iter(NS+'text')))
                self.assertGreaterEqual(box[0], 100)
                self.assertLessEqual(box[2], 340.1)

    def test_group_font_and_transform_are_retained(self):
        root = self.flow('<text>Inherited typography</text>', attrs='font-size="22" transform="translate(10 20)"')
        layout_text_flows(root)
        self.assertEqual(next(root.iter(NS+'g')).get('transform'),'translate(10 20)')
        self.assertTrue(any(e.get('font-size') == '22' for e in root.iter(NS+'tspan')))

    def test_unbreakable_identifiers_are_reported_without_truncation(self):
        root = self.flow('<text font-size="24">unbreakable_identifier</text>', '100 180 60 100')
        layout_text_flows(root)
        self.assertEqual(''.join(root.itertext()), 'unbreakable_identifier')
        self.assertGreater(text_flow_issues(root)[0]['required_width'], 60)

    def test_regular_svg_elements_keep_their_coordinates(self):
        root = ET.fromstring(SVG.format('<g><text x="50" y="75">Label</text></g>'))
        before=ET.tostring(root)
        self.assertEqual(layout_text_flows(root),0)
        self.assertEqual(ET.tostring(root),before)

    def test_unsupported_flow_children_are_reported(self):
        with self.assertRaisesRegex(ValueError, 'direct, untransformed text'):
            layout_text_flows(self.flow('<rect width="20" height="20"/>'))

    def test_preview_and_native_export_preserve_copy_and_layout(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'slide.svg'
            root=self.flow('<text font-size="28">标题</text><text font-size="20">Source <tspan font-weight="bold">12.345</tspan> units and explanation.</text>')
            before=''.join(root.itertext())
            path.write_text(ET.tostring(root,encoding='unicode'),encoding='utf8')
            prepared=prepare_svg_file_content_for_render(path)
            preview=ET.fromstring(prepared)
            self.assertEqual(''.join(''.join(preview.itertext()).split()), ''.join(before.split()))
            heading,body=list(preview.iter(NS+'text'))
            self.assertGreater(_bounds(body)[1],_bounds(heading)[3])
            path.write_text(prepared,encoding='utf8')
            xml=ET.fromstring(convert_svg_to_slide_shapes(path)[0])
            self.assertEqual(''.join(''.join(e.text or '' for e in xml.iter(A+'t')).split()), ''.join(before.split()))


if __name__ == '__main__':
    unittest.main()
