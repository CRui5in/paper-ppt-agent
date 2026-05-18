from __future__ import annotations

import asyncio
import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from PIL import Image

from backend.config import settings
from backend.generator import template_manager
from backend.generator import template_importer as template_importer_module
from backend.generator.template_importer import (
    LLMAssetDecision,
    LLMElementAction,
    LLMPageSelections,
    LLMPlaceholderDecision,
    LLMTemplateImportPlan,
    assist_import_review,
    _call_template_import_llm,
    _apply_feedback_action_patches,
    _apply_annotation_layout_patches,
    _extract_asset_candidates,
    _merge_llm_plan,
    _review_path,
    _templateize_svg_with_plan,
    _templateize_svg,
    _text_candidate,
    _work_dir,
    _write_templateized_svgs,
    _write_json,
    initialize_import_task,
    inline_svg_asset_refs,
    save_import_review,
)
from backend.llm.types import LLMResponse


def _write_png(path: Path, color: tuple[int, int, int] = (255, 0, 0)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (24, 24), color)
    image.save(path)


def _write_svg(path: Path, slide_index: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <rect width="1280" height="720" fill="#fff"/>
  <image href="../assets/logo.png" x="30" y="24" width="90" height="36"/>
  <image href="../assets/photo.png" x="280" y="180" width="500" height="300"/>
  <text x="100" y="120">Original title {slide_index}</text>
  <text x="300" y="260">Body content</text>
</svg>""",
        encoding="utf-8",
    )


def test_extract_asset_candidates_recommends_repeated_corner_logo(workspace_tmp: Path):
    assets = workspace_tmp / "assets"
    svg_dir = workspace_tmp / "svg"
    _write_png(assets / "logo.png")
    _write_png(assets / "photo.png", (0, 0, 255))
    for index in range(1, 5):
        _write_svg(svg_dir / f"slide_{index:02d}.svg", index)

    candidates = _extract_asset_candidates(sorted(svg_dir.glob("slide_*.svg")))

    by_name = {item["file_name"]: item for item in candidates}
    assert by_name["logo.png"]["recommended_role"] == "logo"
    assert by_name["logo.png"]["position_stable"] is True
    assert by_name["logo.png"]["pages"] == [1, 2, 3, 4]
    assert by_name["photo.png"]["recommended_role"] == "ignore"


def test_asset_stability_requires_multiple_pages(workspace_tmp: Path):
    assets = workspace_tmp / "assets"
    svg_dir = workspace_tmp / "svg"
    _write_png(assets / "photo.png", (0, 0, 255))
    svg_dir.mkdir(parents=True, exist_ok=True)
    (svg_dir / "slide_01.svg").write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <image href="../assets/photo.png" x="280" y="180" width="500" height="300"/>
  <image href="../assets/photo.png" x="280" y="180" width="500" height="300"/>
</svg>""",
        encoding="utf-8",
    )

    candidates = _extract_asset_candidates([svg_dir / "slide_01.svg"])

    assert candidates[0]["pages"] == [1]
    assert candidates[0]["position_stable"] is False


def test_templateize_svg_removes_content_and_adds_placeholders(workspace_tmp: Path):
    assets = workspace_tmp / "assets"
    svg_dir = workspace_tmp / "svg"
    _write_png(assets / "logo.png")
    _write_png(assets / "photo.png", (0, 0, 255))
    _write_svg(svg_dir / "slide_01.svg", 1)

    output = _templateize_svg(
        svg_dir / "slide_01.svg",
        "content",
        1280,
        720,
        {"logo.png": "logo", "photo.png": "content_image"},
    )

    assert "Original title" not in output
    assert "Body content" not in output
    assert "assets/logo.png" in output
    assert "../assets/logo.png" not in output
    assert "photo.png" not in output
    assert "{{PAGE_TITLE}}" in output
    assert "{{CONTENT_AREA}}" in output
    assert 'id="content-area"' in output


