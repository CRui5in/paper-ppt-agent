"""Build icon embedding index using Gemini Embedding 2.

Run once to generate assets/icons/index.npz + index_meta.json.
Requires GEMINI_API_KEY environment variable.

Usage:
    python scripts/build_icon_index.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import numpy as np
from google import genai
from google.genai import types

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ICONS_DIR = PROJECT_ROOT / "assets" / "icons"
INDEX_NPZ = ICONS_DIR / "index.npz"
INDEX_META = ICONS_DIR / "index_meta.json"

EMBED_MODEL = "gemini-embedding-2"
EMBED_DIM = 768
BATCH_SIZE = 100  # icons per API call

# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)
_TAGS_RE = re.compile(r"tags:\s*\[(.*?)\]")
_CATEGORY_RE = re.compile(r"category:\s*(.+)")


def _parse_svg_comment(svg_path: Path) -> dict:
    """Extract tags and category from SVG HTML comment."""
    try:
        text = svg_path.read_text(encoding="utf-8")
    except Exception:
        return {}

    m = _COMMENT_RE.search(text)
    if not m:
        return {}

    comment = m.group(1)
    result = {}

    tm = _TAGS_RE.search(comment)
    if tm:
        result["tags"] = [t.strip().strip('"').strip("'") for t in tm.group(1).split(",")]

    cm = _CATEGORY_RE.search(comment)
    if cm:
        result["category"] = cm.group(1).strip().strip('"')

    return result


def _name_to_words(name: str) -> list[str]:
    """Split kebab-case filename into words."""
    return [w for w in name.replace(".svg", "").split("-") if w]


def _build_text(icon: dict) -> str:
    """Build embedding text for one icon."""
    name = icon["name"]
    name_words = " ".join(_name_to_words(name))
    parts = [
        f"presentation icon path: {icon['path']}",
        f"icon name: {name}",
    ]
    if name_words and name_words != name:
        parts.append(f"icon words: {name_words}")
    if icon.get("category"):
        parts.append(f"category: {icon['category']}")
    if icon.get("tags"):
        parts.append(f"tags: {', '.join(icon['tags'])}")
    parts.append(f"library: {icon['lib']}")
    parts.append(
        "usage: simple pictogram for slide callouts, process nodes, KPI labels, "
        "warnings, method cards, contribution cards, and compact visual anchors"
    )
    if name_words:
        parts.append(f"visual metaphor: {name_words}")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Icon collection
# ---------------------------------------------------------------------------

def collect_all_icons() -> list[dict]:
    """Collect metadata for all icons across all libraries."""
    icons = []

    # 1. tabler-outline — full metadata
    outline_dir = ICONS_DIR / "tabler-outline"
    outline_meta = {}  # name -> {tags, category}
    for svg in sorted(outline_dir.glob("*.svg")):
        name = svg.stem
        meta = _parse_svg_comment(svg)
        outline_meta[name] = meta
        icons.append({
            "lib": "tabler-outline",
            "name": name,
            "path": f"tabler-outline/{name}",
            "category": meta.get("category", ""),
            "tags": meta.get("tags", []),
        })

    # 2. tabler-filled — inherit from outline by name match
    filled_dir = ICONS_DIR / "tabler-filled"
    for svg in sorted(filled_dir.glob("*.svg")):
        name = svg.stem
        inherited = outline_meta.get(name, {})
        icons.append({
            "lib": "tabler-filled",
            "name": name,
            "path": f"tabler-filled/{name}",
            "category": inherited.get("category", ""),
            "tags": inherited.get("tags", []),
        })

    # 3. chunk — no metadata, generate from filename
    chunk_dir = ICONS_DIR / "chunk"
    for svg in sorted(chunk_dir.glob("*.svg")):
        name = svg.stem
        words = _name_to_words(name)
        icons.append({
            "lib": "chunk",
            "name": name,
            "path": f"chunk/{name}",
            "category": "",
            "tags": words,
        })

    return icons


# ---------------------------------------------------------------------------
# Embedding generation
# ---------------------------------------------------------------------------

def _embed_single(client: genai.Client, text: str, retries: int = 3) -> list[float]:
    """Embed a single text, return vector."""
    for attempt in range(retries):
        try:
            result = client.models.embed_content(
                model=EMBED_MODEL,
                contents=text,
                config=types.EmbedContentConfig(output_dimensionality=EMBED_DIM),
            )
            return list(result.embeddings[0].values)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 2 ** (attempt + 1)
                print(f"    Retry {attempt + 1}/{retries} after {wait}s: {exc}")
                time.sleep(wait)
            else:
                raise


def build_index(icons: list[dict]) -> np.ndarray:
    """Generate embeddings for all icons and return as numpy array.

    Note: gemini-embedding-2 aggregates multiple inputs into one vector,
    so we must embed one at a time.
    """
    client = genai.Client()
    texts = [_build_text(icon) for icon in icons]

    all_vectors: list[list[float]] = []
    total = len(texts)
    for i, text in enumerate(texts):
        if (i + 1) % 200 == 0 or i == 0:
            print(f"  Embedding {i + 1}/{total} ...")
        vec = _embed_single(client, text)
        all_vectors.append(vec)

    return np.array(all_vectors, dtype=np.float32)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_index(icons: list[dict], vectors: np.ndarray) -> None:
    """Quick sanity check: self-similarity should be ~1.0."""
    print("\nVerification (self-similarity test):")
    # Pick 3 random icons and check their self-similarity
    import random
    random.seed(42)
    samples = random.sample(range(len(icons)), min(3, len(icons)))
    for idx in samples:
        vec = vectors[idx]
        sim = float(np.dot(vec, vec))
        print(f"  [{icons[idx]['lib']}] {icons[idx]['path']} self-sim = {sim:.4f} (expect ~1.0)")
        assert sim > 0.99, f"Self-similarity too low: {sim}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=== Icon Index Builder ===\n")

    # 1. Collect
    print("Collecting icon metadata...")
    icons = collect_all_icons()
    lib_counts = {}
    for ic in icons:
        lib_counts[ic["lib"]] = lib_counts.get(ic["lib"], 0) + 1
    for lib, count in sorted(lib_counts.items()):
        print(f"  {lib}: {count}")
    print(f"  Total: {len(icons)}\n")

    # Tag coverage
    with_tags = sum(1 for ic in icons if ic.get("tags"))
    with_cat = sum(1 for ic in icons if ic.get("category"))
    print(f"  With tags: {with_tags}/{len(icons)} ({100 * with_tags / len(icons):.1f}%)")
    print(f"  With category: {with_cat}/{len(icons)} ({100 * with_cat / len(icons):.1f}%)\n")

    # 2. Embed
    print("Generating embeddings (Gemini Embedding 2, 768d)...")
    t0 = time.time()
    vectors = build_index(icons)
    elapsed = time.time() - t0
    print(f"  Done in {elapsed:.1f}s\n")

    # 3. Save
    print("Saving index...")
    np.savez(INDEX_NPZ, vectors=vectors)
    print(f"  {INDEX_NPZ} ({INDEX_NPZ.stat().st_size / 1024 / 1024:.1f} MB)")

    meta = {
        "version": 2,
        "model": EMBED_MODEL,
        "dimension": EMBED_DIM,
        "count": len(icons),
        "icons": [
            {
                "id": i,
                "lib": ic["lib"],
                "name": ic["name"],
                "path": ic["path"],
                "category": ic.get("category", ""),
                "tags": ic.get("tags", []),
                "text": _build_text(ic),
            }
            for i, ic in enumerate(icons)
        ],
    }
    INDEX_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  {INDEX_META} ({INDEX_META.stat().st_size / 1024:.1f} KB)\n")

    # 4. Verify
    verify_index(icons, vectors)

    print("\n=== Done ===")
    print("Remember to commit index.npz and index_meta.json to Git.")


if __name__ == "__main__":
    main()
