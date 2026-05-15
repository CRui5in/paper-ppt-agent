from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

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


def test_critic_rejects_inline_emphasis_drift() -> None:
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <text x="120" y="260" font-size="18" fill="#0f172a">将乘客按时间排序的上车序列视为</text>
  <text x="760" y="260" font-size="18" fill="#3b82f6">“句子”</text>
</svg>"""

    report = check_svg(svg)

    assert not report.passed
    assert any(v.rule == "inline_emphasis_drift" for v in report.violations)


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


def test_repair_malformed_quoted_font_family_stack(workspace_tmp: Path) -> None:
    svg_path = workspace_tmp / "font_stack.svg"
    svg_path.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <text x="60" y="55" font-family="SF Pro Display", "PingFang SC", "Segoe UI", Roboto, sans-serif" font-size="14">08</text>
  <text x="60" y="100" font-family="SF Pro Display", "PingFang SC", "Segoe UI", Roboto, sans-serif" font-size="28">标题</text>
</svg>""",
        encoding="utf-8",
    )

    changed = repair_svg_file(svg_path)
    content = svg_path.read_text(encoding="utf-8")

    assert changed == 1
    assert 'font-family="SF Pro Display, PingFang SC, Segoe UI, Roboto, sans-serif"' in content
    assert "标题" in content


def test_repair_removes_animation_features(workspace_tmp: Path) -> None:
    svg_path = workspace_tmp / "animated.svg"
    svg_path.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <style>@keyframes pulse { 0% { opacity: .2; } 100% { opacity: 1; } }</style>
  <circle cx="120" cy="120" r="20" fill="#2563EB" style="animation: pulse 2s infinite;">
    <animate attributeName="r" from="10" to="20" dur="1s"/>
  </circle>
  <text x="80" y="200">静态内容</text>
</svg>""",
        encoding="utf-8",
    )

    changed = repair_svg_file(svg_path)
    content = svg_path.read_text(encoding="utf-8")

    assert changed == 1
    assert "<animate" not in content
    assert "@keyframes" not in content
    assert "animation:" not in content
    assert "静态内容" in content


def test_repair_removes_top_left_admin_label_only(workspace_tmp: Path) -> None:
    svg_path = workspace_tmp / "header_label.svg"
    svg_path.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <text x="60" y="55" font-size="14">SECTION 03</text>
  <text x="60" y="100" font-size="28">真实页面标题</text>
  <text x="640" y="680" text-anchor="middle" font-size="14">8 / 19</text>
</svg>""",
        encoding="utf-8",
    )

    changed = repair_svg_file(svg_path)
    content = svg_path.read_text(encoding="utf-8")

    assert changed == 1
    assert "SECTION 03" not in content
    assert "真实页面标题" in content
    assert "8 / 19" in content


def test_repair_sanitizes_double_hyphen_comments(workspace_tmp: Path) -> None:
    svg_path = workspace_tmp / "comment.svg"
    svg_path.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <!-- ---- Step 1: Foundation ---- -->
  <text x="80" y="200">ok</text>
</svg>""",
        encoding="utf-8",
    )

    changed = repair_svg_file(svg_path)
    content = svg_path.read_text(encoding="utf-8")

    assert changed == 1
    assert "----" not in content
    assert "Step 1: Foundation" in content
    ET.fromstring(content)


def test_repair_keeps_valid_tspan_closing_when_escaping_ampersand(
    workspace_tmp: Path,
) -> None:
    svg_path = workspace_tmp / "amp_tspan.svg"
    svg_path.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <text x="0" y="20">
    <tspan x="0" dy="0">LayerNorm 包裹。</tspan>
  </text>
  <g transform="translate(0, 120)">
    <text x="24" y="50">
      <tspan x="24" dy="0">Add & LayerNorm</tspan>
    </text>
  </g>
</svg>""",
        encoding="utf-8",
    )

    changed = repair_svg_file(svg_path)
    content = svg_path.read_text(encoding="utf-8")

    assert changed == 1
    assert "Add &amp; LayerNorm" in content
    assert "</tspan>" in content
    assert '<g transform="translate(0, 120)">' in content
    ET.fromstring(content)


def test_repair_line_duplicate_y1_as_y2(workspace_tmp: Path) -> None:
    svg_path = workspace_tmp / "line_endpoint.svg"
    svg_path.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <line x1="680" y1="180" x2="860" y1="210" stroke="#D97754"/>
</svg>""",
        encoding="utf-8",
    )

    changed = repair_svg_file(svg_path)
    content = svg_path.read_text(encoding="utf-8")

    assert changed == 1
    assert 'x2="860" y2="210"' in content
    ET.fromstring(content)
