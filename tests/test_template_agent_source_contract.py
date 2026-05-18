from __future__ import annotations

import json
from pathlib import Path

from backend.config import settings
from backend.generator.template_agent import _seed_agent_template_sources
from backend.generator.template_importer import _review_manifest_slides, _write_json


def _write_svg(path: Path, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <image href="../assets/logo.png" x="20" y="20" width="120" height="48"/>
  <text x="100" y="120">{label}</text>
</svg>""",
        encoding="utf-8",
    )


def test_review_manifest_slides_reads_legacy_work_sidecar(workspace_tmp: Path) -> None:
    import_id = "sidecar_legacy"
    work_dir = settings.workspaces_dir / "template_imports" / import_id / "work"
    _write_json(
        work_dir / "review_manifest.json",
        {
            "schema_version": 1,
            "slides": [
                {"index": 2, "elements": [{"element_id": "s02_text_001", "text": "Title"}]},
            ],
        },
    )

    slides = _review_manifest_slides(import_id)

    assert 2 in slides
    assert slides[2]["elements"][0]["element_id"] == "s02_text_001"


def test_agent_template_seed_uses_source_copies_and_archives_stale_outputs(workspace_tmp: Path) -> None:
    import_id = "agent_seed"
    import_dir = settings.workspaces_dir / "template_imports" / import_id
    _write_json(
        import_dir / "review.json",
        {
            "import_id": import_id,
            "draft": {
                "page_selections": {
                    "cover": 1,
                    "toc": 2,
                    "chapter": 3,
                    "content": 4,
                    "ending": 5,
                },
            },
        },
    )
    for index, label in enumerate(("Cover", "Toc", "Chapter", "Content", "Ending"), start=1):
        _write_svg(import_dir / "work" / "svg" / f"slide_{index:02d}.svg", label)
    logo = import_dir / "work" / "assets" / "logo.png"
    logo.parent.mkdir(parents=True, exist_ok=True)
    logo.write_bytes(b"logo")

    _seed_agent_template_sources(import_dir)

    cover = import_dir / "agent_template" / "01_cover.svg"
    assert cover.exists()
    assert 'href="assets/logo.png"' in cover.read_text(encoding="utf-8")
    assert (import_dir / "agent_template" / "source_reference" / "cover_slide_01.svg").exists()
    assert (import_dir / "agent_template" / "assets" / "logo.png").read_bytes() == b"logo"
    source_map = json.loads((import_dir / "agent_template" / "source_map.json").read_text(encoding="utf-8"))
    assert source_map["mode"] == "source_derived"
    assert [page["page_type"] for page in source_map["pages"]] == [
        "cover",
        "toc",
        "chapter",
        "content",
        "ending",
    ]

    stale_output = "<svg>custom edit without source marker</svg>"
    cover.write_text(stale_output, encoding="utf-8")
    _seed_agent_template_sources(import_dir)

    seeded = cover.read_text(encoding="utf-8")
    assert stale_output not in seeded
    assert "agent-source" in seeded
    archived = list((import_dir / "agent_template" / "archive").glob("*/cover_01_cover.svg"))
    assert archived
    assert archived[0].read_text(encoding="utf-8") == stale_output
