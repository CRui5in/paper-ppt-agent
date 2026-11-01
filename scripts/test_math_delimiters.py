"""Standard LaTeX delimiter aliases retain their source and vector rendering."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import xml.etree.ElementTree as ET

from backend.generator.svg_finalize.render_math import _math_path, _mathtext_expression, render_math_in_tree
from backend.generator.svg_finalize.render_ready import prepare_svg_file_content_for_render
from backend.generator.svg_to_pptx.builder import create_pptx

NS='{http://www.w3.org/2000/svg}'
EXPRESSION=r'\lVert B_l\rVert_2 \leqslant 1'


class MathDelimiterTests(unittest.TestCase):
    def test_norm_renders_identically_to_supported_delimiters(self):
        self.assertEqual(_math_path(EXPRESSION,28),_math_path(r'\Vert B_l\Vert_2 \leqslant 1',28))

    def test_absolute_values_and_stretchy_nested_delimiters(self):
        for source,expected in (
            (r'\lvert x_i\rvert',r'\vert x_i\vert'),
            (r'\left\lVert\frac{A_i}{B_i}\right\rVert_2',r'\left\Vert\frac{A_i}{B_i}\right\Vert_2'),
            (r'\lVert\lvert x\rvert + y\rVert',r'\Vert\vert x\vert + y\Vert'),
        ):
            with self.subTest(source=source):self.assertEqual(_math_path(source,28),_math_path(expected,28))

    def test_text_and_complete_control_sequences_are_preserved(self):
        for expression in (r'\text{literal \lVert and {nested} \rVert}',r'\text{escaped \{ \lVert \} }',
                           r'\\lVert',r'\lVertical',r'\rVertical',r'\leqslant',r'\unknowncommand{x}'):
            with self.subTest(expression=expression):self.assertEqual(_mathtext_expression(expression),expression)
        self.assertEqual(_mathtext_expression(r'\text{\lVert} + \lVert x\rVert'),r'\text{\lVert} + \Vert x\Vert')

    def test_rendering_retains_source_metadata_and_is_idempotent(self):
        root=ET.Element(NS+'svg',{'viewBox':'0 0 1280 720'})
        group=ET.SubElement(root,NS+'g',{'data-latex':EXPRESSION,'data-math-box':'80 180 600 100','font-size':'32'})
        self.assertEqual(render_math_in_tree(root),1)
        self.assertEqual(group.get('data-latex'),EXPRESSION)
        self.assertEqual(group.find(NS+'title').text,EXPRESSION)
        self.assertIsNotNone(group.find(NS+'path'))
        self.assertEqual(render_math_in_tree(root),0)

    def test_preview_and_native_export_keep_the_equation_as_vectors(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'equation.svg'
            root=ET.Element(NS+'svg',{'viewBox':'0 0 1280 720'})
            ET.SubElement(root,NS+'g',{'data-latex':EXPRESSION,'data-math-box':'80 180 600 100','font-size':'32'})
            path.write_text(ET.tostring(root,encoding='unicode'),encoding='utf8')
            preview=prepare_svg_file_content_for_render(path)
            self.assertIsNotNone(ET.fromstring(preview).find('.//'+NS+'path'))
            self.assertFalse(list(ET.fromstring(preview).iter(NS+'image')))
            output=create_pptx([path],Path(directory)/'equation.pptx')
            conversion=json.loads(output.with_suffix('.conversion_report.json').read_text(encoding='utf8'))
            audit=json.loads(output.with_suffix('.export_audit.json').read_text(encoding='utf8'))
            self.assertEqual(conversion[0]['mode'],'native')
            self.assertEqual(audit['summary']['status'],'passed')


if __name__=='__main__':unittest.main()