def test_text_candidate_uses_powerpoint_tspan_transform_coordinates():
    element = ET.fromstring(
        """<text xmlns="http://www.w3.org/2000/svg"
  transform="matrix(1.3333334 0 0 1.3333334 0 759.36007)"
  font-size="24" fill="#1f4e95">
  <tspan x="100 124 148" y="-500">章节</tspan>
</text>"""
    )

    candidate = _text_candidate(element)

    assert 130 < candidate["x"] < 140
    assert 85 < candidate["y"] < 100
    assert candidate["font_size"] > 30
    assert candidate["width"] > 60


def test_templateize_plan_places_transformed_text_placeholder_at_original_position(workspace_tmp: Path):
    svg_path = workspace_tmp / "slide_01.svg"
    svg_path.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1350 759">
  <text transform="matrix(1.3333334 0 0 1.3333334 0 759.36007)" font-size="24" fill="#1f4e95" font-weight="700">
    <tspan x="180 205 230 255" y="-500">标题</tspan>
  </text>
</svg>""",
        encoding="utf-8",
    )

    output = _templateize_svg_with_plan(
        svg_path,
        1,
        "chapter",
        1350,
        759,
        {},
        set(),
        [
            {
                "page_type": "chapter",
                "element_id": "s01_text_001",
                "action": "replace_with_placeholder",
                "placeholder": "CHAPTER_TITLE",
                "x": 675,
                "y": 320,
                "font_size": 28,
                "text_anchor": "middle",
            }
        ],
    )

    assert "{{CHAPTER_TITLE}}" in output
    assert 'x="0.00"' not in output
    assert 'y="0.00"' not in output
    assert 'fill="#1f4e95"' in output
    assert 'x="675.00"' in output
    assert 'y="320.00"' in output
    assert 'font-size="28.00"' in output
    assert 'text-anchor="middle"' in output


def test_feedback_rule_patches_apply_visible_actions():
    review = {
        "draft": {
            "page_selections": {"chapter": 3, "content": 4},
            "element_actions": [],
            "preserve_texts": [],
        },
        "text_candidates": [{"text": "重复导航", "page_count": 6}],
        "slides": [
            {
                "index": 3,
                "elements": [
                    {"element_id": "s03_text_001", "type": "text", "text": "研究背景", "y": 260, "height": 30, "font_size": 24},
                    {"element_id": "s03_text_002", "type": "text", "text": "1", "y": 340, "height": 90, "font_size": 72},
                ],
            },
            {
                "index": 4,
                "elements": [
                    {"element_id": "s04_text_001", "type": "text", "text": "水华的定义", "y": 82, "height": 28, "font_size": 22},
                    {"element_id": "s04_text_002", "type": "text", "text": "重复导航", "y": 30, "height": 22, "font_size": 16},
                ],
            },
        ],
    }

    patches = _apply_feedback_action_patches(
        review,
        "你把占位内容全部放左上角了，然后章节页数字没替换为占位，然后内容页的页眉文字没去掉",
    )

    actions = {action["element_id"]: action for action in review["draft"]["element_actions"]}
    assert patches
    assert actions["s03_text_002"]["placeholder"] == "CHAPTER_NUM"
    assert actions["s04_text_001"]["action"] == "remove"
    assert "s04_text_002" not in actions


def test_annotation_layout_patches_apply_placeholder_geometry():
    review = {
        "slides": [
            {
                "index": 1,
                "elements": [
                    {
                        "element_id": "s01_text_001",
                        "type": "text",
                        "text": "Title",
                        "x": 120,
                        "y": 180,
                        "width": 420,
                        "height": 64,
                        "font_size": 52,
                    },
                    {
                        "element_id": "s01_text_002",
                        "type": "text",
                        "text": "Subtitle",
                        "x": 130,
                        "y": 250,
                        "width": 380,
                        "height": 42,
                        "font_size": 38,
                    },
                ],
            }
        ],
        "annotations": [
            {
                "annotation_id": "ann_center",
                "slide_index": 1,
                "bbox_norm": {"x": 0.2, "y": 0.22, "width": 0.45, "height": 0.18},
                "note": "居中，文字有点太大，不要重叠",
                "created_at": 1.0,
                "resolved": False,
            }
        ],
        "draft": {
            "page_selections": {"cover": 1},
            "element_actions": [
                {
                    "page_type": "cover",
                    "element_id": "s01_text_001",
                    "action": "replace_with_placeholder",
                    "placeholder": "TITLE",
                },
                {
                    "page_type": "cover",
                    "element_id": "s01_text_002",
                    "action": "replace_with_placeholder",
                    "placeholder": "SUBTITLE",
                },
            ],
        },
    }
    manifest = {"slideSize": {"width_px": 1280, "height_px": 720}}

    patches = _apply_annotation_layout_patches(review, manifest)

    actions = review["draft"]["element_actions"]
    assert patches
    assert actions[0]["text_anchor"] == "middle"
    assert actions[1]["text_anchor"] == "middle"
    assert actions[0]["x"] == actions[1]["x"]
    assert actions[0]["font_size"] < 52
    assert actions[1]["y"] > actions[0]["y"]


def test_save_review_merges_sparse_element_action_patch(workspace_tmp: Path):
    import_id = "sparsepatch123"
    import_root = settings.workspaces_dir / "template_imports" / import_id
    pptx_path = import_root / "source.pptx"
    pptx_path.parent.mkdir(parents=True, exist_ok=True)
    pptx_path.write_bytes(b"pptx")
    initialize_import_task(import_id, pptx_path)
    _write_json(
        _review_path(import_id),
        {
            "import_id": import_id,
            "template_id": "user_sparse_patch",
            "label": "Sparse",
            "status": "review_required",
            "draft": {
                "placeholder_hints": {"cover": {"TITLE": "Old title"}},
                "element_actions": [
                    {
                        "page_type": "cover",
                        "element_id": "s01_text_001",
                        "action": "replace_with_placeholder",
                        "placeholder": "TITLE",
                    },
                    {
                        "page_type": "cover",
                        "element_id": "s01_text_002",
                        "action": "replace_with_placeholder",
                        "placeholder": "SUBTITLE",
                    },
                    {
                        "page_type": "chapter",
                        "element_id": "s03_text_001",
                        "action": "replace_with_placeholder",
                        "placeholder": "CHAPTER_TITLE",
                    },
                ],
            },
            "assets": [],
            "slides": [],
        },
    )

    updated = save_import_review(
        import_id,
        {
            "placeholder_hints": {},
            "element_actions": [
                {
                    "page_type": "chapter",
                    "element_id": "s03_text_002",
                    "action": "remove",
                    "reason": "Remove marked number",
                }
            ],
        },
    )

    actions = updated["draft"]["element_actions"]
    assert len(actions) == 4
    assert any(action.get("placeholder") == "TITLE" for action in actions)
    assert any(action.get("element_id") == "s03_text_002" and action.get("action") == "remove" for action in actions)
    assert updated["draft"]["placeholder_hints"]["cover"]["TITLE"] == "Old title"


def test_llm_merge_preserves_existing_plan_when_feedback_plan_is_empty():
    review = {
        "draft": {
            "page_selections": {"cover": 1},
            "placeholder_hints": {"cover": {"TITLE": "Old title"}},
            "preserve_texts": ["old nav"],
            "element_actions": [
                {
                    "page_type": "cover",
                    "element_id": "s01_text_001",
                    "action": "replace_with_placeholder",
                    "placeholder": "TITLE",
                }
            ],
        },
        "slides": [{"index": 1, "elements": [{"element_id": "s01_text_001"}]}],
        "assets": [],
    }
    plan = LLMTemplateImportPlan(
        page_selections=LLMPageSelections(cover=1),
        element_actions=[],
        placeholder_decisions=[],
        preserve_texts=[],
        notes=["Only layout notes returned"],
    )

    _merge_llm_plan(review, {"slideSize": {"width_px": 1280, "height_px": 720}}, plan)

    assert review["draft"]["element_actions"][0]["placeholder"] == "TITLE"
    assert review["draft"]["placeholder_hints"]["cover"]["TITLE"] == "Old title"
    assert review["draft"]["preserve_texts"] == ["old nav"]


def test_templateized_write_falls_back_when_page_actions_have_no_placeholders(workspace_tmp: Path):
    svg_dir = workspace_tmp / "svg"
    out_dir = workspace_tmp / "out"
    svg_dir.mkdir(parents=True)
    (svg_dir / "slide_01.svg").write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <rect width="1280" height="720" fill="#fff"/>
  <text x="100" y="140" font-size="42">Thesis title</text>
  <text x="100" y="220" font-size="24">Defense subtitle</text>
</svg>""",
        encoding="utf-8",
    )
    context = {
        "review": {
            "slides": [
                {
                    "index": 1,
                    "elements": [
                        {"element_id": "s01_text_001", "type": "text", "text": "Thesis title"},
                        {"element_id": "s01_text_002", "type": "text", "text": "Defense subtitle"},
                    ],
                }
            ]
        },
        "selections": {"cover": 1, "content": 1, "chapter": 1, "ending": 1},
        "svg_clean_dir": svg_dir,
        "canvas_w": 1280,
        "canvas_h": 720,
        "asset_roles_by_file": {},
        "preserve_texts": set(),
        "placeholder_hints": {},
        "element_actions": [
            {
                "page_type": "chapter",
                "element_id": "s01_text_999",
                "action": "remove",
            }
        ],
    }

    _write_templateized_svgs(context, out_dir)

    cover_svg = (out_dir / "01_cover.svg").read_text(encoding="utf-8")
    assert "{{TITLE}}" in cover_svg
    assert "{{SUBTITLE}}" in cover_svg
    assert "Thesis title" not in cover_svg


