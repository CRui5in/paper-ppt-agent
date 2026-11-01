"""Visual critic — render SVG to PNG and ask a multimodal LLM to inspect it.

Complements the static `svg_critic` by catching semantic and layout issues
that XML inspection cannot see: overall composition, visual monotony, decorative
clutter, figure/content mismatch, accidental overlap of icons with text, etc.

The flow per page:

    SVG ──(resvg)──► PNG (in-memory) ──(VLM)──► structured report

The report is shaped to match `svg_critic.CriticReport.to_prompt_block()` so the
existing repair loop in `svg_executor` can consume it without refactoring.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from backend.generator.svg_critic import CriticReport, Violation
from backend.llm.base import LLMProvider
from backend.llm.types import LLMMessage

logger = logging.getLogger(__name__)


@dataclass
class VisualCriticConfig:
    # Render width in pixels. Resvg picks the height to preserve viewBox ratio.
    render_width: int = 1280

    # Cap report size to avoid prompt bloat.
    max_violations: int = 8

    # If True, visual warnings turn into blocking violations (force repair).
    warnings_are_blocking: bool = False

    # JPEG quality for VLM payload. PNG kept by default (lossless).
    use_jpeg: bool = True
    jpeg_quality: int = 80


def render_svg_to_png(svg_content: str, width: int = 1280) -> bytes | None:
    """Render an SVG string to PNG bytes via resvg-py.

    Returns None when resvg is unavailable or rendering fails — callers must
    treat this as "skip visual QA" rather than a hard error.
    """
    try:
        from resvg_py import svg_to_bytes  # type: ignore[import-not-found]
    except ImportError:
        logger.info("resvg_py not installed; visual critic disabled")
        return None

    try:
        # resvg-py 0.2.x signature: svg_to_bytes(svg_string=..., width=...)
        png_bytes = svg_to_bytes(svg_string=svg_content, width=width)
        if isinstance(png_bytes, list):
            # Some versions return a list of ints; convert to bytes.
            png_bytes = bytes(png_bytes)
        return png_bytes
    except Exception as exc:  # noqa: BLE001 - any rendering failure should skip QA
        logger.warning("resvg rendering failed, skipping visual critic: %s", exc)
        return None


def _maybe_to_jpeg(png_bytes: bytes, quality: int = 80) -> tuple[bytes, str]:
    """Convert PNG → JPEG to shrink the VLM payload. Falls back to PNG."""
    try:
        from io import BytesIO

        from PIL import Image
    except ImportError:
        return png_bytes, "image/png"

    try:
        img = Image.open(BytesIO(png_bytes)).convert("RGB")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001
        return png_bytes, "image/png"


_VLM_SYSTEM = """Review the rendered presentation slide at normal slideshow size.

Evaluate:
- Legibility: text size, contrast, line length, and complete visible labels.
- Geometry: text collisions, clipping, container overflow, and alignment of related elements.
- Reading order: grouping, hierarchy, and the association between labels and visuals.
- Figures and charts: readable detail, complete content, accurate visual scales, and correspondence with the title and caption.

Report concrete visible defects with their location and a useful correction.
Use error for defects that impair reading or make a figure/chart misleading.
Use warning for visible problems that weaken comprehension while leaving the page usable.
Return an empty issues array when the page reads clearly.

Return strict JSON:
{
  "issues": [
    {
      "rule": "short_snake_case_id",
      "severity": "error" | "warning",
      "detail": "visible defect, location, and correction"
    }
  ]
}
"""


_USER_TEMPLATE = """Inspect this rendered slide image.

Page number: {page_num}
Page title (from manuscript): {page_title}
Style preset: {style}

Return strict JSON only — no markdown, no commentary.
"""


def _extract_json(text: str) -> dict | None:
    """Best-effort extraction of a JSON object from a model response."""
    text = text.strip()
    # Strip ``` code fences if present.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        # Grab the first {...} block.
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            text = brace.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


@dataclass
class VisualCheckOutcome:
    rendered: bool = False
    media_type: str | None = None
    rendered_image_bytes: bytes | None = None
    skipped_reason: str | None = None
    raw_response_excerpt: str | None = None
    report: CriticReport = field(
        default_factory=lambda: CriticReport(passed=True, violations=[])
    )


async def visual_check(
    svg_content: str,
    *,
    llm: LLMProvider,
    model: str,
    page_num: int,
    page_title: str = "",
    style: str = "academic",
    config: VisualCriticConfig | None = None,
) -> VisualCheckOutcome:
    """Render the SVG and ask a multimodal LLM to flag visual issues.

    Always returns a `VisualCheckOutcome`. When rendering or the VLM call
    fails for any reason, `rendered=False` and the report is empty/passed —
    callers should treat the failure as "skip" and not block generation.
    """
    cfg = config or VisualCriticConfig()
    outcome = VisualCheckOutcome()

    png = render_svg_to_png(svg_content, width=cfg.render_width)
    if png is None:
        outcome.skipped_reason = "render_failed_or_unavailable"
        return outcome

    image_bytes, media_type = (
        _maybe_to_jpeg(png, cfg.jpeg_quality) if cfg.use_jpeg else (png, "image/png")
    )
    outcome.rendered = True
    outcome.media_type = media_type
    outcome.rendered_image_bytes = image_bytes

    prompt = _USER_TEMPLATE.format(
        page_num=page_num,
        page_title=page_title or "(untitled)",
        style=style,
    )
    messages = [
        LLMMessage.system(_VLM_SYSTEM),
        LLMMessage.user_with_image(prompt, image_bytes, media_type=media_type),
    ]

    try:
        response = await llm.chat(messages, model, temperature=0.0, max_tokens=1024)
    except Exception as exc:  # noqa: BLE001 — provider may not support vision
        logger.info("Visual critic LLM call failed (likely text-only model): %s", exc)
        outcome.skipped_reason = "llm_call_failed"
        return outcome

    outcome.raw_response_excerpt = response.content
    parsed = _extract_json(response.content)
    if not parsed or not isinstance(parsed.get("issues"), list):
        logger.warning(
            "Visual critic returned non-JSON or unexpected shape; skipping. raw=%r",
            response.content[:200],
        )
        outcome.skipped_reason = "non_json_response"
        return outcome

    violations: list[Violation] = []
    for raw in parsed["issues"][: cfg.max_violations]:
        if not isinstance(raw, dict):
            continue
        rule = str(raw.get("rule") or "other")[:64]
        severity = raw.get("severity")
        if severity not in ("error", "warning"):
            severity = "warning"
        detail = str(raw.get("detail") or "").strip()
        if not detail:
            continue
        violations.append(
            Violation(
                rule=f"visual:{rule}",
                severity=severity,
                detail=detail,
            )
        )

    error_count = sum(1 for v in violations if v.severity == "error")
    warning_count = sum(1 for v in violations if v.severity == "warning")
    blocking = error_count > 0 or (cfg.warnings_are_blocking and warning_count > 0)
    outcome.report = CriticReport(passed=not blocking, violations=violations)
    return outcome
