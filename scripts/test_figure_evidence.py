"""Keep source captions, multi-panel images, and explicit slide assignments together."""

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import fitz
from PIL import Image

from backend.orchestrator.svg_executor import _explicit_figure_owners, _prepare_parallel_page_inputs
from backend.parser.pdf_parser import PDFParser


class CaptionIdentityTests(unittest.TestCase):
    def test_caption_wins_over_body_reference_and_can_start_inside_text_block(self):
        def line(text, y, bold=False):
            return {'bbox': (40, y, 280, y + 12),
                    'spans': [{'text': text, 'flags': 16 if bold else 0}]}
        blocks = [
            {'type': 0, 'lines': [line('Figure 7: Observed examples.', 250, True)]},
            {'type': 0, 'lines': [line('Figure 7 compares observations in detail.', 340)]},
            {'type': 0, 'lines': [line('The preceding paragraph ends here.', 390),
                                 line('Table 3: Recorded measurements.', 410, True)]},
        ]
        page = SimpleNamespace(get_text=lambda _: {'blocks': blocks})
        captions = PDFParser()._collect_captions(page)
        self.assertEqual(captions['figure7'][0], 'Figure 7: Observed examples.')
        self.assertEqual(captions['figure7'][1].y0, 250)
        self.assertEqual(captions['table3'][0], 'Table 3: Recorded measurements.')
        self.assertEqual(captions['table3'][1].y0, 410)

    def test_table_caption_matches_kind_and_column(self):
        captions = {
            'figure7': ('Figure 7: Right column.', fitz.Rect(320, 220, 560, 240)),
            'table3': ('Table 3: Left column.', fitz.Rect(40, 180, 280, 200)),
        }
        parser = PDFParser()
        self.assertEqual(parser._nearest_caption(fitz.Rect(40, 210, 280, 300), captions, 'table'),
                         'Table 3: Left column.')
        self.assertEqual(parser._nearest_caption(fitz.Rect(320, 250, 560, 340), captions, 'table'), '')

    def test_small_displayed_panels_form_one_complete_caption_crop(self):
        parser = PDFParser()
        with fitz.open() as doc, TemporaryDirectory() as directory:
            page = doc.new_page(width=600, height=800)
            colors = ['red', 'green', 'blue', 'yellow', 'cyan', 'magenta']
            for index, color in enumerate(colors):
                png = BytesIO()
                Image.new('RGB', (160, 80), color).save(png, format='PNG')
                x = 320 + (index % 3) * 81
                y = 180 + (index // 3) * 42
                page.insert_image(fitz.Rect(x, y, x + 80, y + 40), stream=png.getvalue())
            page.insert_text((320, 280), 'Figure 7: Observed examples.', fontsize=10, fontname='hebo')
            # A nearby column and margin numbering are not part of the figure.
            page.insert_textbox(fitz.Rect(40, 150, 280, 310), 'Detailed body explanation. ' * 30, fontsize=10)
            for number in range(4):
                page.insert_text((586, 190 + number * 22), str(501 + number), fontsize=7)
            captions = parser._collect_captions(page)
            with patch.object(parser, '_iter_page_tables', return_value=[]):
                figures = parser._extract_page_figures(doc, page, 0, Path(directory), captions, [0])
            source = [figure for figure in figures if figure.caption.startswith('Figure 7:')]
            self.assertEqual(len(source), 1)
            self.assertEqual(source[0].extraction_method, 'caption_region')
            crop = fitz.Rect(source[0].bbox)
            self.assertLessEqual(crop.x0, 320)
            self.assertGreaterEqual(crop.x1, 562)
            self.assertLess(crop.x1, 586)
            self.assertLessEqual(crop.y0, 180)
            self.assertGreaterEqual(crop.y1, 262)
            with Image.open(source[0].path) as image:
                for index, color in enumerate(colors):
                    x = 360 + (index % 3) * 81
                    y = 200 + (index // 3) * 42
                    point = (round((x - crop.x0) * image.width / crop.width),
                             round((y - crop.y0) * image.height / crop.height))
                    self.assertEqual(image.getpixel(point), Image.new('RGB', (1, 1), color).getpixel((0, 0)))


class FigureAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.figure = {'path': '/assets/source_plot.png', 'caption': 'Figure 7: Measured comparison.',
                       'page_number': 2, 'quality_score': .9, 'natural_width': 800, 'natural_height': 400}
        self.inventory = [self.figure]

    def prepare(self, pages, spec=''):
        claimed = set()
        owners = _explicit_figure_owners(pages, self.inventory)
        return [_prepare_parallel_page_inputs(page_num=index, total_pages=len(pages), page_content=page,
            design_spec=spec, figure_inventory=self.inventory, claimed_paper_figures=claimed, figure_owners=owners)
            for index, page in enumerate(pages, 1)]

    def test_explicit_manuscript_placement_precedes_overview_reference(self):
        first, second = self.prepare(['# Overview\nFigure 7 is discussed later.',
                                      '# Measured comparison\n[[FIG:source_plot]]'])
        self.assertEqual(first['used_figures'], [])
        self.assertNotIn('/assets/source_plot.png', first['rewritten_content'])
        self.assertEqual(second['used_figures'], [self.figure])

    def test_explicit_manuscript_placement_precedes_design_spec_assignment(self):
        first, second = self.prepare(['# Overview', '# Results\n[[FIG:source_plot]]'],
                                    '## VIII. Image Resource List\n| source_plot.png | Slide 1 |')
        self.assertEqual(first['used_figures'], [])
        self.assertEqual(second['used_figures'], [self.figure])

    def test_same_caption_crops_share_one_owner_and_best_crop(self):
        alternate = dict(self.figure, path='/assets/full_plot.png', quality_score=1)
        self.inventory.append(alternate)
        pages = ['# Overview\nFigure 7', '# Results\n[[FIG:source_plot]]', '# Later\n[[FIG:full_plot]]']
        prepared = self.prepare(pages)
        self.assertEqual([len(page['used_figures']) for page in prepared], [0, 1, 0])
        self.assertEqual(prepared[1]['used_figures'], [alternate])

    def test_structural_token_does_not_reserve_content_figure(self):
        first, second = self.prepare(['<!-- page_type: cover -->\n# Title\n[[FIG:source_plot]]',
                                      '# Results\n[[FIG:source_plot]]'])
        self.assertEqual(first['used_figures'], [])
        self.assertEqual(second['used_figures'], [self.figure])

    def test_reference_only_decks_keep_single_use_policy(self):
        prepared = self.prepare(['# Results\nFigure 7', '# More results\nFigure 7'])
        self.assertEqual([len(page['used_figures']) for page in prepared], [1, 0])


if __name__ == '__main__':
    unittest.main()
