"""Template listing and selection endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.generator.template_manager import list_templates, load_template

router = APIRouter(prefix="/templates")


class TemplateInfoResponse(BaseModel):
    template_id: str
    label: str = ""
    summary: str = ""
    tone: str = ""
    theme_mode: str = ""
    category: str = ""
    keywords: list[str] = []
    source: str = "builtin"
    import_mode: str = "builtin"
    editable: bool = False
    slide_count: int = 0
    has_cover: bool = False
    has_chapter: bool = False
    has_content: bool = False
    has_ending: bool = False
    has_toc: bool = False


class TemplateDetailResponse(TemplateInfoResponse):
    content_area: dict[str, int] = {}
    has_cover: bool = False
    has_chapter: bool = False
    has_content: bool = False
    has_ending: bool = False
    has_toc: bool = False


@router.get("", response_model=list[TemplateInfoResponse])
async def get_templates():
    """List all available templates."""
    templates = list_templates()
    return [
        TemplateInfoResponse(
            template_id=t.template_id,
            label=t.label,
            summary=t.summary,
            tone=t.tone,
            theme_mode=t.theme_mode,
            category=t.category,
            keywords=t.keywords,
            source=t.source,
            import_mode=t.import_mode,
            editable=t.editable,
            slide_count=t.slide_count,
            has_cover=t.has_cover,
            has_chapter=t.has_chapter,
            has_content=t.has_content,
            has_ending=t.has_ending,
            has_toc=t.has_toc,
        )
        for t in templates
    ]


@router.get("/{template_id}", response_model=TemplateDetailResponse)
async def get_template_detail(template_id: str):
    """Get detailed information about a specific template."""
    tmpl = load_template(template_id)
    if tmpl is None:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")
    return TemplateDetailResponse(
        template_id=tmpl.info.template_id,
        label=tmpl.info.label,
        summary=tmpl.info.summary,
        tone=tmpl.info.tone,
        theme_mode=tmpl.info.theme_mode,
        category=tmpl.info.category,
        keywords=tmpl.info.keywords,
        source=tmpl.info.source,
        import_mode=tmpl.info.import_mode,
        editable=tmpl.info.editable,
        slide_count=tmpl.info.slide_count,
        content_area=tmpl.content_area,
        has_cover=bool(tmpl.cover_svg),
        has_chapter=bool(tmpl.chapter_svg),
        has_content=bool(tmpl.content_svg),
        has_ending=bool(tmpl.ending_svg),
        has_toc=bool(tmpl.toc_svg),
    )
