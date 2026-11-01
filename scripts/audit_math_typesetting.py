"""Audit saved initial formula inputs without editing workspaces or calling a model."""

import argparse
import json
from pathlib import Path
import time
import xml.etree.ElementTree as ET

from backend.generator.svg_finalize.render_math import _math_path, math_expression


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workspaces', type=Path)
    parser.add_argument('--output', type=Path, default=Path('.test-runtime/math-compatibility/workspaces.json'))
    args = parser.parse_args()
    expressions = {}
    malformed = []
    for path in args.workspaces.glob('*/svg_initial/*.svg'):
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            malformed.append(str(path))
            continue
        for element in root.iter():
            expression = math_expression(element)
            if expression:
                expressions.setdefault(expression, []).append(str(path))
    start = time.perf_counter()
    failures = []
    for expression, sources in expressions.items():
        try:
            _math_path(expression, 28)
        except Exception as exc:
            failures.append({'expression': expression, 'sources': sources, 'error': str(exc)})
    report = {'unique_expressions': len(expressions), 'passed': len(expressions)-len(failures),
              'failed': len(failures), 'seconds': round(time.perf_counter()-start, 2),
              'malformed_svg_files': malformed, 'failures': failures}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({key: report[key] for key in ('unique_expressions', 'passed', 'failed', 'seconds')}, ensure_ascii=False))
    for failure in failures[:15]:
        print(failure['expression'][:150], ':', failure['error'][:150])


if __name__ == '__main__':
    main()