def test_inline_preview_resolves_placeholder_image_refs(workspace_tmp: Path):
    template_dir = workspace_tmp / "template"
    _write_png(template_dir / "header_logo.png")
    svg_path = template_dir / "03_content.svg"
    svg_path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
        '<image href="../images/{{LOGO_HEADER}}" x="0" y="0" width="120" height="50"/>'
        "</svg>",
        encoding="utf-8",
    )

    output = inline_svg_asset_refs(svg_path, max_bytes=2000000)

    assert "data:image/png;base64," in output
    assert "{{LOGO_HEADER}}" not in output


def test_review_confirm_registers_user_template(client, workspace_tmp: Path, monkeypatch):
    templates_root = workspace_tmp / "assets" / "templates"
    layouts_root = templates_root / "layouts"
    monkeypatch.setattr(settings, "templates_dir", templates_root)
    monkeypatch.setattr(template_manager, "_TEMPLATES_ROOT", layouts_root)
    monkeypatch.setattr(template_manager, "_INDEX_PATH", layouts_root / "layouts_index.json")
    monkeypatch.setattr(template_manager, "_USER_INDEX_PATH", layouts_root / "user_templates.json")

    import_id = "review123"
    import_root = settings.workspaces_dir / "template_imports" / import_id
    pptx_path = import_root / "source.pptx"
    pptx_path.parent.mkdir(parents=True, exist_ok=True)
    pptx_path.write_bytes(b"pptx")
    initialize_import_task(import_id, pptx_path)

    work_dir = _work_dir(import_id)
    _write_png(work_dir / "assets" / "logo.png")
    _write_png(work_dir / "assets" / "photo.png", (0, 0, 255))
    _write_svg(work_dir / "svg" / "slide_01.svg", 1)
    _write_svg(work_dir / "svg" / "slide_02.svg", 2)

    manifest = {
        "source": {"name": "source.pptx"},
        "slideSize": {"width_px": 1280, "height_px": 720},
        "theme": {"colors": {"accent1": "#0055AA"}, "fonts": {"minorLatin": "Arial"}},
        "slides": [
            {"index": 1, "elements": [{"element_id": "s01_text_001", "type": "text", "text": "Persistent Header"}]},
            {"index": 2, "elements": []},
        ],
    }
    _write_json(work_dir / "manifest.json", manifest)
    review = {
        "import_id": import_id,
        "template_id": "user_source_test",
        "label": "Source",
        "status": "review_required",
        "export_mode": "test-svg",
        "slide_count": 2,
        "page_types": ["cover", "toc", "chapter", "content", "ending"],
        "asset_roles": ["logo", "background", "decoration", "content_image", "ignore"],
        "page_type_candidates": {"cover": [1], "content": [2]},
        "slides": [],
        "assets": [
            {
                "asset_id": "asset_logo",
                "file_name": "logo.png",
                "usage_count": 2,
                "pages": [1, 2],
                "position_stable": True,
                "recommended_role": "logo",
                "role": "logo",
                "name": "Logo",
                "occurrences": [],
            },
            {
                "asset_id": "asset_photo",
                "file_name": "photo.png",
                "usage_count": 2,
                "pages": [1, 2],
                "position_stable": True,
                "recommended_role": "content_image",
                "role": "content_image",
                "name": "Photo",
                "occurrences": [],
            },
        ],
        "annotations": [
            {
                "annotation_id": "ann_keep",
                "slide_index": 2,
                "bbox_norm": {"x": 0.2, "y": 0.2, "width": 0.2, "height": 0.1},
                "note": "old note",
                "created_at": 1.0,
                "resolved": False,
            }
        ],
        "draft": {
            "label": "Reviewed Template",
            "page_selections": {"cover": 1, "toc": None, "chapter": 1, "content": 2, "ending": 2},
            "assets": {
                "asset_logo": {"role": "logo", "name": "Company logo"},
                "asset_photo": {"role": "content_image", "name": "Photo"},
            },
            "element_actions": [
                {
                    "page_type": "content",
                    "element_id": "s02_text_001",
                    "action": "replace_with_placeholder",
                    "placeholder": "PAGE_TITLE",
                },
                {
                    "page_type": "content",
                    "element_id": "s02_image_002",
                    "action": "replace_with_placeholder",
                    "placeholder": "CONTENT_AREA",
                },
                {
                    "page_type": "content",
                    "element_id": "s02_text_002",
                    "action": "remove",
                },
            ],
        },
        "theme_colors": ["#0055AA"],
        "llm": {"enabled": True, "status": "complete"},
    }
    _write_json(_review_path(import_id), review)

    get_response = client.get(f"/api/templates/import/{import_id}/review")
    assert get_response.status_code == 200
    assert get_response.json()["draft"]["label"] == "Reviewed Template"
    assert get_response.json()["annotations"][0]["note"] == "old note"

    patch_annotation = client.patch(
        f"/api/templates/import/{import_id}/annotation/ann_keep",
        json={"resolved": True, "note": "fixed"},
    )
    assert patch_annotation.status_code == 200
    assert patch_annotation.json()["annotation"]["resolved"] is True

    put_response = client.put(
        f"/api/templates/import/{import_id}/review",
        json={"label": "Renamed", "page_selections": {"content": 2}},
    )
    assert put_response.status_code == 200
    assert put_response.json()["draft"]["label"] == "Renamed"

    preview_response = client.post(
        f"/api/templates/import/{import_id}/preview",
        json={"label": "Renamed", "page_selections": {"content": 2}},
    )
    assert preview_response.status_code == 200
    assert "{{CONTENT_AREA}}" in preview_response.json()["content_svg"]
    assert not (layouts_root / "user_source_test").exists()

    confirm_response = client.post(f"/api/templates/import/{import_id}/confirm")
    assert confirm_response.status_code == 200
    assert confirm_response.json()["status"] == "complete"

    content_svg = layouts_root / "user_source_test" / "03_content.svg"
    assert content_svg.exists()
    assert "{{CONTENT_AREA}}" in content_svg.read_text(encoding="utf-8")
    user_index = json.loads((layouts_root / "user_templates.json").read_text(encoding="utf-8"))
    assert user_index["templates"]["user_source_test"]["label"] == "Renamed"


