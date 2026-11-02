"""Standard TeX constructions share one vector typesetting path."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import xml.etree.ElementTree as ET

from backend.generator.svg_finalize.render_math import _math_path, render_math_in_tree, math_expression
from backend.generator.svg_to_pptx.builder import create_pptx

NS = '{http://www.w3.org/2000/svg}'
EXPRESSIONS = {
    'norm': r'\lVert B_l\rVert_2 \leqslant 1',
    'single_token_fonts': r'M^{(t)}=\mathcal T_r\!\left(\mathcal T_c(M^{(t-1)})\right)',
    'fractions': r'\frac{1}{2}+\dfrac{a}{b}+\tfrac{c}{d}',
    'single_token_fraction': r'\frac12+\sqrt x',
    'matrix': r'\begin{pmatrix}a&b\\c&d\end{pmatrix}',
    'bmatrix': r'\begin{bmatrix}a&b\\c&d\end{bmatrix}',
    'array': r'\begin{array}{c|cc} & x & y\\a & 1 & 2\end{array}',
    'cases': r'f(x)=\begin{cases}x & x>0\\0 & x\le0\end{cases}',
    'aligned': r'\begin{aligned}a&=b+c\\d&=e\end{aligned}',
    'tag': r'E=mc^2\tag{1}',
    'tagged_align': r'\begin{align}x&=y\tag{A}\\y&=z\tag{B}\end{align}',
    'operator_limits': r'\operatorname*{arg\,min}_{x\in\mathbb R} f(x)',
    'accents_and_underbrace': r'\widehat{x}+\overline{y}+\underbrace{a+b}_{c}',
    'chinese_text': r'\text{面积}=a^2',
    'greek': r'\boldsymbol{\alpha}+\Gamma+\Omega',
    'large_operators': r'\sum_{i=1}^{n}x_i+\prod_{j=0}^{m}y_j+\int_0^1 f(x)\,\mathrm dx',
    'substack': r'\sum_{\substack{i>0\\j<0}}a_{ij}',
    'binomial': r'\binom{n}{k}=\frac{n!}{k!(n-k)!}',
    'nested_delimiters': r'\left\lVert\frac{A_i}{\sqrt{B_i}}\right\rVert_2',
    'vae_loss': r'\mathcal{L}=\lVert x-\hat{x}\rVert_1+\frac{\beta}{2}\sum_{j=1}^{J}\left[\mu_j^2+\sigma_j^2-\ln(\sigma_j^2)-1\right]',
    'overset_underset': r'\overset{a}{=}_b+\underset{x\to0}{\lim} f(x)',
    'spacing': r'a\,b\;c\!d\quad e',
    'cancel': r'\cancel{x}+\boxed{y}',
    'color': r'\color{red}{x}+\color{blue}{y}',
    'macro': r'\newcommand{\vect}[1]{\mathbf{#1}}\vect{x}',
}


class StandardTexTests(unittest.TestCase):
    def test_trailing_tex_bracket_is_distinct_from_cdata_terminator(self):
        expression = EXPRESSIONS['vae_loss']
        prefix = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720"><g data-math-box="80 180 1100 120" font-size="28">'
        valid = prefix + f'<desc data-math="latex"><![CDATA[\n{expression}\n]]></desc></g></svg>'
        root = ET.fromstring(valid)
        self.assertEqual(math_expression(root.find(NS+'g')).strip(), expression)
        self.assertEqual(render_math_in_tree(root), 1)
        self.assertIsNotNone(root.find(NS+'g').find(NS+'path'))
        broken = prefix + '<desc data-math="latex"><![CDATA[' + expression[:-1] + ']]></desc></g></svg>'
        root = ET.fromstring(broken)
        self.assertTrue(math_expression(root.find(NS+'g')).endswith(r'\right'))
        with self.assertRaisesRegex(ValueError, r'delimiter.*right'):
            render_math_in_tree(root)

    def test_cdata_source_preserves_alignment_and_reserved_xml_characters(self):
        expression = r'f(x)=\begin{cases}x & x<0\\x^2 & x\ge0\end{cases}'
        source = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">' \
                 '<g data-math-box="100 180 1000 300" font-size="32">' \
                 f'<desc data-math="latex"><![CDATA[{expression}]]></desc></g></svg>'
        root = ET.fromstring(source)
        group = root.find(NS+'g')
        self.assertEqual(math_expression(group), expression)
        self.assertEqual(render_math_in_tree(root), 1)
        self.assertEqual(group.get('data-latex'), expression)
        self.assertIsNotNone(group.find(NS+'path'))
        self.assertEqual(render_math_in_tree(root), 0)
        ET.fromstring(ET.tostring(root, encoding='unicode'))
        with TemporaryDirectory() as directory:
            path = Path(directory)/'cdata.svg'
            path.write_text(source, encoding='utf8')
            output = create_pptx([path], Path(directory)/'cdata.pptx')
            audit = json.loads(output.with_suffix('.export_audit.json').read_text(encoding='utf8'))
            self.assertEqual(audit['summary']['status'], 'passed')

    def test_array_rules_keep_engine_stroke_styles(self):
        from backend.generator.svg_finalize.mathjax import math_artwork
        shapes, _ = math_artwork(EXPRESSIONS['array'], 28)
        self.assertTrue(any(stroke != 'none' and width > 0 for _path, _fill, stroke, width, _extras in shapes))

    def test_standard_constructions_produce_finite_vector_bounds(self):
        for name, expression in EXPRESSIONS.items():
            with self.subTest(name=name):
                path, bounds = _math_path(expression, 28)
                self.assertTrue(path.startswith('M'))
                self.assertGreater(bounds[2], 0)
                self.assertGreater(bounds[3], 0)

    def test_macros_and_equation_labels_are_scoped_to_each_expression(self):
        _math_path(r'\newcommand{\localOnly}{x}\localOnly', 28)
        with self.assertRaisesRegex(ValueError, 'Undefined control sequence'):
            _math_path(r'\localOnly', 28)
        for expression in (r'a=b\tag{1}', r'c=d\tag{1}'):
            self.assertTrue(_math_path(expression, 28)[0])

    def test_invalid_input_stays_an_error_and_next_expression_still_renders(self):
        for expression in (r'\unknowncommand{x}', r'\frac{a}{', r'\begin{pmatrix}a&b'):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                _math_path(expression, 28)
        self.assertTrue(_math_path('x+y', 28)[0])

    def test_ams_and_chinese_equations_export_as_native_shapes(self):
        with TemporaryDirectory() as directory:
            files = []
            for index, expression in enumerate(EXPRESSIONS.values()):
                root = ET.Element(NS+'svg', {'viewBox': '0 0 1280 720'})
                group = ET.SubElement(root, NS+'g', {'data-latex': expression, 'data-math-box': '100 180 1000 300', 'font-size': '32'})
                render_math_in_tree(root)
                self.assertEqual(group.get('data-latex'), expression)
                self.assertFalse(list(root.iter(NS+'text')))
                self.assertFalse(list(root.iter(NS+'image')))
                file = Path(directory)/f'slide_{index+1:03d}.svg'
                file.write_text(ET.tostring(root, encoding='unicode'), encoding='utf8')
                files.append(file)
            output = create_pptx(files, Path(directory)/'math.pptx')
            report = json.loads(output.with_suffix('.conversion_report.json').read_text(encoding='utf8'))
            audit = json.loads(output.with_suffix('.export_audit.json').read_text(encoding='utf8'))
            self.assertTrue(all(slide['mode'] == 'native' for slide in report))
            self.assertEqual(audit['summary']['status'], 'passed')


if __name__ == '__main__':
    unittest.main()
