from __future__ import annotations

from pathlib import Path

from backend.generator.svg_critic import check_svg
from backend.generator.svg_finalize.repair_svg import repair_svg_file


def test_critic_rejects_html_span_inside_svg_text() -> None:
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <text x="76" y="180" font-size="18">Pixel interpolation<span fill="#64748B"> - MAE 0.3598</span></text>
</svg>"""

    report = check_svg(svg)

    assert not report.passed
    assert any(v.rule == "html_span_in_svg_text" for v in report.violations)


def test_critic_rejects_nested_text_elements() -> None:
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <text x="76" y="180" font-size="18">Pixel interpolation<text x="76" y="210">MAE 0.3598</text></text>
</svg>"""

    report = check_svg(svg)

    assert not report.passed
    assert any(v.rule == "nested_text" for v in report.violations)


def test_critic_rejects_later_shape_covering_text() -> None:
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <rect x="80" y="120" width="620" height="140" fill="#ffffff"/>
  <text x="120" y="190" font-size="24">NYU Shanghai / NYU / Tsinghua University / EPFL</text>
  <rect x="440" y="152" width="210" height="70" rx="16" fill="#eaf2ff"/>
</svg>"""

    report = check_svg(svg)

    assert not report.passed
    assert any(v.rule == "shape_covers_text" for v in report.violations)


def test_critic_allows_shape_background_before_label() -> None:
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <rect x="440" y="152" width="210" height="70" rx="16" fill="#eaf2ff"/>
  <text x="470" y="195" font-size="24">Image</text>
</svg>"""

    report = check_svg(svg)

    assert all(v.rule != "shape_covers_text" for v in report.violations)


def test_critic_rejects_empty_bullet_marker() -> None:
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <circle cx="55" cy="200" r="4" fill="#2B6CB0"/>
  <text x="80" y="260" font-size="16">This text belongs to another row.</text>
</svg>"""

    report = check_svg(svg)

    assert not report.passed
    assert any(v.rule == "empty_bullet" for v in report.violations)


def test_critic_rejects_text_overflow_inside_card() -> None:
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <rect x="854" y="372" width="386" height="240" fill="#F8F9FA" stroke="#E2E8F0"/>
  <text x="876" y="444" font-size="13">梯度补偿洞察能否应用于 检测/分割中的其他不平衡 辅助任务？Poly-QGV 的核心 思想——对硬负样本梯度补偿 ——具有通用适用性。</text>
</svg>"""

    report = check_svg(svg)

    assert not report.passed
    assert any(v.rule == "text_overflow_in_container" for v in report.violations)


def test_repair_converts_html_span_inside_svg_text(workspace_tmp: Path) -> None:
    svg_path = workspace_tmp / "span.svg"
    svg_path.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <text x="76" y="180" font-size="18">Pixel interpolation<span fill="#64748B"> - MAE 0.3598</span></text>
</svg>""",
        encoding="utf-8",
    )

    changed = repair_svg_file(svg_path)
    content = svg_path.read_text(encoding="utf-8")

    assert changed == 1
    assert "<span" not in content
    assert "</span>" not in content
    assert '<tspan fill="#64748B"> - MAE 0.3598</tspan>' in content