def test_llm_assist_merges_design_spec_and_asset_decisions(workspace_tmp: Path, monkeypatch):
    import_id = "assist123"
    import_root = settings.workspaces_dir / "template_imports" / import_id
    pptx_path = import_root / "source.pptx"
    pptx_path.parent.mkdir(parents=True, exist_ok=True)
    pptx_path.write_bytes(b"pptx")
    initialize_import_task(import_id, pptx_path)

    work_dir = _work_dir(import_id)
    _write_json(
        work_dir / "manifest.json",
        {
            "source": {"name": "source.pptx"},
            "slideSize": {"width_px": 1280, "height_px": 720},
            "theme": {"colors": {"accent1": "#0055AA"}},
            "slides": [{"index": 1, "textSamples": ["Cover"]}, {"index": 2, "textSamples": ["Content"]}],
        },
    )
    review = {
        "import_id": import_id,
        "template_id": "user_assist_test",
        "label": "Source",
        "status": "review_required",
        "slide_count": 2,
        "page_types": ["cover", "toc", "chapter", "content", "ending"],
        "asset_roles": ["logo", "background", "decoration", "content_image", "ignore"],
        "page_type_candidates": {"cover": [1], "content": [2]},
        "slides": [
            {"index": 1, "elements": [{"element_id": "s01_text_001", "type": "text", "text": "Persistent Header"}]},
            {"index": 2, "elements": []},
        ],
        "assets": [
            {
                "asset_id": "asset_logo",
                "file_name": "logo.png",
                "usage_count": 2,
                "pages": [1, 2],
                "position_stable": True,
                "recommended_role": "decoration",
                "role": "decoration",
                "name": "logo",
                "occurrences": [],
            }
        ],
        "text_candidates": [{"text": "Persistent Header", "pages": [1, 2], "page_count": 2}],
        "draft": {
            "label": "Source",
            "page_selections": {"cover": 1, "content": 2},
            "assets": {"asset_logo": {"role": "decoration", "name": "logo"}},
            "preserve_texts": [],
            "design_spec": "",
        },
        "theme_colors": ["#0055AA"],
        "llm": {"enabled": False, "status": "not_run"},
    }
    _write_json(_review_path(import_id), review)

    async def fake_call(model_config, payload):
        assert payload["asset_candidates"][0]["asset_id"] == "asset_logo"
        return LLMTemplateImportPlan(
            label="Smart Template",
            page_selections=LLMPageSelections(cover=1, content=2),
            asset_decisions=[
                LLMAssetDecision(
                    asset_id="asset_logo",
                    role="logo",
                    name="Institute logo",
                    confidence=0.9,
                    reason="Repeated in the header",
                )
            ],
            placeholder_decisions=[
                LLMPlaceholderDecision(
                    page_type="content",
                    placeholder="PAGE_TITLE",
                    source_text="Persistent Header",
                )
            ],
            element_actions=[
                LLMElementAction(
                    page_type="content",
                    element_id="s01_text_001",
                    action="replace_with_placeholder",
                    placeholder="PAGE_TITLE",
                )
            ],
            preserve_texts=["Persistent Header"],
            design_spec_md="",
        )

    async def fake_design_spec(model_config, payload, review):
        assert review["draft"]["element_actions"][0]["element_id"] == "s01_text_001"
        return "# Smart spec"

    monkeypatch.setattr(template_importer_module, "_call_template_import_llm", fake_call)
    monkeypatch.setattr(template_importer_module, "_call_template_design_spec_llm", fake_design_spec)

    assisted = asyncio.run(
        assist_import_review(
            import_id,
            {"provider": "openai", "model": "test-model", "api_key": "test-key"},
        )
    )

    assert assisted["draft"]["label"] == "Smart Template"
    assert assisted["draft"]["assets"]["asset_logo"]["role"] == "logo"
    assert assisted["assets"][0]["recommendation_source"] == "llm"
    assert assisted["draft"]["preserve_texts"] == ["Persistent Header"]
    assert assisted["draft"]["placeholder_hints"]["content"]["PAGE_TITLE"] == "Persistent Header"
    assert assisted["draft"]["element_actions"][0]["element_id"] == "s01_text_001"
    assert assisted["draft"]["design_spec"] == "# Smart spec"
    assert assisted["llm"]["status"] == "complete"


