"""Vision capability probe for LLM models.

Sends a small JPEG test image to each configured model to determine whether
it supports multimodal (vision) input.  Results are cached in memory so each
model is only probed once per process lifetime.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_EXPECTED_COLORS = {"red", "green", "blue", "white", "black", "yellow", "orange", "purple", "pink", "gray", "grey"}

_VISION_SYSTEM = "You are a helpful vision assistant. Describe images accurately."
_VISION_PROMPT = "What color is the square in this image? Reply with just the color name."
_TEST_IMAGE: bytes | None = None


def _get_test_image() -> bytes:
    """Generate a 64x64 red-square JPEG (≈5 KB) on first use."""
    global _TEST_IMAGE
    if _TEST_IMAGE is not None:
        return _TEST_IMAGE
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (64, 64), color=(255, 255, 255))
    ImageDraw.Draw(img).rectangle([8, 8, 56, 56], fill=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    _TEST_IMAGE = buf.getvalue()
    return _TEST_IMAGE


@dataclass
class ModelVisionResult:
    model: str
    supports_vision: bool
    tested_at: float = 0.0
    error: str | None = None
    response_excerpt: str = ""


_PROBE_TIMEOUT_SECONDS = 20.0


class VisionProbe:
    """Probes LLM models for vision support and caches results."""

    def __init__(self) -> None:
        self._cache: dict[str, ModelVisionResult] = {}
        self._probed = False

    def is_probed(self) -> bool:
        return self._probed

    def get_result(self, model: str) -> ModelVisionResult | None:
        return self._cache.get(model.strip().lower())

    def supports_vision(self, model: str) -> bool | None:
        """True/False if probed, None if unknown."""
        r = self.get_result(model)
        return r.supports_vision if r else None

    def get_vision_model(self, exclude: str = "") -> str | None:
        """Return a model known to support vision (prefers settings.vision_model)."""
        from backend.config import settings

        exclude_lower = exclude.strip().lower()
        # Prefer the explicitly configured vision_model
        preferred = (settings.vision_model or "").strip()
        if preferred:
            r = self._cache.get(preferred.lower())
            if r and r.supports_vision and preferred.lower() != exclude_lower:
                return r.model
        # Fall back to any vision-capable model in the cache
        for key, r in self._cache.items():
            if r.supports_vision and key != exclude_lower:
                return r.model
        return None

    def all_results(self) -> dict[str, ModelVisionResult]:
        return dict(self._cache)

    async def probe_model(self, model: str, llm: Any) -> ModelVisionResult:
        key = model.strip().lower()
        if key in self._cache:
            return self._cache[key]

        MAX_RETRIES = 10
        t0 = time.monotonic()
        try:
            from backend.llm.types import LLMMessage

            image_bytes = _get_test_image()
            messages = [
                LLMMessage.system(_VISION_SYSTEM),
                LLMMessage.user_with_image(
                    _VISION_PROMPT, image_bytes, media_type="image/jpeg"
                ),
            ]
            content = ""
            for attempt in range(MAX_RETRIES):
                try:
                    response = await asyncio.wait_for(
                        llm.chat(messages, model, temperature=0.0, max_tokens=32),
                        timeout=_PROBE_TIMEOUT_SECONDS,
                    )
                    content = (response.content or "").strip().lower()
                    if content:
                        break
                except Exception as retry_exc:
                    err_text = str(retry_exc).lower()
                    # 404 or timeout = definitively no vision support, stop retrying
                    if "404" in err_text or "not found" in err_text or "timeout" in err_text:
                        raise retry_exc
                    # Other errors: retry
                    logger.debug("Vision probe [%s] attempt %d error: %s", model, attempt + 1, str(retry_exc)[:80])
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(1.0)
            # Validate that the model actually "saw" the image by checking
            # whether its answer contains the expected color word.
            supports = any(c in content for c in _EXPECTED_COLORS)
            result = ModelVisionResult(
                model=model,
                supports_vision=supports,
                tested_at=time.time(),
                response_excerpt=content[:100],
            )
            logger.info(
                "Vision probe [%s]: %s (%.1fs) response=%r",
                model,
                "SUPPORTED" if supports else "NOT supported",
                time.monotonic() - t0,
                content[:60],
            )
        except Exception as exc:
            result = ModelVisionResult(
                model=model,
                supports_vision=False,
                tested_at=time.time(),
                error=str(exc)[:200],
            )
            logger.info(
                "Vision probe [%s]: FAILED (%.1fs) error=%s",
                model,
                time.monotonic() - t0,
                str(exc)[:80],
            )
        self._cache[key] = result
        return result

    async def probe_all(
        self,
        models: list[str],
        llm: Any,
    ) -> dict[str, ModelVisionResult]:
        for m in models:
            await self.probe_model(m, llm)
        self._probed = True
        supported = [r.model for r in self._cache.values() if r.supports_vision]
        not_supported = [r.model for r in self._cache.values() if not r.supports_vision]
        logger.info(
            "Vision probe complete: %d supported %s, %d not supported %s",
            len(supported),
            supported,
            len(not_supported),
            not_supported,
        )
        return self._cache


vision_probe = VisionProbe()
