"""Icon semantic search using pre-built Gemini Embedding 2 index.

Provides fast cosine-similarity search over all icon libraries.
Index files (index.npz + index_meta.json) are pre-built and committed to Git.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

EMBED_MODEL = "gemini-embedding-2"
EMBED_DIM = 768
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in _TOKEN_RE.findall(text.lower().replace("-", " "))
        if len(token) >= 3
    }


def _icon_tokens(icon: dict) -> set[str]:
    parts = [
        str(icon.get("name") or ""),
        str(icon.get("category") or ""),
        " ".join(str(tag) for tag in icon.get("tags", []) or []),
        str(icon.get("text") or ""),
    ]
    return _tokens(" ".join(parts))


def _lexical_boost(query: str, icon: dict) -> float:
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0.0

    tokens = _icon_tokens(icon)
    overlap = query_tokens & tokens
    boost = min(0.16, 0.035 * len(overlap))

    query_lower = query.lower()
    name_phrase = str(icon.get("name") or "").replace("-", " ").lower()
    if name_phrase and name_phrase in query_lower:
        boost += 0.08

    for tag in icon.get("tags", []) or []:
        tag_text = str(tag).lower()
        if len(tag_text) >= 3 and tag_text in query_lower:
            boost += 0.04
            break

    return min(boost, 0.24)


def _np():
    """Lazy import numpy."""
    import numpy as np
    return np


def _genai():
    """Lazy import google.genai."""
    from google import genai
    from google.genai import types
    return genai, types


class IconIndex:
    """Pre-loaded icon vector index with semantic search."""

    def __init__(self, icons_dir: Path) -> None:
        self._icons_dir = icons_dir
        self._vectors = None  # np.ndarray | None
        self._meta: dict | None = None
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return

        npz_path = self._icons_dir / "index.npz"
        meta_path = self._icons_dir / "index_meta.json"

        if not npz_path.exists() or not meta_path.exists():
            logger.warning(
                "Icon index not found at %s. "
                "Run `python scripts/build_icon_index.py` to build it.",
                self._icons_dir,
            )
            self._loaded = True
            return

        np = _np()
        data = np.load(npz_path)
        self._vectors = data["vectors"].astype(np.float32, copy=False)
        norms = np.linalg.norm(self._vectors, axis=1, keepdims=True)
        self._vectors = np.divide(
            self._vectors,
            norms,
            out=self._vectors,
            where=norms > 0,
        )
        self._meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self._loaded = True
        logger.info(
            "Loaded icon index: %d icons, %d dimensions",
            self._meta["count"],
            self._meta["dimension"],
        )

    @property
    def is_loaded(self) -> bool:
        self._ensure_loaded()
        return self._vectors is not None

    @property
    def is_available(self) -> bool:
        """Alias for is_loaded."""
        return self.is_loaded

    def search(
        self,
        query: str,
        lib: str | None = None,
        k: int = 5,
    ) -> list[dict]:
        """Search icons by semantic similarity.

        Args:
            query: Natural language description of the desired icon.
            lib: Restrict to a specific library (chunk/tabler-filled/tabler-outline).
            k: Number of top results to return.

        Returns:
            List of dicts with keys: path, name, lib, category, tags, score.
            Empty list if index not loaded.
        """
        self._ensure_loaded()
        if self._vectors is None or self._meta is None:
            return []

        np = _np()

        # Embed query
        query_vec = self._embed_query(query)
        if query_vec is None:
            return []

        # Filter by library if specified
        icons = self._meta["icons"]
        if lib:
            mask = np.array([ic["lib"] == lib for ic in icons])
            indices = np.where(mask)[0]
            if len(indices) == 0:
                return []
            vecs = self._vectors[indices]
        else:
            indices = np.arange(len(icons))
            vecs = self._vectors

        # Cosine similarity plus a small lexical boost. The boost helps exact
        # icon-name/tag hits survive when the semantic embedding is close but
        # too generic for a large visual catalog.
        embedding_sims = vecs @ query_vec
        boosts = np.array(
            [_lexical_boost(query, icons[int(real_idx)]) for real_idx in indices],
            dtype=np.float32,
        )
        sims = embedding_sims + boosts

        # Top-k
        top_k_idx = np.argsort(sims)[-k:][::-1]

        results = []
        for idx in top_k_idx:
            real_idx = indices[idx]
            ic = icons[real_idx]
            results.append({
                "path": ic["path"],
                "name": ic["name"],
                "lib": ic["lib"],
                "category": ic.get("category", ""),
                "tags": ic.get("tags", []),
                "score": float(sims[idx]),
                "embedding_score": float(embedding_sims[idx]),
                "lexical_boost": float(boosts[idx]),
            })

        return results

    def _embed_query(self, query: str):
        """Embed a search query using Gemini Embedding 2. Returns np.ndarray or None."""
        np = _np()
        try:
            genai, types = _genai()
            client = genai.Client()
            formatted = (
                "Find a simple presentation pictogram/icon for this slide concept. "
                "Prefer concise visual metaphors that can anchor a callout, process "
                f"step, metric, warning, or method card. Query: {query}"
            )
            result = client.models.embed_content(
                model=EMBED_MODEL,
                contents=formatted,
                config=types.EmbedContentConfig(output_dimensionality=EMBED_DIM),
            )
            vec = np.array(result.embeddings[0].values, dtype=np.float32)
            # Normalize (gemini-embedding-2 auto-normalizes, but ensure)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec /= norm
            return vec
        except Exception:
            logger.exception("Failed to embed icon query: %s", query)
            return None

    def get_all_icons(self, lib: str | None = None) -> list[dict]:
        """Return all icon metadata, optionally filtered by library."""
        self._ensure_loaded()
        if self._meta is None:
            return []

        icons = self._meta["icons"]
        if lib:
            return [ic for ic in icons if ic["lib"] == lib]
        return icons


# Module-level singleton
_icon_index: IconIndex | None = None


def get_icon_index(icons_dir: Path | None = None) -> IconIndex:
    """Get or create the global IconIndex instance."""
    global _icon_index
    if _icon_index is None:
        if icons_dir is None:
            from backend.config import settings
            icons_dir = settings.icons_dir
        _icon_index = IconIndex(icons_dir)
    return _icon_index