def test_llm_import_plan_repairs_malformed_json(monkeypatch):
    class FakeProvider:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, model, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content='{"label":"Broken" "page_selections":{"content":1},"asset_decisions":[],"placeholder_decisions":[],"preserve_texts":[],"design_spec_md":"# Spec","notes":[]}'
                )
            return LLMResponse(
                content='{"label":"Broken","page_selections":{"content":1},"asset_decisions":[],"placeholder_decisions":[],"preserve_texts":[],"design_spec_md":"# Spec","notes":[]}'
            )

    fake_provider = FakeProvider()
    monkeypatch.setattr(template_importer_module, "create_provider", lambda *args, **kwargs: fake_provider)

    plan = asyncio.run(
        _call_template_import_llm(
            {"provider": "openai", "model": "test-model", "api_key": "test-key"},
            {"slides": [], "asset_candidates": []},
        )
    )

    assert fake_provider.calls == 2
    assert plan.label == "Broken"
    assert plan.page_selections.content == 1


def test_llm_import_plan_regenerates_after_empty_response(monkeypatch):
    class FakeProvider:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, model, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(content="")
            return LLMResponse(
                content='{"label":"Regenerated","page_selections":{"content":2},"asset_decisions":[],"placeholder_decisions":[],"preserve_texts":[],"design_spec_md":"# Spec","notes":[]}'
            )

    fake_provider = FakeProvider()
    monkeypatch.setattr(template_importer_module, "create_provider", lambda *args, **kwargs: fake_provider)

    plan = asyncio.run(
        _call_template_import_llm(
            {"provider": "openai", "model": "test-model", "api_key": "test-key"},
            {"slides": [{"index": 2}], "asset_candidates": []},
        )
    )

    assert fake_provider.calls == 2
    assert plan.label == "Regenerated"
    assert plan.page_selections.content == 2


