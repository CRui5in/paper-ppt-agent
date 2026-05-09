from __future__ import annotations

from pathlib import Path

from backend.generator.svg_finalize.embed_icons import embed_icons_in_file


def test_embed_icons_serializes_plain_svg_shape_tags(tmp_path: Path) -> None:
    icons_dir = tmp_path / "icons"
    chunk_dir = icons_dir / "chunk"
    chunk_dir.mkdir(parents=True)
    (chunk_dir / "sample.svg").write_text(
        (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
            '<path fill="currentColor" d="M1 1h14v14H1z"/>'
            "</svg>"
        ),
        encoding="utf-8",
    )
    svg_path = tmp_path / "slide.svg"
    svg_path.write_text(
        (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '<use data-icon="chunk/sample" x="10" y="20" width="32" height="32" fill="#fff"/>'
            "</svg>"
        ),
        encoding="utf-8",
    )

    assert embed_icons_in_file(svg_path, icons_dir) == 1
    content = svg_path.read_text(encoding="utf-8")

    assert "<!-- icon: chunk/sample -->" in content
    assert "<path" in content
    assert "ns0:" not in content
    assert "data-icon" not in content
