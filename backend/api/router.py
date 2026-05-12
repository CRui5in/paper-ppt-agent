"""Main API router."""

from fastapi import APIRouter

from .endpoints import download, font_update, generate, image_search, preview, providers, refine, session, status, template_import, templates, upload, usage, versions

api_router = APIRouter(prefix="/api")
api_router.include_router(upload.router, tags=["upload"])
api_router.include_router(generate.router, tags=["generate"])
api_router.include_router(refine.router, tags=["refine"])
api_router.include_router(status.router, tags=["status"])
api_router.include_router(download.router, tags=["download"])
api_router.include_router(font_update.router, tags=["fonts"])
api_router.include_router(preview.router, tags=["preview"])
api_router.include_router(providers.router, tags=["providers"])
api_router.include_router(session.router, tags=["session"])
api_router.include_router(templates.router, tags=["templates"])
api_router.include_router(template_import.router, tags=["template-import"])
api_router.include_router(usage.router, tags=["usage"])
api_router.include_router(versions.router, tags=["versions"])
api_router.include_router(image_search.router, tags=["image-search"])
