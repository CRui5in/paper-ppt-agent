"""Source-table identity and cell fidelity before drawing."""

import unittest

from backend.orchestrator.source_records import manuscript_records, page_records, records_context


class SourceRecordTests(unittest.TestCase):
    def test_records_keep_exact_labels_values_units_and_page_scope(self):
        first = '<!-- page_type: content -->\n# Trials\n| Group | Value (%) |\n|:---|---:|\n| A | **0.250** |\n| B | −1.5 ± 0.2 |'
        second = '<!-- page_type: content -->\n# Follow-up\n| Group | Value (%) |\n|---|---|\n| A | 8.75 |'
        pages = manuscript_records(first+'\n\n---\n\n'+second)
        table = pages[0]['tables'][0]
        self.assertEqual(table['columns'], ['Group', 'Value (%)'])
        self.assertEqual(table['rows'][1], {'id':'p1.t1.r2', 'cells':['B','−1.5 ± 0.2']})
        self.assertEqual(pages[1]['tables'][0]['rows'][0]['id'], 'p2.t1.r1')
        self.assertIn('**0.250**', records_context(pages))

    def test_qualitative_trends_do_not_create_observation_rows(self):
        page = page_records('# Trends\nDemand rose during 2010–2015, peaked in 2017, and later declined.\nThe latest reading was about 80 units.', 3)
        self.assertEqual(page['tables'], [])
        self.assertEqual(records_context([page]), '')

    def test_escaped_code_and_mathematical_pipes_stay_inside_cells(self):
        page = page_records('# Symbols\n| Label | Definition |\n|---|---|\n| A\\|B | $|x|$ |\n| C | `left|right` |', 1)
        rows = page['tables'][0]['rows']
        self.assertEqual(rows[0]['cells'], ['A\\|B','$|x|$'])
        self.assertEqual(rows[1]['cells'], ['C','`left|right`'])

    def test_table_looking_code_is_not_source_data(self):
        content = '# Example\n```text\n|Name|Value|\n|---|---|\n|fiction|999|\n```\n\nName | Value\n--- | ---\nmeasured | 1.25'
        tables = page_records(content, 4)['tables']
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]['rows'][0]['cells'], ['measured','1.25'])


if __name__ == '__main__':
    unittest.main()
