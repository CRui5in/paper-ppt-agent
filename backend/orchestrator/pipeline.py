"""Main paper-to-PPT pipeline orchestrator.

Ties together: parsing → research → strategist → SVG executor → finalize → export.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

from backend.config import settings
from backend.api.schemas import ResearchConfig
from backend.generator.svg_critic import CriticReport
from backend.runtime import aoffload, aread_text, awrite_text
from backend.usage.tracker import reset_usage_context, set_usage_context

from .manuscript import count_manuscript_pages
from . import research_agent, strategist_agent, svg_executor
from .research_agent import ResearchContext
from .research_enrichment import enrich_context


@dataclass
class ProgressEvent:
    """Progress event emitted during pipeline execution."""

    stage: str
    status: Literal["started", "progress", "complete", "error"]
    message: str = ""
    progress: float = 0.0  # 0.0 to 1.0
    data: dict | None = None


def _format_enrichment_message(stats) -> str:
    """One-line summary of enrichment results for the progress channel.

    Translation happens on the frontend via i18nStatus — keep this string
    structural and English-source so the translator can pattern-match.
    """
    bits: list[str] = []
    if stats.arxiv_found or stats.arxiv_error is not None:
        if stats.arxiv_error:
            bits.append(f"arXiv: {stats.arxiv_error}")
        else:
            bits.append(f"arXiv: {stats.arxiv_found}")
    if stats.scholar_found or stats.scholar_error is not None:
        if stats.scholar_error:
            bits.append(f"Semantic Scholar: {stats.scholar_error}")
        else:
            bits.append(f"Semantic Scholar: {stats.scholar_found}")
    if stats.web_found or stats.web_error is not None:
        if stats.web_error:
            bits.append(f"Web: {stats.web_error}")
        else:
            bits.append(f"Web: {stats.web_found}")
    if not bits:
        return "External research returned no results"
    return f"External research — {', '.join(bits)}"


def _querying_enrichment_payload(config: ResearchConfig) -> dict:
    payload: dict = {"phase": "querying", "total_findings": 0, "filtered_findings": 0}
    if config.arxiv_search_enabled:
        payload["arxiv"] = {"found": 0}
    if config.semantic_scholar_enabled:
        payload["semantic_scholar"] = {"found": 0}
    if config.web_search_enabled:
        provider = "tavily" if config.tavily_api_key else "serpapi" if config.serpapi_key else None
        web: dict = {"found": 0}
        if provider:
            web["provider"] = provider
        payload["web"] = web
    return payload


async def _write_enrichment_debug(
    project_dir: Path, ctx: ResearchContext, stats
) -> None:
    debug_dir = project_dir / "debug"
    try:
        await aoffload(debug_dir.mkdir, parents=True, exist_ok=True)
        payload = stats.to_dict()
        payload["queries_used"] = list(ctx.queries_used)
        await awrite_text(
            debug_dir / "research_enrichment_stats.json",
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        block = ctx.enrichment_block_for_pass1()
        if block:
            await awrite_text(
                debug_dir / "research_enrichment_pass1_block.md",
                block,
                encoding="utf-8",
            )
    except OSError:
        pass


@dataclass
class GenerationRequest:
    """Request parameters for paper-to-PPT generation."""

    file_path: Path
    source_type: Literal["pdf", "latex"]
    provider: str  # "openai", "anthropic", "gemini"
    model: str
    api_key: str
    base_url: str | None = None
    canvas_format: str = "ppt169"
    style: str = "academic"
    num_pages: int | None = None
    instruction: str = ""
    language: str = "zh"
    detail_level: str = "normal"
    timeout_seconds: int | None = None
    max_critic_attempts: int = 3
    style_overrides: dict | None = None  # {palette: [...], font: "...", density: "..."}
    enable_deep_research: bool = False
    icon_library: str = "chunk"  # chunk / tabler-filled / tabler-outline
    deepseek_settings: dict | None = None
    openai_settings: dict | None = None
    enable_visual_critic: bool = False
    enable_icon: bool = False
    enable_icon_rag: bool = False
    gemini_api_key: str | None = None
    visual_qa_max_attempts: int = 1
    template_id: str | None = None  # Template ID from assets/templates/layouts/
    research_config: ResearchConfig | None = None  # Optional external research enrichment


async def run_pipeline(
    request: GenerationRequest,
) -> AsyncIterator[ProgressEvent]:
    """Execute the full paper-to-PPT pipeline.

    Yields ProgressEvent at each stage transition.
    """
    # Initialize LLM provider
    from backend.generator.project_manager import get_notes, get_svg_files, init_project
    from backend.generator.svg_finalize import finalize_project
    from backend.generator.svg_to_pptx import create_pptx
    from backend.llm import create_provider
    from backend.parser.latex_parser import LaTeXParser
    from backend.parser.pdf_parser import PDFParser

    provider_kwargs = {"base_url": request.base_url}
    if request.deepseek_settings is not None:
        provider_kwargs["deepseek_settings"] = request.deepseek_settings
    if request.openai_settings is not None:
        provider_kwargs["openai_settings"] = request.openai_settings
    llm = create_provider(
        request.provider,
        request.api_key,
        **provider_kwargs,
    )

    # Create project workspace
    project_dir = init_project(
        name="paper_ppt",
        canvas_format=request.canvas_format,
        base_dir=settings.workspaces_dir,
    )

    # Attribute every LLM call inside this run to the caller's job if known.
    job_id = getattr(request, "job_id", None)
    usage_snapshot = set_usage_context(
        job_id=job_id,
        stage="parsing",
        page=None,
        attempt=1,
    )

    try:
        # Stage 1: Parse paper
        yield ProgressEvent(
            "parsing",
            "started",
            "Parsing paper...",
            data={"project_dir": str(project_dir)},
        )

        output_dir = project_dir / "sources"
        if request.source_type == "pdf":
            parser = PDFParser()
        else:
            parser = LaTeXParser()

        paper = await parser.parse(request.file_path, output_dir)

        parse_info: dict = {}
        if request.source_type == "pdf":
            parse_info = dict(
                getattr(parser, "last_parse_info", None)
                or getattr(type(parser), "last_parse_info", None)
                or {}
            )

        complete_msg = f"Parsed: {paper.title}"
        if parse_info.get("fallback"):
            complete_msg += " (layout fallback used)"
        yield ProgressEvent(
            "parsing",
            "complete",
            complete_msg,
            0.15,
            data={"parse_info": parse_info} if parse_info else None,
        )

        # Stage 2: Research agent
        set_usage_context(stage="research", page=None, attempt=1)
        yield ProgressEvent(
            "research",
            "started",
            "Deep reading: analyzing paper content..."
            if request.enable_deep_research
            else "Analyzing paper content...",
        )

        # Optional Stage 2a: external enrichment runs BEFORE Pass 1 so the
        # related-work context is available where it actually matters
        # (gap analysis, contribution framing). We surface per-source stats
        # via ProgressEvent.data so the frontend can show users that the
        # toggles they enabled actually returned something.
        research_ctx = ResearchContext()
        research_cfg = getattr(request, "research_config", None)
        any_source_enabled = bool(
            research_cfg
            and (
                research_cfg.arxiv_search_enabled
                or research_cfg.semantic_scholar_enabled
                or research_cfg.web_search_enabled
            )
        )
        if any_source_enabled:
            yield ProgressEvent(
                "research",
                "progress",
                "Querying external research sources...",
                0.10,
                data={"enrichment": _querying_enrichment_payload(research_cfg)},
            )
            stats = await enrich_context(
                research_ctx,
                paper_title=paper.title,
                paper_abstract=paper.abstract,
                config=research_cfg,
            )
            await _write_enrichment_debug(project_dir, research_ctx, stats)
            yield ProgressEvent(
                "research",
                "progress",
                _format_enrichment_message(stats),
                0.12,
                data={"enrichment": stats.to_dict()},
            )

        yield ProgressEvent("research", "progress", "Pass 1/4 — Deep reading", 0.13)

        # Collect progress events from research_agent via queue for real-time yielding
        _research_progress_queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue()

        def _on_research_progress(message: str, fraction: float) -> None:
            _research_progress_queue.put_nowait(
                ProgressEvent("research", "progress", message, fraction)
            )

        async def _run_analysis() -> str:
            try:
                result = await research_agent.analyze_paper(
                    paper,
                    llm,
                    request.model,
                    instruction=request.instruction,
                    num_pages=request.num_pages,
                    language=request.language,
                    detail_level=request.detail_level,
                    research_context=research_ctx,
                    enable_deep_research=request.enable_deep_research,
                    debug_dir=project_dir / "debug",
                    on_progress=_on_research_progress,
                )
                return result
            finally:
                # ALWAYS unblock the consumer, even if analyze_paper raised.
                # Without this sentinel the outer ``await queue.get()`` loop
                # would block forever on auth errors / network failures and
                # the user would see the pipeline frozen on Pass 1 with no
                # error in the UI until they manually cancel the job.
                await _research_progress_queue.put(None)

        analysis_task = asyncio.create_task(_run_analysis())
        manuscript: str | None = None

        # Drain progress events; the producer always posts the sentinel in
        # its `finally`, so this loop terminates whether analysis succeeded
        # or raised.
        while True:
            event = await _research_progress_queue.get()
            if event is None:
                break
            yield event

        # Now surface the actual result (or re-raise the exception so the
        # outer try/except in run_pipeline emits a proper error event).
        manuscript = analysis_task.result()

        # Save manuscript and deep analysis artifacts
        await awrite_text(project_dir / "manuscript.md", manuscript, encoding="utf-8")
        yield ProgressEvent(
            "research",
            "complete",
            "Deep analysis complete (4-pass)"
            if request.enable_deep_research
            else "Paper analysis complete",
            0.30,
        )

        # Stage 3: Strategist agent
        set_usage_context(stage="strategy", page=None, attempt=1)
        yield ProgressEvent("strategy", "started", "Creating design specification...")
        figure_inventory = _build_figure_inventory(paper, project_dir)

        # Load template context if specified
        template_context_strat = ""
        template_context_exec = ""
        template_skeletons: dict[str, str] | None = None
        if request.template_id:
            from backend.generator.template_manager import (
                build_template_context_for_executor,
                build_template_context_for_strategist,
                build_template_skeletons,
                load_template,
            )
            tmpl = load_template(request.template_id)
            if tmpl:
                template_context_strat = build_template_context_for_strategist(tmpl)
                template_context_exec = build_template_context_for_executor(tmpl)
                template_skeletons = build_template_skeletons(tmpl)
                yield ProgressEvent(
                    "strategy", "progress",
                    f"Template '{request.template_id}' loaded: {tmpl.info.label}",
                )

        design_spec = await strategist_agent.create_design_spec(
            manuscript,
            llm,
            request.model,
            canvas_format=request.canvas_format,
            style=request.style,
            language=request.language,
            detail_level=request.detail_level,
            icon_library=request.icon_library,
            style_overrides=request.style_overrides,
            enable_icon=request.enable_icon,
            enable_icon_rag=request.enable_icon_rag,
            gemini_api_key=request.gemini_api_key,
            figure_inventory=figure_inventory,
            debug_dir=project_dir / "debug",
            template_context=template_context_strat or None,
        )

        # Save design spec
        await awrite_text(project_dir / "design_spec.md", design_spec, encoding="utf-8")
        yield ProgressEvent("strategy", "complete", "Design spec created", 0.40)

        # Stage 4: SVG executor
        set_usage_context(stage="generation", page=None, attempt=1)
        total_pages = count_manuscript_pages(manuscript)
        yield ProgressEvent(
            "generation",
            "started",
            "Generating slide SVGs...",
            0.40,
            data={"total_slides": total_pages},
        )
        generated = 0

        critic_events: list[dict] = []
        svg_update_queue: list[tuple[int, str]] = []

        async def _on_critic(
            page_num: int,
            attempt: int,
            report: CriticReport,
            repair_prompt: str | None,
            archive_path: str | None,
        ) -> None:
            entry: dict = {
                "page": page_num,
                "attempt": attempt,
                "report": report.to_dict(),
            }
            if repair_prompt is not None:
                entry["repair_prompt"] = repair_prompt
            if archive_path is not None:
                entry["archive_path"] = archive_path
            critic_events.append(entry)

        async def _on_svg_update(page_num: int, svg: str) -> None:
            svg_update_queue.append((page_num, svg))

        async for page_num, svg_content in svg_executor.generate_svg_pages(
            design_spec,
            manuscript,
            project_dir,
            llm,
            request.model,
            style=request.style,
            language=request.language,
            detail_level=request.detail_level,
            extra_instruction=_build_style_overrides_block(request.style_overrides),
            on_critic=_on_critic,
            on_svg_update=_on_svg_update,
            figure_inventory=figure_inventory,
            enable_visual_critic=request.enable_visual_critic,
            max_critic_attempts=request.max_critic_attempts,
            visual_qa_max_attempts=request.visual_qa_max_attempts,
            template_context=template_context_exec or None,
            template_skeletons=template_skeletons,
        ):
            generated += 1
            progress = 0.40 + (generated / total_pages) * 0.35

            # Embed images inline for live preview (browser can't load file:// paths)
            preview_svg = _embed_svg_preview(svg_content, project_dir)

            page_critic = [ev for ev in critic_events if ev["page"] == page_num]
            # Send intermediate SVG updates from repair cycles
            while svg_update_queue:
                upd_page, upd_svg = svg_update_queue.pop(0)
                upd_preview = _embed_svg_preview(upd_svg, project_dir)
                yield ProgressEvent(
                    "generation",
                    "progress",
                    f"Repaired slide {upd_page}",
                    progress,
                    data={"page": upd_page, "svg": upd_preview},
                )

            yield ProgressEvent(
                "generation",
                "progress",
                f"Generated slide {page_num}/{total_pages}",
                progress,
                data={
                    "page": page_num,
                    "svg": preview_svg,
                    "critic": page_critic,
                },
            )

        yield ProgressEvent(
            "generation",
            "complete",
            f"{generated} slides generated",
            0.75,
            data={"critic": critic_events},
        )

        await _save_critic_history(project_dir, critic_events)

        # Stage 5: Post-processing
        set_usage_context(stage="postprocess", page=None, attempt=1)
        yield ProgressEvent("postprocess", "started", "Finalizing SVGs...")
        # finalize_project walks every SVG, runs cairosvg / PIL / regex
        # rewrites: minutes of synchronous CPU work. Push it to the offload
        # pool so heartbeats, the WS event bus, and the scheduler dispatcher
        # all keep ticking.
        stats = await aoffload(finalize_project, project_dir)
        yield ProgressEvent(
            "postprocess",
            "complete",
            f"Processed {stats['total_files']} files",
            0.85,
        )

        # Stage 6: Export PPTX
        set_usage_context(stage="export", page=None, attempt=1)
        yield ProgressEvent("export", "started", "Exporting to PowerPoint...")
        svg_files = get_svg_files(project_dir, source="final")
        notes = get_notes(project_dir, svg_files)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        pptx_path = project_dir / "exports" / f"presentation_{timestamp}.pptx"

        await aoffload(
            create_pptx,
            svg_files,
            pptx_path,
            canvas_format=request.canvas_format,
            notes=notes,
        )

        yield ProgressEvent(
            "export",
            "complete",
            "PowerPoint generated!",
            1.0,
            data={"output_path": str(pptx_path)},
        )

    except Exception as e:
        yield ProgressEvent("error", "error", str(e))
        raise
    finally:
        reset_usage_context(usage_snapshot)


async def _load_figure_inventory(project_dir: Path) -> list[dict]:
    """Read figure_review.json from a refine workspace.

    The refine pipeline runs without re-parsing the PDF, so we cannot call
    ``_build_figure_inventory(paper, ...)`` here. ``figure_review.json``
    is written once during the original parse and contains everything the
    executor needs to resolve `[[FIG:id]]` tokens to real hrefs.
    """
    candidate = project_dir / "sources" / "images" / "figure_review.json"
    if not candidate.exists():
        return []
    try:
        text = await aread_text(candidate, encoding="utf-8")
        records = json.loads(text)
    except (OSError, ValueError):
        return []
    inventory: list[dict] = []
    for rec in records:
        try:
            abs_path = Path(rec.get("path") or "").resolve()
            try:
                rel = abs_path.relative_to(project_dir.resolve())
                rel_str = f"../{rel.as_posix()}" if rel.parts and rel.parts[0] == "sources" else rel.as_posix()
            except ValueError:
                rel_str = str(abs_path)
        except (OSError, ValueError):
            rel_str = str(rec.get("path") or "")
        inventory.append({
            "path": rel_str,
            "natural_width": int(rec.get("natural_width") or 0),
            "natural_height": int(rec.get("natural_height") or 0),
            "aspect_ratio": float(rec.get("aspect_ratio") or 0.0),
            "caption": rec.get("caption") or "",
            "page_number": rec.get("page_number"),
        })
    return inventory


def _build_figure_inventory(paper, project_dir: Path) -> list[dict]:
    """Collect available figures with natural sizes for the strategist prompt."""
    inventory: list[dict] = []
    for fig in paper.all_figures():
        if not getattr(fig, "available", False):
            continue
        try:
            rel = Path(fig.path).resolve().relative_to(project_dir.resolve())
            rel_str = f"../{rel.as_posix()}" if rel.parts and rel.parts[0] == "sources" else rel.as_posix()
        except (ValueError, OSError):
            rel_str = str(fig.path)
        inventory.append({
            "path": rel_str,
            "natural_width": int(getattr(fig, "natural_width", 0) or 0),
            "natural_height": int(getattr(fig, "natural_height", 0) or 0),
            "aspect_ratio": float(getattr(fig, "aspect_ratio", 0.0) or 0.0),
            "caption": getattr(fig, "caption", "") or "",
            "page_number": getattr(fig, "page_number", None),
        })
    return inventory


def _embed_svg_preview(svg_content: str, project_dir: Path) -> str:
    """Embed image hrefs as base64 data URIs so browsers can render them.

    Runs the same lightweight render-prep path used by preview/export,
    including icon embedding, image embedding, and safe text flattening.
    """
    from backend.generator.svg_finalize.render_ready import prepare_svg_content_for_render

    try:
        return prepare_svg_content_for_render(svg_content, project_dir / "svg_output")
    except Exception as exc:
        raise RuntimeError(f"SVG preview preparation failed: {exc}") from exc


# ── Refine pipeline ───────────────────────────────────────────────────────────


@dataclass
class RefineRequest:
    """Parameters for a refine (feedback-based iteration) run."""

    project_dir: str        # absolute path to the existing project workspace
    feedback: str           # latest user feedback text
    feedback_history: list[str]  # all feedback rounds including the latest
    job_id: str             # new job ID (for logging / context)
    parent_job_id: str      # job ID of the previous generation
    provider: str = "openai"
    model: str = "gpt-4o"
    api_key: str = ""
    base_url: str | None = None
    canvas_format: str = "ppt169"
    style: str = "academic"
    language: str = "zh"
    detail_level: str = "normal"
    timeout_seconds: int | None = None
    max_critic_attempts: int = 3
    target_pages: list[int] | None = None
    allow_structure_changes: bool = False
    style_overrides: dict | None = None
    icon_library: str = "chunk"
    deepseek_settings: dict | None = None
    openai_settings: dict | None = None
    enable_visual_critic: bool = False
    enable_icon: bool = False
    enable_icon_rag: bool = False
    gemini_api_key: str | None = None
    visual_qa_max_attempts: int = 1
    template_id: str | None = None


async def run_refine_pipeline(
    request: RefineRequest,
) -> AsyncIterator[ProgressEvent]:
    """Re-run stages 4–6 (SVG generation → finalize → export) with feedback.

    Reads the existing ``manuscript.md`` and ``design_spec.md`` from the
    project directory, appends the accumulated feedback as an extra
    instruction block, then re-generates all SVG pages.

    Previously generated SVGs are archived to ``svg_archive/round_N/``
    before being overwritten so the user can compare iterations.

    Yields ProgressEvent at each stage transition — identical shape to
    ``run_pipeline`` so the frontend WebSocket handler needs no changes.
    """
    from backend.generator.project_manager import get_notes, get_svg_files
    from backend.generator.svg_finalize import finalize_project
    from backend.generator.svg_to_pptx import create_pptx
    from backend.llm import create_provider

    project_dir = Path(request.project_dir)
    if not project_dir.exists():
        yield ProgressEvent("error", "error", f"Project directory not found: {project_dir}")
        return

    # Read existing manuscript and design_spec
    manuscript_path = project_dir / "manuscript.md"
    design_spec_path = project_dir / "design_spec.md"

    if not manuscript_path.exists() or not design_spec_path.exists():
        yield ProgressEvent(
            "error", "error",
            "Cannot refine: manuscript.md or design_spec.md missing from project."
        )
        return

    manuscript = await aread_text(manuscript_path, encoding="utf-8")
    design_spec = await aread_text(design_spec_path, encoding="utf-8")

    provider_kwargs = {"base_url": request.base_url}
    if request.deepseek_settings is not None:
        provider_kwargs["deepseek_settings"] = request.deepseek_settings
    if request.openai_settings is not None:
        provider_kwargs["openai_settings"] = request.openai_settings
    llm = create_provider(
        request.provider,
        request.api_key,
        **provider_kwargs,
    )
    target_pages = sorted({page for page in (request.target_pages or []) if page > 0})

    refine_snapshot = set_usage_context(
        job_id=request.job_id,
        stage="refine",
        page=None,
        attempt=1,
    )

    # Build feedback block injected into the SVG executor prompt
    feedback_block = _build_feedback_block(
        request.feedback_history,
        target_pages=target_pages,
        allow_structure_changes=request.allow_structure_changes,
    )
    overrides_block = _build_style_overrides_block(request.style_overrides)
    if overrides_block:
        feedback_block = (
            f"{feedback_block}\n\n{overrides_block}" if feedback_block else overrides_block
        )

    if request.allow_structure_changes:
        set_usage_context(stage="research", page=None, attempt=1)
        yield ProgressEvent("research", "started", "Revising manuscript structure from feedback...", 0.0)
        manuscript = await research_agent.revise_manuscript(
            manuscript,
            llm,
            request.model,
            feedback_history=request.feedback_history,
            language=request.language,
            detail_level=request.detail_level,
            target_pages=target_pages,
            allow_structure_changes=True,
        )
        await awrite_text(manuscript_path, manuscript, encoding="utf-8")
        yield ProgressEvent("research", "complete", "Manuscript revised", 0.15)

        set_usage_context(stage="strategy", page=None, attempt=1)
        yield ProgressEvent("strategy", "started", "Rebuilding design specification...", 0.15)
        refine_figure_inventory = await _load_figure_inventory(project_dir)
        design_spec = await strategist_agent.create_design_spec(
            manuscript,
            llm,
            request.model,
            canvas_format=request.canvas_format,
            style=request.style,
            language=request.language,
            detail_level=request.detail_level,
            icon_library=request.icon_library,
            style_overrides=request.style_overrides,
            enable_icon=request.enable_icon,
            enable_icon_rag=request.enable_icon_rag,
            gemini_api_key=request.gemini_api_key,
            figure_inventory=refine_figure_inventory,
        )
        await awrite_text(design_spec_path, design_spec, encoding="utf-8")
        yield ProgressEvent("strategy", "complete", "Design spec rebuilt", 0.30)

    # Archive current svg_output before overwriting
    _archive_svgs(project_dir)
    if target_pages and not request.allow_structure_changes:
        _seed_svg_output_for_targeted_refine(project_dir, target_pages)

    # ── Stage 4: SVG generation (with feedback) ───────────────────────────
    set_usage_context(stage="generation", page=None, attempt=1)
    total_pages = count_manuscript_pages(manuscript)
    pages_to_generate = len(target_pages) if target_pages and not request.allow_structure_changes else total_pages
    generation_start = 0.30 if request.allow_structure_changes else 0.0
    generation_span = 0.30 if request.allow_structure_changes else 0.60
    yield ProgressEvent(
        "generation", "started",
        "Re-generating selected slides with feedback..." if target_pages and not request.allow_structure_changes else "Re-generating slides with feedback...",
        generation_start,
        data={"total_slides": pages_to_generate},
    )
    generated = 0

    refine_critic_events: list[dict] = []

    async def _refine_on_critic(
        page_num: int,
        attempt: int,
        report: CriticReport,
        repair_prompt: str | None,
        archive_path: str | None,
    ) -> None:
        entry: dict = {
            "page": page_num,
            "attempt": attempt,
            "report": report.to_dict(),
        }
        if repair_prompt is not None:
            entry["repair_prompt"] = repair_prompt
        if archive_path is not None:
            entry["archive_path"] = archive_path
        refine_critic_events.append(entry)

    refine_inventory = await _load_figure_inventory(project_dir)

    # Load template context for refine if specified
    refine_template_ctx = ""
    refine_template_skeletons: dict[str, str] | None = None
    if request.template_id:
        from backend.generator.template_manager import (
            build_template_context_for_executor,
            build_template_skeletons,
            load_template,
        )
        _tmpl = load_template(request.template_id)
        if _tmpl:
            refine_template_ctx = build_template_context_for_executor(_tmpl)
            refine_template_skeletons = build_template_skeletons(_tmpl)

    async for page_num, svg_content in svg_executor.generate_svg_pages(
        design_spec,
        manuscript,
        project_dir,
        llm,
        request.model,
        style=request.style,
        language=request.language,
        detail_level=request.detail_level,
        extra_instruction=feedback_block,
        target_pages=set(target_pages) if target_pages and not request.allow_structure_changes else None,
        on_critic=_refine_on_critic,
        figure_inventory=refine_inventory,
        enable_visual_critic=request.enable_visual_critic,
        max_critic_attempts=request.max_critic_attempts,
        visual_qa_max_attempts=request.visual_qa_max_attempts,
        template_context=refine_template_ctx or None,
        template_skeletons=refine_template_skeletons,
    ):
        generated += 1
        progress = generation_start + (generated / max(pages_to_generate, 1)) * generation_span

        preview_svg = _embed_svg_preview(svg_content, project_dir)
        page_critic = [ev for ev in refine_critic_events if ev["page"] == page_num]
        yield ProgressEvent(
            "generation", "progress",
            f"Generated slide {page_num}/{total_pages}",
            progress,
            data={"page": page_num, "svg": preview_svg, "critic": page_critic},
        )

    yield ProgressEvent(
        "generation",
        "complete",
        f"{generated} slides regenerated",
        generation_start + generation_span,
    )

    # ── Stage 5: Post-processing ──────────────────────────────────────────
    set_usage_context(stage="postprocess", page=None, attempt=1)
    postprocess_start = generation_start + generation_span
    export_start = 0.80 if request.allow_structure_changes else 0.80
    yield ProgressEvent("postprocess", "started", "Finalizing SVGs...", postprocess_start)
    stats = await aoffload(finalize_project, project_dir)
    yield ProgressEvent(
        "postprocess", "complete",
        f"Processed {stats['total_files']} files",
        export_start,
    )

    # ── Stage 6: Export PPTX ─────────────────────────────────────────────
    set_usage_context(stage="export", page=None, attempt=1)
    yield ProgressEvent("export", "started", "Exporting to PowerPoint...", export_start)
    svg_files = get_svg_files(project_dir, source="final")
    notes = get_notes(project_dir, svg_files)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pptx_path = project_dir / "exports" / f"presentation_{timestamp}.pptx"

    await aoffload(
        create_pptx,
        svg_files,
        pptx_path,
        canvas_format=request.canvas_format,
        notes=notes,
    )

    # Persist feedback history to disk for auditability
    await _save_feedback_history(project_dir, request.feedback_history)
    await _save_critic_history(project_dir, refine_critic_events, refine=True)

    yield ProgressEvent(
        "export", "complete",
        "Refined PowerPoint generated!",
        1.0,
        data={"output_path": str(pptx_path), "critic": refine_critic_events},
    )

    reset_usage_context(refine_snapshot)


def _archive_svgs(project_dir: Path) -> None:
    """Copy current svg_output/ into svg_archive/round_N/ before overwriting."""
    import shutil

    svg_output = project_dir / "svg_output"
    if not svg_output.exists():
        return

    archive_base = project_dir / "svg_archive"
    archive_base.mkdir(exist_ok=True)

    # find next round number
    existing = [d for d in archive_base.iterdir() if d.is_dir() and d.name.startswith("round_")]
    next_round = len(existing) + 1
    dest = archive_base / f"round_{next_round:02d}"
    try:
        shutil.copytree(svg_output, dest)
    except Exception:
        pass


def _build_feedback_block(
    feedback_history: list[str],
    *,
    target_pages: list[int] | None = None,
    allow_structure_changes: bool = False,
) -> str:
    """Format accumulated feedback history as a prompt instruction block."""
    if not feedback_history:
        return ""
    lines = ["## User Feedback (apply to ALL slides)"]
    if target_pages:
        lines.append(
            "\n## Targeted Scope\n"
            f"\nOnly modify these slide pages: {', '.join(map(str, target_pages))}."
            "\nKeep all other pages visually and semantically unchanged."
        )
    if allow_structure_changes:
        lines.append(
            "\n## Structural Changes Allowed\n"
            "\nYou may insert new slides, remove slides, split dense slides, or reorder slides if needed."
        )
    for i, fb in enumerate(feedback_history, 1):
        lines.append(f"\n### Round {i}\n{fb.strip()}")
    lines.append(
        "\n**Important:** Address ALL feedback points above when generating each slide."
    )
    return "\n".join(lines)


def _build_style_overrides_block(overrides: dict | None) -> str:
    """Format user style overrides (palette/font/density) as a prompt block."""
    if not overrides:
        return ""
    parts: list[str] = []
    palette = overrides.get("palette") if isinstance(overrides, dict) else None
    font = overrides.get("font") if isinstance(overrides, dict) else None
    font_heading = overrides.get("font_heading") if isinstance(overrides, dict) else None
    font_body = overrides.get("font_body") if isinstance(overrides, dict) else None
    cjk_heading = overrides.get("cjk_heading") if isinstance(overrides, dict) else None
    cjk_body = overrides.get("cjk_body") if isinstance(overrides, dict) else None
    density = overrides.get("density") if isinstance(overrides, dict) else None
    if palette:
        try:
            colors = ", ".join(str(c) for c in palette if c)
        except TypeError:
            colors = ""
        if colors:
            parts.append(
                f"- **Palette override** (use these exact colors for primary / accent / background "
                f"where appropriate): {colors}"
            )
    if font_heading or font_body or cjk_heading or cjk_body:
        if font_heading:
            parts.append(
                f"- **Western heading font**: Use `{font_heading}` for Western heading `<text>` elements."
            )
        if font_body:
            parts.append(
                f"- **Western body font**: Use `{font_body}` for Western body `<text>` elements."
            )
        if cjk_heading:
            parts.append(
                f"- **CJK heading font**: Use `{cjk_heading}` for CJK heading `<text>` elements."
            )
        if cjk_body:
            parts.append(
                f"- **CJK body font**: Use `{cjk_body}` for CJK body `<text>` elements."
            )
    elif font:
        parts.append(
            f"- **Font override**: Use this font-family for every SVG `<text>` element: `{font}`."
        )
    if density:
        density_hint = {
            "compact": "Prefer dense but legible layouts: smaller paddings, tighter line heights, more items per slide.",
            "normal": "Keep balanced spacing and default density.",
            "spacious": "Use generous whitespace, larger margins, fewer items per slide.",
        }.get(str(density), "")
        parts.append(f"- **Density override** (`{density}`): {density_hint}")
    if not parts:
        return ""
    return "## Style Overrides (must follow)\n" + "\n".join(parts)


async def _save_feedback_history(project_dir: Path, history: list[str]) -> None:
    """Append-write feedback_history.json for auditability."""
    path = project_dir / "feedback_history.json"
    await awrite_text(
        path,
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def _save_critic_history(
    project_dir: Path,
    critic_events: list[dict],
    *,
    refine: bool = False,
) -> None:
    """Persist critic events to critic_history.json for post-run analysis."""
    path = project_dir / "critic_history.json"
    existing: dict = {}
    if path.exists():
        try:
            text = await aread_text(path, encoding="utf-8")
            existing = json.loads(text)
        except (json.JSONDecodeError, OSError):
            existing = {}

    key = "refine_events" if refine else "generation_events"
    existing[key] = critic_events
    existing["updated_at"] = datetime.now().isoformat()
    if "created_at" not in existing:
        existing["created_at"] = existing["updated_at"]

    await awrite_text(
        path,
        json.dumps(existing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _seed_svg_output_for_targeted_refine(project_dir: Path, target_pages: list[int]) -> None:
    """Seed svg_output/ with the latest stable deck before partial regeneration."""
    import shutil

    source_dir = project_dir / "svg_final"
    if not source_dir.exists() or not any(source_dir.glob("*.svg")):
        source_dir = project_dir / "svg_output"
    if not source_dir.exists():
        return

    output_dir = project_dir / "svg_output"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    for svg_path in sorted(source_dir.glob("*.svg")):
        shutil.copy2(svg_path, output_dir / svg_path.name)

    for page in target_pages:
        for existing in output_dir.glob(f"{page:02d}_*.svg"):
            existing.unlink(missing_ok=True)