def test_llm_import_plan_raises_after_json_retry_failure(monkeypatch):
    class FakeProvider:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, model, **kwargs):
            self.calls += 1
            return LLMResponse(content="")

    fake_provider = FakeProvider()
    monkeypatch.setattr(template_importer_module, "create_provider", lambda *args, **kwargs: fake_provider)

    with pytest.raises(ValueError, match="valid JSON after structured retry"):
        asyncio.run(
            _call_template_import_llm(
                {"provider": "openai", "model": "test-model", "api_key": "test-key"},
                {"slides": [{"index": 4, "elements": [{"element_id": "s04_text_001"}]}], "asset_candidates": []},
            )
        )

    assert fake_provider.calls == 2


def test_feedback_assist_records_conversation_and_trace(workspace_tmp: Path, monkeypatch):
    import_id = "feedback123"
    import_root = settings.workspaces_dir / "template_imports" / import_id
    pptx_path = import_root / "source.pptx"
    pptx_path.parent.mkdir(parents=True, exist_ok=True)
    pptx_path.write_bytes(b"pptx")
    initialize_import_task(import_id, pptx_path)

    work_dir = _work_dir(import_id)
    _write_json(
        work_dir / "manifest.json",
        {
            "source": {"name": "source.pptx"},
            "slideSize": {"width_px": 1280, "height_px": 720},
            "theme": {"colors": {"accent1": "#0055AA"}},
            "slides": [{"index": 1, "textSamples": ["Content"]}],
        },
    )
    _write_json(
        _review_path(import_id),
        {
            "import_id": import_id,
            "template_id": "user_feedback_test",
            "label": "Source",
            "status": "review_required",
            "slide_count": 1,
            "page_types": ["cover", "toc", "chapter", "content", "ending"],
            "asset_roles": ["logo", "background", "decoration", "content_image", "ignore"],
            "page_type_candidates": {"content": [1]},
            "slides": [{"index": 1, "elements": [{"element_id": "s01_text_001", "type": "text", "text": "Old"}]}],
            "assets": [],
            "text_candidates": [],
            "annotations": [
                {
                    "annotation_id": "ann_1",
                    "slide_index": 1,
                    "bbox_norm": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.1},
                    "note": "remove this title area",
                    "created_at": 1.0,
                    "resolved": False,
                }
            ],
            "draft": {
                "label": "Source",
                "page_selections": {"content": 1},
                "assets": {},
                "preserve_texts": [],
                "element_actions": [
                    {
                        "page_type": "content",
                        "element_id": "s01_text_001",
                        "action": "replace_with_placeholder",
                        "placeholder": "PAGE_TITLE",
                    }
                ],
                "design_spec": "# Old spec",
            },
            "theme_colors": ["#0055AA"],
            "llm": {"enabled": True, "status": "complete"},
        },
    )

    async def fake_call(model_config, payload):
        assert payload["user_feedback"] == "remove the extra title"
        assert payload["user_annotations"] == [
            "On slide 1, region (x=10.0%, y=20.0%, w=30.0%, h=10.0%): remove this title area"
        ]
        return LLMTemplateImportPlan(
            page_selections=LLMPageSelections(content=1),
            element_actions=[
                LLMElementAction(
                    page_type="content",
                    element_id="s01_text_001",
                    action="remove",
                )
            ],
            notes=["Removed the extra title placeholder."],
        )

    async def fake_design_spec(model_config, payload, review):
        assert payload["user_feedback"] == "remove the extra title"
        return "# Updated spec"

    monkeypatch.setattr(template_importer_module, "_call_template_import_llm", fake_call)
    monkeypatch.setattr(template_importer_module, "_call_template_design_spec_llm", fake_design_spec)

    assisted = asyncio.run(
        assist_import_review(
            import_id,
            {"provider": "openai", "model": "test-model", "api_key": "test-key"},
            required=True,
            feedback="remove the extra title",
        )
    )

    assert assisted["draft"]["element_actions"][0]["action"] == "remove"
    assert assisted["draft"]["design_spec"] == "# Updated spec"
    assert assisted["feedback_history"][0]["feedback"] == "remove the extra title"
    assert [message["role"] for message in assisted["conversation"]] == ["user", "assistant"]
    assert assisted["llm"]["changed"] is True
    assert assisted["llm_trace"]["input"]["user_feedback"] == "remove the extra title"
    assert assisted["llm_trace"]["action_plan"]["element_actions"][0]["action"] == "remove"


