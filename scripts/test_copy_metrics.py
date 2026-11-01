"""Read-only first-generation measurements use the selected visible copy."""

import unittest

from backend.generator.svg_text_layout import measure_text
from backend.orchestrator.copy_metrics import copy_measurements, copy_metrics_context


class CopyMeasurementTests(unittest.TestCase):
    def test_a_brief_without_selected_copy_does_not_gain_paper_content(self):
        self.assertEqual(copy_metrics_context('- Design brief: A source figure with a caption.'), '')
        self.assertEqual(copy_metrics_context('- Content:\n- Speaker notes: Oral explanation.'), '')

    def test_inequalities_decimals_and_identifiers_remain_visible_copy(self):
        text = '0.625: area < 10 km² or > 100 km²; model__layer'
        result = copy_measurements('- Content: '+text)
        self.assertEqual(result['copy'][0]['text'], text)

    def test_only_visible_content_is_measured_with_its_exact_values(self):
        block = '- **Design brief**: Geometry planning text.\n- **Content**:\n  - Group **A**: 0.250 units\n  - 混合文字 label\n- **Speaker notes**: This long explanation belongs to the speaker.'
        result = copy_measurements(block)
        self.assertEqual([row['text'] for row in result['copy']], ['Group A: 0.250 units','混合文字 label'])
        self.assertEqual(result['copy'][0]['advance_px'], round(measure_text('Group A: 0.250 units', {'font-family':'Arial','font-size':'18'}),1))

    def test_table_widths_cover_the_longest_cell_and_its_header(self):
        block = '- Content:\n  | Label | Value |\n  |---|---|\n  | Very long label | 0.125 |\n  | B | 1234.50 |\n- Speaker notes: More detail'
        result = copy_measurements(block)
        self.assertEqual(result['copy'], [])
        self.assertEqual(result['tables'][0]['rows'], 2)
        widths = result['tables'][0]['columns']
        self.assertGreaterEqual(widths[0]['max_advance_px'], round(measure_text('Very long label', {'font-family':'Arial','font-size':'18'}),1))
        self.assertGreaterEqual(widths[1]['max_advance_px'], round(measure_text('1234.50', {'font-family':'Arial','font-size':'18'}),1))

    def test_measurements_scale_with_the_requested_reference_size(self):
        block = '- Content: 选择合适的布局与字号'
        small = copy_measurements(block, font_size=12)['copy'][0]['advance_px']
        large = copy_measurements(block, font_size=24)['copy'][0]['advance_px']
        self.assertAlmostEqual(large, small*2, delta=.2)


if __name__ == '__main__':
    unittest.main()
