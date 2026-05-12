"""Online image search for slide content.

Searches for matching images via Tavily or SerpAPI (Google Images),
downloads results, and saves them to the project's images/ directory.

Used by the post-generation image replacement API (Phase B).
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.api.schemas import ResearchConfig

logger = logging.getLogger(__name__)


@dataclass
class ImageSearchResult:
    """A single image search result."""

    url: str
    thumbnail: str = ""
    description: str = ""
    source: str = ""  # "tavily" | "serpapi"

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "thumbnail": self.thumbnail,
            "description": self.description,
            "source": self.source,
        }


# ── Public API ────────────────────────────────────────────────────────────────


async def search_images(
    query: str,
    config: ResearchConfig,
    max_results: int = 8,
) -> list[ImageSearchResult]:
    """Search for images using Tavily (preferred) or SerpAPI (fallback).

    Returns a list of ImageSearchResult with URLs and thumbnails.
    """
    tavily_key = (config.tavily_api_key or "").strip()
    serpapi_key = (config.serpapi_key or "").strip()

    if not tavily_key and not serpapi_key:
        logger.warning("No Tavily or SerpAPI key configured for image search")
        return []

    try:
        import httpx  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("httpx not installed; image search unavailable")
        return []

    if tavily_key:
        return await _search_images_tavily(httpx, query, tavily_key, max_results)
    else:
        return await _search_images_serpapi(httpx, query, serpapi_key, max_results)


async def download_image(
    url: str,
    output_path: Path,
    max_retries: int = 3,
) -> Path | None:
    """Download an image from URL, validate format, convert webp->png.

    Returns the path to the saved file, or None on failure.
    """
    import asyncio

    try:
        import httpx  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError as e:
        logger.warning("Required package not installed: %s", e)
        return None

    output_path.parent.mkdir(parents=True, exist_ok=True)
    ext_format_map = Image.registered_extensions()

    for attempt in range(max_retries):
        try:
            if attempt > 0:
                await asyncio.sleep(attempt)

            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=15.0,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.read()

            # Validate and normalize image
            suffix = output_path.suffix.lower()
            try:
                with Image.open(io.BytesIO(data)) as img:
                    img.load()
                    save_format = ext_format_map.get(suffix, img.format)

                    # Convert webp to png for better compatibility
                    if img.format == "WEBP" or suffix == ".webp":
                        output_path = output_path.with_suffix(".png")
                        save_format = "PNG"

                    img.save(output_path, format=save_format)
                    w, h = img.size
                    logger.info(
                        "Downloaded image: %s (%dx%d)", output_path.name, w, h
                    )
                    return output_path
            except Exception:
                # Not a valid image, try saving raw bytes
                with open(output_path, "wb") as f:
                    f.write(data)
                return output_path

        except Exception as e:
            logger.warning(
                "Download attempt %d failed for %s: %s", attempt + 1, url[:80], e
            )

    logger.error("Failed to download image after %d attempts: %s", max_retries, url[:80])
    return None


# ── Tavily Image Search ──────────────────────────────────────────────────────


async def _search_images_tavily(
    httpx_mod: Any,
    query: str,
    api_key: str,
    max_results: int,
) -> list[ImageSearchResult]:
    """Search images via Tavily API."""
    async with httpx_mod.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "query": query,
                "api_key": api_key,
                "max_results": max_results,
                "include_images": True,
                "include_image_descriptions": True,
                "include_text": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        results: list[ImageSearchResult] = []
        for img in data.get("images", [])[:max_results]:
            if isinstance(img, dict):
                url = img.get("url", "") or ""
                if url:
                    results.append(
                        ImageSearchResult(
                            url=url,
                            description=img.get("description") or "",
                            source="tavily",
                        )
                    )
            elif isinstance(img, str) and img:
                results.append(ImageSearchResult(url=img, source="tavily"))

        logger.info("Tavily image search: %d results for '%s'", len(results), query[:50])
        return results


# ── SerpAPI Image Search ─────────────────────────────────────────────────────


async def _search_images_serpapi(
    httpx_mod: Any,
    query: str,
    api_key: str,
    max_results: int,
) -> list[ImageSearchResult]:
    """Search images via SerpAPI Google Images engine."""
    async with httpx_mod.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            "https://serpapi.com/search",
            params={
                "engine": "google_images",
                "q": query,
                "api_key": api_key,
                "num": max_results,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        results: list[ImageSearchResult] = []
        for item in data.get("images_results", [])[:max_results]:
            url = item.get("original") or item.get("link", "") or ""
            if not url:
                continue
            results.append(
                ImageSearchResult(
                    url=url,
                    thumbnail=item.get("thumbnail") or "",
                    description=item.get("title") or query,
                    source="serpapi",
                )
            )

        logger.info("SerpAPI image search: %d results for '%s'", len(results), query[:50])
        return results


# ── SVG Image Insertion ──────────────────────────────────────────────────────


def svg_has_image_elements(svg_content: str) -> bool:
    """Check if the SVG contains any <image> elements."""
    import re
    return bool(re.search(r"<image\b", svg_content, re.IGNORECASE))


def replace_image_in_svg(
    svg_content: str,
    new_href: str,
    target_element: str | None = None,
) -> str:
    """Replace <image> href attributes in SVG content.

    Returns the modified SVG string.
    """
    import re

    image_pattern = re.compile(
        r'(<image\b[^>]*\b)href="([^"]+)"([^>]*>)', re.IGNORECASE
    )

    if target_element:
        def _replace_specific(match: re.Match) -> str:
            if target_element in match.group(0):
                return f'{match.group(1)}href="{new_href}"{match.group(3)}'
            return match.group(0)

        return image_pattern.sub(_replace_specific, svg_content)
    else:
        return image_pattern.sub(
            lambda m: f'{m.group(1)}href="{new_href}"{m.group(3)}',
            svg_content,
            count=1,
        )


async def auto_insert_image(
    svg_content: str,
    image_href: str,
    image_description: str,
    image_path: Path | None,
    provider: str,
    model: str,
    api_key: str,
    base_url: str | None = None,
) -> str:
    """Use LLM to analyze SVG and insert an image with proper layout.

    Gets image dimensions from the downloaded file, analyzes existing SVG
    layout (text positions, empty space), and generates a modified SVG
    that integrates the image naturally.

    Returns the modified SVG string.
    """
    from backend.llm import create_provider
    from backend.llm.types import LLMMessage

    import re

    # --- Analyze canvas ---
    viewbox_match = re.search(r'viewBox="([^"]+)"', svg_content)
    canvas_w, canvas_h = 1280, 720
    if viewbox_match:
        parts = viewbox_match.group(1).split()
        if len(parts) == 4:
            canvas_w, canvas_h = int(float(parts[2])), int(float(parts[3]))

    # --- Analyze image dimensions ---
    img_w, img_h = 0, 0
    img_ratio = 1.0
    if image_path and image_path.exists():
        try:
            from PIL import Image
            with Image.open(image_path) as img:
                img_w, img_h = img.size
                img_ratio = img_w / img_h if img_h > 0 else 1.0
        except Exception:
            pass

    # --- Analyze existing SVG layout ---
    # Find existing <image> elements
    existing_images = re.findall(r"<image\b[^/]*/>", svg_content, re.IGNORECASE)
    # Find text positions to understand layout
    text_positions = re.findall(
        r"<text\b[^>]*\bx=\"([\d.]+)\"\s+y=\"([\d.]+)\"", svg_content
    )
    text_xs = [float(x) for x, _ in text_positions]
    text_ys = [float(y) for _, y in text_positions]

    # Determine content region occupied by text
    text_right = max(text_xs) if text_xs else canvas_w * 0.5
    text_bottom = max(text_ys) if text_ys else canvas_h * 0.5

    # Check for structural elements (rects that might be cards)
    card_rects = re.findall(
        r"<rect\b[^>]*\bx=\"([\d.]+)\"\s+y=\"([\d.]+)\"\s+width=\"([\d.]+)\"\s+height=\"([\d.]+)\"",
        svg_content,
    )

    layout_analysis = f"""Canvas: {canvas_w}x{canvas_h}
