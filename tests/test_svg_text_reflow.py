from __future__ import annotations

from pathlib import Path

from backend.generator.svg_finalize.svg_text_reflow import reflow_text_in_svg


def test_reflow_preserves_manual_wrapped_card_lines(workspace_tmp: Path) -> None:
    svg_path = workspace_tmp / "wrapped_card.svg"
    svg_path.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <rect x="854" y="372" width="386" height="240" fill="#F8F9FA"/>
  <text x="876" y="444" font-size="13">梯度补偿洞察能否应用于</text>
  <text x="876" y="464" font-size="13">检测/分割中的其他不平衡</text>
  <text x="876" y="484" font-size="13">辅助任务？Poly-QGV 的核心</text>
</svg>""",
        encoding="utf-8",
    )

    changed = reflow_text_in_svg(svg_path)
    content = svg_path.read_text(encoding="utf-8")

    assert changed == 0
    assert content.count("<text") == 3
    assert "梯度补偿洞察能否应用于 检测/分割中的其他不平衡" not in content
