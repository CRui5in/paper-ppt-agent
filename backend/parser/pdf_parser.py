"""PDF paper parser using PyMuPDF.

Extraction strategy (layered, best-effort):

1. **Embedded raster images** — fast, via page.get_images().  Skips tiny
   decorative bitmaps (< MIN_IMG_PX on either side).

2. **Vector / mixed figure regions** — detects figure bounding boxes by
   locating caption text ("Figure N", "Fig.", "Fig N") and then rendering
   that page region at high DPI.  This captures charts, diagrams and any
   figure that is not stored as a plain raster inside the PDF.

3. **Whole-page screenshot fallback** — if a page appears to contain a
   figure block but neither of the above methods produced an image, the
   full page is rendered at standard DPI and saved as a fallback.

4. **Tables** — uses page.find_tables() when available (PyMuPDF ≥ 1.23).
   Extracted as Markdown so the research agent can read them.

Caption proximity matching assigns each extracted image to the nearest
caption/label found on the same page.

Font-size analysis drives heading-level detection (unchanged from v1).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

import fitz  # PyMuPDF

# PyMuPDF ≥ 1.27 ships an optional layout-analysis extension (ONNX-backed).
# Importing ``pymupdf.layout`` registers the hook into PyMuPDF so the
# built-in text/table extractors return structure-aware output. It's a
# strict upgrade — if the package isn't installed we silently fall back
# to the legacy heuristic parser.
try:  # pragma: no cover - optional dependency
    import pymupdf.layout  # noqa: F401 — side-effect import activates layout analyzer
except Exception:
    pass

try:  # pragma: no cover - optional dependency
    import pymupdf4llm as _pymupdf4llm
except Exception:  # pragma: no cover
    _pymupdf4llm = None  # type: ignore[assignment]

# PyMuPDF ≥ 1.24 prints an informational message on import suggesting the
# optional `pymupdf4llm` / `pymupdf_layout` package for richer layout
# analysis. Now that we've installed them (or at least tried to) we can
# silence the mupdf-level error channel for cleanliness.
try:  # pragma: no cover - defensive, API surface differs across versions
    fitz.TOOLS.mupdf_display_errors(False)  # type: ignore[attr-defined]
except Exception:
    pass

from backend.runtime import aoffload

from .base import PaperParser
from .paper_model import PaperFigure, PaperSection, PaperTable, ParsedPaper

if TYPE_CHECKING:
    pass

# ── tunables ─────────────────────────────────────────────────────────────────
MIN_IMG_PX = 80          # ignore embedded bitmaps smaller than this
RENDER_DPI = 150         # DPI for full-page renders
REGION_DPI = 200         # DPI for cropped figure-region renders
CAPTION_SEARCH_PX = 120  # vertical window above/below image rect to find caption
MAX_CAPTION_GAP_PX = 420
MIN_RENDERED_FIGURE_SIDE = 72
MIN_GRAPHIC_AREA_RATIO = 0.015
BODY_TEXT_CHARS_THRESHOLD = 140
LABEL_TEXT_CHARS_THRESHOLD = 80
# ─────────────────────────────────────────────────────────────────────────────

_CAPTION_RE = re.compile(
    r"^\s*(fig(?:ure)?\.?\s*\d+|table\s*\d+)\b",
    re.IGNORECASE,
)


class PDFParser(PaperParser):
    """Parse academic PDF papers using PyMuPDF."""

    # Populated by parse() so the pipeline can surface parser telemetry
    # (parse path used, fallback reason, etc.) to the frontend.
    last_parse_info: dict[str, object] = {}

    async def parse(self, file_path: Path, output_dir: Path) -> ParsedPaper:
        # The whole parse is CPU/IO heavy (fitz C calls, DPI rasterization,
        # pymupdf4llm layout analysis). Run it on the offload pool so the
        # event loop stays responsive — other HTTP requests, websocket
        # frames, the scheduler dispatcher all keep running.
        return await aoffload(self._parse_sync, file_path, output_dir)

    def _parse_sync(self, file_path: Path, output_dir: Path) -> ParsedPaper:
        output_dir.mkdir(parents=True, exist_ok=True)
        images_dir = output_dir / "images"
        images_dir.mkdir(exist_ok=True)

        info: dict[str, object] = {
            "layout_available": _pymupdf4llm is not None,
            "path": "heuristic",
            "fallback": False,
            "fallback_reason": None,
        }

        doc = fitz.open(str(file_path))
        try:
            size_map = self._analyze_font_sizes(doc)
            title = self._extract_title(doc, size_map)
            authors = self._extract_authors(doc)
            sections, abstract = self._extract_sections(doc, size_map, images_dir)

            # If the heuristic section extractor produced nothing useful
            # and pymupdf4llm (+ pymupdf-layout) is available, fall back
            # to its structured Markdown. Layout analysis runs under the
            # hood once `pymupdf.layout` is imported at module load.
            total_section_chars = sum(len(s.content) for s in sections)
            info["heuristic_section_chars"] = total_section_chars
            if _pymupdf4llm is not None and total_section_chars < 400:
                try:
                    md_text = _pymupdf4llm.to_markdown(doc)  # type: ignore[attr-defined]
                    if md_text and md_text.strip():
                        sections = [
                            PaperSection(title="Document", level=1, content=md_text)
                        ]
                        info["path"] = "pymupdf4llm"
                        info["fallback"] = True
                        info["fallback_reason"] = (
                            f"heuristic parser extracted only "
                            f"{total_section_chars} chars; using pymupdf4llm "
                            f"layout-enhanced Markdown."
                        )
                        info["pymupdf4llm_chars"] = len(md_text)
                except Exception as exc:
                    info["path"] = "heuristic"
                    info["fallback"] = False
                    info["fallback_error"] = str(exc)

            PDFParser.last_parse_info = info

            paper = ParsedPaper(
                title=title,
                authors=authors,
                abstract=abstract,
                sections=sections,
                source_type="pdf",
                figures_dir=images_dir,
            )
            self._write_figure_review_manifest(paper, images_dir)
            return paper
        finally:
            doc.close()

    # ── font-size analysis ────────────────────────────────────────────────────

    def _analyze_font_sizes(self, doc: fitz.Document) -> dict[str, float]:
        size_counter: Counter[float] = Counter()
        for page in doc:
            blocks = page.get_text("dict")["blocks"]
            for block in blocks:
                if block["type"] == 0:
                    for line in block["lines"]:
                        for span in line["spans"]:
                            size = round(span["size"], 1)
                            text = span["text"].strip()
                            if text:
                                size_counter[size] += len(text)

        if not size_counter:
            return {"body": 12, "h1": 24, "h2": 18, "h3": 14}

        sorted_sizes = sorted(size_counter.items(), key=lambda x: x[1], reverse=True)
        body_size = sorted_sizes[0][0]
        larger_sizes = sorted(
            [s for s in size_counter if s > body_size + 1], reverse=True
        )

        size_map: dict[str, float] = {"body": body_size}
        if len(larger_sizes) >= 1:
            size_map["h1"] = larger_sizes[0]
        if len(larger_sizes) >= 2:
            size_map["h2"] = larger_sizes[1]
        if len(larger_sizes) >= 3:
            size_map["h3"] = larger_sizes[2]
        return size_map

    def _get_heading_level(self, size: float, size_map: dict, text: str) -> int:
        text = text.strip()
        if len(text) > 80:
            return 0
        level = 0
        if "h1" in size_map and size >= size_map["h1"] - 0.5:
            level = 1
        elif "h2" in size_map and size >= size_map["h2"] - 0.5:
            level = 2
        elif "h3" in size_map and size >= size_map["h3"] - 0.5:
            level = 3
        return level

    # ── metadata ──────────────────────────────────────────────────────────────

    def _extract_title(self, doc: fitz.Document, size_map: dict) -> str:
        if not doc.page_count:
            return "Untitled"
        page = doc[0]
        blocks = page.get_text("dict")["blocks"]
        max_size = 0.0
        title_text = ""
        for block in blocks:
            if block["type"] == 0:
                for line in block["lines"]:
                    for span in line["spans"]:
                        if span["size"] > max_size and span["text"].strip():
                            max_size = span["size"]
                            title_text = span["text"].strip()
        return title_text or "Untitled"

    def _extract_authors(self, doc: fitz.Document) -> list[str]:
        if not doc.page_count:
            return []
        text = doc[0].get_text() or ""
        lines = text.split("\n")
        authors: list[str] = []
        for line in lines[1:10]:
            line = line.strip()
            if not line:
                continue
            if any(kw in line.lower() for kw in ["university", "department", "abstract", "@", "http"]):
                break
            if re.match(r"^[A-Za-z\s,.\-]+$", line) and len(line) < 200:
                authors.extend(a.strip() for a in line.split(",") if a.strip())
                if authors:
                    break
        return authors

    # ── main section + figure extraction ─────────────────────────────────────

    def _extract_sections(
        self,
        doc: fitz.Document,
        size_map: dict,
        images_dir: Path,
    ) -> tuple[list[PaperSection], str]:
        sections: list[PaperSection] = []
        abstract = ""
        current_section: PaperSection | None = None
        content_lines: list[str] = []
        img_counter = [0]  # mutable for nested helpers

        for page_idx, page in enumerate(doc):
            # ── 1. collect caption locations on this page ──────────────────
            caption_map = self._collect_captions(page)

            # ── 2. extract figures (raster + vector regions) ───────────────
            page_figures = self._extract_page_figures(
                doc, page, page_idx, images_dir, caption_map, img_counter
            )

            # ── 3. extract tables ──────────────────────────────────────────
            page_tables = self._extract_page_tables(page)

            # ── 4. extract text and build sections ─────────────────────────
            blocks = page.get_text("dict")["blocks"]
            for block in blocks:
                if block["type"] != 0:
                    continue
                for line in block["lines"]:
                    line_text = ""
                    line_size = 0.0
                    for span in line["spans"]:
                        line_text += span["text"]
                        line_size = max(line_size, span["size"])
                    line_text = line_text.strip()
                    if not line_text:
                        continue

                    heading_level = self._get_heading_level(line_size, size_map, line_text)

                    if heading_level > 0:
                        if current_section:
                            current_section.content = "\n".join(content_lines).strip()
                            sections.append(current_section)
                            content_lines = []

                        if line_text.lower().startswith("abstract"):
                            current_section = PaperSection(
                                title="Abstract",
                                level=heading_level,
                                content="",
                            )
                        else:
                            current_section = PaperSection(
                                title=line_text,
                                level=heading_level,
                                content="",
                                figures=page_figures[:],
                                tables=page_tables[:],
                            )
                            page_figures = []
                            page_tables = []
                    else:
                        content_lines.append(line_text)

            # leftover figures attach to current section
            if page_figures and current_section:
                current_section.figures.extend(page_figures)
            if page_tables and current_section:
                current_section.tables.extend(page_tables)

        if current_section:
            current_section.content = "\n".join(content_lines).strip()
            sections.append(current_section)

        # separate abstract
        new_sections: list[PaperSection] = []
        for section in sections:
            if section.title.lower() == "abstract":
                abstract = section.content
            else:
                new_sections.append(section)

        return new_sections, abstract

    # ── caption collection ────────────────────────────────────────────────────

    def _collect_captions(self, page: fitz.Page) -> dict[str, tuple[str, fitz.Rect]]:
        """Return mapping  label_key → (caption_text, rect)  for this page."""
        result: dict[str, tuple[str, fitz.Rect]] = {}
        blocks = page.get_text("dict")["blocks"]
        for block in blocks:
            if block["type"] != 0:
                continue
            block_text = " ".join(
                span["text"]
                for line in block["lines"]
                for span in line["spans"]
            ).strip()
            m = _CAPTION_RE.search(block_text)
            if m:
                key = re.sub(r"\s+", "", m.group(0).lower())
                rect = fitz.Rect(block["bbox"])
                result[key] = (block_text, rect)
        return result

    def _nearest_caption(
        self,
        img_rect: fitz.Rect,
        caption_map: dict[str, tuple[str, fitz.Rect]],
    ) -> str:
        """Return the caption text closest to img_rect, or ''."""
        best_dist = float("inf")
        best_cap = ""
        for _key, (cap_text, cap_rect) in caption_map.items():
            # vertical distance between centres
            dist = abs(cap_rect.y0 - img_rect.y1)
            if dist < best_dist and dist < CAPTION_SEARCH_PX * 2:
                best_dist = dist
                best_cap = cap_text
        return best_cap

    # ── raster + vector figure extraction ────────────────────────────────────

    def _extract_page_figures(
        self,
        doc: fitz.Document,
        page: fitz.Page,
        page_idx: int,
        images_dir: Path,
        caption_map: dict[str, tuple[str, fitz.Rect]],
        counter: list[int],
    ) -> list[PaperFigure]:
        figures: list[PaperFigure] = []
        seen_hashes: set[str] = set()

        page_rect = page.rect
        text_blocks = self._collect_text_blocks(page)
        drawing_rects = self._collect_drawing_rects(page)

        # ── pass A: displayed raster images only (avoid hidden XObjects) ───
        raster_rects: list[fitz.Rect] = []
        image_infos = page.get_image_info(xrefs=True) if hasattr(page, "get_image_info") else []
        for img_info in image_infos:
            bbox = fitz.Rect(img_info.get("bbox", (0, 0, 0, 0)))
            xref = int(img_info.get("xref", 0) or 0)
            if xref <= 0:
                continue
            if not self._is_meaningful_image_candidate(page_rect, bbox, img_info):
                continue

            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.width < MIN_IMG_PX or pix.height < MIN_IMG_PX:
                    continue
                if pix.n > 4:
                    pix = fitz.Pixmap(fitz.csRGB, pix)

                img_bytes = pix.tobytes("png")
                h = hashlib.md5(img_bytes).hexdigest()[:12]
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)

                raster_rects.append(bbox)
                counter[0] += 1
                img_path = images_dir / f"fig_{counter[0]:03d}_p{page_idx + 1}.png"
                img_path.write_bytes(img_bytes)

                caption = self._nearest_caption(bbox, caption_map)
                score, flags = self._score_figure_region(
                    bbox,
                    page_rect,
                    text_blocks,
                    drawing_rects + raster_rects,
                    caption_map,
                )
                figures.append(
                    PaperFigure(
                        path=img_path,
                        caption=caption,
                        available=True,
                        page_number=page_idx + 1,
                        bbox=self._rect_tuple(bbox),
                        extraction_method="embedded_image",
                        quality_score=score,
                        review_flags=flags,
                        natural_width=int(pix.width),
                        natural_height=int(pix.height),
                    )
                )
            except Exception:
                continue

        # ── pass A.5: render every detected table as its own figure ─────────
        # ``page.find_tables()`` already gave us a tight bbox for each table.
        # Without this pass, three-line scientific tables (which have no raster
        # image and very few drawing rects) fall all the way through to the
        # whole-page fallback, producing the "screenshot of the entire page"
        # bug. Rendering ``tab.bbox`` directly gives us exactly the table area.
        for tab in self._iter_page_tables(page):
            try:
                tab_bbox = fitz.Rect(tab.bbox)
            except Exception:
                continue
            if tab_bbox.width < MIN_RENDERED_FIGURE_SIDE or tab_bbox.height < MIN_RENDERED_FIGURE_SIDE:
                continue
            # Pad slightly so axis labels / borders are not clipped.
            clip = fitz.Rect(
                max(page_rect.x0, tab_bbox.x0 - 6),
                max(page_rect.y0, tab_bbox.y0 - 6),
                min(page_rect.x1, tab_bbox.x1 + 6),
                min(page_rect.y1, tab_bbox.y1 + 6),
            )
            if self._is_region_already_covered(clip, raster_rects):
                continue
            try:
                mat = fitz.Matrix(REGION_DPI / 72, REGION_DPI / 72)
                pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
                img_bytes = pix.tobytes("png")
                h = hashlib.md5(img_bytes).hexdigest()[:12]
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)

                counter[0] += 1
                img_path = images_dir / f"fig_{counter[0]:03d}_p{page_idx + 1}_table.png"
                img_path.write_bytes(img_bytes)

                # Look for a "Table N" caption near this region.
                caption = self._nearest_caption(clip, caption_map)
                figures.append(
                    PaperFigure(
                        path=img_path,
                        caption=caption,
                        available=True,
                        page_number=page_idx + 1,
                        bbox=self._rect_tuple(clip),
                        extraction_method="table_region",
                        quality_score=0.92,
                        review_flags=[],
                        natural_width=int(pix.width),
                        natural_height=int(pix.height),
                    )
                )
                # Mark this region as covered so caption-region pass B does
                # not try to render the same table area again.
                raster_rects.append(clip)
            except Exception:
                continue

        graphic_rects = drawing_rects + raster_rects

        # ── pass B: caption-anchored object-region render ──────────────────
        for cap_text, cap_rect in caption_map.values():
            clip = self._build_caption_anchored_clip(
                page_rect,
                cap_rect,
                text_blocks,
                graphic_rects,
            )
            if clip is None:
                continue

            if self._is_region_already_covered(clip, raster_rects):
                continue

            try:
                mat = fitz.Matrix(REGION_DPI / 72, REGION_DPI / 72)
                pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
                img_bytes = pix.tobytes("png")
                h = hashlib.md5(img_bytes).hexdigest()[:12]
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)

                counter[0] += 1
                img_path = images_dir / f"fig_{counter[0]:03d}_p{page_idx + 1}_vec.png"
                img_path.write_bytes(img_bytes)
                score, flags = self._score_figure_region(
                    clip,
                    page_rect,
                    text_blocks,
                    graphic_rects,
                    caption_map,
                )
                figures.append(
                    PaperFigure(
                        path=img_path,
                        caption=cap_text,
                        available=True,
                        page_number=page_idx + 1,
                        bbox=self._rect_tuple(clip),
                        extraction_method="caption_region",
                        quality_score=score,
                        review_flags=flags,
                        natural_width=int(pix.width),
                        natural_height=int(pix.height),
                    )
                )
            except Exception:
                continue

        # ── pass C: whole-page fallback only when all candidates look weak ─
        # Tightened: a single decent table-region or caption-region figure is
        # enough to suppress the whole-page screenshot. Prior threshold was
        # 0.45 which let three-line tables (which we now extract directly via
        # pass A.5) avoid this fallback entirely; we keep the same threshold
        # but also short-circuit when *any* figure was produced via the more
        # reliable table_region / caption_region paths.
        reliable_methods = {"embedded_image", "table_region", "caption_region"}
        has_reliable = any(
            fig.extraction_method in reliable_methods and fig.quality_score >= 0.45
            for fig in figures
        )
        if caption_map and not has_reliable and not any(fig.quality_score >= 0.45 for fig in figures):
            try:
                mat = fitz.Matrix(RENDER_DPI / 72, RENDER_DPI / 72)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img_bytes = pix.tobytes("png")
                h = hashlib.md5(img_bytes).hexdigest()[:12]
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    counter[0] += 1
                    img_path = images_dir / f"fig_{counter[0]:03d}_p{page_idx + 1}_page.png"
                    img_path.write_bytes(img_bytes)
                    first_cap = next(iter(caption_map.values()))[0]
                    score, flags = self._score_figure_region(
                        page_rect,
                        page_rect,
                        text_blocks,
                        graphic_rects,
                        caption_map,
                    )
                    figures.append(
                        PaperFigure(
                            path=img_path,
                            caption=first_cap,
                            available=True,
                            page_number=page_idx + 1,
                            bbox=self._rect_tuple(page_rect),
                            extraction_method="page_fallback",
                            quality_score=score,
                            review_flags=flags + ["page_level_fallback"],
                            natural_width=int(pix.width),
                            natural_height=int(pix.height),
                        )
                    )
            except Exception:
                pass

        return figures

    def _collect_text_blocks(self, page: fitz.Page) -> list[dict]:
        blocks: list[dict] = []
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            text = " ".join(
                span["text"]
                for line in block.get("lines", [])
                for span in line.get("spans", [])
                if span.get("text", "").strip()
            ).strip()
            if not text:
                continue
            blocks.append(
                {
                    "rect": fitz.Rect(block["bbox"]),
                    "text": text,
                    "char_count": len(text),
                    "line_count": len(block.get("lines", [])),
                }
            )
        return blocks

    def _collect_drawing_rects(self, page: fitz.Page) -> list[fitz.Rect]:
        rects: list[fitz.Rect] = []
        if not hasattr(page, "get_drawings"):
            return rects
        try:
            for drawing in page.get_drawings():
                raw_rect = drawing.get("rect")
                if not raw_rect:
                    continue
                rect = fitz.Rect(raw_rect)
                if rect.width < MIN_RENDERED_FIGURE_SIDE / 2 or rect.height < MIN_RENDERED_FIGURE_SIDE / 2:
                    continue
                rects.append(rect)
        except Exception:
            return rects
        return rects

    def _is_meaningful_image_candidate(
        self,
        page_rect: fitz.Rect,
        bbox: fitz.Rect,
        img_info: dict,
    ) -> bool:
        if bbox.is_empty or bbox.width < MIN_RENDERED_FIGURE_SIDE or bbox.height < MIN_RENDERED_FIGURE_SIDE:
            return False

        area_ratio = (bbox.get_area() / page_rect.get_area()) if page_rect.get_area() else 0
        if area_ratio < MIN_GRAPHIC_AREA_RATIO:
            return False

        width = float(img_info.get("width", 0) or 0)
        height = float(img_info.get("height", 0) or 0)
        if width and height:
            if width < MIN_IMG_PX or height < MIN_IMG_PX:
                return False

        return True

    def _build_caption_anchored_clip(
        self,
        page_rect: fitz.Rect,
        caption_rect: fitz.Rect,
        text_blocks: list[dict],
        graphic_rects: list[fitz.Rect],
    ) -> fitz.Rect | None:
        search_top = max(page_rect.y0, caption_rect.y0 - MAX_CAPTION_GAP_PX)
        graphic_candidates = [
            rect
            for rect in graphic_rects
            if rect.y1 <= caption_rect.y0 + 12
            and rect.y0 >= search_top
            and rect.width >= MIN_RENDERED_FIGURE_SIDE
            and rect.height >= MIN_RENDERED_FIGURE_SIDE / 2
        ]
        if not graphic_candidates:
            return None

        caption_center_x = (caption_rect.x0 + caption_rect.x1) / 2
        seed = min(
            graphic_candidates,
            key=lambda rect: abs(rect.y1 - caption_rect.y0) + abs(rect.x0 + rect.width / 2 - caption_center_x) * 0.35,
        )
        cluster = [seed]
        cluster_rect = fitz.Rect(seed)

        expanded = True
        while expanded:
            expanded = False
            proximity = fitz.Rect(
                cluster_rect.x0 - 32,
                cluster_rect.y0 - 32,
                cluster_rect.x1 + 32,
                min(caption_rect.y0 - 4, cluster_rect.y1 + 32),
            )
            for rect in graphic_candidates:
                if rect in cluster:
                    continue
                if proximity.intersects(rect):
                    cluster.append(rect)
                    cluster_rect.include_rect(rect)
                    expanded = True

        label_zone = fitz.Rect(
            max(page_rect.x0, cluster_rect.x0 - 28),
            max(page_rect.y0, cluster_rect.y0 - 28),
            min(page_rect.x1, cluster_rect.x1 + 28),
            min(caption_rect.y0 - 4, cluster_rect.y1 + 28),
        )
        for block in text_blocks:
            rect = block["rect"]
            if not label_zone.intersects(rect):
                continue
            if block["char_count"] > LABEL_TEXT_CHARS_THRESHOLD and block["line_count"] > 3:
                continue
            cluster_rect.include_rect(rect)

        clip = fitz.Rect(
            max(page_rect.x0, cluster_rect.x0 - 18),
            max(page_rect.y0, cluster_rect.y0 - 18),
            min(page_rect.x1, cluster_rect.x1 + 18),
            min(caption_rect.y0 - 4, cluster_rect.y1 + 18),
        )
        if clip.width < MIN_RENDERED_FIGURE_SIDE or clip.height < MIN_RENDERED_FIGURE_SIDE:
            return None
        return clip

    def _is_region_already_covered(self, region: fitz.Rect, raster_rects: list[fitz.Rect]) -> bool:
        for rect in raster_rects:
            if not region.intersects(rect):
                continue
            overlap = region.intersect(rect).get_area()
            if overlap >= region.get_area() * 0.78:
                return True
        return False

    def _score_figure_region(
        self,
        rect: fitz.Rect,
        page_rect: fitz.Rect,
        text_blocks: list[dict],
        graphic_rects: list[fitz.Rect],
        caption_map: dict[str, tuple[str, fitz.Rect]],
    ) -> tuple[float, list[str]]:
        flags: list[str] = []
        body_text_chars = 0
        label_text_chars = 0
        graphics_overlap_area = 0.0

        for block in text_blocks:
            block_rect = block["rect"]
            if not rect.intersects(block_rect):
                continue
            overlap = rect.intersect(block_rect).get_area()
            if overlap <= 0:
                continue
            if block["char_count"] >= BODY_TEXT_CHARS_THRESHOLD and block["line_count"] >= 3:
                body_text_chars += block["char_count"]
            else:
                label_text_chars += block["char_count"]

        for graphic_rect in graphic_rects:
            if not rect.intersects(graphic_rect):
                continue
            graphics_overlap_area += rect.intersect(graphic_rect).get_area()

        graphic_coverage = graphics_overlap_area / rect.get_area() if rect.get_area() else 0.0
        if body_text_chars > max(120, label_text_chars * 1.5):
            flags.append("body_text_intrusion")
        if graphic_coverage < 0.08:
            flags.append("low_graphic_coverage")
        if rect.width < MIN_RENDERED_FIGURE_SIDE or rect.height < MIN_RENDERED_FIGURE_SIDE:
            flags.append("too_small")
        if rect.x0 <= page_rect.x0 + 4 or rect.x1 >= page_rect.x1 - 4:
            flags.append("touches_page_edge")
        if rect.y0 <= page_rect.y0 + 4 or rect.y1 >= page_rect.y1 - 4:
            flags.append("touches_page_edge")

        caption_gap = None
        for _, cap_rect in caption_map.values():
            if rect.y1 <= cap_rect.y0:
                gap = cap_rect.y0 - rect.y1
                caption_gap = gap if caption_gap is None else min(caption_gap, gap)
        if caption_gap is not None and caption_gap > 80:
            flags.append("caption_far")

        score = 0.9
        score -= min(body_text_chars / 500, 0.45)
        score += min(graphic_coverage, 0.25)
        score -= 0.08 * flags.count("touches_page_edge")
        if "low_graphic_coverage" in flags:
            score -= 0.2
        if "too_small" in flags:
            score -= 0.2
        if "caption_far" in flags:
            score -= 0.08
        return max(0.0, min(1.0, score)), sorted(set(flags))

    @staticmethod
    def _rect_tuple(rect: fitz.Rect) -> tuple[float, float, float, float]:
        return (round(rect.x0, 2), round(rect.y0, 2), round(rect.x1, 2), round(rect.y1, 2))

    def _write_figure_review_manifest(self, paper: ParsedPaper, images_dir: Path) -> None:
        records = []
        for figure in paper.all_figures():
            records.append(
                {
                    "path": str(figure.path),
                    "page_number": figure.page_number,
                    "caption": figure.caption,
                    "bbox": figure.bbox,
                    "extraction_method": figure.extraction_method,
                    "quality_score": figure.quality_score,
                    "review_flags": figure.review_flags,
                    "natural_width": figure.natural_width,
                    "natural_height": figure.natural_height,
                    "aspect_ratio": round(figure.aspect_ratio, 4) if figure.aspect_ratio else None,
                }
            )

        if not records:
            return

        manifest_path = images_dir / "figure_review.json"
        manifest_path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── table extraction ──────────────────────────────────────────────────────

    def _extract_page_tables(self, page: fitz.Page) -> list[PaperTable]:
        """Extract tables as Markdown using find_tables() (PyMuPDF ≥ 1.23)."""
        tables: list[PaperTable] = []
        if not hasattr(page, "find_tables"):
            return tables
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                rows = tab.extract()
                if not rows:
                    continue
                md = self._rows_to_markdown(rows)
                if md:
                    tables.append(PaperTable(markdown=md))
        except Exception:
            pass
        return tables

    def _iter_page_tables(self, page: fitz.Page):
        """Yield ``page.find_tables()`` entries with a usable ``.bbox`` attribute.

        Wrapped so callers don't have to reproduce the ``hasattr`` /
        try/except dance and we can swap the backing implementation if the
        layout-aware extractor is available.
        """
        if not hasattr(page, "find_tables"):
            return
        try:
            tab_finder = page.find_tables()
            for tab in tab_finder.tables:
                if getattr(tab, "bbox", None) is None:
                    continue
                yield tab
        except Exception:
            return

    @staticmethod
    def _rows_to_markdown(rows: list[list[str | None]]) -> str:
        if not rows:
            return ""
        # header
        header = [str(c or "") for c in rows[0]]
        sep = ["---"] * len(header)
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for row in rows[1:]:
            cells = [str(c or "") for c in row]
            # pad to header width
            while len(cells) < len(header):
                cells.append("")
            lines.append("| " + " | ".join(cells[:len(header)]) + " |")
        return "\n".join(lines)