Image to insert: {img_w}x{img_h} (ratio {img_ratio:.2f})
Image description: {image_description}
Existing <image> elements: {len(existing_images)}
Text extends to approximately x={text_right:.0f}, y={text_bottom:.0f}
Found {len(card_rects)} rectangular card elements"""

    system_prompt = f"""You are an expert SVG layout designer for presentation slides. Your task is to insert a new image into an existing SVG slide while maintaining visual balance and readability.

## Hard Rules
1. Output the COMPLETE SVG — every original element must remain, plus the new image
2. Canvas viewBox is "0 0 {canvas_w} {canvas_h}" — NEVER change it
3. All content MUST stay within x=40, y=100, width=1200, height=520 (the content area)
4. NEVER move, resize, or modify existing text elements
5. Use `preserveAspectRatio="xMidYMid slice"` on the image element

## Layout Strategy — pick the best one:

**Strategy A: Split layout** — If the SVG has text occupying the LEFT half (text x < {canvas_w // 2}), place the image on the RIGHT side:
- x = max(text_right + 30, {canvas_w * 0.6:.0f})
- y = 120, width = {canvas_w * 0.35:.0f}, height = width / ratio

**Strategy B: Top image** — If text is concentrated in the BOTTOM half (text y > {canvas_h // 2}), place the image at the TOP:
- Full width or 70% width, centered
- y = 110, height = min(300, {canvas_h * 0.4:.0f}), width = height × ratio

**Strategy C: Replace placeholder** — If there's an existing `<image>` with a placeholder href, replace ONLY its href

**Strategy D: Background** — If the slide is mostly decorative (few text elements), use the image as a semi-transparent background:
- Full canvas, with opacity 0.2 and a white overlay rect for text readability

## After placing the image, adjust surrounding elements:
- If the image overlaps with any `<rect>` card element, make that card narrower or shift it slightly
- Add a subtle shadow or border-radius (clipPath) for polish if it's a content image
- Keep the same color scheme — don't add new colors"""

    user_prompt = f"""{layout_analysis}

Current SVG:
{svg_content}

Insert the image at href="{image_href}" and return the complete modified SVG. Output ONLY the SVG code."""

    try:
        llm = create_provider(provider, api_key, base_url=base_url)
        response = await llm.chat(
            [LLMMessage.system(system_prompt), LLMMessage.user(user_prompt)],
            model=model,
            temperature=0.2,
            max_tokens=16384,
        )

        modified = response.content.strip()

        # Extract SVG from response if wrapped in markdown code block
        if "```" in modified:
            svg_match = re.search(r"```(?:svg|xml)?\s*\n?(.*?)```", modified, re.DOTALL)
            if svg_match:
                modified = svg_match.group(1).strip()

        # Validate it looks like SVG
        if "<svg" in modified and "</svg>" in modified:
            logger.info("LLM successfully inserted image into SVG")
            return modified
        else:
            logger.warning("LLM response doesn't look like valid SVG, falling back")
            return _insert_image_fallback(svg_content, image_href, img_ratio, canvas_w, canvas_h)

    except Exception as e:
        logger.warning("LLM image insertion failed: %s, using fallback", e)
        return _insert_image_fallback(svg_content, image_href, img_ratio, canvas_w, canvas_h)


def _insert_image_fallback(
    svg_content: str,
    image_href: str,
    img_ratio: float = 1.5,
    canvas_w: int = 1280,
    canvas_h: int = 720,
) -> str:
    """Heuristic fallback: analyze text layout and place image in the emptiest region."""
    import re

    # Find text x positions to determine if content is left-aligned
    text_xs = [float(x) for x in re.findall(r'<text\b[^>]*\bx="([\d.]+)"', svg_content)]
    avg_text_x = sum(text_xs) / len(text_xs) if text_xs else canvas_w * 0.35

    # Content area bounds
    content_left = 40
    content_top = 100
    content_w = 1200
    content_h = 520

    if avg_text_x < canvas_w * 0.5:
        # Text is on the LEFT → place image on the RIGHT
        img_w = int(content_w * 0.35)
        img_h = int(img_w / img_ratio)
        img_x = int(canvas_w * 0.62)
        img_y = content_top + 20
    else:
        # Text is on the RIGHT → place image on the LEFT
        img_w = int(content_w * 0.35)
        img_h = int(img_w / img_ratio)
        img_x = content_left + 20
        img_y = content_top + 20

    # Clamp height
    max_h = content_h - 40
    if img_h > max_h:
        img_h = max_h
        img_w = int(img_h * img_ratio)

    clip_id = "search-img-clip"
    image_element = (
        f'\n  <defs><clipPath id="{clip_id}">'
        f'<rect x="{img_x}" y="{img_y}" width="{img_w}" height="{img_h}" '
        f'rx="8" ry="8"/></clipPath></defs>\n'
        f'  <image href="{image_href}" '
        f'x="{img_x}" y="{img_y}" '
        f'width="{img_w}" height="{img_h}" '
        f'preserveAspectRatio="xMidYMid slice" '
        f'clip-path="url(#{clip_id})"/>\n'
    )

    return svg_content.replace("</svg>", f"{image_element}</svg>")


# ── SVG Backup / Undo ────────────────────────────────────────────────────────


def backup_svg(svg_path: Path) -> Path | None:
    """Create a .bak backup of an SVG file before modification.

    Returns the backup path, or None on failure.
    """
    import shutil

    backup_path = svg_path.with_suffix(svg_path.suffix + ".bak")
    try:
        shutil.copy2(svg_path, backup_path)
        logger.info("Backed up SVG: %s", backup_path.name)
        return backup_path
    except OSError as e:
        logger.warning("Failed to backup SVG: %s", e)
        return None


def restore_svg_backup(svg_path: Path) -> bool:
    """Restore an SVG from its .bak backup.

    Returns True if restored successfully.
    """
    backup_path = svg_path.with_suffix(svg_path.suffix + ".bak")
    if not backup_path.exists():
        logger.warning("No backup found: %s", backup_path)
        return False
    try:
        import shutil
        shutil.copy2(backup_path, svg_path)
        backup_path.unlink()
        logger.info("Restored SVG from backup: %s", svg_path.name)
        return True
    except OSError as e:
        logger.warning("Failed to restore SVG backup: %s", e)
        return False


def _guess_extension(url: str) -> str:
    """Guess file extension from URL."""
    clean_url = url.split("?")[0].split("#")[0].lower()
    for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"]:
        if clean_url.endswith(ext):
            return ext
    return ".png"  # default
