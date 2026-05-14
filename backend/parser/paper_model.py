"""Data models for parsed academic papers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass
class PaperFigure:
    """A figure extracted from a paper."""

    path: Path
    caption: str = ""
    label: str | None = None
    available: bool = True  # False when the file could not be located on disk
    page_number: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    extraction_method: str = "unknown"
    quality_score: float = 0.0
    review_flags: list[str] = field(default_factory=list)
    # Natural pixel dimensions of the saved file. Populated by the parser for
    # review/export metadata without requiring the model to guess image files.
    natural_width: int = 0
    natural_height: int = 0

    @property
    def aspect_ratio(self) -> float:
        """width / height, or 0 when unknown."""
        if self.natural_width > 0 and self.natural_height > 0:
            return self.natural_width / self.natural_height
        return 0.0

    @property
    def fig_id(self) -> str:
        """Stable identifier used in `[[FIG:id]]` tokens.

        Derived from the file stem so it survives across pipeline stages
        without needing extra plumbing. The token is the contract between
        ``research_agent`` (which writes them into the manuscript) and
        ``svg_executor`` (which resolves them to real image hrefs before
        the LLM sees the page).
        """
        return self.path.stem


@dataclass
class PaperTable:
    """A table extracted from a paper."""

    markdown: str  # Markdown-formatted table
    caption: str = ""


@dataclass
class PaperSection:
    """A section of an academic paper."""

    title: str
    level: int  # 1=section, 2=subsection, 3=subsubsection
    content: str = ""  # Markdown text content
    figures: list[PaperFigure] = field(default_factory=list)
    tables: list[PaperTable] = field(default_factory=list)
    equations: list[str] = field(default_factory=list)


@dataclass
class ParsedPaper:
    """A fully parsed academic paper."""

    title: str
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    sections: list[PaperSection] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    source_type: Literal["pdf", "latex"] = "pdf"
    figures_dir: Path | None = None

    def all_figures(self) -> list[PaperFigure]:
        """Collect all figures from all sections."""
        figs = []
        for section in self.sections:
            figs.extend(section.figures)
        return figs

    @staticmethod
    def _should_include_figure(fig: PaperFigure) -> bool:
        if not fig.available:
            return False

        suspicious = {"body_text_intrusion", "low_graphic_coverage", "page_level_fallback"}
        if fig.review_flags and suspicious.intersection(fig.review_flags) and fig.quality_score < 0.55:
            return False
        if (
            fig.extraction_method == "caption_region"
            and "body_text_intrusion" in fig.review_flags
            and fig.quality_score < 0.75
        ):
            return False
        if "low_graphic_coverage" in fig.review_flags and fig.quality_score < 0.65:
            return False
        if (
            fig.extraction_method == "embedded_image"
            and "too_small" in fig.review_flags
        ):
            return False
        return True

    def to_markdown(self) -> str:
        """Convert the parsed paper to a single Markdown document."""
        parts = []

        # Title and metadata
        parts.append(f"# {self.title}\n")
        if self.authors:
            parts.append(f"**Authors:** {', '.join(self.authors)}\n")
        if self.abstract:
            parts.append(f"## Abstract\n\n{self.abstract}\n")

        # Sections
        for section in self.sections:
            prefix = "#" * (section.level + 1)
            parts.append(f"{prefix} {section.title}\n")
            if section.content:
                parts.append(section.content)

            for fig in section.figures:
                if not self._should_include_figure(fig):
                    continue
                caption = (fig.caption or "Figure").replace("\n", " ").strip()
                size_hint = ""
                if fig.natural_width > 0 and fig.natural_height > 0:
                    size_hint = (
                        f" (natural {fig.natural_width}×{fig.natural_height}px,"
                        f" ratio {fig.aspect_ratio:.3f})"
                    )
                # Emit a stable `[[FIG:id]]` token instead of a markdown image.
                # The research agent must reproduce this token verbatim when
                # it references this figure on a slide. The executor later
                # resolves the token to a concrete `<image href=...>` so the
                # model never has to guess between figures.
                parts.append(
                    f"\n[[FIG:{fig.fig_id}]] — {caption}{size_hint}\n"
                )

            for table in section.tables:
                if table.caption:
                    parts.append(f"\n*{table.caption}*\n")
                parts.append(f"\n{table.markdown}\n")

        # References
        if self.references:
            parts.append("## References\n")
            for i, ref in enumerate(self.references, 1):
                parts.append(f"{i}. {ref}")

        return "\n".join(parts)
