"""Preview and finalization typeset declared content without inferred repairs."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from backend.generator.svg_finalize.finalize import finalize_project
from backend.generator.svg_finalize.render_ready import prepare_svg_file_content_for_render


NS = '{http://www.w3.org/2000/svg}'
SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">{}</svg>'


class InitialRenderingTests(unittest.TestCase):
    def exercise(self, body):
        with TemporaryDirectory() as directory:
            project = Path(directory)
            source = project / 'svg_output' / 'slide_001.svg'
            source.parent.mkdir()
            content = SVG.format(body)
            source.write_text(content, encoding='utf8')
            preview = prepare_svg_file_content_for_render(source)
            stats = finalize_project(project)
            final = (project / 'svg_final' / source.name).read_text(encoding='utf8')
            self.assertEqual(source.read_text(encoding='utf8'), content)
            self.assertTrue(all(stats[name] == 0 for name in (
                'svgs_repaired', 'texts_merged', 'texts_reflowed',
                'text_integrity_restored', 'filters_removed')))
            return ET.fromstring(preview), ET.fromstring(final)

    def test_coincident_sibling_labels_remain_independent(self):
        roots = self.exercise('<text x="100" y="200" fill="#FF0000">First label</text>'
                              '<text x="100" y="200" fill="#0000FF">Second label</text>')
        for root in roots:
            labels = list(root.iter(NS+'text'))
            self.assertEqual([''.join(e.itertext()) for e in labels], ['First label', 'Second label'])
            self.assertEqual([(e.get('x'), e.get('y')) for e in labels], [('100','200'),('100','200')])

    def test_header_text_and_positioned_circle_label_are_preserved(self):
        roots = self.exercise('<text x="40" y="50">SECTION 03</text>'
                              '<circle cx="80" cy="200" r="10"/>'
                              '<text x="100" y="200" font-size="20">Positioned label</text>')
        for root in roots:
            labels = list(root.iter(NS+'text'))
            self.assertEqual(''.join(labels[0].itertext()), 'SECTION 03')
            self.assertEqual(labels[1].get('y'), '200')

    def test_filter_is_preserved_for_native_compatibility_diagnostics(self):
        roots = self.exercise('<defs><filter id="blur"><feGaussianBlur stdDeviation="3"/></filter></defs>'
                              '<rect x="100" y="180" width="100" height="50" filter="url(#blur)"/>')
        for root in roots:
            self.assertEqual(next(root.iter(NS+'rect')).get('filter'), 'url(#blur)')
            self.assertEqual(len(list(root.iter(NS+'filter'))), 1)

    def test_invalid_xml_is_reported_without_recovery_or_temporary_file_leaks(self):
        with TemporaryDirectory() as directory:
            project = Path(directory)
            source = project / 'svg_output' / 'slide_001.svg'
            source.parent.mkdir()
            malformed = SVG.format('<text x="100" y="200">A & B</text>')
            source.write_text(malformed, encoding='utf8')
            with self.assertRaises(ET.ParseError):
                prepare_svg_file_content_for_render(source)
            self.assertEqual(list(source.parent.glob('.__render_*')), [])
            with self.assertRaises(ET.ParseError):
                finalize_project(project)
            self.assertEqual(source.read_text(encoding='utf8'), malformed)

    def test_text_loss_is_reported_without_checkpoint_restoration(self):
        with TemporaryDirectory() as directory:
            project = Path(directory)
            source = project / 'svg_output' / 'slide_001.svg'
            source.parent.mkdir()
            content = SVG.format('<text x="100" y="200">Complete source copy</text>')
            source.write_text(content, encoding='utf8')
            def lose_copy(path):
                path.write_text(SVG.format(''), encoding='utf8')
                return 1
            with patch('backend.generator.svg_finalize.finalize.flatten_text_in_svg', side_effect=lose_copy):
                with self.assertRaisesRegex(ValueError, 'text changed during finalization'):
                    finalize_project(project)
            self.assertEqual(source.read_text(encoding='utf8'), content)


if __name__ == '__main__':
    unittest.main()