def test_template_listing_includes_builtin_and_user_and_blocks_builtin_delete(
    client,
    workspace_tmp: Path,
    monkeypatch,
):
    templates_root = workspace_tmp / "assets" / "templates"
    layouts_root = templates_root / "layouts"
    monkeypatch.setattr(settings, "templates_dir", templates_root)
    monkeypatch.setattr(template_manager, "_TEMPLATES_ROOT", layouts_root)
    monkeypatch.setattr(template_manager, "_INDEX_PATH", layouts_root / "layouts_index.json")
    monkeypatch.setattr(template_manager, "_USER_INDEX_PATH", layouts_root / "user_templates.json")

    builtin_dir = layouts_root / "builtin_test"
    user_dir = layouts_root / "user_test"
    builtin_dir.mkdir(parents=True)
    user_dir.mkdir(parents=True)
    (builtin_dir / "03_content.svg").write_text("<svg/>", encoding="utf-8")
    (user_dir / "03_content.svg").write_text("<svg/>", encoding="utf-8")
    (layouts_root / "layouts_index.json").write_text(
        json.dumps(
            {
                "categories": {"general": {"layouts": ["builtin_test"]}},
                "layouts": {"builtin_test": {"label": "Builtin"}},
            }
        ),
        encoding="utf-8",
    )
    (layouts_root / "user_templates.json").write_text(
        json.dumps({"templates": {"user_test": {"label": "User", "slideCount": 2}}}),
        encoding="utf-8",
    )

    response = client.get("/api/templates")
    assert response.status_code == 200
    by_id = {item["template_id"]: item for item in response.json()}
    assert by_id["builtin_test"]["source"] == "builtin"
    assert by_id["builtin_test"]["editable"] is False
    assert by_id["user_test"]["source"] == "user"
    assert by_id["user_test"]["editable"] is True

    delete_builtin = client.delete("/api/templates/builtin_test")
    assert delete_builtin.status_code == 400
