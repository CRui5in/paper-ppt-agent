"""Vision capability probe for LLM models.

Sends a small JPEG test image to each configured model to determine whether
it supports multimodal (vision) input. Results are cached in memory so each
model is only probed once per process lifetime.
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Words that indicate the model correctly identified the red square.
_EXPECTED_COLORS = frozenset({
    "red", "crimson", "scarlet", "ruby", "cherry", "vermilion",
})
# Words that indicate the model cannot see images.
_NO_VISION_INDICATORS = frozenset({
    "cannot see", "can't see", "no image", "unable to view",
    "not able to see", "don't see", "no picture",
})

_VISION_SYSTEM = "You are a helpful vision assistant. Describe images accurately."
_VISION_PROMPT = "What color is the square in this image? Reply with just the color name."

# Thread-safe lazy-initialized test image.
_test_image_lock = threading.Lock()
_test_image_cache: bytes | None = None


def _get_test_image() -> bytes:
    """Generate a 64x64 red-square JPEG on first use (thread-safe)."""
    global _test_image_cache
    if _test_image_cache is not None:
        return _test_image_cache
    with _test_image_lock:
        if _test_image_cache is not None:
            return _test_image_cache
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (64, 64), color=(255, 255, 255))
        ImageDraw.Draw(img).rectangle([8, 8, 56, 56], fill=(255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        _test_image_cache = buf.getvalue()
    return _test_image_cache


@dataclass
class ModelVisionResult:
    """Result of a vision probe for a single model."""

    model: str
    supports_vision: bool
    tested_at: float = 0.0
    error: str | None = None
    response_excerpt: str = ""


def _is_vision_response(content: str) -> bool:
    """Check if a response indicates the model actually saw the image."""
    if not content:
        return False
    # If the model explicitly says it can't see, it's not vision-capable.
    if any(indicator in content for indicator in _NO_VISION_INDICATORS):
        return False
    # If the response contains a color word, it's vision-capable.
    if any(color in content for color in _EXPECTED_COLORS):
        return True
    # Non-empty response without denial: likely vision-capable.
    return len(content) > 2


class VisionProbe:
    """Probes LLM models for vision support and caches results.

    Thread-safe: cache mutations are protected by a lock.
    """

    def __init__(
        self,
        *,
        max_retries: int = 3,
        probe_timeout: float = 20.0,
        retry_delay: float = 1.0,
    ) -> None:
        self._cache: dict[str, ModelVisionResult] = {}
        self._lock = threading.Lock()
        self._probed = False
        self._probe_complete = threading.Event()
        self.max_retries = max_retries
        self.probe_timeout = probe_timeout
        self.retry_delay = retry_delay

    def is_probed(self) -> bool:
        return self._probed

    def wait_probed(self, timeout: float | None = None) -> bool:
        """Block until probe_all() completes. Returns True if completed."""
        return self._probe_complete.wait(timeout=timeout)

    def get_result(self, model: str) -> ModelVisionResult | None:
        with self._lock:
            return self._cache.get(model.strip().lower())

    def supports_vision(self, model: str) -> bool | None:
        """True/False if probed, None if unknown."""
        r = self.get_result(model)
        return r.supports_vision if r else None

    def get_vision_model(self, exclude: str = "") -> str | None:
        """Return a model known to support vision (prefers settings.vision_model)."""
        from backend.config import settings

        exclude_lower = exclude.strip().lower()
        # Snapshot the cache to avoid iteration during concurrent mutation.
        with self._lock:
            cache_snapshot = dict(self._cache)

        # Prefer the explicitly configured vision_model.
        preferred = (settings.vision_model or "").strip()
        if preferred:
            r = cache_snapshot.get(preferred.lower())
            if r and r.supports_vision and preferred.lower() != exclude_lower:
                return r.model

        # Fall back to any vision-capable model in the cache.
        for key, r in cache_snapshot.items():
            if r.supports_vision and key != exclude_lower:
                return r.model
        return None

    def all_results(self) -> dict[str, ModelVisionResult]:
        with self._lock:
            return dict(self._cache)

    def reset(self) -> None:
        """Clear all cached probe results (useful for testing)."""
        with self._lock:
            self._cache.clear()
            self._probed = False
            self._probe_complete.clear()

    async def probe_model(self, model: str, llm: Any) -> ModelVisionResult:
        key = model.strip().lower()
        with self._lock:
            if key in self._cache:
                return self._cache[key]

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
            last_error: Exception | None = None
            for attempt in range(self.max_retries):
                try:
                    response = await asyncio.wait_for(
                        llm.chat(messages, model, temperature=0.0, max_tokens=32),
                        timeout=self.probe_timeout,
                    )
                    content = (response.content or "").strip().lower()
                    if content:
                        break
                except Exception as retry_exc:
                    last_error = retry_exc
                    err_text = str(retry_exc).lower()
                    # 404 or timeout: definitively no vision support, stop retrying.
                    if any(kw in err_text for kw in ("404", "not found", "timeout")):
                        raise retry_exc
                    logger.debug(
                        "Vision probe [%s] attempt %d error: %s",
                        model, attempt + 1, str(retry_exc)[:80],
                    )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay)

            supports = _is_vision_response(content)
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

        with self._lock:
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
        self._probe_complete.set()

        with self._lock:
            supported = [r.model for r in self._cache.values() if r.supports_vision]
            not_supported = [r.model for r in self._cache.values() if not r.supports_vision]
        logger.info(
            "Vision probe complete: %d supported %s, %d not supported %s",
            len(supported),
            supported,
            len(not_supported),
            not_supported,
        )
        return self.all_results()


vision_probe = VisionProbe()
