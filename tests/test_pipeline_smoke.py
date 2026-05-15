from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import ModuleType
from backend.orchestrator import pipeline as pipeline_module
from backend.orchestrator.pipeline import GenerationRequest
from backend.parser.paper_model import ParsedPaper, PaperSection
from backend.generator.svg_to_pptx.context import ConvertContext
from backend.generator.svg_to_pptx.elements import (
    _build_text_shape,
    convert_rect,
)
from backend.generator.svg_to_pptx.font_mapping import parse_font_family


def test_pipeline_smoke(monkeypatch, workspace_tmp):
    monkeypatch.setattr(pipeline_module.settings, "workspaces_dir", workspace_tmp / "workspaces")

    class FakeProvider:
        pass

    class FakeParser:
        async def parse(self, file_path, output_dir) -> ParsedPaper:
            output_dir.mkdir(parents=True, exist_ok=True)
            return ParsedPaper(
                title="Test Paper",
                sections=[PaperSection(title="Intro", level=1, content="Body")],
                source_type="pdf",
                figures_dir=output_dir / "images",
            )

    async def fake_analyze(*args, **kwargs) -> str:
        return "# Slide One\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n---\n# Slide Two\nBody"

    async def fake_design(*args, **kwargs) -> str:
        return "# Design spec"

    async def fake_generate(design_spec, manuscript, project_dir, llm, model, **kwargs):
        svg_dir = project_dir / "svg_output"
        svg_dir.mkdir(parents=True, exist_ok=True)
        for index in (1, 2):
            content = (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
                f'<text x="40" y="{80 * index}">Slide {index}</text></svg>'
            )
            (svg_dir / f"{index:02d}_slide.svg").write_text(content, encoding="utf-8")
            yield index, content

    def fake_finalize(project_dir):
        final_dir = project_dir / "svg_final"
        final_dir.mkdir(parents=True, exist_ok=True)
        for svg_file in (project_dir / "svg_output").glob("*.svg"):
            (final_dir / svg_file.name).write_text(svg_file.read_text(encoding="utf-8"), encoding="utf-8")
        return {"total_files": 2}

    def fake_create(svg_files, output_path, canvas_format="ppt169", notes=None):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"pptx")
        return output_path

    monkeypatch.setattr(pipeline_module.research_agent, "analyze_paper", fake_analyze)
    monkeypatch.setattr(pipeline_module.strategist_agent, "create_design_spec", fake_design)
    monkeypatch.setattr(pipeline_module.svg_executor, "generate_svg_pages", fake_generate)

    fake_llm_module = ModuleType("backend.llm")
    fake_llm_module.create_provider = (
        lambda provider, api_key, base_url=None: FakeProvider()
    )
    sys.modules["backend.llm"] = fake_llm_module

    fake_pdf_module = ModuleType("backend.parser.pdf_parser")
    fake_pdf_module.PDFParser = lambda: FakeParser()
    sys.modules["backend.parser.pdf_parser"] = fake_pdf_module

    fake_latex_module = ModuleType("backend.parser.latex_parser")
    fake_latex_module.LaTeXParser = lambda: FakeParser()
    sys.modules["backend.parser.latex_parser"] = fake_latex_module

    fake_finalize_module = ModuleType("backend.generator.svg_finalize")
    fake_finalize_module.finalize_project = fake_finalize
    sys.modules["backend.generator.svg_finalize"] = fake_finalize_module

    fake_builder_module = ModuleType("backend.generator.svg_to_pptx")
    fake_builder_module.create_pptx = fake_create
    sys.modules["backend.generator.svg_to_pptx"] = fake_builder_module

    request = GenerationRequest(
        file_path=workspace_tmp / "paper.pdf",
        source_type="pdf",
        provider="openai",
        model="gpt-4o",
        api_key="key",
    )
    request.file_path.write_bytes(b"%PDF")

    async def collect_events():
        return [event async for event in pipeline_module.run_pipeline(request)]

    events = asyncio.run(collect_events())

    generation_started = next(event for event in events if event.stage == "generation" and event.status == "started")
    assert generation_started.data == {"total_slides": 2}
    assert events[-1].stage == "export"
    assert events[-1].status == "complete"
    assert events[-1].data is not None
    assert Path(events[-1].data["output_path"]).exists()


def test_effective_deep_research_auto_enables_for_parallel_and_very_high(workspace_tmp):
    base = dict(
        file_path=workspace_tmp / "paper.pdf",
        source_type="pdf",
        provider="openai",
        model="gpt-4o",
        api_key="key",
    )

    assert not pipeline_module._effective_deep_research(GenerationRequest(**base))
    assert pipeline_module._effective_deep_research(
        GenerationRequest(**base, generation_mode="page_parallel")
    )
    assert pipeline_module._effective_deep_research(
        GenerationRequest(**base, detail_level="very_high")
    )


def test_svg_export_handles_percentage_lengths_and_opacity():
    class FakeElem:
        def __init__(self, attrib):
            self.attrib = attrib
            self.tag = "rect"

        def get(self, key, default=None):
            return self.attrib.get(key, default)

    ctx = ConvertContext(defs={})
    elem = FakeElem(
        {
            "x": "10",
            "y": "20",
            "width": "50%",
            "height": "40",
            "opacity": "0%",
            "fill": "#ff6600",
            "stroke": "#000000",
            "stroke-width": "2px",
        }
    )

    shape_xml = convert_rect(elem, ctx)

    assert shape_xml
    assert 'cx="' in shape_xml


def test_svg_export_selects_single_ppt_font_from_css_stack():
    fonts = parse_font_family(
        "Inter, Noto Sans CJK SC, Source Han Sans SC, Microsoft YaHei, Arial, sans-serif",
    )

    assert fonts["latin"] in ("Segoe UI", "Arial")
    assert fonts["ea"] == "Microsoft YaHei"


def test_svg_export_text_boxes_do_not_autowrap_or_autofit():
    ctx = ConvertContext(defs={})
    shape_xml = _build_text_shape(
        640,
        228,
        [
            {
                "text": "VFR",
                "font_size": 72,
                "color": "1A365D",
                "bold": True,
                "italic": False,
                "underline": False,
                "font_family": "Microsoft YaHei",
            }
        ],
        ctx,
        "middle",
    )

    assert 'wrap="none"' in shape_xml
    assert "<a:noAutofit/>" in shape_xml
    assert "<a:normAutofit/>" not in shape_xml
